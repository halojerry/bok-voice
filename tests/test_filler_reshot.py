"""按需第二发垫话(reshot,2026-09-25)+ 开场音节去重 单测(全离线,零音频零会话)。

死区(节奏审计实锤):垫话只盖 ~2.3s,watchdog 4s 闸前纯静默 1.7s+;连轮冷却
又保证裸奔轮结构性存在。reshot=同一「播完观察者」挂点的新开门条件:第一发
播完+gap 后回复首音频仍未到、且距 arm >2.2s(真载荷轮才补——治旧链发
BOK_FILLER_CHAIN「固定双发」的根)→ 补一发 hesitation/promise 短句。
开场键=剥停顿标记/标点后的前 2 字,与最近 2 次垫话同键即排除;排光回退同文件
窗(宁重复勿静默——死区比复读贵)。测试姿势镜像 tests/test_fillers.py。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

import agent_runtime.fillers as fillers_mod  # noqa: E402
from agent_runtime.fillers import (  # noqa: E402
    FillerDirector,
    RESHOT_MIN_ELAPSED_S,
    filler_reshot_enabled,
    opening_syllable_key,
)

_PCM = (1000).to_bytes(2, "little", signed=True) * 480  # 20ms @24k


def _make_assets(tmp_path: Path, entries: list[dict]) -> Path:
    """微型 wav+manifest;音频本身 20ms,dur_s 走 manifest(_pick_reshot 排序用)。"""
    assets = tmp_path / "fillers"
    assets.mkdir(parents=True, exist_ok=True)
    for e in entries:
        with wave.open(str(assets / e["file"]), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(_PCM)
    (assets / "manifest.json").write_text(
        json.dumps({"cantonese": entries}, ensure_ascii=False), encoding="utf-8"
    )
    return assets


class _FakePlayHandle:
    """与官方 PlayHandle 同形:future 驱动 done/stop/wait_for_playout(播完即醒)。"""

    def __init__(self):
        self.stopped = False
        self._done = asyncio.get_running_loop().create_future()

    def done(self):
        return self._done.done()

    def stop(self):
        self.stopped = True
        if not self._done.done():
            self._done.set_result(True)

    def complete(self):
        """模拟自然播完(不经 stop)。"""
        if not self._done.done():
            self._done.set_result(True)

    async def wait_for_playout(self):
        await asyncio.shield(self._done)


class _FakePlayer:
    def __init__(self):
        self.plays: list[object] = []
        self.handles: list[_FakePlayHandle] = []

    def play(self, source):
        self.plays.append(source)
        handle = _FakePlayHandle()
        self.handles.append(handle)
        return handle


class _FakeSession:
    agent_state = "thinking"


_NO_PLAYER = object()  # 哨兵:显式传 None(无 player)与不传(自动 FakePlayer)区分

_DEFAULT_POOL = [
    {"text": "普通应承一句。", "file": "cantonese-01.wav", "dur_s": 2.0, "cat": "default"},
    {"text": "嗯，我而家睇下", "file": "cantonese-h01.wav", "dur_s": 1.1, "cat": "hesitation"},
    {"text": "呃，你等我一陣", "file": "cantonese-h02.wav", "dur_s": 1.2, "cat": "hesitation"},
]


def _director(
    tmp_path,
    *,
    entries: list[dict] | None = None,
    lang="cantonese",
    player=_NO_PLAYER,
    guards=None,
    context_resolver=None,
) -> tuple[FillerDirector, _FakePlayer | None]:
    assets = _make_assets(tmp_path, entries if entries is not None else _DEFAULT_POOL)
    if player is _NO_PLAYER:
        player = _FakePlayer()
    d = FillerDirector(
        _FakeSession(),
        lang_resolver=lambda: lang,
        player=player,
        guards=guards or (lambda: False),
        assets_dir=assets,
        context_resolver=context_resolver,
    )
    return d, player


def _fast_env(monkeypatch) -> None:
    """隔离 env:起播/gap 即刻、链发显式关、限次与 reshot 走默认。"""
    monkeypatch.setenv("BOK_FILLER_DELAY_MS", "1")
    monkeypatch.setenv("BOK_FILLER_GAP_MS", "10")
    monkeypatch.setenv("BOK_FILLER_MAX", "6")
    monkeypatch.setenv("BOK_FILLER_CHAIN", "0")
    monkeypatch.delenv("BOK_FILLER_RESHOT", raising=False)


async def _payload_round(d: FillerDirector, player: _FakePlayer, arm_shift: float) -> None:
    """驱动「首发已播完」现场:arm→首发起播→把 arm 时戳回拨(模拟 elapsed)→播完。"""
    d.arm()
    await _wait_first_fire(d, player)
    d._arm_time -= arm_shift
    player.handles[0].complete()


async def _wait_first_fire(d: FillerDirector, player: _FakePlayer) -> None:
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(player.plays) >= 1:
            return
    raise AssertionError("首发未起播")


def _run(coro, timeout=5.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _function_body(src: str, marker: str) -> list[str]:
    """截出 src 里 marker 那个 def 的函数体(到下一条同缩进 def/class 为止)。"""
    lines = src.splitlines()
    start = next(i for i, ln in enumerate(lines) if marker in ln)
    indent = len(lines[start]) - len(lines[start].lstrip())
    body = []
    for ln in lines[start + 1:]:
        stripped = ln.strip()
        if stripped and (len(ln) - len(ln.lstrip())) <= indent and stripped.split()[0] in (
            "def", "async", "class", "@",
        ):
            break
        body.append(ln)
    return body


# ---- env / 常量 / 开场键纯函数 ----


def test_reshot_env_default_on_and_kill_switch(monkeypatch):
    monkeypatch.delenv("BOK_FILLER_RESHOT", raising=False)
    assert filler_reshot_enabled() is True
    monkeypatch.setenv("BOK_FILLER_RESHOT", "0")
    assert filler_reshot_enabled() is False
    monkeypatch.setenv("BOK_FILLER_RESHOT", "garbage")
    assert filler_reshot_enabled() is False  # 非 "1" 一律关(同仓 ==\'1\' 读法)


def test_reshot_min_elapsed_constant_pinned():
    # 载荷轮(TTFT 2.2-3.8s)与正常轮的分界:垫话 0.5s 起播+~1.5s 播完+gap,
    # 载荷轮播完+gap 时 elapsed 落 2.5s+,正常轮落 2.0s 附近——闸钉 2.2。
    assert RESHOT_MIN_ELAPSED_S == 2.2


def test_opening_syllable_key_strips_marks_and_punct():
    assert opening_syllable_key("嗯——呃——") == "嗯呃"
    # 停顿标记是合成指令:剥掉才同键(「嗯<#0.3#>我而家睇下」≡「嗯，我看一下」)
    assert opening_syllable_key("嗯<#0.3#>我而家睇下") == "嗯我"
    assert opening_syllable_key("嗯，我看一下") == "嗯我"
    assert opening_syllable_key("嗯，你等等，我睇下就覆你。") == "嗯你"
    assert opening_syllable_key("") == ""


# ---- 触发矩阵(全流程:arm→首发→播完观察者→reshot 分流) ----


def test_reshot_fires_on_payload_round(tmp_path, monkeypatch, capsys):
    """首发在场+播完+无首音频+elapsed 2.7s → 补一发 hesitation/promise 短句。"""
    _fast_env(monkeypatch)
    monkeypatch.setattr(fillers_mod.random, "random", lambda: 0.99)  # 犹豫混入让位

    async def _case():
        d, player = _director(tmp_path)
        await _payload_round(d, player, arm_shift=RESHOT_MIN_ELAPSED_S + 0.5)
        await asyncio.sleep(0.2)  # gap(10ms)+观察者补位
        assert len(player.plays) == 2 and d._count == 2, "载荷轮死区应补第二发"
        assert d._reshot_done is True
        assert d.fired_this_round() is True
        # 第二发只准出自 hesitation/promise 短桶,不吃普通应承句
        assert d._fired_lines[1] in {"嗯，我而家睇下", "呃，你等我一陣"}
        # 打点形状:BOK_FILLER reshot fired
        assert "BOK_FILLER reshot fired" in capsys.readouterr().out

    _run(_case())


def test_reshot_skipped_when_reply_audio_arrived(tmp_path, monkeypatch, capsys):
    """回复首音频已到 → skip(audio_arrived),hold 契约自会衔接。"""
    _fast_env(monkeypatch)

    async def _case():
        d, player = _director(tmp_path)
        d.arm()
        await _wait_first_fire(d, player)
        d.on_reply_first_audio()  # 回复首音频到达(在播垫话不掐)
        d._arm_time -= 3.0
        player.handles[0].complete()
        await asyncio.sleep(0.2)
        assert len(player.plays) == 1, "回复已出声还补发=叠音"
        assert "reshot skip reason=audio_arrived" in capsys.readouterr().out

    _run(_case())


def test_reshot_skipped_when_elapsed_under_threshold(tmp_path, monkeypatch, capsys):
    """elapsed 未过闸(≈2.1s,正常 TTFT 轮形状)→ skip(elapsed),绝不双发。"""
    _fast_env(monkeypatch)
    monkeypatch.setattr(fillers_mod, "RESHOT_MIN_ELAPSED_S", 2.5)  # 抬闸消时钟抖动

    async def _case():
        d, player = _director(tmp_path)
        await _payload_round(d, player, arm_shift=2.0)  # elapsed≈2.0+调度残差 < 2.5
        await asyncio.sleep(0.2)
        assert len(player.plays) == 1, "elapsed 未过闸不得补发(治旧链发固定双发)"
        assert d._reshot_done is False
        out = capsys.readouterr().out
        assert "reshot skip reason=elapsed" in out

    _run(_case())


def test_reshot_env_off_zero_actions(tmp_path, monkeypatch, capsys):
    """BOK_FILLER_RESHOT=0 → 单发旧行为:首发照播,播完无回复也不补。"""
    _fast_env(monkeypatch)
    monkeypatch.setenv("BOK_FILLER_RESHOT", "0")

    async def _case():
        d, player = _director(tmp_path)
        await _payload_round(d, player, arm_shift=3.0)
        await asyncio.sleep(0.2)
        assert len(player.plays) == 1 and d._count == 1
        assert "reshot" not in capsys.readouterr().out, "关闸=零动作零打点"

    _run(_case())


def test_reshot_once_per_turn(tmp_path, monkeypatch):
    """每轮至多一次:第二发播完仍无回复 → 不发第三条(独立计数,MAX 未耗尽也一样)。"""
    _fast_env(monkeypatch)

    async def _case():
        d, player = _director(tmp_path)
        await _payload_round(d, player, arm_shift=RESHOT_MIN_ELAPSED_S + 0.5)
        await asyncio.sleep(0.2)
        assert len(player.plays) == 2 and d._reshot_done is True
        player.handles[1].complete()  # 第二发也播完,回复仍没来
        await asyncio.sleep(0.2)
        assert len(player.plays) == 2, "reshot 封顶 1 次/轮"
        d.arm()  # 新一轮:计数复位
        assert d._reshot_done is False

    _run(_case())


def test_reshot_blocked_when_max_exhausted(tmp_path, monkeypatch):
    """MAX 耗尽 → 不发(与第一发合计消耗同一 BOK_FILLER_MAX 计数)。"""
    _fast_env(monkeypatch)
    monkeypatch.setenv("BOK_FILLER_MAX", "1")

    async def _case():
        d, player = _director(tmp_path)
        await _payload_round(d, player, arm_shift=RESHOT_MIN_ELAPSED_S + 0.5)
        await asyncio.sleep(0.2)
        assert len(player.plays) == 1 and d._count == 1, "MAX=1 时首发即耗尽预算"
        assert d._reshot_done is False

    _run(_case())


def test_reshot_cancelled_on_new_user_turn(tmp_path, monkeypatch):
    """观察段挂起中 cancel()(用户插话优先)→ gap 呼吸段一并取消,不补发。"""
    _fast_env(monkeypatch)

    async def _case():
        d, player = _director(tmp_path)
        d.arm()
        await _wait_first_fire(d, player)
        d._arm_time -= 3.0
        d.cancel()  # 插话:停播+取消观察者(reshot gap 段同任务,一并死)
        assert player.handles[0].stopped is True
        player.handles[0].complete()  # 停播已 resolve,不再唤醒任何补发
        await asyncio.sleep(0.2)
        assert len(player.plays) == 1, "插话后不得补发"
        assert d._reshot_done is False  # 计数未耗(新轮额度完好)

    _run(_case())


def test_reshot_on_fired_callback_sees_reshot_firing(tmp_path, monkeypatch):
    """on_fired 回调首发/第二发各触发一次;reshot_firing() 只在第二发瞬态为真
    (agent 侧顺延口据此为第二发重开顺延窗)。"""
    _fast_env(monkeypatch)
    seen: list[bool] = []

    async def _case():
        d, player = _director(tmp_path)
        d.set_on_fired(lambda: seen.append(d.reshot_firing()))
        await _payload_round(d, player, arm_shift=RESHOT_MIN_ELAPSED_S + 0.5)
        await asyncio.sleep(0.2)
        assert len(player.plays) == 2
        assert seen == [False, True], seen
        assert d.reshot_firing() is False, "瞬态标必须在播后清零"

    _run(_case())


def test_reshot_second_line_opening_differs(tmp_path, monkeypatch):
    """开场音节去重对第二发生效:首发与补发开场键不得相同(治「嗯——」连发)。"""
    _fast_env(monkeypatch)
    entries = [
        {"text": "嗯，我而家睇下", "file": "cantonese-h01.wav", "dur_s": 1.0, "cat": "hesitation"},
        {"text": "呃，你等我一陣", "file": "cantonese-h02.wav", "dur_s": 1.1, "cat": "hesitation"},
        {"text": "呃——好，我即刻查", "file": "cantonese-h03.wav", "dur_s": 1.2, "cat": "hesitation"},
        {"text": "嗯——好，我幫你跟進", "file": "cantonese-h04.wav", "dur_s": 1.3, "cat": "hesitation"},
    ]

    async def _case():
        d, player = _director(tmp_path, entries=entries)
        await _payload_round(d, player, arm_shift=RESHOT_MIN_ELAPSED_S + 0.5)
        await asyncio.sleep(0.2)
        assert len(player.plays) == 2
        k1 = opening_syllable_key(d._fired_lines[0])
        k2 = opening_syllable_key(d._fired_lines[1])
        assert k1 != k2, f"两连发同开场键: {d._fired_lines}"

    _run(_case())


# ---- _pick_reshot 选池规则(纯离线,不播) ----


def test_reshot_pick_avoids_same_opening_key(tmp_path, monkeypatch):
    """首发已垫「嗯我甲」→ 短半区同键候选被开场键排掉,选不同键的候选。"""
    entries = [
        {"text": "嗯我甲", "file": "a.wav", "dur_s": 1.0, "cat": "hesitation"},
        {"text": "嗯我乙", "file": "b.wav", "dur_s": 1.2, "cat": "hesitation"},
        {"text": "呃你丙", "file": "c.wav", "dur_s": 1.1, "cat": "hesitation"},
    ]

    async def _case():
        d, _ = _director(tmp_path, entries=entries)
        first = entries[0]
        d._record_recent(first)
        got = d._pick_reshot("cantonese")
        assert got is not None and got["text"] == "呃你丙", "同开场键候选必须让位"

    _run(_case())


def test_reshot_pick_falls_back_when_pool_exhausted(tmp_path, monkeypatch):
    """短半区+全池都被开场键排光 → 回退同文件窗(宁重复勿静默,死区比复读贵)。"""
    entries = [
        {"text": "嗯我甲", "file": "a.wav", "dur_s": 1.0, "cat": "hesitation"},
        {"text": "嗯我乙", "file": "b.wav", "dur_s": 1.2, "cat": "hesitation"},
    ]

    async def _case():
        d, _ = _director(tmp_path, entries=entries)
        d._record_recent(entries[0])
        got = d._pick_reshot("cantonese")
        assert got is not None, "排光也不得静默"
        assert got["file"] == "b.wav", "至少回退同文件窗换文件"

    _run(_case())


def test_pick_reshot_pool_rules(tmp_path, monkeypatch):
    """池规则:短半区优先 / 桶承诺池收窄 / 无犹豫承诺池 → None(不硬凑应承句)。"""
    mixed = [
        {"text": "普通应承一句。", "file": "a.wav", "dur_s": 2.0, "cat": "default"},
        {"text": "短犹豫一", "file": "b.wav", "dur_s": 0.5, "cat": "hesitation"},
        {"text": "短犹豫二", "file": "c.wav", "dur_s": 0.6, "cat": "hesitation"},
        {"text": "长犹豫一", "file": "d.wav", "dur_s": 2.9, "cat": "hesitation"},
        {"text": "长犹豫二", "file": "e.wav", "dur_s": 3.0, "cat": "hesitation"},
    ]

    async def _case():
        d, _ = _director(tmp_path, entries=mixed)
        for _ in range(20):
            d._recent.clear()  # 两窗同清(键窗由 _recent 派生)
            got = d._pick_reshot("cantonese")
            assert got is not None and got["cat"] == "hesitation", "只吃犹豫/承诺桶"
            assert got["dur_s"] <= 0.6, f"短半区优先,拣咗长的: {got}"

        # 桶承诺池收窄:wa 桶在场且有 promise_wa 资产 → 桶池优先
        d2, _ = _director(
            tmp_path / "bucket",
            entries=[
                {"text": "短犹豫", "file": "h.wav", "dur_s": 0.5, "cat": "hesitation"},
                {"text": "号码收到承诺", "file": "p.wav", "dur_s": 1.3, "cat": "promise_wa"},
            ],
            context_resolver=lambda: {"bucket": "wa"},
        )
        monkeypatch.setenv("BOK_FILLER_CONTEXT", "1")
        assert d2._pick_reshot("cantonese")["text"] == "号码收到承诺"

        # 池里无 hesitation/promise → None(调用方打 reshot skip reason=pool)
        d3, _ = _director(
            tmp_path / "plain",
            entries=[{"text": "普通应承。", "file": "x.wav", "dur_s": 1.5, "cat": "default"}],
        )
        assert d3._pick_reshot("cantonese") is None

    _run(_case())


# ---- 两窗去重(同文件窗 + 开场键窗,选池通用) ----


def test_two_stage_dedup_excludes_same_opening_and_falls_back(tmp_path, monkeypatch):
    """_pick 通用面:同开场键候选被排;全排光回退同文件窗(有声>静默)。"""
    monkeypatch.setattr(fillers_mod.random, "random", lambda: 0.99)  # 犹豫混入让位

    async def _case():
        entries = [
            {"text": "嗯我甲", "file": "a.wav", "dur_s": 1.0, "cat": "default"},
            {"text": "嗯我乙", "file": "b.wav", "dur_s": 1.0, "cat": "default"},
            {"text": "呃你丙", "file": "c.wav", "dur_s": 1.0, "cat": "default"},
        ]
        d, _ = _director(tmp_path, entries=entries)
        d._record_recent(entries[0])  # 最近播过「嗯我甲」
        got = d._pick("cantonese")
        assert got["text"] == "呃你丙", "同开场键候选应被排掉"
        # 池内两条同首二字不同文件、都被最近播过的开场键排掉 → 回退同文件窗
        d2, _ = _director(
            tmp_path / "fb",
            entries=[
                {"text": "嗯我甲", "file": "a.wav", "dur_s": 1.0, "cat": "default"},
                {"text": "嗯我乙", "file": "b.wav", "dur_s": 1.0, "cat": "default"},
            ],
        )
        d2._record_recent(entries[0])
        got2 = d2._pick("cantonese")
        assert got2 is not None and got2["file"] == "b.wav", "排光必须回退,宁重复勿静默"

    _run(_case())


# ---- watchdog 顺延:第二发享有自己的顺延额度 ----


def test_agent_extend_watchdog_rearms_for_reshot():
    """agent 侧 _extend_response_watchdog 必须读 reshot_firing() 复位 extended 旗
    再重走 _watchdog_extend(源码钉住——闭包内接线无法直接单测)。"""
    root = Path(__file__).resolve().parents[1]
    src = (root / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    body = _function_body(src, "def _extend_response_watchdog")
    joined = "\n".join(body)
    assert "reshot_firing()" in joined, "第二发开播标必须接进顺延口"
    reset = next(i for i, ln in enumerate(body) if 'extended"] = False' in ln)
    # 只认裸调用行(docstring 里「旗标语义见 _watchdog_extend(…)」係同字面)
    extend = next(i for i, ln in enumerate(body) if ln.strip() == "_watchdog_extend(")
    assert reset < extend, "复位必须发生在重走 _watchdog_extend 之前"


def test_watchdog_extend_second_window_after_flag_reset():
    """第二发顺延语义(纯逻辑):首发旗已耗 → 拒;agent 侧复位旗 → 第二发获
    自己的一次性顺延窗(每轮至多两延:首发一延+第二发一延)。"""

    from agent_runtime.agent import _watchdog_extend

    def _armed(deadline_in: float = 4.0) -> dict:
        return {
            "task": None,
            "disarmed": False,
            "deadline": time.monotonic() + deadline_in,
            "extended": False,
        }

    def _stub(rec: list):
        def _spawn(delay: float):
            rec.append(delay)

            class _StubTask:  # 第二发重武装会 swap task:须扛 done()/cancel() 探测
                def done(self):
                    return True  # 已「完成」→ _watchdog_extend 跳过 cancel

                def cancel(self):
                    pass

            return _StubTask()

        return _spawn

    rec: list = []
    state = _armed()
    now = time.monotonic()
    assert _watchdog_extend(state, _stub(rec), 2.0, now) is True
    assert _watchdog_extend(state, _stub(rec), 2.0, now) is False, "首发旗已耗"
    state["extended"] = False  # agent 侧 reshot 复位
    assert _watchdog_extend(state, _stub(rec), 2.0, now) is True, "第二发应获顺延"
    assert len(rec) == 2


# ---- 立法面 ----


def test_forward_env_registers_reshot():
    """BOK_FILLER_RESHOT 已入 bok.py _FORWARD_ENV(prod 封闭 env 面可达)。"""
    import tools.bok as bok  # noqa: F401

    assert "BOK_FILLER_RESHOT" in bok._FORWARD_ENV
