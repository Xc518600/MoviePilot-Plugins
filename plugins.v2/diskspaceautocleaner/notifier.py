import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.log import logger
from app.schemas import NotificationType

from .utils import DiskSpaceUtils


class DiskSpaceNotifier:
    """通知器，负责发送清理报告。"""
    
    def __init__(self, plugin_instance):
        self._plugin = plugin_instance
    
    def notify_report(self, monitor_path: Path, free_gb: float, total_gb: float, free_percent: float,
                       selected: List[Dict[str, Any]], needed_gb: float, scan_paths: Optional[List[str]] = None,
                       diagnosis: Optional[Dict[str, Any]] = None, delete_errors: Optional[List[str]] = None,
                       strategy_name: Optional[str] = None):
        """发送清理报告（按策略区分）。"""
        if not self._plugin._notify:
            return
        diagnosis = diagnosis or {}
        title, text = self.push_message(strategy_name or str(monitor_path), diagnosis, selected)
        entry = self._plugin._get_scan_state_entry(monitor_path)
        # Dedupe is deliberately notification-only; scanning/deletion already ran.
        fingerprint = (bool(diagnosis.get("dry_run")), bool(diagnosis.get("error")),
                       diagnosis.get("stop_reason"), bool(delete_errors))
        if entry.get("notification_fingerprint") == list(fingerprint) and time.time() - float(entry.get("notification_at") or 0) < 3600:
            return
        try:
            self._plugin.post_message(mtype=NotificationType.Plugin, title=title, text=text)
            entry.update(notification_fingerprint=list(fingerprint), notification_at=time.time())
            self._plugin._persist_config()
        except Exception as e:
            logger.warning(f"发送硬盘空间自动清理通知失败:{e}")

    @staticmethod
    def push_message(strategy_name, diagnosis, selected):
        """Return a short human-facing push; keep verbose diagnosis in history/logs."""
        diagnosis = diagnosis or {}
        name = " ".join(str(strategy_name or "未命名策略").split())[:60]
        if name.startswith(("/", "\\")) or "://" in name:
            name = "未命名策略"

        def number(value):
            try:
                value = float(value)
                return value if math.isfinite(value) else None
            except (TypeError, ValueError, OverflowError):
                return None

        after = number(diagnosis.get("after_free_gb"))
        fallback = bool(diagnosis.get("fallback_used") or diagnosis.get("fallback_safety_reached"))
        if diagnosis.get("dry_run"):
            target = diagnosis.get("fallback_target_free_gb") if diagnosis.get("fallback_planned") else diagnosis.get("target_free_gb")
            status = "模拟检查异常（未删除）" if diagnosis.get("error") else "模拟建议（未删除）"
            icon = "💡"
        else:
            target = diagnosis.get("fallback_target_free_gb") if fallback else diagnosis.get("target_free_gb")
            target = number(target)
            if diagnosis.get("error"):
                status, icon = "清理异常", "⚠️"
            elif diagnosis.get("delete_errors"):
                status, icon = ("清理部分完成" if selected else "清理受阻"), "⚠️"
            elif target is not None and after is not None and after >= target:
                status, icon = ("已达安全水位" if fallback else "空间已达目标"), "✅"
            elif not selected and "无候选" in str(diagnosis.get("stop_reason", "")):
                status, icon = "无可清理候选", "⚠️"
            elif target is None or after is None:
                status, icon = "空间状态待确认", "⚠️"
            else:
                status, icon = "空间仍不足", "⚠️"

        target = number(target)

        def gb(value):
            return "未知" if value is None else f"{value:.2f} GB"

        delta_value = number(diagnosis.get("actual_change_gb"))
        if delta_value is not None and abs(delta_value) < 0.005:
            delta_value = 0.0  # Avoid -0.00 without hiding a meaningful decrease.
        delta_text = gb(delta_value)
        count_label = "建议删除" if diagnosis.get("dry_run") else "已删除"
        lines = [f"剩余 {gb(after)} / 目标 {gb(target)}",
                 f"{count_label}{len(selected)} 项，空间变化 {delta_text}"]
        if diagnosis.get("dry_run"):
            lines.append("模拟报告：仅生成建议，未实际删除文件")
            if diagnosis.get("error"):
                lines.append("检查异常，请查看日志")
        elif diagnosis.get("error"):
            lines.append("检查/清理异常，已停止本盘任务，请查看日志")
        elif diagnosis.get("delete_errors"):
            lines.append(f"删除失败/保护复核跳过 {len(diagnosis['delete_errors'])} 项，请查看日志")
        elif icon == "✅":
            pass  # The title already states success; do not repeat it in the body.
        elif target is None or after is None:
            lines.append("空间数据不完整，请查看日志确认")
        elif selected and delta_value is not None and abs(delta_value) < 0.01:
            lines.append("删除后空间未明显增加，可检查硬链接、快照或文件占用")
        elif not selected:
            lines.append("无候选或受限制（保护规则/删除上限）")
        elif delta_value is not None and delta_value < 0:
            lines.append("实测剩余空间减少，可检查并发写入或文件占用")
        else:
            lines.append("空间仍不足，请查看清理记录")
        if diagnosis.get("fallback_used"):
            lines.append("已尝试兜底清理")
        elif diagnosis.get("fallback_planned"):
            lines.append("计划尝试兜底清理（模拟）")
        return f"{icon} {name} · {status}", "\n".join(lines)

    @staticmethod
    def outcome_text(diagnosis, selected):
        def space(value):
            return "未知" if value is None else f"{value:.2f}GB"
        before = diagnosis.get("before_free_gb")
        after = diagnosis.get("after_free_gb")
        target = diagnosis.get("fallback_target_free_gb") if (diagnosis.get("fallback_used") or diagnosis.get("fallback_safety_reached")) else diagnosis.get("target_free_gb")
        if diagnosis.get("error"):
            status = "⚠️ 检查/清理异常"
        elif diagnosis.get("dry_run"):
            status = "💡 模拟报告（未删除，不代表已释放空间）"
        elif after is not None and target is not None and after >= target:
            status = "✅ 实际空间已达目标"
        else:
            status = "⚠️ 清理后空间仍不足，需要处理"
        return (f"{status}\n清理前剩余：{space(before)}；清理后剩余：{space(after)}；目标：{space(target)}"
                f"\n实际空间变化：{space(diagnosis.get('actual_change_gb'))}；"
                f"{'建议' if diagnosis.get('dry_run') else '成功删除'}：{len(selected)}项"
                f"\n停止原因：{diagnosis.get('stop_reason', '未知')}"
                f"\n兜底清理：{('模拟计划（未执行）' if diagnosis.get('fallback_planned') else '已启用/已使用' if diagnosis.get('fallback_used') else ('已启用但未使用' if diagnosis.get('fallback_enabled') else '未启用'))}"
                f"\n删除失败/复核跳过：{len(diagnosis.get('delete_errors') or [])}项")

    def group_candidates(self, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        """将候选项按类型分组（电影、电视剧、其他）。"""
        grouped = {
            "电影": {"icon": "🎬", "name": "电影", "count": 0, "total_size_gb": 0, "items": []},
            "电视剧": {"icon": "📺", "name": "电视剧", "count": 0, "total_size_gb": 0, "items": []},
            "其他": {"icon": "📁", "name": "其他", "count": 0, "total_size_gb": 0, "items": []},
        }
        
        for item in candidates:
            path_str = item.get("path", "")
            name = item.get("name", "")
            size_gb = float(item.get("size_gb") or 0)
            
            # 判断类型：优先使用智能识别
            item_type = "其他"
            
            # 方法1：智能识别（根据目录结构）
            if path_str:
                path_obj = Path(path_str)
                if DiskSpaceUtils.is_series_folder(path_obj):
                    item_type = "电视剧"
                elif path_obj.is_dir():
                    # 检查路径关键词
                    path_lower = path_str.lower()
                    if any(k in path_lower for k in ["/电影/", "/movie/", "/movies/"]):
                        item_type = "电影"
                    elif any(k in path_lower for k in ["/电视剧/", "/电视/", "/tv/", "/series/", "/drama/"]):
                        item_type = "电视剧"
                elif path_obj.is_file():
                    # 文件按父目录判断
                    parent_lower = str(path_obj.parent).lower()
                    if any(k in parent_lower for k in ["/电影/", "/movie/", "/movies/"]):
                        item_type = "电影"
                    elif any(k in parent_lower for k in ["/电视剧/", "/电视/", "/tv/", "/series/", "/drama/"]):
                        item_type = "电视剧"
            
            grouped[item_type]["count"] += 1
            grouped[item_type]["total_size_gb"] += size_gb
            grouped[item_type]["items"].append(item)
        
        # 移除空的分类
        result = {k: v for k, v in grouped.items() if v["count"] > 0}
        return result
    
    def diagnosis_text(self, diagnosis: Optional[Dict[str, Any]]) -> str:
        """格式化诊断信息。"""
        if not diagnosis:
            return ""
        parts = [
            f"扫描{diagnosis.get('items_scanned', 0)}项",
            f"保护跳过{diagnosis.get('protected_skipped', 0)}",
            f"最近保护跳过{diagnosis.get('recent_skipped', 0)}",
            f"未完结/不完整电视剧跳过{diagnosis.get('incomplete_series_skipped', 0)}",
            f"空大小跳过{diagnosis.get('zero_size_skipped', 0)}",
            f"缺失路径{diagnosis.get('roots_missing', 0)}",
            f"候选深度{diagnosis.get('candidate_depth', '')}",
        ]
        if diagnosis.get("fallback_enabled"):
            state = "模拟计划" if diagnosis.get("fallback_planned") else ("已尝试" if diagnosis.get("fallback_used") else "已启用未使用")
            parts.append(f"兜底状态：{state}；最短保留{diagnosis.get('fallback_min_keep_days', '未知')}天；安全水位{diagnosis.get('fallback_target_free_gb', '未知')}GB")
        # 添加扫描时间统计
        scan_time = diagnosis.get('scan_time_seconds', 0)
        if scan_time:
            parts.append(f"耗时{scan_time:.2f}秒")
        # 添加缓存统计
        cache_hits = diagnosis.get('cache_hits', 0)
        cache_misses = diagnosis.get('cache_misses', 0)
        if cache_hits + cache_misses > 0:
            cache_total = cache_hits + cache_misses
            cache_rate = cache_hits / cache_total * 100 if cache_total > 0 else 0
            parts.append(f"缓存命中率{cache_rate:.1f}%")
        if diagnosis.get('tmdb_rating_used') or diagnosis.get('tmdb_rating_ignored'):
            parts.append(
                f"TMDB评分参与{diagnosis.get('tmdb_rating_used', 0)}项/忽略{diagnosis.get('tmdb_rating_ignored', 0)}项"
            )
        
        if diagnosis.get('limit_reached'):
            parts.append("已达扫描上限")
        rejected_roots = diagnosis.get('roots_rejected', diagnosis.get('roots_unsafe', 0))
        if rejected_roots:
            parts.append(f"路径规则跳过{rejected_roots}")
        if diagnosis.get('error_skipped'):
            parts.append(f"错误跳过{diagnosis.get('error_skipped')}")
        return "；".join(str(x) for x in parts if x)
