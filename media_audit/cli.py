"""命令行入口 CLI"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from . import __version__
from .models import AuditResult, AuditStatus, MediaType
from .output import build_summary, export_csv, export_json, render_console
from .pipeline import AuditPipeline


console = Console()


def _make_pipeline(
    rules_config: Optional[str],
    no_hash: bool,
    naming_pattern: Optional[str],
    allow_spaces: bool,
    no_extract: bool,
    no_recursive: bool,
    extensions: Optional[str],
    skip_meta: bool,
) -> AuditPipeline:
    ext_list = None
    if extensions:
        ext_list = [e.strip() for e in extensions.split(",") if e.strip()]
    return AuditPipeline(
        rules_config=rules_config,
        compute_hash=not no_hash,
        naming_pattern=naming_pattern,
        allow_spaces=allow_spaces,
        extract_archives=not no_extract,
        recursive=not no_recursive,
        extensions=ext_list,
        skip_metadata=skip_meta,
    )


@click.group(invoke_without_command=True)
@click.version_option(__version__, "-V", "--version", prog_name="media-audit")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """素材审核命令行工具 - 从本地目录或压缩包读取素材，自动识别类型，抽取元数据，按规则判断需要人工复核的素材。"""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command("audit")
@click.argument(
    "input_path",
    type=click.Path(exists=True, path_type=Path),
)
@click.option(
    "-r", "--rules", "rules_config",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="自定义审核规则配置文件 (JSON)",
)
@click.option(
    "--no-hash",
    is_flag=True,
    help="跳过文件哈希计算（可加速处理，但无法检测重复素材）",
)
@click.option(
    "--naming-pattern",
    type=str,
    help="文件命名正则匹配模式，例如 '^[a-z0-9_-]+\\.[a-z]+$'",
)
@click.option(
    "--allow-spaces",
    is_flag=True,
    help="允许文件名包含空格",
)
@click.option(
    "--no-extract",
    is_flag=True,
    help="不解压压缩包，只扫描目录中的直接文件",
)
@click.option(
    "--no-recursive",
    is_flag=True,
    help="不递归扫描子目录",
)
@click.option(
    "-e", "--extensions",
    type=str,
    help="限制扫描的文件扩展名，用逗号分隔，例如 'jpg,png,mp4'",
)
@click.option(
    "--skip-meta",
    is_flag=True,
    help="跳过深度元数据解析，只做类型识别和基础检查",
)
@click.option(
    "-o", "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    help="输出结果文件路径，根据扩展名自动选择格式 (.json / .csv)",
)
@click.option(
    "--json", "json_output",
    type=click.Path(dir_okay=False, path_type=Path),
    help="导出 JSON 文件",
)
@click.option(
    "--csv", "csv_output",
    type=click.Path(dir_okay=False, path_type=Path),
    help="导出 CSV 文件",
)
@click.option(
    "-q", "--quiet",
    is_flag=True,
    help="不输出终端表格，仅显示汇总",
)
@click.option(
    "-v", "--verbose",
    is_flag=True,
    help="显示详细信息（编码格式、完整路径等）",
)
@click.option(
    "--no-progress",
    is_flag=True,
    help="不显示处理进度条",
)
@click.option(
    "--filter-status",
    type=click.Choice(["pass", "review", "reject"], case_sensitive=False),
    help="仅输出指定状态的素材到导出文件",
)
@click.option(
    "--fail-on-review",
    is_flag=True,
    help="存在需要复核的素材时，以非零退出码结束",
)
def audit_cmd(
    input_path: Path,
    rules_config: Optional[Path],
    no_hash: bool,
    naming_pattern: Optional[str],
    allow_spaces: bool,
    no_extract: bool,
    no_recursive: bool,
    extensions: Optional[str],
    skip_meta: bool,
    output: Optional[Path],
    json_output: Optional[Path],
    csv_output: Optional[Path],
    quiet: bool,
    verbose: bool,
    no_progress: bool,
    filter_status: Optional[str],
    fail_on_review: bool,
) -> None:
    """对 INPUT_PATH（目录或压缩包）执行素材审核。

    INPUT_PATH 可以是单个文件、目录或压缩包（zip/tar 等）。
    """
    pipeline = _make_pipeline(
        rules_config=rules_config,
        no_hash=no_hash,
        naming_pattern=naming_pattern,
        allow_spaces=allow_spaces,
        no_extract=no_extract,
        no_recursive=no_recursive,
        extensions=extensions,
        skip_meta=skip_meta,
    )

    results: list[AuditResult] = []
    summary = None

    if not no_progress:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("({task.completed}/{task.total})"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as progress:
            task_id = progress.add_task("正在扫描素材...", total=None)

            def _progress_cb(done: int, total: int, _path: Path) -> None:
                if progress.tasks[task_id].total is None and total > 0:
                    progress.update(task_id, total=total, description="处理素材")
                progress.update(task_id, completed=done, description=f"处理: {_path.name[:40]}")

            results, summary = pipeline.scan_with_summary(input_path, progress_callback=_progress_cb)
            progress.update(task_id, completed=progress.tasks[task_id].total or 0, description="完成")
    else:
        results, summary = pipeline.scan_with_summary(input_path)

    filtered_results = results
    if filter_status:
        target_status = AuditStatus(filter_status.lower())
        filtered_results = [r for r in results if r.status == target_status]

    if not quiet:
        render_console(filtered_results if filter_status else results, summary, verbose=verbose)
    else:
        _print_mini_summary(summary)

    exported: list[str] = []
    try:
        if output:
            suffix = output.suffix.lower()
            if suffix == ".json":
                export_json(filtered_results, output, summary=summary)
                exported.append(f"JSON: {output}")
            elif suffix == ".csv":
                export_csv(filtered_results, output)
                exported.append(f"CSV: {output}")
            else:
                console.print(f"[yellow]⚠ 无法识别输出格式: {output.suffix}，请使用 --json 或 --csv[/yellow]")

        if json_output:
            export_json(filtered_results, json_output, summary=summary)
            exported.append(f"JSON: {json_output}")
        if csv_output:
            export_csv(filtered_results, csv_output)
            exported.append(f"CSV: {csv_output}")
    except OSError as e:
        console.print(f"[red]✗ 导出文件失败: {e}[/red]")
        sys.exit(2)

    if exported:
        console.print("\n[green]✓ 已导出:[/green]")
        for ex in exported:
            console.print(f"  → {ex}")

    pipeline.cleanup()

    exit_code = 0
    if summary and summary.rejected > 0:
        exit_code = 1
    elif fail_on_review and summary and summary.review > 0:
        exit_code = 3
    if exit_code != 0:
        sys.exit(exit_code)


@cli.command("list-rules")
@click.option(
    "-r", "--rules", "rules_config",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="加载自定义规则配置进行展示",
)
def list_rules_cmd(rules_config: Optional[Path]) -> None:
    """列出当前生效的审核规则。"""
    from .rules import build_default_rules, load_rules_from_config

    rules = load_rules_from_config(rules_config) if rules_config else build_default_rules()
    console.print(f"[bold]共 {len(rules)} 条审核规则:[/bold]\n")
    for idx, r in enumerate(rules, 1):
        status = "✅" if r.enabled else "⛔"
        action_style = {"pass": "green", "review": "yellow", "reject": "red"}.get(r.action.value, "white")
        action_open = "[" + action_style + "]"
        action_close = "[/" + action_style + "]"
        console.print(
            f"  {idx:2d}. {status} [bold cyan]{r.id}[/bold cyan] "
            f"({action_open}{r.action.value.upper()}{action_close})"
        )
        console.print(f"      名称: {r.name}")
        if r.description:
            console.print(f"      说明: {r.description}")
        conditions_str = []
        for c in r.conditions:
            mt_str = ""
            if c.media_types:
                mts = ", ".join(t.value for t in c.media_types)
                mt_str = f" [仅 {mts}]"
            conditions_str.append(f"  • {c.field} {c.operator} {c.value}{mt_str}")
        if conditions_str:
            logic = " AND " if r.condition_logic == "AND" else " OR  "
            console.print(f"      条件 ({r.condition_logic}):")
            for cs in conditions_str:
                console.print(f"      {cs}")
        console.print()


@cli.command("inspect")
@click.argument(
    "file_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--no-hash",
    is_flag=True,
    help="跳过文件哈希计算",
)
def inspect_cmd(file_path: Path, no_hash: bool) -> None:
    """详细检查单个文件，展示其完整元数据信息。"""
    from .metadata import extract_full_metadata
    from .type_detector import detect_media_type

    with console.status(f"正在分析 {file_path.name} ..."):
        md = extract_full_metadata(file_path, compute_hash=not no_hash)
        mt, mime = detect_media_type(file_path)

    from rich.table import Table
    from rich import box

    table = Table(title=f"文件详细信息: {file_path.name}", box=box.ROUNDED, show_header=False)
    table.add_column("字段", style="bold cyan", width=20)
    table.add_column("值", style="white")

    rows = [
        ("完整路径", str(file_path.resolve())),
        ("文件大小", f"{md.file_size:,} bytes ({_fmt_size(md.file_size)})"),
        ("素材类型", f"{mt.value} ({_type_icon(mt)})"),
        ("MIME 类型", mime or "-"),
        ("创建时间", md.created_at.strftime("%Y-%m-%d %H:%M:%S") if md.created_at else "-"),
        ("修改时间", md.modified_at.strftime("%Y-%m-%d %H:%M:%S") if md.modified_at else "-"),
        ("文件哈希 (SHA256)", md.file_hash[:64] + "..." if md.file_hash else "(未计算)"),
        ("宽度 (px)", str(md.width) if md.width else "-"),
        ("高度 (px)", str(md.height) if md.height else "-"),
        ("尺寸", f"{md.width}×{md.height}" if md.width and md.height else "-"),
        ("色彩模式", md.color_mode or "-"),
        ("时长", _fmt_dur(md.duration_seconds)),
        ("编码格式", md.codec or "-"),
        ("码率 (bps)", f"{md.bitrate:,}" if md.bitrate else "-"),
        ("采样率 (Hz)", str(md.sample_rate) if md.sample_rate else "-"),
        ("声道数", str(md.channels) if md.channels else "-"),
        ("帧率 (fps)", f"{md.frame_rate:.2f}" if md.frame_rate else "-"),
        ("文本编码", md.text_encoding or "-"),
        ("文本长度 (字符)", str(md.text_length) if md.text_length else "-"),
    ]
    for k, v in rows:
        table.add_row(k, str(v))

    if md.extra:
        from rich.json import JSON
        import json
        table.add_row("额外信息", "")
        table.add_row("", JSON(json.dumps(md.extra, ensure_ascii=False, default=str)))

    console.print(table)


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for u in ["KB", "MB", "GB", "TB"]:
        n /= 1024
        if n < 1024:
            return f"{n:.2f} {u}"
    return f"{n:.2f} PB"


def _fmt_dur(s: Optional[float]) -> str:
    if not s:
        return "-"
    if s < 60:
        return f"{s:.2f} 秒"
    m, sec = divmod(int(s), 60)
    if m < 60:
        return f"{m}分{sec:02d}秒"
    h, m = divmod(m, 60)
    return f"{h}时{m:02d}分{sec:02d}秒"


def _type_icon(mt: MediaType) -> str:
    return {
        MediaType.IMAGE: "🖼 图片",
        MediaType.VIDEO: "🎬 视频",
        MediaType.AUDIO: "🎵 音频",
        MediaType.TEXT: "📄 文本",
        MediaType.ARCHIVE: "📦 压缩包",
        MediaType.UNKNOWN: "❓ 未知",
    }.get(mt, mt.value)


def _print_mini_summary(summary) -> None:
    if not summary:
        return
    console.print(
        f"[bold]完成:[/bold] 共 {summary.total} 个素材 | "
        f"[green]通过 {summary.passed}[/green] | "
        f"[yellow]复核 {summary.review}[/yellow] | "
        f"[red]拒绝 {summary.rejected}[/red] | "
        f"[magenta]重复 {summary.duplicates}[/magenta] | "
        f"耗时 {summary.duration_seconds:.2f}s"
    )


def main() -> None:
    try:
        cli(standalone_mode=True)
    except KeyboardInterrupt:
        console.print("\n[yellow]⚠ 用户中断[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()
