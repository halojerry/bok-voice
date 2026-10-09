"""interp_lite 单元面：TagGate/官方白名单 parity/DeepSeek fake 传输/pipeline 账本/模块导入烟测。"""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest
from agent_runtime.interp_lite.config import build_instructions
from agent_runtime.interp_lite.providers.mt_deepseek import (
    DeepSeekMT,
    MTHTTPError,
    build_messages,
    endpoint_ok,
)
from agent_runtime.interp_lite.voice_tags import OFFICIAL_VOICE_TAGS, TagGate
from agent_runtime.voice_style import VOICE_TAG_WHITELIST

# ---- 官方 19 标签 parity（文档基线：/Users/halo/Documents/bok/minimax-api_副本.md） ----

_DOC_TAGS = {
    "laughs", "chuckle", "coughs", "clear-throat", "groans", "breath", "pant",
    "inhale", "exhale", "gasps", "sniffs", "sighs", "snorts", "burps",
    "lip-smacking", "humming", "hissing", "emm", "sneezes",
}


def test_official_tags_match_doc_literal():
    assert OFFICIAL_VOICE_TAGS == _DOC_TAGS
    assert len(OFFICIAL_VOICE_TAGS) == 19


def test_a_line_whitelist_subset_of_official():
    """A 线 9 枚白名单必须 ⊆ 官方 19（A/B 语气词汇不双轨立法）。"""
    assert VOICE_TAG_WHITELIST <= OFFICIAL_VOICE_TAGS


# ---- TagGate ----


def test_tag_gate_canonicalizes_fullwidth_and_case():
    g = TagGate()
    assert g.feed("（ＬＡＵＧＨＳ）好") == "(laughs)好"
    assert g.canonicalized == 1


def test_tag_gate_holds_across_deltas():
    g = TagGate()
    assert g.feed("(lau") == ""
    assert g.feed("ghs) 好") == "(laughs) 好"


def test_tag_gate_unknown_bracket_is_content():
    g = TagGate()
    assert g.feed("(USA) 公司") == "(USA) 公司"
    assert g.canonicalized == 0


def test_tag_gate_hang_release_on_overlong_inner():
    g = TagGate()
    out = g.feed("(" + "x" * 30)
    assert out.startswith("(") and "x" * 30 in out
    assert g.hang_released == 1


def test_tag_gate_flush_releases_pending():
    g = TagGate()
    assert g.feed("(ha") == ""
    assert g.flush() == "(ha"


# ---- instructions ----


def test_build_instructions_enumerates_all_official_tags():
    text = build_instructions("zh", "en")
    for t in OFFICIAL_VOICE_TAGS:
        assert f"({t})" in text
    assert "Glossary (keep these renderings exactly): A=B" in build_instructions("zh", "en", "A=B")


def test_build_instructions_cantonese_rule():
    assert "港式粵語" in build_instructions("zh", "cantonese")
    assert "港式粵語" not in build_instructions("zh", "en")


# ---- DeepSeek 客户端（fake 传输） ----


def test_endpoint_ok_guard():
    assert endpoint_ok("https://api.deepseek.com")
    assert not endpoint_ok("http://api.deepseek.com")  # 非 https
    assert not endpoint_ok("https://localhost")
    assert not endpoint_ok("https://127.0.0.1")
    assert not endpoint_ok("https://10.0.0.1")  # 私网


def test_build_messages_shape():
    msgs = build_messages("SYS", [("a", "甲"), ("b", "乙")], "c")
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert msgs[-1]["content"] == "c"


def _mk_client(chunks, status=200, captured=None):
    import httpx

    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["json"] = json.loads(request.content.decode("utf-8"))
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(status, text=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_deepseek_stream_deltas_and_usage():
    asyncio.run(_deepseek_stream_deltas_and_usage())


async def _deepseek_stream_deltas_and_usage():
    captured: dict = {}
    chunks = [
        {"choices": [{"delta": {"content": "你"}}]},
        {"choices": [{"delta": {"content": "好"}}]},
        {"choices": [{"delta": {}}], "usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80}},
    ]
    mt = DeepSeekMT(api_key="sk-test", client=_mk_client(chunks, captured=captured))
    parts = [d async for d in mt.stream(build_messages("S", [], "hi"))]
    assert parts == ["你", "好"]
    assert mt.last_metrics == {"prompt_cache_hit_tokens": 80, "prompt_tokens": 100, "cached_pct": 0.8}
    # docs-first 契约钉死：thinking 显式 disabled；include_usage；流式。
    assert captured["json"]["thinking"] == {"type": "disabled"}
    assert captured["json"]["stream"] is True
    assert captured["json"]["stream_options"] == {"include_usage": True}
    assert captured["auth"] == "Bearer sk-test"


def test_deepseek_stream_http_error_raises():
    asyncio.run(_deepseek_stream_http_error())


async def _deepseek_stream_http_error():
    mt = DeepSeekMT(client=_mk_client([], status=402))
    with pytest.raises(MTHTTPError) as ei:
        async for _ in mt.stream(build_messages("S", [], "hi")):
            pass
    assert ei.value.status == 402


# ---- LiteDoubaoSTT 官方参数档 ----


def test_lite_doubao_config_official_arms():
    from agent_runtime.interp_lite.providers.asr_doubao import LiteDoubaoSTT

    stt = LiteDoubaoSTT(api_key="k")
    req = stt._config()["request"]
    assert req["enable_nonstream"] is True  # 官方推荐：二遍识别
    assert req["force_to_speech_time"] == 1000  # 官方推荐值
    assert "enable_ddc" not in req  # 与语气标记冲突，不开
    # 说话中出译已开（2026-10-09 用户拍板「必须边讲边出声」）：与旧线 B 档同旗。
    assert stt._clause_commit is True and stt._utt_merge is True


def test_lite_doubao_accelerate_arm_uses_existing_key(monkeypatch):
    from agent_runtime.interp_lite.providers.asr_doubao import LiteDoubaoSTT

    monkeypatch.setenv("BOK_DOUBAO_FIRST_TOKEN_BOOST", "1")
    req = LiteDoubaoSTT(api_key="k")._config()["request"]
    assert req["enable_accelerate_text"] is True and req["accelerate_score"] == 3


# ---- pipeline（fake session/mt/lag） ----


class FakeSession:
    """真 session.say 会消费生成器——fake 同款拉起后台排干，并把产出收进 spoken[i]。"""

    def __init__(self):
        self.said: list[str] = []
        self.spoken: list[list[str]] = []
        self._tasks: set = set()

    def say(self, x):
        self.said.append(x)
        if hasattr(x, "__aiter__"):
            bucket: list[str] = []
            self.spoken.append(bucket)

            async def _drain():
                try:
                    async for piece in x:
                        bucket.append(piece)
                except Exception:  # noqa: BLE001 - 排干尽力而为
                    pass

            try:
                t = asyncio.get_running_loop().create_task(_drain())
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)
            except RuntimeError:
                pass
        else:
            self.spoken.append([str(x)])


class FakeLag:
    def __init__(self):
        self.notes, self.dones, self.drops = [], [], []

    def note_src(self, t):
        self.notes.append(t)

    def done_mt(self, ms):
        self.dones.append(ms)

    def drop_src(self):
        self.drops.append(1)


class FakeMT:
    """脚本化 MT：calls[i] = 异步迭代器工厂或异常。"""

    def __init__(self, calls):
        self._calls = list(calls)
        self.last_metrics = {"prompt_cache_hit_tokens": 1, "prompt_tokens": 2, "cached_pct": 0.5}

    async def stream(self, msgs):
        item = self._calls.pop(0)
        if isinstance(item, Exception):
            raise item
        for d in item:
            yield d


def _pipeline(mt, target="zh", voice_tags=True):
    from agent_runtime.interp_lite.pipeline import InterpPipeline

    return InterpPipeline(
        FakeSession(), mt, "SYS", target_lang=target, voice_tags=voice_tags,
        lag=FakeLag(), first_ms={"ms": 0},
    )


async def _consume(gen):
    return [x async for x in gen]


def test_pipeline_stream_happy_path():
    asyncio.run(_pipeline_stream_happy())


async def _pipeline_stream_happy():
    p = _pipeline(FakeMT([iter(["你好，", "世界。"])]))
    await p._translate_say("hello world", p and 0.0)
    session = p.session
    # 逐 delta say：两片各自成为 generator 消费单元；gen 排干后 say 收到 async 生成器。
    assert len(session.said) == 1  # 一个 say(生成器)
    await asyncio.sleep(0.05)  # 等后台排干
    assert "".join(session.spoken[0]) == "你好，世界。"
    assert p.lag.dones and p.pairs[-1][1] == "你好，世界。"


def test_pipeline_gate_falls_back_to_whole_sentence():
    asyncio.run(_pipeline_gate_fallback())


async def _pipeline_gate_fallback():
    # 首段英文（目标 zh）→ 语言门违约零播报 → 整句回退（第二次调用返回中文）。
    p = _pipeline(FakeMT([iter(["Hello ", "world"]), iter(["你好，世界。"])]), target="zh")
    await p._translate_say("hello", 0.0)
    await asyncio.sleep(0.05)
    assert "".join(p.session.spoken[0]) == ""  # gate 版生成器零产出
    assert p.session.said[1] == "你好，世界。"  # 回退整句出声
    assert len(p.lag.dones) == 1  # gate 零播报不记账；回退整句记一次（RC-8：每句恰一次）


def test_pipeline_empty_translation_drops():
    asyncio.run(_pipeline_empty_drops())


async def _pipeline_empty_drops():
    p = _pipeline(FakeMT([iter([])]))
    await p._translate_say("x", 0.0)
    await asyncio.sleep(0.05)
    assert p.lag.drops == [1]


def test_pipeline_fatal_marks_lane_dead_and_falls_back():
    asyncio.run(_pipeline_fatal())


async def _pipeline_fatal():
    from agent_runtime.interp_lite.providers.mt_deepseek import MTHTTPError

    mt = FakeMT([
        MTHTTPError(402, "insufficient balance"),
        MTHTTPError(402, "insufficient balance"),
        MTHTTPError(402, "insufficient balance"),
    ])
    p = _pipeline(mt, target="zh", voice_tags=False)
    p.enqueue("句一")
    task = asyncio.create_task(p.run())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert p.lane_dead["reason"]
    strs = [s for s in p.session.said if isinstance(s, str)]
    assert any("听不清" in s or "再說一遍" in s or "再说一遍" in s for s in strs)


# ---- 模块导入烟测（相对导入面/循环依赖） ----


@pytest.mark.parametrize(
    "mod",
    [
        "agent_runtime.interp_lite",
        "agent_runtime.interp_lite.config",
        "agent_runtime.interp_lite.pipeline",
        "agent_runtime.interp_lite.voice_tags",
        "agent_runtime.interp_lite.worker",
        "agent_runtime.interp_lite.providers.asr_doubao",
        "agent_runtime.interp_lite.providers.mt_deepseek",
        "agent_runtime.interp_lite.providers.tts_minimax",
    ],
)
def test_module_imports(mod):
    assert importlib.import_module(mod) is not None
