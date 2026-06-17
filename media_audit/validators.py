"""异常检测模块 - 重复素材、缺字段、命名规范、编码异常"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .models import (
    AuditResult,
    AuditStatus,
    Issue,
    IssueSeverity,
    MediaMetadata,
    MediaType,
)


NAMING_PATTERN = re.compile(
    r"^[A-Za-z0-9\u4e00-\u9fa5_-]+(\.[A-Za-z0-9]+)?$"
)

SPECIAL_CHARS = re.compile(r"[\\/:*?\"<>|~\s]")

REQUIRED_FIELDS_BY_TYPE: dict[MediaType, list[str]] = {
    MediaType.IMAGE: ["width", "height", "codec"],
    MediaType.VIDEO: ["width", "height", "duration_seconds"],
    MediaType.AUDIO: ["duration_seconds", "codec"],
    MediaType.TEXT: ["text_length", "text_encoding"],
}


class DuplicateDetector:
    def __init__(self) -> None:
        self._hash_map: dict[str, list[str]] = defaultdict(list)

    def add(self, md: MediaMetadata) -> None:
        if md.file_hash:
            self._hash_map[md.file_hash].append(md.file_path)

    def find_duplicates(self, md: MediaMetadata) -> list[str]:
        if not md.file_hash:
            return []
        paths = self._hash_map.get(md.file_hash, [])
        return [p for p in paths if p != md.file_path]

    @property
    def duplicate_groups(self) -> list[list[str]]:
        return [paths for paths in self._hash_map.values() if len(paths) > 1]


def detect_duplicates(
    results: Iterable[AuditResult],
) -> dict[str, list[str]]:
    detector = DuplicateDetector()
    for r in results:
        detector.add(r.metadata)
    dup_map: dict[str, list[str]] = {}
    for r in results:
        dups = detector.find_duplicates(r.metadata)
        if dups:
            dup_map[r.metadata.file_path] = dups
    return dup_map


def apply_duplicate_detection(
    results: list[AuditResult],
    mark_as_review: bool = True,
) -> None:
    if not results:
        return
    dup_map = detect_duplicates(results)
    for r in results:
        path = r.metadata.file_path
        dups = dup_map.get(path, [])
        if dups:
            r.is_duplicate = True
            r.duplicate_of = dups[0]
            issue = Issue(
                code="DUP_FILE",
                message=f"检测到重复素材，共 {len(dups) + 1} 个相同文件，例如：{Path(dups[0]).name}",
                severity=IssueSeverity.WARNING,
                field="file_hash",
                details={"duplicates": dups},
            )
            r.issues.append(issue)
            r.score = max(0.0, r.score - 15)
            if mark_as_review and r.status == AuditStatus.PASS:
                r.status = AuditStatus.REVIEW
            if "DUP_FILE" not in r.matched_rules:
                r.matched_rules.append("DUP_FILE")


def check_required_fields(md: MediaMetadata) -> list[Issue]:
    issues: list[Issue] = []
    required = REQUIRED_FIELDS_BY_TYPE.get(md.media_type, [])
    missing: list[str] = []
    for field in required:
        value = getattr(md, field, None)
        if value is None or value == "" or value == 0:
            if field in ("codec", "text_encoding") and value == "":
                missing.append(field)
            elif field not in ("codec", "text_encoding") and value in (None, 0):
                missing.append(field)
    if missing:
        issues.append(Issue(
            code="MISSING_FIELDS",
            message=f"缺少必要字段：{', '.join(missing)}",
            severity=IssueSeverity.ERROR,
            field=",".join(missing),
            details={"missing_fields": missing},
        ))
    return issues


def check_naming_convention(
    md: MediaMetadata,
    custom_pattern: str | None = None,
    allow_spaces: bool = False,
    allow_unicode: bool = True,
    max_length: int = 200,
) -> list[Issue]:
    issues: list[Issue] = []
    name = Path(md.file_name).stem
    full_name = md.file_name

    if len(full_name) > max_length:
        issues.append(Issue(
            code="NAME_TOO_LONG",
            message=f"文件名过长（{len(full_name)} > {max_length}）",
            severity=IssueSeverity.WARNING,
            field="file_name",
            details={"length": len(full_name), "max_length": max_length},
        ))

    if not allow_spaces and (" " in full_name or "\t" in full_name):
        issues.append(Issue(
            code="NAME_HAS_SPACES",
            message="文件名包含空格",
            severity=IssueSeverity.WARNING,
            field="file_name",
        ))

    if SPECIAL_CHARS.search(full_name):
        bad_chars = set(SPECIAL_CHARS.findall(full_name))
        issues.append(Issue(
            code="NAME_SPECIAL_CHARS",
            message=f"文件名包含特殊字符：{''.join(sorted(bad_chars))}",
            severity=IssueSeverity.ERROR,
            field="file_name",
            details={"special_chars": list(bad_chars)},
        ))

    pattern = re.compile(custom_pattern) if custom_pattern else NAMING_PATTERN
    if not allow_unicode and not custom_pattern:
        pattern = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9]+)?$")
    if full_name and not pattern.match(full_name):
        if not custom_pattern:
            pass
        else:
            issues.append(Issue(
                code="NAME_PATTERN_MISMATCH",
                message=f"文件名不符合命名规则模式：{custom_pattern}",
                severity=IssueSeverity.WARNING,
                field="file_name",
                details={"pattern": custom_pattern},
            ))

    if name.startswith((".", "_")):
        issues.append(Issue(
            code="NAME_HIDDEN_PREFIX",
            message="文件名为隐藏文件前缀开头（. 或 _）",
            severity=IssueSeverity.INFO,
            field="file_name",
        ))

    return issues


def check_encoding_issues(md: MediaMetadata) -> list[Issue]:
    issues: list[Issue] = []
    if md.media_type != MediaType.TEXT:
        return issues

    if not md.text_encoding:
        issues.append(Issue(
            code="ENC_UNKNOWN",
            message="无法识别文本编码",
            severity=IssueSeverity.ERROR,
            field="text_encoding",
        ))
    elif md.text_encoding.lower() in ("gbk", "gb2312", "gb18030"):
        issues.append(Issue(
            code="ENC_NON_UTF8",
            message=f"文本编码为 {md.text_encoding}，建议转换为 UTF-8",
            severity=IssueSeverity.INFO,
            field="text_encoding",
            details={"encoding": md.text_encoding},
        ))
    elif md.text_encoding.lower() == "utf-8-sig":
        issues.append(Issue(
            code="ENC_UTF8_BOM",
            message="UTF-8 文件包含 BOM，建议移除",
            severity=IssueSeverity.WARNING,
            field="text_encoding",
        ))

    return issues


def check_codec_issues(md: MediaMetadata) -> list[Issue]:
    issues: list[Issue] = []
    if md.media_type not in (MediaType.VIDEO, MediaType.AUDIO):
        return issues

    unsupported_video = {"FLV", "RealVideo", "WMV"}
    if md.media_type == MediaType.VIDEO and md.codec in unsupported_video:
        issues.append(Issue(
            code="CODEC_UNSUPPORTED",
            message=f"视频编码 {md.codec} 属于老旧/非标准格式，建议转码为 H.264/H.265",
            severity=IssueSeverity.WARNING,
            field="codec",
            details={"codec": md.codec},
        ))

    if md.media_type == MediaType.VIDEO and md.bitrate:
        if md.height and md.height >= 1080 and md.bitrate < 4_000_000:
            issues.append(Issue(
                code="BITRATE_LOW",
                message=f"1080p+ 视频码率偏低（{md.bitrate // 1000}kbps），建议 ≥4000kbps",
                severity=IssueSeverity.INFO,
                field="bitrate",
            ))
        elif md.height and 720 <= md.height < 1080 and md.bitrate < 2_000_000:
            issues.append(Issue(
                code="BITRATE_LOW",
                message=f"720p 视频码率偏低（{md.bitrate // 1000}kbps），建议 ≥2000kbps",
                severity=IssueSeverity.INFO,
                field="bitrate",
            ))

    return issues


def run_all_validations(
    results: list[AuditResult],
    naming_pattern: str | None = None,
    allow_spaces: bool = False,
) -> None:
    apply_duplicate_detection(results)

    for r in results:
        md = r.metadata
        new_issues: list[Issue] = []
        new_issues.extend(check_required_fields(md))
        new_issues.extend(check_naming_convention(md, custom_pattern=naming_pattern, allow_spaces=allow_spaces))
        new_issues.extend(check_encoding_issues(md))
        new_issues.extend(check_codec_issues(md))

        if new_issues:
            r.issues.extend(new_issues)
            for issue in new_issues:
                if issue.code not in r.matched_rules:
                    r.matched_rules.append(issue.code)
                if issue.severity in (IssueSeverity.ERROR, IssueSeverity.CRITICAL):
                    r.score = max(0.0, r.score - 10)
                    if r.status == AuditStatus.PASS:
                        r.status = AuditStatus.REVIEW
                elif issue.severity == IssueSeverity.WARNING:
                    r.score = max(0.0, r.score - 5)
                    if r.status == AuditStatus.PASS:
                        r.status = AuditStatus.REVIEW
