"""规则引擎 - 自定义审核规则判断"""
from __future__ import annotations

import operator
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .models import (
    AuditResult,
    AuditStatus,
    Issue,
    IssueCategory,
    IssueSeverity,
    MediaMetadata,
    MediaType,
)


@dataclass
class RuleCondition:
    field: str
    operator: str
    value: Any
    media_types: Optional[list[MediaType]] = None

    def match(self, md: MediaMetadata) -> bool:
        if self.media_types and md.media_type not in self.media_types:
            return False
        actual = getattr(md, self.field, None)
        if actual is None and self.field in md.extra:
            actual = md.extra[self.field]
        if actual is None:
            return False
        return _compare(actual, self.operator, self.value)


@dataclass
class AuditRule:
    id: str
    name: str
    description: str = ""
    conditions: list[RuleCondition] = field(default_factory=list)
    condition_logic: str = "AND"
    action: AuditStatus = AuditStatus.REVIEW
    issue_code: str = ""
    issue_message: str = ""
    severity: IssueSeverity = IssueSeverity.WARNING
    category: IssueCategory = IssueCategory.OTHER
    score_delta: float = 0.0
    enabled: bool = True

    def evaluate(self, md: MediaMetadata) -> Optional[Issue]:
        if not self.enabled or not self.conditions:
            return None
        results = [c.match(md) for c in self.conditions]
        matched = all(results) if self.condition_logic == "AND" else any(results)
        if not matched:
            return None
        return Issue(
            code=self.issue_code or self.id,
            message=self.issue_message or self.name,
            severity=self.severity,
            category=self.category,
            details={"rule_id": self.id},
        )


_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
    "contains": lambda a, b: b in a,
    "not_contains": lambda a, b: b not in a,
    "startswith": lambda a, b: str(a).startswith(str(b)),
    "endswith": lambda a, b: str(a).endswith(str(b)),
    "regex": lambda a, b: bool(re.search(str(b), str(a))),
    "is_none": lambda a, _: a is None,
    "not_none": lambda a, _: a is not None,
    "empty": lambda a, _: not a,
    "not_empty": lambda a, _: bool(a),
}


def _compare(a: Any, op: str, b: Any) -> bool:
    func = _OPERATORS.get(op)
    if func is None:
        raise ValueError(f"Unknown operator: {op}")
    try:
        return func(a, b)
    except (TypeError, ValueError):
        return False


def build_default_rules() -> list[AuditRule]:
    rules: list[AuditRule] = []

    rules.append(AuditRule(
        id="IMG_SMALL",
        name="图片分辨率过低",
        description="图片宽度或高度小于 720px，可能影响展示效果",
        conditions=[
            RuleCondition("width", "<", 720, [MediaType.IMAGE]),
        ],
        action=AuditStatus.REVIEW,
        issue_message="图片分辨率过低（宽<720px）",
        severity=IssueSeverity.WARNING,
        score_delta=-10,
    ))

    rules.append(AuditRule(
        id="IMG_LARGE",
        name="图片文件过大",
        description="图片文件超过 10MB，可能影响加载速度",
        conditions=[
            RuleCondition("file_size", ">", 10 * 1024 * 1024, [MediaType.IMAGE]),
        ],
        action=AuditStatus.REVIEW,
        issue_message="图片文件超过 10MB",
        severity=IssueSeverity.WARNING,
        score_delta=-5,
    ))

    rules.append(AuditRule(
        id="IMG_UNSUPPORTED",
        name="图片格式不推荐",
        description="建议使用 JPG/PNG/WebP 格式",
        conditions=[
            RuleCondition("codec", "not_in", ["JPEG", "PNG", "WEBP", "JPG", ""], [MediaType.IMAGE]),
        ],
        action=AuditStatus.REVIEW,
        issue_message="图片格式非推荐格式（JPG/PNG/WebP）",
        severity=IssueSeverity.INFO,
        score_delta=-3,
    ))

    rules.append(AuditRule(
        id="VID_SHORT",
        name="视频时长过短",
        description="视频时长小于 3 秒",
        conditions=[
            RuleCondition("duration_seconds", "<", 3, [MediaType.VIDEO]),
        ],
        action=AuditStatus.REVIEW,
        issue_message="视频时长小于 3 秒",
        severity=IssueSeverity.WARNING,
        score_delta=-10,
    ))

    rules.append(AuditRule(
        id="VID_LONG",
        name="视频时长过长",
        description="视频时长超过 30 分钟，建议分段",
        conditions=[
            RuleCondition("duration_seconds", ">", 30 * 60, [MediaType.VIDEO]),
        ],
        action=AuditStatus.REVIEW,
        issue_message="视频时长超过 30 分钟",
        severity=IssueSeverity.INFO,
        score_delta=-3,
    ))

    rules.append(AuditRule(
        id="VID_SD",
        name="视频清晰度不足",
        description="视频高度小于 720px，非高清",
        conditions=[
            RuleCondition("height", "<", 720, [MediaType.VIDEO]),
        ],
        action=AuditStatus.REVIEW,
        issue_message="视频清晰度不足（高<720px）",
        severity=IssueSeverity.WARNING,
        score_delta=-8,
    ))

    rules.append(AuditRule(
        id="AUD_SHORT",
        name="音频时长过短",
        description="音频时长小于 1 秒",
        conditions=[
            RuleCondition("duration_seconds", "<", 1, [MediaType.AUDIO]),
        ],
        action=AuditStatus.REVIEW,
        issue_message="音频时长小于 1 秒",
        severity=IssueSeverity.WARNING,
        score_delta=-8,
    ))

    rules.append(AuditRule(
        id="AUD_LOSSY",
        name="音频为有损编码",
        description="建议使用无损格式（FLAC/WAV）用于高质量场景",
        conditions=[
            RuleCondition("codec", "in", ["MP3", "AAC", "WMA", "Vorbis"], [MediaType.AUDIO]),
        ],
        action=AuditStatus.REVIEW,
        issue_message="音频使用有损编码格式",
        severity=IssueSeverity.INFO,
        score_delta=-2,
    ))

    rules.append(AuditRule(
        id="TXT_SHORT",
        name="文本内容过短",
        description="文本字符数少于 10 个",
        conditions=[
            RuleCondition("text_length", "<", 10, [MediaType.TEXT]),
        ],
        action=AuditStatus.REVIEW,
        issue_message="文本内容过短（<10 字符）",
        severity=IssueSeverity.INFO,
        score_delta=-2,
    ))

    rules.append(AuditRule(
        id="TXT_LONG",
        name="文本内容过长",
        description="文本字符数超过 10 万，建议拆分",
        conditions=[
            RuleCondition("text_length", ">", 100000, [MediaType.TEXT]),
        ],
        action=AuditStatus.REVIEW,
        issue_message="文本内容过长（>10万字符）",
        severity=IssueSeverity.INFO,
        score_delta=-2,
    ))

    rules.append(AuditRule(
        id="TXT_ENC_UNKNOWN",
        name="文本编码无法识别",
        description="未检测到有效文本编码",
        conditions=[
            RuleCondition("text_encoding", "empty", value=None, media_types=[MediaType.TEXT]),
        ],
        action=AuditStatus.REVIEW,
        issue_message="文本编码无法识别",
        severity=IssueSeverity.WARNING,
        score_delta=-5,
    ))

    rules.append(AuditRule(
        id="META_SIZE_ZERO",
        name="文件大小为 0",
        description="空文件，必须人工复核",
        conditions=[
            RuleCondition("file_size", "==", 0),
        ],
        action=AuditStatus.REJECT,
        issue_message="文件大小为 0（空文件）",
        severity=IssueSeverity.CRITICAL,
        score_delta=-100,
    ))

    rules.append(AuditRule(
        id="TYPE_UNKNOWN",
        name="素材类型未知",
        description="无法识别素材类型",
        conditions=[
            RuleCondition("media_type", "==", MediaType.UNKNOWN),
        ],
        action=AuditStatus.REVIEW,
        issue_message="无法识别素材类型",
        severity=IssueSeverity.ERROR,
        score_delta=-20,
    ))

    rules.append(AuditRule(
        id="NO_DURATION",
        name="音视频缺少时长信息",
        description="音视频文件无法解析出时长",
        conditions=[
            RuleCondition("duration_seconds", "is_none", value=None, media_types=[MediaType.VIDEO, MediaType.AUDIO]),
        ],
        action=AuditStatus.REVIEW,
        issue_message="无法解析出音视频时长",
        severity=IssueSeverity.ERROR,
        score_delta=-15,
    ))

    rules.append(AuditRule(
        id="NO_DIMENSIONS",
        name="图片/视频缺少尺寸信息",
        description="图片或视频无法解析出宽高",
        conditions=[
            RuleCondition("width", "is_none", value=None, media_types=[MediaType.IMAGE, MediaType.VIDEO]),
        ],
        action=AuditStatus.REVIEW,
        issue_message="无法解析出尺寸信息",
        severity=IssueSeverity.ERROR,
        score_delta=-15,
    ))

    return rules


def load_rules_from_config(config_path: str | Path) -> list[AuditRule]:
    path = Path(config_path)
    if not path.exists():
        return build_default_rules()
    import json
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_rules = json.load(f)
    except (json.JSONDecodeError, OSError):
        return build_default_rules()

    rules: list[AuditRule] = []
    for raw in raw_rules:
        conds = []
        for rc in raw.get("conditions", []):
            mts = None
            if "media_types" in rc:
                mts = [MediaType(t) for t in rc["media_types"]]
            conds.append(RuleCondition(
                field=rc["field"],
                operator=rc["operator"],
                value=rc["value"],
                media_types=mts,
            ))
        rules.append(AuditRule(
            id=raw["id"],
            name=raw.get("name", raw["id"]),
            description=raw.get("description", ""),
            conditions=conds,
            condition_logic=raw.get("condition_logic", "AND"),
            action=AuditStatus(raw.get("action", "review")),
            issue_code=raw.get("issue_code", ""),
            issue_message=raw.get("issue_message", ""),
            severity=IssueSeverity(raw.get("severity", "warning")),
            score_delta=float(raw.get("score_delta", 0.0)),
            enabled=raw.get("enabled", True),
        ))
    return rules or build_default_rules()


def apply_rules(md: MediaMetadata, rules: list[AuditRule]) -> AuditResult:
    result = AuditResult(metadata=md, score=100.0)
    matched_rule_ids: list[str] = []

    has_critical = False
    has_error = False
    has_warning = False
    force_reject = False

    for rule in rules:
        issue = rule.evaluate(md)
        if issue is None:
            continue
        result.issues.append(issue)
        matched_rule_ids.append(rule.id)
        result.score = max(0.0, result.score + rule.score_delta)
        if rule.action == AuditStatus.REJECT:
            force_reject = True
        s = issue.severity
        if s == IssueSeverity.CRITICAL:
            has_critical = True
        elif s == IssueSeverity.ERROR:
            has_error = True
        elif s == IssueSeverity.WARNING:
            has_warning = True

    result.matched_rules = matched_rule_ids

    if force_reject or has_critical:
        result.status = AuditStatus.REJECT
    elif has_error or has_warning:
        result.status = AuditStatus.REVIEW
    else:
        result.status = AuditStatus.PASS

    return result
