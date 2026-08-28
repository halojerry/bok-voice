from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, runtime_checkable

from .types import ContextBundle, SessionManifest, TurnEvent


@dataclass
class TranscriptionEvent:
    text: str
    emotion: str = ""
    is_final: bool = False


@dataclass
class LLMEvent:
    text: str = ""
    emotion: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    done: bool = False


@dataclass
class AudioEvent:
    chunk: bytes = b""
    is_final: bool = False


@runtime_checkable
class VADProvider(Protocol):
    def detect_segments(self, audio: bytes) -> list[dict]: ...


@runtime_checkable
class ASRProvider(Protocol):
    async def transcribe(self, audio: bytes, language: str = "zh") -> TranscriptionEvent: ...


@runtime_checkable
class LLMProvider(Protocol):
    def stream_chat(self, context: ContextBundle) -> AsyncIterator[LLMEvent]: ...


@runtime_checkable
class TTSProvider(Protocol):
    async def synthesize(self, text: str, voice: str = "", emotion: str = "") -> list[AudioEvent]: ...


@runtime_checkable
class KnowledgeService(Protocol):
    def search(self, query: str, account_id: str, limit: int = 5) -> list[dict]: ...
    def context(self, task: str, account_id: str, limit: int = 5) -> ContextBundle: ...
    def observe(self, turn: TurnEvent) -> dict: ...
    def import_document(self, account_id: str, path: str, content: str) -> dict: ...


@runtime_checkable
class VectorStore(Protocol):
    async def upsert(self, items: list[dict], account_id: str) -> int: ...
    async def search(self, query: str, account_id: str, limit: int = 5) -> list[dict]: ...
    async def delete(self, account_id: str, ids: list[str]) -> int: ...


@runtime_checkable
class EmbeddingService(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class MarkdownSource(Protocol):
    def read(self, path: str) -> str: ...
    def write(self, path: str, content: str) -> dict: ...
    def versions(self, path: str) -> list[dict]: ...
    def forget(self, path: str) -> dict: ...


@runtime_checkable
class BusinessRepository(Protocol):
    def create_call(self, call: SessionManifest) -> dict: ...
    def get_call(self, call_id: str) -> dict | None: ...
    def update_call(self, call_id: str, **fields) -> dict | None: ...
    def list_calls(self, account_id: str, status: str = "") -> list[dict]: ...
    def create_turn(self, turn: TurnEvent) -> dict: ...
    def get_turns(self, call_id: str) -> list[TurnEvent]: ...
    def get_settlement(self, call_id: str) -> dict | None: ...
    def append_settlement(self, call_id: str, result: dict) -> dict: ...
    def get_object(self, object_id: str) -> dict | None: ...
    def get_persona(self, persona_id: str) -> dict | None: ...
    def list_personas(self, account_id: str = "") -> list[dict]: ...


@runtime_checkable
class SettlementWorker(Protocol):
    def settle(self, call_id: str, transcript: list[TurnEvent]) -> dict: ...


@runtime_checkable
class ProviderRegistryProtocol(Protocol):
    def register(self, kind: str, name: str, provider: object) -> None: ...
    def active(self, kind: str) -> object | None: ...
