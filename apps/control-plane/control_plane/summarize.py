from __future__ import annotations

import json
import re
from typing import Any

import httpx


_SYSTEM = (
    "你是电话客服质检助手。给定一场对话的逐轮文本，提炼：\n"
    "1) 一段 3-5 句的对话总结；\n"
    "2) 你认为是值得沉淀的关键点/新话题（每个一句，含对象关注点、异议、需求）；\n"
    "3) 一条全局洞察（statement：反映该对象群共性的观察；confidence：0~1）。\n"
    "只输出 JSON，格式："
    '{"summary":"...","new_topics":[{"topic":"...","summary":"..."}],'
    '"insight":{"statement":"...","confidence":0.8,"language":"zh"}}'
)


class Summarizer:
    """Runs conversation → summary/topics/insight through the configured LLM.

    Best-effort: any LLM failure falls back to a deterministic metrics-only
    summary so settlement never blocks on the model.
    """

    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout

    def build(self, turns: list[Any], call: dict, settings: dict) -> dict:
        """Return {summary, new_topics, insight} derived from ``turns``."""
        transcript = self._render_transcript(turns)
        if not transcript:
            return {"summary": "", "new_topics": [], "insight": None}
        llm_cfg = settings.get("llm", {}) or {}
        base_url = (llm_cfg.get("base_url") or "").rstrip("/")
        model = llm_cfg.get("model") or ""
        if not base_url or not model:
            return self._fallback(turns)
        try:
            return self._via_llm(base_url, model, transcript, call)
        except Exception as exc:  # pragma: no cover - model/network failure
            print(f"[summarize] LLM summary failed, falling back: {exc!r}", flush=True)
            return self._fallback(turns)

    def _via_llm(self, base_url: str, model: str, transcript: str, call: dict) -> dict:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": f"通话对象：{call.get('object_id','')}\n对话：\n{transcript}",
                },
            ],
            "max_tokens": 512,
            "temperature": 0.2,
            "stream": False,
        }
        r = httpx.post(f"{base_url}/chat/completions", json=payload, timeout=self.timeout)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"].get("content", "")
        return self._parse(content)

    def _parse(self, content: str) -> dict:
        text = content.strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return self._fallback([])
        try:
            data = json.loads(m.group(0))
        except Exception:
            return self._fallback([])
        return {
            "summary": str(data.get("summary", "")),
            "new_topics": list(data.get("new_topics", [])),
            "insight": data.get("insight") or None,
        }

    @staticmethod
    def _render_transcript(turns: list[Any], max_chars: int = 6000) -> str:
        lines: list[str] = []
        total = 0
        for t in turns:
            role = getattr(t, "role", None) or getattr(t, "role", "?")
            text = getattr(t, "transcript", "") or getattr(t, "text", "")
            line = f"{role}: {text}"
            lines.append(line)
            total += len(line)
            if total >= max_chars:
                break
        return "\n".join(lines)

    @staticmethod
    def _fallback(turns: list[Any]) -> dict:
        texts = [getattr(t, "transcript", "") for t in turns if getattr(t, "transcript", "")]
        if not texts:
            return {"summary": "", "new_topics": [], "insight": None}
        # deterministic fallback: first/last key quote + user-turn count
        user = [t for t in texts]
        heads = [x[:60] for x in user[:2]]
        return {
            "summary": f"本场共 {len(user)} 轮。主要内容：{'；'.join(heads)}",
            "new_topics": [],
            "insight": None,
        }
