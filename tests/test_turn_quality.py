"""turn_quality 纯函数(EX-2,2026-09-28):句级置信度三态 band + 碎片判据。

bart: band_from_confidence 只做辅助信号(窄带数字错听高置信实测);
looks_garbled 是确定性文本判据。两函数的边界行为钉死在此。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "core"))

from bok_voice_core.turn_quality import band_from_confidence, looks_garbled  # noqa: E402


# ---- band_from_confidence ----

def test_band_none_and_missing_keys_unknown():
    assert band_from_confidence(None, 0.45, 0.5) == "unknown"
    assert band_from_confidence({}, 0.45, 0.5) == "unknown"
    assert band_from_confidence({"min": 0.1}, 0.45, 0.5) == "unknown"
    assert band_from_confidence("not-a-dict", 0.45, 0.5) == "unknown"


def test_band_mean_below_threshold_low():
    assert band_from_confidence({"mean": 0.30}, 0.45, 0.5) == "low"
    assert band_from_confidence({"mean": 0.449999}, 0.45, 0.5) == "low"
    # 边界:恰等阈值唔算 low(< 严格)
    assert band_from_confidence({"mean": 0.45}, 0.45, 0.5) == "ok"


def test_band_low_token_ratio_low():
    assert band_from_confidence({"mean": 0.9, "low_tokens": 5, "n_tokens": 10}, 0.45, 0.5) == "low"
    assert band_from_confidence({"mean": 0.9, "low_tokens": 4, "n_tokens": 10}, 0.45, 0.5) == "ok"
    # n_tokens=0 唔除、唔触发 ratio 档
    assert band_from_confidence({"mean": 0.9, "low_tokens": 3, "n_tokens": 0}, 0.45, 0.5) == "ok"


def test_band_ok_and_failopen():
    assert band_from_confidence({"mean": 0.9, "min": 0.8}, 0.45, 0.5) == "ok"
    # 非数值 → fail-open unknown(绝不因读数异常误开/误关)
    assert band_from_confidence({"mean": "abc"}, 0.45, 0.5) == "unknown"
    assert band_from_confidence({"mean": 0.9, "low_tokens": "x", "n_tokens": 10}, 0.45, 0.5) == "unknown"


# ---- looks_garbled:spec 列举的六条期望 ----

def test_looks_garbled_expected_behaviors():
    assert looks_garbled("64311133") is True                       # 纯 ASCII 数字
    assert looks_garbled("六四三一一三三") is True                   # 纯中文数字
    assert looks_garbled("拼多多", ("拼多多",)) is True               # 纯热词回声
    assert looks_garbled("我個WhatsApp係六四三") is False            # 真话头+渠道词+号码
    assert looks_garbled("好啦") is False
    assert looks_garbled("唔好意思頭先冇聽清，你講多次") is False


def test_looks_garbled_empty_and_punct_only():
    assert looks_garbled("") is True
    assert looks_garbled("   ") is True
    assert looks_garbled("。，, 、；;") is True
    assert looks_garbled("６４３１１１３３") is True                  # 全角数字
    assert looks_garbled("１２３", (), 3) is True


def test_looks_garbled_hotword_longer_first_and_casefold():
    # 长词先剥:术语含长短重叠时唔残留短词残片
    assert looks_garbled("拼多多多", ("拼多多", "多多")) is True
    # 大小写不敏感
    assert looks_garbled("WhatsApp", ("whatsapp",)) is True


def test_looks_garbled_min_content_chars():
    # 单字内容默认 <2=碎片;调 min=1 则放过
    assert looks_garbled("哦", (), 2) is True
    assert looks_garbled("哦", (), 1) is False
    # 拉丁字母算内容
    assert looks_garbled("ok", (), 2) is False
