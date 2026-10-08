"""W3b B 线四刀(2026-10-08,spec 命中率 4/108+回声重复 34 次+结巴照译+短残句被吞)。

四刀一档,全部可离线单测直喂:
- 刀1 spec busy 闸放宽:FIFO 深度门(BOK_INTERP_SPEC_BUSY_DEPTH)+frag hold 期
  不封(_mt_busy 置位后移)+fired/blocked 观测;
- 刀2 worker 本向回声去重(_InterpEchoDedup):自听回声(刚输出的译文被 ASR 再
  转写)≥0.85 丢弃+同文本 2s 窗重复 final 去重;
- 刀3 结巴清理:_fold_stutter 确定性重复字折叠(只进 MT 输入副本)+豆包
  enable_ddc 官方臂(默认关);
- 刀4 carry buffer:过短 FINAL(<6 内容字)暂存 ≤0.4s 等下段前插合并
  (_CarryBuffer,时钟/定时器注入)。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

INTERP_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "interpret.py").read_text(
    encoding="utf-8"
)
ENV_SRC = (ROOT / "tools" / "bokctl" / "env.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 刀1:spec busy 闸放宽
# ---------------------------------------------------------------------------


def test_spec_busy_depth_default_two(monkeypatch):
    from agent_runtime.interpret import _spec_busy_depth

    monkeypatch.delenv("BOK_INTERP_SPEC_BUSY_DEPTH", raising=False)
    assert _spec_busy_depth() == 2
    monkeypatch.setenv("BOK_INTERP_SPEC_BUSY_DEPTH", "1")
    assert _spec_busy_depth() == 1  # 1=旧「非空即封」档
    monkeypatch.setenv("BOK_INTERP_SPEC_BUSY_DEPTH", "0")
    assert _spec_busy_depth() == 1  # <1 钳 1(深度 0=永不禁封,非法)
    monkeypatch.setenv("BOK_INTERP_SPEC_BUSY_DEPTH", "garbage")
    assert _spec_busy_depth() == 2  # 坏值回缺省
    monkeypatch.setenv("BOK_INTERP_SPEC_BUSY_DEPTH", "3")
    assert _spec_busy_depth() == 3


def test_controller_fire_row_carries_counts_when_stats_given():
    """stats 注入:开火行带 fired/blocked 累计计数(W3b 观测);stats=None 时开火
    行逐字节同旧(零漂移铁律,由既有 spec 套件钉)。"""
    from agent_runtime.interpret import _SpecMtController, _SpecMtDetector, _SpecMtHold

    stats = {"fired": 0, "blocked": 5, "was": True}
    logs: list[str] = []
    ctl = _SpecMtController(
        enabled=True,
        detector=_SpecMtDetector(),
        hold=_SpecMtHold(),
        run_spec=lambda span: _done_pair(("", b"")),
        busy_gate=lambda: False,
        say_cached=lambda *a: None,
        enqueue=lambda rest: None,
        log=logs.append,
        stats=stats,
    )
    async def _run():
        # on_interim 内 create_task 需事件循环;sleep(0) 让 fire task 起跑
        # (_done_pair 的 Future 必须在环内创建——既有 spec 套件同款姿势)。
        ctl.on_interim("你好呀我想问一下，请问")
        ctl.on_interim("你好呀我想问一下，请问呢")
        await asyncio.sleep(0)

    asyncio.run(_run())
    fire_rows = [x for x in logs if "INTERP_SPEC fire" in x]
    assert len(fire_rows) == 1
    assert "fired=1" in fire_rows[0] and "blocked=5" in fire_rows[0]
    assert stats["fired"] == 1


def _done_pair(pair):
    fut = asyncio.Future()
    fut.set_result(pair)
    return fut


def test_busy_gate_wiring_source_pinned():
    """闸体三条铁律源级 pin:①FIFO 条件=深度门(qsize>=depth)非「非空」;
    ②mt_busy/source_drops_pending 两条件原样保留;③封锁 episode 首拍 busy 行;
    ④hold 不封=_mt_busy 置位点在 frag absorb 之后;⑤立法双面登记。"""
    assert "_src_q.qsize() >= depth" in INTERP_SRC
    assert '_mt_busy["flag"] or backlog.source_drops_pending' in INTERP_SRC
    assert "INTERP_SPEC busy depth=" in INTERP_SRC
    # hold 不封:置位点必须在 _frag_absorb 调用之后(真 MT 起跑才置位)。
    absorb_at = INTERP_SRC.index("await _frag_absorb(")
    busy_set_at = INTERP_SRC.index('_mt_busy["flag"] = True')
    assert busy_set_at > absorb_at
    # 立法双面:_FORWARD_ENV 表 + _interp_env B 线透传白名单同键。
    assert '"BOK_INTERP_SPEC_BUSY_DEPTH",' in ENV_SRC
    assert ENV_SRC.count('"BOK_INTERP_SPEC_BUSY_DEPTH"') >= 2


# ---------------------------------------------------------------------------
# 刀2:本向回声/重复去重(_InterpEchoDedup)
# ---------------------------------------------------------------------------


def test_echo_dedup_dup_final_window():
    """同归一文本窗内重复→drop;出窗→放行;标点/空白差异不改判定(_spec_norm)。"""
    from agent_runtime.interpret import _InterpEchoDedup

    d = _InterpEchoDedup(clock=lambda: 0.0)
    assert d.check("你好呀我想问一下。", now=100.0) == ""
    assert d.check("你好呀我想问一下！", now=101.5) == "dup-final"  # 归一后同文本
    assert d.check("另外一句完全不同。", now=102.0) == ""
    # 出窗(默认 8s)放行
    assert d.check("你好呀我想问一下。", now=108.5) == ""


def test_echo_dedup_self_heard_against_own_translations():
    """final ≈ 本向近期输出译文(≥0.85)→self-heard 丢弃(输出被自家输入再转写)。"""
    from agent_runtime.interpret import _InterpEchoDedup

    d = _InterpEchoDedup(clock=lambda: 0.0)
    assert (
        d.check("唔好意思呢句聽唔清楚", now=100.0, own_translations=("唔好意思，呢句聽唔清楚。",))
        == "self-heard"
    )
    # 低相似(不同内容)放行
    assert d.check("完全无关的另一句话", now=100.0, own_translations=("唔好意思，呢句聽唔清楚。",)) == ""


def test_echo_dedup_env_gates(monkeypatch):
    from agent_runtime import interpret

    monkeypatch.delenv("BOK_INTERP_ECHO_DEDUP", raising=False)
    monkeypatch.delenv("BOK_INTERP_ECHO_DUP_WINDOW_S", raising=False)
    assert interpret._echo_dedup_enabled() is True
    assert interpret._echo_dup_window_s() == 8.0
    monkeypatch.setenv("BOK_INTERP_ECHO_DEDUP", "0")
    assert interpret._echo_dedup_enabled() is False
    monkeypatch.setenv("BOK_INTERP_ECHO_DUP_WINDOW_S", "junk")
    monkeypatch.delenv("BOK_INTERP_ECHO_DEDUP", raising=False)
    assert interpret._echo_dup_window_s() == 8.0
    monkeypatch.setenv("BOK_INTERP_ECHO_DUP_WINDOW_S", "-2")
    assert interpret._echo_dup_window_s() == 0.0  # 负数钳 0=判重窗全关


def test_echo_dedup_wiring_pins():
    assert "INTERP_ECHO_DROP reason=" in INTERP_SRC
    assert "_echo_dedup.check(" in INTERP_SRC
    assert "_echo_dedup_enabled()" in INTERP_SRC
    assert "_echo_dedup = _InterpEchoDedup()" in INTERP_SRC


# ---------------------------------------------------------------------------
# 刀3:结巴折叠(_fold_stutter)+豆包官方三臂
# ---------------------------------------------------------------------------


def test_fold_stutter_single_char_run():
    from agent_runtime.interpret import _fold_stutter

    assert _fold_stutter("我我我我我，要不要？") == "我，要不要？"
    assert _fold_stutter("普通句子没有结巴。") == "普通句子没有结巴。"
    # 拉丁/数字 run 绝不折叠(型号/编号风险)
    assert _fold_stutter("AAAA123") == "AAAA123"


def test_fold_stutter_pair_repeat():
    from agent_runtime.interpret import _fold_stutter

    assert _fold_stutter("要不要要不要要不要去广州") == "要不要去广州"
    # 两次重复(<3)不折叠——正常强调语气保留
    assert _fold_stutter("要不要要不要去") == "要不要要不要去"


def test_fold_stutter_gate_and_wiring(monkeypatch):
    from agent_runtime import interpret

    monkeypatch.delenv("BOK_INTERP_STUTTER_FIX", raising=False)
    assert interpret._stutter_fix_enabled() is True
    monkeypatch.setenv("BOK_INTERP_STUTTER_FIX", "0")
    assert interpret._stutter_fix_enabled() is False
    # 接线:折叠在 polish 之后(MT 输入副本链上),原文单轨不触碰
    assert "text_mt = _fold_stutter(text_mt)" in INTERP_SRC
    polish_at = INTERP_SRC.index("text_mt = _polish_for_mt(text, source_lang)")
    fold_at = INTERP_SRC.index("text_mt = _fold_stutter(text_mt)")
    assert fold_at > polish_at


def test_doubao_official_arms_default_off(monkeypatch):
    """官方三臂默认全关=请求体逐字节旧形状;开臂才见对应键。"""
    from agent_runtime.providers.doubao_asr import DoubaoSTT

    for k in ("BOK_DOUBAO_NONSTREAM", "BOK_DOUBAO_DDC", "BOK_DOUBAO_FIRST_TOKEN_BOOST"):
        monkeypatch.delenv(k, raising=False)
    stt = DoubaoSTT(api_key="k")
    req = stt._config()["request"]
    assert "enable_nonstream" not in req and "enable_ddc" not in req
    assert "enable_accelerate_text" not in req

    monkeypatch.setenv("BOK_DOUBAO_NONSTREAM", "1")
    monkeypatch.setenv("BOK_DOUBAO_DDC", "1")
    monkeypatch.setenv("BOK_DOUBAO_FIRST_TOKEN_BOOST", "1")
    req2 = DoubaoSTT(api_key="k")._config()["request"]
    assert req2["enable_nonstream"] is True
    assert req2["enable_ddc"] is True
    assert req2["enable_accelerate_text"] is True and req2["accelerate_score"] == 3
