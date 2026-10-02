"""Qwen Realtime（DashScope）适配器离线单测——不起 WS、零网络、零密钥。

主面：model_family 三族判定 / voice_for_model 族白名单回落 / WS URL+鉴权头组装 /
session.update 旧平铺键集收紧 / kill-switch 总闸 / 源文件卫生（无密钥字面量、
无旧粤语拼写字面）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from openai.types.realtime import (
    RealtimeAudioConfig,
    RealtimeAudioConfigInput,
    RealtimeAudioConfigOutput,
    RealtimeSessionCreateRequest,
)
from openai.types.realtime.realtime_audio_formats import AudioPCM
from openai.types.realtime.realtime_audio_input_turn_detection import ServerVad

from agent_runtime.providers import qwen_realtime

_SRC = Path(qwen_realtime.__file__).read_text(encoding="utf-8")

_AUDIO3_MODEL = "qwen-audio-3.0-realtime-flash"
_OMNI_MODEL = "qwen3-omni-flash-realtime"


# —— model_family：三族判定 ————————————————————————————————


def test_model_family_audio3() -> None:
    assert qwen_realtime.model_family(_AUDIO3_MODEL) == "audio3"
    assert qwen_realtime.model_family("qwen-audio-3.0-realtime-plus") == "audio3"
    # 目录里的 3.1 快照同族（前缀 qwen-audio- + realtime 型）
    assert qwen_realtime.model_family("qwen-audio-3.1-realtime-plus") == "audio3"


def test_model_family_omni() -> None:
    assert qwen_realtime.model_family(_OMNI_MODEL) == "omni"
    # 日期快照后缀同族
    assert qwen_realtime.model_family("qwen3-omni-flash-realtime-2025-09-15") == "omni"


def test_model_family_unknown() -> None:
    assert qwen_realtime.model_family("gpt-realtime") is None
    # realtime ASR / 翻译型不是 S2S 会话模型，不属任何音色族
    assert qwen_realtime.model_family("qwen3-asr-flash-realtime") is None
    assert qwen_realtime.model_family("qwen3-livetranslate-flash-realtime") is None
    assert qwen_realtime.model_family("") is None
    assert qwen_realtime.model_family("qwen-plus") is None


# —— voice_for_model：族内放行 + 族外回落 —————————————————————————


def test_voice_in_family_passes_through() -> None:
    assert qwen_realtime.voice_for_model(_AUDIO3_MODEL, "longanqian") == "longanqian"
    assert qwen_realtime.voice_for_model(_OMNI_MODEL, "Cherry") == "Cherry"
    # 大小写宽容：回白名单规范拼写
    assert qwen_realtime.voice_for_model(_OMNI_MODEL, "cherry") == "Cherry"


def test_voice_cross_family_falls_back_to_family_default() -> None:
    # omni 族音色喂 audio3 族 → 族默认（V7 run1 实弹：族外音色整条 session.update 被拒）
    assert qwen_realtime.voice_for_model(_AUDIO3_MODEL, "Chelsie") == "longanqian"
    assert qwen_realtime.voice_for_model(_OMNI_MODEL, "longanqian") == "Cherry"
    # 乱造音色同样回落
    assert qwen_realtime.voice_for_model(_OMNI_MODEL, "narrator-42") == "Cherry"


def test_voice_empty_wanted_gets_family_default_without_fallback() -> None:
    assert qwen_realtime.voice_for_model(_AUDIO3_MODEL, None) == "longanqian"
    assert qwen_realtime.voice_for_model(_OMNI_MODEL, "") == "Cherry"


def test_voice_unknown_family_passthrough() -> None:
    # 未知族：构造侧另行拒绝，纯函数兜底原样放行
    assert qwen_realtime.voice_for_model("gpt-realtime", "marin") == "marin"


# —— WS URL + 鉴权头组装（差异 #1）——————————————————————————


def _bare_session(model: str = _AUDIO3_MODEL) -> qwen_realtime.QwenRealtimeSession:
    sess = qwen_realtime.QwenRealtimeSession.__new__(qwen_realtime.QwenRealtimeSession)
    sess._opts = SimpleNamespace(
        base_url=qwen_realtime.QWEN_REALTIME_WS_BASE, model=model, api_key="k-test"
    )
    return sess


def test_ws_url_and_headers() -> None:
    url, headers = _bare_session()._create_ws_url_and_headers()
    assert url == (
        "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen-audio-3.0-realtime-flash"
    )
    # 小写 bearer（probe 同款），无 Azure api-key 头
    assert headers["Authorization"] == "bearer k-test"
    assert "api-key" not in headers


def test_ws_constant() -> None:
    assert qwen_realtime.QWEN_REALTIME_WS_BASE == (
        "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    )


# —— session.update 旧平铺键集（差异 #5/#6）——————————————————————


def test_session_update_flat_full_shape() -> None:
    fmt = AudioPCM(rate=24000, type="audio/pcm")
    req = RealtimeSessionCreateRequest(
        type="realtime",
        model=_AUDIO3_MODEL,
        output_modalities=["audio"],
        audio=RealtimeAudioConfig(
            input=RealtimeAudioConfigInput(
                format=fmt,
                noise_reduction=None,
                transcription=None,
                turn_detection=None,  # manual 档 → 显式 null（probe3 实弹）
            ),
            output=RealtimeAudioConfigOutput(format=fmt, speed=1.0, voice="longanqian"),
        ),
        max_output_tokens="inf",  # 官方默认；DashScope 平铺不外发
        tool_choice="auto",  # 官方 to_oai_tool_choice(None) 的产物；无 function calling 必须剥
    )
    flat = qwen_realtime._session_to_dashscope_flat(req)
    assert flat == {
        "modalities": ["audio", "text"],
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "turn_detection": None,
        "voice": "longanqian",
    }
    for banned in (
        "model",
        "tools",
        "tool_choice",
        "speed",
        "max_response_output_tokens",
        "input_audio_transcription",
        "tracing",
        "reasoning",
    ):
        assert banned not in flat


def test_session_update_flat_partial_only_set_keys() -> None:
    req = RealtimeSessionCreateRequest(type="realtime")
    req.audio = RealtimeAudioConfig(output=RealtimeAudioConfigOutput(voice="Cherry"))
    assert qwen_realtime._session_to_dashscope_flat(req) == {"voice": "Cherry"}


def test_session_update_flat_turn_detection_projection() -> None:
    td = ServerVad(
        type="server_vad",
        threshold=0.5,
        prefix_padding_ms=300,
        silence_duration_ms=200,
        create_response=True,
    )
    req = RealtimeSessionCreateRequest(
        type="realtime",
        audio=RealtimeAudioConfig(input=RealtimeAudioConfigInput(turn_detection=td)),
    )
    flat = qwen_realtime._session_to_dashscope_flat(req)
    assert flat["turn_detection"] == {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 200,
        "create_response": True,
    }


# —— 构造：kill-switch / 音色映射 / 档位补钉（零网络）—————————————————


def test_kill_switch_off_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOK_QWEN_REALTIME", "0")
    with pytest.raises(RuntimeError, match="BOK_QWEN_REALTIME"):
        qwen_realtime.QwenRealtimeModel(model=_OMNI_MODEL, api_key="k-test")


def test_construct_default_and_patches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOK_QWEN_REALTIME", raising=False)
    monkeypatch.delenv("QWEN_REALTIME_BASE_URL", raising=False)
    m = qwen_realtime.QwenRealtimeModel(
        model=_AUDIO3_MODEL, api_key="k-test", voice="Chelsie", instructions="你好"
    )
    # 族外音色构造即回落（不发错 session.update）
    assert m._opts.voice == "longanqian"
    assert m._opts.base_url == qwen_realtime.QWEN_REALTIME_WS_BASE
    assert m._opts.is_azure is True  # 官方遗留档：旧事件名/旧 content type 归一
    assert m._opts.api_version  # 非空哨兵
    assert m._provider_label == "DashScope Qwen Realtime"
    assert m.capabilities.user_transcription is True  # 服务端默认转写恒开
    assert m._qwen_instructions == "你好"


def test_construct_voice_mapping_omni(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOK_QWEN_REALTIME", raising=False)
    m = qwen_realtime.QwenRealtimeModel(
        model=_OMNI_MODEL, api_key="k-test", voice="cherry"
    )
    assert m._opts.voice == "Cherry"


def test_construct_rejects_unknown_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOK_QWEN_REALTIME", raising=False)
    with pytest.raises(ValueError, match="模型族"):
        qwen_realtime.QwenRealtimeModel(model="gpt-realtime", api_key="k-test")


def test_construct_requires_api_key_param(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOK_QWEN_REALTIME", raising=False)
    with pytest.raises(ValueError, match="api_key"):
        qwen_realtime.QwenRealtimeModel(model=_OMNI_MODEL, api_key="")


# —— 源文件卫生 ————————————————————————————————————————


def test_no_secret_literals_in_source() -> None:
    assert "sk-" not in _SRC
    # api_key 只从构造参数来：模块零 key env 读取（源内不出现任何大写 KEY env 名）
    assert "API_KEY" not in _SRC


def test_no_legacy_cantonese_literal_in_source() -> None:
    legacy = "y" + "ue"  # 字面拆写：本测试文件自身不得含旧拼写
    assert legacy not in _SRC.lower()


# —— 构造级 instructions 补发姿势（审修 2026-09-26 源级 pin）—————————
# 官方 RealtimeSession.__init__ 先 `self._instructions = None`
# （realtime_model.py:904）再发首发 session.update（:908）——super 之前
# 前置赋值会被清掉。唯一正确姿势=super 之后 `_qwen_instructions` 非空则
# 落 _instructions 并补发一枚完整 session.update。官方 session 构造需要
# 事件循环（__init__ 内 create_task 起连接任务），离线只能源级钉姿势。


def test_instructions_resent_after_super_source_pin() -> None:
    session_init = _SRC.split("class QwenRealtimeSession", 1)[1]
    # super().__init__ 之后、_bstream 之前的补发块顺序正确
    assert "super().__init__(realtime_model" in session_init
    assert "_qwen_instructions" in session_init
    assert "_create_session_update_event()" in session_init
    # 陷阱姿势绝不回归：super 之前不得再从 _qwen_instructions 前置赋值
    # （注释里引用官方 904 行清空行为不算违规，查特征串）
    pre_super = session_init.split("super().__init__", 1)[0]
    assert "_qwen_instructions" not in pre_super
