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
                       diagnosis: Optional[Dict[str, Any]] = None, delete_errors: Optional[List[str]] = None):
        """发送清理报告（精简版）。"""
        if not self._plugin._notify:
            return
        reclaim_gb = sum(float(x.get("size_gb") or 0) for x in selected)

        if not selected:
            # 没有候选删除项，发送空间不足但无候选的通知（精简版）
            lines = [
                "📊 硬盘空间自动清理",
                f"剩余空间: {free_gb:.1f}GB / {total_gb:.1f}GB ({free_percent:.1f}%)",
                f"目标还需释放: {needed_gb:.1f}GB",
                "",
                "⚠️ 未找到符合条件的删除候选",
            ]
        else:
            # 有候选删除项，发送精简的删除/建议通知
            lines = ["📊 硬盘空间自动清理"]
            
            lines.append(f"剩余空间: {free_gb:.1f}GB / {total_gb:.1f}GB ({free_percent:.1f}%)")
            lines.append(f"目标还需释放: {needed_gb:.1f}GB")
            lines.append("")
            
            # 按类型分组候选项（只显示统计，不列明细）
            grouped = self.group_candidates(selected)
            
            for category_type, category_info in grouped.items():
                icon = category_info.get("icon", "📁")
                type_name = category_info.get("name", category_type)
                count = category_info.get("count", 0)
                total_size_gb = category_info.get("total_size_gb", 0)
                
                if count > 0:
                    lines.append(f"{icon} {type_name}: {count}部 共{total_size_gb:.1f}GB")
            
            lines.append("")
            lines.append(f"💰 总计释放: {reclaim_gb:.1f}GB")
            
            if delete_errors:
                lines.append(f"⚠️ 删除失败: {len(delete_errors)}项")
            
            lines.append("💡 本次仅生成报告" if self._plugin._dry_run else "✅ 已执行清理")

        try:
            self._plugin.post_message(mtype=NotificationType.Plugin, title="硬盘空间自动清理", text="\n".join(lines))
        except Exception as e:
            logger.warning(f"发送硬盘空间自动清理通知失败:{e}")
    
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
        
        if diagnosis.get('limit_reached'):
            parts.append("已达扫描上限")
        rejected_roots = diagnosis.get('roots_rejected', diagnosis.get('roots_unsafe', 0))
        if rejected_roots:
            parts.append(f"路径规则跳过{rejected_roots}")
        if diagnosis.get('error_skipped'):
            parts.append(f"错误跳过{diagnosis.get('error_skipped')}")
        return "；".join(str(x) for x in parts if x)
