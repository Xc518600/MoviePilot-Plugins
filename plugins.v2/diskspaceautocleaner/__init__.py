import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.chain.media import MediaChain

from .utils import DiskSpaceUtils
from .scanner import DiskSpaceScanner
from .deleter import DiskSpaceDeleter
from .notifier import DiskSpaceNotifier


class DiskSpaceAutoCleaner(_PluginBase):
    plugin_name = "硬盘空间自动清理"
    plugin_desc = "监控指定硬盘剩余空间，空间不足时按路径映射扫描媒体库并生成清理建议。"
    plugin_icon = "harddisk.png"
    plugin_version = "3.2.22"
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
    _run_once = False
    _tmdb_rating_cache: Dict[str, Dict[str, Any]] = {}
    _poster_cache: Dict[str, Optional[str]] = {}
    _blank_poster = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGQAAACWCAQAAACCseXNAAAAkklEQVR42u3PAREAAAQEMJ9cFFUVkMBtDZbpeiEiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIpcFcbGoK4SMl3wAAAAASUVORK5CYII="

    _size_cache: Dict[str, int] = {}
    _size_cache_lock = threading.Lock()
    _scan_workers: int = 4  # 多线程扫描的线程数

    _timer: Optional[threading.Timer] = None
    _lock = threading.Lock()

    def init_plugin(self, config: dict = None):
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
            self._run_once = DiskSpaceUtils.to_bool(config.get("run_once"), False)

        self.stop_service()
        if self._run_once:
            logger.info("硬盘空间自动清理收到配置页立即运行请求")
            self._run_once = False
            self._persist_config()
            if self._enabled:
                threading.Thread(target=self._run_check, daemon=True).start()
            else:
                logger.info("硬盘空间自动清理未启用，忽略立即运行请求")

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
            threading.Thread(target=self._run_check, daemon=True).start()
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
                                "content": [{"component": "VTextField", "props": {"model": "min_free_gb", "label": "触发剩余空间GB", "type": "number", "placeholder": "5"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VTextField", "props": {"model": "target_free_gb", "label": "目标剩余空间GB", "type": "number", "placeholder": "30"}}]
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
                                "content": [{"component": "VTextField", "props": {"model": "candidate_depth", "label": "候选扫描深度", "type": "number", "placeholder": "2", "hint": "默认2层，可识别 /link5/电影/电影A；填1只扫描根目录第一层"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VTextField", "props": {"model": "max_delete_gb", "label": "每次删除最大空间GB", "type": "number", "placeholder": "1000", "hint": "单次清理最多删除多少GB；只按完整电影/完整电视剧目录删除，不拆分单集/单季。0 表示不限制"}}]
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
            "enabled": self._enabled,
            "dry_run": self._dry_run,
            "notify": self._notify,
            "run_once": False,
            "monitor_paths": self._monitor_paths,
            "media_paths": self._media_paths,
            "path_mappings": self._path_mappings,
            "min_free_gb": self._min_free_gb,
            "target_free_gb": self._target_free_gb,
            "scan_interval_minutes": self._scan_interval_minutes,
            "max_candidates": self._max_candidates,
            "max_scan_items": self._max_scan_items,
            "candidate_depth": self._candidate_depth,
            "recent_days_protect": self._recent_days_protect,
            "protect_dirs": self._protect_dirs,
            "protect_keywords": self._protect_keywords,
            "history_limit": self._history_limit,
            "max_delete_gb": self._max_delete_gb,
            "history": self._history,
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
            ]

        latest_candidates = []
        for item in history:
            candidates = item.get("all_candidates") or item.get("candidates") or []
            if candidates:
                latest_candidates = sorted(candidates, key=lambda x: float(x.get("score") or 0), reverse=True)
                break

        return [
            self._build_latest_candidates_panel(latest_candidates),
        ]

    def _build_latest_candidates_panel(self, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        """构建最新一轮候选评分榜：第一名就是当前最优先删除。"""
        if not candidates:
            return {
                "component": "VAlert",
                "props": {"type": "warning", "variant": "tonal", "text": "最新一轮还没有候选评分数据。请先运行一次空间检查。"}
            }

        cards = []
        for idx, item in enumerate(candidates[:5], start=1):
            cards.append(self._build_candidate_card(item, idx))

        return {
            "component": "VCard",
            "props": {"class": "mb-4"},
            "content": [
                {
                    "component": "VCardTitle",
                    "props": {"class": "pb-1"},
                    "text": "最新候选评分榜"
                },
                {
                    "component": "VCardText",
                    "props": {"class": "pt-0 text-caption"},
                    "text": "按候选评分从高到低排列；第 1 名是当前最优先删除。评分 = 空间收益分 + 时间陈旧分 + 低活跃分 + TMDB评分修正分。"
                },
                {
                    "component": "div",
                    "props": {"class": "grid gap-3 grid-info-card p-4"},
                    "content": cards,
                }
            ]
        }

    def _build_candidate_card(self, item: Dict[str, Any], rank: int) -> Dict[str, Any]:
        poster = self._resolve_candidate_poster(item)
        poster_src = poster or self._blank_poster
        name = item.get("tmdb_title") or item.get("name") or "未知媒体"
        title = str(name)
        if len(title) > 18:
            title = title[:18] + "..."
        score = float(item.get("score") or 0)
        tmdb_rating = item.get("tmdb_rating")
        tmdb_vote_count = item.get("tmdb_vote_count")
        tmdb_modifier = float(item.get("tmdb_modifier") or 0)
        tmdb_reason = item.get("tmdb_reason") or "TMDB评分未参与"
        tmdb_id = item.get("tmdb_id")
        tmdb_type = item.get("tmdb_type") or "movie"
        href = f"https://www.themoviedb.org/{tmdb_type}/{tmdb_id}" if tmdb_id else "#"
        rank_text = "🥇 当前最优先删除" if rank == 1 else f"#{rank}"

        details = [
            f"评分: {score:.2f}（{rank_text}）",
            f"大小: {float(item.get('size_gb') or 0):.2f}GB｜陈旧: {item.get('age_days') or 0}天",
            f"空间分: {float(item.get('space_score') or 0):.2f}｜时间分: {float(item.get('age_score') or 0):.2f}｜低活跃分: {float(item.get('inactive_score') or 0):.2f}",
            f"TMDB: {tmdb_rating if tmdb_rating is not None else '未参与'} / 人数: {tmdb_vote_count if tmdb_vote_count is not None else '-'} / 修正: {tmdb_modifier:+.2f}",
            f"说明: {tmdb_reason}",
        ]

        return {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "overflow-hidden"},
            "content": [
                {
                    "component": "div",
                    "props": {"class": "d-flex justify-space-start flex-nowrap flex-row"},
                    "content": [
                        {
                            "component": "div",
                            "content": [
                                {
                                    "component": "VImg",
                                    "props": {
                                        "src": poster_src,
                                        "height": 150,
                                        "width": 100,
                                        "aspect-ratio": "2/3",
                                        "class": "object-cover shadow ring-gray-500",
                                        "cover": True,
                                    }
                                }
                            ]
                        },
                        {
                            "component": "div",
                            "props": {"class": "min-w-0"},
                            "content": [
                                {
                                    "component": "VCardTitle",
                                    "props": {"class": "py-1 pl-2 pr-4 text-lg whitespace-nowrap"},
                                    "content": [{"component": "a", "props": {"href": href, "target": "_blank"}, "text": title}],
                                },
                                *[
                                    {"component": "VCardText", "props": {"class": "pa-0 px-2 text-caption"}, "text": text}
                                    for text in details
                                ],
                                {"component": "VCardText", "props": {"class": "pa-0 px-2 text-caption text-medium-emphasis"}, "text": item.get("path") or ""},
                            ]
                        }
                    ]
                }
            ]
        }

    def _resolve_candidate_poster(self, item: Dict[str, Any]) -> Optional[str]:
        """页面展示时兜底补海报，兼容旧历史里未保存 poster 的候选。"""
        poster = item.get("poster")
        if poster:
            return poster

        key = str(item.get("tmdb_id") or item.get("path") or item.get("name") or "")
        if key in self._poster_cache:
            return self._poster_cache.get(key)

        poster = None
        try:
            tmdb_id = item.get("tmdb_id")
            tmdb_type = item.get("tmdb_type") or "movie"
            if tmdb_id:
                media_chain = MediaChain()
                mtype = DiskSpaceUtils.tmdb_type_to_media_type(tmdb_type)
                tmdb_info = media_chain.tmdb_info(tmdbid=tmdb_id, mtype=mtype)
                poster = DiskSpaceUtils.get_media_poster(None, tmdb_info)
        except Exception as e:
            logger.debug(f"候选海报兜底查询失败：{item.get('name') or item.get('path')} - {e}")

        self._poster_cache[key] = poster
        return poster

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
        if not self._enabled:
            logger.info("硬盘空间自动清理插件未启用，跳过检查")
            return
        
        monitor_paths = DiskSpaceUtils.lines(self._monitor_paths)
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
            
            usage = shutil.disk_usage(mpath)
            free_gb = usage.free / 1024 ** 3
            total_gb = usage.total / 1024 ** 3
            free_percent = usage.free / usage.total * 100 if usage.total else 0
            logger.info(
                f"硬盘空间检查：{mpath} 剩余 {free_gb:.1f}GB / {total_gb:.1f}GB ({free_percent:.1f}%)，"
                f"触发阈值 {self._min_free_gb}GB，目标剩余 {self._target_free_gb}GB"
            )
            
            if free_gb >= self._min_free_gb:
                self._save_record(
                    mpath,
                    free_gb,
                    total_gb,
                    free_percent,
                    [],
                    f"空间充足：当前剩余 {free_gb:.1f}GB >= 触发阈值 {self._min_free_gb}GB，未生成清理建议",
                    scanner._media_paths_for_monitor(mpath)
                )
                continue
            
            scan_paths = scanner._media_paths_for_monitor(mpath)
            logger.info(
                f"空间不足，开始扫描候选：监控路径={mpath}，扫描路径={', '.join(scan_paths) or '未配置'}，"
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
            else:
                selected_for_record = selected
                summary = "空间不足，已生成建议清理列表" if selected else "空间不足，但未找到符合条件的候选；请查看诊断信息"
            
            self._save_record(mpath, free_gb, total_gb, free_percent, selected_for_record, summary,
                             scan_paths, diagnosis=diagnosis, all_candidates=candidates)
            
            # 只有真实删除成功才发送通知（v2.5+）
            if not self._dry_run and deleted:
                notifier.notify_report(mpath, free_gb, total_gb, free_percent, deleted, needed_gb,
                                      scan_paths=scan_paths, diagnosis=diagnosis,
                                      delete_errors=delete_errors)

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
            })
            self.update_config(config)
        except Exception as e:
            logger.warning(f"保存硬盘空间自动清理运行状态失败：{e}")

    def _save_record(self, monitor_path: Path, free_gb: float, total_gb: float, free_percent: float,
                     selected: List[Dict[str, Any]], summary: str, scan_paths: Optional[List[str]] = None,
                     diagnosis: Optional[Dict[str, Any]] = None,
                     all_candidates: Optional[List[Dict[str, Any]]] = None):
        reclaim_gb = sum(float(x.get("size_gb") or 0) for x in selected)
        scored_candidates = sorted(all_candidates or selected or [], key=lambda x: float(x.get("score") or 0), reverse=True)
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
            "scan_paths": scan_paths or [],
            "scan_paths_text": ", ".join(scan_paths or []),
            "diagnosis": diagnosis or {},
            "diagnosis_text": DiskSpaceNotifier(self).diagnosis_text(diagnosis),
            "all_candidate_count": len(scored_candidates),
            "all_candidates": [self._serialize_candidate(x) for x in scored_candidates[:100]],
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
        }
