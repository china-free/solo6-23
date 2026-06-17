"""元数据抽取模块"""
from __future__ import annotations

import os
import re
import struct
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image

from .models import MediaMetadata, MediaType, compute_file_hash
from .type_detector import detect_media_type


def _parse_mp4_duration(file_path: Path) -> Optional[float]:
    try:
        with open(file_path, "rb") as f:
            data = f.read(1024 * 1024)
        idx = data.find(b"mvhd")
        if idx < 0 or idx + 32 > len(data):
            return None
        version = data[idx + 4]
        if version == 0:
            timescale = struct.unpack(">I", data[idx + 16:idx + 20])[0]
            duration = struct.unpack(">I", data[idx + 20:idx + 24])[0]
        else:
            timescale = struct.unpack(">I", data[idx + 28:idx + 32])[0]
            duration = struct.unpack(">Q", data[idx + 32:idx + 40])[0]
        if timescale > 0:
            return duration / timescale
    except (IOError, OSError, struct.error):
        pass
    return None


def _parse_mp4_dimensions(file_path: Path) -> tuple[Optional[int], Optional[int]]:
    try:
        with open(file_path, "rb") as f:
            data = f.read(4 * 1024 * 1024)
        width, height = None, None
        for atom in [b"avcC", b"hvcC", b"vp09"]:
            idx = data.find(atom)
            if idx > 0:
                try:
                    width = struct.unpack(">H", data[idx + 7:idx + 9])[0]
                    height = struct.unpack(">H", data[idx + 9:idx + 11])[0]
                    break
                except struct.error:
                    pass
        tkhd = data.find(b"tkhd")
        if tkhd > 0 and (width is None or height is None):
            try:
                version = data[tkhd + 4]
                if version == 0:
                    base = tkhd + 84
                else:
                    base = tkhd + 92
                width_raw = struct.unpack(">I", data[base:base + 4])[0]
                height_raw = struct.unpack(">I", data[base + 4:base + 8])[0]
                width = width_raw >> 16
                height = height_raw >> 16
            except struct.error:
                pass
        return width, height
    except (IOError, OSError):
        return None, None


def _parse_wav_duration(file_path: Path) -> tuple[Optional[float], Optional[int], Optional[int]]:
    duration = None
    sample_rate = None
    channels = None
    try:
        with open(file_path, "rb") as f:
            riff = f.read(12)
            if riff[0:4] != b"RIFF" or riff[8:12] != b"WAVE":
                return None, None, None
            while True:
                chunk_header = f.read(8)
                if len(chunk_header) < 8:
                    break
                chunk_id = chunk_header[0:4]
                chunk_size = struct.unpack("<I", chunk_header[4:8])[0]
                if chunk_id == b"fmt ":
                    if chunk_size >= 16:
                        fmt_data = f.read(chunk_size)
                        channels = struct.unpack("<H", fmt_data[2:4])[0]
                        sample_rate = struct.unpack("<I", fmt_data[4:8])[0]
                        byte_rate = struct.unpack("<I", fmt_data[8:12])[0]
                        block_align = struct.unpack("<H", fmt_data[12:14])[0]
                    else:
                        f.seek(chunk_size, 1)
                elif chunk_id == b"data":
                    if sample_rate and channels:
                        block_align = channels * 2
                        num_samples = chunk_size // block_align if block_align else 0
                        if sample_rate > 0:
                            duration = num_samples / sample_rate
                    f.seek(chunk_size, 1)
                else:
                    if chunk_size % 2:
                        chunk_size += 1
                    f.seek(chunk_size, 1)
                if f.tell() >= file_path.stat().st_size:
                    break
    except (IOError, OSError, struct.error):
        pass
    return duration, sample_rate, channels


def _parse_mp3_duration(file_path: Path) -> Optional[float]:
    try:
        file_size = file_path.stat().st_size
        with open(file_path, "rb") as f:
            header = f.read(10)
        bitrate_kbps = 0
        if header[0:3] == b"ID3":
            size = 0
            for i in range(6, 10):
                size = (size << 7) | (header[i] & 0x7F)
            with open(file_path, "rb") as f:
                f.seek(size + 10)
                frame_header = f.read(4)
        else:
            frame_header = header[0:4]
        bitrates = [
            [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0],
            [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0],
            [0, 32, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 320, 0],
        ]
        if len(frame_header) >= 4 and frame_header[0] == 0xFF and (frame_header[1] & 0xE0) == 0xE0:
            version_idx = (frame_header[1] >> 3) & 0x03
            layer_idx = (frame_header[1] >> 1) & 0x03
            bitrate_idx = (frame_header[2] >> 4) & 0x0F
            br_table = 0 if (version_idx in (0, 2) and layer_idx == 3) else (1 if layer_idx == 2 else 2)
            if 0 <= bitrate_idx <= 15:
                bitrate_kbps = bitrates[br_table][bitrate_idx]
        if bitrate_kbps > 0:
            return (file_size * 8) / (bitrate_kbps * 1000)
    except (IOError, OSError, struct.error, IndexError):
        pass
    return None


def _detect_text_encoding(file_path: Path) -> tuple[str, int]:
    encoding = ""
    length = 0
    try:
        with open(file_path, "rb") as f:
            raw = f.read()
        length = len(raw)
        for enc in ("utf-8-sig", "utf-8", "gbk", "gb2312", "utf-16", "latin-1"):
            try:
                decoded = raw.decode(enc)
                encoding = enc
                length = len(decoded)
                break
            except UnicodeDecodeError:
                continue
    except (IOError, OSError):
        pass
    return encoding, length


def _extract_image_metadata(file_path: Path, md: MediaMetadata) -> None:
    try:
        with Image.open(file_path) as img:
            md.width, md.height = img.size
            md.color_mode = img.mode
            md.codec = img.format or ""
            if "dpi" in img.info and img.info["dpi"]:
                md.extra["dpi"] = img.info["dpi"]
            if "exif" in img.info:
                md.extra["has_exif"] = True
            if getattr(img, "is_animated", False):
                md.extra["animated"] = True
                md.extra["frames"] = getattr(img, "n_frames", 0)
    except Exception as e:
        md.extra["image_error"] = str(e)


def _extract_video_metadata(file_path: Path, md: MediaMetadata) -> None:
    ext = file_path.suffix.lower()
    if ext in (".mp4", ".m4v", ".mov"):
        dur = _parse_mp4_duration(file_path)
        if dur is not None:
            md.duration_seconds = dur
        w, h = _parse_mp4_dimensions(file_path)
        if w:
            md.width = w
        if h:
            md.height = h
        if ext == ".mp4":
            md.codec = "H.264" if md.codec == "" else md.codec
    elif ext in (".avi",):
        md.codec = "AVI"
    elif ext in (".mkv",):
        md.codec = "Matroska"
    elif ext in (".webm",):
        md.codec = "WebM"
    elif ext in (".flv",):
        md.codec = "FLV"
    if file_path.stat().st_size > 0 and md.duration_seconds:
        md.bitrate = int((file_path.stat().st_size * 8) / md.duration_seconds)


def _extract_audio_metadata(file_path: Path, md: MediaMetadata) -> None:
    ext = file_path.suffix.lower()
    if ext == ".wav":
        dur, sr, ch = _parse_wav_duration(file_path)
        md.duration_seconds = dur
        md.sample_rate = sr
        md.channels = ch
        md.codec = "PCM"
    elif ext in (".mp3",):
        md.duration_seconds = _parse_mp3_duration(file_path)
        md.codec = "MP3"
    elif ext in (".flac",):
        md.codec = "FLAC"
    elif ext in (".aac",):
        md.codec = "AAC"
    elif ext in (".ogg", ".opus"):
        md.codec = "Vorbis"
    elif ext in (".wma",):
        md.codec = "WMA"
    elif ext in (".m4a",):
        md.codec = "ALAC/AAC"
    if file_path.stat().st_size > 0 and md.duration_seconds:
        md.bitrate = int((file_path.stat().st_size * 8) / md.duration_seconds)


def _extract_text_metadata(file_path: Path, md: MediaMetadata) -> None:
    enc, length = _detect_text_encoding(file_path)
    md.text_encoding = enc
    md.text_length = length
    ext = file_path.suffix.lower()
    if ext in (".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs"):
        try:
            with open(file_path, "r", encoding=enc or "utf-8", errors="ignore") as f:
                text = f.read()
            lines = text.splitlines()
            md.extra["lines"] = len(lines)
            md.extra["non_empty_lines"] = sum(1 for l in lines if l.strip())
            code_lines = sum(1 for l in lines if l.strip() and not l.strip().startswith(("#", "//", "/*", "*")))
            md.extra["code_lines"] = code_lines
        except (IOError, OSError):
            pass


def extract_basic_metadata(file_path: str | Path, compute_hash: bool = True) -> MediaMetadata:
    path = Path(file_path).resolve()
    stat = path.stat()

    md = MediaMetadata(
        file_path=str(path),
        file_name=path.name,
        file_size=stat.st_size,
        created_at=datetime.fromtimestamp(stat.st_ctime),
        modified_at=datetime.fromtimestamp(stat.st_mtime),
    )

    if compute_hash:
        try:
            md.file_hash = compute_file_hash(path)
        except (IOError, PermissionError):
            pass

    media_type, mime = detect_media_type(path)
    md.media_type = media_type
    md.mime_type = mime

    return md


def extract_full_metadata(file_path: str | Path, compute_hash: bool = True) -> MediaMetadata:
    md = extract_basic_metadata(file_path, compute_hash=compute_hash)
    path = Path(file_path)

    if md.media_type == MediaType.IMAGE:
        _extract_image_metadata(path, md)
    elif md.media_type == MediaType.VIDEO:
        _extract_video_metadata(path, md)
    elif md.media_type == MediaType.AUDIO:
        _extract_audio_metadata(path, md)
    elif md.media_type == MediaType.TEXT:
        _extract_text_metadata(path, md)

    return md
