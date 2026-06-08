import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.log import logger


class DiskSpaceUtils:
    """硬盘空间自动清理工具类。"""
    
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
