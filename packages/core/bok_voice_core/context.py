from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .types import ContextBundle, ObjectProfile, PersonaProfile


@runtime_checkable
class ContextAssembler(Protocol):
    def assemble(self, **kwargs: Any) -> ContextBundle: ...


@dataclass
class DefaultContextAssembler:
    """Builds a structured ContextBundle without injecting raw history."""

    def assemble(
        self,
        *,
        object_profile: ObjectProfile | None = None,
        persona: PersonaProfile | None = None,
        product_snippets: list[dict] | None = None,
        history_snippets: list[dict] | None = None,
        current_turns: list[dict] | None = None,
        global_hints: list[dict] | None = None,
    ) -> ContextBundle:
        system_parts: list[str] = []
        if persona:
            system_parts.append(f"你是{persona.name}，代表{persona.company}。语气：{persona.tone}。")
        if object_profile:
            system_parts.append(
                f"当前对话对象：{object_profile.display_name}（{object_profile.role_template}，{object_profile.language}）。"
            )
        return ContextBundle(
            system_prompt="\n".join(system_parts),
            object_card=object_profile.__dict__ if object_profile else {},
            product_snippets=product_snippets or [],
            history_snippets=history_snippets or [],
            current_turns=current_turns or [],
            global_hints=global_hints or [],
            token_estimate=sum(len(str(x)) for x in (product_snippets, history_snippets, current_turns)),
            sources=(product_snippets or []) + (history_snippets or []),
        )


def build_context(**kwargs: Any) -> ContextBundle:
    return DefaultContextAssembler().assemble(**kwargs)
