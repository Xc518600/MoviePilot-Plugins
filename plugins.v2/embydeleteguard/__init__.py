import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from app.core.event import Event, eventmanager
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType, WebhookEventInfo
from app.schemas.types import EventType


class EmbyDeleteGuard(_PluginBase):
    plugin_name = "Emby删除兜底清理"
    plugin_desc = "监听媒体服务器删除事件，延迟复查并兜底清理残留媒体、刮削文件、空目录和下载器任务。默认安全报告模式。"
    plugin_icon = "delete_sweep.png"
    plugin_version = "1.4"
    plugin_author = "老公"
    author_url = ""
    plugin_config_prefix = "embydeleteguard_"
    auth_level = 1

    # 常见媒体文件
    MEDIA_EXTS = {
        ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m2ts", ".iso", ".rmvb", ".webm",
        ".strm",
    }
    # 字幕和刮削文件
    SCRAP_EXTS = {
        ".nfo", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
        ".srt", ".ass", ".ssa", ".sup", ".sub", ".idx", ".vtt",
        ".xml", ".json",
    }
    SCRAP_NAME_HINTS = (
        "poster", "fanart", "backdrop", "banner", "clearlogo", "clearart", "landscape", "thumb", "season", "folder",
    )
    SCRAP_DIR_NAMES = {
        "extrafanart", "extrathumbs", "metadata", ".actors", "actors", "subs", "subtitles",
    }

    _enabled = False
    _notify = True
    _notify_on_residue = True
    _dry_run = True
    _delay_seconds = 60
    _watch_servers = "emby,jellyfin,plex"
    _allowed_roots = ""
    _reference_cleaner_plugins = True
    _reference_enabled_only = True
    _name_search_enabled = True
    _name_search_max_depth = 4
    _name_search_max_results = 80
    _clean_scrap = True
    _clean_empty_dirs = True
    _clean_media = False
    _delete_torrents = False
    _delete_torrent_files = False
    _emit_download_deleted_event = True
    _max_scan_files = 2000
    _path_mappings = ""
    _custom_scrap_extensions = ""
    _dedupe_seconds = 300
    _history_limit = 100
    _history: List[Dict[str, Any]] = []
    _cleaner_plugin_ids = {
        "RemoveLink": "清理媒体文件",
        "RemoveLink1": "国漫",
        "RemoveLink22": "外语电影",
        "RemoveLink2244": "华语电影",
        "RemoveLink224455": "国产电视剧",
        "RemoveLink22445566": "欧美剧",
        "RemoveLink33": "日韩剧",
    }

    _timers: List[threading.Timer] = []
    _recent_keys: Dict[str, float] = {}
    _lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = bool(config.get("enabled", False))
            self._notify = bool(config.get("notify", True))
            self._notify_on_residue = bool(config.get("notify_on_residue", True))
            self._dry_run = bool(config.get("dry_run", True))
            self._delay_seconds = int(config.get("delay_seconds") or 60)
            self._watch_servers = config.get("watch_servers") or "emby,jellyfin,plex"
            self._allowed_roots = config.get("allowed_roots") or ""
            self._reference_cleaner_plugins = bool(config.get("reference_cleaner_plugins", True))
            self._reference_enabled_only = bool(config.get("reference_enabled_only", True))
            self._name_search_enabled = bool(config.get("name_search_enabled", True))
            self._name_search_max_depth = int(config.get("name_search_max_depth") or 4)
            self._name_search_max_results = int(config.get("name_search_max_results") or 80)
            self._clean_scrap = bool(config.get("clean_scrap", True))
            self._clean_empty_dirs = bool(config.get("clean_empty_dirs", True))
            self._clean_media = bool(config.get("clean_media", False))
            self._delete_torrents = bool(config.get("delete_torrents", False))
            self._delete_torrent_files = bool(config.get("delete_torrent_files", False))
            self._emit_download_deleted_event = bool(config.get("emit_download_deleted_event", True))
            self._max_scan_files = int(config.get("max_scan_files") or 2000)
            self._path_mappings = config.get("path_mappings") or ""
            self._custom_scrap_extensions = config.get("custom_scrap_extensions") or ""
            self._dedupe_seconds = int(config.get("dedupe_seconds") or 300)
            self._history_limit = int(config.get("history_limit") or 100)
            history = config.get("history") or []
            self._history = history if isinstance(history, list) else []

        if self._enabled:
            mode = "只报告" if self._dry_run else "自动清理"
            logger.info(
                f"Emby删除兜底清理已启用：mode={mode}, delay={self._delay_seconds}s, "
                f"clean_scrap={self._clean_scrap}, clean_media={self._clean_media}, delete_torrents={self._delete_torrents}, "
                f"reference_cleaner_plugins={self._reference_cleaner_plugins}, name_search={self._name_search_enabled}"
            )
            referenced = self._referenced_cleaner_roots()
            if referenced:
                logger.info(f"Emby删除兜底清理已参考清理媒体文件/分身插件路径：{referenced}")
        else:
            logger.info("Emby删除兜底清理未启用")

    def get_state(self) -> bool:
        return bool(self._enabled)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_page(self) -> List[dict]:
        """展示最近漏删复查历史。"""
        history = list(self._history or [])
        rows = []
        for idx, item in enumerate(history[: self._history_limit], start=1):
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "text": str(idx)},
                    {"component": "td", "text": item.get("time", "")},
                    {"component": "td", "text": item.get("item_name", "")},
                    {"component": "td", "text": item.get("path", "")},
                    {"component": "td", "text": str(item.get("media_count", 0))},
                    {"component": "td", "text": str(item.get("scrap_count", 0))},
                    {"component": "td", "text": str(item.get("other_count", 0))},
                    {"component": "td", "text": str(item.get("empty_dir_count", 0))},
                    {"component": "td", "text": str(item.get("name_match_count", 0))},
                    {"component": "td", "text": str(item.get("torrent_count", 0))},
                    {"component": "td", "text": "是" if item.get("dry_run") else "否"},
                    {"component": "td", "text": item.get("summary", "")},
                ]
            })

        if not rows:
            return [{
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": "暂无漏删复查记录。删除媒体后，插件会在延迟复查完成时把最近记录保存到这里。"
                }
            }]

        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": f"最近漏删复查记录：{len(history[: self._history_limit])} 条。安全报告模式下只记录和通知，不会删除文件或种子。已参考清理插件路径：{len(self._referenced_cleaner_roots())} 个。"
                }
            },
            {
                "component": "VTable",
                "props": {
                    "hover": True,
                    "density": "compact",
                    "fixed-header": True,
                    "style": {"max-height": "620px", "overflow-y": "auto"}
                },
                "content": [
                    {
                        "component": "thead",
                        "content": [{
                            "component": "tr",
                            "content": [
                                {"component": "th", "text": "#"},
                                {"component": "th", "text": "时间"},
                                {"component": "th", "text": "媒体"},
                                {"component": "th", "text": "路径"},
                                {"component": "th", "text": "媒体"},
                                {"component": "th", "text": "刮削/字幕"},
                                {"component": "th", "text": "其他"},
                                {"component": "th", "text": "空目录"},
                                {"component": "th", "text": "名称搜索"},
                                {"component": "th", "text": "种子"},
                                {"component": "th", "text": "只报告"},
                                {"component": "th", "text": "摘要"},
                            ]
                        }]
                    },
                    {
                        "component": "tbody",
                        "content": rows
                    }
                ]
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "dry_run", "label": "安全报告模式", "hint": "开启时只报告残留，不删除任何文件或种子"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "notify", "label": "发送通知"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "notify_on_residue", "label": "检测到漏删时通知", "hint": "复查发现残留文件、空目录或种子时主动通知"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {"model": "delay_seconds", "label": "延迟复查秒数", "type": "number", "placeholder": "60"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {"model": "watch_servers", "label": "监听媒体服务器类型", "placeholder": "emby,jellyfin,plex"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "allowed_roots",
                                        "label": "允许清理的媒体库根目录",
                                        "rows": 3,
                                        "placeholder": "/硬盘1/media\n/硬盘2/media22",
                                        "hint": "强烈建议填写。为空时仅跳过系统危险路径，但不会限制媒体库根目录。"
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "reference_cleaner_plugins", "label": "参考清理媒体文件及分身插件路径", "hint": "自动读取 清理媒体文件/国漫/华语电影/外语电影/国产电视剧/欧美剧/日韩剧 的 monitor_dirs 作为允许复查根目录"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "reference_enabled_only", "label": "只参考已启用的清理插件", "hint": "关闭后会读取这些插件配置里的路径，即使对应插件未启用"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "name_search_enabled", "label": "按媒体名二次搜索", "hint": "Emby 删除后，在对应清理插件路径内按剧集/电影名搜索残留，作为双重保险"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VTextField", "props": {"model": "name_search_max_depth", "label": "名称搜索最大深度", "type": "number", "placeholder": "4"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VTextField", "props": {"model": "name_search_max_results", "label": "名称搜索最大结果数", "type": "number", "placeholder": "80"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "path_mappings",
                                        "label": "路径映射（可选）",
                                        "rows": 3,
                                        "placeholder": "Emby路径=>MoviePilot容器路径\n/mnt/media=>/硬盘2/media22",
                                        "hint": "当 webhook 里的路径和 MoviePilot 容器路径不一致时使用，每行一个映射。"
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "clean_scrap", "label": "清理刮削/字幕"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "clean_empty_dirs", "label": "清理空目录"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "clean_media", "label": "清理残留媒体", "hint": "关闭安全报告模式后才会真正删除"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "delete_torrents", "label": "删除残留种子", "hint": "默认仅报告；开启后删除下载器任务"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "delete_torrent_files", "label": "删种时同时删除源文件", "hint": "危险：一般不建议开启"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "emit_download_deleted_event", "label": "发送删种联动事件", "hint": "让下载器助手再兜底处理一次"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VTextField", "props": {"model": "max_scan_files", "label": "最大扫描文件数", "type": "number", "placeholder": "2000"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VTextField", "props": {"model": "history_limit", "label": "历史记录保留条数", "type": "number", "placeholder": "100"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "custom_scrap_extensions",
                                        "label": "自定义刮削/附属文件后缀",
                                        "rows": 2,
                                        "placeholder": ".txt\n-mediainfo.json",
                                        "hint": "每行或逗号分隔。"
                                    }
                                }]
                            },
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "dry_run": True,
            "notify": True,
            "notify_on_residue": True,
            "delay_seconds": 60,
            "watch_servers": "emby,jellyfin,plex",
            "allowed_roots": "",
            "reference_cleaner_plugins": True,
            "reference_enabled_only": True,
            "name_search_enabled": True,
            "name_search_max_depth": 4,
            "name_search_max_results": 80,
            "path_mappings": "",
            "clean_scrap": True,
            "clean_empty_dirs": True,
            "clean_media": False,
            "delete_torrents": False,
            "delete_torrent_files": False,
            "emit_download_deleted_event": True,
            "max_scan_files": 2000,
            "custom_scrap_extensions": "",
            "dedupe_seconds": 300,
            "history_limit": 100,
            "history": [],
        }

    def stop_service(self):
        with self._lock:
            for timer in self._timers:
                try:
                    timer.cancel()
                except Exception:
                    pass
            self._timers.clear()
            self._recent_keys.clear()

    @eventmanager.register(EventType.WebhookMessage)
    def handle_webhook(self, event: Event = None):
        if not self._enabled or not event:
            return
        event_info: WebhookEventInfo = getattr(event, "event_data", None)
        if not event_info:
            return

        channel = (getattr(event_info, "channel", "") or "").lower()
        if channel and channel not in self._watch_server_set():
            return

        event_type = (getattr(event_info, "event", "") or "").lower()
        if not self._is_delete_event(event_type):
            return

        raw_path = self._extract_item_path(event_info)
        item_name = getattr(event_info, "item_name", None) or self._extract_from_json(event_info, "Name") or "未知媒体"
        item_id = getattr(event_info, "item_id", None) or self._extract_from_json(event_info, "Id") or ""
        if not raw_path:
            logger.warning(f"Emby删除兜底清理收到删除事件但没有路径：event={event_type}, item={item_name}, id={item_id}")
            return

        mapped_path = self._map_path(raw_path)
        path = Path(mapped_path)
        key = f"{channel}:{event_type}:{item_id}:{mapped_path}"
        if self._is_duplicate(key):
            logger.info(f"Emby删除兜底清理忽略重复事件：{item_name} {mapped_path}")
            return

        if not self._is_safe_target(path):
            logger.warning(f"Emby删除兜底清理跳过非允许路径：{mapped_path}")
            return

        search_names = self._extract_search_names(event_info, item_name, path)
        logger.info(f"Emby删除兜底清理收到删除事件：event={event_type}, item={item_name}, path={mapped_path}，将在 {self._delay_seconds}s 后复查，搜索关键词={search_names}")
        timer = threading.Timer(
            max(0, self._delay_seconds),
            self._guard_cleanup,
            kwargs={"path": path, "item_name": item_name, "event_type": event_type, "channel": channel, "search_names": search_names},
        )
        timer.daemon = True
        with self._lock:
            self._timers.append(timer)
        timer.start()

    def _guard_cleanup(self, path: Path, item_name: str, event_type: str, channel: str, search_names: List[str] = None):
        try:
            result = self._inspect_residue(path)
            if self._name_search_enabled:
                result["name_search_matches"] = self._search_by_media_names(search_names or [], path)
            else:
                result["name_search_matches"] = []
            torrents = self._find_torrents(path)
            # 名称搜索命中的残留路径也参与种子匹配，避免只按原 path 漏掉。
            for matched_path in result.get("name_search_matches") or []:
                try:
                    torrents.extend(self._find_torrents(Path(matched_path)))
                except Exception:
                    pass
            result["torrents"] = self._dedupe_torrents(torrents)

            actions = []
            if self._emit_download_deleted_event:
                try:
                    eventmanager.send_event(EventType.DownloadFileDeleted, {"src": str(path)})
                    actions.append("已发送 DownloadFileDeleted 联动事件")
                except Exception as e:
                    logger.warning(f"发送 DownloadFileDeleted 事件失败：{e}")

            if not self._dry_run:
                actions.extend(self._apply_cleanup(result))
                if self._delete_torrents and torrents:
                    actions.extend(self._delete_matched_torrents(torrents))
            else:
                actions.append("安全报告模式：未删除文件或种子")

            self._log_and_notify(path, item_name, event_type, channel, result, actions)
        except Exception as e:
            logger.error(f"Emby删除兜底清理执行失败：{e}", exc_info=True)
        finally:
            with self._lock:
                self._timers = [t for t in self._timers if t.is_alive()]

    def _inspect_residue(self, path: Path) -> Dict[str, Any]:
        candidates = self._candidate_roots(path)
        files: List[Path] = []
        dirs: Set[Path] = set()
        skipped = False
        for root in candidates:
            if not self._is_safe_target(root):
                continue
            if root.is_file():
                files.append(root)
                dirs.add(root.parent)
            elif root.is_dir():
                for current, dirnames, filenames in os.walk(root):
                    cpath = Path(current)
                    dirs.add(cpath)
                    for name in filenames:
                        files.append(cpath / name)
                        if len(files) >= self._max_scan_files:
                            skipped = True
                            break
                    if skipped:
                        break

        media_files = [p for p in files if self._is_media_file(p)]
        scrap_files = [p for p in files if self._is_scrap_file(p) and not self._is_media_file(p)]
        other_files = [p for p in files if p not in media_files and p not in scrap_files]
        empty_dirs = self._find_empty_dirs(dirs)
        return {
            "target": path,
            "candidates": candidates,
            "media_files": media_files,
            "scrap_files": scrap_files,
            "other_files": other_files,
            "empty_dirs": empty_dirs,
            "scan_truncated": skipped,
        }

    def _candidate_roots(self, path: Path) -> List[Path]:
        """
        返回需要复查的精确候选路径。

        注意：删除单集文件后，不能扫描整个季目录，否则会把同季其他集误判为残留；
        删除整个目录后，若目录已经不存在，也不能扫描父级媒体库根目录。
        """
        uniq: List[Path] = []

        def add(p: Path):
            if p and p.exists() and p not in uniq and self._is_safe_target(p):
                uniq.append(p)

        # 原路径还存在：直接复查它本身
        add(path)

        # 文件删除后：只查同 stem 的关联文件/目录，例如 xxx.nfo、xxx.srt、xxx-poster.jpg、xxx/
        if path.suffix and path.parent.exists() and self._is_safe_target(path.parent):
            stem = path.stem.lower()
            for child in path.parent.iterdir():
                name = child.name.lower()
                child_stem = child.stem.lower() if child.is_file() else child.name.lower()
                if child == path:
                    continue
                if child_stem == stem or name.startswith(stem + ".") or name.startswith(stem + "-") or name.startswith(stem + " "):
                    add(child)

        # 目录删除后：只在父目录中寻找同名残留目录/文件，不扫描整个父目录
        if not path.suffix and path.parent.exists() and self._is_safe_target(path.parent):
            target_name = path.name.lower()
            for child in path.parent.iterdir():
                name = child.name.lower()
                if name == target_name or name.startswith(target_name + ".") or name.startswith(target_name + "-"):
                    add(child)

        return uniq

    def _apply_cleanup(self, result: Dict[str, Any]) -> List[str]:
        actions = []
        if self._clean_scrap:
            for p in result.get("scrap_files") or []:
                if self._safe_unlink(p):
                    actions.append(f"删除刮削/字幕：{p}")
        if self._clean_media:
            for p in result.get("media_files") or []:
                if self._safe_unlink(p):
                    actions.append(f"删除残留媒体：{p}")
        if self._clean_empty_dirs:
            # 删除深层空目录优先
            for d in sorted(result.get("empty_dirs") or [], key=lambda x: len(x.parts), reverse=True):
                if self._safe_rmdir(d):
                    actions.append(f"清理空目录：{d}")
        return actions

    def _extract_search_names(self, event_info: WebhookEventInfo, item_name: str, path: Path) -> List[str]:
        """从 Emby/Jellyfin/Plex 删除事件提取适合搜索的剧集/电影名关键词。"""
        names: List[str] = []
        json_obj = getattr(event_info, "json_object", None) or {}
        item = json_obj.get("Item") if isinstance(json_obj, dict) and isinstance(json_obj.get("Item"), dict) else {}
        for key in ("SeriesName", "Name", "OriginalTitle", "FileName"):
            val = item.get(key)
            if val:
                names.append(str(val))
        if item_name:
            names.append(str(item_name))
        if path.name:
            names.append(path.stem if path.suffix else path.name)
        # 清理 S01E02、年份、括号内容等噪声，保留中英文主标题。
        expanded: List[str] = []
        for name in names:
            for candidate in self._normalize_search_name(name):
                if candidate and candidate not in expanded:
                    expanded.append(candidate)
        return expanded[:8]

    @staticmethod
    def _normalize_search_name(name: str) -> List[str]:
        raw = (name or "").strip()
        if not raw:
            return []
        values = [raw]
        cleaned = re.sub(r"S\d{1,2}E\d{1,3}.*$", "", raw, flags=re.I).strip()
        cleaned = re.sub(r"第\s*\d+\s*[集话話].*$", "", cleaned).strip()
        cleaned = re.sub(r"\(\d{4}\)|（\d{4}）", "", cleaned).strip()
        cleaned = re.sub(r"\[[^\]]+\]", "", cleaned).strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-_—")
        if cleaned and cleaned not in values:
            values.append(cleaned)
        # 中文名通常在第一个空格前；英文/点分名保留 cleaned。
        if " " in cleaned:
            first = cleaned.split(" ", 1)[0].strip()
            if re.search(r"[\u4e00-\u9fff]", first) and first not in values:
                values.append(first)
        return [v for v in values if len(v) >= 2]

    def _search_by_media_names(self, names: List[str], original_path: Path) -> List[str]:
        if not names:
            return []
        roots = self._allowed_roots_list()
        if not roots:
            return []
        # 优先搜索原路径所属的清理插件根目录，避免跨硬盘/跨分类误报；找不到归属再搜全部参考根。
        related_roots = []
        try:
            op = original_path.resolve(strict=False)
        except Exception:
            op = original_path.absolute()
        for root in roots:
            try:
                rp = root.resolve(strict=False)
                if op == rp or op.is_relative_to(rp):
                    related_roots.append(root)
            except Exception:
                if str(op).startswith(str(root).rstrip("/") + "/"):
                    related_roots.append(root)
        search_roots = related_roots or roots
        matches: List[str] = []
        seen = set()
        lowered = [n.lower() for n in names if n]
        for root in search_roots:
            if len(matches) >= self._name_search_max_results:
                break
            if not root.exists() or not root.is_dir() or not self._is_safe_target(root):
                continue
            root_depth = len(root.parts)
            for current, dirnames, filenames in os.walk(root):
                cpath = Path(current)
                depth = len(cpath.parts) - root_depth
                if depth >= self._name_search_max_depth:
                    dirnames[:] = []
                candidates = list(dirnames) + list(filenames)
                for name in candidates:
                    nlow = name.lower()
                    if any(key and key in nlow for key in lowered):
                        full = (cpath / name).as_posix()
                        if full not in seen:
                            seen.add(full)
                            matches.append(full)
                            if len(matches) >= self._name_search_max_results:
                                break
                if len(matches) >= self._name_search_max_results:
                    break
        return matches

    @staticmethod
    def _dedupe_torrents(torrents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        uniq = []
        seen = set()
        for item in torrents or []:
            key = (item.get("downloader"), item.get("hash") or item.get("id") or item.get("name"))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(item)
        return uniq

    def _find_torrents(self, path: Path) -> List[Dict[str, Any]]:
        matched = []
        try:
            services = DownloaderHelper().get_services()
        except Exception as e:
            logger.warning(f"获取下载器服务失败：{e}")
            return matched
        target = path.as_posix().rstrip("/")
        for name, service in (services or {}).items():
            instance = getattr(service, "instance", None)
            if not instance:
                continue
            torrents = []
            try:
                if hasattr(instance, "get_torrents"):
                    data = instance.get_torrents()
                    torrents = data[0] if isinstance(data, tuple) else data
                elif hasattr(instance, "qbc") and getattr(instance, "qbc", None):
                    torrents = instance.qbc.torrents_info()
            except Exception as e:
                logger.warning(f"读取下载器 {name} 种子失败：{e}")
                continue
            for torrent in torrents or []:
                info = self._torrent_info(torrent)
                paths = [info.get("save_path"), info.get("content_path"), info.get("root_path"), info.get("download_dir")]
                if any(self._path_related(target, str(p or "")) for p in paths):
                    info.update({"downloader": name, "service": service, "torrent": torrent})
                    matched.append(info)
        return matched

    def _delete_matched_torrents(self, torrents: List[Dict[str, Any]]) -> List[str]:
        actions = []
        by_downloader: Dict[str, Dict[str, Any]] = {}
        for item in torrents:
            dl = item.get("downloader")
            if not dl:
                continue
            by_downloader.setdefault(dl, {"service": item.get("service"), "ids": [], "names": []})
            by_downloader[dl]["ids"].append(item.get("hash") or item.get("id"))
            by_downloader[dl]["names"].append(item.get("name"))
        for dl, data in by_downloader.items():
            service = data.get("service")
            instance = getattr(service, "instance", None) if service else None
            ids = [i for i in data.get("ids", []) if i]
            if not instance or not ids:
                continue
            try:
                if hasattr(instance, "delete_torrents"):
                    instance.delete_torrents(delete_file=self._delete_torrent_files, ids=ids)
                elif hasattr(instance, "remove_torrents"):
                    instance.remove_torrents(hashs=ids, delete_file=self._delete_torrent_files)
                actions.append(f"删除下载器 {dl} 种子 {len(ids)} 个：{', '.join((data.get('names') or [])[:3])}")
            except Exception as e:
                logger.error(f"删除下载器 {dl} 种子失败：{e}")
        return actions

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

    def _log_and_notify(self, path: Path, item_name: str, event_type: str, channel: str, result: Dict[str, Any], actions: List[str]):
        media_count = len(result.get("media_files") or [])
        scrap_count = len(result.get("scrap_files") or [])
        other_count = len(result.get("other_files") or [])
        empty_count = len(result.get("empty_dirs") or [])
        torrent_count = len(result.get("torrents") or [])
        name_match_count = len(result.get("name_search_matches") or [])
        title = "Emby删除兜底清理"
        lines = [
            f"媒体：{item_name}",
            f"事件：{channel}/{event_type}",
            f"路径：{path}",
            f"残留：媒体 {media_count}，刮削/字幕 {scrap_count}，其他 {other_count}，空目录 {empty_count}，名称搜索 {name_match_count}，种子 {torrent_count}",
        ]
        if result.get("scan_truncated"):
            lines.append(f"扫描达到上限 {self._max_scan_files}，结果可能不完整")
        sample_files = (result.get("media_files") or [])[:5] + (result.get("scrap_files") or [])[:5]
        if sample_files:
            lines.append("残留样例：")
            lines.extend([f"- {p}" for p in sample_files[:8]])
        if result.get("name_search_matches"):
            lines.append("名称搜索命中：")
            lines.extend([f"- {p}" for p in (result.get("name_search_matches") or [])[:8]])
        if result.get("torrents"):
            lines.append("残留种子：")
            for t in (result.get("torrents") or [])[:5]:
                lines.append(f"- [{t.get('downloader')}] {t.get('name')}")
        if actions:
            lines.append("动作：")
            lines.extend([f"- {a}" for a in actions[:8]])
        text = "\n".join(lines)
        logger.info(text)
        has_residue = bool(media_count or scrap_count or other_count or empty_count or name_match_count or torrent_count)
        self._save_history_record(
            path=path,
            item_name=item_name,
            event_type=event_type,
            channel=channel,
            result=result,
            actions=actions,
            has_residue=has_residue,
        )
        if has_residue:
            logger.warning(f"Emby删除兜底清理检测到漏删：{item_name}，媒体 {media_count}，刮削/字幕 {scrap_count}，其他 {other_count}，空目录 {empty_count}，名称搜索 {name_match_count}，种子 {torrent_count}")

        should_notify = bool(self._notify and (
            (has_residue and self._notify_on_residue)
            or (not has_residue and not self._dry_run)
        ))
        if should_notify:
            try:
                notify_title = "Emby删除兜底清理：检测到漏删" if has_residue else title
                self.post_message(mtype=NotificationType.Plugin, title=notify_title, text=text)
            except Exception as e:
                logger.warning(f"发送通知失败：{e}")

    def _save_history_record(self, path: Path, item_name: str, event_type: str, channel: str,
                             result: Dict[str, Any], actions: List[str], has_residue: bool):
        """保存最近复查记录到插件配置，供插件页面查看。"""
        try:
            media_files = [str(p) for p in (result.get("media_files") or [])]
            scrap_files = [str(p) for p in (result.get("scrap_files") or [])]
            other_files = [str(p) for p in (result.get("other_files") or [])]
            empty_dirs = [str(p) for p in (result.get("empty_dirs") or [])]
            torrents = result.get("torrents") or []
            name_matches = [str(p) for p in (result.get("name_search_matches") or [])]
            torrent_items = [
                {
                    "downloader": t.get("downloader"),
                    "name": t.get("name"),
                    "hash": t.get("hash") or t.get("id"),
                    "save_path": t.get("save_path"),
                    "content_path": t.get("content_path"),
                }
                for t in torrents[:20]
            ]
            summary_parts = []
            if media_files:
                summary_parts.append(f"媒体{len(media_files)}")
            if scrap_files:
                summary_parts.append(f"刮削/字幕{len(scrap_files)}")
            if other_files:
                summary_parts.append(f"其他{len(other_files)}")
            if empty_dirs:
                summary_parts.append(f"空目录{len(empty_dirs)}")
            if name_matches:
                summary_parts.append(f"名称搜索{len(name_matches)}")
            if torrent_items:
                summary_parts.append(f"种子{len(torrent_items)}")
            summary = "，".join(summary_parts) if summary_parts else "无残留"
            record = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "item_name": item_name,
                "event_type": event_type,
                "channel": channel,
                "path": str(path),
                "has_residue": has_residue,
                "media_count": len(media_files),
                "scrap_count": len(scrap_files),
                "other_count": len(other_files),
                "empty_dir_count": len(empty_dirs),
                "name_match_count": len(name_matches),
                "torrent_count": len(torrents),
                "dry_run": bool(self._dry_run),
                "summary": summary,
                "media_files": media_files[:20],
                "scrap_files": scrap_files[:30],
                "other_files": other_files[:20],
                "empty_dirs": empty_dirs[:20],
                "name_search_matches": name_matches[:40],
                "torrents": torrent_items,
                "actions": list(actions or [])[:20],
            }
            with self._lock:
                self._history.insert(0, record)
                self._history = self._history[: max(1, int(self._history_limit or 100))]
            # 保存到插件配置；避免把运行态对象写入配置，只写基础字段和历史。
            config = self.get_config() or {}
            if not isinstance(config, dict):
                config = {}
            config.update({
                "enabled": self._enabled,
                "notify": self._notify,
                "notify_on_residue": self._notify_on_residue,
                "dry_run": self._dry_run,
                "delay_seconds": self._delay_seconds,
                "watch_servers": self._watch_servers,
                "allowed_roots": self._allowed_roots,
                "reference_cleaner_plugins": self._reference_cleaner_plugins,
                "reference_enabled_only": self._reference_enabled_only,
                "name_search_enabled": self._name_search_enabled,
                "name_search_max_depth": self._name_search_max_depth,
                "name_search_max_results": self._name_search_max_results,
                "path_mappings": self._path_mappings,
                "clean_scrap": self._clean_scrap,
                "clean_empty_dirs": self._clean_empty_dirs,
                "clean_media": self._clean_media,
                "delete_torrents": self._delete_torrents,
                "delete_torrent_files": self._delete_torrent_files,
                "emit_download_deleted_event": self._emit_download_deleted_event,
                "max_scan_files": self._max_scan_files,
                "custom_scrap_extensions": self._custom_scrap_extensions,
                "dedupe_seconds": self._dedupe_seconds,
                "history_limit": self._history_limit,
                "history": self._history,
            })
            self.update_config(config)
        except Exception as e:
            logger.warning(f"保存漏删历史记录失败：{e}")

    def _extract_item_path(self, event_info: WebhookEventInfo) -> Optional[str]:
        path = getattr(event_info, "item_path", None)
        if path:
            return path
        json_obj = getattr(event_info, "json_object", None) or {}
        if isinstance(json_obj, dict):
            item = json_obj.get("Item") if isinstance(json_obj.get("Item"), dict) else json_obj
            for key in ("Path", "path", "FileName", "filename"):
                if item.get(key):
                    return item.get(key)
        return None

    def _extract_from_json(self, event_info: WebhookEventInfo, key: str) -> Optional[str]:
        json_obj = getattr(event_info, "json_object", None) or {}
        if isinstance(json_obj, dict):
            item = json_obj.get("Item") if isinstance(json_obj.get("Item"), dict) else json_obj
            return item.get(key)
        return None

    @staticmethod
    def _is_delete_event(event_type: str) -> bool:
        normalized = (event_type or "").lower().replace("_", ".").replace("-", ".")
        return any(token in normalized for token in (
            "delete", "deleted", "remove", "removed", "library.deleted", "item.deleted"
        ))

    def _watch_server_set(self) -> Set[str]:
        return {x.strip().lower() for x in re.split(r"[,，\s]+", self._watch_servers or "") if x.strip()}

    def _custom_scrap_exts(self) -> Set[str]:
        exts = set()
        for item in re.split(r"[,，\s]+", self._custom_scrap_extensions or ""):
            item = item.strip().lower()
            if not item:
                continue
            exts.add(item if item.startswith(".") or item.startswith("-") else f".{item}")
        return exts

    def _is_media_file(self, path: Path) -> bool:
        return path.suffix.lower() in self.MEDIA_EXTS

    def _is_scrap_file(self, path: Path) -> bool:
        name = path.name.lower()
        suffix = path.suffix.lower()
        if suffix in self.SCRAP_EXTS or suffix in self._custom_scrap_exts():
            return True
        if any(hint in name for hint in self.SCRAP_NAME_HINTS):
            return True
        return any(part.lower() in self.SCRAP_DIR_NAMES for part in path.parts)

    def _find_empty_dirs(self, dirs: Set[Path]) -> List[Path]:
        empty = []
        for d in sorted(dirs, key=lambda x: len(x.parts), reverse=True):
            try:
                if d.exists() and d.is_dir() and self._is_safe_target(d) and not any(d.iterdir()):
                    empty.append(d)
            except Exception:
                pass
        return empty

    def _safe_unlink(self, path: Path) -> bool:
        try:
            if not path.exists() or not path.is_file() or not self._is_safe_target(path):
                return False
            path.unlink()
            return True
        except Exception as e:
            logger.warning(f"删除文件失败 {path}: {e}")
            return False

    def _safe_rmdir(self, path: Path) -> bool:
        try:
            if not path.exists() or not path.is_dir() or not self._is_safe_target(path):
                return False
            path.rmdir()
            return True
        except Exception:
            return False

    def _map_path(self, path: str) -> str:
        mapped = path
        for line in (self._path_mappings or "").splitlines():
            line = line.strip()
            if not line or "=>" not in line:
                continue
            src, dst = [x.strip() for x in line.split("=>", 1)]
            if src and mapped.startswith(src):
                mapped = dst.rstrip("/") + mapped[len(src):]
                break
        return mapped

    def _allowed_roots_list(self) -> List[Path]:
        roots: List[Path] = []
        for line in (self._allowed_roots or "").splitlines():
            line = line.strip()
            if line:
                roots.append(Path(line))
        if self._reference_cleaner_plugins:
            roots.extend(Path(p) for p in self._referenced_cleaner_roots())
        # 去重，保持顺序；注意 /media2 与 /media22 是不同 Path，后续 is_relative_to 会按路径边界判断。
        uniq: List[Path] = []
        seen = set()
        for root in roots:
            key = root.as_posix().rstrip("/")
            if key and key not in seen:
                seen.add(key)
                uniq.append(Path(key))
        return uniq

    def _referenced_cleaner_roots(self) -> List[str]:
        """读取 清理媒体文件 及其分身插件的 monitor_dirs/STRM 映射目录。"""
        roots: List[str] = []
        for pid, name in self._cleaner_plugin_ids.items():
            try:
                cfg = self.systemconfig.get(f"plugin.{pid}") or {}
            except Exception as e:
                logger.debug(f"读取清理插件 {pid}/{name} 配置失败：{e}")
                cfg = {}
            if not isinstance(cfg, dict):
                continue
            if self._reference_enabled_only and not bool(cfg.get("enabled")):
                continue
            for line in str(cfg.get("monitor_dirs") or "").splitlines():
                line = line.strip()
                if line:
                    roots.append(line.rstrip("/"))
            # STRM 映射格式：strm目录:存储类型:网盘目录 或 strm目录:网盘目录，这里只取第一段监控目录。
            for line in str(cfg.get("strm_path_mappings") or "").splitlines():
                line = line.strip()
                if not line or ":" not in line:
                    continue
                roots.append(line.split(":", 1)[0].strip().rstrip("/"))
        # 去重
        uniq: List[str] = []
        seen = set()
        for root in roots:
            if root and root not in seen:
                seen.add(root)
                uniq.append(root)
        return uniq

    def _is_safe_target(self, path: Path) -> bool:
        try:
            p = path.resolve(strict=False)
        except Exception:
            p = path.absolute()
        danger = {Path("/"), Path("/app"), Path("/config"), Path("/tmp"), Path("/var"), Path("/usr"), Path("/bin"), Path("/sbin"), Path("/lib"), Path("/etc")}
        if p in danger or len(p.parts) < 3:
            return False
        roots = self._allowed_roots_list()
        if roots:
            for root in roots:
                try:
                    r = root.resolve(strict=False)
                except Exception:
                    r = root.absolute()
                try:
                    if p == r or p.is_relative_to(r):
                        return True
                except Exception:
                    if str(p).startswith(str(r).rstrip("/") + "/"):
                        return True
            return False
        return True

    @staticmethod
    def _path_related(target: str, candidate: str) -> bool:
        if not target or not candidate:
            return False
        target = target.rstrip("/")
        candidate = candidate.rstrip("/")
        return candidate == target or candidate.startswith(target + "/") or target.startswith(candidate + "/")

    def _is_duplicate(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            self._recent_keys = {k: v for k, v in self._recent_keys.items() if now - v < self._dedupe_seconds}
            if key in self._recent_keys:
                return True
            self._recent_keys[key] = now
            return False
