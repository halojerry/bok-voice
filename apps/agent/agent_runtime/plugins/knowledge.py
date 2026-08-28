from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from bok_voice_core.types import TurnEvent


@dataclass
class KnowledgePlugin:
    """Thin wrapper around a KnowledgeService; MVP delegates to the service."""

    service: object = None  # KnowledgeService

    def observe(self, turn: TurnEvent) -> dict:
        if self.service is None:
            return {"observed": False}
        return self.service.observe(turn)
