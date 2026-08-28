from __future__ import annotations

import struct

import pytest

from agent_runtime.providers.volc_v3_protocol import (
    EventType,
    MsgType,
    MsgTypeFlagBits,
    Message,
)


def test_oneshot_request_marshal_matches_talky_header():
    """单向流式一帧 FullClientRequest（NoSeq）=> header 0x11 0x10 0x10 0x00 + payload。"""
    body = b'{"user":{"uid":"bok-voice"},"req_params":{"text":"hi"}}'
    msg = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.NoSeq, payload=body)
    raw = msg.marshal()
    assert raw[:4] == bytes([0x11, 0x10, 0x10, 0x00])
    payload_len = struct.unpack(">I", raw[4:8])[0]
    assert payload_len == len(body)
    assert raw[8:] == body


def test_audio_response_roundtrip():
    """AudioOnlyServer + WithEvent => event + session_id + connect_id + payload 解析正确。"""
    session_id = "session-123"
    audio = b"\x00\x01" * 100
    msg = Message(
        type=MsgType.AudioOnlyServer,
        flag=MsgTypeFlagBits.WithEvent,
        event=EventType.TTSResponse,
        session_id=session_id,
        payload=audio,
    )
    raw = msg.marshal()
    parsed = Message.from_bytes(raw)
    assert parsed.type == MsgType.AudioOnlyServer
    assert parsed.flag == MsgTypeFlagBits.WithEvent
    assert parsed.event == EventType.TTSResponse
    assert parsed.session_id == session_id
    assert parsed.payload == audio


def test_full_server_response_roundtrip():
    """FullServerResponse + WithEvent（TTSSentenceStart/SessionFinished）解析正确。"""
    payload = b'{"res_params":{}}'
    msg = Message(
        type=MsgType.FullServerResponse,
        flag=MsgTypeFlagBits.WithEvent,
        event=EventType.SessionFinished,
        session_id="s-1",
        payload=payload,
    )
    parsed = Message.from_bytes(msg.marshal())
    assert parsed.type == MsgType.FullServerResponse
    assert parsed.event == EventType.SessionFinished
    assert parsed.payload == payload


def test_error_frame_roundtrip():
    """Error 帧携带 error_code + payload。"""
    msg = Message(type=MsgType.Error, flag=MsgTypeFlagBits.NoSeq, error_code=55000000, payload=b"{}")
    parsed = Message.from_bytes(msg.marshal())
    assert parsed.type == MsgType.Error
    assert parsed.error_code == 55000000
    assert parsed.payload == b"{}"


def test_short_data_raises():
    with pytest.raises(ValueError):
        Message.from_bytes(b"\x11")
