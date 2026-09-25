"""W2c 语境化过渡承诺(2026-09-24)单测——桶判定/选池优先/闸,零音频零会话。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

import agent_runtime.fillers as fillers_mod  # noqa: E402
from agent_runtime.fillers import FillerDirector, derive_context_bucket  # noqa: E402


def _director(monkeypatch, tmp_path, entries, **kw):
    monkeypatch.setattr(fillers_mod, "load_manifest", lambda d: {"zh": entries})
    return FillerDirector(
        None,
        lang_resolver=lambda: "zh",
        guards=lambda: False,
        assets_dir=tmp_path,
        **kw,
    )


def test_derive_bucket_priority_order():
    kw = dict(has_steps=True, step_index=2, goal="跟进", ref="通知")
    assert derive_context_bucket(turn_provider="graph-notify", **kw) == "handoff"
    assert derive_context_bucket(turn_provider="branch-notify", **kw) == "handoff"
    assert derive_context_bucket(wa_signal_kind="captured", **kw) == "wa"
    assert derive_context_bucket(wa_signal_kind="offered", **kw) == "wa"
    assert derive_context_bucket(wa_step=True, wa_captured=False, **kw) == "wa"
    base = {k: v for k, v in kw.items() if k not in ("goal", "ref", "step_index")}
    assert derive_context_bucket(goal="赔偿三档", ref="承诺", step_say_done=True, **base) == "comp"
    assert derive_context_bucket(goal="赔偿三档", ref="承诺", step_say_done=False, **base) == "query"
    assert derive_context_bucket(goal="确认身份", ref="您好", step_index=0, **base) == "identity"
    assert derive_context_bucket(goal="查物流进度", ref="", step_index=3, **base) == "query"
    assert derive_context_bucket(goal="其他", ref="", step_index=3, verdict="question", **base) == "query"
    assert derive_context_bucket(goal="其他", ref="", step_index=3, **base) == ""


def test_pick_prefers_promise_pool(monkeypatch, tmp_path):
    entries = [
        {"text": "通用", "file": "a.wav", "dur_s": 1.0, "cat": "check"},
        {"text": "通知专员", "file": "p1.wav", "dur_s": 1.0, "cat": "promise_handoff"},
    ]
    monkeypatch.setenv("BOK_FILLER_CONTEXT", "1")
    d = _director(monkeypatch, tmp_path, entries)
    assert d._pick("zh", "", "handoff")["file"] == "p1.wav"
    assert d._pick("zh", "check", "wa")["file"] == "a.wav"  # 桶池缺失回现行阶梯
    monkeypatch.setenv("BOK_FILLER_CONTEXT", "0")
    # kill=旧档:桶优先**消失**(而非封禁 promise 条目)——promise 条目在旧档只是
    # 普通池成员,通用池必须可被选中。12 采样断言(a 全不被选概率 2^-12);旧版
    # 单次断言在清窗后是 random.choice 掷硬币,1/5 假红实证。
    picks = set()
    for _ in range(12):
        d._recent.clear()
        picks.add(d._pick("zh", "", "handoff")["file"])
    assert "a.wav" in picks, "kill=旧档:桶优先消失,通用池应可被选中"


def test_query_bucket_overrides_cat_when_no_promise_pool(monkeypatch, tmp_path):
    entries = [
        {"text": "通用默认", "file": "d.wav", "dur_s": 1.0, "cat": "default"},
        {"text": "查证承诺", "file": "c.wav", "dur_s": 1.0, "cat": "check"},
    ]
    monkeypatch.setenv("BOK_FILLER_CONTEXT", "1")
    d = _director(monkeypatch, tmp_path, entries)
    got = set()
    for _ in range(12):
        d._recent.clear()
        got.add(d._pick("zh", "minimal", "query")["file"])
    assert got == {"c.wav"}


def test_context_resolver_failure_is_silent(monkeypatch, tmp_path):
    def boom():
        raise RuntimeError("no flow")
    d = _director(monkeypatch, tmp_path, [], context_resolver=boom)
    assert d._current_bucket() == ""
    d2 = _director(monkeypatch, tmp_path, [], context_resolver=lambda: {"bucket": "wa"})
    assert d2._current_bucket() == "wa"


def test_forward_env_registers_context_gate():
    import tools.bok as bok  # noqa: E402

    assert "BOK_FILLER_CONTEXT" in bok._FORWARD_ENV
