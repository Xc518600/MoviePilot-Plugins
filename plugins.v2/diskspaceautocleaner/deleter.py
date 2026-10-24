import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.chain.media import MediaChain
from app.log import logger

from .utils import DiskSpaceUtils


class DiskSpaceDeleter:
    """删除器，负责安全删除文件。"""
    
    def __init__(self, plugin_instance):
        self._plugin = plugin_instance
        self._media_chain = MediaChain()
    
    def path_size(self, path: Path, size_cache: Dict[str, int], 
                  size_cache_lock: 'threading.Lock') -> int:
        """获取路径大小（带缓存）。"""
        return self.get_path_size_cached(path, size_cache, size_cache_lock)
    
    def get_path_size_cached(self, path: Path, size_cache: Dict[str, int],
                            size_cache_lock: 'threading.Lock') -> int:
        """带缓存的目录大小计算（添加TTL缓存过期机制）。"""
        try:
            stat = path.stat()
            # 使用路径和修改时间作为缓存键，确保文件变化时重新计算
            cache_key = f"{path.as_posix()}:{stat.st_mtime}"
            cache_ttl = 600  # 缓存过期时间：10分钟（600秒）
            
            with size_cache_lock:
                if cache_key in size_cache:
                    size, cache_time = size_cache[cache_key]
                    # 检查缓存是否过期
                    if time.time() - cache_time < cache_ttl:
                        return size
            
            size = DiskSpaceUtils.calc_path_size_fast(path, self._plugin._max_scan_items)
            
            with size_cache_lock:
                size_cache[cache_key] = (size, time.time())
                # 缓存超过100个时清理旧的
                if len(size_cache) > 100:
                    size_cache.clear()
            
            return size
        except Exception:
            return 0
    
    def measure_for_budget(self, item, scan_paths, monitor_path):
        """Uncached logical bytes, fail closed on errors/links/mount boundaries.

        Concurrent external writers must be excluded by the operator; no userspace
        preflight can atomically freeze an actively changing media directory.
        """
        path = Path(item["path"])
        # Fallback authorization is an in-memory per-run capability, never a
        # persisted/UI boolean.  Validate it before touching filesystem state.
        if item.get("fallback") is True:
            if (not getattr(self._plugin, "_fallback_enabled", False)
                    or getattr(self._plugin, "_fallback_run_token", None) is None
                    or item.get("_fallback_token") is not self._plugin._fallback_run_token
                    or int(item.get("fallback_min_keep_days", -1)) != int(getattr(self._plugin, "_fallback_min_keep_days", -2))):
                raise ValueError("兜底候选标记无效")
        resolved = path.resolve(strict=True)
        roots = [Path(p).resolve(strict=True) for p in scan_paths]
        if path.is_symlink() or not any(resolved != r and DiskSpaceUtils.is_relative_to(resolved, r) for r in roots):
            raise ValueError("不允许符号链接、扫描根自身或边界外路径")
        protect = [str(Path(p).resolve()) for p in DiskSpaceUtils.lines(self._plugin._protect_dirs)]
        keywords = DiskSpaceUtils.lines(self._plugin._protect_keywords)
        device = Path(monitor_path).stat().st_dev
        def measure(node):
            stat = node.lstat()
            if node.is_symlink() or stat.st_dev != device:
                raise ValueError("符号链接或跨盘/挂载点")
            if not DiskSpaceUtils.is_safe_root(node, protect, keywords):
                raise ValueError("命中保护路径/关键词")
            if node.is_dir():
                # Protect descendants too: deleting an ancestor must not bypass
                # protected subdirectories or recent-file protection.
                return sum(measure(child) for child in node.iterdir())
            if not node.is_file():
                raise ValueError("非普通文件")
            return stat.st_size
        protect_days = int(self._plugin._recent_days_protect or 0)
        # 仅接受扫描器在本轮可信标记的兜底候选，且保留策略最短天数。
        if item.get("fallback") is True:
            protect_days = int(getattr(self._plugin, "_fallback_min_keep_days", protect_days))
        if protect_days > 0 and time.time() - path.stat().st_mtime < protect_days * 86400:
            raise ValueError("命中最近修改保护")
        return measure(resolved)

    def delete_selected(self, selected: List[Dict[str, Any]], 
                        scan_paths: Optional[List[str]] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
        """安全删除已选候选项（带重试机制）。只允许删除位于本次扫描路径下的文件/目录。"""
        deleted: List[Dict[str, Any]] = []
        errors: List[str] = []
        safe_roots = [Path(p).resolve(strict=False) for p in (scan_paths or []) if p]
        protect_dirs = [Path(p).as_posix().rstrip("/") for p in DiskSpaceUtils.lines(self._plugin._protect_dirs)]
        protect_keywords = [k.lower() for k in DiskSpaceUtils.lines(self._plugin._protect_keywords)]
        max_retries = 3  # 最大重试次数
        retry_delay = 0.5  # 重试间隔（秒）
        
        for item in selected:
            raw_path = item.get("path")
            if not raw_path:
                continue
            path = Path(raw_path)
            try:
                resolved = path.resolve(strict=False)
                if path.is_symlink() or not safe_roots or not any(resolved != root and DiskSpaceUtils.is_relative_to(resolved, root) for root in safe_roots):
                    msg = f"跳过不在扫描路径内的候选：{path}"
                    logger.warning(msg)
                    errors.append(msg)
                    continue
                
                if not DiskSpaceUtils.is_safe_root(path, protect_dirs, protect_keywords):
                    msg = f"跳过不符合路径规则的路径：{path}"
                    logger.warning(msg)
                    errors.append(msg)
                    continue

                if DiskSpaceUtils.is_series_candidate(path):
                    series_ok, series_reason = DiskSpaceUtils.is_completed_complete_series(
                        path,
                        max_scan_items=self._plugin._max_scan_items,
                        media_chain=self._media_chain,
                    )
                    if not series_ok:
                        msg = f"跳过未完结或未完整入库的电视剧：{path}，原因={series_reason}"
                        logger.warning(msg)
                        errors.append(msg)
                        continue
                
                if not path.exists():
                    logger.info(f"跳过不存在的路径：{path}")
                    continue
                
                # 删除（带重试）
                last_error = None
                for attempt in range(max_retries):
                    try:
                        if path.is_dir():
                            shutil.rmtree(path)
                        else:
                            path.unlink()
                        deleted.append(item)
                        logger.info(f"已删除：{path}")
                        break  # 成功，跳出重试循环
                    except Exception as e:
                        last_error = e
                        if attempt < max_retries - 1:
                            logger.debug(f"删除失败（第{attempt + 1}次尝试），{retry_delay}秒后重试：{path} - {e}")
                            time.sleep(retry_delay)
                        else:
                            msg = f"删除失败（已重试{max_retries}次）{path}: {e}"
                            logger.warning(msg)
                            errors.append(msg)
            except Exception as e:
                msg = f"删除处理异常 {path}: {e}"
                logger.warning(msg)
                errors.append(msg)
        
        return deleted, errors
