"""审核流水线 - 目录扫描、压缩包解压、批量处理"""
from __future__ import annotations

import bz2
import gzip
import lzma
import os
import shutil
import tempfile
import time
import tarfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Iterable, Optional

from .models import (
    AuditResult,
    AuditStatus,
    AuditSummary,
    Issue,
    IssueSeverity,
    MediaMetadata,
    MediaType,
)
from .metadata import extract_basic_metadata, extract_full_metadata
from .rules import AuditRule, apply_rules, build_default_rules, load_rules_from_config
from .type_detector import (
    ARCHIVE_EXTENSIONS,
    ArchiveSupportLevel,
    _guess_single_decompressed_name,
    detect_media_type,
    get_archive_support_level,
    is_archive,
    list_archive_contents,
)
from .validators import run_all_validations
from .output import build_summary


ALL_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif",
    ".svg", ".ico", ".heic",
    ".mp4", ".avi", ".mov", ".wmv", ".flv", ".mkv", ".webm", ".m4v", ".3gp", ".mpeg", ".mpg", ".ts",
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a", ".opus", ".ape",
    ".txt", ".md", ".markdown", ".log", ".csv", ".json", ".xml", ".html", ".htm",
    ".css", ".js", ".py", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".ts",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".rtf", ".srt", ".ass",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".tbz2",
)


def collect_files(
    path: str | Path,
    recursive: bool = True,
    include_archives: bool = True,
    extensions: Optional[Iterable[str]] = None,
) -> list[Path]:
    root = Path(path).resolve()
    collected: list[Path] = []

    allowed_exts = None
    if extensions:
        allowed_exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}

    if root.is_file():
        if _should_include(root, allowed_exts, include_archives):
            collected.append(root)
        return collected

    if not root.is_dir():
        return collected

    iterator = root.rglob("*") if recursive else root.glob("*")
    for p in iterator:
        if p.is_file() and _should_include(p, allowed_exts, include_archives):
            collected.append(p.resolve())

    return sorted(collected)


def _should_include(
    path: Path,
    allowed_exts: Optional[set[str]],
    include_archives: bool,
) -> bool:
    name_lower = path.name.lower()
    if name_lower.startswith(".") or "__MACOSX" in name_lower:
        return False

    ext = path.suffix.lower()
    name = path.name.lower()

    if not include_archives:
        for ae in ARCHIVE_EXTENSIONS:
            if name.endswith(ae):
                return False

    if allowed_exts:
        if ext in allowed_exts:
            return True
        for ae in allowed_exts:
            if name.endswith(ae):
                return True
        return False

    return True


@contextmanager
def _temp_dir(prefix: str = "media_audit_") -> Generator[Path, None, None]:
    td = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield td
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _extract_single_compressed(archive: Path, target: Path) -> Optional[Path]:
    name = _guess_single_decompressed_name(archive)
    out_path = target / name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    opener = None
    suffix = archive.suffix.lower()
    if suffix == ".gz":
        opener = gzip.open
    elif suffix == ".bz2":
        opener = bz2.open
    elif suffix == ".xz":
        opener = lzma.open
    if not opener:
        head = archive.read_bytes()[:8]
        if head.startswith(b"\x1f\x8b"):
            opener = gzip.open
        elif head.startswith(b"BZh"):
            opener = bz2.open
        elif head.startswith(b"\xfd7zXZ\x00"):
            opener = lzma.open
    if not opener:
        return None
    try:
        with opener(archive, "rb") as src, open(out_path, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
    except (OSError, EOFError, lzma.LZMAError, gzip.BadGzipFile, ValueError):
        if out_path.exists():
            out_path.unlink(missing_ok=True)
        return None
    if out_path.is_file() and out_path.stat().st_size >= 0:
        return out_path.resolve()
    return None


def extract_archive(
    archive_path: str | Path,
    extract_to: Optional[str | Path] = None,
) -> tuple[Path, list[Path], ArchiveSupportLevel, str]:
    archive = Path(archive_path).resolve()
    target = Path(extract_to).resolve() if extract_to else Path(tempfile.mkdtemp(prefix="extracted_"))
    target.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []

    level, mime = get_archive_support_level(archive)

    if level == ArchiveSupportLevel.NONE:
        note = (
            f"压缩包格式暂不支持自动解压（mime={mime or 'unknown'}），"
            "请安装对应工具后手动解压或使用 zip/tar.gz 等标准格式。"
        )
        return target, [], level, note

    if zipfile.is_zipfile(archive):
        try:
            with zipfile.ZipFile(archive) as zf:
                safe_names = []
                for name in zf.namelist():
                    clean = name.lstrip("/").lstrip("\\")
                    if ".." in clean or clean.startswith((".", "/", "\\")):
                        continue
                    resolved = (target / clean).resolve()
                    if not str(resolved).startswith(str(target.resolve())):
                        continue
                    safe_names.append(name)
                zf.extractall(target, members=safe_names)
                for name in safe_names:
                    clean = name.lstrip("/").lstrip("\\")
                    fp = target / clean
                    if fp.is_file() and not fp.name.startswith("."):
                        extracted.append(fp.resolve())
        except (zipfile.BadZipFile, OSError, RuntimeError) as e:
            return target, [], level, f"zip 解压失败: {type(e).__name__}: {e}"
        note = f"zip 解压成功，共 {len(extracted)} 个文件"
        return target, extracted, level, note

    try:
        with tarfile.open(archive) as tf:
            safe_members = []
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                clean = member.name.lstrip("/").lstrip("\\")
                if ".." in clean or clean.startswith("."):
                    continue
                resolved = (target / clean).resolve()
                if not str(resolved).startswith(str(target.resolve())):
                    continue
                member.name = clean
                safe_members.append(member)
            for member in safe_members:
                try:
                    tf.extract(member, target)
                    fp = target / member.name
                    if fp.is_file():
                        extracted.append(fp.resolve())
                except (OSError, tarfile.TarError):
                    continue
    except (tarfile.TarError, OSError):
        pass
    else:
        if extracted:
            return target, extracted, level, f"tar 解压成功，共 {len(extracted)} 个文件"

    if level == ArchiveSupportLevel.SINGLE:
        out = _extract_single_compressed(archive, target)
        if out is not None:
            return target, [out], level, f"单文件压缩解包成功 -> {out.name}"
        return target, [], level, "单文件压缩解包失败（文件损坏或格式不匹配）"

    return target, [], level, "未识别的压缩格式或解压失败"


class AuditPipeline:
    def __init__(
        self,
        rules: Optional[list[AuditRule]] = None,
        rules_config: Optional[str | Path] = None,
        compute_hash: bool = True,
        naming_pattern: Optional[str] = None,
        allow_spaces: bool = False,
        extract_archives: bool = True,
        recursive: bool = True,
        extensions: Optional[Iterable[str]] = None,
        skip_metadata: bool = False,
    ) -> None:
        if rules_config:
            self.rules = load_rules_from_config(rules_config)
        elif rules:
            self.rules = rules
        else:
            self.rules = build_default_rules()
        self.compute_hash = compute_hash
        self.naming_pattern = naming_pattern
        self.allow_spaces = allow_spaces
        self.extract_archives = extract_archives
        self.recursive = recursive
        self.extensions = list(extensions) if extensions else None
        self.skip_metadata = skip_metadata
        self._temp_dirs: list[Path] = []

    def __del__(self) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        for td in self._temp_dirs:
            shutil.rmtree(td, ignore_errors=True)
        self._temp_dirs.clear()

    def _recursive_extract(self, archive_path: Path, depth: int = 0) -> tuple[list[Path], list[Issue], str]:
        issues: list[Issue] = []
        all_extracted: list[Path] = []
        if depth > 3:
            issues.append(Issue(
                code="ARCHIVE_TOO_DEEP",
                message=f"压缩包嵌套超过 {depth} 层，停止递归解压",
                severity=IssueSeverity.WARNING,
                field="file_path",
            ))
            return all_extracted, issues, "嵌套过深"

        td, extracted, level, note = extract_archive(archive_path)
        self._temp_dirs.append(td)

        if level == ArchiveSupportLevel.NONE:
            issues.append(Issue(
                code="UNSUPPORTED_ARCHIVE",
                message=note,
                severity=IssueSeverity.WARNING,
                field="media_type",
                details={"note": note, "support_level": level.value},
            ))
            return all_extracted, issues, note

        if not extracted:
            issues.append(Issue(
                code="ARCHIVE_EMPTY",
                message=note or "压缩包解压后未得到任何可审核的素材",
                severity=IssueSeverity.WARNING,
                field="media_type",
                details={"support_level": level.value},
            ))
            return all_extracted, issues, note

        issues.append(Issue(
            code="ARCHIVE_EXTRACTED",
            message=note,
            severity=IssueSeverity.INFO,
            field="media_type",
            details={
                "support_level": level.value,
                "extracted_count": len(extracted),
            },
        ))

        pending = list(extracted)
        seen: set[str] = set()
        while pending:
            f = pending.pop(0)
            key = str(f.resolve())
            if key in seen:
                continue
            seen.add(key)
            if not f.is_file():
                continue
            emt, _ = detect_media_type(f)
            if emt == MediaType.ARCHIVE:
                sub_extracted, sub_issues, sub_note = self._recursive_extract(f, depth=depth + 1)
                issues.extend(sub_issues)
                for s in sub_extracted:
                    if str(s.resolve()) not in seen:
                        pending.append(s)
            else:
                all_extracted.append(f.resolve())

        return all_extracted, issues, note

    def scan(
        self,
        input_path: str | Path,
        progress_callback: Optional[callable] = None,
    ) -> list[AuditResult]:
        start = time.time()
        raw_files = collect_files(
            input_path,
            recursive=self.recursive,
            include_archives=True,
            extensions=self.extensions,
        )

        all_files: list[Path] = []
        archive_side_issues: dict[str, list[Issue]] = {}

        for f in raw_files:
            mt, _ = detect_media_type(f)
            if self.extract_archives and mt == MediaType.ARCHIVE:
                extracted, issues, _note = self._recursive_extract(f, depth=0)
                archive_side_issues[str(f.resolve())] = issues
                for ef in extracted:
                    if ef.is_file():
                        emt, _ = detect_media_type(ef)
                        if emt != MediaType.ARCHIVE:
                            all_files.append(ef)
                all_files.append(f.resolve())
            else:
                all_files.append(f.resolve())

        results: list[AuditResult] = []
        total = len(all_files)

        for idx, file_path in enumerate(all_files):
            side_issues = archive_side_issues.get(str(file_path.resolve()), [])
            try:
                if self.skip_metadata:
                    md = extract_basic_metadata(file_path, compute_hash=self.compute_hash)
                else:
                    md = extract_full_metadata(file_path, compute_hash=self.compute_hash)
                result = apply_rules(md, self.rules)
            except Exception as e:
                md = MediaMetadata(
                    file_path=str(file_path.resolve()),
                    file_name=file_path.name,
                    file_size=file_path.stat().st_size if file_path.exists() else 0,
                )
                result = AuditResult(
                    metadata=md,
                    status=AuditStatus.REVIEW,
                    score=0.0,
                    issues=[Issue(
                        code="PROCESS_ERROR",
                        message=f"处理文件出错: {type(e).__name__}: {e}",
                        severity=IssueSeverity.ERROR,
                        details={"error_type": type(e).__name__},
                    )],
                )
            if side_issues:
                result.issues.extend(side_issues)
                for si in side_issues:
                    if si.code not in result.matched_rules:
                        result.matched_rules.append(si.code)
                    if si.severity in (IssueSeverity.ERROR, IssueSeverity.CRITICAL):
                        result.score = max(0.0, result.score - 10)
                        if result.status == AuditStatus.PASS:
                            result.status = AuditStatus.REVIEW
                    elif si.severity == IssueSeverity.WARNING:
                        result.score = max(0.0, result.score - 5)
                        if result.status == AuditStatus.PASS:
                            result.status = AuditStatus.REVIEW
            results.append(result)
            if progress_callback:
                progress_callback(idx + 1, total, file_path)

        run_all_validations(
            results,
            naming_pattern=self.naming_pattern,
            allow_spaces=self.allow_spaces,
        )

        return results

    def scan_with_summary(
        self,
        input_path: str | Path,
        progress_callback: Optional[callable] = None,
    ) -> tuple[list[AuditResult], AuditSummary]:
        start = time.time()
        results = self.scan(input_path, progress_callback=progress_callback)
        elapsed = time.time() - start
        summary = build_summary(results, duration=elapsed)
        return results, summary
