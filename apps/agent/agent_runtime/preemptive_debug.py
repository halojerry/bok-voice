"""抢跑（preemptive generation）失效诊断探针。

背景：livekit-agents 1.8.0 抢跑机制在本仓 A 线 843 次失效 / 0 次命中（agent.log
2026-09-10 实测），即投机 LLM 请求全部白烧。框架在 ``_on_turn_completed`` 里按
四条件决定快照去留：转写等价、chat_ctx 等价、tools 相等、tool_choice 相等，
失效时只给一句笼统 WARNING，看不出死在哪条。

``BOK_PREEMPTIVE_DEBUG=1`` 时把前两条比较包一层：不等价即打印两边差异
（转写全文 / 逐 item 的 id·role·内容头），tools/tool_choice 由排除法读日志。
纯观测不改行为；0 开销（不设 env 时 install 是 no-op）。
"""

from __future__ import annotations

import os
import time

_installed = False


def _item_repr(item) -> str:  # noqa: ANN001 - livekit ChatItem 联合类型
    role = getattr(item, "role", None)
    head = ""
    content = getattr(item, "content", None)
    if isinstance(content, list) and content:
        first = content[0]
        head = first if isinstance(first, str) else type(first).__name__
    elif isinstance(content, str):
        head = content
    name = getattr(item, "name", None) or ""
    return f"{getattr(item, 'type', '?')}#{str(getattr(item, 'id', '?'))[-6:]}:{role or name}:{str(head)[:48]!r}"


def _ctx_repr(ctx) -> str:  # noqa: ANN001 - livekit ChatContext
    items = list(getattr(ctx, "items", []))
    return f"n={len(items)} [{'; '.join(_item_repr(i) for i in items[-6:])}]"


def install_preemptive_debug() -> None:
    """包住框架比较函数；仅 BOK_PREEMPTIVE_DEBUG=1 时生效，幂等（防重复包裹）。"""
    global _installed
    if _installed or os.environ.get("BOK_PREEMPTIVE_DEBUG", "") != "1":
        return
    _installed = True
    try:
        from livekit.agents import llm as llm_module
        from livekit.agents.llm import chat_context
        from livekit.agents.voice import agent_activity
    except Exception as exc:  # pragma: no cover - 无 livekit 环境（单测）直接跳过
        print(f"BOK_PREEMPTIVE_DEBUG install skipped: {exc!r}", flush=True)
        return

    orig_equiv = agent_activity._transcripts_equivalent

    def _transcripts_equivalent(first: str, second: str | None) -> bool:
        ok = orig_equiv(first, second)
        if ok:
            print(
                f"BOK_PREEMPTIVE_DEBUG transcript_match spec={first!r} final={second!r}",
                flush=True,
            )
        else:
            print(
                f"BOK_PREEMPTIVE_DEBUG transcript_mismatch "
                f"spec={first!r} final={second!r}",
                flush=True,
            )
        return ok

    agent_activity._transcripts_equivalent = _transcripts_equivalent

    orig_is_equivalent = chat_context.ChatContext.is_equivalent

    def _is_equivalent(self, other) -> bool:  # noqa: ANN001
        ok = orig_is_equivalent(self, other)
        if not ok:
            a, b = list(getattr(self, "items", [])), list(getattr(other, "items", []))
            diff_at = next(
                (i for i, (x, y) in enumerate(zip(a, b)) if str(x) != str(y)),
                min(len(a), len(b)),
            )
            print(
                f"BOK_PREEMPTIVE_DEBUG ctx_mismatch len={len(a)}v{len(b)} "
                f"diff_at={diff_at} snapshot[{diff_at if diff_at < len(a) else -1}]="
                f"{_item_repr(a[diff_at]) if diff_at < len(a) else '<none>'} "
                f"current[{diff_at if diff_at < len(b) else -1}]="
                f"{_item_repr(b[diff_at]) if diff_at < len(b) else '<none>'} "
                f"snapshot_tail={_ctx_repr(self)} current_tail={_ctx_repr(other)}",
                flush=True,
            )
        return ok

    chat_context.ChatContext.is_equivalent = _is_equivalent

    # speculative 触发观测：原方法是同步 def（audio_recognition 同步调用），
    # 包装必须同为同步 def——async 包装会得到未 await 协程，整个抢跑被关掉。
    orig_spec = agent_activity.AgentActivity.on_preemptive_generation

    def _on_preemptive_generation(self, info) -> None:  # noqa: ANN001
        opts = self.preemptive_generation_opts
        gates = []
        if not opts["enabled"]:
            gates.append("disabled")
        if self._scheduling_paused:
            gates.append("scheduling_paused")
        if self._new_turns_blocked:
            gates.append("new_turns_blocked")
        cur = self._current_speech
        if cur is not None and not cur.interrupted:
            gates.append(f"busy_speech({cur.id[-6:]})")
        if not isinstance(self.llm, llm_module.LLM):
            gates.append("llm_not_pipeline")
        if (
            info.started_speaking_at is not None
            and time.time() - info.started_speaking_at > opts["max_speech_duration"]
        ):
            gates.append(f"max_speech_duration({opts['max_speech_duration']}s)")
        if self._preemptive_generation_count >= opts["max_retries"]:
            gates.append(f"max_retries({self._preemptive_generation_count})")
        print(
            f"BOK_PREEMPTIVE_DEBUG speculate transcript={info.new_transcript!r} "
            f"count={self._preemptive_generation_count} "
            f"blocked={','.join(gates) if gates else 'none'}",
            flush=True,
        )
        orig_spec(self, info)

    agent_activity.AgentActivity.on_preemptive_generation = _on_preemptive_generation

    # 比较结局观测：快照存在时，四条件逐条打点（在原比较之外重算一遍只读条件）。
    print("BOK_PREEMPTIVE_DEBUG installed", flush=True)
