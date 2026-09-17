"""--qa-status:与物化同口径算 key,ok/missing 两态;--entry-id 过滤。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in ("apps/agent", "packages/core", "scripts"):
    sp = str(ROOT / p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

from agent_runtime.tts_cache import TtsAudioCache  # noqa: E402
import pregen_tts  # noqa: E402


def _rows():
    return [
        {"id": "qa:ok", "answer_text": "您好，包裹已到驿站。", "lang": "zh", "enabled": True},
        {"id": "qa:miss", "answer_text": "不好意思，请稍等。", "lang": "zh", "enabled": True},
        {"id": "qa:off", "answer_text": "停用条目不计", "lang": "zh", "enabled": False},
        {"id": "qa:empty", "answer_text": "", "lang": "zh", "enabled": True},
    ]


def test_qa_status_ok_and_missing(tmp_path):
    cache = TtsAudioCache(tmp_path)
    rows = _rows()
    # 先用同函数算计划,再把 ok 条目物化进缓存,状态应翻成 ok。
    plan = pregen_tts._qa_status(
        rows, persona_pool=[], lang_personas={"zh": None},
        all_personas=False, tts_cfg={}, voice_mode="single",
        model="speech-2.8-hd", sample_rate=24000, cache=cache,
    )
    assert set(plan) == {"qa:ok", "qa:miss"}  # 停用/空答案不进计划
    miss = plan["qa:miss"]
    assert miss["state"] == "missing" and miss["voice"] and miss["key"]
    cache.store(miss["key"], b"\x00\x00" * 100, text="不好意思，请稍等。",
                voice=miss["voice"], model="speech-2.8-hd", pin=True)
    plan2 = pregen_tts._qa_status(
        rows, persona_pool=[], lang_personas={"zh": None},
        all_personas=False, tts_cfg={}, voice_mode="single",
        model="speech-2.8-hd", sample_rate=24000, cache=cache,
    )
    assert plan2["qa:miss"]["state"] == "ok"
    assert plan2["qa:ok"]["state"] == "missing"  # 未物化的仍 missing


def test_entry_id_filter(tmp_path):
    cache = TtsAudioCache(tmp_path)
    plan = pregen_tts._qa_status(
        _rows(), persona_pool=[], lang_personas={"zh": None},
        all_personas=False, tts_cfg={}, voice_mode="single",
        model="speech-2.8-hd", sample_rate=24000, cache=cache,
        entry_ids={"qa:miss"},
    )
    assert set(plan) == {"qa:miss"}
