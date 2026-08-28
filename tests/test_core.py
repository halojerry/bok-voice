import asyncio

from bok_voice_core.context import build_context
from bok_voice_core.policies import ProviderRegistry, ProviderState, select_session_manifest
from bok_voice_core.settlement import SettlementTrigger
from bok_voice_core.types import CallMode, CallSession, TurnEvent

from agent_runtime.providers.fakes import FakeASR, FakeLLM, FakeTTS, FakeVAD


def test_context_assembler_does_not_inject_raw_history():
    bundle = build_context(
        product_snippets=[{"text": "MOQ 100"}],
        history_snippets=[{"text": "客户对价格敏感"}],
        current_turns=[{"role": "user", "text": "能便宜吗"}],
    )
    assert bundle.product_snippets
    assert bundle.history_snippets
    assert bundle.current_turns
    assert bundle.token_estimate > 0


def test_session_manifest_default_providers():
    m = select_session_manifest(
        session_id="s1",
        account_id="a1",
        object_id="o1",
        persona_id="p1",
        mode=CallMode.SIMULATION,
    )
    assert m.providers["asr"] == "sherpa"
    assert m.policy == "offline_first"


def test_provider_registry_failover():
    reg = ProviderRegistry()
    reg.register("asr", "sherpa", FakeASR())
    reg.register("asr", "volcano", FakeASR())
    reg.mark("asr", "sherpa", ProviderState.QUARANTINED, "down")
    active = reg.active("asr")
    assert active is not None
    assert reg.active("tts") is None


def test_fake_provider_contracts():
    assert FakeVAD().detect_segments(b"x")[0]["is_speech"]
    assert asyncio.run(FakeASR().transcribe(b"x")) is not None
    assert asyncio.run(FakeTTS().synthesize("hi"))[0].is_final
    async def collect():
        return [e async for e in FakeLLM().stream_chat(True)]  # type: ignore[arg-type]

    events = asyncio.run(collect())
    assert events and events[-1].done


def test_settlement_metrics():
    trigger = SettlementTrigger()
    turns = [
        TurnEvent(trace_id="c", call_id="c", turn_id="t1", role="user", transcript="嗯 然后 这个价格", emotion="neutral"),
        TurnEvent(trace_id="c", call_id="c", turn_id="t2", role="assistant", transcript="好的", emotion="friendly"),
    ]
    call = CallSession(id="c", account_id="a", object_id="o", persona_id="p", mode=CallMode.SIMULATION)
    result = trigger.build_result(call, turns)
    assert result["status"] == "done"
    assert result["metrics"]["filler_ratio"] >= 0
    assert result["transcript_doc_path"].startswith("accounts/a/")
