"""B 线缺源遥测(2026-09-30 call-72112fd7 定案)。

现场:同房三轨齐发(服务器实锤 me-/other- 麦 +16s 双发布)而 fwd 全程零订阅
零 ASR、rev 同形正常——订阅空挂在我们的代码面零痕迹。本遥测每
BOK_INTERP_SRC_TELEMETRY_S 秒分辨「对端没发麦」(SRC_NO_AUDIO_TRACK) vs
「发了订不上」(SRC_TRACK_NOT_SUBSCRIBED),下一例现场直接指认断点层。

测试面:分类纯函数 + 接线源级 pin + env 立法。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.interpret import _src_track_state  # noqa: E402

INTERP_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "interpret.py").read_text(
    encoding="utf-8"
)
BOK_SRC = (ROOT / "tools" / "bok.py").read_text(encoding="utf-8")

AUDIO = 1  # rtc.TrackKind.KIND_AUDIO 的 int 值(测试腿不拉真 rtc)
VIDEO = 2


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


def test_telemetry_wiring_source_pins():
    """接线源级 pin:看护挂点 + 两态观测行 + env 闸 + bok.py 透传立法。"""
    assert 'os.environ.get("BOK_INTERP_SRC_TELEMETRY", "1") != "1"' in INTERP_SRC
    assert "_src_track_state(" in INTERP_SRC
    assert "SRC_NO_AUDIO_TRACK identity=" in INTERP_SRC
    assert "SRC_TRACK_NOT_SUBSCRIBED identity=" in INTERP_SRC
    assert "asyncio.create_task(_src_track_watch())" in INTERP_SRC
    assert '"BOK_INTERP_SRC_TELEMETRY"' in BOK_SRC
    assert '"BOK_INTERP_SRC_TELEMETRY_S"' in BOK_SRC
