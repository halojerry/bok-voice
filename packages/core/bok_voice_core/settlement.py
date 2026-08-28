from __future__ import annotations

from dataclasses import dataclass, field

from .types import CallSession, SettlementStatus, TurnEvent


@dataclass
class CourseMetrics:
    filler_ratio: float = 0.0
    hesitation_ratio: float = 0.0
    vague_ratio: float = 0.0
    speech_density: float = 0.0
    avg_turn_seconds: float = 0.0
    emotion_distribution: dict[str, int] = field(default_factory=dict)
    interruption_count: int = 0


@dataclass
class SettlementResult:
    call_id: str
    status: SettlementStatus = SettlementStatus.PENDING
    metrics: CourseMetrics = field(default_factory=CourseMetrics)
    transcript_doc_path: str = ""
    settlement_doc_path: str = ""
    new_topics: list[dict] = field(default_factory=list)
    global_insight_id: str = ""
    error: str = ""


FILLERS = {"嗯", "啊", "然后", "就是"}
HEDGES = {"可能", "也许", "应该", "大概"}
VAGUE = {"东西", "那个", "一些"}


class SettlementTrigger:
    """Computes expression metrics and builds a SettlementResult (pure, testable)."""

    def compute_metrics(self, turns: list[TurnEvent]) -> CourseMetrics:
        texts = [t.transcript for t in turns]
        joined = "".join(texts)

        def count_ratio(words: set[str]) -> float:
            total = sum(len(t) for t in texts) or 1
            return round(sum(1 for w in words if w in joined) / total, 4)

        return CourseMetrics(
            filler_ratio=count_ratio(FILLERS),
            hesitation_ratio=count_ratio(HEDGES),
            vague_ratio=count_ratio(VAGUE),
            speech_density=round(len(joined) / max(1, len(turns)), 2),
            emotion_distribution=_emotion_distribution(turns),
        )

    def build_result(self, call: CallSession, turns: list[TurnEvent]) -> dict:
        metrics = self.compute_metrics(turns)
        return {
            "call_id": call.id,
            "status": SettlementStatus.DONE.value,
            "metrics": metrics.__dict__,
            "new_topics": [],
            "transcript_doc_path": f"accounts/{call.account_id}/objects/{call.object_id}/calls/{call.id}/transcript.md",
            "settlement_doc_path": f"accounts/{call.account_id}/objects/{call.object_id}/calls/{call.id}/settlement.md",
            "global_insight_id": "",
            "error": "",
        }


def _emotion_distribution(turns: list[TurnEvent]) -> dict[str, int]:
    dist: dict[str, int] = {}
    for t in turns:
        if t.emotion:
            dist[t.emotion] = dist.get(t.emotion, 0) + 1
    return dist
