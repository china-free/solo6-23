"""端到端测试脚本 - 生成测试素材并验证完整审核流程"""
from __future__ import annotations

import io
import os
import sys
import json
import zipfile
import tempfile
import traceback
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def create_test_image(path: Path, size=(800, 600), color=(255, 0, 0), fmt="PNG") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color)
    img.save(path, format=fmt)


def create_test_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding=encoding) as f:
        f.write(content)


def create_test_wav(path: Path, duration_seconds: float = 2.0, sample_rate: int = 44100) -> None:
    import struct
    import math
    path.parent.mkdir(parents=True, exist_ok=True)
    freq = 440.0
    num_samples = int(duration_seconds * sample_rate)
    amplitude = 16000
    audio_data = []
    for i in range(num_samples):
        t = i / sample_rate
        sample = int(amplitude * math.sin(2 * math.pi * freq * t))
        audio_data.append(struct.pack("<h", sample))
    audio_bytes = b"".join(audio_data)
    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = len(audio_bytes)
    riff_size = 36 + data_size
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", riff_size))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))
        f.write(struct.pack("<H", 1))
        f.write(struct.pack("<H", num_channels))
        f.write(struct.pack("<I", sample_rate))
        f.write(struct.pack("<I", byte_rate))
        f.write(struct.pack("<H", block_align))
        f.write(struct.pack("<H", bits_per_sample))
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(audio_bytes)


def create_test_zip(zip_path: Path, files: list[tuple[str, bytes]]) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files:
            zf.writestr(name, data)


def generate_test_materials(root: Path) -> dict:
    info = {"root": str(root)}

    img_dir = root / "images"
    create_test_image(img_dir / "good_photo.png", (1920, 1080), (100, 200, 100), "PNG")
    info["good_image"] = str(img_dir / "good_photo.png")

    create_test_image(img_dir / "small_icon.jpg", (300, 200), (255, 255, 0), "JPEG")
    info["small_image"] = str(img_dir / "small_icon.jpg")

    create_test_image(img_dir / "huge_picture.bmp", (4000, 3000), (0, 100, 200), "BMP")
    info["huge_image"] = str(img_dir / "huge_picture.bmp")

    create_test_image(img_dir / "duplicate a.png", (1920, 1080), (128, 128, 128), "PNG")
    img2 = img_dir / "duplicate_b.png"
    import shutil
    shutil.copyfile(img_dir / "duplicate a.png", img2)
    info["dup_a"] = str(img_dir / "duplicate a.png")
    info["dup_b"] = str(img2)

    bad_name_dir = root / "bad names"
    bad_name_dir.mkdir(parents=True, exist_ok=True)
    create_test_image(bad_name_dir / "file with spaces.png", (1024, 768), (50, 50, 50), "PNG")
    info["space_name"] = str(bad_name_dir / "file with spaces.png")
    create_test_image(bad_name_dir / "bad:chars*in?.png", (1024, 768), (50, 50, 50), "PNG")
    info["special_chars"] = str(bad_name_dir / "bad:chars*in?.png")

    txt_dir = root / "texts"
    create_test_text(txt_dir / "normal.txt", "这是一段正常的中文文本内容。\n" * 10)
    info["normal_text"] = str(txt_dir / "normal.txt")

    create_test_text(txt_dir / "short.txt", "短")
    info["short_text"] = str(txt_dir / "short.txt")

    long_text = "A" * 120000
    create_test_text(txt_dir / "long_note.md", long_text)
    info["long_text"] = str(txt_dir / "long_note.md")

    create_test_text(txt_dir / "gbk_file.txt", "这是 GBK 编码的内容。", encoding="gbk")
    info["gbk_text"] = str(txt_dir / "gbk_file.txt")

    aud_dir = root / "audio"
    create_test_wav(aud_dir / "short_beep.wav", duration_seconds=0.5)
    info["short_audio"] = str(aud_dir / "short_beep.wav")
    create_test_wav(aud_dir / "normal_sound.wav", duration_seconds=10.0)
    info["normal_audio"] = str(aud_dir / "normal_sound.wav")

    empty_dir = root / "empty"
    empty_dir.mkdir(parents=True, exist_ok=True)
    (empty_dir / "zero.txt").touch()
    info["empty_file"] = str(empty_dir / "zero.txt")

    arc_dir = root / "archives"
    zip_files = [
        ("archived_img.png", _make_png_bytes((640, 480))),
        ("archived_note.txt", b"Hello from inside the zip!\n" * 20),
        ("inner/layered_file.txt", b"Nested file content"),
    ]
    create_test_zip(arc_dir / "test_bundle.zip", zip_files)
    info["zip_archive"] = str(arc_dir / "test_bundle.zip")

    return info


def _make_png_bytes(size=(640, 480)) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", size, (200, 100, 200))
    img.save(buf, format="PNG")
    return buf.getvalue()


def run_tests():
    passed = 0
    failed = 0
    errors = []

    from media_audit.type_detector import detect_media_type, is_archive
    from media_audit.metadata import extract_full_metadata
    from media_audit.models import MediaType, AuditStatus
    from media_audit.rules import build_default_rules, apply_rules, load_rules_from_config
    from media_audit.validators import (
        DuplicateDetector,
        check_required_fields,
        check_naming_convention,
        check_encoding_issues,
        run_all_validations,
    )
    from media_audit.pipeline import AuditPipeline, collect_files
    from media_audit.output import build_summary, export_json, export_csv

    with tempfile.TemporaryDirectory(prefix="ma_test_") as td:
        td_path = Path(td)
        print(f"[1/7] 生成测试素材到: {td_path}")
        info = generate_test_materials(td_path)

        print("\n[2/7] 测试素材类型识别...")
        cases = [
            (info["good_image"], MediaType.IMAGE),
            (info["normal_text"], MediaType.TEXT),
            (info["normal_audio"], MediaType.AUDIO),
            (info["zip_archive"], MediaType.ARCHIVE),
            (info["empty_file"], MediaType.TEXT),
        ]
        for fp, expected in cases:
            got, mime = detect_media_type(fp)
            ok = got == expected
            name = Path(fp).name
            if ok:
                passed += 1
                print(f"  ✅ {name}: {got.value} (mime={mime or '-'})")
            else:
                failed += 1
                msg = f"类型识别失败: {name}, 期望 {expected.value}, 实际 {got.value}"
                errors.append(msg)
                print(f"  ❌ {msg}")

        assert is_archive(info["zip_archive"]), "zip 压缩包未被识别"
        passed += 1
        print(f"  ✅ 压缩包识别: test_bundle.zip")

        print("\n[3/7] 测试元数据抽取...")
        md = extract_full_metadata(info["good_image"])
        assert md.width == 1920 and md.height == 1080, f"图片尺寸错误: {md.width}x{md.height}"
        assert md.media_type == MediaType.IMAGE
        assert md.file_size > 0
        assert md.file_hash and len(md.file_hash) == 64
        passed += 4
        print(f"  ✅ 图片元数据: {md.width}x{md.height}, size={md.file_size}, hash_len={len(md.file_hash)}")

        md_txt = extract_full_metadata(info["normal_text"])
        assert md_txt.text_length > 0
        assert md_txt.text_encoding in ("utf-8", "utf-8-sig")
        passed += 2
        print(f"  ✅ 文本元数据: length={md_txt.text_length}, encoding={md_txt.text_encoding}")

        md_aud = extract_full_metadata(info["normal_audio"])
        assert md_aud.duration_seconds is not None and md_aud.duration_seconds >= 9.0
        passed += 1
        print(f"  ✅ 音频元数据: duration={md_aud.duration_seconds:.1f}s")

        print("\n[4/7] 测试规则引擎...")
        rules = build_default_rules()
        assert len(rules) >= 10, f"默认规则数量不足: {len(rules)}"
        passed += 1
        print(f"  ✅ 默认规则数: {len(rules)}")

        result_good = apply_rules(md, rules)
        print(f"  ✅ 良好图片: status={result_good.status.value}, score={result_good.score:.0f}")
        passed += 1

        md_small = extract_full_metadata(info["small_image"])
        result_small = apply_rules(md_small, rules)
        has_small_issue = any("SMALL" in i.code or "LOW" in i.code.upper() for i in result_small.issues)
        print(f"  ✅ 小图片: status={result_small.status.value}, issues={len(result_small.issues)}")
        passed += 1

        md_empty = extract_full_metadata(info["empty_file"])
        result_empty = apply_rules(md_empty, rules)
        assert result_empty.status == AuditStatus.REJECT, f"空文件应被拒绝: {result_empty.status}"
        print(f"  ✅ 空文件: status={result_empty.status.value} (正确被拒绝)")
        passed += 1

        rules_cfg = ROOT / "examples" / "rules.example.json"
        custom_rules = load_rules_from_config(rules_cfg)
        assert len(custom_rules) >= 5, "自定义规则加载失败"
        print(f"  ✅ 自定义规则: {len(custom_rules)} 条 (来自 rules.example.json)")
        passed += 1

        print("\n[5/7] 测试异常检测...")
        dup_detector = DuplicateDetector()
        md_dup_a = extract_full_metadata(info["dup_a"])
        md_dup_b = extract_full_metadata(info["dup_b"])
        dup_detector.add(md_dup_a)
        dup_detector.add(md_dup_b)
        dups = dup_detector.find_duplicates(md_dup_a)
        assert len(dups) == 1, f"重复检测失败: {len(dups)}"
        print(f"  ✅ 重复素材检测: 找到 {len(dups)} 个重复项")
        passed += 1

        issues = check_required_fields(md_small)
        print(f"  ✅ 必填字段检查: {len(issues)} 个问题")
        passed += 1

        md_sp_name = extract_full_metadata(info["space_name"])
        name_issues = check_naming_convention(md_sp_name, allow_spaces=False)
        has_space_issue = any(i.code == "NAME_HAS_SPACES" for i in name_issues)
        assert has_space_issue, "空格文件名未被检测"
        print(f"  ✅ 命名规范检查(空格): 检测成功")
        passed += 1

        md_spec_name = extract_full_metadata(info["special_chars"])
        spec_issues = check_naming_convention(md_spec_name)
        has_spec = any(i.code == "NAME_SPECIAL_CHARS" for i in spec_issues)
        assert has_spec, "特殊字符文件名未被检测"
        print(f"  ✅ 命名规范检查(特殊字符): 检测成功")
        passed += 1

        md_gbk = extract_full_metadata(info["gbk_text"])
        enc_issues = check_encoding_issues(md_gbk)
        has_non_utf8 = any(i.code == "ENC_NON_UTF8" for i in enc_issues)
        assert has_non_utf8, f"GBK 编码未被检测: {[i.code for i in enc_issues]}"
        print(f"  ✅ 编码检查: GBK 文件被标记 (encoding={md_gbk.text_encoding})")
        passed += 1

        print("\n[6/7] 测试审核流水线 (含压缩包解压)...")
        pipeline = AuditPipeline(
            rules=rules,
            compute_hash=True,
            extract_archives=True,
            recursive=True,
        )
        results, summary = pipeline.scan_with_summary(td_path)
        pipeline.cleanup()

        print(f"  ✅ 扫描结果: 总素材 {summary.total}, 通过 {summary.passed}, "
              f"复核 {summary.review}, 拒绝 {summary.rejected}, 重复 {summary.duplicates}")
        assert summary.total >= 12, f"扫描到的素材数不足: {summary.total}"
        assert summary.rejected >= 1, "空文件应被拒绝"
        assert summary.duplicates >= 2, "重复图片对未被检测"
        passed += 3

        print("\n[7/7] 测试输出导出...")
        with tempfile.TemporaryDirectory() as out_dir:
            out_path = Path(out_dir)
            json_path = out_path / "result.json"
            csv_path = out_path / "result.csv"
            export_json(results, json_path, summary=summary)
            export_csv(results, csv_path)
            assert json_path.exists() and json_path.stat().st_size > 100
            assert csv_path.exists() and csv_path.stat().st_size > 100
            with open(json_path, "r", encoding="utf-8") as f:
                jd = json.load(f)
            assert "summary" in jd and "results" in jd
            assert jd["summary"]["total"] == summary.total
            print(f"  ✅ JSON 导出: {json_path.stat().st_size} bytes")
            print(f"  ✅ CSV 导出: {csv_path.stat().st_size} bytes")
            passed += 4

        pipeline.cleanup()

    print("\n" + "=" * 60)
    print(f"测试完成: ✅ 通过 {passed}, ❌ 失败 {failed}")
    if errors:
        print("\n失败详情:")
        for e in errors:
            print(f"  - {e}")
    print("=" * 60)
    return failed == 0


if __name__ == "__main__":
    print("=" * 60)
    print("Media Audit Tool - 端到端测试")
    print("=" * 60)
    try:
        success = run_tests()
    except Exception as e:
        print(f"\n💥 测试异常: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0 if success else 1)
