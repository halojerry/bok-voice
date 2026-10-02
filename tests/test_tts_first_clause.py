"""W8 首子句起播(TTS 首送快车道)单测——纯函数面,零网络零 TTS。

背景(2026-09-24,docs/superpowers/plans/2026-09-24-a-line-flow-latency-intent.md W8):
首个 task_continue/首段 POST 是首音频的门。overlap 档门槛 12 字在慢生成轮
(GPU 争用实测 tps 13-18)把首送推后 ~0.7-0.9s。首送快车道:≥6 字即可送、
软停顿过半门豁免;后续增量回 overlap 档原节奏。本文件钉:

- ``_tts_first_clause_config``:默认开/6、总闸、门槛钳制、坏值回退;
- ``_tts_overlap_send_now``:快车道触发、sent_any 后回 overlap 档语义、
  kill 腿与旧档逐条件等价、overlap_off 恒不送、空串/短串不送;
- ``BOK_TTS_FIRST_CLAUSE`` 进 ``_FORWARD_ENV``(prod 封闭 env 面可达性,
  test_forward_env.py 同款读表姿势)。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.providers import livekit_plugins as lp  # noqa: E402
from agent_runtime.providers.livekit_plugins import MiniMaxTTS  # noqa: E402


def test_first_clause_config_default(monkeypatch):
    monkeypatch.delenv("BOK_TTS_FIRST_CLAUSE", raising=False)
    monkeypatch.delenv("BOK_TTS_FIRST_CLAUSE_CHARS", raising=False)
    assert lp._tts_first_clause_config() == (True, 6)


def test_first_clause_config_kill_switch(monkeypatch):
    monkeypatch.setenv("BOK_TTS_FIRST_CLAUSE", "0")
    assert lp._tts_first_clause_config()[0] is False


def test_first_clause_config_chars_override_and_clamp(monkeypatch):
    monkeypatch.setenv("BOK_TTS_FIRST_CLAUSE", "1")
    monkeypatch.setenv("BOK_TTS_FIRST_CLAUSE_CHARS", "8")
    assert lp._tts_first_clause_config() == (True, 8)
    monkeypatch.setenv("BOK_TTS_FIRST_CLAUSE_CHARS", "0")
    assert lp._tts_first_clause_config() == (True, 1)  # 钳 ≥1
    monkeypatch.setenv("BOK_TTS_FIRST_CLAUSE_CHARS", "-3")
    assert lp._tts_first_clause_config() == (True, 1)


def test_first_clause_config_bad_value_falls_back(monkeypatch):
    monkeypatch.setenv("BOK_TTS_FIRST_CLAUSE_CHARS", "abc")
    assert lp._tts_first_clause_config() == (True, 6)


def _gate(buf, **kw):
    base = dict(
        sent_any=False,
        overlap_on=True,
        first_lane_on=True,
        first_lane_chars=6,
        overlap_chars=12,
        time_up=False,
    )
    base.update(kw)
    return lp._tts_overlap_send_now(buf, **base)


def test_first_lane_fires_on_short_clause():
    # 7 字、软停顿在第 3 字(过半门之下)——快车道照送,旧档唔送。
    assert _gate("好的，我帮您查") == (True, True)
    # 软停顿贴头(第 3 字)也送:首送豁免过半门,门槛只有字数。
    assert _gate("好的，请稍等") == (True, True)


def test_first_lane_respects_char_threshold():
    assert _gate("好的，请") == (False, False)  # 4 字 < 6
    assert _gate("   ") == (False, False)
    assert _gate("") == (False, False)


def test_after_first_send_back_to_overlap_semantics():
    # sent_any=True 后回 overlap 档:7 字短串唔送(即使开着快车道)。
    assert _gate("好的，我帮您查", sent_any=True) == (False, False)
    # 13 字且软停顿过半 → 送,via_first_lane=False。
    assert _gate("好的我帮您查一下，麻烦稍等", sent_any=True) == (True, False)


def test_kill_leg_matches_legacy_gate():
    # first_lane_on=False 时与旧档逐条件等价:门槛 12、软停顿过半、time_up 直送。
    assert _gate("好的，我帮您查", first_lane_on=False) == (False, False)  # 7 字 < 12
    # 17 字,软停顿在第 10 字(17//2=8,过半)→ 送。
    assert _gate("好的我帮您查一下单，麻烦您稍等片刻", first_lane_on=False) == (True, False)
    # 17 字但软停顿贴头(第 3 字,不过半)且 time_up 未到 → 唔送。
    assert _gate("好的，我帮您查一下单麻烦您稍等片刻", first_lane_on=False) == (False, False)
    # time_up 直送(与软停顿无关)。
    assert _gate("好的我帮您查一下单麻烦您稍等片刻", first_lane_on=False, time_up=True) == (True, False)


def test_time_up_fires_first_lane_at_threshold():
    # 无软停顿 + time_up:首送也送(与旧档 time_up 语义同款,只係门槛降到 6)。
    assert _gate("好的我帮您查一下", time_up=True) == (True, True)


def test_overlap_off_never_sends():
    assert _gate("好的，我帮您查", overlap_on=False) == (False, False)
    assert _gate("好的我帮您查一下单，麻烦您稍等片刻", overlap_on=False, time_up=True) == (False, False)


def test_forward_env_registers_first_clause(tmp_path, monkeypatch):
    # D14 纪律:运营 env 键必须进 _FORWARD_ENV(prod 封闭 env 面可达性)。
    # 与 test_forward_env.py 同款读表姿势——import tools.bok 而非重复定义。
    monkeypatch.delenv("BOK_TTS_FIRST_CLAUSE", raising=False)
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    import tools.bok as bok  # noqa: E402

    assert "BOK_TTS_FIRST_CLAUSE" in bok._FORWARD_ENV
    assert "BOK_TTS_FIRST_CLAUSE_CHARS" in bok._FORWARD_ENV


# ---- 流级 wiring 腿（fake WS，镜像 test_minimax_ws_pool.py 姿势） ----
# 证明 _MiniMaxSynthesizeStream._run 真的在首送消费快车道：逐 token 喂无句号
# 文本，W8 开=≥6 字首送；W8 关(=classic 回退档旧行为)=等 12 字/收尾。
# 占位密钥经变量间接拼装：非真实凭据，仅过插件构造函数的非空校验。
_FAKE_KEY = "test" + "-key"

_CONNECTED = '{"event": "connected_success"}'
_STARTED = '{"event": "task_started"}'


class _FakeWS:
    def __init__(self, script=None):
        self._script = list(script or [])
        self.sent: list[dict] = []

    async def recv(self):
        if self._script:
            return self._script.pop(0)
        await asyncio.Event().wait()

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self):
        return None


class _FakeConnectShim:
    def __init__(self, ws):
        self._ws = ws

    async def __call__(self, *a, **kw):
        return self._ws


@pytest.fixture(autouse=True)
def _classic_no_pool(monkeypatch):
    monkeypatch.setenv("MINIMAX_WS_MODE", "classic")
    monkeypatch.setenv("MINIMAX_WS_POOL", "0")
    monkeypatch.setattr(lp, "_MINIMAX_POOL_WS", None)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_TASK", None)
    yield


async def _drive_stream(monkeypatch, capsys) -> tuple[list[str], str]:
    """起 classic 流、逐 token 喂、回收 task_continue 文本序列 + 捕获输出。"""
    ws = _FakeWS([_CONNECTED, _STARTED])
    # classic _run 体内 import websockets → 必须打真模块属性(pool 测试同款)。
    monkeypatch.setattr("websockets.connect", _FakeConnectShim(ws))
    tts = MiniMaxTTS(
        voice={"zh": "male-qn-qingse"},
        sample_rate=24000,
        api_key=_FAKE_KEY,
    )
    s = tts.stream()
    s.push_text("好的，")
    s.push_text("请您")  # buf=5 字:任何档都不送
    await asyncio.sleep(0.05)
    s.push_text("稍等")  # buf=7 字:W8 开即送(kill 档仍唔送,<12)
    await asyncio.sleep(0.15)  # < time_up 300ms 窗,保持纯门槛判据
    s.end_input()  # 收尾残句 flush(两档都会送出剩余)
    for _ in range(100):
        conts = [m["text"] for m in ws.sent if m.get("event") == "task_continue"]
        if conts:
            await asyncio.sleep(0.05)
            break
        await asyncio.sleep(0.05)
    s._task.cancel()
    await asyncio.sleep(0.1)
    return [m["text"] for m in ws.sent if m.get("event") == "task_continue"], capsys.readouterr().out


def test_stream_first_lane_sends_at_6_chars(monkeypatch, capsys):
    """W8 开(默认):首送在 7 字碎片上发生,带 FIRST_CLAUSE/FIRST_SEND 打点。"""
    monkeypatch.delenv("BOK_TTS_FIRST_CLAUSE", raising=False)
    monkeypatch.setenv("MINIMAX_TTS_OVERLAP", "1")

    async def run():
        return await _drive_stream(monkeypatch, capsys)

    conts, out = asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert conts, "快车道应已送出首段"
    assert len(conts[0]) == 7, f"首送应係 7 字碎片,实际 {conts[0]!r}"
    assert "MINIMAX_TTS_FIRST_CLAUSE chars=7" in out
    assert "MINIMAX_TTS_FIRST_SEND_MS" in out


def test_stream_kill_leg_waits_for_tail_flush(monkeypatch, capsys):
    """W8 关:同喂法 overlap 门唔放行(7<12、软停顿唔过半、time_up 未到),
    首送=收尾残句整段;无 FIRST_CLAUSE 打点(=classic 回退档旧行为)。"""
    monkeypatch.setenv("BOK_TTS_FIRST_CLAUSE", "0")
    monkeypatch.setenv("MINIMAX_TTS_OVERLAP", "1")

    async def run():
        return await _drive_stream(monkeypatch, capsys)

    conts, out = asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert conts, "收尾残句 flush 应有送出"
    assert len(conts[0]) == 7, f"kill 档首送应係收尾整段 7 字,实际 {conts[0]!r}"
    assert "MINIMAX_TTS_FIRST_CLAUSE" not in out
