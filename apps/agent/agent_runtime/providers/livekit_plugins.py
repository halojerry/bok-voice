"""LiveKit-compatible provider plugins: OpenAI-compatible LLMs + offline fakes."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass

import httpx
from livekit.agents import APIConnectOptions, llm, stt, tts, vad

# 粤语特征字/词：Qwen3-ASR 对粤语偶发判成 Chinese（语言标签不稳），
# 若文本命中这些地道粤语用字则按粤语处理，避免 LLM 被误判成普通话后回普。
# 只用「普通话里基本不出现」的粤语专用字/词；普粤共用字（下、咁、系等）不作为特征。
_CANTONESE_CHARS = set(
    "冇嘅哋佢喺嚟啲嗰喎㗎冚瞓攞揾搵嘥乜嘢咩啫哂啩嗌"
    "唔係咗睇俾畀掂啱唞冧聽"
)
_CANTONESE_WORDS = (
    "唔該", "唔系", "唔係", "唔好", "唔使", "唔知", "唔想", "唔會", "唔同",
    "唔緊要", "傾偈", "而家", "依家", "啱啱", "睇下", "睇睇",
    "嗰陣時", "邊度", "幾時", "點解", "唔該晒",
    "係咪", "係呀", "係嘅", "好嘅", "係唔係", "唔係呀", "冇問題", "冇所謂",
    "搞掂", "聽日", "聽講", "喺邊", "點樣", "幾多錢", "幾好", "咁樣",
)
# 普通话句子的功能字（对应粤语：嘅/咗/呢/嗰/乜/冇/嗎…）：普通话转写里高频、
# 而地道粤语口语转写几乎不用。整句长度 + 功能字双重命中才判「强普通话」。
_MANDARIN_MARKERS = ("的", "了", "这", "那", "什", "么", "说", "没", "给", "们", "您")

# 中文对话里常被整词借用的英语感叹词：出现它们不代表用户切到英语。
_EN_ACKS = {"ok", "okay", "yes", "yeah", "yep", "no", "nope", "hi", "hey", "hello", "bye", "thx"}


def _looks_cantonese(text: str) -> bool:
    t = text or ""
    if any(ch in _CANTONESE_CHARS for ch in t):
        return True
    return any(w in t for w in _CANTONESE_WORDS)


def _looks_mandarin(text: str) -> bool:
    """普通话书面/口语特征：中文字符里功能字命中 2+ 个，或整句够长（≥8 字）无粤语特征。

    只统计汉字；纯拉丁/越南文等乱码不算普通话（避免 ASR 把非中英粤乱码
    当"强普通话"证据,把整场粤语拉走)。
    """
    t = text or ""
    han = [ch for ch in t if "\u4e00" <= ch <= "\u9fff"]
    if not han:
        return False
    hits = sum(1 for ch in han if ch in _MANDARIN_MARKERS)
    return len(han) >= 8 or hits >= 2


def _looks_english(text: str) -> bool:
    """文本含实质性英文单词（剔除 ok/yes 等借用感叹词）才算英语强证据。"""
    words: list[str] = []
    cur: list[str] = []
    for ch in (text or ""):
        if ch.isascii() and ch.isalpha():
            cur.append(ch.lower())
        else:
            if cur:
                words.append("".join(cur))
                cur = []
    if cur:
        words.append("".join(cur))
    return any(w not in _EN_ACKS for w in words)


def _classify_spoken_language(lang: str, text: str) -> tuple[str, bool]:
    """归一语言标签并给出「强证据」判断。

    返回 (lang, strong)：strong=True 表示该判定有可靠证据（粤语特征字/词、
    明确的 yue 标签、实质性英文、够长的普通话句子）；strong=False 表示标签
    模糊（普通话/英文标签 + 短句或借用词）——这种轮次不应把说话人语言拉走。
    """
    key = (lang or "").strip().lower()
    if key in {"yue", "cantonese"}:
        return "yue", True
    if key in {"en", "english"}:
        if _looks_english(text):
            return "en", True
        return "en", False
    if key in {"zh", "chinese", "mandarin"}:
        # 判普通话但文本明显是粤语 → 纠偏（整词命中，避免普粤共用字误伤）。
        if _looks_cantonese(text):
            return "yue", True
        if _looks_mandarin(text):
            return "zh", True
        # 短句（好/嗯/係 之类）两种语言都可能：不构成强证据，交给滞后逻辑。
        return "zh", False
    return "zh", False


def _normalize_asr_language(lang: str, text: str) -> str:
    """ASR 语言标签归一 + 粤语特征纠偏（供 SpeechData/日志使用，不丢强证据信息）。"""
    norm, _ = _classify_spoken_language(lang, text)
    return norm


@dataclass
class LanguageState:
    """Shared between ASR and TTS so replies use the language the user spoke.

    lang 的切换带滞后：只有强证据（明确的 yue/en 标签、粤语特征字词、够长的
    普通话句子）才允许改变当前语言；标签模糊的短轮次（好/嗯/係…）保持原语言，
    避免 ASR 单轮误标把「粤语客户」拉成普通话、LLM 跟着回普、TTS 切音色。
    开场语言由 agent 按人设/对象语言预置，同样受此保护。
    """

    lang: str = "zh"

    def update(self, lang: str | None, text: str = "") -> None:
        norm, strong = _classify_spoken_language(lang, text)
        if strong:
            self.lang = norm


class OpenAICompatLLM(llm.LLM):
    provider = "openai-compat"
    model = ""

    def __init__(self, api_key, model, base_url):
        from openai import AsyncOpenAI

        super().__init__()
        self._model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "256"))

    def chat(self, *, chat_ctx, tools=None, conn_options=None, parallel_tool_calls=None, tool_choice=None, extra_kwargs=None):
        messages = _chat_messages(chat_ctx)
        return _OpenAICompatStream(
            self, chat_ctx, messages, conn_options or APIConnectOptions(), self._max_tokens
        )._real


def _chat_messages(chat_ctx) -> list[dict]:
    messages = []
    for item in getattr(chat_ctx, "items", []):
        if isinstance(item, llm.ChatMessage):
            content = getattr(item, "content", "")
            if isinstance(content, str):
                text = content
            else:
                # content 里文本部分是纯 str（ChatContent = str | ImageContent | AudioContent），
                # 之前用 getattr(c,"text") 会把用户文本全部丢成空串，导致 LLM 听不见用户。
                parts = [
                    c if isinstance(c, str) else (getattr(c, "text", "") or "")
                    for c in content
                ]
                text = "\n".join(parts)
            messages.append({"role": item.role, "content": text})
    if not messages:
        messages = [{"role": "system", "content": "你是 Bok Voice 客服助手。"}]
    return messages


class DeepSeekLLM(OpenAICompatLLM):
    provider = "deepseek"
    model = "deepseek-chat"

    def __init__(self, api_key, model="deepseek-chat", base_url="https://api.deepseek.com/v1"):
        super().__init__(api_key=api_key, model=model, base_url=base_url)


class MlxLlmLLM(OpenAICompatLLM):
    """Local OpenAI-compatible LLM (mlx_lm on macOS, llama-server on Windows).

    Both servers expose /v1 on 127.0.0.1:1235 and run with thinking disabled
    (enable_thinking=false), so replies are fast and content-only.
    """

    provider = "mlx"
    model = "local"

    def __init__(
        self,
        api_key="mlx",
        model=None,
        base_url="http://127.0.0.1:1235/v1",
    ):
        # mlx_lm server requires the real model path in requests; "local" is
        # only a last-resort placeholder when no env/settings provide one.
        if model in (None, "", "local"):
            model = os.environ.get("MLX_LLM_MODEL") or "local"
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url
            or os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1"),
        )


class ScriptedLLM(llm.LLM):
    """Deterministic LLM used by the offline E2E/CI path.

    Inspects the assembled chat context for ``expect_kw`` (a token from the imported
    knowledge/instructions) and returns ``output`` verbatim. This makes the
    "knowledge is injected -> LLM replies per specified script" behaviour testable
    without any cloud API.
    """

    provider = "scripted"
    model = "scripted"

    def __init__(self, expect_kw: str = "", output: str = ""):
        super().__init__()
        self._expect = expect_kw
        self._output = output or "（脚本回复）"

    def chat(self, *, chat_ctx, tools=None, conn_options=None, parallel_tool_calls=None, tool_choice=None, extra_kwargs=None):
        return _ScriptedLLMStream(self, chat_ctx, conn_options or APIConnectOptions())._real


class _ScriptedLLMStream:
    def __init__(self, plugin, chat_ctx, conn_options):
        class _Stream(llm.LLMStream):
            async def _run(self):
                joined = " ".join(
                    str(getattr(x, "text_content", "") or "") for x in getattr(chat_ctx, "items", [])
                )
                hit = (not plugin._expect) or (plugin._expect in joined)
                print("SCRIPTED_LLM_CHECK", f"expect={plugin._expect!r}", f"hit={hit}", flush=True)
                self._event_ch.send_nowait(
                    llm.ChatChunk(
                        id="scripted",
                        delta=llm.ChoiceDelta(content=plugin._output, role="assistant"),
                    )
                )

        self._real = _Stream(llm=plugin, chat_ctx=chat_ctx, tools=[], conn_options=conn_options)

    def __aiter__(self):
        return self._real


class _OpenAICompatStream:
    def __init__(self, plugin, chat_ctx, messages, conn_options, max_tokens=256):
        class _Stream(llm.LLMStream):
            async def _run(self):
                print("LLM_REQUEST_START", flush=True)
                try:
                    stream = await plugin._client.chat.completions.create(
                        model=plugin._model,
                        messages=messages,
                        stream=True,
                        max_tokens=max_tokens,
                        # 专业客服取中低温：过高显油滑/跑题/乱码（本地 9B 更明显），过低像念稿。
                        # 0.4 让语气自然但稳定克制；可用 env LLM_TEMPERATURE 覆盖。
                        temperature=float(os.environ.get("LLM_TEMPERATURE", "0.4")),
                    )
                    async for chunk in stream:
                        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                            print("LLM_REPLY_CHUNK", len(chunk.choices[0].delta.content), flush=True)
                            delta = llm.ChoiceDelta(content=chunk.choices[0].delta.content, role="assistant")
                            self._event_ch.send_nowait(llm.ChatChunk(id=getattr(chunk, "id", "stream"), delta=delta))
                except asyncio.CancelledError:
                    raise

        self._real = _Stream(llm=plugin, chat_ctx=chat_ctx, tools=[], conn_options=conn_options)

    def __aiter__(self):
        return self._real


class _ExprPrependStream(llm.LLMStream):
    """在真实 LLM 流之前先发一个 <expr type="expression" label="..."/> 标记块。"""

    def __init__(self, plugin, inner: "llm.LLMStream", tag: str):
        super().__init__(llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions())
        self._inner = inner
        self._tag = tag

    async def _run(self):
        self._event_ch.send_nowait(
            llm.ChatChunk(id="expr-tag", delta=llm.ChoiceDelta(content=self._tag, role="assistant"))
        )
        async for ev in self._inner:
            self._event_ch.send_nowait(ev)


class ContextState:
    """Shared per-call context memory: per-turn knowledge + running summary.

    Populated asynchronously by the agent's turn listener; read synchronously
    by ``ContextAwareLLM`` to inject a compact, progressive-disclosure system
    message (top-K snippets + bounded conversation summary) each turn.
    """

    def __init__(self, account_id: str = "", max_snippets: int = 3, max_summary_chars: int = 800):
        self.account_id = account_id
        self._max_snippets = max_snippets
        self._max_summary_chars = max_summary_chars
        self._snippets: list[str] = []
        self._summary_lines: list[str] = []
        self._user_lang: str = ""
        self._web: list[str] = []
        self._flow_overview: str = ""
        self._flow_current: str = ""

    def set_flow(self, overview: str, current: str) -> None:
        """设置对话流程:overview 为基础注入(全貌),current 为每轮当前步约束。"""
        self._flow_overview = overview
        self._flow_current = current

    def set_flow_current(self, current: str) -> None:
        """每轮更新当前步约束(flow controller 推进后调用)。"""
        self._flow_current = current

    def set_web(self, results: str | list[str]) -> None:
        """注入联网检索结果（Wikipedia/DDG 摘要），随 system 消息给 LLM 参考。"""
        if isinstance(results, str):
            self._web = [results] if results.strip() else []
        elif results:
            self._web = list(results)
        else:
            self._web = []
        # 联网结果最多保留 2 条、各截断，避免撑爆上下文。
        if len(self._web) > 2:
            self._web = self._web[:2]

    def set_user_language(self, lang: str | None) -> None:
        """ASR 每轮检测到的用户语言：随 system 指令注入，约束回复语言。"""
        key = (lang or "").strip().lower()
        if key in {"chinese", "zh", "mandarin"}:
            self._user_lang = "zh"
        elif key in {"cantonese", "yue"}:
            self._user_lang = "yue"
        elif key in {"english", "en"}:
            self._user_lang = "en"
        else:
            self._user_lang = key

    def set_knowledge(self, snippets: list[dict]) -> None:
        seen: set[str] = set()
        out: list[str] = []
        for s in snippets or []:
            text = str(s.get("text", "") or "").strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
            if len(out) >= self._max_snippets:
                break
        self._snippets = out

    def add_summary(self, role: str, text: str, max_char: int = 200) -> None:
        line = f"{role}: {str(text)[:max_char]}"
        self._summary_lines.append(line)
        joined = "\n".join(self._summary_lines)
        while len(joined) > self._max_summary_chars and len(self._summary_lines) > 1:
            self._summary_lines.pop(0)
            joined = "\n".join(self._summary_lines)

    def render_system_message(self) -> str:
        parts: list[str] = []
        if self._user_lang:
            names = {"zh": "普通话/中文", "yue": "粤语（广东话）", "en": "英语"}
            name = names.get(self._user_lang, self._user_lang)
            if self._user_lang == "yue":
                rule = (
                    "整段用港式粤语（香港客服腔）嚟讲，唔好用书面语、普通话，亦唔好用广州式偏书面的讲法。"
                    "口吻要有港味：唔該晒、唔好意思、我哋/你哋、而家、聽日、啱啱、幫你睇返、唔使擔心。"
                    "可以自然夹杂英文单词/短语（check、confirm、send、email、App、online、status、refund、case、update 呢类服务同操作词），"
                    "似香港人打电话咁讲；但唔好讲成句英文，亦唔好为咗夹而夹。"
                    "（语气参考：「唔好意思，我帮你 check 返个 status，refund 一般 3–5 个工作天会到帐。」"
                    "「我 send 个 email 俾你，跟住入面嘅步骤做就 OK。」）"
                    "唔好解释你讲紧咩语言，唔好加任何注释或者括号。"
                )
            elif self._user_lang == "en":
                rule = "Reply in natural spoken English only (like on a phone call); do not explain or add notes."
            else:
                rule = (
                    "用自然口语的普通话回复，像打电话那样说，不要用书面语或播音腔；"
                    "不要解释你正在使用什么语言，不要添加任何注释或括号。"
                )
            parts.append(f"【用户语言】当前用户正在使用：{name}。{rule}")
        # 客服应答准则：永不主动说"不知道/查不到"，知识不够时用客服话术兜住。
        # 这是客服与聊天机器人的本质区别——客户要的是被接住，不是被拒绝。
        parts.append(
            "【应答准则】你是客服，绝不能说「不知道」「查不到」「不清楚」「没这个资料」「我帮不了你」。"
            "资料/知识不够回答时，用客服的方式接住客户："
            "① 先给确定能给的（安抚、已确认信息、下一步动作）；"
            "② 需要查证/转办的，明确告诉客户你会去核实并回复（如「我帮你查实下，几分钟内回你」），或转给能处理的人/专员跟进；"
            "③ 客户的问题超出当前业务，就用引导话术收住（如「呢单我帮你转俾专门跟进嘅同事，佢会即刻同你联系」），绝不冷场、绝不空手。"
        )
        if self._snippets:
            parts.append("【实时检索到的资料（知识库）】\n" + "\n".join(f"- {s}" for s in self._snippets))
        if self._web:
            parts.append(
                "【联网检索到的资料（来源：Wikipedia/即时答案，可能过时或不准）】\n"
                + "\n".join(f"- {s}" for s in self._web)
                + "\n这些资料可作参考；若与客户问题不直接相关或不足，按「应答准则」接住客户，"
                + "不要生硬说查不到。"
            )
        if self._summary_lines:
            parts.append("【本通对话记忆】\n" + "\n".join(self._summary_lines[-8:]))
        if self._flow_overview:
            parts.append("【话术流程总览(别照读,按进度推进)】\n" + self._flow_overview)
        if self._flow_current:
            parts.append("【现在这一步】\n" + self._flow_current)
        return "\n\n".join(parts)


class ContextAwareLLM(llm.LLM):
    """Injects progressive-disclosure knowledge + bounded conversation memory
    as a system message before every LLM call, keeping the prompt short.
    """

    provider = "context-aware"

    def __init__(self, inner: llm.LLM, context_state: ContextState | None = None):
        super().__init__()
        self._inner = inner
        self._ctx = context_state

    def chat(
        self,
        *,
        chat_ctx,
        tools=None,
        conn_options=None,
        parallel_tool_calls=None,
        tool_choice=None,
        extra_kwargs=None,
    ):
        if self._ctx is not None:
            msg = self._ctx.render_system_message()
            if msg:
                copy = chat_ctx.copy()
                items = list(copy.items)
                # 系统指令必须位于对话开头：把实时上下文（知识/记忆/用户语言）
                # 合并进第一条 system，而不是追加在用户消息之后（后者遵从度差）。
                if items and isinstance(items[0], llm.ChatMessage) and items[0].role == "system":
                    head = items[0].content
                    if isinstance(head, str):
                        merged = f"{msg}\n\n{head}"
                    else:
                        merged = [msg, *head]
                    items[0] = llm.ChatMessage(role="system", content=merged)
                else:
                    items.insert(0, llm.ChatMessage(role="system", content=msg))
                # 截断历史:保留最近 N 轮(默认 4 对),更早的靠「本通对话记忆」摘要兜底。
                # 每轮全量历史会让 prefill 随轮次线性变慢(实测 12 轮 TTFT 2.4s);
                # 截断让上下文有上界,TTFT 稳定。
                max_turns = int(os.environ.get("LLM_HISTORY_TURNS", "4"))
                items = _truncate_chat_items(items, max_turns=max_turns)
                copy.items = items
                chat_ctx = copy
        return self._inner.chat(
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
        )


def _truncate_chat_items(items: list, max_turns: int = 4) -> list:
    """保留开头 system(s) + 最近 max_turns 轮(user/assistant 对)。

    livekit 的 chat_ctx 累积整通历史;旧轮信息由 ContextState 的「本通对话记忆」
    摘要承担,这里只把原始对话剪到最近几轮,控制 prefill token 上界。
    """
    if max_turns <= 0:
        return items
    # 分离 system(前部)与对话(后部)
    split = 0
    for i, it in enumerate(items):
        if getattr(it, "role", "") == "system":
            split = i + 1
        else:
            break
    system_part = items[:split]
    dialog = items[split:]
    if len(dialog) <= max_turns * 2:
        return items
    # 保留最近 max_turns 对(2*max_turns 条),截断更早的
    return system_part + dialog[-(max_turns * 2) :]


class ExprAwareLLM(llm.LLM):
    """确定性 mood 通道（Path B 的兜底保障，见 AGENT.md §3）。

    官方 expressive 依赖 LLM 输出里的 <expr type="expression" label="英文mood"/> 标记，
    真实模型未必遵守指令。本包装器在每次 assistant 回复前强制前置一个标记——
    情绪取对话中最后一条 user 消息的文本分类（EmotionProcessor，11 类英文 label）。
    - 转录管线（TranscriptForwarder）会无条件剥离该标记并发布 lk.expression → 前端 mood；
    - 进 TTS 的一路由 agent.py 的 tts_text_transforms 剥掉，保证不被朗读。
    """

    provider = "expr-aware"

    def __init__(self, inner: llm.LLM, emotion_state=None):
        super().__init__()
        self._inner = inner
        from ..plugins.emotion import EmotionProcessor

        self._emotion = EmotionProcessor()
        self._emotion_state = emotion_state

    def chat(self, *, chat_ctx, tools=None, conn_options=None, parallel_tool_calls=None, tool_choice=None, extra_kwargs=None):
        last_user = ""
        for item in reversed(getattr(chat_ctx, "items", []) or []):
            if getattr(item, "role", None) == "user":
                last_user = getattr(item, "text_content", None) or ""
                break
        mood = self._emotion.classify(last_user)
        if self._emotion_state is not None:
            self._emotion_state.mood = mood
        tag = f'<expr type="expression" label="{mood}"/>'
        inner = self._inner.chat(
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
        )
        return _ExprPrependStream(self, inner, tag)


class FakeLiveKitVAD(vad.VAD):
    model = "fake"
    provider = "fake-vad"

    def __init__(self):
        super().__init__(capabilities=vad.VADCapabilities(update_interval=0.1))

    def stream(self):
        return _FakeVADStream(self)


class _FakeVADStream(vad.VADStream):
    async def _main_task(self):
        import asyncio

        spoke = False
        done = False
        frames = []
        async for item in self._input_ch:
            if done:
                # After one clean turn, swallow everything until the stream is re-used.
                # This stops the old continuous START/END loop that forced the scheduler into
                # a permanently paused state during the client's join/greeting audio.
                continue
            if isinstance(item, self._FlushSentinel):
                if spoke:
                    self._event_ch.send_nowait(vad.VADEvent(type=vad.VADEventType.END_OF_SPEECH, samples_index=0, timestamp=0.0, speech_duration=0.0, silence_duration=0.0, probability=1.0, speaking=False, frames=frames))
                    spoke = False
                    done = True
                    frames = []
                else:
                    done = True
                continue
            frames.append(item)
            if not spoke:
                spoke = True
                print("FAKE_VAD_START", flush=True)
                self._event_ch.send_nowait(vad.VADEvent(type=vad.VADEventType.START_OF_SPEECH, samples_index=0, timestamp=0.0, speech_duration=0.0, silence_duration=0.0, probability=1.0, speaking=True))
                # Keep buffering frames for the configured segment length, then emit one END.
                # This makes the fake VAD fire a single turn per stream instead of endless
                # START/END pairs, which is what the LiveKit turn detector expects.
                await asyncio.sleep(0.5)
                print("FAKE_VAD_END", flush=True)
                self._event_ch.send_nowait(vad.VADEvent(type=vad.VADEventType.END_OF_SPEECH, samples_index=0, timestamp=0.0, speech_duration=0.5, silence_duration=0.0, probability=1.0, speaking=False, frames=frames))
                spoke = False
                done = True
                frames = []


class FakeLiveKitSTT(stt.STT):
    model = "fake"
    provider = "fake-stt"

    def __init__(self, text=None):
        text = text or os.environ.get("FAKE_STT_TEXT", "你好，请介绍一下你们的产品。")
        super().__init__(capabilities=stt.STTCapabilities(streaming=True, interim_results=False, diarization=False, aligned_transcript=False, offline_recognize=False, keyterms=False, chat_context=False))
        self._text = text

    def stream(self, *, language=None, conn_options=None):
        return _FakeSTTStream(self, conn_options or APIConnectOptions(), self._text)

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        return stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT, alternatives=[stt.SpeechData(language="zh", text=self._text)])


class _FakeSTTStream(stt.RecognizeStream):
    def __init__(self, stt_, conn_options, text):
        super().__init__(stt=stt_, conn_options=conn_options)
        self._text = text
        self._emitted = False

    async def _run(self):
        import asyncio

        async for item in self._input_ch:
            if not self._emitted and not isinstance(item, self._FlushSentinel):
                # Buffer a little, then emit a single FINAL once we know the current segment
                # is underway. Debounce so multiple frames don't fan out duplicates.
                await asyncio.sleep(0.2)
                if not self._emitted:
                    self._emitted = True
                    print("FAKE_STT_FINAL", flush=True)
                    self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT, alternatives=[stt.SpeechData(language="zh", text=self._text)]))
                continue
            if isinstance(item, self._FlushSentinel):
                if not self._emitted:
                    self._emitted = True
                    print("FAKE_STT_FINAL", flush=True)
                    self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT, alternatives=[stt.SpeechData(language="zh", text=self._text)]))


class FakeLiveKitTTS(tts.TTS):
    model = "fake"
    provider = "fake-tts"

    def __init__(self, sample_rate=16000):
        # streaming=False so LiveKit wraps it in `tts.StreamAdapter`, which calls our
        # `synthesize()` per sentence. Declaring streaming=True but only implementing the
        # non-streaming `synthesize()` made `tts_node` call the unimplemented `stream()`.
        super().__init__(capabilities=tts.TTSCapabilities(streaming=False, aligned_transcript=True), sample_rate=sample_rate, num_channels=1)

    def synthesize(self, text, *, conn_options=None):
        return _FakeTTSStream(self, text, conn_options or APIConnectOptions())


class _FakeTTSStream(tts.ChunkedStream):
    def __init__(self, tts_, text, conn_options):
        super().__init__(tts=tts_, input_text=text, conn_options=conn_options)

    async def _run(self, output_emitter):
        print("FAKE_TTS_PUSH", flush=True)
        output_emitter.initialize(
            request_id="fake-tts",
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
            stream=False,
        )
        samples = int(self._tts.sample_rate * 0.2)
        pcm = bytes(samples * 2)  # 16-bit mono silence
        output_emitter.push(pcm)
        output_emitter.flush()


class SherpaSenseVoiceSTT(stt.STT):
    """Local SenseVoice ASR via sherpa-onnx (zh/en/ja/ko/yue)."""

    model = "sherpa-sense-voice"
    provider = "sherpa-onnx"

    def __init__(self, model_dir=None, language_state: LanguageState | None = None):
        import os

        import sherpa_onnx

        model_dir = model_dir or os.environ.get("SHERPA_MODEL_DIR", "data/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue")
        self._model_dir = model_dir
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=os.path.join(model_dir, "model.int8.onnx"),
            tokens=os.path.join(model_dir, "tokens.txt"),
            num_threads=2,
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",
            use_itn=True,
        )
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
                diarization=False,
                aligned_transcript=False,
                offline_recognize=True,
                keyterms=False,
                chat_context=False,
            )
        )
        # SenseVoice 情绪标签（<|HAPPY|> 等）在 _decode_pcm 里被解析后记录在此，
        # 供上下文装配/情绪分析使用（文本本身保持干净）。
        self.last_emotion: str | None = None
        self.last_language: str = "zh"
        self._language_state = language_state or LanguageState()

    def stream(self, *, language=None, conn_options=None):
        return _SherpaSTTStream(self, conn_options or APIConnectOptions())

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        # Batch contract consumed by `stt.StreamAdapter` at VAD END_OF_SPEECH. Must return a
        # `SpeechEvent`, not a plain string (the old code returned a str, which the adapter
        # then tried to treat as an event and blew up in production).
        text, lang = _SherpaSTTStream(self, conn_options or APIConnectOptions())._recognize_buffer(buffer)
        self._language_state.update(lang, text)
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id="",
            alternatives=[stt.SpeechData(language=self._language_state.lang, text=text)] if text else [],
        )


class _SherpaSTTStream(stt.RecognizeStream):
    def __init__(self, stt_, conn_options):
        super().__init__(stt=stt_, conn_options=conn_options, sample_rate=16000)
        self._stt_ = stt_
        self._frames = []

    async def _run(self):
        async for item in self._input_ch:
            if isinstance(item, self._FlushSentinel):
                text, lang = self._recognize_frames()
                if text:
                    self._stt_._language_state.update(lang, text)
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(
                            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                            alternatives=[stt.SpeechData(language=self._stt_._language_state.lang, text=text)],
                        )
                    )
                self._frames = []
            else:
                self._frames.append(item)

    def _recognize_buffer(self, buffer):
        import numpy as np

        data = getattr(buffer, "data", b"")
        return self._decode_pcm(data, getattr(buffer, "sample_rate", 16000))

    def _recognize_frames(self):
        pcm = b"".join(f.data for f in self._frames)
        return self._decode_pcm(pcm, 16000)

    def _decode_pcm(self, pcm: bytes, sample_rate: int):
        import numpy as np

        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size == 0:
            return "", ""
        s = self._stt_._recognizer.create_stream()
        s.accept_waveform(sample_rate, samples)
        self._stt_._recognizer.decode_stream(s)
        text = s.result.text or ""
        # SenseVoice 富标签：剥离语言/事件标签（<|zh|>、<|nospeech|>…），
        # 但保留情绪标签信息（<|HAPPY|> 等）——不再像旧版那样一刀切洗掉。
        import re

        _EMOTION_TAGS = {
            "HAPPY", "SAD", "ANGRY", "NEUTRAL", "FEARFUL", "DISGUSTED", "SURPRISED", "EMO_UNKNOWN",
        }
        _tag_re = re.compile(r"<\|([^|]*)\|>")

        def _repl(m: "re.Match[str]") -> str:
            tag = m.group(1).strip().upper()
            if tag in _EMOTION_TAGS:
                self._stt_.last_emotion = tag.lower()
            if tag in {"ZH", "EN", "YUE"}:
                self._stt_.last_language = {"ZH": "zh", "EN": "en", "YUE": "yue"}[tag]
            return ""  # 所有标签都不进入显示文本

        clean = _tag_re.sub(_repl, text).strip()
        return clean, self._stt_.last_language


class VolcanoTTS(tts.TTS):
    """Volcengine (火山) small-model WebSocket streaming TTS.

    Uses the official V3 unidirectional streaming protocol:
    ``wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream``.
    If credentials are missing or the upstream call fails, it degrades to a short beep so the
    voice pipeline (VAD -> STT -> LLM -> TTS -> playout) can be validated offline.
    """

    model = "volcano-tts"
    provider = "volcengine"

    def __init__(self, sample_rate=24000):
        super().__init__(
            # The Volcano stream here is exposed through the non-streaming `synthesize()`;
            # LiveKit wraps it with `tts.StreamAdapter` so we don't need a `stream()`.
            capabilities=tts.TTSCapabilities(streaming=False, aligned_transcript=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self._sample_rate = sample_rate

    def synthesize(self, text, *, conn_options=None):
        return _VolcanoTTSStream(self, text, conn_options or APIConnectOptions())


class _VolcanoTTSStream(tts.ChunkedStream):
    def __init__(self, tts_, text, conn_options):
        super().__init__(tts=tts_, input_text=text, conn_options=conn_options)
        self._text = text
        self._tts_ = tts_

    async def _run(self, output_emitter):
        output_emitter.initialize(
            request_id="volcano-tts",
            sample_rate=self._tts_.sample_rate,
            num_channels=self._tts_.num_channels,
            mime_type="audio/pcm",
            stream=False,
        )
        import os

        app_id = os.environ.get("VOLC_APP_ID", "")
        token = os.environ.get("VOLC_ACCESS_TOKEN", "")
        if not app_id or not token:
            print("VOLC_TTS_MISSING_CREDENTIALS", flush=True)
            await self._emit_beep(output_emitter)
            return

        try:
            import asyncio
            import json
            import uuid

            import websockets

            from .volc_v3_protocol import EventType, MsgType, MsgTypeFlagBits, Message, receive_message

            resource_id = os.environ.get("VOLC_RESOURCE_ID", "seed-tts-2.0")
            speaker = os.environ.get("VOLC_SPEAKER", "zh_female_vv_uranus_bigtts")
            language = os.environ.get("VOLC_LANGUAGE", "")
            dialect = os.environ.get("VOLC_DIALECT", "")

            req_params: dict = {
                "text": self._text,
                "speaker": speaker,
                "audio_params": {"format": "pcm", "sample_rate": self._tts_.sample_rate},
                "speech_rate": int(os.environ.get("VOLC_SPEECH_RATE", "0")),
                "loudness_rate": int(os.environ.get("VOLC_LOUDNESS_RATE", "0")),
            }
            if language:
                req_params["explicit_language"] = language
            if dialect:
                req_params["explicit_dialect"] = dialect

            uri = "wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream"
            addr = os.environ.get("VOLC_TTS_ENDPOINT", uri)  # 允许测试/降级时覆盖端点
            ws = await websockets.connect(
                addr,
                additional_headers={
                    "X-Api-App-Id": app_id,
                    "X-Api-Access-Key": token,
                    "X-Api-Resource-Id": resource_id,
                    "X-Api-Request-Id": str(uuid.uuid4()),
                },
                open_timeout=15,
                max_size=20_000_000,
            )
            # 单向流式：一帧 FullClientRequest（无事件号 flag），携带 user + req_params。
            body = json.dumps(
                {"user": {"uid": "bok-voice"}, "req_params": req_params},
                ensure_ascii=False,
            ).encode("utf-8")
            frame = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.NoSeq, payload=body)
            await ws.send(frame.marshal())

            audio_bytes = 0
            while True:
                msg = await asyncio.wait_for(receive_message(ws), timeout=30)
                if msg.type == MsgType.Error:
                    break
                if msg.type == MsgType.AudioOnlyServer or msg.event == EventType.TTSResponse:
                    if msg.payload:
                        audio_bytes += len(msg.payload)
                        output_emitter.push(msg.payload)
                if msg.event in (EventType.SessionFinished, EventType.ConnectionFinished):
                    break
            await ws.close()
            print("VOLC_TTS_AUDIO_BYTES", audio_bytes, flush=True)
        except Exception as exc:
            print("VOLC_TTS_ERROR", repr(exc), flush=True)
            await self._emit_beep(output_emitter)
        finally:
            output_emitter.flush()

    async def _emit_beep(self, output_emitter):
        import math

        sr = self._tts_.sample_rate
        n = int(sr * 0.4)
        pcm = bytearray()
        for i in range(n):
            v = int(12000 * math.sin(2 * math.pi * 440 * i / sr))
            pcm += v.to_bytes(2, "little", signed=True)
        output_emitter.push(bytes(pcm))
        output_emitter.flush()


class MiniMaxTTS(tts.TTS):
    """LiveKit TTS adapter for MiniMax 语音合成 (T2A).

    云端大模型 TTS：粤语地道（Cantonese_Male_news_anchor_vv2 等 40 语种音色），
    情绪/自然度好。HTTP 同步接口返回 hex 编码 PCM（audio_setting.format=pcm），
    无需转码。双端点：国内 api.minimax.cn / 海外 api.minimax.chat，
    由 MINIMAX_BASE_URL 显式指定，缺省按 MINIMAX_REGION（cn/intl）选择。
    """

    model = "minimax-tts"
    provider = "minimax"

    _CN = "https://api.minimax.cn/v1/t2a_v2"
    _INTL = "https://api.minimax.chat/v1/t2a_v2"

    def __init__(
        self,
        *,
        voice: str | dict = "",
        language_state: LanguageState | None = None,
        sample_rate: int = 24000,
        api_key: str = "",
    ):
        super().__init__(
            # 真流式：声明 streaming=True，voice 管线调 stream() 走 SynthesizeStream，
            # 不再被 StreamAdapter + SentenceTokenizer 包（那会等整句/全文才送 TTS，
            # 中文切句不可靠导致首包要等 LLM 全文吐完）。stream() 内单条 WS 连接按
            # LLM 增量文本持续 task_continue，MiniMax 边合成边回音频，首包几百 ms。
            capabilities=tts.TTSCapabilities(streaming=True, aligned_transcript=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self._voice = voice
        self._language_state = language_state or LanguageState()
        self._key = api_key

    def _endpoint(self) -> str:
        base = os.environ.get("MINIMAX_BASE_URL", "").strip()
        if base:
            return base.rstrip("/")
        region = os.environ.get("MINIMAX_REGION", "cn").strip().lower()
        return self._INTL if region in {"intl", "global", "chat"} else self._CN

    def _api_key(self) -> str:
        # 持久化优先：agent 构造时传入 settings 里的 tts.api_key（设置页保存，重启不丢）；
        # env 是部署级覆盖（MINIMAX_API_KEY）。两者都空则无凭据。
        return self._key or os.environ.get("MINIMAX_API_KEY", "")

    def _endpoint_ws(self) -> str:
        """WebSocket 端点:国内 wss://api.minimax.cn/ws/v1/t2a_v2,海外 .chat。"""
        base = os.environ.get("MINIMAX_WS_URL", "").strip()
        if base:
            return base
        region = os.environ.get("MINIMAX_REGION", "cn").strip().lower()
        return "wss://api.minimax.chat/ws/v1/t2a_v2" if region in {"intl", "global", "chat"} else "wss://api.minimax.cn/ws/v1/t2a_v2"

    def _model(self) -> str:
        return os.environ.get("MINIMAX_MODEL", "speech-2.8-hd")

    def _resolve_voice(self) -> str:
        if isinstance(self._voice, dict):
            return str(self._voice.get(self._language_state.lang) or self._voice.get("zh") or "")
        raw = str(self._voice or "")
        if raw.startswith("{"):
            try:
                mapping = json.loads(raw)
                return str(mapping.get(self._language_state.lang) or mapping.get("zh") or "")
            except Exception:
                return raw
        return raw

    def synthesize(self, text, *, conn_options=None):
        # 保留整段合成路径：livekit 某些非 stream 调用 / 测试仍会走 synthesize。
        return _MiniMaxTTSStream(self, text, conn_options or APIConnectOptions())

    def stream(self, *, conn_options=None):
        """真流式 SynthesizeStream：单 WS 连接按增量文本持续 task_continue。"""
        return _MiniMaxSynthesizeStream(self, conn_options or APIConnectOptions())


class _MiniMaxSynthesizeStream(tts.SynthesizeStream):
    """MiniMax 增量流式：一条 WS 连接，LLM 文本增量到达即 task_continue。

    与旧 ChunkedStream（等整段文本 → 一次 WS）不同：livekit 的 tts_node 对
    streaming=True 的 TTS 会直接调 stream()，把 LLM 逐块文本 push 进来，不再
    用 StreamAdapter 的句子切分（中文切句要等整句/全文，是首包 8-18s 的根因）。
    这里每收到一段文本就 task_continue 到同一条 WS，MiniMax 边合成边回音频，
    首包延迟 ≈ LLM 首句时间 + WS 首音频块，而非等全文。
    """

    def __init__(self, tts_: "MiniMaxTTS", conn_options):
        super().__init__(tts=tts_, conn_options=conn_options)
        self._tts_ = tts_

    async def _run(self, output_emitter):
        import websockets

        try:
            key = self._tts_._api_key()
            if not key:
                print("MINIMAX_TTS_MISSING_CREDENTIALS", flush=True)
                return
            voice = self._tts_._resolve_voice()
            if not voice:
                print("MINIMAX_TTS_NO_VOICE", flush=True)
                return
            sample_rate = self._tts_.sample_rate

            ws = await websockets.connect(
                self._tts_._endpoint_ws(),
                additional_headers={"Authorization": f"Bearer {key}"},
                open_timeout=10,
                max_size=20_000_000,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("MINIMAX_TTS_WS_CONNECT", repr(exc), flush=True)
            # WS 连不上 → 回退整段 synthesize(StreamAdapter 包装,端到端仍出声)
            return

        init_done = False
        frame_bytes = int(sample_rate / 5) * 2  # 200ms 帧
        buf = bytearray()
        recv_task: asyncio.Task | None = None
        try:
            # 首帧 connected_success
            try:
                await asyncio.wait_for(ws.recv(), timeout=10)
            except Exception:
                pass
            start = {
                "event": "task_start",
                "model": self._tts_._model(),
                "voice_setting": {
                    "voice_id": voice,
                    "speed": float(os.environ.get("MINIMAX_SPEED", "1")),
                    "vol": float(os.environ.get("MINIMAX_VOL", "1")),
                    "pitch": int(os.environ.get("MINIMAX_PITCH", "0")),
                },
                "audio_setting": {"sample_rate": sample_rate, "format": "pcm", "channel": 1},
            }
            await ws.send(json.dumps(start))
            try:
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if resp.get("event") != "task_started":
                    print("MINIMAX_TTS_WS_START_FAIL", str(resp)[:200], flush=True)
                    return
            except Exception as exc:
                print("MINIMAX_TTS_WS_START", repr(exc), flush=True)
                return

            async def _recv_loop():
                nonlocal init_done, buf
                while True:
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                    except asyncio.TimeoutError:
                        break
                    except Exception:
                        break
                    data = msg.get("data") or {}
                    audio_hex = data.get("audio") or ""
                    if audio_hex:
                        chunk = bytes.fromhex(audio_hex)
                        if not init_done:
                            output_emitter.initialize(
                                request_id="minimax-tts",
                                sample_rate=sample_rate,
                                num_channels=self._tts_.num_channels,
                                mime_type="audio/pcm",
                                stream=True,
                            )
                            output_emitter.start_segment(segment_id="minimax-tts")
                            init_done = True
                        buf.extend(chunk)
                        while len(buf) >= frame_bytes:
                            output_emitter.push(bytes(buf[:frame_bytes]))
                            output_emitter.flush()
                            del buf[:frame_bytes]
                    # is_final = 当前已合成文本的段边界，不代表任务结束（task_continue
                    # 可继续追加合成）。这里只标记，不退出；收尾由调用方 cancel。
                    if msg.get("is_final") and buf:
                        output_emitter.push(bytes(buf))
                        output_emitter.flush()
                        buf.clear()

            recv_task = asyncio.create_task(_recv_loop())

            # 按句增量合成:把 _input_ch 文本按句子边界(。！？!?)切句,每句 task_continue
            # 发「该句增量」(非累积全文)。实测语义:
            # - 发累积全文会重复合成前面句子(长回复下明显重读);
            # - 纯逐块增量(不按句)在快速 send 下丢音频(服务端要等足文本才合成)。
            # 按句切分 + 连续发送 = 首句到就出声(低延迟)且不重复(每句只发一次)。
            # 音频按序回流,recv_loop 持续推给 emitter,无需句间等待。
            _SENT_END = "。！？!?"
            sent_buf = ""
            async for item in self._input_ch:
                if isinstance(item, self._FlushSentinel):
                    continue
                text = str(item or "")
                if not text.strip():
                    continue
                sent_buf += text
                while True:
                    idx = min(
                        (sent_buf.find(ch) for ch in _SENT_END if sent_buf.find(ch) != -1),
                        default=-1,
                    )
                    if idx == -1:
                        break
                    sentence = sent_buf[: idx + 1]
                    sent_buf = sent_buf[idx + 1 :]
                    if sentence.strip():
                        await ws.send(json.dumps({"event": "task_continue", "text": sentence.strip()}))
            if sent_buf.strip():
                await ws.send(json.dumps({"event": "task_continue", "text": sent_buf.strip()}))
            # 文本结束:发 task_finish 让服务端吐完剩余音频并回 is_final
            try:
                await ws.send(json.dumps({"event": "task_finish"}))
            except Exception:
                pass
            # 收尾:等 recv_loop 把剩余音频推完(最多 15s)
            if recv_task:
                try:
                    await asyncio.wait_for(recv_task, timeout=15)
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("MINIMAX_TTS_WS_ERR", repr(exc), flush=True)
        finally:
            if recv_task:
                recv_task.cancel()
                try:
                    await recv_task
                except Exception:
                    pass
            try:
                await ws.close()
            except Exception:
                pass
            # 推完残余音频并结束 segment(收到 is_final 时 buf 可能还有尾部)
            if init_done and buf:
                try:
                    output_emitter.push(bytes(buf))
                    output_emitter.flush()
                    output_emitter.end_segment()
                except Exception:
                    pass


class _MiniMaxTTSStream(tts.ChunkedStream):
    def __init__(self, tts_, text, conn_options):
        super().__init__(tts=tts_, input_text=text, conn_options=conn_options)
        self._text = text
        self._tts_ = tts_

    async def _run(self, output_emitter):
        try:
            key = self._tts_._api_key()
            if not key:
                print("MINIMAX_TTS_MISSING_CREDENTIALS", flush=True)
                await self._emit_beep(output_emitter)
                return
            voice = self._tts_._resolve_voice()
            if not voice:
                print("MINIMAX_TTS_NO_VOICE", flush=True)
                await self._emit_beep(output_emitter)
                return
            sample_rate = self._tts_.sample_rate
            # WebSocket 流式(像电话:首包 ~380ms 边合成边推);失败/显式关闭回退 HTTP 整段。
            if os.environ.get("MINIMAX_WS", "1") == "1":
                ok = await self._run_ws(output_emitter, key, voice, sample_rate)
                if ok:
                    return
                print("MINIMAX_TTS_WS_FALLBACK_HTTP", flush=True)
            await self._run_http(output_emitter, key, voice, sample_rate)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("MINIMAX_TTS_FATAL", repr(exc), flush=True)
            try:
                await self._emit_beep(output_emitter)
            except Exception:  # pragma: no cover
                pass

    async def _run_ws(self, output_emitter, key: str, voice: str, sample_rate: int) -> bool:
        """MiniMax WebSocket 流式:task_start → task_continue(text) → 边收 hex 音频边推。"""
        import ssl

        import websockets

        url = self._endpoint_ws()
        try:
            ws = await websockets.connect(
                url,
                additional_headers={"Authorization": f"Bearer {key}"},
                open_timeout=10,
                max_size=20_000_000,
            )
        except Exception as exc:
            print("MINIMAX_TTS_WS_CONNECT", repr(exc), flush=True)
            return False
        try:
            # 首帧通常是 connected_success
            try:
                await asyncio.wait_for(ws.recv(), timeout=10)
            except Exception:
                pass
            start = {
                "event": "task_start",
                "model": self._model(),
                "voice_setting": {
                    "voice_id": voice,
                    "speed": float(os.environ.get("MINIMAX_SPEED", "1")),
                    "vol": float(os.environ.get("MINIMAX_VOL", "1")),
                    "pitch": int(os.environ.get("MINIMAX_PITCH", "0")),
                },
                "audio_setting": {"sample_rate": sample_rate, "format": "pcm", "channel": 1},
            }
            await ws.send(json.dumps(start))
            try:
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if resp.get("event") != "task_started":
                    print("MINIMAX_TTS_WS_START_FAIL", str(resp)[:200], flush=True)
                    return False
            except Exception as exc:
                print("MINIMAX_TTS_WS_START", repr(exc), flush=True)
                return False
            await ws.send(json.dumps({"event": "task_continue", "text": self._text}))
            pcm_total = 0
            frame_bytes = int(sample_rate / 5) * 2  # 200ms 帧
            buf = bytearray()
            init_done = False
            while True:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                except asyncio.TimeoutError:
                    break
                data = msg.get("data") or {}
                audio_hex = data.get("audio") or ""
                if audio_hex:
                    chunk = bytes.fromhex(audio_hex)
                    if not init_done:
                        output_emitter.initialize(
                            request_id="minimax-tts",
                            sample_rate=sample_rate,
                            num_channels=self._tts_.num_channels,
                            mime_type="audio/pcm",
                            stream=True,
                        )
                        output_emitter.start_segment(segment_id="minimax-tts")
                        init_done = True
                    # 攒 200ms 帧推给 livekit,让它边收边播
                    buf.extend(chunk)
                    while len(buf) >= frame_bytes:
                        output_emitter.push(bytes(buf[:frame_bytes]))
                        output_emitter.flush()
                        del buf[:frame_bytes]
                        pcm_total += frame_bytes
                if msg.get("is_final"):
                    if buf:
                        output_emitter.push(bytes(buf))
                        output_emitter.flush()
                        pcm_total += len(buf)
                    if init_done:
                        output_emitter.end_segment()
                    break
            print("MINIMAX_TTS_WS_BYTES", pcm_total, flush=True)
            return init_done  # 有推流才算成功
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("MINIMAX_TTS_WS_ERR", repr(exc), flush=True)
            return False
        finally:
            try:
                await ws.close()
            except Exception:  # pragma: no cover
                pass

    async def _run_http(self, output_emitter, key: str, voice: str, sample_rate: int) -> None:
        """HTTP 整段合成(WS 不可用时的降级)。"""
        endpoint = self._tts_._endpoint()
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        endpoint,
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={
                            "model": self._model(),
                            "text": self._text,
                            "voice_setting": {
                                "voice_id": voice,
                                "speed": float(os.environ.get("MINIMAX_SPEED", "1")),
                                "vol": float(os.environ.get("MINIMAX_VOL", "1")),
                                "pitch": int(os.environ.get("MINIMAX_PITCH", "0")),
                            },
                            "audio_setting": {"sample_rate": sample_rate, "format": "pcm", "channel": 1},
                        },
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    data = body.get("data") or {}
                    audio_hex = data.get("audio") or ""
                    if not audio_hex:
                        raise RuntimeError(f"minimax empty audio: {body.get('base_resp')}")
                    pcm = bytes.fromhex(audio_hex)
                    output_emitter.initialize(
                        request_id="minimax-tts",
                        sample_rate=sample_rate,
                        num_channels=self._tts_.num_channels,
                        mime_type="audio/pcm",
                        stream=False,
                    )
                    output_emitter.push(pcm)
                    print("MINIMAX_TTS_BYTES", len(pcm), flush=True)
                    return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                print("MINIMAX_TTS_RETRY", attempt + 1, repr(exc), flush=True)
                await asyncio.sleep(0.5 * (attempt + 1))
        print("MINIMAX_TTS_ERROR", repr(last_exc), flush=True)
        await self._emit_beep(output_emitter)

    async def _emit_beep(self, output_emitter):
        import math

        sr = self._tts_.sample_rate
        n = int(sr * 0.4)
        pcm = bytearray()
        for i in range(n):
            v = int(12000 * math.sin(2 * math.pi * 440 * i / sr))
            pcm += v.to_bytes(2, "little", signed=True)
        output_emitter.push(bytes(pcm))
        output_emitter.flush()


class Qwen3TTSTTS(tts.TTS):
    """LiveKit TTS adapter for the local Qwen3-TTS sidecar."""

    model = "qwen3-tts"
    provider = "qwen3-tts"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8788",
        voice: str | dict = "",
        language_state: LanguageState | None = None,
        instruct: str = "",
        emotion_state=None,
        sample_rate: int = 24000,
    ):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False, aligned_transcript=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self._base_url = base_url.rstrip("/")
        self._voice = voice
        self._language_state = language_state or LanguageState()
        self._instruct = instruct
        self._emotion_state = emotion_state

    def _resolve_instruct(self) -> str:
        # 把当前情绪映射为动态语气指令，与静态 instruct 一起传给 CustomVoice。
        parts = [p for p in (self._instruct, (self._emotion_state.instruct_for_mood() if self._emotion_state else "")) if p]
        return "；".join(parts)

    def synthesize(self, text, *, conn_options=None):
        return _Qwen3TTSStream(self, text, conn_options or APIConnectOptions())

    def _resolve_voice(self) -> str:
        if isinstance(self._voice, dict):
            return str(self._voice.get(self._language_state.lang) or self._voice.get("zh") or "")
        raw = str(self._voice or "")
        if raw.startswith("{"):
            try:
                mapping = json.loads(raw)
                return str(mapping.get(self._language_state.lang) or mapping.get("zh") or "")
            except Exception:
                return raw
        return raw


class _Qwen3TTSStream(tts.ChunkedStream):
    def __init__(self, tts_, text, conn_options):
        super().__init__(tts=tts_, input_text=text, conn_options=conn_options)
        self._text = text
        self._tts_ = tts_

    async def _run(self, output_emitter):
        # emitter 是否已 initialize：未收到响应就被打断/挂断时 emitter 从未启动，
        # 此时 flush 会抛 "AudioEmitter isn't started"（误报 TTS 断链）。只在已启动后收尾。
        started = False
        try:
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=120) as client:
                        resp = await client.post(
                            f"{self._tts_._base_url}/v1/audio/speech",
                            json={
                                "input": self._text,
                                "voice": self._tts_._resolve_voice(),
                                "language": self._tts_._language_state.lang,
                                "instruct": self._tts_._resolve_instruct(),
                                "sample_rate": self._tts_.sample_rate,
                                "response_format": "pcm",
                                "streaming": True,
                                "chunk_ms": 200,
                            },
                        )
                        resp.raise_for_status()
                        # Stream the PCM body into 200ms frames. The sidecar
                        # streams with non_streaming_mode=False (Qwen3-TTS
                        # Dual-Track fast path), and pushing frames here lets
                        # LiveKit start playback/barge-in handling as soon as
                        # the first frames are available instead of one blob.
                        pcm_total = 0
                        frame_bytes = (
                            self._tts_.sample_rate // 5
                        ) * 2  # 200ms, 16-bit mono
                        buf = bytearray()
                        first_audio = False
                        output_emitter.initialize(
                            request_id="qwen3-tts",
                            sample_rate=self._tts_.sample_rate,
                            num_channels=self._tts_.num_channels,
                            mime_type="audio/pcm",
                            stream=True,
                        )
                        started = True
                        output_emitter.start_segment(segment_id="qwen3-tts")
                        async for data in resp.aiter_bytes():
                            buf.extend(data)
                            # Push the first partial frame as soon as ~40ms is
                            # available instead of waiting for a full 200ms
                            # buffer: the sidecar streams ~83ms model chunks
                            # (QWEN3_TTS_STREAM_INTERVAL=0.1), so this shaves
                            # ~150ms off the time-to-first-audio without
                            # changing steady-state frame size.
                            if not first_audio and len(buf) >= frame_bytes // 5:
                                output_emitter.push(bytes(buf))
                                output_emitter.flush()
                                pcm_total += len(buf)
                                buf.clear()
                                first_audio = True
                            while len(buf) >= frame_bytes:
                                output_emitter.push(bytes(buf[:frame_bytes]))
                                output_emitter.flush()
                                del buf[:frame_bytes]
                                pcm_total += frame_bytes
                        if buf:
                            output_emitter.push(bytes(buf))
                            output_emitter.flush()
                            pcm_total += len(buf)
                        output_emitter.end_segment()
                        print("QWEN3_TTS_BYTES", pcm_total, flush=True)
                        return
                except Exception as exc:  # noqa: BLE001 - retry transient gateway failures
                    last_exc = exc
                    print("QWEN3_TTS_RETRY", attempt + 1, repr(exc), flush=True)
                    await asyncio.sleep(0.5 * (attempt + 1))
            if not asyncio.current_task().cancelling():
                print("QWEN3_TTS_ERROR", repr(last_exc), flush=True)
                await self._emit_beep(output_emitter)
        except asyncio.CancelledError:
            # 会话关闭/打断时不播放故障蜂鸣，直接收尾。
            raise
        except Exception as exc:
            print("QWEN3_TTS_FATAL", repr(exc), flush=True)
            await self._emit_beep(output_emitter)
        finally:
            if started:
                try:
                    output_emitter.flush()
                except Exception:  # pragma: no cover - 收尾失败不影响主流程
                    pass

    async def _emit_beep(self, output_emitter):
        import math

        sr = self._tts_.sample_rate
        n = int(sr * 0.4)
        pcm = bytearray()
        for i in range(n):
            v = int(12000 * math.sin(2 * math.pi * 440 * i / sr))
            pcm += v.to_bytes(2, "little", signed=True)
        output_emitter.push(bytes(pcm))
        output_emitter.flush()


class Qwen3ASRSTT(stt.STT):
    """LiveKit STT adapter for the local Qwen3-ASR sidecar."""

    model = "qwen3-asr"
    provider = "qwen3-asr"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8787",
        language_state: LanguageState | None = None,
    ):
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
                diarization=False,
                aligned_transcript=False,
                offline_recognize=True,
                keyterms=False,
                chat_context=False,
            )
        )
        self._base_url = base_url.rstrip("/")
        self._language_state = language_state or LanguageState()

    def stream(self, *, language=None, conn_options=None):
        return _Qwen3ASRStream(self, conn_options or APIConnectOptions())

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        text, lang = await _Qwen3ASRStream(
            self, conn_options or APIConnectOptions()
        )._recognize_buffer(buffer)
        if text:
            self._language_state.update(lang, text)
            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                request_id="",
                alternatives=[stt.SpeechData(language=self._language_state.lang, text=text)],
            )
        return stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT, request_id="", alternatives=[])


class _Qwen3ASRStream(stt.RecognizeStream):
    def __init__(self, stt_, conn_options):
        super().__init__(stt=stt_, conn_options=conn_options, sample_rate=16000)
        self._stt_ = stt_
        self._frames = []

    async def _run(self):
        try:
            async for item in self._input_ch:
                if isinstance(item, self._FlushSentinel):
                    try:
                        text, lang = await self._recognize_frames()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        text, lang = "", ""
                    if text:
                        self._stt_._language_state.update(lang, text)
                        self._event_ch.send_nowait(
                            stt.SpeechEvent(
                                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                                alternatives=[stt.SpeechData(language=self._stt_._language_state.lang, text=text)],
                            )
                        )
                    self._frames = []
                else:
                    self._frames.append(item)
        except asyncio.CancelledError:
            raise
        finally:
            self._frames = []

    async def _recognize_buffer(self, buffer):
        data = getattr(buffer, "data", b"")
        pcm = bytes(data)
        return await self._post_audio(pcm)

    async def _recognize_frames(self):
        pcm = b"".join(getattr(f, "data", b"") for f in self._frames)
        return await self._post_audio(pcm)

    async def _post_audio(self, pcm: bytes):
        if not pcm:
            return "", ""
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    start = await client.post(f"{self._stt_._base_url}/api/start")
                    start.raise_for_status()
                    session_id = start.json()["session_id"]
                    for i in range(0, len(pcm), 3200):
                        await client.post(
                            f"{self._stt_._base_url}/api/chunk",
                            params={"session_id": session_id},
                            content=pcm[i : i + 3200],
                            headers={"Content-Type": "application/octet-stream"},
                        )
                    final = await client.post(
                        f"{self._stt_._base_url}/api/finish",
                        params={"session_id": session_id},
                    )
                    final.raise_for_status()
                    data = final.json()
                    text = str(data.get("text") or "")
                    lang = str(data.get("language") or "")
                    lang = _normalize_asr_language(lang, text)
                    print("QWEN3_ASR_TEXT", repr(text[:120]), lang, flush=True)
                    return text, lang
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry transient gateway failures
                last_exc = exc
                print("QWEN3_ASR_RETRY", attempt + 1, repr(exc), flush=True)
                await asyncio.sleep(0.5 * (attempt + 1))
        print("QWEN3_ASR_ERROR", repr(last_exc), flush=True)
        return "", ""
