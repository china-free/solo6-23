"""素材类型识别模块"""
from __future__ import annotations

import mimetypes
import zipfile
import tarfile
import gzip
import bz2
from pathlib import Path
from typing import Optional, Tuple

from .models import MediaType


IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif",
    ".svg", ".ico", ".heic", ".heif", ".raw", ".psd", ".ai", ".eps",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".avi", ".mov", ".wmv", ".flv", ".mkv", ".webm", ".m4v",
    ".3gp", ".mpeg", ".mpg", ".ts", ".mts", ".vob", ".ogv", ".rmvb",
}

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a", ".opus",
    ".ape", ".alac", ".aiff", ".dsd", ".amr",
}

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".log", ".csv", ".json", ".xml", ".html",
    ".htm", ".css", ".js", ".py", ".java", ".c", ".cpp", ".h", ".cs",
    ".php", ".rb", ".go", ".rs", ".ts", ".jsx", ".tsx", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".rtf", ".srt", ".ass", ".sub",
    ".sql", ".sh", ".bat", ".ps1", ".pdf", ".doc", ".docx", ".rtf",
}

ARCHIVE_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".tbz2",
    ".tar.gz", ".tar.bz2", ".tar.xz", ".zst", ".lz",
}


IMAGE_MAGIC = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"BM": "image/bmp",
    b"RIFF": "image/webp",
    b"\x00\x00\x01\x00": "image/x-icon",
    b"\x00\x00\x02\x00": "image/x-icon",
    b"II*\x00": "image/tiff",
    b"MM\x00*": "image/tiff",
}

VIDEO_MAGIC = {
    b"\x00\x00\x00\x18ftyp": "video/mp4",
    b"\x00\x00\x00\x20ftyp": "video/mp4",
    b"RIFF": "video/avi",
    b"FLV": "video/x-flv",
    b"OggS": "video/ogg",
    b"\x1aE\xdf\xa3": "video/x-matroska",
}

AUDIO_MAGIC = {
    b"ID3": "audio/mpeg",
    b"\xff\xfb": "audio/mpeg",
    b"\xff\xf3": "audio/mpeg",
    b"RIFF": "audio/wav",
    b"fLaC": "audio/flac",
    b"OggS": "audio/ogg",
    b"ADIF": "audio/aac",
    b"FORM": "audio/aiff",
}

ARCHIVE_MAGIC = {
    b"PK\x03\x04": "application/zip",
    b"PK\x05\x06": "application/zip",
    b"Rar!": "application/x-rar",
    b"\x1f\x8b": "application/gzip",
    b"BZh": "application/x-bzip2",
    b"\xfd7zXZ\x00": "application/x-7z-compressed",
    b"ustar": "application/x-tar",
}


def _read_magic(file_path: Path, max_bytes: int = 32) -> bytes:
    try:
        with open(file_path, "rb") as f:
            return f.read(max_bytes)
    except (IOError, PermissionError):
        return b""


def _match_magic(data: bytes, magic_map: dict[bytes, str]) -> Optional[str]:
    for magic, mime in magic_map.items():
        if data.startswith(magic):
            return mime
    return None


def _detect_riff_subtype(data: bytes) -> Optional[Tuple[MediaType, str]]:
    if not data.startswith(b"RIFF") or len(data) < 16:
        return None
    sub = data[8:12]
    if sub == b"WAVE":
        return MediaType.AUDIO, "audio/wav"
    if sub == b"WEBP":
        return MediaType.IMAGE, "image/webp"
    if sub == b"AVI ":
        return MediaType.VIDEO, "video/avi"
    return None


def detect_by_extension(file_path: str | Path) -> Tuple[MediaType, str]:
    path = Path(file_path)
    ext = path.suffix.lower()
    name_lower = path.name.lower()

    if ext in IMAGE_EXTENSIONS or (ext == "" and False):
        mime = mimetypes.types_map.get(ext, "image/*")
        return MediaType.IMAGE, mime

    if ext in VIDEO_EXTENSIONS:
        mime = mimetypes.types_map.get(ext, "video/*")
        return MediaType.VIDEO, mime

    if ext in AUDIO_EXTENSIONS:
        mime = mimetypes.types_map.get(ext, "audio/*")
        return MediaType.AUDIO, mime

    if ext in TEXT_EXTENSIONS:
        mime = mimetypes.types_map.get(ext, "text/plain")
        return MediaType.TEXT, mime

    for arc_ext in ARCHIVE_EXTENSIONS:
        if name_lower.endswith(arc_ext):
            return MediaType.ARCHIVE, "application/x-compressed"

    return MediaType.UNKNOWN, ""


def detect_by_magic(file_path: str | Path) -> Tuple[MediaType, str]:
    path = Path(file_path)
    if not path.is_file():
        return MediaType.UNKNOWN, ""

    data = _read_magic(path, max_bytes=64)
    if not data:
        return MediaType.UNKNOWN, ""

    if riff := _detect_riff_subtype(data):
        return riff

    if mime := _match_magic(data, IMAGE_MAGIC):
        return MediaType.IMAGE, mime
    if mime := _match_magic(data, VIDEO_MAGIC):
        return MediaType.VIDEO, mime
    if mime := _match_magic(data, AUDIO_MAGIC):
        return MediaType.AUDIO, mime
    if mime := _match_magic(data, ARCHIVE_MAGIC):
        return MediaType.ARCHIVE, mime

    try:
        with open(path, "rb") as f:
            sample = f.read(4096)
        sample.decode("utf-8")
        return MediaType.TEXT, "text/plain"
    except UnicodeDecodeError:
        pass

    return MediaType.UNKNOWN, ""


def detect_media_type(file_path: str | Path) -> Tuple[MediaType, str]:
    path = Path(file_path)
    ext_type, ext_mime = detect_by_extension(path)
    magic_type, magic_mime = detect_by_magic(path)

    if magic_type != MediaType.UNKNOWN:
        return magic_type, magic_mime or ext_mime
    if ext_type != MediaType.UNKNOWN:
        return ext_type, ext_mime

    return MediaType.UNKNOWN, ext_mime or magic_mime


def is_archive(file_path: str | Path) -> bool:
    path = Path(file_path)
    if zipfile.is_zipfile(path):
        return True
    try:
        with tarfile.open(path):
            return True
    except (tarfile.TarError, OSError):
        pass
    try:
        with gzip.open(path):
            return True
    except (OSError, EOFError, gzip.BadGzipFile):
        pass
    try:
        with bz2.open(path):
            return True
    except (OSError, EOFError, ValueError):
        pass
    return False


def list_archive_contents(file_path: str | Path) -> list[str]:
    path = Path(file_path)
    contents: list[str] = []

    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as zf:
                contents = [n for n in zf.namelist() if not n.endswith("/")]
        except (zipfile.BadZipFile, OSError):
            pass
        return contents

    try:
        with tarfile.open(path) as tf:
            contents = [m.name for m in tf.getmembers() if m.isfile()]
    except (tarfile.TarError, OSError):
        pass

    return contents
