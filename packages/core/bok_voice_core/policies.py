from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .types import CallMode, ProviderKind, SessionManifest


class ProviderState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILOVER = "failover"
    QUARANTINED = "quarantined"
    RECOVERY = "recovery"


@dataclass
class ProviderHealth:
    kind: ProviderKind
    name: str
    state: ProviderState = ProviderState.HEALTHY
    last_error: str = ""


@dataclass
class FailoverPolicy:
    max_failovers_per_session: int = 1
    timeout_ms: int = 8000
    retry: int = 2


@dataclass
class ProviderSelection:
    kind: ProviderKind
    primary: str
    fallback: str = ""


@dataclass
class ProviderRegistry:
    """Registry of live provider objects by kind/name, plus simple failover."""

    providers: dict[str, dict[str, object]] = field(default_factory=dict)
    health: dict[str, ProviderHealth] = field(default_factory=dict)

    def register(self, kind: str, name: str, provider: object) -> None:
        self.providers.setdefault(kind, {})[name] = provider
        self.health[f"{kind}:{name}"] = ProviderHealth(
            kind=ProviderKind(kind), name=name
        )

    def active(self, kind: str) -> object | None:
        available = self.providers.get(kind, {})
        for name in available:
            health = self.health.get(f"{kind}:{name}")
            if health and health.state in {ProviderState.HEALTHY, ProviderState.DEGRADED}:
                return available[name]
        return None

    def mark(self, kind: str, name: str, state: ProviderState, error: str = "") -> None:
        self.health[f"{kind}:{name}"] = ProviderHealth(
            kind=ProviderKind(kind), name=name, state=state, last_error=error
        )


def select_session_manifest(
    *,
    session_id: str,
    account_id: str,
    object_id: str,
    persona_id: str,
    mode: CallMode,
    direction: str = "webrtc",
    language: str = "zh",
    providers: dict[str, str] | None = None,
    policy: str = "offline_first",
    tts_reference_voice: str = "",
) -> SessionManifest:
    return SessionManifest(
        session_id=session_id,
        account_id=account_id,
        object_id=object_id,
        persona_id=persona_id,
        mode=mode,
        direction=direction,
        language=language,
        providers=providers or {"vad": "livekit", "asr": "sherpa", "llm": "mlx", "tts": "gpt_sovits"},
        policy=policy,
        tts_reference_voice=tts_reference_voice,
    )
