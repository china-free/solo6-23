"""数据模型定义"""
from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


class MediaType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"
    ARCHIVE = "archive"
    UNKNOWN = "unknown"


class AuditStatus(str, Enum):
    PASS = "pass"
    REVIEW = "review"
    REJECT = "reject"


class IssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class IssueCategory(str, Enum):
    CONTENT = "content"
    FORMAT = "format"
    NAMING = "naming"
    METADATA_MISSING = "metadata_missing"
    TOOL_LIMIT = "tool_limit"
    DUPLICATE = "duplicate"
    FILE_ERROR = "file_error"
    OTHER = "other"


ISSUE_CATEGORY_LABELS = {
    IssueCategory.CONTENT: ("内容质量", "📏", "yellow"),
    IssueCategory.FORMAT: ("格式编码", "🎞", "magenta"),
    IssueCategory.NAMING: ("命名规范", "🏷", "blue"),
    IssueCategory.METADATA_MISSING: ("元数据缺失", "🔍", "cyan"),
    IssueCategory.TOOL_LIMIT: ("工具限制", "🛠", "bright_black"),
    IssueCategory.DUPLICATE: ("重复素材", "🔁", "magenta"),
    IssueCategory.FILE_ERROR: ("文件错误", "💥", "red"),
    IssueCategory.OTHER: ("其他", "📋", "white"),
}


ISSUE_CATEGORY_ACTIONS = {
    IssueCategory.CONTENT: "根据业务标准评估是否接受，或重新制作/裁剪素材",
    IssueCategory.FORMAT: "转码为推荐格式（如 JPG/PNG/WebP、H.264 MP4、FLAC/WAV、UTF-8）",
    IssueCategory.NAMING: "按命名规范重命名后重新提交",
    IssueCategory.METADATA_MISSING: "补充素材元数据，或使用标准工具重新导出",
    IssueCategory.TOOL_LIMIT: "人工复核内容是否合规（工具无法自动判定）",
    IssueCategory.DUPLICATE: "去重后保留一份正式版本再提审",
    IssueCategory.FILE_ERROR: "修复或替换损坏/空文件",
    IssueCategory.OTHER: "人工确认处理方式",
}


class Issue(BaseModel):
    code: str
    message: str
    severity: IssueSeverity = IssueSeverity.WARNING
    category: IssueCategory = IssueCategory.OTHER
    field: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)


class MediaMetadata(BaseModel):
    file_path: str
    file_name: str
    file_size: int
    media_type: MediaType = MediaType.UNKNOWN
    mime_type: str = ""
    file_hash: str = ""
    created_at: Optional[datetime] = None
    modified_at: Optional[datetime] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration_seconds: Optional[float] = None
    codec: str = ""
    bitrate: Optional[int] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    text_length: Optional[int] = None
    text_encoding: str = ""
    frame_rate: Optional[float] = None
    color_mode: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


class AuditResult(BaseModel):
    metadata: MediaMetadata
    status: AuditStatus = AuditStatus.REVIEW
    score: float = 0.0
    issues: list[Issue] = Field(default_factory=list)
    matched_rules: list[str] = Field(default_factory=list)
    is_duplicate: bool = False
    duplicate_of: Optional[str] = None
    audit_time: datetime = Field(default_factory=datetime.now)

    @property
    def has_errors(self) -> bool:
        return any(i.severity in (IssueSeverity.ERROR, IssueSeverity.CRITICAL) for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.severity == IssueSeverity.WARNING for i in self.issues)


class AuditSummary(BaseModel):
    total: int = 0
    passed: int = 0
    review: int = 0
    rejected: int = 0
    duplicates: int = 0
    issues_by_severity: dict[str, int] = Field(default_factory=dict)
    issues_by_code: dict[str, int] = Field(default_factory=dict)
    duration_seconds: float = 0.0


def compute_file_hash(file_path: str | Path, chunk_size: int = 8192) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()
