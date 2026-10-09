"""豆包 R4-C 端窗看门狗单测（2026-10-09 W8-A2；零网络：fake WS 帧序列驱动）。

病理（P2 实证）：豆包 SAUC 不显式设 ``end_window_size`` 时缺省 ~3044ms 才
definite（文档标 800）；lite 显式下发后 definite 先于本地 VAD END 到——VAD 卡死
（START 后永不 END）时负 seq 结构性发不出=段缓冲无界增长、未 definite 尾巴永不
落稿。看门狗=definite 增量后服务端再无新内容且超宽限本地仍未 END → 主动走既有
``_finalize_utterance`` 收段。本文件钉：缺省关（旧线逐字节）/lite 恒开/开火判据
四闸（喂帧中/definite 前提/全文冻结/宽限）/真语音续行让位/END 先到下岗/段间记账
清零。fake WS/VAD 驱动器复用 ``test_doubao_asr``（单源不二份）。

宽限常量 monkeypatch 提速（生产 2.0s，测试 0.35-0.4s；节拍 0.25s 两态同值）。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_runtime.providers import doubao_asr as da  # noqa: E402
from agent_runtime.providers.doubao_asr import (  # noqa: E402
    MSG_AUDIO_ONLY_REQ,
    DoubaoSTT,
    _DoubaoLiveStream,
    parse_server_frame,
)

import tests.test_doubao_asr as base  # noqa: E402  (fake WS/VAD/造帧单源)

_PART1 = "你好，帮我查下单号。"
_PART2 = "好的那先这样。"
_GROWN_TEXT = "你好，帮我查下单号。好的那先这样"  # 第二句 interim（未判停）


def _utts(text: str, parts: list[tuple[str, bool]]) -> dict:
    """utterances 全量重发形状（服务端真形）：definite 分句按序累积在列。"""
    return {
        "result": {
            "text": text,
            "utterances": [{"text": t, "definite": d} for t, d in parts],
        }
    }


def _connect_frames(monkeypatch, *, audio: list[dict], last: dict) -> dict:
    """fake connect：每音频包按序吐 audio 帧（吐完即静默=服务端冻结）；末包吐 last。"""
    calls = {"n": 0}

    def _connect(*a, **kw):
        calls["n"] += 1
        fake = base._FakeWS(
            on_audio=[base._srv_frame(p, seq=1) for p in audio],
            on_last=[base._srv_frame(last, last=True)],
        )
        calls["ws"] = fake

        async def _ret():
            return fake

        return _ret()

    monkeypatch.setattr("websockets.connect", _connect)
    return calls


def _lite(vad, **kw):
    """lite 姿势旗组（服务端分句+clause_commit+尾窗+看门狗），单测显式构造。"""
    kw.setdefault("utt_wait_s", 0.1)
    return DoubaoSTT(
        api_key="k",
        vad_=vad,
        server_utterances=True,
        clause_commit=True,
        utt_merge=True,
        end_window_watchdog=True,
        **kw,
    )


def _neg_seqs(ws) -> list[int]:
    out = []
    for f in ws.sent:
        fr = parse_server_frame(f)
        if fr["type"] == MSG_AUDIO_ONLY_REQ and (fr["seq"] or 0) < 0:
            out.append(fr["seq"])
    return out


async def _drive_no_end(
    stt: DoubaoSTT,
    vad: base._FakeVad,
    *,
    packets: int,
    hold_s: float,
    end: bool = False,
) -> list[tuple[str, str]]:
    """卡死 VAD 驱动：START→N×INFERENCE（每包 200ms=一条 fake 响应）→默认不发
    END、静置 hold_s（看门狗宽限窗）→关流。返回 (事件名, 文本) 序列。"""
    stream = _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())
    for _ in range(200):
        await asyncio.sleep(0.005)
        if vad.streams:
            break
    vs = vad.streams[-1]
    got: list[tuple[str, str]] = []

    async def _collect() -> None:
        try:
            while True:
                ev = await asyncio.wait_for(stream.__anext__(), timeout=0.5)
                text = ev.alternatives[0].text if getattr(ev, "alternatives", None) else ""
                got.append((ev.type.name, text))
        except (asyncio.TimeoutError, StopAsyncIteration):
            return

    collector = asyncio.create_task(_collect())
    vs.q.put_nowait(base._ev(da.vad.VADEventType.START_OF_SPEECH,
                             frames=[types.SimpleNamespace(data=b"\x11\x11" * 800)]))
    await asyncio.sleep(0.03)
    for _ in range(packets):
        vs.q.put_nowait(base._ev(da.vad.VADEventType.INFERENCE_DONE,
                                 frames=[types.SimpleNamespace(data=b"\x22\x22" * 3200)]))
        await asyncio.sleep(0.12)  # 留 fake 响应与事件泵轮转窗
    if end:
        vs.q.put_nowait(base._ev(da.vad.VADEventType.END_OF_SPEECH))
    await asyncio.sleep(hold_s)
    stream._input_ch.close()
    try:
        await asyncio.wait_for(collector, 3)
    except asyncio.TimeoutError:
        collector.cancel()
    await stream.aclose()
    return got


def test_watchdog_flag_default_off_is_legacy(monkeypatch, capsys):
    """缺省不传旗=旧线逐字节：definite 到而 VAD 卡死也绝不提前收段（无观测行、
    无负 seq、无本地 END）——病理形态原样保留，行为零变化。"""
    monkeypatch.setattr(da, "_END_WINDOW_WATCHDOG_S", 0.35)
    base._fake_merge(monkeypatch)
    calls = _connect_frames(
        monkeypatch,
        audio=[_utts(_PART1, [(_PART1, True)])],
        last=_utts(_PART1, [(_PART1, True)]),
    )
    vad = base._FakeVad()
    stt = DoubaoSTT(api_key="k", vad_=vad, server_utterances=True, clause_commit=True,
                    utt_merge=True)  # 不传看门狗旗=旧姿势

    events = asyncio.run(_drive_no_end(stt, vad, packets=1, hold_s=1.0))
    names = [n for n, _ in events]
    out = capsys.readouterr().out
    assert "END_WINDOW watchdog" not in out
    assert "END_OF_SPEECH" not in names  # VAD 卡死：本地 END 从未发生
    assert _neg_seqs(calls["ws"]) == []  # 负 seq 结构性未发出（病理形态本身）


def test_watchdog_fires_when_vad_stuck_after_definite(monkeypatch, capsys):
    """主刀路径：definite 到→服务端全文冻结→宽限内本地不 END → 观测行+主动负 seq
    收段（全程无本地 END 事件）；definite FINAL 由分句道发出、收段不重发。"""
    monkeypatch.setattr(da, "_END_WINDOW_WATCHDOG_S", 0.35)
    base._fake_merge(monkeypatch)
    calls = _connect_frames(
        monkeypatch,
        audio=[_utts(_PART1, [(_PART1, True)])],
        last=_utts(_PART1, [(_PART1, True)]),
    )
    vad = base._FakeVad()
    stt = _lite(vad)

    events = asyncio.run(_drive_no_end(stt, vad, packets=1, hold_s=1.2))
    names = [n for n, _ in events]
    finals = [t for n, t in events if n == "FINAL_TRANSCRIPT"]
    out = capsys.readouterr().out
    assert "[doubao] END_WINDOW watchdog fired definite=1" in out
    assert "END_OF_SPEECH" not in names  # 全程无本地 END——收段是看门狗干的
    assert finals == [_PART1]  # definite 分句道（不随收段重发）
    assert len(_neg_seqs(calls["ws"])) == 1  # 看门狗主动负 seq 定稿（病理段被收割）


def test_watchdog_holds_while_text_grows(monkeypatch, capsys):
    """真语音续行让位：definite 之后服务端全文仍在增长（新 utterance 进行中）→
    看门狗宽限内绝不拦腰切活语音（无观测行、无负 seq）。"""
    monkeypatch.setattr(da, "_END_WINDOW_WATCHDOG_S", 0.35)
    base._fake_merge(monkeypatch)
    calls = _connect_frames(
        monkeypatch,
        audio=[
            _utts(_PART1, [(_PART1, True)]),
            _utts(_GROWN_TEXT, [(_PART1, False)]),  # definite 后新内容（未判停）
        ],
        last=_utts(_GROWN_TEXT + "。", [(_PART1, True), (_PART2, True)]),
    )
    vad = base._FakeVad()
    stt = _lite(vad)

    asyncio.run(_drive_no_end(stt, vad, packets=2, hold_s=1.2))
    out = capsys.readouterr().out
    assert "END_WINDOW watchdog" not in out  # 服务端还在出新内容=不收割
    assert _neg_seqs(calls["ws"]) == []


def test_watchdog_reanchors_on_second_definite(monkeypatch, capsys):
    """锚点随最新 definite 走：definite①→新内容→definite②（服务端 end_window
    判停第二句）→全文再冻结→以 definite② 起算宽限后开火（definite=2）。"""
    monkeypatch.setattr(da, "_END_WINDOW_WATCHDOG_S", 0.4)
    base._fake_merge(monkeypatch)
    calls = _connect_frames(
        monkeypatch,
        audio=[
            _utts(_PART1, [(_PART1, True)]),
            _utts(_GROWN_TEXT, [(_PART1, False)]),
            _utts(_GROWN_TEXT + "。", [(_PART1, True), (_PART2, True)]),  # 第二句判停
        ],
        last=_utts(_GROWN_TEXT + "。", [(_PART1, True), (_PART2, True)]),
    )
    vad = base._FakeVad()
    stt = _lite(vad)

    events = asyncio.run(_drive_no_end(stt, vad, packets=3, hold_s=1.4))
    finals = [t for n, t in events if n == "FINAL_TRANSCRIPT"]
    out = capsys.readouterr().out
    assert "[doubao] END_WINDOW watchdog fired definite=2" in out
    assert finals == [_PART1, _PART2]  # 两句各一条（分句道）
    assert len(_neg_seqs(calls["ws"])) == 1


def test_watchdog_silent_without_definite(monkeypatch, capsys):
    """无 definite 不开火：服务端只有 interim（从未判停）→ 看门狗无触发前提
    （哪怕全文冻结超宽限）——它只收割「服务端已判停」的段。"""
    monkeypatch.setattr(da, "_END_WINDOW_WATCHDOG_S", 0.35)
    base._fake_merge(monkeypatch)
    calls = _connect_frames(
        monkeypatch,
        audio=[_utts("还在说话中", [("还在说话中", False)])],
        last=_utts("还在说话中。", [("还在说话中。", True)]),
    )
    vad = base._FakeVad()
    stt = _lite(vad)

    asyncio.run(_drive_no_end(stt, vad, packets=1, hold_s=1.2))
    out = capsys.readouterr().out
    assert "END_WINDOW watchdog" not in out
    assert _neg_seqs(calls["ws"]) == []


def test_watchdog_standdown_when_local_end_first(monkeypatch, capsys):
    """本地 END 先到（正常节奏）=看门狗下岗：零观测行、负 seq 恰一次（END 路
    定稿，非看门狗路）。"""
    monkeypatch.setattr(da, "_END_WINDOW_WATCHDOG_S", 0.35)
    base._fake_merge(monkeypatch)
    calls = _connect_frames(
        monkeypatch,
        audio=[_utts(_PART1, [(_PART1, True)])],
        last=_utts(_PART1, [(_PART1, True)]),
    )
    vad = base._FakeVad()
    stt = _lite(vad)

    events = asyncio.run(_drive_no_end(stt, vad, packets=1, hold_s=0.6, end=True))
    names = [n for n, _ in events]
    out = capsys.readouterr().out
    assert "END_WINDOW watchdog" not in out
    assert "END_OF_SPEECH" in names
    assert len(_neg_seqs(calls["ws"])) == 1  # END 路定稿（看门狗未抢跑）


def test_watchdog_counters_reset_next_segment(monkeypatch, capsys):
    """段间记账清零：上一段看门狗收段后，新段无 definite 就绝不再开火（计数不跨
    段携带）。"""
    monkeypatch.setattr(da, "_END_WINDOW_WATCHDOG_S", 0.35)
    base._fake_merge(monkeypatch)
    calls = {"n": 0}

    def _connect(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            fake = base._FakeWS(
                on_audio=[base._srv_frame(_utts(_PART1, [(_PART1, True)]), seq=1)],
                on_last=[base._srv_frame(_utts(_PART1, [(_PART1, True)]), last=True)],
            )
        else:
            fake = base._FakeWS(
                on_audio=[base._srv_frame(
                    _utts("第二段无判停", [("第二段无判停", False)]), seq=1)],
                on_last=[base._srv_frame(_utts("第二段无判停。", [("第二段无判停。", True)]),
                                         last=True)],
            )

        async def _ret():
            return fake

        return _ret()

    monkeypatch.setattr("websockets.connect", _connect)
    vad = base._FakeVad()
    stt = _lite(vad)

    async def scenario():
        stream = _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())
        for _ in range(200):
            await asyncio.sleep(0.005)
            if vad.streams:
                break
        vs = vad.streams[-1]
        # 段1：definite→冻结→看门狗开火。
        vs.q.put_nowait(base._ev(da.vad.VADEventType.START_OF_SPEECH,
                                 frames=[types.SimpleNamespace(data=b"\x11\x11" * 800)]))
        await asyncio.sleep(0.03)
        vs.q.put_nowait(base._ev(da.vad.VADEventType.INFERENCE_DONE,
                                 frames=[types.SimpleNamespace(data=b"\x22\x22" * 3200)]))
        await asyncio.sleep(1.0)
        # 段2（无 definite）：看门狗记账须已清零，不开火。
        vs.q.put_nowait(base._ev(da.vad.VADEventType.START_OF_SPEECH,
                                 frames=[types.SimpleNamespace(data=b"\x33\x33" * 800)]))
        await asyncio.sleep(0.03)
        vs.q.put_nowait(base._ev(da.vad.VADEventType.INFERENCE_DONE,
                                 frames=[types.SimpleNamespace(data=b"\x44\x44" * 3200)]))
        await asyncio.sleep(1.0)
        stream._input_ch.close()
        await asyncio.sleep(0.3)
        await stream.aclose()

    asyncio.run(scenario())
    out = capsys.readouterr().out
    assert out.count("END_WINDOW watchdog fired") == 1  # 只段1开火，段2记账已清


def test_lite_doubao_wiring_watchdog_on(monkeypatch):
    """接线 pin：LiteDoubaoSTT 构造即带看门狗（缺省开）；A 线/旧 B 线装配零触碰。"""
    from agent_runtime.interp_lite.providers.asr_doubao import LiteDoubaoSTT

    for k in ("BOK_INTERP_SERVER_UTT", "BOK_DOUBAO_END_WINDOW_MS"):
        monkeypatch.delenv(k, raising=False)
    lite = LiteDoubaoSTT(api_key="k")
    assert lite._end_window_watchdog is True
    assert lite._server_utterances is True
