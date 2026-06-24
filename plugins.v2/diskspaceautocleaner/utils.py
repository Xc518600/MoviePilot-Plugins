import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.chain.media import MediaChain
from app.core.metainfo import MetaInfo
from app.log import logger
from app.schemas import MediaType


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
    EPISODE_METADATA_TAGS = {
        "totalepisodes", "total_episodes", "episodecount", "episode_count", "numberofepisodes",
        "number_of_episodes", "episodes", "aired_episodes", "airedepisodes"
    }
    STATUS_METADATA_TAGS = {"status", "state", "seriesstatus"}

    @staticmethod
    def build_tmdb_titles(path: Path) -> List[str]:
        """为 TMDB 识别构造多个标题候选：原名、带年份名、清洗名。"""
        raw_name = path.stem if path.is_file() else path.name
        cleaned = DiskSpaceUtils.extract_movie_title(path)
        titles: List[str] = []

        def add_title(value: Optional[str]):
            text = str(value or "").strip()
            if text and text not in titles:
                titles.append(text)

        add_title(raw_name)

        year_match = re.search(r'(19|20)\d{2}', raw_name)
        if cleaned and year_match:
            add_title(f"{cleaned} {year_match.group(0)}")

        add_title(cleaned)
        return titles
    
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
    def _strip_xml_ns(tag: str) -> str:
        """去除 XML 命名空间并统一小写。"""
        return str(tag or "").split("}")[-1].strip().lower()

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        """从字符串/数字中安全提取正整数。"""
        if value is None:
            return None
        try:
            if isinstance(value, (int, float)):
                number = int(value)
                return number if 1 <= number <= 5000 else None
            match = re.search(r"\d{1,4}", str(value))
            if not match:
                return None
            number = int(match.group(0))
            return number if 1 <= number <= 5000 else None
        except Exception:
            return None

    @staticmethod
    def _parse_xml_file(path: Path) -> Optional[ET.Element]:
        """宽容解析 XML/NFO 文件，失败返回 None。"""
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if not text.strip():
                return None
            return ET.fromstring(text.encode("utf-8"))
        except Exception:
            return None

    @staticmethod
    def _collect_nfo_files(path: Path, max_files: int = 300) -> List[Path]:
        """收集候选电视剧目录里的 NFO 文件，优先读取剧集/季元数据，再读取分集元数据。"""
        try:
            if path.is_file():
                nfo = path.with_suffix(".nfo")
                return [nfo] if nfo.exists() else []
            if not path.is_dir():
                return []

            preferred_names = {"tvshow.nfo", "show.nfo", "series.nfo", "season.nfo"}
            preferred: List[Path] = []
            others: List[Path] = []
            for root, _, files in os.walk(path):
                for name in files:
                    if not name.lower().endswith(".nfo"):
                        continue
                    item = Path(root) / name
                    if name.lower() in preferred_names:
                        preferred.append(item)
                    else:
                        others.append(item)
                    if len(preferred) + len(others) >= max_files:
                        break
                if len(preferred) + len(others) >= max_files:
                    break
            return sorted(preferred) + sorted(others)
        except Exception:
            return []

    @staticmethod
    def _extract_xml_metadata(path: Path, max_files: int = 300) -> Dict[str, Any]:
        """解析 MoviePilot/Emby/Jellyfin/Kodi 常见 NFO/TMDB 风格元数据。"""
        status_values: List[str] = []
        episode_counts: List[int] = []
        episode_keys = set()
        parsed_files = 0

        for nfo in DiskSpaceUtils._collect_nfo_files(path, max_files=max_files):
            root = DiskSpaceUtils._parse_xml_file(nfo)
            if root is None:
                continue
            parsed_files += 1
            root_name = DiskSpaceUtils._strip_xml_ns(root.tag)
            current_season: Optional[int] = None
            current_episode: Optional[int] = None
            for elem in root.iter():
                tag = DiskSpaceUtils._strip_xml_ns(elem.tag)
                text = (elem.text or "").strip()
                if not text:
                    continue
                if tag in DiskSpaceUtils.STATUS_METADATA_TAGS:
                    status_values.append(text)
                if root_name not in {"episodedetails", "episode"} and tag in DiskSpaceUtils.EPISODE_METADATA_TAGS:
                    number = DiskSpaceUtils._safe_int(text)
                    if number:
                        episode_counts.append(number)
                if tag in {"season", "seasonnumber"}:
                    current_season = DiskSpaceUtils._safe_int(text)
                elif tag in {"episode", "episodenumber"}:
                    current_episode = DiskSpaceUtils._safe_int(text)

            if root_name in {"episodedetails", "episode"} and current_episode:
                episode_keys.add((current_season or 1, current_episode))

        return {
            "parsed_nfo_files": parsed_files,
            "status_values": status_values,
            "episode_counts": episode_counts,
            "episode_keys": episode_keys,
        }

    @staticmethod
    def _extract_episode_keys_from_name(name: str) -> List[Tuple[int, int]]:
        """从文件名中提取 SxxEyy / 第xx集 / Exx 等分集编号。"""
        keys: List[Tuple[int, int]] = []
        text = str(name or "")
        for match in re.finditer(r"[Ss](\d{1,2})[ ._-]*[Ee](\d{1,4})", text):
            season = DiskSpaceUtils._safe_int(match.group(1)) or 1
            episode = DiskSpaceUtils._safe_int(match.group(2))
            if episode:
                keys.append((season, episode))
        for match in re.finditer(r"(?:第\s*)?(\d{1,4})\s*(?:集|话|話)", text):
            episode = DiskSpaceUtils._safe_int(match.group(1))
            if episode:
                keys.append((1, episode))
        if not keys:
            match = re.search(r"(?:^|[ ._\-])E(\d{1,4})(?:\D|$)", text, flags=re.IGNORECASE)
            if match:
                episode = DiskSpaceUtils._safe_int(match.group(1))
                if episode:
                    keys.append((1, episode))
        return keys

    @staticmethod
    def collect_local_episode_keys(path: Path, max_items: int = 10000) -> set:
        """从本地视频文件名和分集 NFO 中收集唯一集编号。"""
        keys = set()
        scanned = 0
        try:
            files: List[Path] = []
            if path.is_file():
                files = [path]
            elif path.is_dir():
                for root, _, names in os.walk(path):
                    for name in names:
                        suffix = Path(name).suffix.lower()
                        if suffix in DiskSpaceUtils.VIDEO_EXTENSIONS or suffix == ".nfo":
                            files.append(Path(root) / name)
                            scanned += 1
                            if scanned >= max_items:
                                break
                    if scanned >= max_items:
                        break
            for item in files:
                if item.suffix.lower() in DiskSpaceUtils.VIDEO_EXTENSIONS:
                    for key in DiskSpaceUtils._extract_episode_keys_from_name(item.name):
                        keys.add(key)
                    continue
                root = DiskSpaceUtils._parse_xml_file(item)
                if root is None:
                    continue
                root_name = DiskSpaceUtils._strip_xml_ns(root.tag)
                if root_name not in {"episodedetails", "episode"}:
                    continue
                season = None
                episode = None
                for elem in root.iter():
                    tag = DiskSpaceUtils._strip_xml_ns(elem.tag)
                    text = (elem.text or "").strip()
                    if tag in {"season", "seasonnumber"}:
                        season = DiskSpaceUtils._safe_int(text)
                    elif tag in {"episode", "episodenumber"}:
                        episode = DiskSpaceUtils._safe_int(text)
                if episode:
                    keys.add((season or 1, episode))
        except Exception:
            pass
        return keys

    @staticmethod
    def get_tmdb_episode_count(path: Path, media_chain=None) -> Tuple[Optional[int], Optional[str]]:
        """
        通过 MoviePilot 的 MediaChain 从 TMDB 获取电视剧总集数和完结状态。
        
        返回: (总集数, 完结状态)
        """
        if media_chain is None:
            return None, None
            
        try:
            titles = DiskSpaceUtils.build_tmdb_titles(path)
            if not titles:
                logger.warning(f"TMDB 查询失败: {path.name} - 无法从目录名提取标题")
                return None, None

            logger.info(f"TMDB 查询开始: path={path.name}, titles={titles}")

            last_status = None
            for title in titles:
                meta_info = MetaInfo(title)
                meta_info.type = MediaType.TV

                mediainfo = media_chain.recognize_media(meta=meta_info)
                if not mediainfo:
                    logger.warning(f"TMDB 查询失败: {path.name} - recognize_media 未识别到媒体信息, title={title}")
                    continue

                tmdb_id = mediainfo.tmdb_id
                if not tmdb_id:
                    logger.warning(
                        f"TMDB 查询失败: {path.name} - recognize_media 未返回 tmdb_id, "
                        f"title={title}, mediainfo_title={getattr(mediainfo, 'title', None)}, year={getattr(mediainfo, 'year', None)}"
                    )
                    continue

                tmdb_info = media_chain.tmdb_info(tmdbid=tmdb_id, mtype=MediaType.TV)
                if not tmdb_info:
                    logger.warning(f"TMDB 查询失败: {path.name} - tmdb_info 返回空, tmdb_id={tmdb_id}, title={title}")
                    continue

                total_episodes = tmdb_info.get("number_of_episodes") or tmdb_info.get("total_episodes") or tmdb_info.get("episode_count")
                status = tmdb_info.get("status")
                last_status = status

                if not total_episodes:
                    logger.warning(
                        f"TMDB 查询失败: {path.name} - TMDB 详情缺少总集数字段, "
                        f"tmdb_id={tmdb_id}, title={title}, keys={list(tmdb_info.keys())[:20]}"
                    )
                    continue

                logger.info(
                    f"TMDB 查询成功: path={path.name}, title={title}, tmdb_id={tmdb_id}, "
                    f"total_episodes={total_episodes}, status={status}"
                )
                return total_episodes, status

            return None, last_status
            
        except Exception as e:
            logger.warning(f"TMDB 查询失败: {path.name} - {str(e)}")
            return None, None

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
    def is_completed_complete_series(path: Path, max_scan_items: int = 10000, media_chain=None) -> Tuple[bool, str]:
        """
        电视剧删除保护：通过 TMDB 查询确认“已完结”且“本地集数完整”才允许删除。
        """
        if not DiskSpaceUtils.is_series_candidate(path):
            return True, "非电视剧候选"

        # 从 TMDB 获取总集数和完结状态
        expected_count, tmdb_status = DiskSpaceUtils.get_tmdb_episode_count(path, media_chain)
        
        if not expected_count:
            return False, "无法从 TMDB 获取电视剧总集数"
        
        # 检查 TMDB 状态是否为已完结
        if tmdb_status:
            status_lower = str(tmdb_status).lower()
            if status_lower in ["ended", "completed", "canceled"]:
                # 已完结，继续检查本地集数
                pass
            elif status_lower in ["returning series", "planned", "in production", "ongoing"]:
                return False, f"电视剧未完结（TMDB状态={tmdb_status}）"
        
        # 统计本地集数
        local_episode_keys = DiskSpaceUtils.collect_local_episode_keys(path, max_items=max_scan_items)
        xml_meta = DiskSpaceUtils._extract_xml_metadata(path, max_files=max(50, min(max_scan_items, 300)))
        xml_episode_keys = xml_meta.get("episode_keys") or set()
        if xml_episode_keys:
            local_episode_keys.update(xml_episode_keys)
        local_count = len(local_episode_keys) if local_episode_keys else DiskSpaceUtils.count_video_files(path, max_items=max_scan_items)
        
        if local_count < expected_count:
            return False, f"电视剧本地集数不完整：{local_count}/{expected_count}"
        
        return True, f"电视剧已完结且本地集数完整：{local_count}/{expected_count}（来源=TMDB）"

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
