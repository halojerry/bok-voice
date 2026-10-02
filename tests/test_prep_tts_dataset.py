"""prep_tts_dataset 纯函数面离线测试（TTS-SFT-DATA-PREP 阶段 C 数据先行）。

**验收口径：本文件全部离线**——不碰 :8787 sidecar、不做真 ASR 转写、不产真实
训练数据（sidecar 在跑也只测纯函数）。覆盖：

- merge_windows：相邻窗续并 / <1s 碎片丢弃 / 超 30s 不硬切只告警计数 /
  孤段不足 3s 丢弃 / 段够长后大缝不再续并。
- pcm_stats（削波+静音统计）与 decode_s16 字节序。
- JSONL 行序列化 round-trip（中文原样）与 apply_ref 回填幂等。
- choose_ref：8-10s 区间内取最干净；区间外回落最近并置 widened。
- parse_silencedetect：ffmpeg stderr 文本 → 语音窗（含尾静音未闭合）。
- sidecar_language：CLI 规范值 → sidecar 提示值（zh 下发 Chinese）。
- require_ffmpeg：二进制缺失 = SystemExit(2) 人话报错（monkeypatch shutil.which）。

夹具一律 canto 前缀（术语门禁：语言规范值=cantonese，禁旧拼写）。
"""

from __future__ import annotations

import array
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import prep_tts_dataset as prep  # noqa: E402


# ── 窗口合并 ───────────────────────────────────────────────────────────────


def test_canto_merge_windows_adjacent_join() -> None:
    # 两个 2s 窗缝 0.2s（<=gap_merge）→ 并成一条 ~4.2s（>=min_sec 保留）
    out, stats = prep.merge_windows([(0.0, 2.0), (2.2, 4.2)], min_sec=3.0)
    assert out == [(0.0, 4.2)]
    assert stats["fragments_dropped"] == 0
    assert stats["short_dropped"] == 0
    assert stats["oversized_kept"] == 0


def test_canto_merge_windows_short_chunk_bridges_larger_gap() -> None:
    # 段还不足 3s 时允许跨 <=short_join_gap(1.0s) 的缝拉邻窗凑长
    out, _ = prep.merge_windows([(0.0, 1.5), (2.3, 4.5)], min_sec=3.0, gap_merge=0.3, short_join_gap=1.0)
    assert out == [(0.0, 4.5)]


def test_canto_merge_windows_fragment_dropped() -> None:
    # <1s 碎片丢弃（计数），孤证不并
    out, stats = prep.merge_windows([(0.0, 0.5), (2.0, 6.0)], min_sec=3.0, gap_merge=0.3)
    assert out == [(2.0, 6.0)]
    assert stats["fragments_dropped"] == 1


def test_canto_merge_windows_short_orphan_dropped() -> None:
    # 2.5s 孤段、两侧大缝（>short_join_gap）凑不成长 → 丢弃计数
    out, stats = prep.merge_windows(
        [(0.0, 2.5), (5.0, 9.0)], min_sec=3.0, gap_merge=0.3, short_join_gap=1.0
    )
    assert out == [(5.0, 9.0)]
    assert stats["short_dropped"] == 1


def test_canto_merge_windows_oversize_not_hard_split() -> None:
    # 单条连续语音 35s：不硬切，整段保留 + oversized_kept 告警计数
    out, stats = prep.merge_windows([(0.0, 35.0)], min_sec=3.0, max_sec=30.0)
    assert out == [(0.0, 35.0)]
    assert stats["oversized_kept"] == 1


def test_canto_merge_windows_stops_merging_at_max() -> None:
    # 预闭合：并入下一窗会超 max_sec 时先封当前段（切片永不落在语音窗内部）
    out, stats = prep.merge_windows(
        [(0.0, 14.0), (14.1, 28.0), (28.1, 42.0)], min_sec=3.0, max_sec=30.0, gap_merge=0.3
    )
    assert out == [(0.0, 28.0), (28.1, 42.0)]
    assert stats["oversized_kept"] == 0


# ── 削波 / 静音统计 ────────────────────────────────────────────────────────


def test_canto_pcm_stats_clipping_and_silence() -> None:
    samples = array.array("h", [32767, -32768, 32000, -32001, 100, -100, 0, 499])
    st = prep.pcm_stats(samples)
    # |v|>=32000 记削波：32767/-32768/32000/-32001 共 4 个
    assert st["clipped"] == 4
    assert st["total_samples"] == 8
    # |v|<500 记静音：100/-100/0/499 共 4 个
    assert st["silence_ratio"] == pytest.approx(0.5)
    assert st["clip_ratio"] == pytest.approx(0.5)


def test_canto_decode_s16_roundtrip_little_endian() -> None:
    import struct

    raw = struct.pack("<4h", 1, -2, 32767, -32768)
    arr = prep.decode_s16(raw)
    assert list(arr) == [1, -2, 32767, -32768]


def test_canto_pcm_stats_empty() -> None:
    st = prep.pcm_stats(array.array("h"))
    assert st["clipped"] == 0
    assert st["clip_ratio"] == 0.0
    assert st["silence_ratio"] == 0.0


# ── 时长分布 ───────────────────────────────────────────────────────────────


def test_canto_duration_stats_p50_p90() -> None:
    durs = [3.0 + i for i in range(10)]  # 3..12
    st = prep.duration_stats(durs)
    assert st["count"] == 10
    assert st["total_sec"] == pytest.approx(75.0)
    assert st["p50"] == pytest.approx(7.5)
    assert st["p90"] == pytest.approx(11.1)
    assert st["min"] == 3.0
    assert st["max"] == 12.0


def test_canto_duration_stats_empty() -> None:
    assert prep.duration_stats([])["count"] == 0


# ── JSONL 契约 / ref 回填 ──────────────────────────────────────────────────


def test_canto_jsonl_round_trip() -> None:
    rows = [
        {"audio": "segments/canto_call__seg001.wav", "text": "你好，請問係陳生嗎？", "ref_audio": "ref.wav"},
        {"audio": "segments/canto_call__seg002.wav", "text": "係咪要報身份證號碼？", "ref_audio": "ref.wav"},
    ]
    lines = [prep.jsonl_line(r) for r in rows]
    text = "\n".join(lines) + "\n"
    # 中文不转义（官方 prepare_data 直接读，ensure_ascii=False 保原样）
    assert "陳生" in text
    assert "\\u" not in text
    back = prep.parse_jsonl(text)
    assert back == rows


def test_canto_apply_ref_backfills_and_idempotent() -> None:
    rows = [
        {"audio": "segments/a.wav", "text": "第一段", "ref_audio": ""},
        {"audio": "segments/b.wav", "text": "第二段", "ref_audio": "old.wav"},
    ]
    once = prep.apply_ref(rows, "ref.wav")
    assert all(r["ref_audio"] == "ref.wav" for r in once)
    # 其余字段不动
    assert once[0]["audio"] == "segments/a.wav"
    # 幂等：重复回填结果一致
    assert prep.apply_ref(once, "ref.wav") == once
    # 不改入参
    assert rows[0]["ref_audio"] == ""


def test_canto_choose_ref_prefers_cleanest_in_range() -> None:
    cands = [
        {"name": "seg_a", "duration": 9.0, "clip_ratio": 0.01, "silence_ratio": 0.2},
        {"name": "seg_b", "duration": 8.5, "clip_ratio": 0.0, "silence_ratio": 0.3},
        {"name": "seg_c", "duration": 20.0, "clip_ratio": 0.0, "silence_ratio": 0.0},
    ]
    picked, widened = prep.choose_ref(cands)
    assert picked is not None and picked["name"] == "seg_b"  # 区间内削波最少
    assert widened is False


def test_canto_choose_ref_falls_back_nearest_with_warning() -> None:
    cands = [
        {"name": "seg_a", "duration": 4.0, "clip_ratio": 0.0, "silence_ratio": 0.0},
        {"name": "seg_b", "duration": 12.0, "clip_ratio": 0.05, "silence_ratio": 0.0},
    ]
    picked, widened = prep.choose_ref(cands)
    assert picked is not None and picked["name"] == "seg_b"  # 距 9s 中点最近
    assert widened is True


def test_canto_choose_ref_empty() -> None:
    assert prep.choose_ref([]) == (None, False)


# ── ffmpeg silencedetect 解析 ──────────────────────────────────────────────


def test_canto_parse_silencedetect_inverts_to_speech() -> None:
    stderr = "\n".join(
        [
            "[silencedetect @ 0x1] silence_start: 1.0",
            "[silencedetect @ 0x1] silence_end: 1.6 | silence_duration: 0.6",
            "[silencedetect @ 0x1] silence_start: 4.0",
            "[silencedetect @ 0x1] silence_end: 4.5 | silence_duration: 0.5",
        ]
    )
    assert prep.parse_silencedetect(stderr, total_sec=6.0) == [(0.0, 1.0), (1.6, 4.0), (4.5, 6.0)]


def test_canto_parse_silencedetect_unterminated_tail_silence() -> None:
    stderr = "[silencedetect @ 0x1] silence_start: 5.0"
    # 尾部 silence_start 未闭合 → 视为延伸到 total_sec，语音只到 5.0
    assert prep.parse_silencedetect(stderr, total_sec=8.0) == [(0.0, 5.0)]


def test_canto_parse_silencedetect_no_silence() -> None:
    assert prep.parse_silencedetect("nothing here", total_sec=3.0) == [(0.0, 3.0)]


# ── sidecar 语言映射 ───────────────────────────────────────────────────────


def test_canto_sidecar_language_map() -> None:
    assert prep.sidecar_language("zh") == "Chinese"
    assert prep.sidecar_language("cantonese") == "cantonese"
    assert prep.sidecar_language("en") == "English"
    with pytest.raises(ValueError):
        prep.sidecar_language("klingon")


# ── ffmpeg 缺失报错路径 ────────────────────────────────────────────────────


def test_canto_require_ffmpeg_missing_exit_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(SystemExit) as ei:
        prep.require_ffmpeg()
    assert ei.value.code == 2  # 人话报错走 stderr，退出码恒 2
    err = capsys.readouterr().err
    assert "ffmpeg" in err
    assert "audioread" in err  # 人话报错顺带说明为何不兜底 audioread


def test_canto_require_ffmpeg_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/opt/homebrew/bin/ffmpeg" if name == "ffmpeg" else None)
    assert prep.require_ffmpeg() == "/opt/homebrew/bin/ffmpeg"


# ── 报告装配（离线面：manifest + 假转写 → JSONL/报告，不碰 sidecar）────────


def test_canto_assemble_rows_and_report(tmp_path: Path) -> None:
    segments_dir = tmp_path / "segments"
    segments_dir.mkdir()
    asr_dir = tmp_path / "asr"
    asr_dir.mkdir()
    manifest = {
        "version": 1,
        "vad_backend": "ffmpeg",
        "sources": {
            "canto_call.wav": {
                "vad_backend": "ffmpeg",
                "merge_stats": {"fragments_dropped": 1, "short_dropped": 0, "oversized_kept": 0},
                "segments": [
                    {"name": "canto_call__seg001.wav", "source": "canto_call.wav", "start": 0.0,
                     "end": 9.0, "duration": 9.0, "clip_ratio": 0.0, "silence_ratio": 0.6},
                    {"name": "canto_call__seg002.wav", "source": "canto_call.wav", "start": 9.3,
                     "end": 18.0, "duration": 8.7, "clip_ratio": 0.002, "silence_ratio": 0.8},
                ],
            }
        },
    }
    (asr_dir / "canto_call__seg001.wav.json").write_text(
        json.dumps({"text": "你好，請問係邊位？", "language": "cantonese"}, ensure_ascii=False),
        encoding="utf-8",
    )  # seg002 故意不转写 → pending

    class _Args:
        input = tmp_path / "raw"
        lang = "cantonese"
        asr_url = prep.DEFAULT_ASR_URL
        skip = ""
        max_seg = 30.0
        _resample_stats = None
        _ref_info = None

    built = prep.step_assemble(tmp_path, manifest, asr_dir, _Args())  # type: ignore[arg-type]
    rows = prep.parse_jsonl((tmp_path / "train_raw.jsonl").read_text(encoding="utf-8"))
    # 只有已转写片段进 train_raw；ref 为占位（--pick-ref 才物化）
    assert rows == [{"audio": "segments/canto_call__seg001.wav", "text": "你好，請問係邊位？", "ref_audio": "ref.wav"}]
    report = built["report"]
    assert report["asr"]["pending"] == 1
    assert report["asr"]["verified"] is False
    assert report["segments"]["count"] == 2
    assert report["vad"]["fragments_dropped"] == 1
    assert report["qc"]["flags"] and any("ref_missing" in f for f in report["qc"]["flags"])
    # 静音占比=跨段均值（0.6+0.8)/2=0.7 > 0.6 → silence_heavy 旗
    assert any("silence_heavy" in f for f in report["qc"]["flags"])
    # 削波均值 (0+0.002)/2 = 0.001 恰在阈值上不触发（0.001 > 0.001 为假）
    assert not any("clipping" in f for f in report["qc"]["flags"])
    # 逐片段转写表：两段都在，含转写文本与 pending 位
    table = {t["name"]: t for t in report["segments_table"]}
    assert table["canto_call__seg001.wav"]["text"] == "你好，請問係邊位？"
    assert table["canto_call__seg002.wav"]["transcribed"] is False
