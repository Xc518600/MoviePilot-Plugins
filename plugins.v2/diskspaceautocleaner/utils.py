import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger


class DiskSpaceUtils:
    """硬盘空间自动清理工具类。"""
    VIDEO_EXTENSIONS = {
        ".mp4", ".mkv", ".avi", ".rmvb", ".flv", ".wmv", ".ts", ".mov", ".m4v",
        ".mpg", ".mpeg", ".webm", ".iso", ".m2ts", ".strm"
    }
    ONGOING_SERIES_MARKERS = {
        "未完结", "连载", "连载中", "更新中", "未完待续", "未完", "ongoing",
        "continuing", "returning series", "in production"
    }
    COMPLETED_SERIES_MARKERS = {
        "已完结", "完结", "全集", "全剧终", "complete", "completed", "ended", "status>ended"
    }
    
    @staticmethod
    def to_bool(value: Any, default: bool = False) -> bool:
        """兼容 MoviePilot 配置中布尔值可能以字符串/数字形式传入。"""
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "y", "on", "启用", "开启", "是"}:
            return True
        if text in {"false", "0", "no", "n", "off", "禁用", "关闭", "否", ""}:
            return False
        return default

    @staticmethod
    def to_int(value: Any, default: int = 0) -> int:
        """兼容 MoviePilot 配置中数字可能以字符串形式传入，并保留 0。"""
        if value is None or value == "":
            return default
        try:
            return int(float(str(value).strip()))
        except Exception:
            return default

    @staticmethod
    def lines(text: str) -> List[str]:
        """将文本分割成行，去除空行。"""
        if not text:
            return []
        return [line.strip() for line in str(text).splitlines() if line.strip()]

    @staticmethod
    def is_relative_to(path: Path, root: Path) -> bool:
        """检查 path 是否在 root 之下。"""
        try:
            path.relative_to(root)
            return True
        except Exception:
            return False

    @staticmethod
    def is_series_folder(path: Path) -> bool:
        """判断是否为电视剧目录（使用 os.scandir() 优化）。"""
        try:
            if not path.is_dir():
                return False
            with os.scandir(path) as it:
                for entry in it:
                    if entry.is_dir():
                        name = entry.name.lower()
                        if (name.startswith("season") or 
                            (name.startswith("s") and len(name) > 1 and name[1:].isdigit()) or
                            "season" in name):
                            return True
        except Exception:
            pass
        return False

    @staticmethod
    def is_series_candidate(path: Path) -> bool:
        """判断候选项是否应按电视剧处理。"""
        try:
            if DiskSpaceUtils.detect_root_type(path) == "电视剧":
                return True
            if DiskSpaceUtils.is_series_folder(path):
                return True
            path_lower = path.as_posix().lower()
            return any(k in path_lower for k in ["/电视剧/", "/电视/", "/tv/", "/series/", "/drama/"])
        except Exception:
            return False

    @staticmethod
    def _read_series_metadata_text(path: Path, max_files: int = 8, max_chars: int = 200_000) -> str:
        """读取电视剧目录内少量 NFO/元数据文本，用于判断完结状态。读取失败时返回空文本。"""
        chunks: List[str] = [path.name]
        candidates: List[Path] = []
        try:
            if path.is_file():
                candidates.append(path.with_suffix(".nfo"))
            elif path.is_dir():
                preferred = [path / "tvshow.nfo", path / "show.nfo", path / "series.nfo"]
                candidates.extend(preferred)
                candidates.extend(sorted(path.glob("*.nfo"))[:max_files])
        except Exception:
            return " ".join(chunks)

        seen = set()
        total_chars = 0
        for item in candidates:
            try:
                if item in seen or not item.exists() or not item.is_file():
                    continue
                seen.add(item)
                text = item.read_text(encoding="utf-8", errors="ignore")[:max_chars]
                chunks.append(text)
                total_chars += len(text)
                if total_chars >= max_chars:
                    break
            except Exception:
                continue
        return "\n".join(chunks)

    @staticmethod
    def _extract_expected_episode_count(text: str) -> Optional[int]:
        """从目录名/NFO 中提取“全 N 集/共 N 集/N 集全”等总集数。"""
        patterns = [
            r"(?:全|共|全集|完结|已完结)\s*(\d{1,4})\s*(?:集|话|話|episodes?|eps?)",
            r"(\d{1,4})\s*(?:集|话|話)\s*(?:全|完结|已完结)",
            r"(?:totalepisodes|episodecount|episodes?)\s*[:：>\s]+(\d{1,4})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            try:
                value = int(match.group(1))
                if 1 <= value <= 2000:
                    return value
            except Exception:
                continue
        return None

    @staticmethod
    def count_video_files(path: Path, max_items: int = 10000) -> int:
        """统计候选目录下的视频文件数量，超过 max_items 时提前停止。"""
        count = 0
        try:
            if path.is_file():
                return 1 if path.suffix.lower() in DiskSpaceUtils.VIDEO_EXTENSIONS else 0
            for root, _, files in os.walk(path):
                for name in files:
                    if Path(name).suffix.lower() in DiskSpaceUtils.VIDEO_EXTENSIONS:
                        count += 1
                        if count >= max_items:
                            return count
        except Exception:
            return count
        return count

    @staticmethod
    def is_completed_complete_series(path: Path, max_scan_items: int = 10000) -> Tuple[bool, str]:
        """
        电视剧删除保护：必须能明确证明“已完结”且“本地集数完整”才允许删除。

        证明方式采用保守本地判断：
        - 路径名或 NFO 明确出现完结标记；
        - 路径名或 NFO 能提取总集数；
        - 本地视频文件数量 >= 总集数；
        任一条件不满足就返回 False，避免误删未完结电视剧。
        """
        if not DiskSpaceUtils.is_series_candidate(path):
            return True, "非电视剧候选"

        metadata_text = DiskSpaceUtils._read_series_metadata_text(path)
        metadata_lower = metadata_text.lower()
        if any(marker.lower() in metadata_lower for marker in DiskSpaceUtils.ONGOING_SERIES_MARKERS):
            return False, "电视剧包含未完结/连载标记"

        has_completed_marker = any(marker.lower() in metadata_lower for marker in DiskSpaceUtils.COMPLETED_SERIES_MARKERS)
        if not has_completed_marker:
            return False, "电视剧未发现明确完结标记"

        expected_count = DiskSpaceUtils._extract_expected_episode_count(metadata_text)
        if not expected_count:
            return False, "电视剧未发现总集数，无法确认完整"

        local_count = DiskSpaceUtils.count_video_files(path, max_items=max_scan_items)
        if local_count < expected_count:
            return False, f"电视剧本地集数不完整：{local_count}/{expected_count}"

        return True, f"电视剧已完结且本地集数完整：{local_count}/{expected_count}"

    @staticmethod
    def detect_root_type(path: Path) -> str:
        """检测根目录类型（电视剧/电影/其他）。"""
        path_str = path.as_posix().lower()
        
        tv_keywords = ["/电视剧/", "/电视/", "/tv/", "/series/", "/drama/"]
        for keyword in tv_keywords:
            if keyword in path_str:
                return "电视剧"
        
        movie_keywords = ["/电影/", "/movie/", "/movies/"]
        for keyword in movie_keywords:
            if keyword in path_str:
                return "电影"
        
        return "其他"

    @staticmethod
    def is_safe_root(path: Path, protect_dirs: List[str], protect_keywords: List[str]) -> bool:
        """检查路径是否安全，不在保护列表中。"""
        path_str = path.as_posix()
        path_lower = path_str.lower()
        
        for protect_dir in protect_dirs:
            if protect_dir and path_str.startswith(protect_dir.rstrip("/")):
                return False
        
        for keyword in protect_keywords:
            if keyword and keyword.lower() in path_lower:
                return False
        
        return True

    @staticmethod
    def calc_path_size_fast(path: Path, max_scan_items: int) -> int:
        """快速计算目录大小（使用 os.scandir() 优化）。"""
        if path.is_file():
            try:
                return path.stat().st_size
            except Exception:
                return 0
        
        total = 0
        count = 0
        
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if entry.is_file(follow_symlinks=False):
                        try:
                            total += entry.stat().st_size
                            count += 1
                        except Exception:
                            pass
                    elif entry.is_dir(follow_symlinks=False):
                        total += DiskSpaceUtils.calc_path_size_fast(Path(entry.path), max_scan_items)
                    
                    if count > max_scan_items:
                        break
        except Exception:
            pass
        
        return total

    @staticmethod
    def extract_movie_title(path: Path) -> Optional[str]:
        """从路径中提取电影/电视剧标题。"""
        name = path.name
        name = re.sub(r'\.(mp4|mkv|avi|rmvb|flv|wmv|ts|mov|m4v)$', '', name, flags=re.IGNORECASE)
        name = re.sub(r'\b(19|20)\d{2}[^a-z]*$', '', name)
        name = re.sub(r'\b(19|20)\d{2}\.', '', name)
        name = re.sub(r'[\(\[（【]\s*(19|20)\d{2}\s*[\)\]）】]\s*$', '', name)
        name = re.sub(r'\s*[-_.]\s*(19|20)\d{2}\s*$', '', name)
        name = re.sub(r'\b(1080p|720p|4k|2160p|480p|360p)\b', '', name, flags=re.IGNORECASE)
        name = re.sub(r'\b(web-dl|bluray|bdrip|hdtv|hdcam|ts|cam)\b', '', name, flags=re.IGNORECASE)
        name = re.sub(r'\-\s*[\w\-]+$', '', name)
        name = re.sub(r'[\(\[（【]\s*$', '', name)
        name = re.sub(r'[\s\-_.]+$', '', name)
        name = re.sub(r'\s{2,}', ' ', name)
        return name.strip() if name.strip() else None
