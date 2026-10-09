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
    # 长度保险丝（同日合流补件）：无标点长句防饿死；旧线 DoubaoSTT 默认关。
    assert stt._len_fuse is True
    from agent_runtime.providers.doubao_asr import DoubaoSTT as _Old

    assert _Old(api_key="k")._len_fuse is False  # 旧线逐字节（旗不传=零行为）


def test_first_block_cut_pure():
    """快启动层首块切点：标点 ≥5 字/保险丝 8 字/稳定性/数字 run 保护。"""
    from agent_runtime.providers.doubao_asr import _first_block_cut

    # 弱标点边界 ≥5 字（含标点计数），且上一 interim 同坐标稳定。
    assert _first_block_cut("我想请问，", "我想请问，") == 5
    # <5 字不切。
    assert _first_block_cut("我想，", "我想，") is None
    # 无标点 → 保险丝 8 字。
    assert _first_block_cut("我想请你们帮我看", "我想请你们帮我看") == 8
    # 首次目击（prev_full 空=未稳定）不切。
    assert _first_block_cut("我想请问，你们", "") is None
    # 数字 run 不被劈开（切点推到 run 结束后的边界，绝不落在 run 中间）。
    assert _first_block_cut("单号AB12345，好的", "单号AB12345，好的") == 9


def test_first_block_wiring_lite_only(monkeypatch):
    """快启动层接线：lite 旗（len_fuse）+意群档 12 下，7 字弱标点首块即提交；
    旧线旗关同语料不提前提交（首块快启动=薄线专属）。"""
    import tests.test_doubao_asr as da

    for k in ("QWEN3_ASR_COMMIT_MIN_INTERVAL_S", "QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("QWEN3_ASR_COMMIT_MIN_INTERVAL_S", "0")
    monkeypatch.setenv("QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS", "12")  # 意群档
    replies = ["我想请问，", "我想请问，你们那边", "我想请问，你们那边几点开门"]
    da._fake_merge(monkeypatch)
    da._make_connect_replies(
        monkeypatch, replies=replies, final_text="我想请问，你们那边几点开门。"
    )
    vad = da._FakeVad()
    stt_lite = da.DoubaoSTT(api_key="k", vad_=vad, clause_commit=True, len_fuse=True)
    events = asyncio.run(da._drive_clause(stt_lite, vad, packets=3))
    finals_lite = [t for n, t in events if n == "FINAL_TRANSCRIPT"]
    assert finals_lite and finals_lite[0] == "我想请问，"  # 7 字弱标点快启动

    da._fake_merge(monkeypatch)
    da._make_connect_replies(
        monkeypatch, replies=replies, final_text="我想请问，你们那边几点开门。"
    )
    vad2 = da._FakeVad()
    stt_old = da.DoubaoSTT(api_key="k", vad_=vad2, clause_commit=True)  # len_fuse 默认关
    events2 = asyncio.run(da._drive_clause(stt_old, vad2, packets=3))
    finals_old = [t for n, t in events2 if n == "FINAL_TRANSCRIPT"]
    assert not any(t == "我想请问，" for t in finals_old)  # 旧线不受快启动层影响


def test_server_utterances_config(monkeypatch):
    """lite 装配：服务端分句缺省开+end_window_size 500；总闸/灵敏度 env 可调；旧线默认关。"""
    from agent_runtime.interp_lite.providers.asr_doubao import LiteDoubaoSTT
    from agent_runtime.providers.doubao_asr import DoubaoSTT as _Old

    for k in ("BOK_INTERP_SERVER_UTT", "BOK_DOUBAO_END_WINDOW_MS"):
        monkeypatch.delenv(k, raising=False)
    lite = LiteDoubaoSTT(api_key="k")
    assert lite._server_utterances is True
    req = lite._config()["request"]
    assert req["end_window_size"] == 500
    assert _Old(api_key="k")._server_utterances is False  # 旧线零变化

    monkeypatch.setenv("BOK_INTERP_SERVER_UTT", "0")
    off = LiteDoubaoSTT(api_key="k")
    assert off._server_utterances is False
    assert "end_window_size" not in off._config()["request"]  # 闸关不发该键

    monkeypatch.setenv("BOK_INTERP_SERVER_UTT", "1")
    monkeypatch.setenv("BOK_DOUBAO_END_WINDOW_MS", "320")
    assert LiteDoubaoSTT(api_key="k")._config()["request"]["end_window_size"] == 320


def test_server_definite_commits_stream():
    """流层 definite 消费：见新 definite 即 FINAL（去重、不重发）；display 剥已发前缀。"""
    import asyncio

    from agent_runtime.providers import doubao_asr as da
    from agent_runtime.providers.doubao_asr import DoubaoSTT, _DoubaoLiveStream

    async def scenario():
        stt = DoubaoSTT(api_key="k", clause_commit=True, server_utterances=True)
        stream = _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())
        events: list[tuple[str, str]] = []

        class _FakeCh:
            @staticmethod
            def close():
                pass

            def send_nowait(self, ev):
                t = ev.alternatives[0].text if ev.alternatives else ""
                events.append((str(ev.type).split(".")[-1], t))

        stream._event_ch = _FakeCh()
        # 帧1：一个 definite 分句 + 全文累积到「你好，今天」
        stream._on_payload({
            "result": {
                "text": "你好，今天天气怎么样",
                "utterances": [{"text": "你好，", "definite": True},
                               {"text": "今天天气怎么样", "definite": False}],
            }
        })
        # 帧2：第二个分句转 definite（首个重发——计数去重，只发新）
        stream._on_payload({
            "result": {
                "text": "你好，今天天气怎么样？",
                "utterances": [{"text": "你好，", "definite": True},
                               {"text": "今天天气怎么样？", "definite": True}],
            }
        })
        await stream.aclose()
        return events

    events = asyncio.run(scenario())
    finals = [t for n, t in events if n == "FINAL_TRANSCRIPT"]
    assert finals == ["你好，", "今天天气怎么样？"]  # 按序、无重复
    interims = [t for n, t in events if n == "INTERIM_TRANSCRIPT"]
    assert all("你好" not in i or i.endswith("怎么样") or i == "你好，今天天气怎么样" for i in interims)


def test_starve_threshold_derived_from_end_window(monkeypatch):
    """饥饿接管阈值=end_window 派生（call-6f92bfd4）：等过窗仍无 definite 即接管。

    env 同键 ``BOK_DOUBAO_END_WINDOW_MS``；钳 [0.4,2.0]s——300ms 窗也至少
    0.4s（防 definite/本地闸贴脸双发），2000ms 封顶（防调窗把闸饿死）。"""
    import asyncio

    from agent_runtime.providers import doubao_asr as da
    from agent_runtime.providers.doubao_asr import DoubaoSTT, _DoubaoLiveStream

    async def _starve_s(ew_ms: str) -> float:
        monkeypatch.setenv("BOK_DOUBAO_END_WINDOW_MS", ew_ms)
        stt = DoubaoSTT(api_key="k", clause_commit=True, server_utterances=True)
        return _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())._cc_starve_s

    assert asyncio.run(_starve_s("500")) == 0.5
    assert asyncio.run(_starve_s("300")) == 0.4  # 下钳
    assert asyncio.run(_starve_s("2000")) == 2.0  # 上钳

    async def _bad():
        monkeypatch.setenv("BOK_DOUBAO_END_WINDOW_MS", "notanumber")
        stt = DoubaoSTT(api_key="k", clause_commit=True, server_utterances=True)
        return _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())._cc_starve_s

    assert asyncio.run(_bad()) == 0.5  # 坏值回缺省


def test_server_hybrid_starve_and_merge(monkeypatch):
    """混合档：连续语流饥饿→本地闸接管；definite 后到→前缀合并只发增量不重发。"""
    import asyncio

    from agent_runtime.providers import doubao_asr as da
    from agent_runtime.providers.doubao_asr import DoubaoSTT, _DoubaoLiveStream

    async def scenario():
        stt = DoubaoSTT(api_key="k", clause_commit=True, server_utterances=True, len_fuse=True)
        stream = _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())
        events: list[tuple[str, str]] = []

        class _FakeCh:
            @staticmethod
            def close():
                pass

            def send_nowait(self, ev):
                t = ev.alternatives[0].text if ev.alternatives else ""
                events.append((str(ev.type).split(".")[-1], t))

        stream._event_ch = _FakeCh()
        monkeypatch.setenv("QWEN3_ASR_COMMIT_MIN_INTERVAL_S", "0")
        monkeypatch.setenv("QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS", "8")
        # 连续语流：无 definite，本地闸意群档接管（饥饿 ≥1.2s + 标点稳定）。
        stream._maybe_interim("我今天想去")
        stream._cc_starve_t0 -= 2.0  # 模拟饥饿钟已跑 2s
        # 跨 interim 稳定（闸的判据：候选段在上一 interim 同坐标一致）——三连喂
        stream._maybe_interim("我今天想去酒店那边，你")
        stream._maybe_interim("我今天想去酒店那边，你知")  # 稳定后 → 本地闸切
        await stream.aclose()
        return events, stream

    events, stream = asyncio.run(scenario())
    finals = [t for n, t in events if n == "FINAL_TRANSCRIPT"]
    assert any("酒店那边" in t for t in finals)  # 饥饿接管出了本地闸 FINAL

    # definite 后到（覆盖已提交前缀）→ 前缀合并只发增量。
    async def scenario2():
        committed = stream._cc_committed_text
        stream2 = _DoubaoLiveStream(
            DoubaoSTT(api_key="k", clause_commit=True, server_utterances=True), conn_options=da.APIConnectOptions()
        )
        ev2: list[str] = []

        class _Ch2:
            @staticmethod
            def close():
                pass

            def send_nowait(_, ev):
                ev2.append(ev.alternatives[0].text if ev.alternatives else "")

        stream2._event_ch = _Ch2()
        # 本地闸已交「我今天想去酒店check，」；definite 全文到达
        stream2._cc_committed_text = committed
        stream2._cc_committed_len = len(committed)
        stream2._su_concat = ""  # 本地闸提交不进 su_concat（definite 坐标系独立起算）
        stream2._on_payload({
            "result": {
                "text": committed + "不然不行",
                "utterances": [{"text": committed + "不然不行", "definite": True}],
            }
        })
        await stream2.aclose()
        return ev2

    ev2 = asyncio.run(scenario2())
    assert [t for t in ev2 if t.strip()] == ["不然不行"]  # 只发增量，不重发前缀（空尾巴滤）


def test_len_fuse_cut_pure():
    """长度保险丝切点纯函数：内容字计数/ASCII run 防劈/门槛不足 None。"""
    from agent_runtime.providers.doubao_asr import _len_fuse_cut

    # 攒够 6 个内容字（标点/空白不计）→ 切点在其后。
    assert _len_fuse_cut("你好世界今天天气很好", 0, 6) == 6
    assert _len_fuse_cut("你好，世界。今天天气", 0, 6) == 8  # 标点占位不计内容字（第6内容字=今@7→切8）
    # ASCII run 防劈：门槛落在 run 内 → 后移到 run 尾（单号 7890123 不劈）。
    s = "订单号是七八九零一二三四五六"  # 12 内容字无 ASCII run
    assert _len_fuse_cut(s, 0, 6) == 6  # 无 run：门槛即切
    s2 = "单号 ABC12345 后面还有内容"
    cut = _len_fuse_cut(s2, 0, 4)  # 门槛落在 ABC12 内 → 推到 run 尾
    assert s2[:cut].endswith("ABC12345")
    # 门槛不足 → None。
    assert _len_fuse_cut("太短", 0, 6) is None
    # start 偏移（已提交前缀之后）。
    assert _len_fuse_cut("前缀你好世界今天天气", 2, 6) == 8


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

    def say(self, x, **_kw):  # **_kw 吞 allow_interruptions 等真 say 旗（v2 静音替换解除）
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
    """脚本化 MT：calls[i] = 异步迭代器工厂或异常；msgs 记录逐次消息序列。"""

    def __init__(self, calls):
        self._calls = list(calls)
        self.msgs: list[list[dict]] = []
        self.last_metrics = {"prompt_cache_hit_tokens": 1, "prompt_tokens": 2, "cached_pct": 0.5}

    async def stream(self, msgs):
        self.msgs.append(msgs)
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


def _fast_quiet(s: float = 0.05):
    """话段静默钟临时压短（测试免等 1.2s）。"""
    import contextlib

    import agent_runtime.interp_lite.pipeline as pl

    @contextlib.contextmanager
    def _cm():
        old = pl._UTT_QUIET_S
        pl._UTT_QUIET_S = s
        try:
            yield
        finally:
            pl._UTT_QUIET_S = old

    return _cm()


def test_pipeline_stream_one_say_across_chunks():
    asyncio.run(_pipeline_stream_happy())


async def _pipeline_stream_happy():
    """v2 连续流：两块一条 say（块间零天窗的结构保证）；上下文对衔接；末块全出。"""
    mt = FakeMT([iter(["你好，", "世界。"]), iter(["我们去", "玩吧？"])])
    p = _pipeline(mt)
    with _fast_quiet():
        p.feed_interim("你好世界")                    # 3 字（hold 1）→ 未到量
        p.feed_interim("你好世界我们")                 # 首块 ≥4 字即出（抢首声，唯一非标点切点）
        p.feed_interim("你好世界我们去玩吧？")          # 余段（标点被 hold 回扣）→ 不切
        p.feed_final("你好世界我们去玩吧？")            # 提交：视图=全文（display 重清）
        await asyncio.sleep(0.25)                     # 静默钟触发收尾末块
    session = p.session
    assert len(session.said) == 1  # 一条话段=一个 say(generator)——v1 的逐子句多流废除
    assert "".join(session.spoken[0]) == "你好，世界。我们去玩吧？"
    # 第二块消息带「本话段前文」上下文对（源=首块、译=已产）——块间衔接单点
    assert len(mt.msgs) == 2
    assert mt.msgs[1][-3] == {"role": "user", "content": "你好世界我"}
    assert mt.msgs[1][-2] == {"role": "assistant", "content": "你好，世界。"}
    assert mt.msgs[1][-1] == {"role": "user", "content": "们去玩吧？"}
    assert p.lag.notes and p.lag.dones and p.pairs[-1][1] == "你好，世界。我们去玩吧？"


def test_pipeline_gate_falls_back_to_whole_utterance():
    asyncio.run(_pipeline_gate_fallback())


async def _pipeline_gate_fallback():
    # 首块英文（目标 zh）→ 语言门违约零播报 → 收尾整段回退（第二次调用中文）。
    # 尾标点单独成块会被丢（纯标点无内容），故脚本只剩 chunk1 + 回退两次调用。
    p = _pipeline(FakeMT([iter(["Hello ", "world"]), iter(["你好，世界。"])]), target="zh")
    with _fast_quiet():
        p.feed_interim("hello world，")
        await asyncio.sleep(0.2)
    assert p.session.spoken[0] == []  # gate 版生成器零产出
    assert p.session.said[1] == "你好，世界。"  # 回退整段出声
    assert len(p.lag.dones) == 1  # gate 零播报不记账；回退整段记一次（RC-8：每话段恰一次）


def test_pipeline_empty_translation_drops():
    asyncio.run(_pipeline_empty_drops())


async def _pipeline_empty_drops():
    p = _pipeline(FakeMT([iter([])]))
    with _fast_quiet():
        p.feed_interim("你好呀")
        await asyncio.sleep(0.2)
    assert p.lag.drops == [1]


def test_pipeline_fatal_marks_lane_dead_and_falls_back():
    asyncio.run(_pipeline_fatal())


async def _pipeline_fatal():
    from agent_runtime.interp_lite.providers.mt_deepseek import MTHTTPError

    mt = FakeMT([MTHTTPError(402, "insufficient balance")])
    p = _pipeline(mt, target="zh", voice_tags=False)
    with _fast_quiet():
        p.feed_interim("你好世界")
        await asyncio.sleep(0.25)
    assert p.lane_dead["reason"]
    strs = [s for s in p.session.said if isinstance(s, str)]
    assert any("听不清" in s or "再說一遍" in s or "再说一遍" in s for s in strs)
    assert p.lag.dones  # 兜底句配对记账（note_src 在收尾时挂上）


# ---- 切块器纯函数（v2 首声/衔接的结构单点） ----


def test_chunker_first_chunk_fires_early():
    from agent_runtime.interp_lite.chunker import pick_cut

    assert pick_cut("你好世界", first=True) == 4  # 首块 ≥4 字即出（抢首声，唯一的非标点切点）
    assert pick_cut("你好", first=True) == 0  # 不足 4 字继续攒
    # 逗号在门槛下（内容 2<4）不单独成切点 → 整段出（首块仍最先出）
    assert pick_cut("你好，世界", first=True) == 5


def test_chunker_regular_cuts_by_punctuation_only():
    from agent_runtime.interp_lite.chunker import pick_cut

    assert pick_cut("你好世界", first=False) == 0  # 无标点：不切（严格标点界，防乱切）
    assert pick_cut("你好世界，后面还有", first=False) == 5  # 逗号即切（含标点）
    assert pick_cut("你好世界，后面还有内容哦。", first=False) == 13  # 句末标点一行出
    # 保险丝只对病态无标点长跑生效（正常语速碰不到）
    assert pick_cut("你好世界" * 6, first=False) == 24


def test_chunker_run_safe_and_flush():
    from agent_runtime.interp_lite.chunker import pick_cut

    # 保险丝切点落在 ASCII run 内→回退到 run 起点（整 run 留给下一块）
    s = "你好" * 10 + "abcdef"
    assert pick_cut(s, first=False) == 20
    assert pick_cut("还剩一点尾巴", flush=True) == 6  # flush=全出
    assert pick_cut("", first=True) == 0


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
