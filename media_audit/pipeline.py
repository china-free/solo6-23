"""审核流水线 - 目录扫描、压缩包解压、批量处理"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import zipfile
import tarfile
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
    detect_media_type,
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


def extract_archive(
    archive_path: str | Path,
    extract_to: Optional[str | Path] = None,
) -> tuple[Path, list[Path]]:
    archive = Path(archive_path).resolve()
    target = Path(extract_to).resolve() if extract_to else Path(tempfile.mkdtemp(prefix="extracted_"))
    target.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []

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
        except (zipfile.BadZipFile, OSError, RuntimeError):
            pass
        return target, extracted

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

    return target, extracted


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
        archive_files: list[Path] = []

        for f in raw_files:
            mt, _ = detect_media_type(f)
            if self.extract_archives and mt == MediaType.ARCHIVE:
                archive_files.append(f)
            else:
                all_files.append(f)

        for af in archive_files:
            td, extracted = extract_archive(af)
            self._temp_dirs.append(td)
            for ef in extracted:
                if ef.is_file():
                    emt, _ = detect_media_type(ef)
                    if emt != MediaType.ARCHIVE:
                        all_files.append(ef)

        results: list[AuditResult] = []
        total = len(all_files)

        for idx, file_path in enumerate(all_files):
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
