"""专项测试：压缩格式识别、解压能力、不支持格式告警"""
from __future__ import annotations

import bz2
import gzip
import io
import json
import lzma
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from media_audit.models import MediaType
from media_audit.type_detector import (
    ArchiveSupportLevel,
    detect_media_type,
    get_archive_support_level,
    is_archive,
    list_archive_contents,
)
from media_audit.pipeline import AuditPipeline, extract_archive


def _make_png_bytes(size=(640, 480), color=(100, 200, 100)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def make_tar_gz(path: Path, members: list[tuple[str, bytes]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def make_tar_bz2(path: Path, members: list[tuple[str, bytes]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:bz2") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def make_tar_xz(path: Path, members: list[tuple[str, bytes]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:xz") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def write_single_gz(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as f:
        f.write(payload)


def write_single_bz2(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with bz2.open(path, "wb") as f:
        f.write(payload)


def write_single_xz(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with lzma.open(path, "wb") as f:
        f.write(payload)


def write_fake_rar(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"Rar!\x1a\x07\x01\x00" + b"fake body" * 16)


def write_fake_7z(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 16 + b"fake body" * 16)


def run() -> int:
    passed = 0
    failed = 0
    errors = []

    with tempfile.TemporaryDirectory(prefix="ma_arc_") as td:
        base = Path(td)

        png = _make_png_bytes()
        txt = b"Hello, World!\n" * 200

        make_tar_gz(base / "bundle.tgz", [("photo.png", png), ("notes.txt", txt)])
        make_tar_bz2(base / "bundle.tar.bz2", [("img.png", png)])
        make_tar_xz(base / "bundle.tar.xz", [("a.txt", txt), ("b.png", png)])
        write_single_gz(base / "notes.txt.gz", txt)
        write_single_bz2(base / "document.txt.bz2", txt)
        write_single_xz(base / "data.txt.xz", txt)
        write_single_gz(base / "only_png.png.gz", png)
        write_fake_rar(base / "old_backup.rar")
        write_fake_7z(base / "stuff.7z")
        with zipfile.ZipFile(base / "clean.zip", "w") as zf:
            zf.writestr("z_img.png", png)
            zf.writestr("z_doc.txt", txt)

        cases = [
            ("bundle.tgz", ArchiveSupportLevel.FULL, MediaType.ARCHIVE, 2),
            ("bundle.tar.bz2", ArchiveSupportLevel.FULL, MediaType.ARCHIVE, 1),
            ("bundle.tar.xz", ArchiveSupportLevel.FULL, MediaType.ARCHIVE, 2),
            ("notes.txt.gz", ArchiveSupportLevel.SINGLE, MediaType.ARCHIVE, 1),
            ("document.txt.bz2", ArchiveSupportLevel.SINGLE, MediaType.ARCHIVE, 1),
            ("data.txt.xz", ArchiveSupportLevel.SINGLE, MediaType.ARCHIVE, 1),
            ("only_png.png.gz", ArchiveSupportLevel.SINGLE, MediaType.ARCHIVE, 1),
            ("clean.zip", ArchiveSupportLevel.FULL, MediaType.ARCHIVE, 2),
            ("old_backup.rar", ArchiveSupportLevel.NONE, MediaType.ARCHIVE, 0),
            ("stuff.7z", ArchiveSupportLevel.NONE, MediaType.ARCHIVE, 0),
        ]

        print("[1/3] 类型识别 & 支持等级检测")
        for name, expected_level, expected_type, _ in cases:
            fp = base / name
            mt, mime = detect_media_type(fp)
            lv, lv_mime = get_archive_support_level(fp)
            ia = is_archive(fp)
            ok_mt = mt == expected_type
            ok_lv = lv == expected_level
            ok = ok_mt and ok_lv and ia
            if ok:
                passed += 1
                print(f"  ✅ {name}: type={mt.value}, level={lv.value}, is_archive=True")
            else:
                failed += 1
                msg = (
                    f"{name}: type={mt.value}(期望 {expected_type.value}), "
                    f"level={lv.value}(期望 {expected_level.value}), is_archive={ia}"
                )
                errors.append(msg)
                print(f"  ❌ {msg}")

        print("\n[2/3] extract_archive 逐项验证")
        for name, expected_level, _, expected_count in cases:
            fp = base / name
            target, extracted, level, note = extract_archive(fp)
            shutil.rmtree(target, ignore_errors=True)
            ok_count = len(extracted) == expected_count
            ok_lv = level == expected_level
            ok = ok_count and ok_lv
            if expected_level == ArchiveSupportLevel.NONE:
                has_unsupported_note = "暂不支持" in note or "mime=" in note
                ok = ok and has_unsupported_note
            if ok:
                passed += 1
                print(f"  ✅ {name}: 解压出 {len(extracted)} 个文件 (level={level.value})")
                if expected_level == ArchiveSupportLevel.NONE:
                    print(f"       告警: {note[:70]}...")
            else:
                failed += 1
                msg = (
                    f"{name}: 解压出 {len(extracted)} 个(期望 {expected_count}), "
                    f"level={level.value}(期望 {expected_level.value}), note={note[:60]}"
                )
                errors.append(msg)
                print(f"  ❌ {msg}")

        print("\n[3/3] AuditPipeline 完整流水线验证（含 Issue 注入）")
        pipeline = AuditPipeline(extract_archives=True, recursive=True)
        results, summary = pipeline.scan_with_summary(base)
        pipeline.cleanup()

        by_code: dict[str, int] = {}
        for r in results:
            for i in r.issues:
                by_code[i.code] = by_code.get(i.code, 0) + 1
        name_to_result = {r.metadata.file_name: r for r in results}

        expected_codes = ["ARCHIVE_EXTRACTED", "UNSUPPORTED_ARCHIVE"]
        for code in expected_codes:
            cnt = by_code.get(code, 0)
            if code == "ARCHIVE_EXTRACTED" and cnt >= 7:
                passed += 1
                print(f"  ✅ {code}: {cnt} 条（≥7 个压缩包被正确识别为已解压）")
            elif code == "UNSUPPORTED_ARCHIVE" and cnt >= 2:
                passed += 1
                print(f"  ✅ {code}: {cnt} 条（≥2 个不支持的压缩包被正确告警）")
            else:
                failed += 1
                msg = f"Issue code 数量异常: {code}={cnt}"
                errors.append(msg)
                print(f"  ❌ {msg}")

        rar_result = name_to_result.get("old_backup.rar")
        sevenz_result = name_to_result.get("stuff.7z")
        for label, r in [("old_backup.rar", rar_result), ("stuff.7z", sevenz_result)]:
            if r is None:
                failed += 1
                errors.append(f"压缩包自身未进入审核结果: {label}")
                print(f"  ❌ 压缩包自身未进入审核: {label}")
                continue
            has_issue = any(i.code == "UNSUPPORTED_ARCHIVE" for i in r.issues)
            if has_issue:
                passed += 1
                print(f"  ✅ {label} 作为压缩包已纳入审核，并带有 UNSUPPORTED_ARCHIVE 警告")
            else:
                failed += 1
                msg = f"{label} 缺少 UNSUPPORTED_ARCHIVE Issue，实际: {[i.code for i in r.issues]}"
                errors.append(msg)
                print(f"  ❌ {msg}")

        expected_inner = {
            "photo.png", "notes.txt", "img.png", "a.txt", "b.png",
            "z_img.png", "z_doc.txt",
            "notes.txt", "document.txt", "data.txt", "only_png.png",
        }
        inner_names = {r.metadata.file_name for r in results} & expected_inner
        missing = expected_inner - inner_names
        if not missing:
            passed += 1
            print(f"  ✅ 预期内部文件全部被解压并进入审核（共 {len(inner_names)} 个）")
        else:
            failed += 1
            msg = f"内部文件缺失: {sorted(missing)}"
            errors.append(msg)
            print(f"  ❌ {msg}")

        print(f"\n  📊 流水线汇总: 总素材={summary.total}, 通过={summary.passed}, "
              f"复核={summary.review}, 拒绝={summary.rejected}")

    print("\n" + "=" * 60)
    print(f"压缩格式专项测试: ✅ 通过 {passed}, ❌ 失败 {failed}")
    if errors:
        print("\n失败详情:")
        for e in errors:
            print(f"  - {e}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
