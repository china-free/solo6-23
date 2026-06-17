"""输出模块 - 终端表格 / CSV / JSON 导出"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Iterable, Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from .models import (
    AuditResult,
    AuditStatus,
    AuditSummary,
    IssueSeverity,
    MediaMetadata,
    MediaType,
)


def _format_size(size_bytes: int) -> str:
    if size_bytes is None or size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)
    while size >= 1024.0 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    return f"{size:.1f} {units[i]}"


def _format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def _format_dimensions(md: MediaMetadata) -> str:
    if md.width and md.height:
        return f"{md.width}×{md.height}"
    return "-"


def _status_style(status: AuditStatus) -> str:
    return {
        AuditStatus.PASS: "bold green",
        AuditStatus.REVIEW: "bold yellow",
        AuditStatus.REJECT: "bold red",
    }.get(status, "white")


def _type_label(mt: MediaType) -> str:
    return {
        MediaType.IMAGE: "🖼 图片",
        MediaType.VIDEO: "🎬 视频",
        MediaType.AUDIO: "🎵 音频",
        MediaType.TEXT: "📄 文本",
        MediaType.ARCHIVE: "📦 压缩包",
        MediaType.UNKNOWN: "❓ 未知",
    }.get(mt, mt.value)


def _severity_style(sev: IssueSeverity) -> str:
    return {
        IssueSeverity.INFO: "cyan",
        IssueSeverity.WARNING: "yellow",
        IssueSeverity.ERROR: "red",
        IssueSeverity.CRITICAL: "bold red",
    }.get(sev, "white")


def build_summary(results: list[AuditResult], duration: float = 0.0) -> AuditSummary:
    summary = AuditSummary(duration_seconds=duration)
    for r in results:
        summary.total += 1
        if r.status == AuditStatus.PASS:
            summary.passed += 1
        elif r.status == AuditStatus.REVIEW:
            summary.review += 1
        elif r.status == AuditStatus.REJECT:
            summary.rejected += 1
        if r.is_duplicate:
            summary.duplicates += 1
        for issue in r.issues:
            sev_key = issue.severity.value
            summary.issues_by_severity[sev_key] = summary.issues_by_severity.get(sev_key, 0) + 1
            code_key = issue.code
            summary.issues_by_code[code_key] = summary.issues_by_code.get(code_key, 0) + 1
    return summary


def render_console(
    results: list[AuditResult],
    summary: Optional[AuditSummary] = None,
    verbose: bool = False,
    show_issues: bool = True,
) -> None:
    console = Console()

    if summary is None:
        summary = build_summary(results)

    _render_summary_panel(console, summary)
    _render_results_table(console, results, verbose=verbose)

    if show_issues and any(r.issues for r in results):
        _render_issues_table(console, results)


def _render_summary_panel(console: Console, summary: AuditSummary) -> None:
    total = max(summary.total, 1)
    pass_rate = summary.passed / total * 100
    review_rate = summary.review / total * 100
    reject_rate = summary.rejected / total * 100

    stats_text = Text()
    stats_text.append("📊 审核统计\n\n", style="bold blue underline")
    stats_text.append(f"总素材数:  {summary.total}\n", style="white")
    stats_text.append(f"✅ 通过:    {summary.passed} ({pass_rate:.1f}%)\n", style="green")
    stats_text.append(f"⚠️  复核:   {summary.review} ({review_rate:.1f}%)\n", style="yellow")
    stats_text.append(f"❌ 拒绝:    {summary.rejected} ({reject_rate:.1f}%)\n", style="red")
    stats_text.append(f"🔁 重复素材: {summary.duplicates}\n", style="magenta")

    if summary.issues_by_severity:
        stats_text.append(f"\n问题严重度分布:\n", style="bold cyan")
        for sev, cnt in sorted(summary.issues_by_severity.items()):
            style = _severity_style(IssueSeverity(sev))
            stats_text.append(f"  {sev.upper():10s}: {cnt}\n", style=style)

    stats_text.append(f"\n⏱  耗时: {summary.duration_seconds:.2f}s", style="dim")

    console.print(Panel(stats_text, title="[bold]素材审核结果汇总", border_style="blue"))


def _render_results_table(
    console: Console,
    results: list[AuditResult],
    verbose: bool = False,
) -> None:
    table = Table(
        title="素材清单",
        box=box.ROUNDED,
        header_style="bold cyan",
        show_lines=False,
    )

    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("文件名", style="white", overflow="fold")
    table.add_column("类型", justify="center")
    table.add_column("大小", justify="right")
    table.add_column("规格", justify="right")
    table.add_column("时长", justify="right")
    table.add_column("状态", justify="center")
    table.add_column("评分", justify="right")
    if verbose:
        table.add_column("编码/格式", style="dim")
        table.add_column("路径", style="dim", overflow="fold")

    for idx, r in enumerate(results, 1):
        md = r.metadata
        dim_str = _format_dimensions(md)
        dur_str = _format_duration(md.duration_seconds)
        status_style = _status_style(r.status)
        score_style = "green" if r.score >= 80 else ("yellow" if r.score >= 50 else "red")

        row = [
            str(idx),
            md.file_name,
            _type_label(md.media_type),
            _format_size(md.file_size),
            dim_str,
            dur_str,
            Text(r.status.value.upper(), style=status_style),
            Text(f"{r.score:.0f}", style=score_style),
        ]
        if verbose:
            codecs = []
            if md.codec:
                codecs.append(md.codec)
            if md.text_encoding:
                codecs.append(md.text_encoding)
            row.append(" / ".join(codecs) or "-")
            row.append(md.file_path)

        table.add_row(*row)

    console.print(table)


def _render_issues_table(console: Console, results: list[AuditResult]) -> None:
    table = Table(
        title="问题明细",
        box=box.ROUNDED,
        header_style="bold red",
        show_lines=True,
    )

    table.add_column("文件", style="white", overflow="fold")
    table.add_column("代码", style="bold")
    table.add_column("严重度", justify="center")
    table.add_column("问题描述", overflow="fold")

    for r in results:
        if not r.issues:
            continue
        name = r.metadata.file_name
        for issue in r.issues:
            table.add_row(
                name,
                issue.code,
                Text(issue.severity.value.upper(), style=_severity_style(issue.severity)),
                issue.message,
            )
            name = "↳"

    console.print(table)


def export_json(
    results: list[AuditResult],
    output_path: str | Path,
    summary: Optional[AuditSummary] = None,
    pretty: bool = True,
) -> None:
    if summary is None:
        summary = build_summary(results)
    data = {
        "summary": json.loads(summary.model_dump_json()),
        "results": [json.loads(r.model_dump_json()) for r in results],
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if pretty:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        else:
            json.dump(data, f, ensure_ascii=False, default=str)


def export_csv(
    results: list[AuditResult],
    output_path: str | Path,
    include_issues: bool = True,
) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for r in results:
        md = r.metadata
        row = {
            "file_path": md.file_path,
            "file_name": md.file_name,
            "file_size": md.file_size,
            "file_size_human": _format_size(md.file_size),
            "media_type": md.media_type.value,
            "mime_type": md.mime_type,
            "width": md.width or "",
            "height": md.height or "",
            "dimensions": _format_dimensions(md),
            "duration_seconds": md.duration_seconds or "",
            "duration_human": _format_duration(md.duration_seconds),
            "codec": md.codec,
            "bitrate": md.bitrate or "",
            "sample_rate": md.sample_rate or "",
            "channels": md.channels or "",
            "text_length": md.text_length or "",
            "text_encoding": md.text_encoding,
            "frame_rate": md.frame_rate or "",
            "color_mode": md.color_mode,
            "file_hash": md.file_hash,
            "status": r.status.value,
            "score": f"{r.score:.1f}",
            "is_duplicate": "yes" if r.is_duplicate else "no",
            "duplicate_of": r.duplicate_of or "",
            "matched_rules": "|".join(r.matched_rules),
            "audit_time": r.audit_time.isoformat() if r.audit_time else "",
        }
        if include_issues:
            issue_codes = [i.code for i in r.issues]
            issue_messages = [i.message for i in r.issues]
            issue_severities = [i.severity.value for i in r.issues]
            row["issue_count"] = len(r.issues)
            row["issue_codes"] = "|".join(issue_codes)
            row["issue_messages"] = " || ".join(issue_messages)
            row["issue_max_severity"] = max(
                (i.severity.value for i in r.issues),
                default="",
            )
        rows.append(row)

    if not rows:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write("")
        return

    fieldnames = list(rows[0].keys())
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def to_json_string(results: list[AuditResult], summary: Optional[AuditSummary] = None) -> str:
    if summary is None:
        summary = build_summary(results)
    data = {
        "summary": json.loads(summary.model_dump_json()),
        "results": [json.loads(r.model_dump_json()) for r in results],
    }
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)
