"""E7 离线润色的**接线**钉死（2026-09-21）。

模块本体（``packages/core/bok_voice_core/polish.py``）已有离线单测
（``tests/test_polish.py`` + ``test_output_guard.py``）；本文件只钉**接线语义**
（§31.6 队列 ③ 的三处落点）：

- 函数级：kill-switch 默认关、只有 ``"1"`` 开、fail-soft（润色抛异常→原文）、
  入参宽容；
- 面级：三处落点（挂断后纪要 prompt 文本 / QA 挖掘输入 / L-① 漏网轮输入）
  开档真吃到润色稿、关档逐字原样；**原始证据面**（settle 落盘
  ``transcript.md``、no-LLM 兜底摘要的引语）与非线面（``normalize_question``、
  agent 实时轮）不受影响；
- 结构级：三处落点的接线位置读源钉死（照 ``test_snippet_leak_wiring.py`` 姿势，
  闭包接线离线起不了真栈）；**实时轮结构性不 import**（绝不进实时轮）；
- 立法面：``BOK_POLISH_OFFLINE`` 走 CP 面（``bok._control_plane_env``，
  同 ``BOK_SETTLE_LLM_*`` 先例）、**不进** ``_FORWARD_ENV``（那是 agent worker 面）；
- 测量基线：R1 标注语料 152 轮的改动读数（文件末「测量基线」段）。

## 默认关的证据（2026-09-21 实测，与 ``polish_wiring`` 模块 docstring 同源）

- R1 标注语料 152 轮：**48 轮被改（31.6%）**，其中「呃呃呃呃」1 轮的润色稿被
  E3 Guard 判空（``empty``）而**回退原文**——出口 Guard 在真实数据上真在工作；
- 真库（只读）7744 条 A 线转写：**625 条改动（8.1%）**；
- **连带破坏面（本批次「先量再判」抓到，已修）**：``collapse_repetitions`` 的紧邻/三连
  折叠原会吃掉**粘着字母或连串**的数字（``MT3000→MT30``、``soak111→soak1``）与英文
  单词里的重复双字母（``exceeded→exceed``）——这些形态**不产出 Guard 硬保护 token**
  （``output_guard._NUMBER_RE`` 要求两侧非字母数字），故被**静默放行**。真库实测
  逼出后（632→625 条改动）已在 ``polish.py`` 用 ASCII 两侧边界修掉，并留回归判例
  ``test_ascii_runs_never_collapsed``——**这条是「默认关」的发现过程，不再是它的理由**。
- **默认关的现行理由**：它改的是**喂进挖掘与纪要的文本**，而挖掘答案一旦被人工采纳就
  **罐头化**、下一通真实通话照播。收益（去口水/去噪）已量到，但**没有真栈 A/B 证明它
  改善簇纯度/纪要质量**，故不擅自改生产输入面；要开＝运营显式 ``BOK_POLISH_OFFLINE=1``
  并自行比对一轮再决定。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "")  # CP 导入面副作用：强制内存仓
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-polish-wiring-0123456789")

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402

from bok_voice_core import polish_wiring  # noqa: E402
from bok_voice_core.polish_wiring import (  # noqa: E402
    POLISH_OFFLINE_ENV,
    polish_offline_enabled,
    polish_offline_text,
)
from bok_voice_core.qa_text import mine_qa_pairs, normalize_question  # noqa: E402
from control_plane import gap_mining as gm  # noqa: E402
from control_plane.summarize import Summarizer  # noqa: E402

_SUMMARIZE_SRC = (ROOT / "apps/control-plane/control_plane/summarize.py").read_text("utf-8")
_MAIN_SRC = (ROOT / "apps/control-plane/control_plane/main.py").read_text("utf-8")
_QA_TEXT_SRC = (ROOT / "packages/core/bok_voice_core/qa_text.py").read_text("utf-8")
_GAP_SRC = (ROOT / "apps/control-plane/control_plane/gap_mining.py").read_text("utf-8")

_DIRTY_Q = "呃，你哋幾時送到？"
_CLEAN_Q = "你哋幾時送到？"
_DIRTY_A = "哦，兩到三日到。"
_CLEAN_A = "兩到三日到。"

_SEQ = iter(range(1, 100_000))


# ---------------------------------------------------------------- 函数级语义


def test_kill_switch_defaults_off_and_only_one_is_on(monkeypatch):
    """默认关：未设 = 逐字原样；仓规字面只有 "1" 开（"0"/"true"/"yes" 都不算）。"""
    monkeypatch.delenv(POLISH_OFFLINE_ENV, raising=False)
    assert polish_offline_enabled() is False
    assert polish_offline_text(_DIRTY_Q) == _DIRTY_Q
    for value in ("0", "true", "yes", "on", ""):
        monkeypatch.setenv(POLISH_OFFLINE_ENV, value)
        assert polish_offline_enabled() is False
        assert polish_offline_text(_DIRTY_Q) == _DIRTY_Q


def test_kill_switch_on_applies_deterministic_polish(monkeypatch):
    monkeypatch.setenv(POLISH_OFFLINE_ENV, "1")
    assert polish_offline_enabled() is True
    assert polish_offline_text(_DIRTY_Q) == _CLEAN_Q
    # 数字铁律在接线层同款：报号串逐字不动（润色不改数字）
    assert polish_offline_text("我的單號係 881766554433") == "我的單號係 881766554433"


def test_helper_is_fail_soft_when_polish_raises(monkeypatch):
    """fail-soft：润色抛异常 → 返回原文（纪要/挖掘绝不被清洗炸掉）。"""
    monkeypatch.setenv(POLISH_OFFLINE_ENV, "1")

    def _boom(_text):
        raise RuntimeError("polish exploded")

    monkeypatch.setattr(polish_wiring, "polish_text", _boom)
    assert polish_offline_text(_DIRTY_Q) == _DIRTY_Q


def test_helper_tolerates_none_and_non_str(monkeypatch):
    monkeypatch.setenv(POLISH_OFFLINE_ENV, "1")
    assert polish_offline_text(None) == ""
    assert polish_offline_text(1234) == "1234"
    assert polish_offline_text("") == ""


# ---------------------------------------------------------------- 落点 1：纪要输入


def _turns() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(role="user", transcript=_DIRTY_Q),
        SimpleNamespace(role="assistant", transcript=_DIRTY_A),
    ]


def test_summarizer_prompt_text_polished_only_when_on(monkeypatch):
    """纪要 prompt 文本：开档吃润色稿、关档逐字原样（role 前缀与行形状不动）。"""
    monkeypatch.delenv(POLISH_OFFLINE_ENV, raising=False)
    off = Summarizer._render_transcript(_turns())
    assert off == f"user: {_DIRTY_Q}\nassistant: {_DIRTY_A}"
    monkeypatch.setenv(POLISH_OFFLINE_ENV, "1")
    on = Summarizer._render_transcript(_turns())
    assert on == f"user: {_CLEAN_Q}\nassistant: {_CLEAN_A}"


def test_summarizer_prompt_polishing_is_fail_soft(monkeypatch):
    monkeypatch.setenv(POLISH_OFFLINE_ENV, "1")

    def _boom(_text):
        raise RuntimeError("polish exploded")

    monkeypatch.setattr(polish_wiring, "polish_text", _boom)
    assert Summarizer._render_transcript(_turns()) == f"user: {_DIRTY_Q}\nassistant: {_DIRTY_A}"


def test_summarizer_no_llm_fallback_quote_stays_raw(monkeypatch):
    """非接线面：no-LLM 兜底摘要的引语仍是原文（它不喂 LLM，也不该被清洗）。"""
    monkeypatch.setenv(POLISH_OFFLINE_ENV, "1")
    fb = Summarizer._fallback(_turns())
    assert _DIRTY_Q[:60] in fb["summary"]


# ---------------------------------------------------------------- 落点 2：QA 挖掘


def _convos() -> list[list[dict]]:
    return [[
        {"role": "user", "text": _DIRTY_Q, "lang": "zh"},
        {"role": "assistant", "text": _DIRTY_A, "lang": "zh"},
    ]]


def test_mine_qa_pairs_polished_only_when_on(monkeypatch):
    monkeypatch.delenv(POLISH_OFFLINE_ENV, raising=False)
    off = mine_qa_pairs(_convos(), min_calls=1)
    assert off[0]["question"] == normalize_question(_DIRTY_Q)
    assert off[0]["answer"] == _DIRTY_A
    monkeypatch.setenv(POLISH_OFFLINE_ENV, "1")
    on = mine_qa_pairs(_convos(), min_calls=1)
    assert on[0]["question"] == normalize_question(_CLEAN_Q)
    assert on[0]["answer"] == _CLEAN_A


def test_mine_qa_pairs_fail_soft_and_input_not_mutated(monkeypatch):
    monkeypatch.setenv(POLISH_OFFLINE_ENV, "1")

    def _boom(_text):
        raise RuntimeError("polish exploded")

    monkeypatch.setattr(polish_wiring, "polish_text", _boom)
    convos = _convos()
    out = mine_qa_pairs(convos, min_calls=1)
    assert out[0]["question"] == normalize_question(_DIRTY_Q)
    # 映射出的是新 dict：调用方（repo 行）不被改写
    assert convos[0][0]["text"] == _DIRTY_Q and convos[0][1]["text"] == _DIRTY_A


# ---------------------------------------------------------------- 落点 3：L-① 漏网轮


def test_gap_mining_turn_text_polished_only_when_on(monkeypatch):
    monkeypatch.delenv(POLISH_OFFLINE_ENV, raising=False)
    assert gm._turn_text(SimpleNamespace(transcript=_DIRTY_Q)) == _DIRTY_Q
    monkeypatch.setenv(POLISH_OFFLINE_ENV, "1")
    assert gm._turn_text(SimpleNamespace(transcript=_DIRTY_Q)) == _CLEAN_Q


def _seed_exchange(repo, question: str, answer: str, *, account: str = "acc-001") -> str:
    from bok_voice_core.types import CallMode, SessionManifest, TurnEvent

    n = next(_SEQ)
    cid = f"call-polish-{n}"
    obj = repo.create_object(account, {"display_name": "张三", "phone": "+85200000001"})
    repo.create_call(
        SessionManifest(
            session_id=cid,
            account_id=account,
            object_id=obj["id"],
            persona_id="",
            mode=CallMode.LIVE,
            direction="outbound",
            language="zh",
            providers={},
        )
    )
    ts = lambda k: f"2026-09-21T10:00:00.{k:06d}"  # noqa: E731 - 固定宽度单调戳
    repo.create_turn(
        TurnEvent(trace_id=cid, call_id=cid, turn_id=f"t{n}", role="user",
                  transcript=question, language="zh", created_at=ts(n),
                  line="a", speaker="customer")
    )
    repo.create_turn(
        TurnEvent(trace_id=cid, call_id=cid, turn_id=f"t{n + 1}", role="assistant",
                  transcript=answer, language="zh", created_at=ts(n + 1),
                  line="a", speaker="agent_ai", gen="llm", template_step=3)
    )
    return cid


def test_gap_mining_report_carries_polished_text(monkeypatch):
    """报告面（客户候选问法 + 样例答案）开档吃润色稿、关档逐字原样。"""
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    for _ in range(3):
        _seed_exchange(repo, _DIRTY_Q, _DIRTY_A)
    monkeypatch.delenv(POLISH_OFFLINE_ENV, raising=False)
    off = gm.build_llm_gap_report(repo, account_id="acc-001", min_calls=3)
    assert [g["customer_text"] for g in off["gaps"]] == [_DIRTY_Q]
    assert off["gaps"][0]["sample_answer"] == _DIRTY_A
    monkeypatch.setenv(POLISH_OFFLINE_ENV, "1")
    on = gm.build_llm_gap_report(repo, account_id="acc-001", min_calls=3)
    assert [g["customer_text"] for g in on["gaps"]] == [_CLEAN_Q]
    assert on["gaps"][0]["sample_answer"] == _CLEAN_A


def test_normalize_question_is_not_wired(monkeypatch):
    """运行时匹配面纯归一：开档下逐字不变（润色不得漏进 qa_gate 的匹配键）。"""
    monkeypatch.setenv(POLISH_OFFLINE_ENV, "1")
    assert normalize_question(_DIRTY_Q) == "呃你哋幾時送到"
    # 归一只剥标点：口水词仍在、口语数词不归一（有意——与运行时匹配必须同形）
    assert normalize_question("呃，两千三百塊") == "呃两千三百塊"


# ---------------------------------------------------------------- 结构级锚


def test_source_anchor_summarize_wires_render_transcript_only():
    """落点：润色只挂在 ``_render_transcript`` 的文本装配行上（喂 LLM 的那份）。"""
    assert 'line = f"{role}: {polish_offline_text(text)}"' in _SUMMARIZE_SRC
    call = _SUMMARIZE_SRC.index('line = f"{role}: {polish_offline_text(text)}"')
    assert _SUMMARIZE_SRC.index("def _render_transcript") < call
    # 只有一处调用（不散落）
    assert _SUMMARIZE_SRC.count("polish_offline_text(") == 1


def test_source_anchor_settlement_transcript_doc_is_raw_evidence():
    """原始证据面不接线：落盘 transcript.md 的 ``_write_settlement_docs`` 逐字不碰。"""
    doc_def = _MAIN_SRC.index("def _write_settlement_docs")
    doc_end = _MAIN_SRC.index("async def _write_distill_knowledge", doc_def)
    seg = _MAIN_SRC[doc_def:doc_end]
    assert "polish" not in seg
    assert 't.get("transcript")' in seg  # 仍是原话账本字段


def test_source_anchor_qa_mining_single_point_before_pairing():
    """落点：``mine_qa_pairs`` 入口一处映射，先于配对循环（问与答同一份润色稿）。"""
    body = _QA_TEXT_SRC.index("def mine_qa_pairs")
    mapped = _QA_TEXT_SRC.index('polish_offline_text(t.get("text") or "")', body)
    stats = _QA_TEXT_SRC.index("stats: dict[tuple[str, str], dict] = {}", mapped)
    assert stats > mapped
    assert _QA_TEXT_SRC.count("polish_offline_text(") == 1


def test_source_anchor_gap_mining_single_reader():
    """落点：本模块唯一取 transcript 的入口是 ``_turn_text``（单点，零散读=红线）。"""
    anchor = _GAP_SRC.index("def _turn_text(turn) -> str:")
    assert "polish_offline_text(" in _GAP_SRC[anchor:anchor + 900]
    assert _GAP_SRC.count('getattr(prev_customer, "transcript"') == 0
    assert _GAP_SRC.count("_turn_text(prev_customer)") == 1
    assert _GAP_SRC.count("_turn_text(t)") == 1
    # 模块内只有 _turn_text 一处读 transcript
    assert _GAP_SRC.count('"transcript"') == 1


def test_live_turn_path_never_imports_polish():
    """绝不进实时轮：A 线 agent worker 全树不 import/调用润色接线。"""
    pattern = re.compile(r"polish_offline|polish_wiring")
    agent_dir = ROOT / "apps" / "agent" / "agent_runtime"
    offenders = [
        str(p.relative_to(ROOT))
        for p in sorted(agent_dir.rglob("*.py"))
        if "__pycache__" not in p.parts and pattern.search(p.read_text("utf-8"))
    ]
    assert offenders == []


# ---------------------------------------------------------------- 立法面（env）


def test_env_face_is_cp_not_worker_face(monkeypatch, tmp_path):
    """kill-switch 走 CP 面、不进 agent worker 面（_FORWARD_ENV/passthrough）。"""
    assert POLISH_OFFLINE_ENV == "BOK_POLISH_OFFLINE"
    assert POLISH_OFFLINE_ENV not in bok._FORWARD_ENV
    assert POLISH_OFFLINE_ENV not in bok._BOK_PASSTHROUGH_KEYS
    monkeypatch.setenv(POLISH_OFFLINE_ENV, "1")
    passed = {}
    bok._apply_bok_passthrough_env(passed)  # worker 面透传
    assert POLISH_OFFLINE_ENV not in passed
    cp_env = bok._control_plane_env(tmp_path / "x.db")  # CP 面显式注入
    assert cp_env.get(POLISH_OFFLINE_ENV) == "1"


def test_env_face_absent_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv(POLISH_OFFLINE_ENV, raising=False)
    assert POLISH_OFFLINE_ENV not in bok._control_plane_env(tmp_path / "x.db")


# ---------------------------------------------------------------- 测量基线


def _r1_corpus() -> list[dict]:
    path = ROOT / "scripts" / ".r1_gold.20260921.json"
    if not path.exists():
        pytest.skip("R1 标注语料不在盘（scripts/.r1_gold.20260921.json）")
    return json.loads(path.read_text("utf-8"))


def test_r1_corpus_measurement_baseline(monkeypatch):
    """R1 标注语料 152 轮的实测读数（默认策略；改 polish.py 会动这里——正是目的）。

    读数含义：48/152 轮被改（31.6%）、1 轮润色稿被 Guard 判空回退原文、0 轮被
    其它原因拒。这两个数是「默认关」判断的量化依据之一（见文件头）。
    """
    from bok_voice_core.polish import polish_text

    monkeypatch.delenv(POLISH_OFFLINE_ENV, raising=False)
    corpus = _r1_corpus()
    assert len(corpus) == 152
    changed = 0
    rejected = 0
    reasons: dict[str, int] = {}
    for row in corpus:
        result = polish_text(row["txt"])
        if result.text != row["txt"]:
            changed += 1
        if not result.guard.accepted:
            rejected += 1
            reasons[result.guard.reason] = reasons.get(result.guard.reason, 0) + 1
    assert changed == 48
    assert rejected == 1 and reasons == {"empty": 1}
    # 接线层与本体同读数（关档不动、开档即本体输出）
    monkeypatch.setenv(POLISH_OFFLINE_ENV, "1")
    assert sum(1 for r in corpus if polish_offline_text(r["txt"]) != r["txt"]) == 48


def test_ascii_runs_never_collapsed(monkeypatch):
    """回归（2026-09-21 批次 6）：ASCII run 里的重复**永不折**——数字是数据不是口水词。

    原实现（批次 5）会把 ``MT3000 → MT30``、``soak111 → soak1``、``exceeded → exceed``
    折掉，而出口 Guard **拦不住**（粘着字母的数字串不产出硬保护 token）——真库实测
    632 条改动里含这类破坏。修法＝三枚重复正则都加 ASCII 两侧边界
    （``(?<![A-Za-z0-9])`` / ``(?![A-Za-z0-9])``）；中文侧的真重复折叠不受影响。
    """
    monkeypatch.setenv(POLISH_OFFLINE_ENV, "1")
    for src in ("MT3000 优惠多少", "soak111-738723", "exceeded the plan", "订单号 MT3000 已发出"):
        assert polish_offline_text(src) == src
    # 中文侧真重复照折（修复不能把功能一起折掉）
    assert polish_offline_text("我我我 想問下") == "我 想問下"
