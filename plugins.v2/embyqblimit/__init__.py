# -*- coding: utf-8 -*-
"""
Emby/Jellyfin/Plex 播放自动限速 MoviePilot 下载器插件
直接调用 MoviePilot 已配置的媒体服务器和下载器，播放时限速，停止后恢复。
"""

from datetime import datetime
import math
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
    plugin_version = "2.5.1"
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
        return [
            {
                "cmd": "/qb_tasks",
                "event": EventType.PluginAction,
                "desc": "查看 qBittorrent 任务列表",
                "category": "下载器",
                "data": {"action": "qb_tasks"}
            },
            {
                "cmd": "/qb_pause",
                "event": EventType.PluginAction,
                "desc": "暂停指定 qBittorrent 任务，示例：/qb_pause 1",
                "category": "下载器",
                "data": {"action": "qb_pause"}
            },
            {
                "cmd": "/qb_resume",
                "event": EventType.PluginAction,
                "desc": "恢复指定 qBittorrent 任务，示例：/qb_resume 1",
                "category": "下载器",
                "data": {"action": "qb_resume"}
            },
            {
                "cmd": "/qb_pause_all",
                "event": EventType.PluginAction,
                "desc": "暂停全部下载中任务",
                "category": "下载器",
                "data": {"action": "qb_pause_all"}
            },
            {
                "cmd": "/qb_resume_all",
                "event": EventType.PluginAction,
                "desc": "恢复全部已暂停任务",
                "category": "下载器",
                "data": {"action": "qb_resume_all"}
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """插件 API 列表"""
        return []

    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event = None):
        """处理远程命令交互"""
        if not event:
            return
        event_data = event.event_data or {}
        if not isinstance(event_data, dict):
            return
        action = (event_data.get("action") or "").strip()
        if action not in {"qb_tasks", "qb_pause", "qb_resume", "qb_pause_all", "qb_resume_all"}:
            return

        args = event_data.get("args") or event_data.get("arg") or ""
        text = event_data.get("text") or ""
        payload = self._handle_qb_command(action=action, args=args, text=text)
        if not payload:
            return
        self.post_message(
            mtype=getattr(NotificationType, self._notify_type, NotificationType.Plugin),
            title=payload.get("title") or "Emby限速助手",
            text=payload.get("text") or ""
        )

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
            media_info = f"{self._last_playing_title}" if self._last_playing_title else "未知媒体"
            limit_info = f"已限速：下载 {self._qb_download_limit}KB/s，上传 {self._qb_upload_limit}KB/s"
            self._send_notification("🎬 检测到播放", f"{media_info}\n{limit_info}")
            self._apply_limit(notify=False)
            self._is_playing = True
        elif not is_playing and self._is_playing:
            logger.info(f"{source}检测到媒体服务器 {self._media_server} 停止播放，恢复下载器 {self._downloader} 速度")
            if self._restore_on_stop:
                # 计算恢复的速度值用于通知
                if self._restore_download_limit > 0 or self._restore_upload_limit > 0:
                    restore_dl = self._restore_download_limit
                    restore_ul = self._restore_upload_limit
                else:
                    restore_dl = self._original_download_limit
                    restore_ul = self._original_upload_limit
                
                restore_info = f"已恢复：下载 {restore_dl}KB/s，上传 {restore_ul}KB/s"
                self._send_notification("⏸️ 播放结束", restore_info)
                self._restore_limit(notify=False)
            else:
                self._send_notification("⏸️ 播放结束", "未自动恢复限速")
            
            self._is_playing = False
            self._last_playing_title = ""

    def _get_downloader_service(self):
        """获取 MoviePilot 下载器服务"""
        if not self._downloader:
            return None
        return DownloaderHelper().get_service(name=self._downloader)

    def _handle_qb_command(self, action: str, args: Any = None, text: str = "") -> Dict[str, str]:
        """处理 qB 任务命令"""
        try:
            if not self._downloader:
                return {
                    "title": "Emby限速助手 - 命令执行失败",
                    "text": "当前插件未配置下载器，请先在插件设置里选择 qBittorrent 下载器。"
                }

            torrents = self._get_qb_torrents()
            if action == "qb_tasks":
                return {
                    "title": "Emby限速助手 - qB任务列表",
                    "text": self._format_torrent_list(torrents)
                }

            if action in {"qb_pause", "qb_resume"}:
                indices = self._parse_command_indexes(args=args, text=text)
                if not indices:
                    cmd = "/qb_pause 1" if action == "qb_pause" else "/qb_resume 1"
                    return {
                        "title": "Emby限速助手 - 参数错误",
                        "text": f"请提供任务编号，例如：{cmd}\n\n先发送 /qb_tasks 查看编号。"
                    }
                selected, invalid = self._pick_torrents_by_index(torrents, indices)
                if not selected:
                    return {
                        "title": "Emby限速助手 - 未找到任务",
                        "text": f"没有匹配到编号：{', '.join(str(i) for i in indices)}\n\n请先发送 /qb_tasks 查看当前编号。"
                    }
                changed, skipped = self._filter_torrents_for_action(selected, action)
                if not changed:
                    status_text = "都已经暂停" if action == "qb_pause" else "都已经在下载/排队"
                    return {
                        "title": "Emby限速助手 - 无需操作",
                        "text": f"选中的任务{status_text}。\n\n{self._format_selected_torrents(selected, invalid=invalid, skipped=skipped)}"
                    }
                self._operate_torrents(action, changed)
                verb = "已暂停" if action == "qb_pause" else "已恢复"
                return {
                    "title": f"Emby限速助手 - {verb}任务",
                    "text": self._format_selected_torrents(changed, prefix=verb, invalid=invalid, skipped=skipped)
                }

            if action in {"qb_pause_all", "qb_resume_all"}:
                changed, skipped = self._filter_torrents_for_action(torrents, action)
                if not changed:
                    status_text = "当前没有可暂停的下载中任务" if action == "qb_pause_all" else "当前没有可恢复的已暂停任务"
                    return {
                        "title": "Emby限速助手 - 无需操作",
                        "text": status_text
                    }
                self._operate_torrents(action, changed)
                verb = "已暂停全部下载中任务" if action == "qb_pause_all" else "已恢复全部已暂停任务"
                lines = [verb, "", f"共处理 {len(changed)} 个任务："]
                lines.extend([f"- {item.get('name')}" for item in changed[:20]])
                if len(changed) > 20:
                    lines.append(f"- ……其余 {len(changed) - 20} 个任务未展开")
                if skipped:
                    lines.append("")
                    lines.append(f"跳过 {len(skipped)} 个当前状态不匹配的任务")
                return {
                    "title": f"Emby限速助手 - {verb}",
                    "text": "\n".join(lines)
                }
        except Exception as e:
            logger.error(f"处理 qB 命令失败: {e}")
            return {
                "title": "Emby限速助手 - 命令执行失败",
                "text": str(e)
            }
        return {}

    def _get_qb_torrents(self) -> List[Dict[str, Any]]:
        """获取 qB 任务列表"""
        service = self._get_downloader_service()
        if not service or not service.instance:
            raise RuntimeError(f"获取下载器失败：{self._downloader or '未配置'}")
        instance = service.instance

        torrents = []
        if hasattr(instance, "get_torrents"):
            data = instance.get_torrents()
            torrents = data[0] if isinstance(data, tuple) else data
        elif hasattr(instance, "qbc") and getattr(instance, "qbc", None):
            torrents = instance.qbc.torrents_info()
        else:
            raise RuntimeError(f"下载器 {self._downloader} 不支持读取任务列表")

        results = []
        for idx, torrent in enumerate(torrents or [], start=1):
            info = self._torrent_info(torrent)
            info.update({
                "index": idx,
                "state": self._torrent_field(torrent, "state"),
                "progress": self._torrent_field(torrent, "progress"),
                "size": self._torrent_field(torrent, "size") or self._torrent_field(torrent, "total_size"),
                "dlspeed": self._torrent_field(torrent, "dlspeed") or self._torrent_field(torrent, "dl_speed"),
                "upspeed": self._torrent_field(torrent, "upspeed") or self._torrent_field(torrent, "up_speed"),
            })
            results.append(info)
        return results

    @staticmethod
    def _torrent_field(torrent: Any, key: str):
        if isinstance(torrent, dict):
            return torrent.get(key)
        return getattr(torrent, key, None)

    def _torrent_info(self, torrent: Any) -> Dict[str, Any]:
        def get(key: str):
            if isinstance(torrent, dict):
                return torrent.get(key)
            try:
                return getattr(torrent, key)
            except Exception:
                return None

        return {
            "id": get("id") or get("hash") or get("hashString"),
            "hash": get("hash") or get("hashString") or get("id"),
            "name": get("name"),
            "save_path": get("save_path") or get("downloadDir"),
            "content_path": get("content_path"),
            "root_path": get("root_path"),
            "download_dir": get("downloadDir") or get("download_dir"),
        }

    def _parse_command_indexes(self, args: Any = None, text: str = "") -> List[int]:
        raw = []
        if args is not None:
            if isinstance(args, list):
                raw.extend([str(item) for item in args])
            else:
                raw.append(str(args))
        if text:
            raw.append(str(text))
        merged = " ".join(raw).replace("，", " ").replace(",", " ")
        indexes = []
        for part in merged.split():
            if part.startswith("/"):
                continue
            if part.isdigit():
                value = int(part)
                if value > 0 and value not in indexes:
                    indexes.append(value)
        return indexes

    def _pick_torrents_by_index(self, torrents: List[Dict[str, Any]], indexes: List[int]) -> Tuple[List[Dict[str, Any]], List[int]]:
        mapping = {item.get("index"): item for item in torrents}
        selected = []
        invalid = []
        for idx in indexes:
            item = mapping.get(idx)
            if item:
                selected.append(item)
            else:
                invalid.append(idx)
        return selected, invalid

    def _filter_torrents_for_action(self, torrents: List[Dict[str, Any]], action: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        changed = []
        skipped = []
        for item in torrents or []:
            if self._can_apply_action(item, action):
                changed.append(item)
            else:
                skipped.append(item)
        return changed, skipped

    def _can_apply_action(self, torrent: Dict[str, Any], action: str) -> bool:
        state = str(torrent.get("state") or "").lower()
        if action in {"qb_pause", "qb_pause_all"}:
            return "pause" not in state and "paused" not in state
        if action in {"qb_resume", "qb_resume_all"}:
            return "pause" in state or "stopped" in state
        return False

    def _operate_torrents(self, action: str, torrents: List[Dict[str, Any]]):
        service = self._get_downloader_service()
        if not service or not service.instance:
            raise RuntimeError(f"获取下载器失败：{self._downloader or '未配置'}")
        instance = service.instance
        ids = [item.get("hash") or item.get("id") for item in torrents if item.get("hash") or item.get("id")]
        if not ids:
            raise RuntimeError("没有可操作的任务 ID")

        if action in {"qb_pause", "qb_pause_all"}:
            if hasattr(instance, "stop_torrents"):
                instance.stop_torrents(ids)
            elif hasattr(instance, "pause_torrents"):
                instance.pause_torrents(ids)
            elif hasattr(instance, "qbc") and getattr(instance, "qbc", None):
                instance.qbc.torrents_pause(torrent_hashes=ids)
            else:
                raise RuntimeError(f"下载器 {self._downloader} 不支持暂停任务")
        elif action in {"qb_resume", "qb_resume_all"}:
            if hasattr(instance, "start_torrents"):
                instance.start_torrents(ids)
            elif hasattr(instance, "resume_torrents"):
                instance.resume_torrents(ids)
            elif hasattr(instance, "qbc") and getattr(instance, "qbc", None):
                instance.qbc.torrents_resume(torrent_hashes=ids)
            else:
                raise RuntimeError(f"下载器 {self._downloader} 不支持恢复任务")

    def _format_torrent_list(self, torrents: List[Dict[str, Any]]) -> str:
        if not torrents:
            return "当前没有 qBittorrent 任务。"
        lines = [f"下载器：{self._downloader}", f"当前共 {len(torrents)} 个任务", "", "可用命令：", "/qb_pause 编号", "/qb_resume 编号", "/qb_pause_all", "/qb_resume_all", "", "任务列表："]
        for item in torrents[:30]:
            progress = self._format_progress(item.get("progress"))
            size = self._format_bytes(item.get("size"))
            ds = self._format_speed(item.get("dlspeed"))
            us = self._format_speed(item.get("upspeed"))
            state = self._human_state(item.get("state"))
            lines.append(f"{item.get('index')}. [{state}] {item.get('name')}")
            lines.append(f"   进度：{progress}｜大小：{size}｜↓{ds} ↑{us}")
        if len(torrents) > 30:
            lines.append("")
            lines.append(f"仅展示前 30 个任务，剩余 {len(torrents) - 30} 个未展开。")
        return "\n".join(lines)

    def _format_selected_torrents(self, torrents: List[Dict[str, Any]], prefix: str = "", invalid: Optional[List[int]] = None,
                                  skipped: Optional[List[Dict[str, Any]]] = None) -> str:
        lines = []
        if prefix:
            lines.append(prefix)
            lines.append("")
        lines.append(f"下载器：{self._downloader}")
        lines.append(f"共 {len(torrents)} 个任务：")
        for item in torrents[:20]:
            lines.append(f"- #{item.get('index')} {item.get('name')}")
        if len(torrents) > 20:
            lines.append(f"- ……其余 {len(torrents) - 20} 个任务未展开")
        if invalid:
            lines.append("")
            lines.append(f"无效编号：{', '.join(str(i) for i in invalid)}")
        if skipped:
            lines.append("")
            lines.append(f"跳过 {len(skipped)} 个状态不匹配的任务")
        return "\n".join(lines)

    @staticmethod
    def _format_progress(value: Any) -> str:
        try:
            num = float(value or 0)
            if num <= 1:
                num *= 100
            return f"{num:.1f}%"
        except Exception:
            return "0.0%"

    @staticmethod
    def _format_bytes(value: Any) -> str:
        try:
            num = float(value or 0)
        except Exception:
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB"]
        idx = 0
        while num >= 1024 and idx < len(units) - 1:
            num /= 1024.0
            idx += 1
        return f"{num:.1f} {units[idx]}"

    def _format_speed(self, value: Any) -> str:
        return f"{self._format_bytes(value)}/s"

    @staticmethod
    def _human_state(state: Any) -> str:
        raw = str(state or "")
        lower = raw.lower()
        if "paused" in lower or lower.startswith("pause"):
            return "已暂停"
        if "downloading" in lower or lower == "dl":
            return "下载中"
        if "uploading" in lower:
            return "做种中"
        if "stalled" in lower:
            return "等待中"
        if "queued" in lower:
            return "排队中"
        if "error" in lower or "missing" in lower:
            return "错误"
        if "checking" in lower:
            return "校验中"
        if "meta" in lower:
            return "获取元数据"
        return raw or "未知"

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

    def _apply_limit(self, notify: bool = True):
        """应用限速"""
        try:
            self._save_current_limits()
            self._set_downloader_limits(
                download_limit=self._qb_download_limit,
                upload_limit=self._qb_upload_limit,
                notify=notify
            )
        except Exception as e:
            logger.error(f"应用限速失败: {str(e)}")

    def _restore_limit(self, notify: bool = True):
        """恢复原速"""
        try:
            if self._restore_download_limit > 0 or self._restore_upload_limit > 0:
                dl = self._restore_download_limit
                ul = self._restore_upload_limit
            else:
                dl = self._original_download_limit
                ul = self._original_upload_limit

            self._set_downloader_limits(download_limit=dl, upload_limit=ul, notify=notify)
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

    def _set_downloader_limits(self, download_limit: int, upload_limit: int, notify: bool = True):
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
            if notify and (int(download_limit or 0) > 0 or int(upload_limit or 0) > 0):
                self._send_notification(
                    "🐢 已应用限速",
                    f"下载：{download_limit}KB/s\n上传：{upload_limit}KB/s"
                )
        except Exception as e:
            logger.error(f"设置下载器限速失败: {str(e)}")
            self._send_notification(
                "限速设置失败",
                f"{str(e)}\n请检查下载器配置"
            )

    def _send_notification(self, title: str, message: str = ""):
        """发送通知消息
        
        Args:
            title: 通知标题
            message: 通知内容
        """
        if not self._notify:
            return
        
        try:
            self.post_message(
                mtype=getattr(NotificationType, self._notify_type, NotificationType.Plugin),
                title=f"🎥 Emby限速助手 - {title}",
                text=message
            )
        except Exception:
            try:
                if self._message_helper:
                    self._message_helper.put(
                        title=f"🎥 Emby限速助手 - {title}",
                        message=message
                    )
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
                                                "content": [{"component": "VTextField", "props": {"model": "restore_download_limit", "label": "固定恢复下载限速", "type": "number", "suffix": "KB/s", "hint": "0 使用播放前原始限速", "persistent-hint": True}}]
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [{"component": "VTextField", "props": {"model": "restore_upload_limit", "label": "固定恢复上传限速", "type": "number", "suffix": "KB/s", "hint": "0 使用播放前原始限速", "persistent-hint": True}}]
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
