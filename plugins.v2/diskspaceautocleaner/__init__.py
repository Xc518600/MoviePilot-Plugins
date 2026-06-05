import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType


class DiskSpaceAutoCleaner(_PluginBase):
    plugin_name = "硬盘空间自动清理"
    plugin_desc = "监控指定硬盘/媒体库剩余空间，在空间不足时按路径映射扫描对应媒体库并生成清理建议。v1.2 默认只报告，不删除任何文件。"
    plugin_icon = "harddisk.png"
    plugin_version = "1.2"
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
    _min_free_gb = 300
    _target_free_gb = 500
    _scan_interval_minutes = 60
    _max_candidates = 30
    _max_scan_items = 5000
    _recent_days_protect = 30
    _protect_dirs = ""
    _protect_keywords = ""
    _history_limit = 50
    _history: List[Dict[str, Any]] = []
    _run_once = False

    _timer: Optional[threading.Timer] = None
    _lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = bool(config.get("enabled", False))
            self._notify = bool(config.get("notify", True))
            self._dry_run = bool(config.get("dry_run", True))
            self._monitor_paths = config.get("monitor_paths") or ""
            self._media_paths = config.get("media_paths") or ""
            self._path_mappings = config.get("path_mappings") or ""
            self._min_free_gb = int(config.get("min_free_gb") or 300)
            self._target_free_gb = int(config.get("target_free_gb") or 500)
            self._scan_interval_minutes = int(config.get("scan_interval_minutes") or 60)
            self._max_candidates = int(config.get("max_candidates") or 30)
            self._max_scan_items = int(config.get("max_scan_items") or 5000)
            self._recent_days_protect = int(config.get("recent_days_protect") or 30)
            self._protect_dirs = config.get("protect_dirs") or ""
            self._protect_keywords = config.get("protect_keywords") or ""
            self._history_limit = int(config.get("history_limit") or 50)
            history = config.get("history") or []
            self._history = history if isinstance(history, list) else []
            self._run_once = bool(config.get("run_once", False))

        self.stop_service()
        if self._run_once:
            logger.info("硬盘空间自动清理收到配置页立即运行请求")
            self._run_once = False
            self._persist_config()
            threading.Thread(target=self._run_check, daemon=True).start()

        if self._enabled:
            logger.info(
                f"硬盘空间自动清理已启用：dry_run={self._dry_run}, interval={self._scan_interval_minutes}min, "
                f"min_free={self._min_free_gb}GB, target_free={self._target_free_gb}GB"
            )
            self._schedule_next(initial=True)
        else:
            logger.info("硬盘空间自动清理未启用")

    def get_state(self) -> bool:
        return bool(self._enabled)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/run_now",
                "summary": "立即运行空间检查",
                "description": "手动触发硬盘空间检查并生成清理建议，不受定时检查间隔限制。",
                "methods": ["POST"]
            }
        ]

    def stop_service(self):
        with self._lock:
            if self._timer:
                try:
                    self._timer.cancel()
                except Exception:
                    pass
                self._timer = None

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
                                "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "dry_run", "label": "安全报告模式", "hint": "v1.2，不删除任何文件"}}]
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
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{"component": "VTextarea", "props": {"model": "monitor_paths", "label": "监控硬盘/挂载路径", "rows": 3, "placeholder": "/media\n/硬盘1", "hint": "用于检查剩余空间，每行一个路径"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{"component": "VTextarea", "props": {"model": "media_paths", "label": "默认媒体扫描路径", "rows": 4, "placeholder": "/media/电影\n/media/电视剧", "hint": "没有匹配到路径映射时，才扫描这些默认目录"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{"component": "VTextarea", "props": {"model": "path_mappings", "label": "硬盘路径到媒体库路径映射", "rows": 4, "placeholder": "/硬盘5=>/link5\n/vol5=>/link5", "hint": "当某个监控硬盘空间不足时，只扫描它对应的媒体库存放路径。格式：监控路径=>媒体库路径，每行一个。例：硬盘5 对应 link5"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VTextField", "props": {"model": "min_free_gb", "label": "触发剩余空间GB", "type": "number", "placeholder": "300"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VTextField", "props": {"model": "target_free_gb", "label": "目标剩余空间GB", "type": "number", "placeholder": "500"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VTextField", "props": {"model": "scan_interval_minutes", "label": "检查间隔分钟", "type": "number", "placeholder": "60"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VTextField", "props": {"model": "recent_days_protect", "label": "最近新增保护天数", "type": "number", "placeholder": "30"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VTextField", "props": {"model": "max_candidates", "label": "最多候选数量", "type": "number", "placeholder": "30"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VTextField", "props": {"model": "max_scan_items", "label": "最大扫描条目", "type": "number", "placeholder": "5000"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VTextField", "props": {"model": "history_limit", "label": "历史记录保留条数", "type": "number", "placeholder": "50"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{"component": "VTextarea", "props": {"model": "protect_dirs", "label": "保护目录", "rows": 3, "placeholder": "/media/电影/收藏\n/media/电视剧/保留", "hint": "路径命中这些目录时不会进入候选"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{"component": "VTextarea", "props": {"model": "protect_keywords", "label": "保护关键词", "rows": 3, "placeholder": "收藏\n周杰伦\n宫崎骏", "hint": "路径或文件名包含关键词时不会进入候选"}}]
                            },
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "dry_run": True,
            "notify": True,
            "run_once": False,
            "monitor_paths": "",
            "media_paths": "",
            "path_mappings": "",
            "min_free_gb": 300,
            "target_free_gb": 500,
            "scan_interval_minutes": 60,
            "max_candidates": 30,
            "max_scan_items": 5000,
            "recent_days_protect": 30,
            "protect_dirs": "",
            "protect_keywords": "",
            "history_limit": 50,
            "history": [],
            "sources": "immediate",
        }

    def get_page(self) -> List[dict]:
        history = list(self._history or [])[: self._history_limit]
        if not history:
            return [
                {
                    "component": "VAlert",
                    "props": {"type": "info", "variant": "tonal", "text": "暂无硬盘空间检查记录。启用插件后会按间隔检查并生成建议。"}
                },
                {
                    "component": "VCard",
                    "props": {"class": "mb-4"},
                    "content": [
                        {
                            "component": "VCardText",
                            "content": [
                                {"component": "div", "content": "点击下方按钮立即执行硬盘空间检查并生成清理建议，不受定时检查间隔限制。"}
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
                {
                    "component": "VAlert",
                    "props": {"type": "success", "variant": "tonal", "text": "执行结果将显示在下方表格中。"}
                },
            ]

        rows = []
        for idx, item in enumerate(history, start=1):
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "text": str(idx)},
                    {"component": "td", "text": item.get("time", "")},
                    {"component": "td", "text": item.get("monitor_path", "")},
                    {"component": "td", "text": item.get("free_text", "")},
                    {"component": "td", "text": str(item.get("candidate_count", 0))},
                    {"component": "td", "text": item.get("reclaim_text", "")},
                    {"component": "td", "text": item.get("summary", "")},
                ]
            })

        return [
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "text": "硬盘空间自动清理 v1.2：支持路径映射；配置页支持保存后立即运行一次；只报告，不删除任何文件。"}
            },
            {
                "component": "VCard",
                "props": {"class": "mb-4"},
                "content": [
                    {
                        "component": "VCardText",
                        "content": [
                            {"component": "div", "content": "点击下方按钮立即执行硬盘空间检查并生成清理建议，不受定时检查间隔限制。"}
                        ],
                    },
                    {
                        "component": "VCardActions",
                        "props": {"class": "justify-end"},
                        "content": [
                            {"component": "VBtn", "props": {"text": "立即运行检查", "color": "primary", "variant": "outlined", "action": "plugin_run_now", "loading": False}}
                        ]
                    }
                ]
            },
            {
                "component": "VAlert",
                "props": {"type": "success", "variant": "tonal", "text": "执行结果将显示在下方表格中。"}
            },
            {
                "component": "VTable",
                "props": {"hover": True, "density": "compact", "fixed-header": True, "style": {"max-height": "620px", "overflow-y": "auto"}},
                "content": [
                    {"component": "thead", "content": [{"component": "tr", "content": [
                        {"component": "th", "text": "#"},
                        {"component": "th", "text": "时间"},
                        {"component": "th", "text": "监控路径"},
                        {"component": "th", "text": "剩余空间"},
                        {"component": "th", "text": "候选"},
                        {"component": "th", "text": "预计释放"},
                        {"component": "th", "text": "摘要"},
                    ]}]},
                    {"component": "tbody", "content": rows},
                ]
            }
        ]

    def _schedule_next(self, initial: bool = False):
        if not self._enabled:
            return
        delay = 5 if initial else max(60, int(self._scan_interval_minutes or 60) * 60)
        timer = threading.Timer(delay, self._run_check)
        timer.daemon = True
        with self._lock:
            self._timer = timer
        timer.start()

    def _run_check(self):
        try:
            self._check_space_and_report()
        except Exception as e:
            logger.error(f"硬盘空间自动清理检查失败：{e}", exc_info=True)
        finally:
            self._schedule_next(initial=False)

    def _check_space_and_report(self):
        monitor_paths = self._lines(self._monitor_paths)
        if not monitor_paths:
            logger.warning("硬盘空间自动清理未配置监控路径")
            return
        for monitor in monitor_paths:
            mpath = Path(monitor)
            if not mpath.exists():
                logger.warning(f"监控路径不存在：{mpath}")
                continue
            usage = shutil.disk_usage(mpath)
            free_gb = usage.free / 1024 ** 3
            total_gb = usage.total / 1024 ** 3
            free_percent = usage.free / usage.total * 100 if usage.total else 0
            logger.info(f"硬盘空间检查：{mpath} 剩余 {free_gb:.1f}GB / {total_gb:.1f}GB ({free_percent:.1f}%)")
            if free_gb >= self._min_free_gb:
                self._save_record(mpath, free_gb, total_gb, free_percent, [], "空间充足，未生成清理建议")
                continue
            candidates = self._build_candidates(mpath)
            needed_gb = max(0, self._target_free_gb - free_gb)
            selected = self._select_candidates(candidates, needed_gb)
            self._save_record(mpath, free_gb, total_gb, free_percent, selected, "空间不足，已生成建议清理列表")
            self._notify_report(mpath, free_gb, total_gb, free_percent, selected, needed_gb)

    def _build_candidates(self, monitor_path: Optional[Path] = None) -> List[Dict[str, Any]]:
        media_paths = self._media_paths_for_monitor(monitor_path)
        protect_dirs = [Path(p).as_posix().rstrip("/") for p in self._lines(self._protect_dirs)]
        protect_keywords = [k.lower() for k in self._lines(self._protect_keywords)]
        candidates = []
        scanned = 0
        now = time.time()
        recent_seconds = max(0, int(self._recent_days_protect or 0)) * 86400

        for media_root in media_paths:
            root = Path(media_root)
            if not root.exists() or not root.is_dir() or not self._is_safe_root(root):
                continue
            try:
                for child in root.iterdir():
                    scanned += 1
                    if scanned > self._max_scan_items:
                        logger.warning(f"扫描达到上限：{self._max_scan_items}")
                        return sorted(candidates, key=lambda x: x.get("score", 0), reverse=True)
                    try:
                        if self._is_protected(child, protect_dirs, protect_keywords):
                            continue
                        stat = child.stat()
                        if recent_seconds and now - stat.st_mtime < recent_seconds:
                            continue
                        size = self._path_size(child)
                        if size <= 0:
                            continue
                        age_days = max(0, int((now - stat.st_mtime) / 86400))
                        size_gb = size / 1024 ** 3
                        score = age_days + size_gb * 2
                        candidates.append({
                            "path": child.as_posix(),
                            "name": child.name,
                            "size": size,
                            "size_gb": size_gb,
                            "age_days": age_days,
                            "mtime": stat.st_mtime,
                            "score": score,
                            "type": "目录" if child.is_dir() else "文件",
                        })
                    except Exception as e:
                        logger.debug(f"扫描候选失败 {child}: {e}")
            except Exception as e:
                logger.warning(f"扫描媒体目录失败 {root}: {e}")
        return sorted(candidates, key=lambda x: x.get("score", 0), reverse=True)

    def _select_candidates(self, candidates: List[Dict[str, Any]], needed_gb: float) -> List[Dict[str, Any]]:
        selected = []
        total = 0.0
        for item in candidates:
            if len(selected) >= self._max_candidates:
                break
            selected.append(item)
            total += float(item.get("size_gb") or 0)
            if needed_gb > 0 and total >= needed_gb:
                break
        return selected

    def _notify_report(self, monitor_path: Path, free_gb: float, total_gb: float, free_percent: float,
                       selected: List[Dict[str, Any]], needed_gb: float):
        if not self._notify:
            return
        reclaim_gb = sum(float(x.get("size_gb") or 0) for x in selected)
        lines = [
            "硬盘空间自动清理：空间不足提醒",
            f"监控路径：{monitor_path}",
            f"剩余空间：{free_gb:.1f}GB / {total_gb:.1f}GB ({free_percent:.1f}%)",
            f"目标还需释放：{needed_gb:.1f}GB",
            f"建议候选：{len(selected)} 个，预计可释放 {reclaim_gb:.1f}GB",
            "当前为 v1.0 安全报告模式：未删除任何文件。",
        ]
        if selected:
            lines.append("建议清理样例：")
            for item in selected[:10]:
                lines.append(f"- {item.get('name')} | {item.get('size_gb'):.1f}GB | {item.get('age_days')}天未修改 | {item.get('path')}")
        try:
            self.post_message(mtype=NotificationType.Plugin, title="硬盘空间自动清理：空间不足", text="\n".join(lines))
        except Exception as e:
            logger.warning(f"发送硬盘空间自动清理通知失败：{e}")

    def _save_record(self, monitor_path: Path, free_gb: float, total_gb: float, free_percent: float,
                     selected: List[Dict[str, Any]], summary: str):
        reclaim_gb = sum(float(x.get("size_gb") or 0) for x in selected)
        record = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "monitor_path": monitor_path.as_posix(),
            "free_gb": free_gb,
            "total_gb": total_gb,
            "free_percent": free_percent,
            "free_text": f"{free_gb:.1f}GB / {total_gb:.1f}GB ({free_percent:.1f}%)",
            "candidate_count": len(selected),
            "reclaim_gb": reclaim_gb,
            "reclaim_text": f"{reclaim_gb:.1f}GB",
            "summary": summary,
            "candidates": [
                {
                    "path": x.get("path"),
                    "name": x.get("name"),
                    "size_gb": round(float(x.get("size_gb") or 0), 2),
                    "age_days": x.get("age_days"),
                    "type": x.get("type"),
                }
                for x in selected[:50]
            ],
        }
        with self._lock:
            self._history.insert(0, record)
            self._history = self._history[: max(1, int(self._history_limit or 50))]
        self._persist_config()

    def _persist_config(self):
        try:
            config = self.get_config() or {}
            if not isinstance(config, dict):
                config = {}
            config.update({
                "enabled": self._enabled,
                "dry_run": True,
                "notify": self._notify,
                "monitor_paths": self._monitor_paths,
                "media_paths": self._media_paths,
                "path_mappings": self._path_mappings,
                "min_free_gb": self._min_free_gb,
                "target_free_gb": self._target_free_gb,
                "scan_interval_minutes": self._scan_interval_minutes,
                "max_candidates": self._max_candidates,
                "max_scan_items": self._max_scan_items,
                "recent_days_protect": self._recent_days_protect,
                "protect_dirs": self._protect_dirs,
                "protect_keywords": self._protect_keywords,
                "history_limit": self._history_limit,
                "history": self._history,
            })
            self.update_config(config)
        except Exception as e:
            logger.warning(f"保存硬盘空间自动清理配置失败：{e}")

    @staticmethod
    def _lines(text: str) -> List[str]:
        return [x.strip() for x in str(text or "").splitlines() if x.strip()]


    def handle_run_now(self) -> Dict[str, Any]:
        """
        插件页面“立即运行检查”按钮触发的 API。
        手动执行一次空间检查并生成清理建议。
        """
        try:
            self._check_space_and_report()
            return {
                "success": True,
                "message": "空间检查已完成，结果请查看下方表格",
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            }
        except Exception as e:
            logger.error(f"硬盘空间自动清理立即运行失败：{e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            }

    @staticmethod
    def _format_size(size: int) -> str:
        gb = size / 1024 ** 3
        return f"{gb:.1f}GB"

    def _is_safe_root(self, path: Path) -> bool:
        try:
            p = path.resolve(strict=False)
        except Exception:
            p = path.absolute()
        danger = {Path("/"), Path("/app"), Path("/config"), Path("/tmp"), Path("/var"), Path("/usr"), Path("/bin"), Path("/sbin"), Path("/lib"), Path("/etc")}
        if p in danger or len(p.parts) < 3:
            return False
        return True

    def _is_protected(self, path: Path, protect_dirs: List[str], protect_keywords: List[str]) -> bool:
        p = path.as_posix()
        plow = p.lower()
        for root in protect_dirs:
            if root and (p == root or p.startswith(root.rstrip("/") + "/")):
                return True
        for keyword in protect_keywords:
            if keyword and keyword in plow:
                return True
        return False

    def _path_size(self, path: Path) -> int:
        if path.is_file():
            try:
                return path.stat().st_size
            except Exception:
                return 0
        total = 0
        count = 0
        for current, dirnames, filenames in os.walk(path):
            for name in filenames:
                try:
                    fp = Path(current) / name
                    total += fp.stat().st_size
                    count += 1
                    if count > self._max_scan_items:
                        return total
                except Exception:
                    pass
        return total
    def _media_paths_for_monitor(self, monitor_path: Path) -> List[str]:
        """
        根据路径映射，推断当前监控硬盘对应的媒体扫描路径。
        返回：要扫描的媒体路径列表
        """
        # 1. 尝试通过 path_mappings 精确匹配监控路径
        for line in self._lines(self._path_mappings):
            if '=>' not in line:
                continue
            src, dst = [x.strip() for x in line.split('=>', 1)]
            if not src or not dst:
                continue
            try:
                src_path = Path(src)
                dst_path = Path(dst)
                if monitor_path.resolve() == src_path.resolve():
                    return [dst_path.as_posix()]
                elif monitor_path.resolve().is_relative_to(src_path.resolve()):
                    return [dst_path.as_posix()]
            except Exception:
                continue
        # 2. 没有匹配到映射，使用默认媒体路径
        return self._lines(self._media_paths)


