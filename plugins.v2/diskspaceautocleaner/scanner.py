import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.chain.media import MediaChain
from app.log import logger

from .utils import DiskSpaceUtils


class DiskSpaceScanner:
    """媒体扫描器，负责扫描候选和生成建议。"""
    
    def __init__(self, plugin_instance):
        self._plugin = plugin_instance
        self._lock = threading.Lock()
        self._media_chain = MediaChain()
    
    def build_candidates(self,
                        size_cache: Dict[str, int],
                        size_cache_lock: threading.Lock,
                        monitor_path: Optional[Path] = None,
                        scan_paths: Optional[List[str]] = None,
                        target_release_gb: float = 0) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """构建清理候选列表（使用多线程并行扫描）。"""
        media_paths = scan_paths if scan_paths is not None else self._media_paths_for_monitor(monitor_path)
        protect_dirs = [Path(p).as_posix().rstrip("/") for p in DiskSpaceUtils.lines(self._plugin._protect_dirs)]
        protect_keywords = [k.lower() for k in DiskSpaceUtils.lines(self._plugin._protect_keywords)]
        candidates: List[Dict[str, Any]] = []
        
        diagnosis = {
            "scan_paths": media_paths,
            "roots_total": len(media_paths),
            "roots_missing": 0,
            "roots_rejected": 0,
            "items_scanned": 0,
            "protected_skipped": 0,
            "recent_skipped": 0,
            "incomplete_series_skipped": 0,
            "zero_size_skipped": 0,
            "error_skipped": 0,
            "candidate_depth": max(1, int(self._plugin._candidate_depth or 2)),
            "limit_reached": False,
            "scan_time_seconds": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "tmdb_rating_used": 0,
            "tmdb_rating_ignored": 0,
        }
        now = time.time()
        recent_seconds = max(0, int(self._plugin._recent_days_protect or 0)) * 86400
        max_items = max(1, int(self._plugin._max_scan_items or 5000))
        depth = max(1, int(self._plugin._candidate_depth or 2))
        
        # 使用多线程并行扫描多个媒体根目录
        scan_start_time = time.time()
        logger.info(
            f"候选扫描开始：路径={', '.join(media_paths) or '未配置'}，深度={depth}，"
            f"最大条目={max_items}，线程={self._plugin._scan_workers}"
        )
        
        with ThreadPoolExecutor(max_workers=self._plugin._scan_workers) as executor:
            # 提交所有扫描任务
            future_to_root = {
                executor.submit(self._scan_media_root, root, depth, now, recent_seconds,
                               max_items, protect_dirs, protect_keywords,
                               size_cache, size_cache_lock, target_release_gb): root
                for root in media_paths
            }
            
            # 收集结果
            for future in as_completed(future_to_root):
                root = future_to_root[future]
                try:
                    root_candidates, root_diagnosis = future.result()
                    candidates.extend(root_candidates)
                    # 合并诊断信息（线程安全）
                    with self._lock:
                        diagnosis["items_scanned"] += root_diagnosis.get("items_scanned", 0)
                        diagnosis["roots_missing"] += root_diagnosis.get("roots_missing", 0)
                        diagnosis["roots_rejected"] += root_diagnosis.get("roots_rejected", 0)
                        diagnosis["protected_skipped"] += root_diagnosis.get("protected_skipped", 0)
                        diagnosis["recent_skipped"] += root_diagnosis.get("recent_skipped", 0)
                        diagnosis["incomplete_series_skipped"] += root_diagnosis.get("incomplete_series_skipped", 0)
                        diagnosis["zero_size_skipped"] += root_diagnosis.get("zero_size_skipped", 0)
                        diagnosis["error_skipped"] += root_diagnosis.get("error_skipped", 0)
                        diagnosis["cache_hits"] += root_diagnosis.get("cache_hits", 0)
                        diagnosis["cache_misses"] += root_diagnosis.get("cache_misses", 0)
                        diagnosis["tmdb_rating_used"] += root_diagnosis.get("tmdb_rating_used", 0)
                        diagnosis["tmdb_rating_ignored"] += root_diagnosis.get("tmdb_rating_ignored", 0)
                except Exception as e:
                    with self._lock:
                        diagnosis["error_skipped"] += 1
                    logger.error(f"扫描媒体根目录失败 {root}: {e}", exc_info=True)
        
        scan_time = time.time() - scan_start_time
        diagnosis["scan_time_seconds"] = round(scan_time, 2)
        
        # 检查扫描上限
        if diagnosis["items_scanned"] >= max_items:
            diagnosis["limit_reached"] = True
            logger.warning(f"扫描达到上限：{max_items} 项，耗时 {scan_time:.2f} 秒")
        logger.info(
            f"候选扫描完成：候选={len(candidates)}项，扫描={diagnosis['items_scanned']}项，"
            f"缺失={diagnosis['roots_missing']}，保护跳过={diagnosis['protected_skipped']}，"
            f"最近跳过={diagnosis['recent_skipped']}，电视剧未完结/不完整跳过={diagnosis['incomplete_series_skipped']}，"
            f"错误={diagnosis['error_skipped']}，耗时={scan_time:.2f}秒"
        )
        
        return sorted(candidates, key=lambda x: x.get("score", 0), reverse=True), diagnosis
    
    def _scan_media_root(self, root: Path, depth: int, now: float, recent_seconds: int,
                         max_items: int, protect_dirs: List[str], protect_keywords: List[str],
                         size_cache: Dict[str, int], size_cache_lock: threading.Lock,
                         target_release_gb: float = 0) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """扫描单个媒体根目录（线程安全）。"""
        root = Path(root)
        candidates: List[Dict[str, Any]] = []
        diagnosis = {
            "items_scanned": 0,
            "roots_missing": 0,
            "roots_rejected": 0,
            "protected_skipped": 0,
            "recent_skipped": 0,
            "incomplete_series_skipped": 0,
            "zero_size_skipped": 0,
            "error_skipped": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "tmdb_rating_used": 0,
            "tmdb_rating_ignored": 0,
        }
        
        if not root.exists() or not root.is_dir():
            diagnosis["roots_missing"] = 1
            logger.warning(f"媒体扫描路径不存在或不是目录：{root}")
            return candidates, diagnosis
        
        if not DiskSpaceUtils.is_safe_root(root, protect_dirs, protect_keywords):
            diagnosis["roots_rejected"] = 1
            logger.warning(f"媒体扫描路径被路径规则跳过：{root}")
            return candidates, diagnosis
        
        try:
            for child in self._iter_candidate_items(root, depth):
                diagnosis["items_scanned"] += 1
                
                if diagnosis["items_scanned"] > max_items:
                    logger.warning(f"扫描达到上限：{max_items}")
                    break
                
                try:
                    if not DiskSpaceUtils.is_safe_root(child, protect_dirs, protect_keywords):
                        diagnosis["protected_skipped"] += 1
                        continue
                    
                    stat = child.stat()
                    if recent_seconds and now - stat.st_mtime < recent_seconds:
                        diagnosis["recent_skipped"] += 1
                        continue

                    if DiskSpaceUtils.is_series_candidate(child):
                        series_ok, series_reason = DiskSpaceUtils.is_completed_complete_series(
                            child, max_scan_items=self._plugin._max_scan_items, media_chain=self._media_chain
                        )
                        if not series_ok:
                            diagnosis["incomplete_series_skipped"] += 1
                            logger.info(f"跳过电视剧候选：{child.name}，原因={series_reason}")
                            continue
                    
                    # 使用缓存获取大小（兼容TTL缓存）
                    cache_key = f"{child.as_posix()}:{stat.st_mtime}"
                    needs_calc = False
                    size = 0
                    with size_cache_lock:
                        if cache_key in size_cache:
                            # 检查是否为TTL缓存（元组格式）
                            cached_value = size_cache[cache_key]
                            if isinstance(cached_value, tuple):
                                size, cache_time = cached_value
                                # 检查缓存是否过期
                                cache_ttl = 600  # 10分钟
                                if time.time() - cache_time < cache_ttl:
                                    diagnosis["cache_hits"] += 1
                                else:
                                    needs_calc = True
                                    diagnosis["cache_misses"] += 1
                            else:
                                # 旧格式缓存，直接返回
                                size = cached_value
                                diagnosis["cache_hits"] += 1
                        else:
                            needs_calc = True
                            diagnosis["cache_misses"] += 1
                    if needs_calc:
                        size = DiskSpaceUtils.calc_path_size_fast(child, self._plugin._max_scan_items)
                        with size_cache_lock:
                            size_cache[cache_key] = (size, time.time())
                    
                    if size <= 0:
                        diagnosis["zero_size_skipped"] += 1
                        continue
                    
                    age_days = max(0, int((now - stat.st_mtime) / 86400))
                    size_gb = size / 1024 ** 3
                    tmdb_rating = self._get_tmdb_rating(child, stat.st_mtime)
                    tmdb_modifier = 0.0
                    tmdb_vote_average = None
                    tmdb_vote_count = None
                    tmdb_weighted_rating = None
                    tmdb_title = None
                    tmdb_id = None
                    poster = None
                    tmdb_reason = "未获取到 TMDB 评分"
                    if tmdb_rating:
                        tmdb_vote_average = tmdb_rating.get("vote_average")
                        tmdb_vote_count = tmdb_rating.get("vote_count")
                        tmdb_weighted_rating = tmdb_rating.get("weighted_rating")
                        tmdb_title = tmdb_rating.get("title")
                        tmdb_id = tmdb_rating.get("tmdb_id")
                        poster = tmdb_rating.get("poster")
                        tmdb_modifier = float(tmdb_rating.get("modifier") or 0)
                        tmdb_reason = tmdb_rating.get("reason") or "TMDB 评分已参与排序"
                        if tmdb_rating.get("used"):
                            diagnosis["tmdb_rating_used"] += 1
                        else:
                            diagnosis["tmdb_rating_ignored"] += 1
                    else:
                        diagnosis["tmdb_rating_ignored"] += 1

                    score_detail = self._score_candidate(size_gb=size_gb, age_days=age_days,
                                                         target_release_gb=target_release_gb,
                                                         tmdb_modifier=tmdb_modifier)
                    score = score_detail["score"]
                    
                    candidates.append({
                        "path": child.as_posix(),
                        "name": child.name,
                        "size": size,
                        "size_gb": size_gb,
                        "age_days": age_days,
                        "mtime": stat.st_mtime,
                        "score": score,
                        "space_score": score_detail["space_score"],
                        "age_score": score_detail["age_score"],
                        "inactive_score": score_detail["inactive_score"],
                        "tmdb_modifier": tmdb_modifier,
                        "tmdb_rating": tmdb_vote_average,
                        "tmdb_weighted_rating": tmdb_weighted_rating,
                        "tmdb_vote_count": tmdb_vote_count,
                        "tmdb_title": tmdb_title,
                        "tmdb_id": tmdb_id,
                        "poster": poster,
                        "tmdb_reason": tmdb_reason,
                        "type": "目录" if child.is_dir() else "文件",
                    })
                    logger.info(
                        f"候选入列：{child.name}，体积={size_gb:.2f}GB，天数={age_days}，"
                        f"空间分={score_detail['space_score']:.2f}，时间分={score_detail['age_score']:.2f}，"
                        f"低活跃分={score_detail['inactive_score']:.2f}，TMDB修正={tmdb_modifier:.2f}，"
                        f"总分={score:.2f}，TMDB={tmdb_reason}"
                    )
                except Exception as e:
                    diagnosis["error_skipped"] += 1
                    logger.warning(f"扫描候选失败 {child}: {e}")
        except Exception as e:
            diagnosis["error_skipped"] += 1
            logger.error(f"扫描媒体目录失败 {root}: {e}", exc_info=True)
        
        return candidates, diagnosis

    @staticmethod
    def _age_bucket_score(days: int, max_score: float) -> float:
        """按最终方案将陈旧天数映射为分数。"""
        if days >= 180:
            return max_score
        if days >= 90:
            return round(max_score * 22 / 30, 2)
        if days >= 30:
            return round(max_score * 15 / 30, 2)
        if days >= 7:
            return round(max_score * 8 / 30, 2)
        return 0.0

    def _score_candidate(self, size_gb: float, age_days: int,
                         target_release_gb: float, tmdb_modifier: float = 0) -> Dict[str, float]:
        """
        计算候选删除优先级：空间收益分 + 时间陈旧分 + 低活跃分 + TMDB评分修正分。

        当前插件没有可靠播放/访问记录来源，低活跃分默认不参与，避免把文件 mtime/atime
        误当成真实播放活跃度。后续若接入媒体服务器播放记录，可在这里补充 inactive_score。
        """
        target = float(target_release_gb or 0)
        if target <= 0:
            target = max(float(size_gb or 0), 1.0)
        space_score = min(40.0, max(0.0, float(size_gb or 0)) / target * 40.0)
        age_score = self._age_bucket_score(int(age_days or 0), 30.0)
        inactive_score = 0.0
        score = space_score + age_score + inactive_score + float(tmdb_modifier or 0)
        return {
            "space_score": round(space_score, 2),
            "age_score": round(age_score, 2),
            "inactive_score": round(inactive_score, 2),
            "score": round(score, 2),
        }

    def _get_tmdb_rating(self, path: Path, mtime: float) -> Optional[Dict[str, Any]]:
        """获取 TMDB 评分，按路径和 mtime 缓存 30 天。"""
        cache = getattr(self._plugin, "_tmdb_rating_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self._plugin, "_tmdb_rating_cache", cache)

        key = f"{path.as_posix()}:{int(mtime or 0)}"
        now = time.time()
        cached = cache.get(key)
        if isinstance(cached, dict) and now - float(cached.get("cache_time") or 0) < 30 * 86400:
            value = cached.get("value")
            return value if isinstance(value, dict) else None

        value = DiskSpaceUtils.get_tmdb_rating(path, self._media_chain)
        cache[key] = {"cache_time": now, "value": value}
        if len(cache) > 1000:
            for old_key, _ in sorted(cache.items(), key=lambda item: item[1].get("cache_time", 0))[:200]:
                cache.pop(old_key, None)
        return value
    
    def _iter_candidate_items(self, root: Path, depth: int):
        """智能扫描候选（使用迭代替代递归，避免递归深度限制）：
        - 电视剧根目录：只扫描第一级子目录（剧集名），避免删除单季导致缺集
        - 混放根目录：智能识别电视剧，只返回剧集根目录，不扫描季目录
        - 电影根目录：按配置深度扫描
        """
        depth = max(1, int(depth or 1))
        root_type = DiskSpaceUtils.detect_root_type(root)
        
        # 电视剧根目录：只扫描第一级子目录（剧集名）
        if root_type == "电视剧":
            try:
                with os.scandir(root) as it:
                    for entry in it:
                        if entry.is_dir():
                            yield Path(entry.path)
            except Exception as e:
                logger.debug(f"扫描电视剧根目录失败 {root}: {e}")
            return
        
        if root_type == "其他":
            # 混放路径，使用智能扫描（迭代版本）
            yield from self._walk_mixed_iterative(root, depth)
        else:
            # 电影路径，使用正常扫描（迭代版本）
            yield from self._walk_normal_iterative(root, depth)
    
    def _walk_mixed_iterative(self, root: Path, depth: int):
        """混放路径的智能扫描（迭代版本）。"""
        stack = [(root, 1)]
        while stack:
            current, level = stack.pop()
            try:
                with os.scandir(current) as it:
                    children = list(it)
            except Exception:
                continue
            for entry in reversed(children):  # 反转以保持原始顺序
                child = Path(entry.path)
                # 如果是电视剧目录，只返回根目录
                if child.is_dir() and DiskSpaceUtils.is_series_folder(child):
                    yield child
                
                # 其他目录/文件按深度扫描
                elif level >= depth or child.is_file():
                    yield child
                elif child.is_dir():
                    stack.append((child, level + 1))
    
    def _walk_normal_iterative(self, root: Path, depth: int):
        """正常扫描（迭代版本）。"""
        stack = [(root, 1)]
        while stack:
            current, level = stack.pop()
            try:
                with os.scandir(current) as it:
                    children = list(it)
            except Exception:
                continue
            for entry in reversed(children):  # 反转以保持原始顺序
                child = Path(entry.path)
                if level >= depth or child.is_file():
                    yield child
                elif child.is_dir():
                    stack.append((child, level + 1))
    
    def _media_paths_for_monitor(self, monitor_path: Path) -> List[str]:
        """根据路径映射，推断当前监控硬盘对应的媒体扫描路径（线程安全）。"""
        # 线程安全地读取配置
        with self._lock:
            path_mappings = self._plugin._path_mappings
            media_paths = self._plugin._media_paths
        
        # 1. 尝试通过 path_mappings 精确匹配监控路径
        for line in DiskSpaceUtils.lines(path_mappings):
            if '=>' not in line:
                continue
            src, dst = [x.strip() for x in line.split('=>', 1)]
            if not src or not dst:
                continue
            try:
                src_path = Path(src)
                dst_path = Path(dst)
                monitor_resolved = monitor_path.resolve(strict=False)
                src_resolved = src_path.resolve(strict=False)
                if monitor_resolved == src_resolved or DiskSpaceUtils.is_relative_to(monitor_resolved, src_resolved):
                    return [x.strip() for x in dst.split(",") if x.strip()]
            except Exception:
                continue
        # 2. 没有匹配到映射，使用默认媒体路径
        return DiskSpaceUtils.lines(media_paths)
