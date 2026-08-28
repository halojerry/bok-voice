from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from bok_voice_core.context import DefaultContextAssembler
from bok_voice_core.types import ContextBundle, SessionManifest


@dataclass
class ContextInjector:
    """Builds the ContextBundle for the LLM from structured business data."""

    assembler: DefaultContextAssembler = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.assembler is None:
            self.assembler = DefaultContextAssembler()

    def prepare(self, manifest: SessionManifest, object_card: dict, persona: dict) -> ContextBundle:
        return self.assembler.assemble(
            object_profile=object_card,
            persona=persona,
            product_snippets=[],
            history_snippets=[],
            current_turns=[],
        )
