"""Volcengine (火山) TTS V3 WebSocket 二进制协议（独立、可单测、可替换）。

对应官方协议包 ``TTS Websocket Bidirection protocols.zip`` 与
``websocket unidirectional.zip`` 中的 ``protocols.py``。我们只保留本系统
（Book Voice 单向流式 TTS）需要用到的编解码能力，并将其抽成独立模块，以便：

- 每一层可独立替换 / 独立测试 / 独立降级；
- 不把火山私有协议细节散落在插件类里。

帧格式（version, header_size, msg_type, flag, serialization, compression,
event, session_id, connect_id, sequence, error_code, payload），
请参考官方 ``Message.marshal()`` 实现；这里保持一致。
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable

import websockets


class MsgType(IntEnum):
    """消息类型。"""

    Invalid = 0
    FullClientRequest = 0b1
    AudioOnlyClient = 0b10
    FullServerResponse = 0b1001
    AudioOnlyServer = 0b1011
    FrontEndResultServer = 0b1100
    Error = 0b1111


class MsgTypeFlagBits(IntEnum):
    """消息类型 flag 位。"""

    NoSeq = 0
    PositiveSeq = 0b1
    LastNoSeq = 0b10
    NegativeSeq = 0b11
    WithEvent = 0b100


class VersionBits(IntEnum):
    Version1 = 1
    Version2 = 2
    Version3 = 3
    Version4 = 4


class HeaderSizeBits(IntEnum):
    HeaderSize4 = 1
    HeaderSize8 = 2
    HeaderSize12 = 3
    HeaderSize16 = 4


class SerializationBits(IntEnum):
    Raw = 0
    JSON = 0b1
    Thrift = 0b11
    Custom = 0b1111


class CompressionBits(IntEnum):
    None_ = 0
    Gzip = 0b1
    Custom = 0b1111


class EventType(IntEnum):
    """事件类型。"""

    None_ = 0
    # 连接（上游）
    StartConnection = 1
    FinishConnection = 2
    # 连接（下游）
    ConnectionStarted = 50
    ConnectionFailed = 51
    ConnectionFinished = 52
    # 会话（上游）
    StartSession = 100
    CancelSession = 101
    FinishSession = 102
    # 会话（下游）
    SessionStarted = 150
    SessionCanceled = 151
    SessionFinished = 152
    SessionFailed = 153
    UsageResponse = 154
    # 通用的上游请求
    TaskRequest = 200
    UpdateConfig = 201
    # TTS（下游）
    TTSSentenceStart = 350
    TTSSentenceEnd = 351
    TTSResponse = 352
    TTSSubtitle = 364


@dataclass
class Message:
    """火山 V3 TTS 协议消息对象。"""

    version: VersionBits = VersionBits.Version1
    header_size: HeaderSizeBits = HeaderSizeBits.HeaderSize4
    type: MsgType = MsgType.Invalid
    flag: MsgTypeFlagBits = MsgTypeFlagBits.NoSeq
    serialization: SerializationBits = SerializationBits.JSON
    compression: CompressionBits = CompressionBits.None_

    event: EventType | int = EventType.None_
    session_id: str = ""
    connect_id: str = ""
    sequence: int = 0
    error_code: int = 0

    payload: bytes = b""

    @classmethod
    def from_bytes(cls, data: bytes) -> "Message":
        """从字节流解析消息。"""
        if len(data) < 3:
            raise ValueError(f"数据过短：期望至少 3 字节，实得 {len(data)}")
        type_and_flag = data[1]
        msg_type = MsgType(type_and_flag >> 4)
        flag = MsgTypeFlagBits(type_and_flag & 0b00001111)
        msg = cls(type=msg_type, flag=flag)
        msg.unmarshal(data)
        return msg

    def marshal(self) -> bytes:
        """序列化消息为字节流。"""
        buffer = io.BytesIO()
        header = [
            (self.version << 4) | self.header_size,
            (self.type << 4) | self.flag,
            (self.serialization << 4) | self.compression,
        ]
        header_size = 4 * self.header_size
        if padding := header_size - len(header):
            header.extend([0] * padding)
        buffer.write(bytes(header))

        for writer in self._get_writers():
            writer(buffer)
        return buffer.getvalue()

    def unmarshal(self, data: bytes) -> None:
        """从字节流反序列化消息。"""
        buffer = io.BytesIO(data)
        version_and_header_size = buffer.read(1)[0]
        self.version = VersionBits(version_and_header_size >> 4)
        self.header_size = HeaderSizeBits(version_and_header_size & 0b00001111)
        buffer.read(1)
        serialization_compression = buffer.read(1)[0]
        self.serialization = SerializationBits(serialization_compression >> 4)
        self.compression = CompressionBits(serialization_compression & 0b00001111)

        header_size = 4 * self.header_size
        read_size = 3
        if padding_size := header_size - read_size:
            buffer.read(padding_size)

        for reader in self._get_readers():
            reader(buffer)
        remaining = buffer.read()
        if remaining:
            raise ValueError(f"消息解析后仍有残留数据：{remaining}")

    def _get_writers(self) -> list[Callable[[io.BytesIO], None]]:
        writers: list[Callable[[io.BytesIO], None]] = []
        if self.flag == MsgTypeFlagBits.WithEvent:
            writers.extend([self._write_event, self._write_session_id])
        if self.type in [
            MsgType.FullClientRequest,
            MsgType.FullServerResponse,
            MsgType.FrontEndResultServer,
            MsgType.AudioOnlyClient,
            MsgType.AudioOnlyServer,
        ]:
            if self.flag in [MsgTypeFlagBits.PositiveSeq, MsgTypeFlagBits.NegativeSeq]:
                writers.append(self._write_sequence)
        elif self.type == MsgType.Error:
            writers.append(self._write_error_code)
        else:
            raise ValueError(f"不支持的消息类型：{self.type}")
        writers.append(self._write_payload)
        return writers

    def _get_readers(self) -> list[Callable[[io.BytesIO], None]]:
        readers: list[Callable[[io.BytesIO], None]] = []
        if self.type in [
            MsgType.FullClientRequest,
            MsgType.FullServerResponse,
            MsgType.FrontEndResultServer,
            MsgType.AudioOnlyClient,
            MsgType.AudioOnlyServer,
        ]:
            if self.flag in [MsgTypeFlagBits.PositiveSeq, MsgTypeFlagBits.NegativeSeq]:
                readers.append(self._read_sequence)
        elif self.type == MsgType.Error:
            readers.append(self._read_error_code)
        else:
            raise ValueError(f"不支持的消息类型：{self.type}")
        if self.flag == MsgTypeFlagBits.WithEvent:
            readers.extend([self._read_event, self._read_session_id, self._read_connect_id])
        readers.append(self._read_payload)
        return readers

    def _write_event(self, buffer: io.BytesIO) -> None:
        buffer.write(struct.pack(">i", int(self.event)))

    def _write_session_id(self, buffer: io.BytesIO) -> None:
        if self.event in [
            EventType.StartConnection,
            EventType.FinishConnection,
            EventType.ConnectionStarted,
            EventType.ConnectionFailed,
        ]:
            return
        session_bytes = self.session_id.encode("utf-8")
        size = len(session_bytes)
        if size > 0xFFFFFFFF:
            raise ValueError(f"Session ID 过长 ({size} bytes)")
        buffer.write(struct.pack(">I", size))
        if size > 0:
            buffer.write(session_bytes)

    def _write_sequence(self, buffer: io.BytesIO) -> None:
        buffer.write(struct.pack(">i", self.sequence))

    def _write_error_code(self, buffer: io.BytesIO) -> None:
        buffer.write(struct.pack(">I", self.error_code))

    def _write_payload(self, buffer: io.BytesIO) -> None:
        size = len(self.payload)
        if size > 0xFFFFFFFF:
            raise ValueError(f"Payload 过长 ({size} bytes)")
        buffer.write(struct.pack(">I", size))
        buffer.write(self.payload)

    def _read_event(self, buffer: io.BytesIO) -> None:
        event_bytes = buffer.read(4)
        if event_bytes:
            event_value = struct.unpack(">i", event_bytes)[0]
            try:
                self.event = EventType(event_value)
            except ValueError:
                self.event = event_value

    def _read_session_id(self, buffer: io.BytesIO) -> None:
        if self.event in [
            EventType.StartConnection,
            EventType.FinishConnection,
            EventType.ConnectionStarted,
            EventType.ConnectionFailed,
            EventType.ConnectionFinished,
        ]:
            return
        size_bytes = buffer.read(4)
        if size_bytes:
            size = struct.unpack(">I", size_bytes)[0]
            if size > 0:
                self.session_id = buffer.read(size).decode("utf-8")

    def _read_connect_id(self, buffer: io.BytesIO) -> None:
        if self.event in [
            EventType.ConnectionStarted,
            EventType.ConnectionFailed,
            EventType.ConnectionFinished,
        ]:
            size_bytes = buffer.read(4)
            if size_bytes:
                size = struct.unpack(">I", size_bytes)[0]
                if size > 0:
                    self.connect_id = buffer.read(size).decode("utf-8")

    def _read_sequence(self, buffer: io.BytesIO) -> None:
        sequence_bytes = buffer.read(4)
        if sequence_bytes:
            self.sequence = struct.unpack(">i", sequence_bytes)[0]

    def _read_error_code(self, buffer: io.BytesIO) -> None:
        error_code_bytes = buffer.read(4)
        if error_code_bytes:
            self.error_code = struct.unpack(">I", error_code_bytes)[0]

    def _read_payload(self, buffer: io.BytesIO) -> None:
        size_bytes = buffer.read(4)
        if size_bytes:
            size = struct.unpack(">I", size_bytes)[0]
            if size > 0:
                self.payload = buffer.read(size)

    def __repr__(self) -> str:
        return (
            f"Message(type={self.type}, flag={self.flag}, event={self.event}, "
            f"session_id={self.session_id!r}, connect_id={self.connect_id!r}, "
            f"sequence={self.sequence}, error_code={self.error_code}, "
            f"payload_size={len(self.payload)})"
        )


async def receive_message(ws: "websockets.WebSocketClientProtocol") -> Message:
    """从 WebSocket 读取一条协议消息。"""
    data = await ws.recv()
    if isinstance(data, str):
        raise ValueError(f"收到意料之外的文本消息：{data}")
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError(f"收到意料之外的消息类型：{type(data)}")
    return Message.from_bytes(bytes(data))


async def start_connection(ws: "websockets.WebSocketClientProtocol") -> None:
    msg = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.WithEvent)
    msg.event = EventType.StartConnection
    msg.payload = b"{}"
    await ws.send(msg.marshal())


async def start_session(
    ws: "websockets.WebSocketClientProtocol", payload: bytes, session_id: str
) -> None:
    msg = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.WithEvent)
    msg.event = EventType.StartSession
    msg.session_id = session_id
    msg.payload = payload
    await ws.send(msg.marshal())


async def task_request(
    ws: "websockets.WebSocketClientProtocol", payload: bytes, session_id: str
) -> None:
    msg = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.WithEvent)
    msg.event = EventType.TaskRequest
    msg.session_id = session_id
    msg.payload = payload
    await ws.send(msg.marshal())


async def finish_session(
    ws: "websockets.WebSocketClientProtocol", session_id: str
) -> None:
    msg = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.WithEvent)
    msg.event = EventType.FinishSession
    msg.session_id = session_id
    msg.payload = b"{}"
    await ws.send(msg.marshal())


async def cancel_session(
    ws: "websockets.WebSocketClientProtocol", session_id: str
) -> None:
    msg = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.WithEvent)
    msg.event = EventType.CancelSession
    msg.session_id = session_id
    msg.payload = b"{}"
    await ws.send(msg.marshal())


async def finish_connection(ws: "websockets.WebSocketClientProtocol") -> None:
    msg = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.WithEvent)
    msg.event = EventType.FinishConnection
    msg.payload = b"{}"
    await ws.send(msg.marshal())

