"""B 线缺源遥测 + 订阅自愈(2026-09-30 call-72112fd7 定案;2026-10-02 刀1 补行为面)。

现场:同房三轨齐发(服务器实锤 me-/other- 麦 +16s 双发布)而 fwd 全程零订阅
零 ASR、rev 同形正常——订阅空挂在我们的代码面零痕迹。本遥测每
BOK_INTERP_SRC_TELEMETRY_S 秒分辨「对端没发麦」(SRC_NO_AUDIO_TRACK) vs
「发了订不上」(SRC_TRACK_NOT_SUBSCRIBED),heal 档对后者发 set_subscribed(True)
(官方手动订阅口)打 SRC_TRACK_RESUBSCRIBE。

测试面:分类纯函数 + **看护循环行为(假 room 直驱,不启 worker)** + 接线源级
pin + env 立法。RC-1(2026-10-02 刀1):旧闭包 heal 分支引用未定义的 `part`
(首次进 unsubscribed 态 NameError 杀死看护),行为测试直接执行该分支——复现必红。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from livekit import rtc  # noqa: E402

from _bok_src import bok_source  # noqa: E402
from agent_runtime.interpret import (  # noqa: E402
    _src_track_state,
    _src_track_watch_loop,
)

INTERP_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "interpret.py").read_text(
    encoding="utf-8"
)
BOK_SRC = bok_source()

AUDIO = rtc.TrackKind.KIND_AUDIO
VIDEO = rtc.TrackKind.KIND_VIDEO
IDENT = "me-room"

# ---------------------------------------------------------------------------
# 分类纯函数
# ---------------------------------------------------------------------------


def _pub(sid: str, kind: int, subscribed: bool):
    return SimpleNamespace(sid=sid, kind=kind, track=object() if subscribed else None)


def _part(*pubs):
    return SimpleNamespace(track_publications={p.sid: p for p in pubs})


def test_classifier_none_when_participant_absent():
    assert _src_track_state(None, AUDIO) == ("none", [])


def test_classifier_none_when_no_audio_publications():
    assert _src_track_state(_part(_pub("TR_v", VIDEO, True)), AUDIO) == ("none", [])
    assert _src_track_state(_part(), AUDIO) == ("none", [])


def test_classifier_unsubscribed_when_published_but_no_track():
    """现场形态:音轨已发布(publication 在席)但本 worker 订不上(track=None)。"""
    state, sids = _src_track_state(_part(_pub("TR_a", AUDIO, False)), AUDIO)
    assert state == "unsubscribed"
    assert sids == ["TR_a"]


def test_classifier_partial_unsubscribed_reports_missing():
    state, sids = _src_track_state(
        _part(_pub("TR_a", AUDIO, True), _pub("TR_b", AUDIO, False)), AUDIO
    )
    assert state == "unsubscribed"
    assert sids == ["TR_b"]


def test_classifier_ok_when_subscribed():
    assert _src_track_state(_part(_pub("TR_a", AUDIO, True)), AUDIO) == ("ok", [])


# ---------------------------------------------------------------------------
# 看护循环行为面(假 room 直驱)
# ---------------------------------------------------------------------------


class _FakePub:
    """最小 publication 形状:kind/sid/track + set_subscribed(可令其抛)。"""

    def __init__(self, sid: str, kind, subscribed: bool, raises: bool = False):
        self.sid = sid
        self.kind = kind
        self.track = object() if subscribed else None
        self.calls: list[bool] = []
        self.raises = raises

    def set_subscribed(self, v: bool) -> None:
        self.calls.append(v)
        if self.raises:
            raise RuntimeError("boom")


class _ScriptedRoom:
    """按轮次返回脚本化 participant 的假 room:每轮一次 `.get()`=确定性轮数驱动。

    脚本条目:None=对端不在房;pub 列表=该轮 listen 身份的音轨集(末轮重复)。
    经 room 而非 participant 计数——heal 路径会再次读 `track_publications`
    (分类一次、自愈一次),按属性访问计数会把一轮算成两轮。
    """

    def __init__(self, rounds: list[list[_FakePub] | None]):
        self._rounds = list(rounds)
        self.rounds = 0
        self.remote_participants = self  # `.get()` 在房名册上,脚本即名册

    def get(self, _identity: str):
        idx = min(self.rounds, len(self._rounds) - 1)
        self.rounds += 1
        entry = self._rounds[idx]
        if entry is None:
            return None
        return SimpleNamespace(track_publications={p.sid: p for p in entry})


def _drive(room: _ScriptedRoom, *, heal: bool, until_rounds: int = 1, interval: float = 0.005) -> None:
    """驱动看护到第 until_rounds 轮,然后 closed 置位收队。

    看护活着退出(未被异常杀死)+ 轮数达到=断言;超时未达轮数即失败。
    """

    async def _main() -> None:
        closed = asyncio.Event()
        task = asyncio.create_task(
            _src_track_watch_loop(room, IDENT, closed, interval, heal)
        )
        deadline = time.monotonic() + 3.0
        while room.rounds < until_rounds and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        assert room.rounds >= until_rounds, (
            f"watch loop only ran {room.rounds}/{until_rounds} rounds (dead?)"
        )
        closed.set()
        await asyncio.wait_for(task, timeout=1.0)
        assert task.exception() is None  # 循环正常退出,没被 NameError 等杀死

    asyncio.run(_main())


def test_watch_loop_heals_unsubscribed_audio_pub(capsys):
    """unsubscribed + heal=1 → set_subscribed(True) 打到 track=None 的音轨 pub。

    RC-1 复现点:旧闭包此分支 NameError(`part` 未定义),行为面直接红。
    """
    pub = _FakePub("TR_a", AUDIO, subscribed=False)
    _drive(_ScriptedRoom([[pub]]), heal=True, until_rounds=1)
    out = capsys.readouterr().out
    assert pub.calls == [True]
    assert "SRC_TRACK_NOT_SUBSCRIBED identity=me-room sids=['TR_a'] rounds=1" in out
    assert "SRC_TRACK_RESUBSCRIBE identity=me-room sids=['TR_a'] rounds=1" in out


def test_watch_loop_heal_off_is_observation_only(capsys):
    """heal=0 纯观测:遥测行照打,set_subscribed 一次不调(旧 kill-switch 语义)。"""
    pub = _FakePub("TR_a", AUDIO, subscribed=False)
    _drive(_ScriptedRoom([[pub]]), heal=False, until_rounds=3)
    out = capsys.readouterr().out
    assert pub.calls == []
    assert "SRC_TRACK_NOT_SUBSCRIBED identity=me-room sids=['TR_a'] rounds=3" in out
    assert "SRC_TRACK_RESUBSCRIBE" not in out


def test_watch_loop_survives_set_subscribed_exception(capsys):
    """单 pub 自愈抛异常 → 打 failed 行且循环不死(下一轮照跑)。"""
    pub = _FakePub("TR_a", AUDIO, subscribed=False, raises=True)
    _drive(_ScriptedRoom([[pub]]), heal=True, until_rounds=2)
    out = capsys.readouterr().out
    assert "SRC_TRACK_RESUBSCRIBE failed sid=TR_a" in out
    # 第 2 轮照常遥测=异常没杀循环。
    assert "SRC_TRACK_NOT_SUBSCRIBED identity=me-room sids=['TR_a'] rounds=2" in out


def test_watch_loop_ok_state_resets_round_counters(capsys):
    """ok 态清零计数:unsub(1)→unsub(2)→ok→unsub 重新从 1 起步。"""
    room = _ScriptedRoom(
        [
            [_FakePub("TR_a", AUDIO, subscribed=False)],
            [_FakePub("TR_b", AUDIO, subscribed=False)],
            [_FakePub("TR_c", AUDIO, subscribed=True)],
            [_FakePub("TR_d", AUDIO, subscribed=False)],
        ]
    )
    _drive(room, heal=False, until_rounds=4)
    out = capsys.readouterr().out
    assert "sids=['TR_a'] rounds=1" in out
    assert "sids=['TR_b'] rounds=2" in out
    assert "sids=['TR_d'] rounds=1" in out  # 计数器已归零(否则会是 rounds=3)


def test_watch_loop_ok_state_silent(capsys):
    """ok 态零打印零自愈(常态零噪音)。"""
    pub = _FakePub("TR_a", AUDIO, subscribed=True)
    _drive(_ScriptedRoom([[pub]]), heal=True, until_rounds=2)
    assert capsys.readouterr().out == ""
    assert pub.calls == []


def test_watch_loop_no_track_cadence(capsys):
    """对端未发麦:第 3 轮起报一次(rounds==3 或 %10),不是每轮刷屏。"""
    room = _ScriptedRoom([None])  # 对端不在房
    _drive(room, heal=True, until_rounds=3)
    out = capsys.readouterr().out
    assert "SRC_NO_AUDIO_TRACK identity=me-room rounds=3" in out
    assert "rounds=1 (对端未发布麦克风" not in out


def test_watch_loop_returns_promptly_when_closed():
    """closed 已置位 → 立即返回(不等 interval)。"""
    closed = asyncio.Event()
    closed.set()

    async def _main() -> float:
        t0 = time.monotonic()
        await asyncio.wait_for(
            _src_track_watch_loop(_ScriptedRoom([None]), IDENT, closed, 30.0, True),
            timeout=1.0,
        )
        return time.monotonic() - t0

    assert asyncio.run(_main()) < 0.5


# ---------------------------------------------------------------------------
# 接线源级 pin + env 立法
# ---------------------------------------------------------------------------


def test_telemetry_wiring_source_pins():
    """接线源级 pin:模块级循环 + 池化 spawn(裸 create_task 静默死已退役)。"""
    assert 'os.environ.get("BOK_INTERP_SRC_TELEMETRY", "1") != "1"' in INTERP_SRC
    assert "_src_track_state(" in INTERP_SRC
    assert "SRC_NO_AUDIO_TRACK identity=" in INTERP_SRC
    assert "SRC_TRACK_NOT_SUBSCRIBED identity=" in INTERP_SRC
    assert "async def _src_track_watch_loop(" in INTERP_SRC
    assert "_src_track_watch_loop(room, listen_identity, closed, interval, heal)" in INTERP_SRC
    assert "_watch_tasks" in INTERP_SRC and '"SRC_WATCH_ERR"' in INTERP_SRC
    # RC-1 防复发:裸 create_task 形态必须绝迹(它令 NameError 静默死)。
    assert "asyncio.create_task(_src_track_watch())" not in INTERP_SRC
    assert '"BOK_INTERP_SRC_TELEMETRY"' in BOK_SRC
    assert '"BOK_INTERP_SRC_TELEMETRY_S"' in BOK_SRC


def test_selfheal_wiring_source_pins():
    """订阅自愈 pin:part 单点解析(RC-1)+ set_subscribed 官方口 + env 立法。"""
    assert '"BOK_INTERP_SRC_HEAL"' in BOK_SRC
    assert 'os.environ.get("BOK_INTERP_SRC_HEAL", "1") == "1"' in INTERP_SRC
    assert "_p.set_subscribed(True)" in INTERP_SRC
    assert "SRC_TRACK_RESUBSCRIBE identity=" in INTERP_SRC
    # RC-1 根因形态:每轮 part 单点解析(分类与自愈同源),heal 不再引用未定义名。
    assert "part = room.remote_participants.get(listen_identity)" in INTERP_SRC


def test_settle_deferred_when_humans_still_active_source_pins():
    """半场结算闸(2026-09-30 对账):close_on_disconnect 先走端不再早结算。"""
    assert "settle deferred (participants still active:" in INTERP_SRC
    assert "留最后离场方向结算" in INTERP_SRC


def test_textonly_direction_skips_tts_assembly_source_pins():
    """text-only 方向(BOK_INTERP_REV_AUDIO=0 回退档;2026-10-08 起双向出声为默认)
    不构造/不连云端 TTS(零收益连接根除)。"""
    assert "_build_tts_provider(tts_cfg, target_lang, session_voices) if _dir_audio else None" in INTERP_SRC
    assert "voice_tags = (\n        _dir_audio" in INTERP_SRC  # 语气标记同门(无合成=纯噪音)
