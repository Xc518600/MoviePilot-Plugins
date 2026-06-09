# -*- coding: utf-8 -*-
"""
Emby/Jellyfin/Plex 播放自动限速 MoviePilot 下载器插件
直接调用 MoviePilot 已配置的媒体服务器和下载器，播放时限速，停止后恢复。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import threading
import time

from app.core.event import eventmanager, Event
from app.helper.downloader import DownloaderHelper
from app.helper.mediaserver import MediaServerHelper
from app.helper.message import MessageHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType


class EmbyQBLimit(_PluginBase):
    # 插件基本信息
    plugin_name = "Emby自动限速"
    plugin_desc = "监听媒体服务器真实播放会话，播放时自动限速，停止后恢复"
    plugin_version = "2.4.2"
    plugin_author = "老公"
    plugin_description = "监听MoviePilot媒体服务器Webhook并查询真实播放会话，播放时自动限速已配置下载器，停止后恢复"
    plugin_icon = "play_circle_outline.png"
    plugin_level = 1
    auth_level = 1
    plugin_config_prefix = "embyqblimit_"

    # 配置
    _enabled = False
    _downloader = ""
    _media_server = ""
    _qb_download_limit = 1024
    _qb_upload_limit = 1024
    _restore_on_stop = True
    _restore_download_limit = 0
    _restore_upload_limit = 0
    _check_interval = 10
    _notify = True
    _notify_type = "Plugin"
    _whitelist_users = ""
    _whitelist_devices = ""

    # 运行时状态
    _is_playing = False
    _original_download_limit = 0
    _original_upload_limit = 0
    _last_playback_check = 0
    _last_playing_title = ""
    _monitor_thread = None
    _stop_event = threading.Event()
    _message_helper = None

    def init_plugin(self, config: dict = None) -> bool:
        """初始化插件"""
        self.stop_service()

        if config:
            self._enabled = config.get("enabled", False)
            self._downloader = config.get("downloader") or config.get("qb_downloader") or ""
            self._media_server = config.get("media_server") or config.get("mediaserver") or ""
            self._qb_download_limit = int(config.get("qb_download_limit") or 1024)
            self._qb_upload_limit = int(config.get("qb_upload_limit") or 1024)
            self._restore_on_stop = config.get("restore_on_stop", True)
            self._restore_download_limit = int(config.get("restore_download_limit") or 0)
            self._restore_upload_limit = int(config.get("restore_upload_limit") or 0)
            self._check_interval = max(int(config.get("check_interval") or 10), 5)
            self._notify = config.get("notify", True)
            self._notify_type = config.get("notify_type", "Plugin")
            self._whitelist_users = config.get("whitelist_users", "")
            self._whitelist_devices = config.get("whitelist_devices", "")

        try:
            self._message_helper = MessageHelper()
        except Exception:
            self._message_helper = None

        logger.info(
            f"Emby自动限速配置加载：enabled={self._enabled}, "
            f"downloader={self._downloader or '未选择'}, media_server={self._media_server or '未选择'}, "
            f"download_limit={self._qb_download_limit}KB/s, upload_limit={self._qb_upload_limit}KB/s, "
            f"restore_on_stop={self._restore_on_stop}, check_interval={self._check_interval}s"
        )

        if self._enabled:
            if not self._downloader:
                logger.warning("Emby自动限速未选择下载器，监控不会启动")
                return True
            if not self._media_server:
                logger.warning("Emby自动限速未选择媒体服务器，监控不会启动")
                return True
            self.start_monitor()

        return True

    def get_state(self) -> bool:
        """获取插件状态"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """远程命令列表"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """插件 API 列表"""
        return []

    def stop_service(self):
        """停止服务"""
        self._stop_event.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5)
        self._monitor_thread = None
        logger.info("EmbyQB限速插件已停止")

    def start_monitor(self):
        """启动监控"""
        if self._monitor_thread and self._monitor_thread.is_alive():
            return

        self._stop_event.clear()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        logger.info(f"EmbyQB限速监控已启动，媒体服务器：{self._media_server}，下载器：{self._downloader}")

    def _monitor_loop(self):
        """监控循环：作为 Webhook 事件的兜底，查询真实播放会话。"""
        while not self._stop_event.is_set():
            try:
                if not self._enabled:
                    time.sleep(5)
                    continue
                self._refresh_play_state(source="轮询")
                self._last_playback_check = time.time()
                self._stop_event.wait(self._check_interval)
            except Exception as e:
                logger.error(f"EmbyQB限速监控异常: {str(e)}")
                self._stop_event.wait(30)

    @eventmanager.register(EventType.WebhookMessage)
    def check_playing_sessions(self, event: Event = None):
        """媒体服务器 Webhook 触发后立即刷新真实播放状态。"""
        if not self._enabled:
            return
        if not event:
            return
        event_data = event.event_data
        event_name = getattr(event_data, "event", "")
        channel = getattr(event_data, "channel", "")
        if event_name not in [
            "playback.start", "PlaybackStart", "media.play",
            "playback.stop", "PlaybackStop", "media.stop",
            "playback.pause", "PlaybackPause", "media.pause"
        ]:
            return
        media_conf = self._get_media_server_config()
        media_type = getattr(media_conf, "type", "") if media_conf else ""
        if channel and media_type and channel != media_type:
            return
        logger.info(f"收到媒体服务器播放事件 {channel}:{event_name}，刷新限速状态")
        self._refresh_play_state(source="Webhook")

    def _refresh_play_state(self, source: str = "轮询"):
        """根据真实播放会话切换限速状态。"""
        is_playing = self._check_media_server_playing()
        if is_playing and not self._is_playing:
            logger.info(f"{source}检测到媒体服务器 {self._media_server} 开始播放，开始限速下载器 {self._downloader}")
            self._send_notification("开始播放，正在限速...")
            self._apply_limit()
            self._is_playing = True
        elif not is_playing and self._is_playing:
            logger.info(f"{source}检测到媒体服务器 {self._media_server} 停止播放，恢复下载器 {self._downloader} 速度")
            if self._restore_on_stop:
                self._restore_limit()
            self._send_notification("停止播放，已恢复原速")
            self._is_playing = False
            self._last_playing_title = ""

    def _get_downloader_service(self):
        """获取 MoviePilot 下载器服务"""
        if not self._downloader:
            return None
        return DownloaderHelper().get_service(name=self._downloader)

    def _get_media_server_config(self):
        """获取 MoviePilot 媒体服务器配置"""
        if not self._media_server:
            return None
        return MediaServerHelper().get_config(name=self._media_server)

    @staticmethod
    def _csv_contains(value: str, csv_text: str) -> bool:
        """逗号分隔字符串包含判断"""
        if not csv_text:
            return True
        allow_list = [item.strip() for item in csv_text.split(",") if item.strip()]
        if not allow_list:
            return True
        return value in allow_list

    def _check_media_server_playing(self) -> bool:
        """检查 MoviePilot 已配置媒体服务器的真实播放会话。"""
        try:
            service = MediaServerHelper().get_service(name=self._media_server)
            if not service or not service.instance:
                logger.warning(f"获取媒体服务器失败: {self._media_server}")
                return False

            if service.type == "emby":
                return self._check_emby_sessions(service)
            if service.type == "jellyfin":
                return self._check_jellyfin_sessions(service)
            if service.type == "plex":
                return self._check_plex_sessions(service)

            logger.warning(f"不支持的媒体服务器类型: {service.type}")
            return False
        except Exception as e:
            logger.error(f"检查媒体服务器播放状态失败: {str(e)}")
            return False

    def _check_emby_sessions(self, service) -> bool:
        """查询 Emby 真实 Sessions。"""
        res = service.instance.get_data("[HOST]emby/Sessions?api_key=[APIKEY]")
        logger.info(f"Emby自动限速查询 Emby Sessions 状态：{getattr(res, 'status_code', None)}")
        if not res or res.status_code != 200:
            return False
        for session in res.json() or []:
            if self._is_valid_emby_like_session(session):
                item = session.get("NowPlayingItem") or {}
                self._last_playing_title = item.get("Name") or item.get("OriginalTitle") or "未知媒体"
                return True
        return False

    def _check_jellyfin_sessions(self, service) -> bool:
        """查询 Jellyfin 真实 Sessions。"""
        res = service.instance.get_data("[HOST]Sessions?api_key=[APIKEY]")
        logger.info(f"Emby自动限速查询 Jellyfin Sessions 状态：{getattr(res, 'status_code', None)}")
        if not res or res.status_code != 200:
            return False
        for session in res.json() or []:
            if self._is_valid_emby_like_session(session):
                item = session.get("NowPlayingItem") or {}
                self._last_playing_title = item.get("Name") or item.get("OriginalTitle") or "未知媒体"
                return True
        return False

    def _check_plex_sessions(self, service) -> bool:
        """查询 Plex 真实 Sessions。"""
        plex = service.instance.get_plex()
        if not plex:
            return False
        for session in plex.sessions() or []:
            session_type = getattr(session, "TAG", "") or getattr(session, "type", "")
            if session_type and str(session_type).lower() != "video":
                continue
            player = getattr(session, "player", None)
            state = getattr(player, "state", "") if player else ""
            if state and str(state).lower() == "paused":
                continue
            username = getattr(getattr(session, "user", None), "title", "") or getattr(session, "username", "") or ""
            device = getattr(player, "title", "") if player else ""
            if self._whitelist_users and username and not self._csv_contains(username, self._whitelist_users):
                continue
            if self._whitelist_devices and device and not self._csv_contains(device, self._whitelist_devices):
                continue
            self._last_playing_title = getattr(session, "title", "") or getattr(session, "grandparentTitle", "") or "未知媒体"
            return True
        return False

    def _is_valid_emby_like_session(self, session: dict) -> bool:
        """判断 Emby/Jellyfin session 是否为有效播放中视频。"""
        if not session.get("NowPlayingItem"):
            return False
        if session.get("PlayState", {}).get("IsPaused"):
            return False
        item = session.get("NowPlayingItem") or {}
        if item.get("MediaType") and item.get("MediaType") != "Video":
            return False
        username = session.get("UserName", "")
        device = session.get("DeviceName", "")
        if self._whitelist_users and username and not self._csv_contains(username, self._whitelist_users):
            return False
        if self._whitelist_devices and device and not self._csv_contains(device, self._whitelist_devices):
            return False
        return True

    def _apply_limit(self):
        """应用限速"""
        try:
            self._save_current_limits()
            self._set_downloader_limits(
                download_limit=self._qb_download_limit,
                upload_limit=self._qb_upload_limit
            )
        except Exception as e:
            logger.error(f"应用限速失败: {str(e)}")

    def _restore_limit(self):
        """恢复原速"""
        try:
            dl = self._restore_download_limit if self._restore_download_limit is not None else self._original_download_limit
            ul = self._restore_upload_limit if self._restore_upload_limit is not None else self._original_upload_limit

            self._set_downloader_limits(download_limit=dl, upload_limit=ul)
            logger.info(f"已恢复下载器限速: 下载={dl}KB/s, 上传={ul}KB/s")
        except Exception as e:
            logger.error(f"恢复下载器限速失败: {str(e)}")

    def _save_current_limits(self):
        """保存当前下载器限速设置"""
        try:
            service = self._get_downloader_service()
            if not service or not service.instance:
                logger.error(f"获取下载器失败: {self._downloader}")
                return
            if not hasattr(service.instance, "get_speed_limit"):
                logger.error(f"下载器 {self._downloader} 不支持读取限速")
                return

            limits = service.instance.get_speed_limit()
            if not limits:
                return
            self._original_download_limit = int(limits[0] or 0)
            self._original_upload_limit = int(limits[1] or 0)
            logger.info(f"已保存下载器原限速: 下载={self._original_download_limit}KB/s, 上传={self._original_upload_limit}KB/s")
        except Exception as e:
            logger.error(f"保存下载器限速设置失败: {str(e)}")

    def _set_downloader_limits(self, download_limit: int, upload_limit: int):
        """设置 MoviePilot 下载器限速"""
        try:
            service = self._get_downloader_service()
            if not service or not service.instance:
                raise RuntimeError(f"获取下载器失败: {self._downloader}")
            if not hasattr(service.instance, "set_speed_limit"):
                raise RuntimeError(f"下载器 {self._downloader} 不支持设置限速")

            ok = service.instance.set_speed_limit(
                download_limit=int(download_limit or 0),
                upload_limit=int(upload_limit or 0)
            )
            if ok is False:
                raise RuntimeError("下载器返回设置失败")

            logger.info(f"已应用下载器限速: 下载={download_limit}KB/s, 上传={upload_limit}KB/s")
            if int(download_limit or 0) > 0 or int(upload_limit or 0) > 0:
                self._send_notification(f"已应用限速: 下载={download_limit}KB/s, 上传={upload_limit}KB/s")
        except Exception as e:
            logger.error(f"设置下载器限速失败: {str(e)}")
            self._send_notification(f"设置限速失败: {str(e)}")

    def _send_notification(self, message: str):
        """发送通知消息"""
        if not self._notify:
            return
        try:
            self.post_message(
                mtype=getattr(NotificationType, self._notify_type, NotificationType.Plugin),
                title="Emby自动限速",
                text=message
            )
        except Exception:
            try:
                if self._message_helper:
                    self._message_helper.put(title="Emby自动限速", message=message)
            except Exception as e:
                logger.error(f"发送通知失败: {str(e)}")

    @staticmethod
    def _service_items(services: Dict[str, Any]) -> List[Dict[str, str]]:
        """服务字典转 VSelect items"""
        items = []
        for name, conf in services.items():
            service_type = getattr(conf, "type", "") or ""
            title = f"{name} ({service_type})" if service_type else name
            items.append({"title": title, "value": name})
        return items

    def get_form(self) -> Tuple[list, dict]:
        """获取配置表单"""
        downloader_items = self._service_items(DownloaderHelper().get_configs())
        mediaserver_items = self._service_items(MediaServerHelper().get_configs())

        default_downloader = self._downloader or (downloader_items[0]["value"] if downloader_items else "")
        default_media_server = self._media_server or (mediaserver_items[0]["value"] if mediaserver_items else "")

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "class": "mb-4"
                        },
                        "text": "下载器和媒体服务器直接使用 MoviePilot 已配置服务；这里只需要选择，不需要重复填写地址、账号或 API Key。"
                    },
                    {
                        "component": "VCard",
                        "props": {"variant": "outlined", "class": "mb-4"},
                        "content": [
                            {"component": "VCardTitle", "props": {"class": "text-h6"}, "text": "基础设置"},
                            {
                                "component": "VCardText",
                                "content": [
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件", "color": "primary"}}]
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [{"component": "VSwitch", "props": {"model": "notify", "label": "发送通知", "color": "primary"}}]
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [{"component": "VSelect", "props": {
                                                    "model": "notify_type", "label": "通知类型",
                                                    "items": [
                                                        {"title": "插件", "value": "Plugin"},
                                                        {"title": "系统", "value": "System"},
                                                        {"title": "站点", "value": "SiteMessage"},
                                                    ]
                                                }}]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VCard",
                        "props": {"variant": "outlined", "class": "mb-4"},
                        "content": [
                            {"component": "VCardTitle", "props": {"class": "text-h6"}, "text": "MoviePilot 服务选择"},
                            {
                                "component": "VCardText",
                                "content": [
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 6},
                                                "content": [{"component": "VSelect", "props": {
                                                    "model": "downloader", "label": "下载器",
                                                    "items": downloader_items,
                                                    "hint": "来自 MoviePilot 系统设置里已启用的下载器",
                                                    "persistent-hint": True
                                                }}]
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 6},
                                                "content": [{"component": "VSelect", "props": {
                                                    "model": "media_server", "label": "媒体服务器",
                                                    "items": mediaserver_items,
                                                    "hint": "来自 MoviePilot 系统设置里已启用的 Emby/Jellyfin/Plex",
                                                    "persistent-hint": True
                                                }}]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VCard",
                        "props": {"variant": "outlined", "class": "mb-4"},
                        "content": [
                            {"component": "VCardTitle", "props": {"class": "text-h6"}, "text": "限速设置"},
                            {
                                "component": "VCardText",
                                "content": [
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 6},
                                                "content": [{"component": "VTextField", "props": {"model": "qb_download_limit", "label": "播放时下载限速", "type": "number", "suffix": "KB/s", "hint": "0 表示不限速", "persistent-hint": True}}]
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 6},
                                                "content": [{"component": "VTextField", "props": {"model": "qb_upload_limit", "label": "播放时上传限速", "type": "number", "suffix": "KB/s", "hint": "0 表示不限速", "persistent-hint": True}}]
                                            }
                                        ]
                                    },
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [{"component": "VSwitch", "props": {"model": "restore_on_stop", "label": "停止播放后恢复原速", "color": "primary"}}]
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [{"component": "VTextField", "props": {"model": "restore_download_limit", "label": "固定恢复下载限速", "type": "number", "suffix": "KB/s", "hint": "0 表示不限速", "persistent-hint": True}}]
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [{"component": "VTextField", "props": {"model": "restore_upload_limit", "label": "固定恢复上传限速", "type": "number", "suffix": "KB/s", "hint": "0 表示不限速", "persistent-hint": True}}]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VCard",
                        "props": {"variant": "outlined", "class": "mb-4"},
                        "content": [
                            {"component": "VCardTitle", "props": {"class": "text-h6"}, "text": "检测设置"},
                            {
                                "component": "VCardText",
                                "content": [
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [{"component": "VTextField", "props": {"model": "check_interval", "label": "检查间隔", "type": "number", "suffix": "秒", "hint": "最低 5 秒", "persistent-hint": True}}]
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [{"component": "VTextField", "props": {"model": "whitelist_users", "label": "白名单用户", "placeholder": "多个用英文逗号分隔"}}]
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [{"component": "VTextField", "props": {"model": "whitelist_devices", "label": "白名单设备", "placeholder": "多个用英文逗号分隔"}}]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "notify_type": "Plugin",
            "downloader": default_downloader,
            "media_server": default_media_server,
            "qb_download_limit": 1024,
            "qb_upload_limit": 1024,
            "check_interval": 10,
            "whitelist_users": "",
            "whitelist_devices": "",
            "restore_on_stop": True,
            "restore_download_limit": 0,
            "restore_upload_limit": 0,
        }

    def get_page(self) -> list:
        """获取插件详情页面"""
        media_conf = self._get_media_server_config()
        media_type = getattr(media_conf, "type", "") if media_conf else ""
        return [
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {"component": "VCardTitle", "text": "Emby播放自动限速状态"},
                    {
                        "component": "VList",
                        "content": [
                            {"component": "VListItem", "props": {"title": "插件状态", "subtitle": "已启用" if self._enabled else "已禁用"}},
                            {"component": "VListItem", "props": {"title": "下载器", "subtitle": self._downloader or "未选择"}},
                            {"component": "VListItem", "props": {"title": "媒体服务器", "subtitle": f"{self._media_server or '未选择'} {f'({media_type})' if media_type else ''}"}},
                            {"component": "VListItem", "props": {"title": "当前播放状态", "subtitle": "播放中" if self._is_playing else "未播放"}},
                            {"component": "VListItem", "props": {"title": "当前播放", "subtitle": self._last_playing_title or "无"}},
                            {"component": "VListItem", "props": {"title": "最后检查时间", "subtitle": datetime.fromtimestamp(self._last_playback_check).strftime("%Y-%m-%d %H:%M:%S") if self._last_playback_check > 0 else "未检查"}},
                            {"component": "VListItem", "props": {"title": "保存的原速限制", "subtitle": f"下载: {self._original_download_limit}KB/s, 上传: {self._original_upload_limit}KB/s"}},
                        ]
                    }
                ]
            }
        ]
