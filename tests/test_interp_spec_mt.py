"""B 线 interim 投机翻译(prewarm-and-confirm,2026-10-06)。

Ethan 指令:B 线反应必须快过 A 线——MT 往返+TTS 首包在说话期间预付,final 到
达时译文即声。本档钉住:

- 纯函数面:kill-switch 读法、子句前缀抽取、内容字数、归一化、final↔span
  前缀比对与余段切分;
- `_SpecMtDetector` 触发矩阵:边界类型/字数地板/2 次 interim 稳定/≥500ms 稳定/
  数字串否决/再开火 ≥6 字增长/每段 ≤2 预算/切段重置/坐标系重置(本地尾巴流);
- `_SpecMtController` 行为:kill-switch=0 零动作、busy 闸让路、HIT(cached play
  +余段入队+账本 FIFO 配对)、MISS(未就绪 cancel/sim 不足弃)、单飞顶替、
  cancel 卫生;
- entrypoint 接线源级 pin:interim 喂 detector、final 过确认、真 MT busy 旗、
  shutdown 收线、INTERP_SPEC 观测行、`_FORWARD_ENV`/`_interp_env` 双面登记。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.interpret import (  # noqa: E402
    _spec_clause_prefix,
    _spec_content_chars,
    _spec_defer_max_s,
    _spec_mt_enabled,
    _spec_norm,
    _spec_prefix_split,
    _spec_refire_chars,
    _SpecMtController,
    _SpecMtDetector,
    _SpecMtHold,
)

INTERP_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "interpret.py").read_text(
    encoding="utf-8"
)
ENV_SRC = (ROOT / "tools" / "bokctl" / "env.py").read_text(encoding="utf-8")
AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(
    encoding="utf-8"
)


# ---------------------------------------------------------------------------
# 纯函数面
# ---------------------------------------------------------------------------


def test_spec_mt_enabled_default_on_and_zero_off(monkeypatch):
    monkeypatch.delenv("BOK_INTERP_SPEC_MT", raising=False)
    assert _spec_mt_enabled() is True
    monkeypatch.setenv("BOK_INTERP_SPEC_MT", "0")
    assert _spec_mt_enabled() is False


def test_spec_clause_prefix_boundary_types():
    # 全角逗号/顿号/分号/句号/问叹号 + 半角同族,全收;无边界=None。
    assert _spec_clause_prefix("你好呀我想问一下，请问") == "你好呀我想问一下，"
    assert _spec_clause_prefix("包裹多少公斤、多重") == "包裹多少公斤、"
    assert _spec_clause_prefix("first half; second") == "first half;"
    assert _spec_clause_prefix("结束了。后面") == "结束了。"
    assert _spec_clause_prefix("真的吗！我不信") == "真的吗！"
    assert _spec_clause_prefix("what? yes") == "what?"
    assert _spec_clause_prefix("没有任何边界标点") is None
    assert _spec_clause_prefix("") is None


def test_spec_content_chars_strips_punct_only():
    assert _spec_content_chars("你好，世界！") == 4
    assert _spec_content_chars("abc, def") == 6
    assert _spec_content_chars("，。！") == 0


def test_detector_threshold_counts_len_incl_boundary_punct():
    """字数门口径=提交闸同款 len 含边界标点(call-21739d55 第 6 句实证:「坐
    地铁到啊，」5 正字+1 逗号——提交闸放行、投机闸也须放行;旧剥标点口径
    数 5=同一子句「提交了却不投机」,碎片照样付全价 MT)。"""
    det = _SpecMtDetector(clock=lambda: 100.0)
    assert det.feed("坐地铁到啊，", now=100.0) is None  # 首见
    assert det.feed("坐地铁到啊，广州去玩", now=100.2) == "坐地铁到啊，"  # 第二次目击即开火
    # 纯标点+短内容仍挡:「好的，」len 3 < 6
    det2 = _SpecMtDetector(clock=lambda: 100.0)
    assert det2.feed("好的，", now=100.0) is None
    assert det2.feed("好的，请问", now=100.2) is None


def test_spec_norm_mirrors_ticket_norm_shape():
    assert _spec_norm("你好，世界！ A") == "你好世界a"
    assert _spec_norm("") == ""


def test_spec_prefix_split_exact_prefix_cuts_remainder():
    final = "你好呀我想问一下，请问你们这个集运怎么收费的"
    span = "你好呀我想问一下，"
    sim, rest = _spec_prefix_split(final, span)
    assert sim >= 0.99
    assert rest == "请问你们这个集运怎么收费的"


def test_spec_prefix_split_no_remainder_when_equal():
    sim, rest = _spec_prefix_split("你好呀我想问一下，", "你好呀我想问一下，")
    assert sim >= 0.99
    assert rest == ""


def test_spec_prefix_split_tolerates_one_char_fix():
    # ASR 对 span 尾部个别字的修正:相似度落 0.85-1.0 区间,切点按归一长度锚。
    final = "你好呀我想问一下，请问几块钱"  # final 首段与 span 同长
    span = "你好呀我想问一下，"  # 完全一致 → HIT
    sim, rest = _spec_prefix_split(final, span)
    assert sim >= 0.85
    assert rest == "请问几块钱"
    # 一字之差(修正):相似度仍过门,余段照切。
    sim2, rest2 = _spec_prefix_split("你好呀我想问壹下，请问几块钱", "你好呀我想问一下，")
    assert sim2 >= 0.85
    assert rest2 == "请问几块钱"


def test_spec_prefix_split_mismatch_returns_low_sim():
    sim, rest = _spec_prefix_split("完全不同的一句话内容呀", "你好呀我想问一下，")
    assert sim < 0.85
    assert rest == ""


def test_spec_prefix_split_final_shorter_than_span_never_cuts():
    sim, rest = _spec_prefix_split("你好", "你好呀我想问一下，")
    assert rest == ""


# ---------------------------------------------------------------------------
# _SpecMtDetector:触发矩阵
# ---------------------------------------------------------------------------


def _mk_detector(now: list[float] | None = None) -> _SpecMtDetector:
    clock = (lambda: now[-1]) if now else (lambda: 100.0)
    return _SpecMtDetector(clock=clock), (now if now is not None else [100.0])


def test_detector_needs_two_interims_or_500ms():
    det, now = _mk_detector()
    # 第 1 次 interim:候选刚出现,既没 2 次也没 500ms → 不开火。
    assert det.feed("你好呀我想问一下，请问", now=100.0) is None
    # 第 2 次(候选未变)→ 稳定 → 开火。
    assert det.feed("你好呀我想问一下，请问你们", now=100.2) == "你好呀我想问一下，"
    det.reset_segment()
    # 单次 interim 但 ≥500ms → 时间稳定性路径同样开火。
    det2, now2 = _mk_detector()
    assert det2.feed("你好呀我想问一下，请问", now=100.0) is None
    assert det2.feed("你好呀我想问一下，请问", now=100.6) == "你好呀我想问一下，"


def test_detector_char_floor_six_content_chars():
    det, now = _mk_detector()
    assert det.feed("好的，", now=100.0) is None  # 内容 2 字 < 6
    assert det.feed("好的，请问一下", now=100.2) is None  # 「好的，」仍 <6
    assert det.feed("好的呀我想问一下，请问", now=100.4) is None  # 新候选首见
    assert det.feed("好的呀我想问一下，请问你们", now=100.6) == "好的呀我想问一下，"


def test_detector_digit_run_veto():
    det, now = _mk_detector()
    # 数字串 ≥4 位(镜像 flow._digit_runs_in 族)永不投机。
    assert det.feed("我的单号是78901234，麻烦查下", now=100.0) is None
    assert det.feed("运费七八九零一二三四，怎么算", now=100.2) is None


def test_detector_refire_requires_growth_and_budget():
    det, now = _mk_detector()
    assert det.feed("你好呀我想问一下，请问", now=100.0) is None
    first = det.feed("你好呀我想问一下，请问你们", now=100.2)
    assert first == "你好呀我想问一下，"
    # 同候选没长进 → 不再开火(增长门)。
    assert det.feed("你好呀我想问一下，请问你们", now=100.4) is None
    # 长进 <6 字 → 不开火。
    assert det.feed("你好呀我想问一下，请问你们的", now=100.6) is None
    # 长进 ≥6 字:新候选先过稳定门(身份变化=稳定性重计),再开火(预算 2 内)。
    assert det.feed("你好呀我想问一下，请问你们这个集运怎么收费，", now=100.8) is None
    second = det.feed("你好呀我想问一下，请问你们这个集运怎么收费，", now=101.0)
    assert second == "你好呀我想问一下，请问你们这个集运怎么收费，"
    # 预算烧完(每段 ≤2)→ 再长也不开火。
    assert (
        det.feed("你好呀我想问一下，请问你们这个集运怎么收费，然后呢我想知道", now=101.2)
        is None
    )


def test_detector_reset_segment_restores_budget():
    det, now = _mk_detector()
    assert det.feed("你好呀我想问一下，请问", now=100.0) is None
    assert det.feed("你好呀我想问一下，请问你们", now=100.2) is not None
    assert det.feed("你好呀我想问一下，请问你们这个集运怎么收费，", now=100.4) is None
    assert det.feed("你好呀我想问一下，请问你们这个集运怎么收费，", now=100.6) is not None
    assert det.feed("再长也不行了吧啦吧啦吧啦，", now=100.8) is None
    assert det.feed("再长也不行了吧啦吧啦吧啦，", now=101.0) is None  # 预算尽
    det.reset_segment()
    assert det.feed("新的一句从这里开始说，后面", now=101.4) is None  # 新段首见
    assert det.feed("新的一句从这里开始说，后面", now=101.6) is not None  # 预算重置


def test_detector_coordinate_reset_reearns_stability():
    """本地 ASR 尾巴流:句级提交后 interim 坐标系重置(前缀断裂)——候选身份
    变化必须重计稳定性,绝不拿旧稳定度给新 span 开火。"""
    det, now = _mk_detector()
    assert det.feed("第一句子句已经讲完了，", now=100.0) is None
    assert det.feed("第一句子句已经讲完了，", now=100.2) is not None
    det.reset_segment()
    # 坐标系重置:新的尾巴流与旧候选无公共前缀 → 第 1 次不见 2 次稳定不开火。
    assert det.feed("全新尾巴", now=100.4) is None


# ---------------------------------------------------------------------------
# _SpecMtController:行为面
# ---------------------------------------------------------------------------


class _FakeLoop:
    """最小事件循环替身:create_task 用真 asyncio,日志收列表。"""

    def __init__(self):
        self.logs: list[str] = []

    def log(self, line: str) -> None:
        self.logs.append(line)


def _mk_controller(
    *,
    enabled=True,
    run_spec=None,
    busy_gate=lambda: False,
    say_calls=None,
    enqueue_calls=None,
    detector=None,
    spec_wait_s=None,
    defer_max_s=None,
    refire_chars=None,
):
    loop = _FakeLoop()
    hold = _SpecMtHold()
    ctl = _SpecMtController(
        enabled=enabled,
        detector=detector or _SpecMtDetector(),
        hold=hold,
        run_spec=run_spec or (lambda span: _done(("", b""))),
        busy_gate=busy_gate,
        say_cached=lambda final_src, text, pcm: say_calls.append((final_src, text, pcm)),
        enqueue=lambda rest: enqueue_calls.append(rest),
        log=loop.log,
        spec_wait_s=spec_wait_s,
        defer_max_s=defer_max_s,
        refire_chars=refire_chars,
    )
    return ctl, hold, loop


def _done(pair):
    fut = asyncio.Future()
    fut.set_result(pair)
    return fut


def test_controller_disabled_zero_actions(monkeypatch):
    """kill-switch=0:interim 不开火、final 走旧路径(False),slot 恒空。"""
    monkeypatch.setenv("BOK_INTERP_SPEC_MT", "0")
    assert _spec_mt_enabled() is False
    say_calls: list = []
    enqueue_calls: list = []
    ctl, hold, loop = _mk_controller(
        enabled=False, say_calls=say_calls, enqueue_calls=enqueue_calls
    )
    ctl.on_interim("你好呀我想问一下，请问你们这个集运怎么收费，")
    assert hold.src == "" and hold.task is None
    assert ctl.on_final("你好呀我想问一下，请问你们这个集运怎么收费的") is False
    assert say_calls == [] and enqueue_calls == []
    assert loop.logs == []


def test_controller_busy_gate_skips_fire():
    ctl, hold, loop = _mk_controller(busy_gate=lambda: True)
    ctl.on_interim("你好呀我想问一下，请问")
    assert hold.src == "" and hold.task is None
    assert loop.logs == []


def _fire_ctl(ctl, *, base="你好呀我想问一下，请问"):
    """同候选喂两次(2 次 interim 稳定)触发开火;返回开火 span。"""
    ctl.on_interim(base)
    ctl.on_interim(base + "呢")
    return base + "，"


def test_controller_hit_plays_cached_and_enqueues_remainder():
    """HIT:前缀比对过门 → say_cached 收 (final_src, 译文, pcm),余段入队,
    账本 FIFO 配对次序=投句(final 全文)→兜底 done_mt(0)→余段 note_src。"""
    from agent_runtime.interpret import _LagLedger

    say_calls: list = []
    enqueue_calls: list = []
    ledger = _LagLedger()
    ctl, hold, loop = _mk_controller(
        run_spec=lambda span: _done(("hello there", b"PCM")),
        say_calls=say_calls,
        enqueue_calls=enqueue_calls,
    )

    # 替换 say_cached/enqueue 注入账本配对(镜像 entrypoint _spec_say_cached/
    # _spec_enqueue 纪律:先 say 后 note_src/done_mt(0);余段入队即 note_src)。
    def _say(final_src, text, pcm):
        say_calls.append((final_src, text, pcm))
        ledger.note_src(final_src)
        ledger.done_mt(0)

    ctl._say_cached = _say
    ctl._enqueue = lambda rest: (enqueue_calls.append(rest), ledger.note_src(rest))

    async def main():
        # 开火 → run_spec 立即就绪(译文+PCM 预热完成)。
        _fire_ctl(ctl)
        await asyncio.sleep(0.01)  # 让 _fire 跑完(跨两个 loop tick)
        assert hold.text == "hello there" and hold.pcm == b"PCM"
        # final 到达 → 确认 HIT。
        played = ctl.on_final("你好呀我想问一下，请问你们这个集运怎么收费的")
        assert played is True
        # 账本 FIFO 次序:投机对(final 全文,mt_ms=0)已 say+done_mt 进 pending,
        # 余段对在 src 队头等它的 MT——与 item 到达序(投机译文先,余段后)一一对应。
        assert ledger._pending and ledger._pending[0][1] == len(
            "你好呀我想问一下，请问你们这个集运怎么收费的"
        )
        assert ledger._pending[0][2] == 0
        assert ledger._src and ledger._src[0][1] == len("请问你们这个集运怎么收费的")

    asyncio.run(main())
    assert len(say_calls) == 1
    assert say_calls[0][0] == "你好呀我想问一下，请问你们这个集运怎么收费的"
    assert say_calls[0][1] == "hello there" and say_calls[0][2] == b"PCM"
    assert enqueue_calls == ["请问你们这个集运怎么收费的"]
    hit_rows = [x for x in loop.logs if "INTERP_SPEC hit" in x]
    assert len(hit_rows) == 1 and "rest=" in hit_rows[0]


def test_controller_miss_not_ready_cancels_inflight():
    """final 到达时 MT/TTS 未就绪且 C2 关档(spec_wait_s=0,旧行为)→ cancel
    在途任务、slot 弃、say 不调、旧路径。"""
    say_calls: list = []
    enqueue_calls: list = []

    async def slow_run(span):
        await asyncio.sleep(30)  # 永不就绪(被 cancel 打断)
        return ("x", b"y")

    ctl, hold, loop = _mk_controller(run_spec=slow_run, say_calls=say_calls,
                                     enqueue_calls=enqueue_calls, spec_wait_s=0.0)

    async def main():
        _fire_ctl(ctl)
        task = hold.task
        assert task is not None and not task.done()
        await asyncio.sleep(0.01)  # 让 _fire 进入 await(取消才走 except abort 行)
        assert ctl.on_final("你好呀我想问一下，请问你们这个集运怎么收费的") is False
        await asyncio.sleep(0.01)
        assert task.cancelled()
        assert say_calls == [] and enqueue_calls == []
        assert hold.src == "" and hold.text == "" and hold.pcm == b""

    asyncio.run(main())
    assert any("INTERP_SPEC miss" in x and "not_ready" in x for x in loop.logs)
    assert any("INTERP_SPEC abort" in x for x in loop.logs)


# ---- C2:必中体有界延迟交付(2026-10-08 时效波) ----


def test_controller_defer_hit_delivers_when_task_lands():
    """not_ready 但 sim 过门=必中:等在途合成落地 → HIT 零合成直播(延迟档)。
    final 即刻回 True(调用方跳过正常路径),交付由 done_callback 完成。"""
    say_calls: list = []
    enqueue_calls: list = []

    async def slow_run(span):
        await asyncio.sleep(0.08)  # 晚一点落地(模拟 PCM 排干赶不上 final)
        return ("hello there", b"PCM")

    ctl, hold, loop = _mk_controller(run_spec=slow_run, say_calls=say_calls,
                                     enqueue_calls=enqueue_calls, spec_wait_s=0.5)

    async def main():
        _fire_ctl(ctl)
        assert hold.task is not None and not hold.task.done()
        await asyncio.sleep(0.01)
        # final 到达:slot 未就绪但必中 → 延迟交付(回 True)。
        assert ctl.on_final("你好呀我想问一下，请问你们这个集运怎么收费的") is True
        assert say_calls == []  # 尚未播(等落地)
        await asyncio.sleep(0.2)  # 过 0.08s 落地点
        assert say_calls and say_calls[0][1] == "hello there"
        assert enqueue_calls == ["请问你们这个集运怎么收费的"]  # 余段照排
        assert ctl._deferred is None

    asyncio.run(main())
    assert any("INTERP_SPEC defer " in x for x in loop.logs)
    assert any("INTERP_SPEC hit" in x and "deferred=1" in x for x in loop.logs)


def test_controller_defer_fallback_enqueues_on_timeout():
    """必中体等满上限仍未落地 → 兜底正常入队(final 全文),零播放。"""
    say_calls: list = []
    enqueue_calls: list = []

    async def never_run(span):
        await asyncio.sleep(30)
        return ("x", b"y")

    ctl, hold, loop = _mk_controller(run_spec=never_run, say_calls=say_calls,
                                     enqueue_calls=enqueue_calls, spec_wait_s=0.05)

    async def main():
        _fire_ctl(ctl)
        await asyncio.sleep(0.01)
        assert ctl.on_final("你好呀我想问一下，请问你们这个集运怎么收费的") is True
        await asyncio.sleep(0.3)  # 过 0.05s 兜底点
        assert say_calls == []
        assert enqueue_calls == ["你好呀我想问一下，请问你们这个集运怎么收费的"]
        assert ctl._deferred is None
        assert hold.task is None

    asyncio.run(main())
    assert any("INTERP_SPEC defer-fallback" in x for x in loop.logs)


def test_controller_defer_blocks_new_fires_till_settled():
    """延迟交付在途=on_interim 不开火(防 hold 被新 span 顶掉竞态)。"""
    say_calls: list = []
    enqueue_calls: list = []

    async def slow_run(span):
        await asyncio.sleep(0.1)
        return ("hello there", b"PCM")

    ctl, hold, loop = _mk_controller(run_spec=slow_run, say_calls=say_calls,
                                     enqueue_calls=enqueue_calls, spec_wait_s=0.5)

    async def main():
        _fire_ctl(ctl)
        await asyncio.sleep(0.01)
        assert ctl.on_final("你好呀我想问一下，请问你们这个集运怎么收费的") is True
        # 在途期:新 interim 全部哑火。
        ctl.on_interim("全新的另一句话呀，")
        assert hold.task is None and hold.src == ""
        await asyncio.sleep(0.25)
        assert say_calls  # 原必中体照常交付

    asyncio.run(main())


# ---- S2: defer 窗口自适应续窗+增 prefix 再投机(2026-10-09,call-4322e14d) ----


def test_spec_defer_max_s_reader(monkeypatch):
    """硬帽秒读数:缺省 8.0;坏值回缺省;负数钳 0(=续窗关)。"""
    monkeypatch.delenv("BOK_INTERP_SPEC_DEFER_MAX_S", raising=False)
    assert _spec_defer_max_s() == 8.0
    monkeypatch.setenv("BOK_INTERP_SPEC_DEFER_MAX_S", "5")
    assert _spec_defer_max_s() == 5.0
    monkeypatch.setenv("BOK_INTERP_SPEC_DEFER_MAX_S", "abc")
    assert _spec_defer_max_s() == 8.0
    monkeypatch.setenv("BOK_INTERP_SPEC_DEFER_MAX_S", "-1")
    assert _spec_defer_max_s() == 0.0
    monkeypatch.setenv("BOK_INTERP_SPEC_DEFER_MAX_S", "0")
    assert _spec_defer_max_s() == 0.0


def test_spec_refire_chars_reader(monkeypatch):
    """再投机门槛读数:缺省 6(镜像 detector REFIRE_GROWTH_CHARS);坏值回缺省;负数钳 0。"""
    monkeypatch.delenv("BOK_INTERP_SPEC_REFIRE_CHARS", raising=False)
    assert _spec_refire_chars() == 6
    monkeypatch.setenv("BOK_INTERP_SPEC_REFIRE_CHARS", "3")
    assert _spec_refire_chars() == 3
    monkeypatch.setenv("BOK_INTERP_SPEC_REFIRE_CHARS", "abc")
    assert _spec_refire_chars() == 6
    monkeypatch.setenv("BOK_INTERP_SPEC_REFIRE_CHARS", "-2")
    assert _spec_refire_chars() == 0


def test_controller_defer_window_renews_on_prefix_growth():
    """①说话中 interim 持续增长→窗口滚动不 defer-fallback→任务落地 HIT。

    固定窗(0.15s)远早于任务落地(0.45s)——旧行为此处必 defer-fallback;续窗
    每次增长重置到 now+wait,必中体等满落地=零合成直播。"""
    say_calls: list = []
    enqueue_calls: list = []

    async def slow_run(span):
        await asyncio.sleep(0.45)  # 落地晚于固定窗
        return ("hello there", b"PCM")

    ctl, hold, loop = _mk_controller(
        run_spec=slow_run, say_calls=say_calls, enqueue_calls=enqueue_calls,
        spec_wait_s=0.15, defer_max_s=5.0, refire_chars=6,
    )
    final = "你好呀我想问一下，请问你们这个集运怎么收费的"

    async def main():
        _fire_ctl(ctl)
        await asyncio.sleep(0.01)
        assert ctl.on_final(final) is True  # defer 已武装
        # 增长 interim(无新边界标点→候选不前进→只续窗不替槽)。
        growth = [
            "你好呀我想问一下，请问你们",
            "你好呀我想问一下，请问你们的",
            "你好呀我想问一下，请问你们的这个",
            "你好呀我想问一下，请问你们的这个集运",
            "你好呀我想问一下，请问你们的这个集运怎么",
        ]
        for txt in growth:
            await asyncio.sleep(0.07)  # 末拍 0.35s 落在 0.15s 固定窗之外——续窗救活
            ctl.on_interim(txt)
        await asyncio.sleep(0.2)  # 过 0.45s 落地点
        assert say_calls and say_calls[0][1] == "hello there"
        assert say_calls[0][0] == final
        assert enqueue_calls == ["请问你们这个集运怎么收费的"]  # 余段照排
        assert ctl._deferred is None

    asyncio.run(main())
    assert any("INTERP_SPEC hit" in x and "deferred=1" in x for x in loop.logs)
    assert not any("defer-fallback" in x for x in loop.logs)
    assert not any("refire" in x for x in loop.logs)  # 候选未长进:只续窗


def test_controller_defer_growth_stall_falls_back_with_expire_row():
    """②增长停滞无 final→照旧 defer-fallback,且打 defer-expire(grew=0=final 真丢)。"""
    say_calls: list = []
    enqueue_calls: list = []

    async def never_run(span):
        await asyncio.sleep(30)
        return ("x", b"y")

    ctl, hold, loop = _mk_controller(
        run_spec=never_run, say_calls=say_calls, enqueue_calls=enqueue_calls,
        spec_wait_s=0.08, defer_max_s=8.0, refire_chars=6,
    )
    final = "你好呀我想问一下，请问你们这个集运怎么收费的"

    async def main():
        _fire_ctl(ctl)
        await asyncio.sleep(0.01)
        assert ctl.on_final(final) is True
        # 停滞期 interim(新句起步,不以 held 前缀开头)=不续窗,窗口照旧节奏。
        ctl.on_interim("全新的另一句话呀，")
        await asyncio.sleep(0.3)  # 过 0.08s 兜底点
        assert say_calls == []
        assert enqueue_calls == [final]
        assert ctl._deferred is None
        assert hold.task is None

    asyncio.run(main())
    expire_rows = [x for x in loop.logs if "INTERP_SPEC defer-expire" in x]
    assert len(expire_rows) == 1 and "grew=0" in expire_rows[0]
    assert any("INTERP_SPEC defer-fallback" in x for x in loop.logs)


def test_controller_defer_growth_refires_and_replaces_slot():
    """③增长 ≥6 内容字触发再投机替槽:旧任务 cancel(身份守卫不结账)、新 span
    接管 slot;新任务落地=HIT 播新译文(旧前缀译文相似度风险消除)。"""
    say_calls: list = []
    enqueue_calls: list = []
    spans: list[str] = []

    async def scripted_run(span):
        spans.append(span)
        if len(spans) == 1:
            await asyncio.sleep(0.5)  # 首投机:慢(将被再投机顶掉)
            return ("T[旧]", b"OLD")
        await asyncio.sleep(0.03)  # 再投机:快落地
        return ("T[新]", b"NEW")

    ctl, hold, loop = _mk_controller(
        run_spec=scripted_run, say_calls=say_calls, enqueue_calls=enqueue_calls,
        spec_wait_s=0.6, defer_max_s=10.0, refire_chars=6,
    )
    final = "你好呀我想问一下，请问你们这个集运怎么收费的"

    async def main():
        _fire_ctl(ctl)
        await asyncio.sleep(0.01)
        assert ctl.on_final(final) is True
        await asyncio.sleep(0.02)
        # 增长 ≥6 内容字且带新边界标点:候选前进→替槽。
        ctl.on_interim("你好呀我想问一下，请问你们这个集运怎么收费，帮我看看")
        assert hold.src == "你好呀我想问一下，请问你们这个集运怎么收费，"
        await asyncio.sleep(0.02)  # 让新投机任务起跑(scripted_run 记录 span)
        assert spans == ["你好呀我想问一下，", "你好呀我想问一下，请问你们这个集运怎么收费，"]
        await asyncio.sleep(0.25)  # 新任务(0.03s)落地
        assert say_calls and say_calls[0][1] == "T[新]" and say_calls[0][2] == b"NEW"
        assert say_calls[0][0] == final  # 播出锚仍是 defer 时的 final(配对不换)
        assert enqueue_calls == ["请问你们这个集运怎么收费的"]
        assert ctl._deferred is None

    asyncio.run(main())
    assert any("INTERP_SPEC refire" in x for x in loop.logs)
    assert any("INTERP_SPEC hit" in x and "deferred=1" in x for x in loop.logs)
    assert not any("defer-fallback" in x for x in loop.logs)
    assert any("INTERP_SPEC abort" in x for x in loop.logs)  # 旧投机被顶掉(cancel)


def test_controller_defer_hard_cap_expires_with_grew_row():
    """④续窗到硬帽→fallback(defer-expire 带 grew>0=说话没停等到帽)。"""
    say_calls: list = []
    enqueue_calls: list = []

    async def never_run(span):
        await asyncio.sleep(30)
        return ("x", b"y")

    ctl, hold, loop = _mk_controller(
        run_spec=never_run, say_calls=say_calls, enqueue_calls=enqueue_calls,
        spec_wait_s=0.5, defer_max_s=0.25, refire_chars=6,
    )
    final = "你好呀我想问一下，请问你们这个集运怎么收费的"

    async def main():
        _fire_ctl(ctl)
        await asyncio.sleep(0.01)
        assert ctl.on_final(final) is True
        await asyncio.sleep(0.1)  # age 0.1 < cap 0.25:续窗 remaining=min(0.5, 0.15)
        ctl.on_interim("你好呀我想问一下，请问你们")  # grew=4(归一差)
        await asyncio.sleep(0.4)  # 过 cap(0.25)兜底点——固定窗(0.5)还没到
        assert enqueue_calls == [final]  # 硬帽到期兜底(非固定窗到期)
        assert ctl._deferred is None

    asyncio.run(main())
    expire_rows = [x for x in loop.logs if "INTERP_SPEC defer-expire" in x]
    assert len(expire_rows) == 1 and "grew=4" in expire_rows[0]
    assert any("INTERP_SPEC defer-fallback" in x for x in loop.logs)


def test_controller_defer_growth_past_cap_expires_immediately():
    """④b 增长事件到来时已过硬帽(计时器竞态窗):就地过期同步结账,不等下一拍。

    正常节奏下硬帽计时器先响(首窗/续窗都被 cap 封顶),本支只在「增长事件与
    帽点同拍竞态」时可达——白盒拨 born 模拟,钉的是同步结账不挂下一拍。"""
    say_calls: list = []
    enqueue_calls: list = []

    async def never_run(span):
        await asyncio.sleep(30)
        return ("x", b"y")

    ctl, hold, loop = _mk_controller(
        run_spec=never_run, say_calls=say_calls, enqueue_calls=enqueue_calls,
        spec_wait_s=5.0, defer_max_s=0.2, refire_chars=6,
    )
    final = "你好呀我想问一下，请问你们这个集运怎么收费的"

    async def main():
        _fire_ctl(ctl)
        await asyncio.sleep(0.01)
        assert ctl.on_final(final) is True
        ctl._deferred["born"] -= 1.0  # 竞态姿势:增长事件到时龄已过硬帽(计时器未及处理)
        ctl.on_interim("你好呀我想问一下，请问你们")
        assert enqueue_calls == [final]  # 增长事件当场过期(零额外等待)
        assert ctl._deferred is None

    asyncio.run(main())
    assert any("INTERP_SPEC defer-expire" in x and "grew=4" in x for x in loop.logs)


def test_controller_defer_max_zero_disables_renewal_family():
    """DEFER_MAX_S=0=续窗整族关:增长事件不续窗不替槽,回 C2 固定窗(旧行为逐字节)。"""
    say_calls: list = []
    enqueue_calls: list = []

    async def never_run(span):
        await asyncio.sleep(30)
        return ("x", b"y")

    ctl, hold, loop = _mk_controller(
        run_spec=never_run, say_calls=say_calls, enqueue_calls=enqueue_calls,
        spec_wait_s=0.1, defer_max_s=0.0, refire_chars=6,
    )
    final = "你好呀我想问一下，请问你们这个集运怎么收费的"

    async def main():
        _fire_ctl(ctl)
        await asyncio.sleep(0.01)
        assert ctl.on_final(final) is True
        ctl.on_interim("你好呀我想问一下，请问你们这个集运怎么收费，帮我看看")  # 增长也被无视
        assert hold.src == ""  # 不替槽
        await asyncio.sleep(0.3)  # 固定窗 0.1s 到期
        assert enqueue_calls == [final]

    asyncio.run(main())
    assert not any("refire" in x for x in loop.logs)
    assert any("INTERP_SPEC defer-fallback" in x for x in loop.logs)
    assert any("INTERP_SPEC defer-expire" in x and "grew=0" in x for x in loop.logs)


def test_controller_defer_initial_window_capped_by_defer_max():
    """硬帽同时封顶首窗:cap < wait 时首到期=cap(总 defer 龄恒有界)。"""
    say_calls: list = []
    enqueue_calls: list = []

    async def never_run(span):
        await asyncio.sleep(30)
        return ("x", b"y")

    ctl, hold, loop = _mk_controller(
        run_spec=never_run, say_calls=say_calls, enqueue_calls=enqueue_calls,
        spec_wait_s=1.0, defer_max_s=0.15, refire_chars=6,
    )
    final = "你好呀我想问一下，请问你们这个集运怎么收费的"

    async def main():
        _fire_ctl(ctl)
        await asyncio.sleep(0.01)
        assert ctl.on_final(final) is True
        await asyncio.sleep(0.35)  # 过 cap 0.15(未及固定窗 1.0)
        assert enqueue_calls == [final]

    asyncio.run(main())
    assert any("INTERP_SPEC defer-expire" in x for x in loop.logs)


def test_controller_refire_stats_counted_when_stats_present():
    """再投机计入 stats.fired(与 fire 同账面——busy 行的 fired/blocked 口径一致)。"""
    say_calls: list = []
    enqueue_calls: list = []
    stats = {"fired": 0, "blocked": 0, "was": False}

    async def slow_run(span):
        await asyncio.sleep(30)
        return ("x", b"y")

    ctl, hold, loop = _mk_controller(
        run_spec=slow_run, say_calls=say_calls, enqueue_calls=enqueue_calls,
        spec_wait_s=1.0, defer_max_s=8.0, refire_chars=6,
    )
    ctl._stats = stats
    final = "你好呀我想问一下，请问你们这个集运怎么收费的"

    async def main():
        _fire_ctl(ctl)
        assert stats["fired"] == 1
        await asyncio.sleep(0.01)
        assert ctl.on_final(final) is True
        ctl.on_interim("你好呀我想问一下，请问你们这个集运怎么收费，帮我看看")
        assert stats["fired"] == 2  # 再投机同账
        assert hold.src == "你好呀我想问一下，请问你们这个集运怎么收费，"
        ctl.cancel("shutdown")  # 收线卫生:单据+在途任务就地终结

    asyncio.run(main())
    assert any("refire" in x and "fired=2" in x for x in loop.logs)


def test_controller_miss_similarity_discards_ready_slot():
    """就绪 slot 但 final 与 span 前缀比对不过 0.85 → MISS:弃 PCM,零播放。"""
    say_calls: list = []
    enqueue_calls: list = []
    ctl, hold, loop = _mk_controller(
        run_spec=lambda span: _done(("hello there", b"PCM")),
        say_calls=say_calls,
        enqueue_calls=enqueue_calls,
    )

    async def main():
        _fire_ctl(ctl)
        await asyncio.sleep(0.01)
        assert ctl.on_final("完全不同的一句话内容呀") is False

    asyncio.run(main())
    assert say_calls == [] and enqueue_calls == []
    assert any("INTERP_SPEC miss" in x and "sim=" in x for x in loop.logs)


def test_controller_single_flight_supersedes_old_task():
    """新 span 顶掉旧在途投机(单飞):旧任务 cancel、新任务接管 slot。"""
    cancelled: list = []

    class _TaskSpy:
        def __init__(self, coro):
            self._t = asyncio.ensure_future(coro)
            self.cancelled = False

        def done(self):
            return self._t.done()

        def cancel(self):
            self.cancelled = True
            cancelled.append(True)
            return self._t.cancel()

    async def slow_run(span):
        await asyncio.sleep(30)
        return ("x", b"y")

    ctl, hold, loop = _mk_controller(run_spec=slow_run)
    orig_create = asyncio.create_task

    async def main():
        # 用真 asyncio.create_task,但包一层 spy 记 cancel。
        def spy_create(coro):
            t = orig_create(coro)
            spy = _TaskSpy(asyncio.sleep(0))
            spy._t = t
            hold_task_spy.append(spy)
            return spy

        hold_task_spy: list = []
        import agent_runtime.interpret as itp

        real = itp.asyncio.create_task
        itp.asyncio.create_task = spy_create
        try:
            _fire_ctl(ctl)
            assert hold_task_spy and hold_task_spy[0].done() is False
            ctl.on_interim("你好呀我想问一下，请问你们这个集运怎么收费，")
            ctl.on_interim("你好呀我想问一下，请问你们这个集运怎么收费，")
            await asyncio.sleep(0.01)
            assert hold_task_spy[0].cancelled is True  # 旧投机被顶掉
            assert hold.src == "你好呀我想问一下，请问你们这个集运怎么收费，"
        finally:
            itp.asyncio.create_task = real

    asyncio.run(main())
    assert any("INTERP_SPEC abort" in x for x in loop.logs)


def test_controller_cancel_hygiene_on_shutdown():
    ctl, hold, loop = _mk_controller(
        run_spec=lambda span: _done(("t", b"p")),
    )

    async def main():
        _fire_ctl(ctl)
        task = hold.task
        assert task is not None
        ctl.cancel("shutdown")
        await asyncio.sleep(0.01)
        assert task.cancelled()
        assert hold.src == "" and hold.task is None
        # 幂等:再 cancel 不炸。
        ctl.cancel("shutdown")

    asyncio.run(main())


# ---------------------------------------------------------------------------
# entrypoint 接线源级 pin(闭包不可直调,镜像仓内源级 pin 惯例)
# ---------------------------------------------------------------------------


def test_on_user_input_wiring_source_pinned():
    """interim 喂 detector、final 过确认、HIT 跳过正常路径——接线三条都钉。"""
    assert "spec_ctl.on_interim(text)" in INTERP_SRC
    assert "if spec_ctl.on_final(text):\n            return" in INTERP_SRC
    # 旧路径铁证:确认 False 后原样 put_nowait+note_src(逐字节语义保留)。
    assert "_src_q.put_nowait(text)" in INTERP_SRC
    assert "_lag.note_src(text)" in INTERP_SRC


def test_mt_busy_flag_wiring_source_pinned():
    """真 MT 在途旗:worker 取句置位/finally 清位——投机 busy 闸的数据源。
    W1-②:闸改多行形状(新增死道并门 `_mt_lane_dead`),pin 同步。"""
    assert '_mt_busy["flag"] = True' in INTERP_SRC
    assert '_mt_busy["flag"] = False' in INTERP_SRC
    assert "_mt_busy[\"flag\"]" in INTERP_SRC
    assert "backlog.source_drops_pending" in INTERP_SRC
    assert "or _mt_lane_dead[\"reason\"]" in INTERP_SRC


def test_shutdown_spec_cancel_source_pinned():
    assert 'spec_ctl.cancel("shutdown")' in INTERP_SRC


def test_spec_run_uses_same_mt_once_and_synth_drain_source_pinned():
    """投机执行体=同款 _mt_once + tts_provider.synthesize 全量排干(同 provider
    =音色/模型/语速天然同源,confirm 的 PCM 即真值,无需对缓存 key)。"""
    assert "translated = await _mt_once(llm_provider, ctx, target_lang=target_lang)" in INTERP_SRC
    assert "tts_provider.synthesize(" in INTERP_SRC
    # HIT 直播=零合成 say(audio=frames)(qa_gate 罐头低 TTFT 车同构)。
    assert "audio=frames_aiter(pcm_to_frames(pcm, tts_provider.sample_rate))" in INTERP_SRC


def test_spec_observability_rows_source_pinned():
    """探针可测的账面:fire/hit/miss/abort 四种 INTERP_SPEC 行都在源里。"""
    assert "INTERP_SPEC fire chars=" in INTERP_SRC
    assert "INTERP_SPEC hit chars=" in INTERP_SRC
    assert "INTERP_SPEC miss chars=" in INTERP_SRC
    assert "INTERP_SPEC abort chars=" in INTERP_SRC


def test_spec_env_registered_in_forward_env_and_interp_env():
    """立法双面:_FORWARD_ENV 表 + _interp_env B 线透传白名单同键。"""
    assert '"BOK_INTERP_SPEC_MT",' in ENV_SRC
    assert ENV_SRC.count('"BOK_INTERP_SPEC_MT"') >= 2
    # S2 defer 自适应两键同姿势(读键=模块常量,静态扫描不认字面量——登记即立法)。
    for key in ("BOK_INTERP_SPEC_DEFER_MAX_S", "BOK_INTERP_SPEC_REFIRE_CHARS"):
        assert f'"{key}",' in ENV_SRC
        assert ENV_SRC.count(f'"{key}"') >= 2  # _FORWARD_ENV + _interp_env 双面


def test_spec_machine_never_wired_into_a_line():
    """A 线零影响钉:agent.py 全文无 spec 机器件引用(控制器只服务 B 线双装配)。"""
    assert "_SpecMt" not in AGENT_SRC
    assert "BOK_INTERP_SPEC" not in AGENT_SRC


def test_spec_arm_banner_source_pinned():
    """开闸装配行(实弹日志可 grep 确认 armed)。"""
    assert "spec_mt armed" in INTERP_SRC
