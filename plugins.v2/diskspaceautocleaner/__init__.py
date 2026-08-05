import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.helper.mediaserver import MediaServerHelper
from app.chain.media import MediaChain

from .utils import DiskSpaceUtils
from .scanner import DiskSpaceScanner
from .deleter import DiskSpaceDeleter
from .notifier import DiskSpaceNotifier


class DiskSpaceAutoCleaner(_PluginBase):
    _max_strategy_slots = 8
    plugin_name = "硬盘空间自动清理"
    plugin_desc = "监控指定硬盘剩余空间，空间不足时按单盘策略扫描媒体库并生成清理建议。"
    plugin_icon = "harddisk.png"
    plugin_version = "3.9.6"
    plugin_author = "老公"
    author_url = ""
    plugin_config_prefix = "diskspaceautocleaner_"
    auth_level = 1

    _enabled = False
    _notify = True
    _dry_run = True
    _monitor_paths = ""
    _media_paths = ""
    _path_mappings = ""
    _min_free_gb = 5
    _target_free_gb = 30
    _scan_interval_minutes = 60
    _max_candidates = 30
    _max_scan_items = 5000
    _candidate_depth = 2
    _recent_days_protect = 30
    _max_delete_gb = 1000  # 每次删除的最大空间限制（GB）
    _protect_dirs = ""
    _protect_keywords = ""
    _history_limit = 50
    _history: List[Dict[str, Any]] = []
    _scan_state: Dict[str, Dict[str, Any]] = {}
    _media_server = ""
    _active_play_protect = True
    _recent_play_days = 7
    _scan_cooldown_minutes = 360
    _scan_backoff_multiplier = 2
    _scan_backoff_max_minutes = 1440
    _tmdb_top_n = 30
    _strategy_profiles = ""
    _current_strategy_name = ""
    _run_once = False
    _tmdb_rating_cache: Dict[str, Dict[str, Any]] = {}
    _poster_cache: Dict[str, Optional[str]] = {}
    _blank_poster = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGQAAACWCAQAAACCseXNAAAAkklEQVR42u3PAREAAAQEMJ9cFFUVkMBtDZbpeiEiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIpcFcbGoK4SMl3wAAAAASUVORK5CYII="

    _size_cache: Dict[str, int] = {}
    _size_cache_lock = threading.Lock()
    _scan_workers: int = 4  # 多线程扫描的线程数

    _timer: Optional[threading.Timer] = None
    _lock = threading.Lock()
    _check_running = False
    _last_check_started_at = 0.0
    _dedupe_window_seconds = 30

    def init_plugin(self, config: dict = None):
        triggered_immediate_run = False
        if config:
            self._enabled = DiskSpaceUtils.to_bool(config.get("enabled"), False)
            self._notify = DiskSpaceUtils.to_bool(config.get("notify"), True)
            self._dry_run = DiskSpaceUtils.to_bool(config.get("dry_run"), True)
            self._monitor_paths = config.get("monitor_paths") or ""
            self._media_paths = config.get("media_paths") or ""
            self._path_mappings = config.get("path_mappings") or ""
            self._min_free_gb = DiskSpaceUtils.to_int(config.get("min_free_gb"), 5)
            self._target_free_gb = DiskSpaceUtils.to_int(config.get("target_free_gb"), 30)
            self._scan_interval_minutes = DiskSpaceUtils.to_int(config.get("scan_interval_minutes"), 60)
            self._max_candidates = DiskSpaceUtils.to_int(config.get("max_candidates"), 30)
            self._max_scan_items = DiskSpaceUtils.to_int(config.get("max_scan_items"), 5000)
            self._candidate_depth = DiskSpaceUtils.to_int(config.get("candidate_depth"), 2)
            self._recent_days_protect = DiskSpaceUtils.to_int(config.get("recent_days_protect"), 30)
            self._max_delete_gb = DiskSpaceUtils.to_int(config.get("max_delete_gb"), 1000)
            self._protect_dirs = config.get("protect_dirs") or ""
            self._protect_keywords = config.get("protect_keywords") or ""
            self._history_limit = DiskSpaceUtils.to_int(config.get("history_limit"), 50)
            history = config.get("history") or []
            self._history = history if isinstance(history, list) else []
            scan_state = config.get("scan_state") or {}
            self._scan_state = scan_state if isinstance(scan_state, dict) else {}
            self._run_once = DiskSpaceUtils.to_bool(config.get("run_once"), False)
            self._media_server = config.get("media_server") or ""
            self._active_play_protect = DiskSpaceUtils.to_bool(config.get("active_play_protect"), True)
            self._recent_play_days = DiskSpaceUtils.to_int(config.get("recent_play_days"), 7)
            self._scan_cooldown_minutes = DiskSpaceUtils.to_int(config.get("scan_cooldown_minutes"), 360)
            self._scan_backoff_multiplier = max(1, DiskSpaceUtils.to_int(config.get("scan_backoff_multiplier"), 2))
            self._scan_backoff_max_minutes = DiskSpaceUtils.to_int(config.get("scan_backoff_max_minutes"), 1440)
            self._tmdb_top_n = max(1, DiskSpaceUtils.to_int(config.get("tmdb_top_n"), 30))
            strategy_profiles = config.get("strategy_profiles") or ""
            form_strategy_profiles = self._build_strategy_profiles_from_form(config)
            self._strategy_profiles = form_strategy_profiles or strategy_profiles

        self.stop_service()
        if self._run_once:
            logger.info("硬盘空间自动清理收到配置页立即运行请求")
            self._run_once = False
            self._persist_config()
            if self._enabled:
                triggered_immediate_run = True
                threading.Thread(target=lambda: self._run_check(schedule_next=False, trigger="config_run_once"), daemon=True).start()
            else:
                logger.info("硬盘空间自动清理未启用，忽略立即运行请求")

        if self._enabled:
            logger.info(
                f"硬盘空间自动清理已启用：dry_run={self._dry_run}, interval={self._scan_interval_minutes}min, "
                f"min_free={self._min_free_gb}GB, target_free={self._target_free_gb}GB"
            )
            self._schedule_next(initial=not triggered_immediate_run)
        else:
            logger.info("硬盘空间自动清理未启用")

    def get_state(self) -> bool:
        return bool(self._enabled)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    @staticmethod
    def _service_items(services: Dict[str, Any]) -> List[Dict[str, str]]:
        """媒体服务字典转 VSelect items。"""
        items: List[Dict[str, str]] = [{"title": "未选择", "value": ""}]
        for name, conf in (services or {}).items():
            service_type = getattr(conf, "type", "") or ""
            title = f"{name} ({service_type})" if service_type else name
            items.append({"title": title, "value": name})
        return items

    def _parse_strategy_profiles(self) -> List[Dict[str, Any]]:
        """解析多策略配置。格式：空行分隔策略块，每行 key=value。"""
        raw = str(self._strategy_profiles or "").replace(chr(13) + chr(10), chr(10))
        if not raw.strip():
            return []

        def split_list(value: str) -> List[str]:
            value = str(value or "").replace('，', ',').replace(';', ',')
            return [x.strip() for x in value.split(',') if x.strip()]

        profiles: List[Dict[str, str]] = []
        current: Dict[str, str] = {}
        for line in raw.split(chr(10)):
            stripped = line.strip()
            if not stripped:
                if current:
                    profiles.append(current)
                    current = {}
                continue
            if stripped.startswith('#') or '=' not in stripped:
                continue
            k, v = [x.strip() for x in stripped.split('=', 1)]
            current[k.lower()] = v
        if current:
            profiles.append(current)

        parsed: List[Dict[str, Any]] = []
        for idx, item in enumerate(profiles, start=1):
            monitor_path = str(item.get('monitor_path') or '').strip()
            monitor_paths = split_list(item.get('monitor_paths', ''))
            if monitor_path and monitor_path not in monitor_paths:
                monitor_paths = [monitor_path, *monitor_paths]
            media_paths = split_list(item.get('media_paths', ''))
            parsed.append({
                'name': item.get('name') or f'策略{idx}',
                'monitor_path': monitor_paths[0] if monitor_paths else '',
                'monitor_paths': monitor_paths,
                'media_paths': media_paths,
                'min_free_gb': DiskSpaceUtils.to_int(item.get('min_free_gb'), self._min_free_gb),
                'target_free_gb': DiskSpaceUtils.to_int(item.get('target_free_gb'), self._target_free_gb),
                'recent_days_protect': DiskSpaceUtils.to_int(item.get('recent_days_protect'), self._recent_days_protect),
                'recent_play_days': DiskSpaceUtils.to_int(item.get('recent_play_days'), self._recent_play_days),
                'max_delete_gb': DiskSpaceUtils.to_int(item.get('max_delete_gb'), self._max_delete_gb),
                'candidate_depth': DiskSpaceUtils.to_int(item.get('candidate_depth'), self._candidate_depth),
                'max_candidates': DiskSpaceUtils.to_int(item.get('max_candidates'), self._max_candidates),
                'max_scan_items': DiskSpaceUtils.to_int(item.get('max_scan_items'), self._max_scan_items),
                'scan_cooldown_minutes': DiskSpaceUtils.to_int(item.get('scan_cooldown_minutes'), self._scan_cooldown_minutes),
                'scan_backoff_multiplier': max(1, DiskSpaceUtils.to_int(item.get('scan_backoff_multiplier'), self._scan_backoff_multiplier)),
                'scan_backoff_max_minutes': DiskSpaceUtils.to_int(item.get('scan_backoff_max_minutes'), self._scan_backoff_max_minutes),
                'tmdb_top_n': max(1, DiskSpaceUtils.to_int(item.get('tmdb_top_n'), self._tmdb_top_n)),
                'media_server': item.get('media_server') or self._media_server,
                'active_play_protect': DiskSpaceUtils.to_bool(item.get('active_play_protect'), self._active_play_protect),
                'protect_dirs': split_list(item.get('protect_dirs', '')) or DiskSpaceUtils.lines(self._protect_dirs),
                'protect_keywords': split_list(item.get('protect_keywords', '')) or DiskSpaceUtils.lines(self._protect_keywords),
            })
        return parsed

    def _get_effective_monitor_paths(self) -> List[str]:
        result: List[str] = []
        strategies = self._parse_strategy_profiles()
        for strategy in strategies:
            monitor_path = str(strategy.get('monitor_path') or '').strip()
            if monitor_path and monitor_path not in result:
                result.append(monitor_path)
            for item in strategy.get('monitor_paths') or []:
                if item not in result:
                    result.append(item)
        for item in DiskSpaceUtils.lines(self._monitor_paths):
            if item not in result:
                result.append(item)
        return result

    def _build_default_strategy(self, monitor_path: Optional[Path] = None) -> Dict[str, Any]:
        name = monitor_path.as_posix() if monitor_path else '默认策略'
        monitor_path_text = monitor_path.as_posix() if monitor_path else ''
        return {
            'name': name,
            'monitor_path': monitor_path_text,
            'monitor_paths': [monitor_path_text] if monitor_path_text else [],
            'media_paths': DiskSpaceUtils.lines(self._media_paths),
            'min_free_gb': self._min_free_gb,
            'target_free_gb': self._target_free_gb,
            'recent_days_protect': self._recent_days_protect,
            'recent_play_days': self._recent_play_days,
            'max_delete_gb': self._max_delete_gb,
            'candidate_depth': self._candidate_depth,
            'max_candidates': self._max_candidates,
            'max_scan_items': self._max_scan_items,
            'scan_cooldown_minutes': self._scan_cooldown_minutes,
            'scan_backoff_multiplier': self._scan_backoff_multiplier,
            'scan_backoff_max_minutes': self._scan_backoff_max_minutes,
            'tmdb_top_n': self._tmdb_top_n,
            'media_server': self._media_server,
            'active_play_protect': self._active_play_protect,
            'protect_dirs': DiskSpaceUtils.lines(self._protect_dirs),
            'protect_keywords': DiskSpaceUtils.lines(self._protect_keywords),
        }

    def _resolve_strategy_for_monitor(self, monitor_path: Path) -> Dict[str, Any]:
        base = self._build_default_strategy(monitor_path)
        monitor_resolved = monitor_path.resolve(strict=False)
        for strategy in self._parse_strategy_profiles():
            for raw in strategy.get('monitor_paths') or []:
                try:
                    sp = Path(raw).resolve(strict=False)
                    if monitor_resolved == sp or DiskSpaceUtils.is_relative_to(monitor_resolved, sp):
                        merged = dict(base)
                        merged.update(strategy)
                        merged['monitor_path'] = strategy.get('monitor_path') or (strategy.get('monitor_paths') or base.get('monitor_paths') or [''])[0]
                        merged['monitor_paths'] = strategy.get('monitor_paths') or base.get('monitor_paths')
                        merged['media_paths'] = strategy.get('media_paths') or base.get('media_paths')
                        return merged
                except Exception:
                    continue
        return base

    def _apply_strategy_context(self, strategy: Dict[str, Any]) -> Dict[str, Any]:
        mapping = {
            '_media_paths': chr(10).join(strategy.get('media_paths') or []),
            '_min_free_gb': strategy.get('min_free_gb', self._min_free_gb),
            '_target_free_gb': strategy.get('target_free_gb', self._target_free_gb),
            '_recent_days_protect': strategy.get('recent_days_protect', self._recent_days_protect),
            '_recent_play_days': strategy.get('recent_play_days', self._recent_play_days),
            '_max_delete_gb': strategy.get('max_delete_gb', self._max_delete_gb),
            '_candidate_depth': strategy.get('candidate_depth', self._candidate_depth),
            '_max_candidates': strategy.get('max_candidates', self._max_candidates),
            '_max_scan_items': strategy.get('max_scan_items', self._max_scan_items),
            '_scan_cooldown_minutes': strategy.get('scan_cooldown_minutes', self._scan_cooldown_minutes),
            '_scan_backoff_multiplier': strategy.get('scan_backoff_multiplier', self._scan_backoff_multiplier),
            '_scan_backoff_max_minutes': strategy.get('scan_backoff_max_minutes', self._scan_backoff_max_minutes),
            '_tmdb_top_n': strategy.get('tmdb_top_n', self._tmdb_top_n),
            '_media_server': strategy.get('media_server', self._media_server),
            '_active_play_protect': strategy.get('active_play_protect', self._active_play_protect),
            '_protect_dirs': chr(10).join(strategy.get('protect_dirs') or []),
            '_protect_keywords': chr(10).join(strategy.get('protect_keywords') or []),
            '_current_strategy_name': strategy.get('name') or '默认策略',
        }
        previous = {k: getattr(self, k) for k in mapping.keys()}
        for key, value in mapping.items():
            setattr(self, key, value)
        return previous

    def _restore_strategy_context(self, previous: Dict[str, Any]):
        for key, value in (previous or {}).items():
            setattr(self, key, value)

    def _build_strategy_profiles_from_form(self, config: Dict[str, Any]) -> str:
        """从可视化表单字段拼回 strategy_profiles 文本，兼容旧版文本配置。"""
        if not isinstance(config, dict):
            return ""

        def norm_lines(value: Any) -> str:
            return "\n".join([x.strip() for x in str(value or "").replace("\r\n", "\n").split("\n") if x.strip()])

        def norm_csv(value: Any) -> str:
            text = str(value or "").replace("\r\n", "\n")
            if "\n" in text:
                return ",".join([x.strip() for x in text.split("\n") if x.strip()])
            parts = []
            for raw in text.replace("，", ",").replace(";", ",").split(","):
                raw = raw.strip()
                if raw:
                    parts.append(raw)
            return ",".join(parts)

        blocks: List[str] = []
        for idx in range(1, self._max_strategy_slots + 1):
            prefix = f"strategy_{idx}_"
            name = str(config.get(prefix + "name") or "").strip()
            monitor_path = str(config.get(prefix + "monitor_path") or "").strip()
            media_paths = norm_csv(config.get(prefix + "media_paths"))
            if not name and not monitor_path and not media_paths:
                continue
            lines = [f"name={name or f'策略{idx}'}"]
            if monitor_path:
                lines.append(f"monitor_path={monitor_path}")
                lines.append(f"monitor_paths={monitor_path}")
            for key in ["media_paths", "min_free_gb", "target_free_gb", "recent_days_protect", "recent_play_days", "max_delete_gb", "media_server", "candidate_depth", "max_candidates", "max_scan_items", "scan_cooldown_minutes", "scan_backoff_multiplier", "scan_backoff_max_minutes", "tmdb_top_n"]:
                value = config.get(prefix + key)
                if value is None or str(value).strip() == "":
                    continue
                if key == "media_paths":
                    value = norm_csv(value)
                lines.append(f"{key}={value}")
            active_play = config.get(prefix + "active_play_protect")
            if active_play is not None:
                lines.append(f"active_play_protect={'true' if DiskSpaceUtils.to_bool(active_play, True) else 'false'}")
            protect_keywords = norm_lines(config.get(prefix + "protect_keywords")).replace(chr(10), ',')
            if protect_keywords:
                lines.append(f"protect_keywords={protect_keywords}")
            protect_dirs = norm_lines(config.get(prefix + "protect_dirs"))
            if protect_dirs:
                lines.append(f"protect_dirs={protect_dirs.replace(chr(10), ',')}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _strategy_form_defaults(self, default_media_server: str) -> List[Dict[str, Any]]:
        templates = [
            {"name": "硬盘3-外语电影", "recent_days_protect": 15, "recent_play_days": 7, "max_delete_gb": 150},
            {"name": "硬盘4-国产电视剧", "recent_days_protect": 45, "recent_play_days": 20, "max_delete_gb": 60},
            {"name": "硬盘5-欧美电视剧", "recent_days_protect": 25, "recent_play_days": 15, "max_delete_gb": 90},
        ]
        parsed = self._parse_strategy_profiles()
        slot_count = max(3, min(self._max_strategy_slots, len(parsed) + 1))
        results: List[Dict[str, Any]] = []
        for idx in range(slot_count):
            template = templates[idx] if idx < len(templates) else {
                "name": f"策略{idx + 1}",
                "recent_days_protect": self._recent_days_protect,
                "recent_play_days": self._recent_play_days,
                "max_delete_gb": self._max_delete_gb,
            }
            base = {
                "name": template["name"],
                "monitor_path": "",
                "media_paths": "",
                "min_free_gb": self._min_free_gb,
                "target_free_gb": self._target_free_gb,
                "recent_days_protect": template["recent_days_protect"],
                "recent_play_days": template["recent_play_days"],
                "max_delete_gb": template["max_delete_gb"],
                "scan_cooldown_minutes": self._scan_cooldown_minutes,
                "scan_backoff_multiplier": self._scan_backoff_multiplier,
                "scan_backoff_max_minutes": self._scan_backoff_max_minutes,
                "tmdb_top_n": self._tmdb_top_n,
                "media_server": default_media_server,
                "active_play_protect": self._active_play_protect,
                "protect_keywords": "",
            }
            if idx < len(parsed):
                item = parsed[idx]
                base.update({
                    "name": item.get("name") or base["name"],
                    "monitor_path": item.get("monitor_path") or "",
                    "media_paths": "\n".join(item.get("media_paths") or []),
                    "min_free_gb": item.get("min_free_gb", base["min_free_gb"]),
                    "target_free_gb": item.get("target_free_gb", base["target_free_gb"]),
                    "recent_days_protect": item.get("recent_days_protect", base["recent_days_protect"]),
                    "recent_play_days": item.get("recent_play_days", base["recent_play_days"]),
                    "max_delete_gb": item.get("max_delete_gb", base["max_delete_gb"]),
                    "scan_cooldown_minutes": item.get("scan_cooldown_minutes", base["scan_cooldown_minutes"]),
                    "scan_backoff_multiplier": item.get("scan_backoff_multiplier", base["scan_backoff_multiplier"]),
                    "scan_backoff_max_minutes": item.get("scan_backoff_max_minutes", base["scan_backoff_max_minutes"]),
                    "tmdb_top_n": item.get("tmdb_top_n", base["tmdb_top_n"]),
                    "media_server": item.get("media_server") or base["media_server"],
                    "active_play_protect": item.get("active_play_protect", base["active_play_protect"]),
                    "protect_keywords": "\n".join(item.get("protect_keywords") or []),
                })
            results.append(base)
        return results

    def _build_strategy_form_cards(self, mediaserver_items: List[Dict[str, Any]], strategies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        cards: List[Dict[str, Any]] = []
        for idx, strategy in enumerate(strategies, start=1):
            prefix = f"strategy_{idx}_"
            cards.append({
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{
                    "component": "VCard",
                    "props": {"variant": "outlined", "class": "mb-2"},
                    "content": [
                        {"component": "VCardTitle", "text": f"策略{idx}配置"},
                        {"component": "VCardText", "props": {"class": "pt-0 text-caption"}, "text": "每张卡片对应一块硬盘和它的媒体路径；监控盘路径为空时，该策略不生效。"},
                        {
                            "component": "VRow",
                            "content": [
                                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": prefix + "name", "label": "策略名称", "placeholder": strategy.get("name") or f"策略{idx}"}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSelect", "props": {"model": prefix + "media_server", "label": "媒体服务器", "items": mediaserver_items, "hint": "直接选择 MoviePilot 已配置媒体服务器", "persistent-hint": True}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": prefix + "active_play_protect", "label": "正在播放保护"}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": prefix + "monitor_path", "label": "监控盘路径", "placeholder": "/硬盘3", "hint": "一盘一个策略；填写这张卡对应的唯一监控路径", "persistent-hint": True}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextarea", "props": {"model": prefix + "media_paths", "label": "媒体扫描路径", "rows": 2, "placeholder": "/link3/外语电影", "hint": "每行一个；只扫描这块盘对应的媒体路径"}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": prefix + "min_free_gb", "label": "触发剩余GB", "type": "number"}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": prefix + "target_free_gb", "label": "目标剩余GB", "type": "number"}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": prefix + "recent_days_protect", "label": "最近新增保护天数", "type": "number"}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": prefix + "recent_play_days", "label": "最近播放降权天数", "type": "number"}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": prefix + "max_delete_gb", "label": "每次删除最大GB", "type": "number"}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": prefix + "scan_cooldown_minutes", "label": "扫描冷却分钟", "type": "number", "hint": "持续低空间时，本策略两次候选扫描的最短间隔"}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": prefix + "scan_backoff_multiplier", "label": "退避倍率", "type": "number", "hint": "连续低空间时，扫描冷却会按倍率递增"}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": prefix + "scan_backoff_max_minutes", "label": "最大退避分钟", "type": "number"}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": prefix + "tmdb_top_n", "label": "TMDB 精排前N项", "type": "number", "hint": "只对前 N 个初筛候选做 TMDB 评分修正"}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": prefix + "max_candidates", "label": "最多候选数量", "type": "number"}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": prefix + "max_scan_items", "label": "最大扫描条目", "type": "number"}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": prefix + "candidate_depth", "label": "候选扫描深度", "type": "number"}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextarea", "props": {"model": prefix + "protect_dirs", "label": "保护目录", "rows": 2, "placeholder": "/link4/国产剧/保留", "hint": "每行一个；命中这些目录时跳过"}}]},
                                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextarea", "props": {"model": prefix + "protect_keywords", "label": "保护关键词", "rows": 2, "placeholder": "收藏\n经典\n在追", "hint": "每行一个；命中即跳过"}}]},
                            ]
                        }
                    ]
                }]
            })
        return cards

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/run_now",
                "endpoint": self.run_now,
                "summary": "立即运行空间检查",
                "description": "手动触发硬盘空间检查并生成清理建议，不受定时检查间隔限制。",
                "methods": ["POST"]
            }
        ]

    def run_now(self):
        """插件 API：立即运行一次空间检查。"""
        try:
            logger.info("硬盘空间自动清理收到 API 立即运行请求")
            self.stop_service()
            if self._enabled:
                self._schedule_next(initial=False)
            threading.Thread(target=lambda: self._run_check(schedule_next=False, trigger="api_run_now"), daemon=True).start()
            return {"success": True, "message": "已开始后台执行空间检查"}
        except Exception as e:
            logger.error(f"硬盘空间自动清理 API 立即运行失败：{e}", exc_info=True)
            return {"success": False, "message": f"立即运行失败：{e}"}

    def stop_service(self):
        with self._lock:
            if self._timer:
                try:
                    self._timer.cancel()
                except Exception:
                    pass
                self._timer = None

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        mediaserver_items = self._service_items(MediaServerHelper().get_configs())
        default_media_server = self._media_server if any(item.get("value") == self._media_server for item in mediaserver_items) else ""
        strategy_defaults = self._strategy_form_defaults(default_media_server)
        strategy_cards = self._build_strategy_form_cards(mediaserver_items, strategy_defaults)
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
                                "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "dry_run", "label": "仅生成报告（不删除）", "hint": "开启时只给出清理建议，不删除文件；关闭后才会执行自动清理"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "notify", "label": "发送通知"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "run_once", "label": "保存后立即运行一次", "hint": "打开后保存配置，会立刻执行一次检查并自动关闭"}}]
                            },
                            *strategy_cards,
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "已改为一盘一个策略：每张卡只配置一块盘和它对应的媒体路径。当前界面最多展示 8 个策略位；旧版全局监控路径、默认扫描路径、路径映射仅作兼容读取，不再作为主配置入口。"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VTextField", "props": {"model": "scan_interval_minutes", "label": "检查间隔分钟", "type": "number", "placeholder": "60"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VTextField", "props": {"model": "history_limit", "label": "历史记录保留条数", "type": "number", "placeholder": "50"}}]
                            },
                        ]
                    }
                ]
            }
        ], self._build_form_data(strategy_defaults)

    def _build_form_data(self, strategy_defaults: List[Dict[str, Any]]) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "enabled": self._enabled,
            "dry_run": self._dry_run,
            "notify": self._notify,
            "run_once": False,
            "scan_interval_minutes": self._scan_interval_minutes,
            "history_limit": self._history_limit,
            "history": self._history,
            "scan_state": self._scan_state,
            "strategy_profiles": self._strategy_profiles,
            "sources": "immediate",
        }
        for idx, strategy in enumerate(strategy_defaults, start=1):
            prefix = f"strategy_{idx}_"
            data[prefix + "name"] = strategy.get("name") or f"策略{idx}"
            data[prefix + "monitor_path"] = strategy.get("monitor_path") or ""
            data[prefix + "media_paths"] = strategy.get("media_paths") or ""
            data[prefix + "min_free_gb"] = strategy.get("min_free_gb", self._min_free_gb)
            data[prefix + "target_free_gb"] = strategy.get("target_free_gb", self._target_free_gb)
            data[prefix + "recent_days_protect"] = strategy.get("recent_days_protect", self._recent_days_protect)
            data[prefix + "recent_play_days"] = strategy.get("recent_play_days", self._recent_play_days)
            data[prefix + "max_delete_gb"] = strategy.get("max_delete_gb", self._max_delete_gb)
            data[prefix + "scan_cooldown_minutes"] = strategy.get("scan_cooldown_minutes", self._scan_cooldown_minutes)
            data[prefix + "scan_backoff_multiplier"] = strategy.get("scan_backoff_multiplier", self._scan_backoff_multiplier)
            data[prefix + "scan_backoff_max_minutes"] = strategy.get("scan_backoff_max_minutes", self._scan_backoff_max_minutes)
            data[prefix + "tmdb_top_n"] = strategy.get("tmdb_top_n", self._tmdb_top_n)
            data[prefix + "media_server"] = strategy.get("media_server") or ""
            data[prefix + "active_play_protect"] = strategy.get("active_play_protect", self._active_play_protect)
            data[prefix + "protect_keywords"] = strategy.get("protect_keywords") or ""
            data[prefix + "protect_dirs"] = strategy.get("protect_dirs") or ""
            data[prefix + "max_candidates"] = strategy.get("max_candidates", self._max_candidates)
            data[prefix + "max_scan_items"] = strategy.get("max_scan_items", self._max_scan_items)
            data[prefix + "candidate_depth"] = strategy.get("candidate_depth", self._candidate_depth)
        return data

    def get_page(self) -> List[dict]:
        history = list(self._history or [])[: self._history_limit]
        if not history:
            return [
                {
                    "component": "VAlert",
                    "props": {"type": "info", "variant": "tonal", "text": "暂无硬盘空间检查记录。启用插件后会按间隔检查并生成候选媒体海报。"}
                },
                {
                    "component": "VCard",
                    "props": {"class": "mb-4"},
                    "content": [
                        {
                            "component": "VCardText",
                            "content": [
                                {"component": "div", "content": "点击下方按钮立即执行硬盘空间检查；页面会分别展示待删除候选和已删除记录。"}
                            ],
                        },
                        {
                            "component": "VCardActions",
                            "props": {"class": "justify-end"},
                            "content": [
                                {"component": "VBtn", "props": {"text": "立即运行检查", "color": "primary", "variant": "outlined", "action": "plugin_run_now"}}
                            ]
                        }
                    ]
                },
            ]

        latest_by_strategy: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in history:
            strategy_name = item.get("strategy_name") or item.get("monitor_path") or "默认策略"
            if strategy_name in seen:
                continue
            seen.add(strategy_name)
            latest_by_strategy.append(item)

        latest_deleted_by_strategy: Dict[str, Dict[str, Any]] = {}
        for item in history:
            strategy_name = item.get("strategy_name") or item.get("monitor_path") or "默认策略"
            if strategy_name in latest_deleted_by_strategy:
                continue
            if self._build_deleted_candidates(item):
                latest_deleted_by_strategy[strategy_name] = item

        latest_strategy_map: Dict[str, Dict[str, Any]] = {
            (item.get("strategy_name") or item.get("monitor_path") or "默认策略"): item
            for item in latest_by_strategy
        }
        strategy_totals: Dict[str, Dict[str, Any]] = self._build_strategy_totals(history, latest_strategy_map)

        overview_items: List[Dict[str, Any]] = []
        merged_pending_candidates: List[Dict[str, Any]] = []
        merged_deleted_candidates: List[Dict[str, Any]] = []
        for item in latest_by_strategy:
            pending_candidates = self._build_pending_candidates(item)
            strategy_name = item.get("strategy_name") or item.get("monitor_path") or "默认策略"
            deleted_record = latest_deleted_by_strategy.get(strategy_name)
            deleted_candidates = self._build_deleted_candidates(deleted_record) if deleted_record else []
            overview_items.append({
                "strategy_name": strategy_name,
                "monitor_path": item.get("monitor_path") or "-",
                "free_text": item.get("free_text") or "-",
                "summary": item.get("summary") or "-",
                "time": item.get("time") or "-",
                "pending_count": len(pending_candidates),
                "deleted_count": len(deleted_candidates),
            })
            for candidate in pending_candidates:
                enriched = dict(candidate)
                enriched["strategy_name"] = strategy_name
                enriched["record_time"] = item.get("time") or "-"
                enriched["monitor_path"] = item.get("monitor_path") or "-"
                merged_pending_candidates.append(enriched)
            for candidate in deleted_candidates:
                enriched = dict(candidate)
                enriched["strategy_name"] = strategy_name
                enriched["record_time"] = (deleted_record or {}).get("time") or item.get("time") or "-"
                enriched["monitor_path"] = (deleted_record or {}).get("monitor_path") or item.get("monitor_path") or "-"
                merged_deleted_candidates.append(enriched)

        merged_pending_candidates.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
        merged_deleted_candidates.sort(key=lambda x: float(x.get("score") or 0), reverse=True)

        total_deleted_count = len(merged_deleted_candidates)
        total_deleted_gb = sum(float(x.get("size_gb") or 0) for x in merged_deleted_candidates)
        total_pending_count = len(merged_pending_candidates)
        total_pending_gb = sum(float(x.get("size_gb") or 0) for x in merged_pending_candidates)
        historical_deleted_count = sum(int(item.get("deleted_count") or 0) for item in strategy_totals.values())
        historical_deleted_gb = sum(float(item.get("deleted_gb") or 0) for item in strategy_totals.values())
        latest_time = history[0].get("time") or "-"

        page: List[dict] = []
        page.append(self._build_stats_overview_panel(
            historical_deleted_count=historical_deleted_count,
            historical_deleted_gb=historical_deleted_gb,
            total_pending_count=total_pending_count,
            total_pending_gb=total_pending_gb,
            total_deleted_count=total_deleted_count,
            total_deleted_gb=total_deleted_gb,
            strategy_count=len(latest_by_strategy),
            latest_time=latest_time,
        ))
        page.append(self._build_strategy_summary_panel(overview_items, strategy_totals))

        if merged_deleted_candidates:
            page.append(self._build_latest_candidates_panel(
                merged_deleted_candidates,
                title="全局已删除",
                subtitle="",
                empty_text="当前暂无已删除媒体记录。",
                status_label="已删除"
            ))

        if merged_pending_candidates:
            page.append(self._build_latest_candidates_panel(
                merged_pending_candidates,
                title="全局待删除候选海报墙",
                subtitle="这里展示当前仍存在的候选媒体海报；按删除优先级从高到低排序。",
                empty_text="当前暂无待删除候选媒体。",
                status_label="待删除"
            ))

        for item in latest_by_strategy:
            strategy_name = item.get("strategy_name") or item.get("monitor_path") or "默认策略"
            pending_candidates = self._build_pending_candidates(item)
            if pending_candidates:
                enriched_pending_candidates: List[Dict[str, Any]] = []
                for candidate in sorted(pending_candidates, key=lambda x: float(x.get("score") or 0), reverse=True):
                    enriched = dict(candidate)
                    enriched["strategy_name"] = strategy_name
                    enriched["record_time"] = item.get("time") or "-"
                    enriched["monitor_path"] = item.get("monitor_path") or "-"
                    enriched_pending_candidates.append(enriched)
                page.append(self._build_latest_candidates_panel(
                    enriched_pending_candidates,
                    title=f"{strategy_name} · 待删除候选",
                    subtitle=f"监控盘：{item.get('monitor_path') or '-'}｜最近记录：{item.get('time') or '-'}",
                    empty_text=f"{strategy_name} 暂无待删除候选媒体。",
                    status_label="待删除"
                ))

        if not merged_deleted_candidates and not merged_pending_candidates:
            page.append({
                "component": "VAlert",
                "props": {"type": "warning", "variant": "tonal", "text": "当前没有可展示的数据。可能空间充足、候选已被删除，或扫描路径不存在。"}
            })

        return page

    def _build_pending_candidates(self, record: Dict[str, Any]) -> List[Dict[str, Any]]:
        candidates = record.get("all_candidates") or record.get("candidates") or []
        if self._is_deleted_record(record):
            deleted_paths = {
                str(item.get("path") or "")
                for item in (record.get("deleted_candidates") or record.get("candidates") or [])
                if item.get("path")
            }
            pending = [item for item in candidates if str(item.get("path") or "") not in deleted_paths]
            return self._filter_existing_candidates(pending)
        return self._filter_existing_candidates(candidates)

    def _build_deleted_candidates(self, record: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self._is_deleted_record(record):
            return []
        deleted = record.get("deleted_candidates") or record.get("candidates") or []
        return list(deleted)

    @staticmethod
    def _is_deleted_record(record: Dict[str, Any]) -> bool:
        mode = str(record.get("record_mode") or "").strip().lower()
        if mode:
            return mode == "deleted"
        summary = str(record.get("summary") or "")
        return "已执行自动清理" in summary

    def _filter_existing_candidates(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for item in candidates or []:
            try:
                path = item.get("path")
                if path and Path(path).exists():
                    result.append(item)
            except Exception:
                continue
        return result


    def _build_stats_overview_panel(self, historical_deleted_count: int, historical_deleted_gb: float,
                                    total_pending_count: int, total_pending_gb: float,
                                    total_deleted_count: int, total_deleted_gb: float,
                                    strategy_count: int, latest_time: str) -> Dict[str, Any]:
        latest_scan_short = latest_time[5:16] if latest_time and latest_time != "-" and len(latest_time) >= 16 else latest_time
        metrics = [
            {
                "title": "历史总删除",
                "value": f"{historical_deleted_count}",
                "unit": "项",
                "subtitle": "累计释放空间",
                "highlight": self._format_size_text(historical_deleted_gb),
                "color": "error",
                "icon": "🗑️",
                "surface": "累计成果",
                "accent": f"已沉淀 {historical_deleted_count} 条删除记录",
            },
            {
                "title": "当前待删除",
                "value": f"{total_pending_count}",
                "unit": "项",
                "subtitle": "预计释放空间",
                "highlight": self._format_size_text(total_pending_gb),
                "color": "warning",
                "icon": "📦",
                "surface": "当前压力",
                "accent": "按候选优先级持续滚动",
            },
            {
                "title": "最近已删除",
                "value": f"{total_deleted_count}",
                "unit": "项",
                "subtitle": "最近展示释放",
                "highlight": self._format_size_text(total_deleted_gb),
                "color": "success",
                "icon": "✅",
                "surface": "最新结果",
                "accent": "方便回看最近实际清掉的内容",
            },
            {
                "title": "策略数量",
                "value": f"{strategy_count}",
                "unit": "个",
                "subtitle": "最近扫描时间",
                "highlight": latest_scan_short,
                "color": "primary",
                "icon": "📊",
                "surface": "运行状态",
                "accent": f"最近扫描：{latest_time}",
            },
        ]
        return {
            "component": "VCard",
            "props": {"class": "mb-4 overflow-hidden"},
            "content": [
                {
                    "component": "div",
                    "props": {"class": "px-4 pt-4 pb-1 d-flex flex-wrap align-center justify-space-between ga-2"},
                    "content": [
                        {
                            "component": "div",
                            "content": [
                                {"component": "VCardTitle", "props": {"class": "px-0 pt-1 pb-1 text-h5"}, "text": "历史总计"},
                                {"component": "VCardText", "props": {"class": "px-0 pt-0 pb-1 text-caption"}, "text": ""},
                            ]
                        }
                    ]
                },
                {
                    "component": "div",
                    "props": {"class": "px-4 pb-2"},
                    "content": [
                        {
                            "component": "VCard",
                            "props": {"variant": "outlined", "class": "px-1 py-1"},
                            "content": [
                                {
                                    "component": "VRow",
                                    "props": {"class": "ma-0", "dense": True},
                                    "content": [
                                        {
                                            "component": "VCol",
                                            "props": {"cols": 12, "md": 3, "class": "py-2"},
                                            "content": [
                                                {"component": "div", "props": {"class": "px-3 text-caption text-medium-emphasis"}, "text": "历史总释放"},
                                                {"component": "div", "props": {"class": "px-3 text-subtitle-1 font-weight-bold"}, "text": self._format_size_text(historical_deleted_gb)},
                                            ]
                                        },
                                        {
                                            "component": "VCol",
                                            "props": {"cols": 12, "md": 3, "class": "py-2"},
                                            "content": [
                                                {"component": "div", "props": {"class": "px-3 text-caption text-medium-emphasis"}, "text": "待删释放预估"},
                                                {"component": "div", "props": {"class": "px-3 text-subtitle-1 font-weight-bold"}, "text": self._format_size_text(total_pending_gb)},
                                            ]
                                        },
                                        {
                                            "component": "VCol",
                                            "props": {"cols": 12, "md": 3, "class": "py-2"},
                                            "content": [
                                                {"component": "div", "props": {"class": "px-3 text-caption text-medium-emphasis"}, "text": "最近删除释放"},
                                                {"component": "div", "props": {"class": "px-3 text-subtitle-1 font-weight-bold"}, "text": self._format_size_text(total_deleted_gb)},
                                            ]
                                        },
                                        {
                                            "component": "VCol",
                                            "props": {"cols": 12, "md": 3, "class": "py-2"},
                                            "content": [
                                                {"component": "div", "props": {"class": "px-3 text-caption text-medium-emphasis"}, "text": "当前活跃策略"},
                                                {"component": "div", "props": {"class": "px-3 text-subtitle-1 font-weight-bold"}, "text": f"{strategy_count} 个"},
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                },
                {
                    "component": "VRow",
                    "props": {"class": "px-2 pb-3"},
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "sm": 6, "md": 3},
                            "content": [{
                                "component": "VCard",
                                "props": {"variant": "tonal", "color": metric.get("color"), "class": "h-100 overflow-hidden"},
                                "content": [
                                    {
                                        "component": "div",
                                        "props": {"class": "px-4 pt-3 pb-1 d-flex align-center justify-space-between"},
                                        "content": [
                                            {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": metric.get("surface")},
                                            {"component": "div", "props": {"class": "text-h6"}, "text": metric.get("icon")},
                                        ]
                                    },
                                    {"component": "VCardText", "props": {"class": "pb-1 text-caption text-medium-emphasis"}, "text": metric.get("title")},
                                    {
                                        "component": "div",
                                        "props": {"class": "px-4 d-flex align-end ga-2"},
                                        "content": [
                                            {"component": "div", "props": {"class": "text-h3 font-weight-bold lh-1"}, "text": metric.get("value")},
                                            {"component": "div", "props": {"class": "text-caption pb-1 text-medium-emphasis"}, "text": metric.get("unit")},
                                        ]
                                    },
                                    {"component": "VCardText", "props": {"class": "pt-2 pb-0 text-caption text-medium-emphasis"}, "text": metric.get("subtitle")},
                                    {"component": "VCardText", "props": {"class": "pt-1 pb-1 text-subtitle-2 font-weight-medium"}, "text": metric.get("highlight")},
                                    {"component": "VCardText", "props": {"class": "pt-0 pb-3 text-caption text-medium-emphasis"}, "text": metric.get("accent")},
                                ]
                            }]
                        }
                        for metric in metrics
                    ]
                }
            ]
        }

    def _build_strategy_summary_panel(self, items: List[Dict[str, Any]], strategy_totals: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        if not items:
            return {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "text": "暂无策略统计数据。"}
            }

        cards = []
        for item in items:
            strategy_name = item.get("strategy_name") or "默认策略"
            totals = strategy_totals.get(strategy_name) or {}
            cards.append({
                "component": "VCol",
                "props": {"cols": 12, "sm": 6, "lg": 4},
                "content": [{
                    "component": "VCard",
                    "props": {"variant": "outlined", "class": "h-100"},
                    "content": [
                        {"component": "VCardTitle", "props": {"class": "pb-1 text-subtitle-1"}, "text": strategy_name},
                        {"component": "VCardText", "props": {"class": "pt-0 text-caption"}, "text": f"监控盘：{item.get('monitor_path') or '-'}"},
                        {"component": "div", "props": {"class": "px-4 pb-2 d-flex flex-wrap ga-2"}, "content": [
                            {"component": "VChip", "props": {"size": "small", "variant": "tonal", "color": "error"}, "text": f"累计删除 {int(totals.get('deleted_count') or 0)} 项"},
                            {"component": "VChip", "props": {"size": "small", "variant": "tonal", "color": "success"}, "text": f"累计释放 {self._format_size_text(float(totals.get('deleted_gb') or 0))}"},
                            {"component": "VChip", "props": {"size": "small", "variant": "tonal", "color": "warning"}, "text": f"当前待删 {int(item.get('pending_count') or 0)} 项"},
                            {"component": "VChip", "props": {"size": "small", "variant": "tonal", "color": "primary"}, "text": f"扫描 {int(totals.get('scan_count') or 0)} 次"},
                        ]},
                    ]
                }]
            })

        return {
            "component": "VCard",
            "props": {"class": "mb-4"},
            "content": [
                {"component": "VCardTitle", "text": "策略统计"},
                {"component": "VCardText", "props": {"class": "pt-0 text-caption"}, "text": ""},
                {"component": "VRow", "props": {"class": "px-2 pb-4"}, "content": cards}
            ]
        }

    def _build_strategy_totals(self, history: List[Dict[str, Any]], latest_strategy_map: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        totals: Dict[str, Dict[str, Any]] = {}
        for record in history:
            strategy_name = record.get("strategy_name") or record.get("monitor_path") or "默认策略"
            item = totals.setdefault(strategy_name, {
                "deleted_count": 0,
                "deleted_gb": 0.0,
                "scan_count": 0,
            })
            item["scan_count"] += 1
            deleted_candidates = self._build_deleted_candidates(record)
            item["deleted_count"] += len(deleted_candidates)
            item["deleted_gb"] += sum(float(x.get("size_gb") or 0) for x in deleted_candidates)

        for strategy_name, latest in latest_strategy_map.items():
            item = totals.setdefault(strategy_name, {
                "deleted_count": 0,
                "deleted_gb": 0.0,
                "scan_count": 0,
            })
            item["monitor_path"] = latest.get("monitor_path") or "-"
            item["latest_time"] = latest.get("time") or "-"
            item["latest_free_text"] = latest.get("free_text") or "-"
            item["latest_summary"] = latest.get("summary") or "-"
        return totals

    @staticmethod
    def _format_size_text(size_gb: float) -> str:
        if size_gb >= 1024:
            return f"{size_gb / 1024:.2f}TB"
        return f"{size_gb:.2f}GB"

    def _build_latest_candidates_panel(self, candidates: List[Dict[str, Any]], title: str = "待删除候选媒体海报墙",
                                       subtitle: Optional[str] = None, empty_text: Optional[str] = None,
                                       status_label: str = "待删除") -> Dict[str, Any]:
        """构建媒体海报墙：可展示待删除候选，也可展示已删除记录。"""
        if not candidates:
            return {
                "component": "VAlert",
                "props": {"type": "warning", "variant": "tonal", "text": empty_text or f"{title} 暂无可展示数据。"}
            }

        cards = []
        for idx, item in enumerate(candidates[:12], start=1):
            cards.append(self._build_candidate_card(item, idx, status_label=status_label))

        return {
            "component": "VCard",
            "props": {"class": "mb-4"},
            "content": [
                {
                    "component": "VCardTitle",
                    "props": {"class": "pb-1"},
                    "text": title
                },
                {
                    "component": "VCardText",
                    "props": {"class": "pt-0 text-caption"},
                    "text": subtitle or "按候选评分从高到低排列；优先看海报、体积、天数和评分来判断删谁。"
                },
                {
                    "component": "VCardText",
                    "props": {"class": "pt-0 text-caption text-medium-emphasis"},
                    "text": ""
                },
                {
                    "component": "VRow",
                    "props": {"class": "pa-2"},
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "sm": 6, "md": 4, "lg": 3},
                            "content": [card],
                        }
                        for card in cards
                    ],
                }
            ]
        }

    def _build_candidate_card(self, item: Dict[str, Any], rank: int, status_label: str = "待删除") -> Dict[str, Any]:
        poster = self._resolve_candidate_poster(item)
        poster_src = poster or self._blank_poster
        name = item.get("tmdb_title") or item.get("name") or "未知媒体"
        title = str(name)
        if len(title) > 18:
            title = title[:18] + "..."
        score = float(item.get("score") or 0)
        tmdb_rating = item.get("tmdb_rating")
        tmdb_vote_count = item.get("tmdb_vote_count")
        tmdb_reason = item.get("tmdb_reason") or "TMDB评分未参与"
        tmdb_id = item.get("tmdb_id")
        tmdb_type = item.get("tmdb_type") or "movie"
        href = f"https://www.themoviedb.org/{tmdb_type}/{tmdb_id}" if tmdb_id else "#"
        if status_label == "已删除":
            rank_text = "✅ 最近已删除" if rank == 1 else ("🗑️ 已删除" if rank <= 3 else f"#{rank}")
        else:
            rank_text = "🥇 当前最优先删除" if rank == 1 else ("🔥 高优先级" if rank <= 3 else f"#{rank}")

        activity_reason = item.get("activity_reason") or "未命中播放保护/最近播放降权"
        size_text = f"{float(item.get('size_gb') or 0):.2f}GB"
        age_text = f"{item.get('age_days') or 0}天"
        strategy_name = item.get("strategy_name") or "-"
        monitor_path = item.get("monitor_path") or "-"
        record_time = item.get("record_time") or "-"
        path_text = item.get("path") or ""
        short_path = path_text if len(path_text) <= 52 else f"...{path_text[-52:]}"
        score_color = "success" if status_label == "已删除" else ("error" if rank == 1 else ("warning" if rank <= 3 else "primary"))

        meta_chips = [
            {"label": status_label, "color": "success" if status_label == "已删除" else "error"},
            {"label": f"评分 {score:.2f}", "color": "primary"},
            {"label": size_text, "color": "warning"},
            {"label": age_text, "color": "secondary"},
        ]
        if tmdb_rating is not None:
            meta_chips.append({"label": f"TMDB {tmdb_rating}", "color": "success"})

        return {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "overflow-hidden h-100"},
            "content": [
                {
                    "component": "div",
                    "props": {"class": "position-relative"},
                    "content": [
                        {
                            "component": "VImg",
                            "props": {
                                "src": poster_src,
                                "height": 300,
                                "cover": True,
                                "class": "object-cover",
                            }
                        },
                        {
                            "component": "div",
                            "props": {"class": "position-absolute top-0 left-0 right-0 d-flex justify-space-between align-start pa-3"},
                            "content": [
                                {
                                    "component": "VChip",
                                    "props": {"size": "small", "color": "grey-darken-3", "variant": "flat"},
                                    "text": status_label,
                                },
                                {
                                    "component": "VChip",
                                    "props": {"size": "small", "color": score_color, "variant": "flat"},
                                    "text": rank_text,
                                },
                            ]
                        },
                        {
                            "component": "div",
                            "props": {"class": "position-absolute left-0 right-0 bottom-0 pa-3"},
                            "content": [
                                {
                                    "component": "VCard",
                                    "props": {"variant": "tonal", "color": "grey-darken-4"},
                                    "content": [
                                        {
                                            "component": "VCardTitle",
                                            "props": {"class": "pb-1 text-subtitle-1 text-white"},
                                            "content": [{"component": "a", "props": {"href": href, "target": "_blank"}, "text": title}],
                                        },
                                        {
                                            "component": "VCardText",
                                            "props": {"class": "pt-0 pb-2 text-caption text-white"},
                                            "text": f"{strategy_name} · {size_text} · {age_text}",
                                        },
                                    ]
                                }
                            ]
                        }
                    ]
                },
                {
                    "component": "VCardText",
                    "props": {"class": "pt-3 pb-2"},
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "d-flex flex-wrap ga-2"},
                            "content": [
                                {"component": "VChip", "props": {"size": "small", "color": chip.get("color"), "variant": "tonal"}, "text": chip.get("label")}
                                for chip in meta_chips
                            ]
                        }
                    ]
                },
                {
                    "component": "VCardText",
                    "props": {"class": "py-0"},
                    "content": [
                        {
                            "component": "VRow",
                            "props": {"class": "ma-0"},
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {"cols": 4, "class": "py-1"},
                                    "content": [
                                        {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": "删除评分"},
                                        {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold"}, "text": f"{score:.2f}"},
                                    ]
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 4, "class": "py-1"},
                                    "content": [
                                        {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": "所属策略"},
                                        {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold text-truncate"}, "text": strategy_name},
                                    ]
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 4, "class": "py-1"},
                                    "content": [
                                        {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": "记录时间"},
                                        {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold"}, "text": record_time[5:16] if len(record_time) >= 16 else record_time},
                                    ]
                                },
                            ]
                        }
                    ]
                },
                {"component": "VDivider"},
            ]
        }

    def _resolve_candidate_poster(self, item: Dict[str, Any]) -> Optional[str]:
        """页面展示时按豆瓣榜单插件口径取海报：只认已保存的 MoviePilot poster。"""
        poster = item.get("poster")
        if poster:
            return poster
        return None

    def _get_media_server_service(self, name: str):
        """获取 MoviePilot 已配置的媒体服务器服务实例。"""
        try:
            return MediaServerHelper().get_service(name=name)
        except Exception as e:
            logger.warning(f"获取媒体服务器服务失败：{name} - {e}")
            return None

    def _schedule_next(self, initial: bool = False):
        if not self._enabled:
            return
        delay = 5 if initial else max(60, int(self._scan_interval_minutes or 60) * 60)
        timer = threading.Timer(delay, self._run_check)
        timer.daemon = True
        with self._lock:
            self._timer = timer
        timer.start()

    def _run_check(self, schedule_next: bool = True, trigger: str = "scheduled"):
        now = time.time()
        with self._lock:
            if self._check_running:
                logger.info(f"硬盘空间自动清理已有检查正在进行，跳过本次{trigger}触发，避免重复扫盘")
                should_run = False
            elif self._last_check_started_at and now - self._last_check_started_at < self._dedupe_window_seconds:
                logger.info(
                    f"距离上次检查仅 {now - self._last_check_started_at:.1f} 秒，跳过本次{trigger}触发，避免重复扫盘"
                )
                should_run = False
            else:
                self._check_running = True
                self._last_check_started_at = now
                should_run = True

        try:
            if should_run:
                self._check_space_and_report()
        except Exception as e:
            logger.error(f"硬盘空间自动清理检查失败：{e}", exc_info=True)
        finally:
            with self._lock:
                self._check_running = False
            if schedule_next and self._enabled:
                self._schedule_next(initial=False)

    def _check_space_and_report(self):
        if not self._enabled:
            logger.info("硬盘空间自动清理插件未启用，跳过检查")
            return
        
        monitor_paths = self._get_effective_monitor_paths()
        if not monitor_paths:
            logger.warning("硬盘空间自动清理未配置监控路径")
            return
        
        # 初始化模块
        scanner = DiskSpaceScanner(self)
        deleter = DiskSpaceDeleter(self)
        notifier = DiskSpaceNotifier(self)
        
        for monitor in monitor_paths:
            mpath = Path(monitor)
            if not mpath.exists():
                logger.warning(f"监控路径不存在：{mpath}")
                continue

            strategy = self._resolve_strategy_for_monitor(mpath)
            previous = self._apply_strategy_context(strategy)
            try:
                usage = shutil.disk_usage(mpath)
                free_gb = usage.free / 1024 ** 3
                total_gb = usage.total / 1024 ** 3
                free_percent = usage.free / usage.total * 100 if usage.total else 0
                logger.info(
                    f"硬盘空间检查：{mpath} [{self._current_strategy_name}] 剩余 {free_gb:.1f}GB / {total_gb:.1f}GB ({free_percent:.1f}%)，"
                    f"触发阈值 {self._min_free_gb}GB，目标剩余 {self._target_free_gb}GB"
                )

                scan_paths = strategy.get("media_paths") or scanner._media_paths_for_monitor(mpath)
                if free_gb >= self._min_free_gb:
                    self._mark_space_recovered(mpath)
                    self._save_record(
                        mpath,
                        free_gb,
                        total_gb,
                        free_percent,
                        [],
                        f"空间充足：当前剩余 {free_gb:.1f}GB >= 触发阈值 {self._min_free_gb}GB，未生成清理建议",
                        scan_paths,
                        strategy_name=self._current_strategy_name
                    )
                    continue

                if self._should_skip_scan_for_cooldown(mpath):
                    cooldown_text = self._cooldown_status_text(mpath)
                    self._save_record(
                        mpath,
                        free_gb,
                        total_gb,
                        free_percent,
                        [],
                        f"空间不足，但处于扫描冷却期：{cooldown_text}",
                        scan_paths,
                        diagnosis={"scan_time_seconds": 0, "cooldown_active": True, "cooldown_text": cooldown_text},
                        strategy_name=self._current_strategy_name
                    )
                    continue

                logger.info(
                    f"空间不足，开始扫描候选：监控路径={mpath}，策略={self._current_strategy_name}，扫描路径={', '.join(scan_paths) or '未配置'}，"
                    f"深度={self._candidate_depth}，最大条目={self._max_scan_items}，线程={self._scan_workers}"
                )
                needed_gb = max(0, self._target_free_gb - free_gb)
                candidates, diagnosis = scanner.build_candidates(
                    monitor_path=mpath,
                    scan_paths=scan_paths,
                    size_cache=self._size_cache,
                    size_cache_lock=self._size_cache_lock,
                    target_release_gb=needed_gb,
                )
                selected = self._select_candidates(candidates, needed_gb)
                logger.info(
                    f"空间不足：当前剩余 {free_gb:.1f}GB < 触发阈值 {self._min_free_gb}GB，"
                    f"目标剩余 {self._target_free_gb}GB，需要释放约 {needed_gb:.1f}GB；"
                    f"扫描候选 {len(candidates)} 项，选中 {len(selected)} 项"
                )
                deleted, delete_errors = ([], [])

                if selected and not self._dry_run:
                    deleted, delete_errors = deleter.delete_selected(selected, scan_paths=scan_paths)
                    selected_for_record = deleted
                    summary = "空间不足，已执行自动清理" if deleted else "空间不足，但自动清理未成功；请查看错误日志"
                    record_mode = "deleted" if deleted else "pending"
                else:
                    selected_for_record = selected
                    summary = "空间不足，已生成建议清理列表" if selected else "空间不足，但未找到符合条件的候选；请查看诊断信息"
                    record_mode = "pending"

                self._save_record(mpath, free_gb, total_gb, free_percent, selected_for_record, summary,
                                 scan_paths, diagnosis=diagnosis, all_candidates=candidates,
                                 strategy_name=self._current_strategy_name,
                                 record_mode=record_mode,
                                 deleted_candidates=deleted)
                self._mark_low_space_scan(mpath)

                if not self._dry_run and deleted:
                    notifier.notify_report(mpath, free_gb, total_gb, free_percent, deleted, needed_gb,
                                          scan_paths=scan_paths, diagnosis=diagnosis,
                                          delete_errors=delete_errors,
                                          strategy_name=self._current_strategy_name)
            finally:
                self._restore_strategy_context(previous)

    def _select_candidates(self, candidates: List[Dict[str, Any]], needed_gb: float) -> List[Dict[str, Any]]:
        selected = []
        total = 0.0
        max_delete_gb = float(self._max_delete_gb if self._max_delete_gb is not None else 1000)
        skipped_oversize = 0
        skipped_total_limit = 0
        
        for item in candidates:
            # 检查候选数量限制
            if len(selected) >= self._max_candidates:
                break
            
            # 检查已达到目标空间
            if needed_gb > 0 and total >= needed_gb:
                break
            
            # 单次删除上限按"完整媒体项"判断：完整电视剧/电影超过上限就跳过，不能拆分删除
            item_size_gb = float(item.get("size_gb") or 0)
            item_name = item.get("name") or item.get("path") or "未知媒体"
            if max_delete_gb > 0 and item_size_gb > max_delete_gb:
                skipped_oversize += 1
                logger.info(f"候选项超过单次删除上限，跳过完整媒体：{item_name} {item_size_gb:.1f}GB > {max_delete_gb:.1f}GB")
                continue
            
            # 加入这个完整媒体后超过总上限，也跳过并继续找后面的更小候选
            if max_delete_gb > 0 and total + item_size_gb > max_delete_gb:
                skipped_total_limit += 1
                logger.info(f"加入候选会超过单次删除总上限，跳过完整媒体：{item_name}，当前{total:.1f}GB + {item_size_gb:.1f}GB > {max_delete_gb:.1f}GB")
                continue
            
            selected.append(item)
            total += item_size_gb
        
        if max_delete_gb > 0 and not selected and skipped_oversize:
            logger.warning(f"找到候选但均超过单次删除上限 {max_delete_gb:.1f}GB；请调大“每次删除最大空间GB”或降低保护条件")
        elif skipped_oversize or skipped_total_limit:
            logger.info(f"单次删除上限筛选完成：已选{len(selected)}项 {total:.1f}GB，跳过超单项上限{skipped_oversize}项，跳过总量超限{skipped_total_limit}项")
        
        return selected

    def _persist_config(self):
        """
        仅持久化插件运行时状态，避免定时检查或旧实例用内存旧配置
        覆盖用户刚在页面保存的配置。用户配置项由 MoviePilot 保存流程负责。
        """
        try:
            config = self.get_config() or {}
            if not isinstance(config, dict):
                config = {}
            config.update({
                "run_once": self._run_once,
                "history": self._history,
                "scan_state": self._scan_state,
            })
            self.update_config(config)
        except Exception as e:
            logger.warning(f"保存硬盘空间自动清理运行状态失败：{e}")

    def _state_key(self, monitor_path: Path) -> str:
        strategy_name = self._current_strategy_name or monitor_path.as_posix()
        return f"{monitor_path.as_posix()}::{strategy_name}"

    def _get_scan_state_entry(self, monitor_path: Path) -> Dict[str, Any]:
        key = self._state_key(monitor_path)
        entry = self._scan_state.get(key)
        if not isinstance(entry, dict):
            entry = {}
            self._scan_state[key] = entry
        return entry

    def _compute_scan_cooldown_minutes(self, entry: Dict[str, Any]) -> int:
        base = max(0, int(self._scan_cooldown_minutes or 0))
        multiplier = max(1, int(self._scan_backoff_multiplier or 1))
        max_minutes = max(base, int(self._scan_backoff_max_minutes or base or 0))
        streak = max(1, int(entry.get("low_space_scan_streak") or 1))
        cooldown = base * (multiplier ** max(0, streak - 1)) if base > 0 else 0
        if max_minutes > 0:
            cooldown = min(cooldown, max_minutes)
        return int(cooldown)

    def _should_skip_scan_for_cooldown(self, monitor_path: Path) -> bool:
        entry = self._get_scan_state_entry(monitor_path)
        next_allowed = float(entry.get("next_allowed_scan_at") or 0)
        return next_allowed > time.time()

    def _cooldown_status_text(self, monitor_path: Path) -> str:
        entry = self._get_scan_state_entry(monitor_path)
        next_allowed = float(entry.get("next_allowed_scan_at") or 0)
        remaining = max(0, int(next_allowed - time.time()))
        cooldown = int(entry.get("last_cooldown_minutes") or 0)
        streak = int(entry.get("low_space_scan_streak") or 0)
        if remaining <= 0:
            return "冷却已结束"
        minutes, seconds = divmod(remaining, 60)
        return f"剩余 {minutes}分{seconds}秒（连续低空间扫描 {streak} 次，本轮冷却 {cooldown} 分钟）"

    def _mark_space_recovered(self, monitor_path: Path):
        entry = self._get_scan_state_entry(monitor_path)
        entry.update({
            "low_space_scan_streak": 0,
            "next_allowed_scan_at": 0,
            "last_cooldown_minutes": 0,
            "last_recovered_at": time.time(),
        })

    def _mark_low_space_scan(self, monitor_path: Path):
        entry = self._get_scan_state_entry(monitor_path)
        streak = max(0, int(entry.get("low_space_scan_streak") or 0)) + 1
        entry["low_space_scan_streak"] = streak
        entry["last_low_space_scan_at"] = time.time()
        cooldown = self._compute_scan_cooldown_minutes(entry)
        entry["last_cooldown_minutes"] = cooldown
        entry["next_allowed_scan_at"] = time.time() + cooldown * 60 if cooldown > 0 else 0

    def _save_record(self, monitor_path: Path, free_gb: float, total_gb: float, free_percent: float,
                     selected: List[Dict[str, Any]], summary: str, scan_paths: Optional[List[str]] = None,
                     diagnosis: Optional[Dict[str, Any]] = None,
                     all_candidates: Optional[List[Dict[str, Any]]] = None,
                     strategy_name: Optional[str] = None,
                     record_mode: Optional[str] = None,
                     deleted_candidates: Optional[List[Dict[str, Any]]] = None):
        reclaim_gb = sum(float(x.get("size_gb") or 0) for x in selected)
        scored_candidates = sorted(all_candidates or selected or [], key=lambda x: float(x.get("score") or 0), reverse=True)
        deleted_serialized = [self._serialize_candidate(x) for x in (deleted_candidates or [])[:50]]
        record = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "monitor_path": monitor_path.as_posix(),
            "strategy_name": strategy_name or self._current_strategy_name or monitor_path.as_posix(),
            "free_gb": free_gb,
            "total_gb": total_gb,
            "free_percent": free_percent,
            "free_text": f"{free_gb:.1f}GB / {total_gb:.1f}GB ({free_percent:.1f}%)",
            "candidate_count": len(selected),
            "reclaim_gb": reclaim_gb,
            "reclaim_text": f"{reclaim_gb:.1f}GB",
            "summary": summary,
            "scan_paths": scan_paths or [],
            "scan_paths_text": ", ".join(scan_paths or []),
            "diagnosis": diagnosis or {},
            "diagnosis_text": DiskSpaceNotifier(self).diagnosis_text(diagnosis),
            "all_candidate_count": len(scored_candidates),
            "record_mode": record_mode or "pending",
            "all_candidates": [self._serialize_candidate(x) for x in scored_candidates[:100]],
            "deleted_candidates": deleted_serialized,
            "candidates": [
                self._serialize_candidate(x) for x in selected[:50]
            ],
        }
        
        # 保存到历史记录
        self._history.insert(0, record)
        if len(self._history) > self._history_limit:
            self._history.pop()
        logger.info(
            f"硬盘空间检查记录已保存：{monitor_path}，摘要={summary}，候选={len(selected)}项，"
            f"预计释放={reclaim_gb:.1f}GB，扫描耗时={record['diagnosis'].get('scan_time_seconds', 0)}秒"
        )
        
        # 持久化配置
        self._persist_config()

    @staticmethod
    def _serialize_candidate(item: Dict[str, Any]) -> Dict[str, Any]:
        """压缩保存候选评分数据，供页面展示最新候选榜。"""
        return {
            "path": item.get("path"),
            "name": item.get("name"),
            "size_gb": round(float(item.get("size_gb") or 0), 2),
            "age_days": item.get("age_days"),
            "score": round(float(item.get("score") or 0), 2),
            "space_score": round(float(item.get("space_score") or 0), 2),
            "age_score": round(float(item.get("age_score") or 0), 2),
            "inactive_score": round(float(item.get("inactive_score") or 0), 2),
            "tmdb_modifier": round(float(item.get("tmdb_modifier") or 0), 2),
            "tmdb_rating": item.get("tmdb_rating"),
            "tmdb_weighted_rating": item.get("tmdb_weighted_rating"),
            "tmdb_vote_count": item.get("tmdb_vote_count"),
            "tmdb_title": item.get("tmdb_title"),
            "tmdb_id": item.get("tmdb_id"),
            "tmdb_type": item.get("tmdb_type"),
            "poster": item.get("poster"),
            "tmdb_reason": item.get("tmdb_reason"),
            "type": item.get("type"),
            "activity_reason": item.get("activity_reason"),
        }
