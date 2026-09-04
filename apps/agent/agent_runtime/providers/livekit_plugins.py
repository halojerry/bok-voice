"""LiveKit-compatible provider plugins: OpenAI-compatible LLMs + offline fakes."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass

import httpx
from livekit.agents import APIConnectOptions, NOT_GIVEN, llm, stt, tts, utils, vad
from livekit.plugins.openai import LLM as _OpenAICompatBase

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

# 港式粤语高频用词表(普→港口语词):只放「普通话书面词 → 港式口语词」的用词替换,
# 不放纯字形(简→繁)条目——字形由规则里的「直接輸出繁體」统一管,避免两件事搅在一起。
# 只保留客服高频词(疑问/人称/常用动词/礼貌/集运业务);低频书面词交给模型粤语能力,
# 控制静态前缀体积(prefill 与 KV-cache 都受益)。
_HK_CANTONESE_LEXICON = (
    "这个→呢個 那个→嗰個 这些→呢啲 这里→呢度 那里→嗰度 什么→乜嘢 怎么→點樣 为什么→點解 "
    "谁→邊個 哪里→邊度 什么时候→幾時 多少→幾多 "
    "是→係 不是→唔係 的→嘅 了→咗 在→喺 来→嚟 没有→冇 不要→唔好 不用→唔使 不知道→唔知 "
    "现在→而家 刚刚→啱啱 今天→今日 明天→聽日 "
    "我们→我哋 你们→你哋 他们→佢哋 告诉→話俾 看→睇 找→搵 给→俾 拿→攞 "
    "谢谢→唔該晒 对不起→唔好意思 没问题→冇問題 "
    "快递→速遞 包裹→集運件 联系→聯絡 确认→確認 帮忙→幫手 可以吗→得唔得/可唔可以 "
)

# 中文对话里常被整词借用的英语感叹词：出现它们不代表用户切到英语。
_EN_ACKS = {"ok", "okay", "yes", "yeah", "yep", "no", "nope", "hi", "hey", "hello", "bye", "thx"}


def _inject_pauses(text: str) -> str:
    """给要交给 MiniMax 的整段文本在句末适度加停顿标签 <#0.3#>。

    只在「该句较长(≥16 字)且以 。！？!? 收尾」的句子后插,制造自然断句停顿;
    短句/疑问反问不硬插,避免拖沓。可用 MINIMAX_PAUSE=0 关、MINIMAX_PAUSE_SECS 调时长。
    停顿标签不能连续叠加(文档限制),此处逐句只加一个,安全。
    """
    if os.environ.get("MINIMAX_PAUSE", "1") != "1":
        return text
    if not text:
        return text
    try:
        secs = float(os.environ.get("MINIMAX_PAUSE_SECS", "0.3"))
    except ValueError:  # pragma: no cover
        secs = 0.3
    tag = f"<#{min(max(secs, 0.01), 1.5):.2f}#>"
    out = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。！？!?" and len(buf) >= 16:
            out.append(buf + tag)
            buf = ""
    if buf:
        out.append(buf)
    return "".join(out)


# ---- 发音教学形输出拦截 ----
# 弱模型(4B)对 prompt 里「数字要读成汉字」这类规则会过度字面化,自造一段
# 「粤语用字粤拼(Jyutping)发音要点 1一jat1…10十sap6」课程并让 TTS 照念。
# 正常客服回复永不出现这些术语/罗马拼音+调号,命中即整段替换成「请重报单号」。
_LECTURE_TERMS = (
    "發音要點", "发音要点", "發音教學", "发音教学", "拼音教學", "拼音教学",
    "入聲字", "入声字", "入聲", "入声", "韻母", "韵母", "聲調", "声调",
    "尾音收", "粵拼", "粤拼", "Jyutping", "jyutping", "JYUTPING",
    "双唇閉合", "双唇闭合", "發音短促", "发音短促", "高升調", "高升调",
)
# 罗马字拼音/粤拼+声调数字:jat1、gau2、saam1、yi1 这类。不用开头的 \b,
# 让「一jat1」这种紧贴汉字的写法也能命中;≥3 个才判「课程」,避免正常夹
# 英文(如 version2/order3 偶发)误伤。
_JYUTPING_TOKEN_RE = re.compile(r"[A-Za-z]{1,8}[1-6]\b")
# 罐头的语言跟随文本里的粤语特征字(兜底);有明确会话语言时用会话语言。
_CANTONESE_HINT_CHARS = set("嘅唔冇喺嗰咁嚟啲佢哋")
_LECTURE_CANNED_CANTONESE = "唔好意思，頭先聽得唔係好清楚，可唔可以再講多次個單號或者訂單號碼俾我？"
_LECTURE_CANNED_ZH = "不好意思，刚才没太听清楚，可以再把单号或订单号码说一遍吗？"


def is_lecture_text(text: str) -> bool:
    """整段是否「发音/拼音/声调教学」形(正常客服回复不会是)。"""
    if not text:
        return False
    if any(w in text for w in _LECTURE_TERMS):
        return True
    return len(_JYUTPING_TOKEN_RE.findall(text)) >= 3


def _lecture_lang(text: str) -> str:
    if any(ch in text for ch in _CANTONESE_HINT_CHARS):
        return "cantonese"
    return "zh"


def lecture_canned(lang: str | None = None) -> str:
    return _LECTURE_CANNED_CANTONESE if lang == "cantonese" else _LECTURE_CANNED_ZH


def lecture_guard(text: str, lang: str | None = None) -> str:
    """教学形输出 → 罐头的「请客户再报一次单号」;正常回复原样返回。

    在转录落库与 TTS 合成两处都套用,保证音频同 transcript 一致——
    唔会「录低咗段教学、播咗第二句」。
    """
    if not text:
        return text
    if not is_lecture_text(text):
        return text
    return lecture_canned(lang or _lecture_lang(text))


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
    明确的 cantonese 标签、实质性英文、够长的普通话句子）；strong=False 表示标签
    模糊（普通话/英文标签 + 短句或借用词）——这种轮次不应把说话人语言拉走。
    """
    key = (lang or "").strip().lower()
    if key == "cantonese":
        return "cantonese", True
    if key in {"en", "english"}:
        if _looks_english(text):
            return "en", True
        return "en", False
    if key in {"zh", "chinese", "mandarin"}:
        # 判普通话但文本明显是粤语 → 纠偏（整词命中，避免普粤共用字误伤）。
        if _looks_cantonese(text):
            return "cantonese", True
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

    规范语言值: zh / cantonese / en（粤语统一叫 cantonese，全时空唯一拼写；
    旧值已由 CP 启动迁移清零，代码不再兜别名）。
    lang 的切换带滞后：只有强证据（明确的 cantonese/en 标签、粤语特征字词、够长的
    普通话句子）才允许改变当前语言；标签模糊的短轮次（好/嗯/係…）保持原语言，
    避免 ASR 单轮误标把「粤语客户」拉成普通话、LLM 跟着回普、TTS 切音色。
    开场语言由 agent 按人设/对象语言预置，同样受此保护。
    """

    lang: str = "zh"

    def update(self, lang: str | None, text: str = "") -> None:
        norm, strong = _classify_spoken_language(lang, text)
        if strong:
            self.lang = norm


class PinnedLanguageState(LanguageState):
    """钉定语言态：lang 恒等于构造值，update 永不改写。

    用于「语言钉死」场景（B 线同传源语言 / A 线设置 asr.language_mode=fixed）：
    per-request hint 整场恒下发钉定语言，不吃 ASR 强证据漂移，也不与共享
    language_state 的回复锚定/滞回互相干扰。
    """

    def update(self, lang: str | None, text: str = "") -> None:
        pass


class MlxLlmLLM(_OpenAICompatBase):
    """本地 OpenAI 兼容 LLM（macOS mlx_lm / Windows llama-server，:1235，thinking 关闭）。

    内芯=官方 livekit-plugins-openai（兼容任意 OpenAI 端点）：白得 function tools
    解析、APIError 重试、error 事件、TTFT/usage 官方 metrics；原先手写的流解析/
    重试/秒表已删。stop/max_tokens 走 extra_body（本地服务吃经典参数，不吃新的
    max_completion_tokens）；温度 LLM_TEMPERATURE 默认 0.35（4B 小模型防飘/复读）。
    """

    provider = "mlx"

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
        extra_body = {
            "max_tokens": int(os.environ.get("LLM_MAX_TOKENS", "160")),
            # Qwen3 对话模板以 <|im_end|> 收尾:唔传 stop 个 server 会当文字输出
            # (转录/TTS 见住 <|im_end|>),喺源头截停最干净;下游再剥多一重保险。
            "stop": ["<|im_end|>", "<|im_start|>", "<|endoftext|>"],
        }
        # 定制采样(env 未设时不进请求,A 线默认路径零变化):B 线 MT 档要
        # top_p/top_k/重复惩罚收窄采样,防翻译小模型自由发挥/复读。top_k 收
        # 整数(mlx_lm server 按 int 校验),其余收浮点。
        for key, env_key in (
            ("top_p", "LLM_TOP_P"),
            ("top_k", "LLM_TOP_K"),
            ("repetition_penalty", "LLM_REPETITION_PENALTY"),
        ):
            raw = os.environ.get(env_key, "").strip()
            if not raw:
                continue
            try:
                extra_body[key] = int(raw) if raw.isdigit() else float(raw)
            except ValueError:  # pragma: no cover - 配错当没配,唔炸构造
                continue
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url
            or os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1"),
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.35")),
            extra_body=extra_body,
        )

    async def _prewarm_impl(self) -> None:
        # 真实 1-token 生成：暖 mlx 模型（冷启动的 KV 分配/首 token 占首包大头）。
        # 官方 prewarm 只验连接；AgentSession 构造时会自动调用本钩子。
        if os.environ.get("LLM_WARMUP", "1") != "1":
            return
        try:
            await self._client.chat.completions.create(
                model=self._opts.model,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1,
            )
            print("[agent] llm warmup done", flush=True)
        except Exception as exc:  # pragma: no cover - warmup 失败不致命
            print(f"[agent] llm warmup skipped: {exc!r}", flush=True)


class DeepSeekLLM(_OpenAICompatBase):
    """DeepSeek 云端（OpenAI 兼容契约，与本地 MlxLlmLLM 同一官方内芯）。"""

    provider = "deepseek"

    def __init__(self, api_key="", model="deepseek-chat", base_url="https://api.deepseek.com/v1"):
        super().__init__(
            model=model or "deepseek-chat",
            api_key=api_key,
            base_url=base_url,
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.35")),
            extra_body={"max_tokens": int(os.environ.get("LLM_MAX_TOKENS", "160"))},
        )


# Hy-MT2 官方模板的目标语名称(中文变体);未知语言值直接原样进模板。
_MT_PROMPT_NAMES = {"zh": "中文", "cantonese": "粤语", "en": "英语"}


def _forward_extra_kwargs(extra_kwargs):
    """包装层向内芯透传 extra_kwargs 的统一口径:非空 dict 原样,其余一律 NOT_GIVEN。

    官方 openai 内芯(1.7.1)用 is_given(extra_kwargs) 判定,而 is_given(None)=True,
    透传 None 会在内芯 extra.update(None) 处 TypeError——框架从不传 extra_kwargs,
    包装层默认值必须给 NOT_GIVEN(与官方 chat 签名同契约),None 绝不进内芯。
    """
    return extra_kwargs if extra_kwargs else NOT_GIVEN


def _bind_metrics_forward(inner: llm.LLM, outer: llm.LLM) -> None:
    """包装层把内芯的 LLMMetrics("metrics_collected")转发到自己身上。

    官方管线只在 session.llm(最外层包装)上挂监听(agent_activity.py:
    self.llm.on("metrics_collected", ...)),而 LLMMetrics 由内芯的流监视器 emit 在
    【创建流的对象】上(llm/llm.py:432 self._llm.emit)——包装层不转发,LLM metrics
    永远到不了 session,agent.py 的 LLM_TTFT_MS(official) 哑火(RCA §0.3)。
    STT 侧 Qwen3ASRLiveSTT 已同款转发;这里给 LLM 包装层统一补齐。
    """
    inner.on("metrics_collected", lambda *args, **kwargs: outer.emit("metrics_collected", *args, **kwargs))


def _mt_prompt(text: str, target_lang: str) -> str:
    """官方 Hy-MT2 中文翻译模板:只要译文,不解释。"""
    name = _MT_PROMPT_NAMES.get(target_lang, target_lang)
    return f"将以下文本翻译为 `{name}`，注意只需要输出翻译后的结果，不要额外解释：\n\n`{text}`"


class StatelessMTLLM(llm.LLM):
    """逐句无状态 MT 包装(Hy-MT2 翻译小模型,B 线同传专用)。

    MT 模型逐句无状态:每次调用只取进来 chat_ctx 的最后一条 user 文本,套官方
    模板压成一条 user 消息发内芯。丢历史有两个理由——历史会污染译文(前文术语/
    译法串味,翻译要每句独立);且无状态请求前缀恒定,prefill 不随通话增长,
    TTFT 全场稳定(第 100 句同第 1 句快)。
    """

    def __init__(self, inner: llm.LLM, target_lang: str):
        super().__init__()
        self._inner = inner
        self._target_lang = target_lang
        _bind_metrics_forward(inner, self)

    @property
    def model(self) -> str:
        # model/provider 跟内芯走(usage/metrics 面板显示真实内芯,不是包装层)。
        return str(getattr(self._inner, "model", "unknown"))

    @property
    def provider(self) -> str:
        return str(getattr(self._inner, "provider", "unknown"))

    def chat(
        self,
        *,
        chat_ctx,
        tools=None,
        conn_options=None,
        parallel_tool_calls=None,
        tool_choice=None,
        extra_kwargs=NOT_GIVEN,
    ):
        last_user = ""
        for item in reversed(getattr(chat_ctx, "items", []) or []):
            if getattr(item, "role", None) == "user":
                last_user = str(getattr(item, "text_content", None) or "")
                break
        if not last_user:
            # 异常轮次(冇 user 文本):原样透传,唔发空模板请求。
            return self._inner.chat(
                chat_ctx=chat_ctx,
                tools=tools,
                conn_options=conn_options,
                parallel_tool_calls=parallel_tool_calls,
                tool_choice=tool_choice,
                extra_kwargs=_forward_extra_kwargs(extra_kwargs),
            )
        mt_ctx = llm.ChatContext()
        mt_ctx.add_message(role="user", content=_mt_prompt(last_user, self._target_lang))
        return self._inner.chat(
            chat_ctx=mt_ctx,
            tools=tools,
            conn_options=conn_options,
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            extra_kwargs=_forward_extra_kwargs(extra_kwargs),
        )

    async def _prewarm_impl(self) -> None:
        # 委托内芯:MT 模型同样吃 1-token 真生成的暖机收益(对齐 MlxLlmLLM)。
        inner_prewarm = getattr(self._inner, "_prewarm_impl", None)
        if inner_prewarm is not None:
            await inner_prewarm()


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


class _ExprPrependStream(llm.LLMStream):
    """在真实 LLM 流之前先发一个 <expr type="expression" label="..."/> 标记块。"""

    def __init__(self, plugin, inner: "llm.LLMStream", tag: str):
        super().__init__(llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions())
        self._inner = inner
        self._tag = tag

    async def _metrics_monitor_task(self, event_aiter) -> None:
        # 官方 LLMStream 基类会为每条流跑一个 metrics 监视器,流结束时在【绑定的
        # llm】上 emit "metrics_collected"(llm/llm.py:432)。本流只是「expr 标记块 +
        # 透传内芯」:若照基类 emit,会得到一份 TTFT≈0 的假 LLMMetrics(expr 标记块
        # 是首块、has_response()=True 直接掐表),且 usage 与内芯那份双计。真实
        # LLMMetrics 由内芯(MlxLlmLLM)发出、经 _bind_metrics_forward 逐层转发,
        # 这里只排空监视分支(tee 的另一个 peer 不排空会白积 buffer),不 emit。
        async for _ in event_aiter:
            pass

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

    def __init__(self, account_id: str = "", max_snippets: int = 2, max_summary_chars: int = 1200):
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
        elif key == "cantonese":
            self._user_lang = "cantonese"
        elif key in {"english", "en"}:
            self._user_lang = "en"
        else:
            self._user_lang = key

    def set_knowledge(self, snippets: list[dict]) -> None:
        seen: set[str] = set()
        out: list[str] = []
        for s in snippets or []:
            text = str(s.get("text", "") or "").strip()
            # 单条截断：防超大文档整段进 system（单条无限长会撑爆每轮 prefill）。
            # 150 字≈尾部预算目标 ≤~120 token 的一条配额（瘦砍自 350：尾部整段
            # 每轮重 prefill，是 TTFT 大头）。
            if len(text) > 150:
                text = text[:149] + "…"
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
        """完整 system 段（兼容旧调用/测试）：稳定指令前缀 + 易变参考尾部。"""
        prefix = self.render_instruction_prefix()
        tail = self.render_context_tail()
        if prefix and tail:
            return f"{prefix}\n\n{tail}"
        return prefix or tail

    def render_instruction_prefix(self) -> str:
        """【稳定指令前缀】——放最前、紧贴人设 base。

        含：用户语言规则 / 回复节奏 / 应答准则 / 话术流程总览(整通不变)。
        不变量：前缀整场字节不变（步骤推进只改尾部）→ mlx KV-cache 整场命中；
        当前步约束已移到尾部（推进若改前缀,token0 起整段重 prefill,实测卡 3-5s）。
        真正每轮变的当前步/检索资料/记忆都放 render_context_tail()。
        """
        parts: list[str] = []
        if self._user_lang:
            names = {"zh": "普通话/中文", "cantonese": "粤语（广东话）", "en": "英语"}
            name = names.get(self._user_lang, self._user_lang)
            if self._user_lang == "cantonese":
                rule = self._cantonese_rule()
            elif self._user_lang == "en":
                rule = "Reply in natural spoken English only (like on a phone call); do not explain or add notes."
            else:
                rule = self._zh_rule()
            parts.append(f"【用户语言】当前用户正在使用：{name}。{rule}")
        # 语音通话的节奏约束：客服回复要短、口语、像打电话——一次只推进一件事，
        # 说太长会把客户堵住、也拖慢每轮。这是"通话感"的关键。配合「句式短、句号收尾」，
        # TTS 才能在首句生成完就出声（overlap），而不是等整段吐完。
        parts.append(
            "【回复节奏】这是语音通话，一次回复要简短口语，像真人打电话：最多 2~3 句，"
            "每句尽量短（一句话 20 字内更佳），不要一次把所有信息/方案/步骤都讲完；"
            "先讲结论，再补一句必要解释，句与句之间自然停顿，句尾用句号或问号收住。"
            "讲完当前要点就停下把话交回客户，等客户回应再继续下一步。"
        )
        # 客服应答准则：永不主动说"不知道/查不到"，知识不够时用客服话术兜住。
        # 这是客服与聊天机器人的本质区别——客户要的是被接住，不是被拒绝。
        parts.append(
            "【应答准则】你是客服，绝不能说「不知道」「查不到」「不清楚」「没这个资料」「我帮不了你」。"
            "资料/知识不够回答时，用客服的方式接住客户："
            "① 先给确定能给的（安抚、已确认信息、下一步动作）；"
            "② 需要查证/转办的，明确告诉客户你会跟进处理，或转给能处理的人/专员跟进；"
            "但「帮你核实/查一下再覆你」只适用于真係要查外部资料嘅情况——如果你正按話術流程步"
            "推进(例如要向客户讲赔偿方案/引导办理)，就直接照当前步讲，绝唔好用「等我查下/幾分鐘內覆你」"
            "呢類拖延话术，亦唔好喺流程中途自把自为承诺返覆；"
            "③ 客户的问题超出当前业务，就用引导话术收住（如「呢单我帮你转俾专门跟进嘅同事，佢会即刻同你联系」），绝不冷场、绝不空手。"
        )
        if self._flow_overview:
            parts.append("【话术流程总览(别照读,按进度推进)】\n" + self._flow_overview)
        return "\n\n".join(parts)

    def render_context_tail(self) -> str:
        """【易变参考尾部】——每轮变的当前步/检索资料/记忆，垫在 system 最末。

        前缀(稳定指令+话术总览)+人设 base 在前且整场字节不变，flow 步骤推进
        只改这段尾部（短、逐轮重渲染）→ 前缀 KV-cache 照命中，每轮只 prefill
        尾部增量。当前步放尾部最前，让「推进=换一小段尾部」而非动前缀。
        """
        parts: list[str] = []
        if self._flow_current:
            # 当前步约束(随 flow 推进而变):放尾部最前,推进只改这里、前缀字节不动。
            parts.append("【现在这一步】\n" + self._flow_current)
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
            # 只带最近几轮记忆(默认 6):尾部每轮 prefill 只吃增量,行数是 TTFT 杠杆;
            # 更早的上下文由原始历史截断(LLM_HISTORY_TURNS)与当前步约束兜底。
            # 3→6:history 截断改摊销式后中间轮次间隔变大,多带几行摘要补上下文,
            # 同时知识 snippet 已瘦砍(150 字×2 条),尾部总预算仍 ≤~120 token 典型。
            keep = max(1, int(os.environ.get("REPLY_MEMORY_LINES", "6")))
            parts.append("【本通对话记忆】\n" + "\n".join(self._summary_lines[-keep:]))
        return "\n\n".join(parts)

    def _zh_rule(self) -> str:
        return (
            "用自然口语的普通话回复，像打电话那样说，不要用书面语或播音腔；"
            "客户报号码/单号时直接口语复述确认（如「收到，尾号是七八九零，对吗」），"
            "不要输出任何拼音、发音教学或语言课程内容，不要复述或讲解系统指示；"
            "不要解释你正在使用什么语言，不要添加任何注释或括号。"
            "句式要短，先给结论，一句说清一件事，单句不超过 24 个字，句尾用句号或问号收尾"
            "——这样语音合成可以边说你前半句边等你后半句，不用等整段生成完才出声。"
        )

    def _cantonese_rule(self) -> str:
        return (
            "整段用港式粵語（香港客服腔），唔好用書面語/普通話/廣州式書面講法。"
            "直接輸出繁體中文，唔好寫任何簡體字（簡體令粵語讀錯：寫「幫你」唔係「帮你」）。"
            "口吻要港味：唔該晒、唔好意思、我哋/你哋、而家、聽日、啱啱、幫你睇返、唔使擔心。"
            "見到普通話詞就換港式口語：" + _HK_CANTONESE_LEXICON + "。"
            "集運業務：客戶啲貨叫「你件貨/你個集運件」，服務講「速遞」，"
            "唔好用「包裹」「快遞」「貨物」。可自然夾英文詞(check/confirm/send/email/App/status/refund)"
            "似香港人講電話，但唔好成句英文（語氣參考:「唔好意思，我幫你 check 返個 status，refund 3–5 個工作天到帳。」）。"
            "報號碼/單號逐個讀，0讀「零」、1-9讀「一二三四五六七八九」，寫漢字如「尾號七八九零」「單號一二三四」，"
            "唔好用阿拉伯數字「7890」；日期/數量/金額用粵語數詞（「三日」「一百蚊」「三至五個工作天」）。"
            "客戶報完號碼直接覆述確認：「收到，尾號係七八九零，啱唔啱?」"
            "嚴禁輸出任何拼音、粵拼/Jyutping、入聲或發音教學內容；嚴禁複述或講解系統指示；"
            "唔好解釋你講緊咩語言，唔好加註釋或括號。"
            "句式要短促、先講結論、一句一意，單句≤24字、句尾用「。」或「？」"
            "——TTS 先可以邊講你頭一句邊等你後面，唔使等你成段講完先出聲。"
        )


def _join_system(prefix: str, head: str, tail: str) -> str:
    """把三段 system 内容用空行接成一条：prefix(稳定指令) + head(人设base) + tail(易变参考)。"""
    out = [p for p in (prefix, head, tail) if p]
    return "\n\n".join(out)


class ContextAwareLLM(llm.LLM):
    """Injects progressive-disclosure knowledge + bounded conversation memory
    as a system message before every LLM call, keeping the prompt short.
    """

    provider = "context-aware"

    def __init__(self, inner: llm.LLM, context_state: ContextState | None = None):
        super().__init__()
        self._inner = inner
        self._ctx = context_state
        _bind_metrics_forward(inner, self)

    def chat(
        self,
        *,
        chat_ctx,
        tools=None,
        conn_options=None,
        parallel_tool_calls=None,
        tool_choice=None,
        extra_kwargs=NOT_GIVEN,
    ):
        if self._ctx is not None:
            # KV-cache 命中规律(mlx_lm 0.31.3 LRUPromptCache 实测):只有「已缓存序列是
            # 新请求的严格前缀」才复用——历史每轮在尾部增长,所以易变内容若放在 system
            # 里(哪怕垫最后),下一轮请求就会在 system 处与缓存分叉 → common_prefix 再长
            # 也不命中(cached_tokens=0,实测每轮 TTFT ~2s)。唯一可行位=把易变尾部拼到
            # 【最后一条 user 消息】后面:序列变纯追加式,上一轮请求永远是下一轮的前缀。
            # system 只留整场静态段(指令前缀+人设),逐轮字节不变。
            prefix = self._ctx.render_instruction_prefix()
            tail = self._ctx.render_context_tail()
            if prefix or tail:
                copy = chat_ctx.copy()
                items = list(copy.items)
                if items and isinstance(items[0], llm.ChatMessage) and items[0].role == "system":
                    head = items[0].content
                    if isinstance(head, str):
                        merged = _join_system(prefix, head, "")
                    else:
                        merged = [*([prefix] if prefix else []), *head]
                    items[0] = llm.ChatMessage(role="system", content=merged)
                else:
                    items.insert(0, llm.ChatMessage(role="system", content=_join_system(prefix, "", "")))
                # 易变尾部(当前步/知识/记忆)拼到最后一条 user 消息尾部(仅请求副本,
                # 不落库——下一轮 chat_ctx 仍由框架持久历史 + 重新渲染组装)。
                if tail:
                    for j in range(len(items) - 1, -1, -1):
                        if getattr(items[j], "role", "") == "user":
                            content = items[j].content
                            if isinstance(content, str):
                                new_content = f"{content}\n\n{tail}" if content else tail
                            else:
                                new_content = [*content, tail]
                            items[j] = llm.ChatMessage(role="user", content=new_content)
                            break
                # 截断历史(摊销式,见 _truncate_chat_items):超过 2×N 对才剪回 N 对
                # (默认 8 对),更早的靠「本通对话记忆」摘要兜底。每轮全量历史会让
                # prefill 随轮次线性变慢(实测 12 轮 TTFT 2.4s);滞回让截断之间保持
                # 纯追加(KV-cache 命中),上下文仍有上界。4→8:摊销后第 1-16 轮纯
                # 追加全缓存命中,单会话记忆(客户地址/单号/诉求)留原文更久。
                max_turns = int(os.environ.get("LLM_HISTORY_TURNS", "8"))
                items = _truncate_chat_items(items, max_turns=max_turns)
                copy.items = items
                chat_ctx = copy
        return self._inner.chat(
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            extra_kwargs=_forward_extra_kwargs(extra_kwargs),
        )


def _truncate_chat_items(items: list, max_turns: int = 4) -> list:
    """保留开头 system(s) + 对话历史;摊销式截断（滞回）。

    旧实现:dialog 一超过 max_turns 对就【每轮】截到 max_turns 对——截断动了序列
    头部,mlx KV-cache(只认严格前缀)每轮重新锚定,省下的 prefill 全赔回去。
    现在:dialog 涨到 2×max_turns 对才动手、一次剪回 max_turns 对——之后 max_turns
    轮内纯追加(缓存逐轮命中),每 max_turns 轮才重锚一次。更早的信息由
    ContextState「本通对话记忆」摘要承担,剪掉不丢上下文。
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
    # 滞回:超过 2×max_turns 对(4×max_turns 条)才截,剪回 max_turns 对。
    if len(dialog) <= max_turns * 4:
        return items
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
        _bind_metrics_forward(inner, self)

    def chat(self, *, chat_ctx, tools=None, conn_options=None, parallel_tool_calls=None, tool_choice=None, extra_kwargs=NOT_GIVEN):
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
            extra_kwargs=_forward_extra_kwargs(extra_kwargs),
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
    """Local SenseVoice ASR via sherpa-onnx (zh/en/ja/ko + Cantonese)."""

    model = "sherpa-sense-voice"
    provider = "sherpa-onnx"

    def __init__(self, model_dir=None, language_state: LanguageState | None = None):
        import os

        import sherpa_onnx

        model_dir = model_dir or os.environ.get("SHERPA_MODEL_DIR", "data/models/sherpa-onnx-sense-voice")
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
                # 供应商标签 YUE 是外部字面量；内部统一归一到规范值 cantonese。
                self._stt_.last_language = {"ZH": "zh", "EN": "en", "YUE": "cantonese"}[tag]
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
    _ENDPOINT_WS_CN = "wss://api.minimax.cn/ws/v1/t2a_v2"
    _ENDPOINT_WS_INTL = "wss://api.minimax.chat/ws/v1/t2a_v2"

    def _ws_voice_setting(self, voice: str) -> dict:
        """任务级 voice_setting（三条合成路径共用同一构造,避免漏 emotion/pitch）。"""
        return {
            "voice_id": voice,
            "speed": float(os.environ.get("MINIMAX_SPEED", "1")),
            "vol": float(os.environ.get("MINIMAX_VOL", "1")),
            "pitch": int(os.environ.get("MINIMAX_PITCH", "0")),
            "emotion": self._resolve_emotion(),
        }

    def __init__(
        self,
        *,
        voice: str | dict = "",
        language_state: LanguageState | None = None,
        sample_rate: int = 24000,
        api_key: str = "",
        emotion_state=None,
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
        self._emotion_state = emotion_state

    def _resolve_emotion(self) -> str:
        """当前轮 mood → MiniMax emotion（task_start 整段一个情绪）。

        之前没接 emotion → 全程 calm，听感很"平"。默认开；MINIMAX_EMOTION=0 可关。
        """
        if os.environ.get("MINIMAX_EMOTION", "1") != "1":
            return "calm"
        if self._emotion_state is not None:
            try:
                return self._emotion_state.minimax_emotion()
            except Exception:  # pragma: no cover - 情绪解析失败回落 calm
                pass
        return "calm"

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
        return self._ENDPOINT_WS_INTL if region in {"intl", "global", "chat"} else self._ENDPOINT_WS_CN

    def _model(self) -> str:
        return os.environ.get("MINIMAX_MODEL", "speech-2.8-hd")

    def _language_boost(self) -> str:
        """目标语 language_boost(env 注入,B 线同传按 target_lang 钉死;空=不下发)。

        枚举值由 interpret 侧写入 MINIMAX_LANGUAGE_BOOST,这里只透传——
        A 线没设该 env,请求里就完全不带这个键,行为零变化。
        """
        return os.environ.get("MINIMAX_LANGUAGE_BOOST", "").strip()

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
        # 整段齐晒先落 stream——喺度套教学形拦截最稳(逐句流式只喺 send 前拦)。
        text = lecture_guard(str(text), self._speech_lang())
        return _MiniMaxTTSStream(self, text, conn_options or APIConnectOptions())

    def _speech_lang(self) -> str | None:
        """罐头回应的语言:会话锚定语言(zh/cantonese)优先,其它(如 en)留 None 自动判。"""
        lang = self._language_state.lang
        return lang if lang in ("zh", "cantonese") else None

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
        # 教学形拦截已触发过就唔再重复播罐头(同段后续课程句静默丢弃)。
        self._lecture_fired = False

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

    async def _run(self, output_emitter):
        import websockets

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
            print(f"MINIMAX_TTS_VOICE {voice} lang={self._tts_._language_state.lang}", flush=True)
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
            # 连唔到 WS 冇音频可推(livekit 会 APIError no audio frames → 静音吞回复)。
            # 呢个 streaming 路径唔像 ChunkedStream 咁有 HTTP 回退——播一声 beep
            # 令客户知 AI 有反应过,至少唔係无差别静音。
            print("MINIMAX_TTS_WS_CONNECT", repr(exc), flush=True)
            try:
                await self._emit_beep(output_emitter)
            except Exception:  # pragma: no cover
                pass
            return

        init_done = False
        frame_bytes = int(sample_rate / 5) * 2  # 200ms 帧
        buf = bytearray()
        recv_task: asyncio.Task | None = None
        t_start = time.monotonic()
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
                    "emotion": self._tts_._resolve_emotion(),
                },
                "audio_setting": {"sample_rate": sample_rate, "format": "pcm", "channel": 1},
                # 官方参数:流式不回传聚合音频,显著降尾包体积与传输耗时。
                "stream_options": {"exclude_aggregated_audio": True},
            }
            # language_boost 锁语种(B 线同传按目标语注入 env):源语音常夹第三方
            # 词,显式锁死防合成语种漂移;枚举值是 MiniMax API 外部字面量。
            boost = self._tts_._language_boost()
            if boost:
                start["language_boost"] = boost
            await ws.send(json.dumps(start))
            try:
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if resp.get("event") != "task_started":
                    print("MINIMAX_TTS_WS_START_FAIL", str(resp)[:200], flush=True)
                    try:
                        await self._emit_beep(output_emitter)
                    except Exception:  # pragma: no cover
                        pass
                    return
            except Exception as exc:
                print("MINIMAX_TTS_WS_START", repr(exc), flush=True)
                try:
                    await self._emit_beep(output_emitter)
                except Exception:  # pragma: no cover
                    pass
                return

            async def _recv_loop():
                nonlocal init_done, buf
                first_pushed = False
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
                                request_id=utils.shortuuid(),
                                sample_rate=sample_rate,
                                num_channels=self._tts_.num_channels,
                                mime_type="audio/pcm",
                                stream=True,
                            )
                            output_emitter.start_segment(segment_id=utils.shortuuid())
                            init_done = True
                        buf.extend(chunk)
                        if not first_pushed and len(buf) >= frame_bytes // 5:
                            # P0 首帧早推:不足 200ms 先推 ~40ms,早出声(照 Qwen3-TTS 同款)。
                            # P0 秒表:首个音频块推送时刻(距 task_start)。
                            print(
                                f"TTS_FIRST_AUDIO_MS {(time.monotonic() - t_start) * 1000:.0f}",
                                flush=True,
                            )
                            output_emitter.push(bytes(buf))
                            output_emitter.flush()
                            buf.clear()
                            first_pushed = True
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

            # 增量合成:文本按边界切分逐段 task_continue。实测语义:
            # - 发累积全文会重复合成前面句子(长回复下明显重读);
            # - 纯逐块增量(不按句)在快速 send 下丢音频(服务端要等足文本才合成)。
            # 按句切分 + 连续发送 = 首句到就出声(低延迟)且不重复(每句只发一次)。
            # MINIMAX_TTS_OVERLAP=1(默认):句号之间也按「≥N 字 / 标点停顿 / ≥T ms」增量提前送,
            # 让 MiniMax 在 LLM 整句写完前先出前半句音频;连续数字/字母串不切开(防单号腰斩读错)。
            # 音频按序回流,recv_loop 持续推给 emitter,无需句间等待。
            _SENT_END = "。！？!?"
            _SOFT_BREAK = "，、；;：:"
            sent_buf = ""
            sent_any = False
            try:
                overlap_on = os.environ.get("MINIMAX_TTS_OVERLAP", "1") == "1"
            except Exception:  # pragma: no cover
                overlap_on = True
            try:
                _overlap_chars = int(os.environ.get("MINIMAX_TTS_OVERLAP_CHARS", "12"))
            except Exception:  # pragma: no cover
                _overlap_chars = 12
            try:
                _overlap_ms = int(os.environ.get("MINIMAX_TTS_OVERLAP_MS", "300"))
            except Exception:  # pragma: no cover
                _overlap_ms = 300
            _last_send = time.monotonic()

            def _flushable(s: str) -> bool:
                """overlap 增量可否送出：不能把连续的号码/数字串拦腰截断。

                MiniMax 对 task_continue 会拼接增量后按整句语义合成，但把一串
                「七八九零」切成「七八」+「九零」可能在拼接边界出现停顿/重读，
                故遇结尾是非空格连续数字/字母的串要等它收尾再送。
                """
                if not s:
                    return False
                tail = s.rstrip("。！？!?，、；;：: \t")
                # 只拦 latin/数字结尾(词/号码可能被拦腰截断)。唔可以用裸 isalpha():
                # CJK 汉字 isalpha()==True → 中文片段全被拦,overlap 对中文全死。
                return not (tail and tail[-1].isascii() and tail[-1].isalnum())

            async def _send_text(s: str) -> None:
                nonlocal sent_any, _last_send
                if not self._lecture_fired and is_lecture_text(s):
                    # 开场即教学 → 播一次罐头的「请再报单号」,唔好照读课程;
                    # 若前面已出过正常音频,课程句静默丢弃,唔追加罐头(避免二重声)。
                    self._lecture_fired = True
                    if not sent_any:
                        await ws.send(
                            json.dumps({"event": "task_continue", "text": lecture_canned(self._tts_._speech_lang())})
                        )
                        sent_any = True
                    return
                if is_lecture_text(s):
                    return  # 已触发过,课程延续句照丢
                await ws.send(json.dumps({"event": "task_continue", "text": s}))
                sent_any = True
                _last_send = time.monotonic()

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
                        await _send_text(sentence.strip())
                # overlap:句号之间的增量,满足「≥N 字且有软停顿/距上次够久」就提前送。
                if (
                    overlap_on
                    and not self._lecture_fired
                    and sent_buf.strip()
                    and len(sent_buf.strip()) >= _overlap_chars
                ):
                    soft_idx = -1
                    for ch in _SOFT_BREAK:
                        pos = sent_buf.rfind(ch)
                        if pos != -1:
                            soft_idx = max(soft_idx, pos)
                    now = time.monotonic()
                    time_up = (now - _last_send) * 1000 >= _overlap_ms
                    if (soft_idx != -1 and soft_idx >= len(sent_buf.strip()) // 2) or time_up:
                        frag = sent_buf.strip()
                        if _flushable(frag):
                            await _send_text(frag)
                            sent_buf = ""
                if self._lecture_fired:
                    sent_buf = ""  # 已触发 → 清掉未分句的课程尾部
            if sent_buf.strip():
                if not self._lecture_fired and is_lecture_text(sent_buf.strip()):
                    self._lecture_fired = True
                    if not sent_any:
                        await ws.send(
                            json.dumps({"event": "task_continue", "text": lecture_canned(self._tts_._speech_lang())})
                        )
                elif not self._lecture_fired:
                    await ws.send(json.dumps({"event": "task_continue", "text": _inject_pauses(sent_buf.strip())}))
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
            print(f"MINIMAX_TTS_VOICE {voice} lang={self._tts_._language_state.lang}", flush=True)
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
                "model": self._tts_._model(),
                "voice_setting": {
                    "voice_id": voice,
                    "speed": float(os.environ.get("MINIMAX_SPEED", "1")),
                    "vol": float(os.environ.get("MINIMAX_VOL", "1")),
                    "pitch": int(os.environ.get("MINIMAX_PITCH", "0")),
                    "emotion": self._tts_._resolve_emotion(),
                },
                "audio_setting": {"sample_rate": sample_rate, "format": "pcm", "channel": 1},
                # 官方参数:流式不回传聚合音频,显著降尾包体积与传输耗时。
                "stream_options": {"exclude_aggregated_audio": True},
            }
            # language_boost 锁语种(B 线同传按目标语注入 env):源语音常夹第三方
            # 词,显式锁死防合成语种漂移;枚举值是 MiniMax API 外部字面量。
            boost = self._tts_._language_boost()
            if boost:
                start["language_boost"] = boost
            await ws.send(json.dumps(start))
            try:
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if resp.get("event") != "task_started":
                    print("MINIMAX_TTS_WS_START_FAIL", str(resp)[:200], flush=True)
                    return False
            except Exception as exc:
                print("MINIMAX_TTS_WS_START", repr(exc), flush=True)
                return False
            await ws.send(json.dumps({"event": "task_continue", "text": _inject_pauses(self._text)}))
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
                            request_id=utils.shortuuid(),
                            sample_rate=sample_rate,
                            num_channels=self._tts_.num_channels,
                            mime_type="audio/pcm",
                            stream=True,
                        )
                        output_emitter.start_segment(segment_id=utils.shortuuid())
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
                    payload = {
                        "model": self._tts_._model(),
                        "text": _inject_pauses(self._text),
                        "voice_setting": {
                            "voice_id": voice,
                            "speed": float(os.environ.get("MINIMAX_SPEED", "1")),
                            "vol": float(os.environ.get("MINIMAX_VOL", "1")),
                            "pitch": int(os.environ.get("MINIMAX_PITCH", "0")),
                            "emotion": self._tts_._resolve_emotion(),
                        },
                        "audio_setting": {"sample_rate": sample_rate, "format": "pcm", "channel": 1},
                    }
                    # language_boost 与 WS 路径同源(env 注入,空则完全不带该键)。
                    boost = self._tts_._language_boost()
                    if boost:
                        payload["language_boost"] = boost
                    resp = await client.post(
                        endpoint,
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json=payload,
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    data = body.get("data") or {}
                    audio_hex = data.get("audio") or ""
                    if not audio_hex:
                        raise RuntimeError(f"minimax empty audio: {body.get('base_resp')}")
                    pcm = bytes.fromhex(audio_hex)
                    output_emitter.initialize(
                        request_id=utils.shortuuid(),
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
            # 真流式（对齐 MiniMaxTTS）：sidecar /v1/audio/speech 本身就按 chunk_ms
            # 流式回 PCM（非 StreamAdapter 模拟）。声明 streaming=True 后 voice 管线
            # 直接调 stream() 把 LLM 增量文本喂进来,不再被 StreamAdapter +
            # blingfire SentenceTokenizer 包（那要等整句边界,中文切句不可靠,
            # 实测多等 150-790ms 才有第一段文本可合成）。分句/增量节奏由
            # _Qwen3SynthesizeStream 自己掌（一任务一句,POST 流式回帧）。
            capabilities=tts.TTSCapabilities(streaming=True, aligned_transcript=False),
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

    def stream(self, *, conn_options=None):
        """真流式 SynthesizeStream：LLM 文本增量到达,按句切任务 POST sidecar。"""
        return _Qwen3SynthesizeStream(self, conn_options or APIConnectOptions())

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


async def _qwen3_tts_post_frames(
    tts_: "Qwen3TTSTTS",
    text: str,
    output_emitter,
    state: dict,
    *,
    end_segment: bool = True,
) -> bool:
    """单个合成任务：POST 一段文本到 qwen3-tts sidecar,把 PCM 流切成 200ms 帧推给
    emitter(首个 ~40ms 早推,与旧 _Qwen3TTSStream 同款节奏)。

    state={"started": bool} 跨任务共享——多段流式共用同一个 emitter 初始化与
    segment,只在首段 initialize。voice/instruct/emotion 每任务即时解析
    (情绪变化逐段生效,等价 MiniMax 的 task_start 语义)。
    end_segment=False 时由调用方(流式路径)在整场文本结束后统一收尾。
    返回是否成功推出音频。
    """
    last_exc: Exception | None = None
    pushed_any = False  # 本任务是否已有音频落地(半途断流后重试会重读音频)
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{tts_._base_url}/v1/audio/speech",
                    json={
                        "input": text,
                        "voice": tts_._resolve_voice(),
                        "language": tts_._language_state.lang,
                        "instruct": tts_._resolve_instruct(),
                        "sample_rate": tts_.sample_rate,
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
                frame_bytes = (tts_.sample_rate // 5) * 2  # 200ms, 16-bit mono
                buf = bytearray()
                first_audio = False
                if not state["started"]:
                    output_emitter.initialize(
                        request_id="qwen3-tts",
                        sample_rate=tts_.sample_rate,
                        num_channels=tts_.num_channels,
                        mime_type="audio/pcm",
                        stream=True,
                    )
                    state["started"] = True
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
                        pushed_any = True
                    while len(buf) >= frame_bytes:
                        output_emitter.push(bytes(buf[:frame_bytes]))
                        output_emitter.flush()
                        del buf[:frame_bytes]
                        pcm_total += frame_bytes
                        pushed_any = True
                if buf:
                    output_emitter.push(bytes(buf))
                    output_emitter.flush()
                    pcm_total += len(buf)
                    pushed_any = True
                if end_segment:
                    output_emitter.end_segment()
                print("QWEN3_TTS_BYTES", pcm_total, flush=True)
                return True
        except Exception as exc:  # noqa: BLE001 - retry transient gateway failures
            last_exc = exc
            if pushed_any:
                # 半途断流:本句已有音频推进 emitter,重 POST 会从 byte 0 重推同句
                # (听感重读)。有音频落地就唔重试,交上层决定收尾姿势。
                print("QWEN3_TTS_MIDSTREAM_ABORT", attempt + 1, repr(exc), flush=True)
                break
            print("QWEN3_TTS_RETRY", attempt + 1, repr(exc), flush=True)
            await asyncio.sleep(0.5 * (attempt + 1))
    print("QWEN3_TTS_ERROR", repr(last_exc), flush=True)
    return False


async def _qwen3_tts_beep(tts_, output_emitter):
    """故障蜂鸣。真 AudioEmitter.push 喺未 initialize 时会抛
    "AudioEmitter isn't started"（tts.py:900），上层 except:pass 静默吞掉 →
    客户面对无差别静音。所以 beep 自己先 initialize+start_segment。
    调用方约定：只喺「本场零音频」（state["started"]=False）时调用，唔会重初始化。
    """
    output_emitter.initialize(
        request_id="qwen3-tts-beep",
        sample_rate=tts_.sample_rate,
        num_channels=tts_.num_channels,
        mime_type="audio/pcm",
        stream=True,
    )
    output_emitter.start_segment(segment_id="qwen3-tts-beep")
    import math

    sr = tts_.sample_rate
    n = int(sr * 0.4)
    pcm = bytearray()
    for i in range(n):
        v = int(12000 * math.sin(2 * math.pi * 440 * i / sr))
        pcm += v.to_bytes(2, "little", signed=True)
    output_emitter.push(bytes(pcm))
    output_emitter.flush()
    output_emitter.end_segment()


class _Qwen3TTSStream(tts.ChunkedStream):
    """整段合成（synthesize 兼容路径）：一段文本一个任务,POST 流式回帧。"""

    def __init__(self, tts_, text, conn_options):
        super().__init__(tts=tts_, input_text=text, conn_options=conn_options)
        self._text = text
        self._tts_ = tts_

    async def _run(self, output_emitter):
        # emitter 是否已 initialize：未收到响应就被打断/挂断时 emitter 从未启动，
        # 此时 flush 会抛 "AudioEmitter isn't started"（误报 TTS 断链）。只在已启动后收尾。
        state = {"started": False}
        try:
            ok = await _qwen3_tts_post_frames(self._tts_, self._text, output_emitter, state)
            # beep 只畀「全场零音频」:音频已出过再失败,补 beep 会叠喺已播内容后面。
            if not ok and not state["started"] and not asyncio.current_task().cancelling():
                await self._emit_beep(output_emitter)
        except asyncio.CancelledError:
            # 会话关闭/打断时不播放故障蜂鸣，直接收尾。
            raise
        except Exception as exc:
            print("QWEN3_TTS_FATAL", repr(exc), flush=True)
            await self._emit_beep(output_emitter)
        finally:
            if state["started"]:
                try:
                    output_emitter.flush()
                except Exception:  # pragma: no cover - 收尾失败不影响主流程
                    pass

    async def _emit_beep(self, output_emitter):
        await _qwen3_tts_beep(self._tts_, output_emitter)


class _Qwen3SynthesizeStream(tts.SynthesizeStream):
    """Qwen3-TTS 增量流式（镜像 _MiniMaxSynthesizeStream 结构）。

    LLM 文本增量 push 进来后按句切任务,一任务一次 HTTP POST(与 synthesize()
    同一端点同一 JSON),sidecar 边合成边流 PCM,这里收一段推一段。首段音频
    在第一个句号就开推,唔再等 SentenceTokenizer 凑整句/全文。
    - voice/instruct/emotion 每任务经 _resolve_voice/_resolve_instruct 即时解析。
    - overlap:句号之间的增量满足「≥N 字且有软停顿/距上次够久」提前送
      (QWEN3_TTS_OVERLAP,默认开);连续数字/字母串唔切开(防单号腰斩)。
    - 任一任务三次重试都失败 → 置 broken 停止后续任务(唔逐句白等 3 轮);
      全场一字未出(emitter 未启动)则补一声 beep,唔畀客户面对无差别静音。
    """

    def __init__(self, tts_: "Qwen3TTSTTS", conn_options):
        super().__init__(tts=tts_, conn_options=conn_options)
        self._tts_ = tts_

    async def _run(self, output_emitter):
        state = {"started": False}
        broken = False
        pushed_any = False
        try:
            _SENT_END = "。！？!?"
            _SOFT_BREAK = "，、；;：:"
            try:
                overlap_on = os.environ.get("QWEN3_TTS_OVERLAP", "1") == "1"
            except Exception:  # pragma: no cover
                overlap_on = True
            try:
                _overlap_chars = int(os.environ.get("QWEN3_TTS_OVERLAP_CHARS", "12"))
            except Exception:  # pragma: no cover
                _overlap_chars = 12
            try:
                _overlap_ms = int(os.environ.get("QWEN3_TTS_OVERLAP_MS", "300"))
            except Exception:  # pragma: no cover
                _overlap_ms = 300
            _last_send = time.monotonic()

            def _flushable(s: str) -> bool:
                """overlap 增量可否送出：不能把连续的号码/数字串拦腰截断。"""
                if not s:
                    return False
                tail = s.rstrip("。！？!?，、；;：: \t")
                # 只拦 latin/数字结尾(词/号码可能被拦腰截断)。唔可以用裸 isalpha():
                # CJK 汉字 isalpha()==True → 中文片段全被拦,overlap 对中文全死。
                return not (tail and tail[-1].isascii() and tail[-1].isalnum())

            sent_buf = ""
            async for item in self._input_ch:
                if isinstance(item, self._FlushSentinel):
                    continue
                text = str(item or "")
                if not text.strip():
                    continue
                pushed_any = True
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
                        ok = await _qwen3_tts_post_frames(
                            self._tts_, sentence.strip(), output_emitter, state,
                            end_segment=False,
                        )
                        _last_send = time.monotonic()
                        if not ok:
                            broken = True
                            break
                if broken:
                    break
                # overlap:句号之间的增量提前送(与 MiniMax 同款节奏)。
                if (
                    overlap_on
                    and not broken
                    and sent_buf.strip()
                    and len(sent_buf.strip()) >= _overlap_chars
                ):
                    soft_idx = -1
                    for ch in _SOFT_BREAK:
                        pos = sent_buf.rfind(ch)
                        if pos != -1:
                            soft_idx = max(soft_idx, pos)
                    now = time.monotonic()
                    time_up = (now - _last_send) * 1000 >= _overlap_ms
                    if (soft_idx != -1 and soft_idx >= len(sent_buf.strip()) // 2) or time_up:
                        frag = sent_buf.strip()
                        if _flushable(frag):
                            ok = await _qwen3_tts_post_frames(
                                self._tts_, frag, output_emitter, state,
                                end_segment=False,
                            )
                            _last_send = time.monotonic()
                            if not ok:
                                broken = True
                                break
                            sent_buf = ""
            if not broken and sent_buf.strip():
                # 收尾残句:全场文本结束,把没凑够一句的尾巴合成掉。
                await _qwen3_tts_post_frames(
                    self._tts_, sent_buf.strip(), output_emitter, state,
                    end_segment=False,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("QWEN3_TTS_STREAM_FATAL", repr(exc), flush=True)
        finally:
            if state["started"]:
                try:
                    output_emitter.end_segment()
                except Exception:  # pragma: no cover - 收尾失败不影响主流程
                    pass
            elif pushed_any and not asyncio.current_task().cancelling():
                # 一段音频都冇出过(sidecar 挂了):beep 一下,至少证明 AI 有反应。
                try:
                    await _qwen3_tts_beep(self._tts_, output_emitter)
                except Exception:  # pragma: no cover
                    pass


# ASR 语言提示:值=模型 config support_languages 的规范名(mlx 层大小写不敏感匹配)。
# 通话模式(pin=False):cantonese 必钉(auto 会误判成普通话)、en 必钉(支持纯英语会话),
# zh 保持 auto 容忍夹英文 code-switching;同传模式(pin=True):源语言用户选定且固定,全钉。
_ASR_LANG_HINTS = {"cantonese": "Cantonese", "en": "English", "zh": "Chinese"}


def _asr_language_hint(lang_state: str, pin: bool) -> str:
    hint = _ASR_LANG_HINTS.get(lang_state, "")
    if not pin and hint == "Chinese":
        return ""
    return hint


class Qwen3ASRSTT(stt.STT):
    """LiveKit STT adapter for the local Qwen3-ASR sidecar."""

    model = "qwen3-asr"
    provider = "qwen3-asr"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8787",
        language_state: LanguageState | None = None,
        pin_language: bool = False,
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
        # True=语言钉死(同传:源语言是用户建房时选定的,zh/en/cantonese 都下发 hint);
        # False=通话模式(只有 cantonese 钉防误判,zh/en 交 auto 容忍夹语 code-switching)。
        self._pin_language = pin_language

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
        t0 = time.monotonic()
        # 语言提示:值=模型 config support_languages 的规范名(Chinese/English/Cantonese,
        # mlx 层大小写不敏感)。通话模式只有 cantonese/en 钉——cantonese 防 auto 误判成
        # 普通话(啱唔靈→难唔难),en 支持纯英语会话;zh 保持 auto 容忍夹英文
        # (「我哋 check 個 status」)。同传模式源语言固定,三种全钉。
        lang_hint = _asr_language_hint(self._stt_._language_state.lang, self._stt_._pin_language)
        print(f"QWEN3_ASR_HINT {lang_hint or 'auto'} lang_state={self._stt_._language_state.lang}", flush=True)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    params = {"session_id": ""} if lang_hint else None
                    start = await client.post(
                        f"{self._stt_._base_url}/api/start",
                        params={"language": lang_hint} if lang_hint else None,
                    )
                    start.raise_for_status()
                    session_id = start.json()["session_id"]
                    final = await client.post(
                        f"{self._stt_._base_url}/api/finish",
                        params={"session_id": session_id},
                        content=bytes(pcm),
                        headers={"Content-Type": "application/octet-stream"},
                    )
                    final.raise_for_status()
                    data = final.json()
                    text = str(data.get("text") or "")
                    lang = str(data.get("language") or "")
                    lang = _normalize_asr_language(lang, text)
                    print(
                        f"QWEN3_ASR_TEXT {repr(text[:120])} {lang} "
                        f"ASR_MS={(time.monotonic() - t0) * 1000:.0f}",
                        flush=True,
                    )
                    return text, lang
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry transient gateway failures
                last_exc = exc
                print("QWEN3_ASR_RETRY", attempt + 1, repr(exc), flush=True)
                await asyncio.sleep(0.5 * (attempt + 1))
        print("QWEN3_ASR_ERROR", repr(last_exc), flush=True)
        return "", ""


def _common_prefix(a: str, b: str) -> str:
    """两段文本的公共前缀（字符级）——滑窗 partial 的「稳定部分」判定。"""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return a[:i]


_ASR_PARTIAL_POST_MS = float(os.environ.get("QWEN3_ASR_CHUNK_MS", "300"))
# PREFLIGHT(抢跑)发射节流:稳定前缀比【上次发射】至少长 4 字才再发(首发仍须 ≥6 字)。
# 每个 PREFLIGHT 事件都吃一次框架抢跑预算(on_preemptive_generation count+1,
# max_retries 封顶;烧穿后 FINAL 到达只 cancel 不重建 → commit 从零生成,实测 +0.2-0.7s)。
# 长句滑窗每 ~300ms 一窗、逐字增长,旧「比 _stable 长 1 字就发」会把预算在说话中途
# 烧光;≥4 字(约一个词组)把 3s 句的发射从 3-5 次压到 1-2 次,FINAL 那拍预算必够。
_ASR_PREFLIGHT_MIN_GROWTH_CHARS = 4


class _Qwen3ASRLiveStream(stt.RecognizeStream):
    """VAD 骨架 + 滑窗 partial（官方 StreamAdapterWrapper 的 partial 增强版）。

    与官方 stt.StreamAdapter 同一套 VAD 两任务骨架（START/END_OF_SPEECH 事件、
    END 触发整句转写），增强：
    - 说话期间每 ~300ms 把增量 PCM 喂 sidecar /api/chunk（同一流式会话），回传
      滑窗 partial：全文 → INTERIM_TRANSCRIPT（前端实时字幕）；连续两窗一致的
      稳定前缀 → PREFLIGHT_TRANSCRIPT（1.7 官方抢跑生成专用事件，LLM 在客户
      停嘴前就 prefill，commit 校验通过直接复用，省 ~0.4-0.6s）。
    - END_OF_SPEECH 用 /api/finish 补传尾段、取整句高精度转写——WhatsApp 数字
      捕获/话术推进零降级；partial 的跳变被「稳定前缀」约束，唔会进最终稿。
    """

    def __init__(self, stt_, *, vad, conn_options):
        super().__init__(stt=stt_, conn_options=conn_options, sample_rate=16000)
        self._stt_ = stt_  # 内层 Qwen3ASRSTT（base_url/语言状态/钉定）
        self._vad = vad
        self._session_id: str | None = None
        self._pending = bytearray()  # 尚未 POST 给 sidecar 的增量 PCM
        self._last_partial = ""  # 上一窗全文（INTERIM 去重）
        self._prev_partial = ""  # 稳定前缀参照窗
        self._stable = ""  # 已发 PREFLIGHT 的最长稳定前缀
        self._last_post = 0.0
        self._finishing = False

    async def _run(self) -> None:
        vad_stream = self._vad.stream()

        async def _forward_input() -> None:
            """forward input to vad（与官方 StreamAdapter 一致）"""
            async for input in self._input_ch:
                if isinstance(input, self._FlushSentinel):
                    vad_stream.flush()
                    continue
                vad_stream.push_frame(input)
            vad_stream.end_input()

        async def _recognize() -> None:
            started = False
            async for event in vad_stream:
                if event.type == vad.VADEventType.START_OF_SPEECH:
                    started = True
                    self._event_ch.send_nowait(stt.SpeechEvent(stt.SpeechEventType.START_OF_SPEECH))
                    await self._start_session()
                elif event.type == vad.VADEventType.INFERENCE_DONE:
                    if not started or self._finishing:
                        continue
                    # 1.7 utils.merge_frames=rtc.combine_audio_frames:返回【单个】
                    # rtc.AudioFrame(不可迭代,官方 StreamAdapter 同款用法)。
                    self._pending.extend(bytes(utils.merge_frames(event.frames).data))
                    await self._maybe_partial()
                elif event.type == vad.VADEventType.END_OF_SPEECH:
                    if not started:
                        continue
                    self._finishing = True
                    speech_end_time = time.time() - event.silence_duration - event.inference_duration
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH, speech_end_time=speech_end_time)
                    )
                    text, lang = await self._finish_session()
                    started = False
                    self._finishing = False
                    self._reset()
                    if text:
                        self._stt_._language_state.update(lang, text)
                        self._event_ch.send_nowait(
                            stt.SpeechEvent(
                                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                                alternatives=[stt.SpeechData(language=self._stt_._language_state.lang, text=text)],
                                speech_end_time=speech_end_time,
                            )
                        )

        await asyncio.gather(_forward_input(), _recognize())

    async def _start_session(self) -> None:
        lang_hint = _asr_language_hint(self._stt_._language_state.lang, self._stt_._pin_language)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{self._stt_._base_url}/api/start",
                    params={"language": lang_hint} if lang_hint else None,
                )
                r.raise_for_status()
                self._session_id = r.json()["session_id"]
        except Exception as exc:  # noqa: BLE001 - 建会话失败 → 整句路径照样可用
            self._session_id = None
            print(f"QWEN3_ASR_PARTIAL start failed: {exc!r}", flush=True)

    async def _maybe_partial(self) -> None:
        now = time.monotonic()
        if not self._session_id or now - self._last_post < _ASR_PARTIAL_POST_MS:
            return
        if len(self._pending) < 16000 * 2 * 0.6:  # <0.6s 无转写价值
            return
        self._last_post = now
        pcm = bytes(self._pending)
        self._pending.clear()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{self._stt_._base_url}/api/chunk",
                    params={"session_id": self._session_id},
                    content=pcm,
                    headers={"Content-Type": "application/octet-stream"},
                )
                r.raise_for_status()
                data = r.json()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - partial 尽力而为,忙时/抖动静默跳过
            return
        text = str(data.get("text") or "")
        if not text or text == self._last_partial:
            return
        lang = _normalize_asr_language(str(data.get("language") or ""), text)
        self._last_partial = text
        # INTERIM：滑窗全文（可能跳变，只供展示，唔进历史/唔落库）
        self._event_ch.send_nowait(
            stt.SpeechEvent(
                type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                alternatives=[stt.SpeechData(language=lang, text=text)],
            )
        )
        # 稳定前缀：与上一窗的公共前缀，首发 ≥6 字；再发须比上次发射多 ≥
        # _ASR_PREFLIGHT_MIN_GROWTH_CHARS 字（长度增长节流，保框架抢跑预算给 FINAL）。
        common = _common_prefix(self._prev_partial, text)
        self._prev_partial = text
        if (
            len(common) >= 6
            and len(common) - len(self._stable) >= _ASR_PREFLIGHT_MIN_GROWTH_CHARS
        ):
            self._stable = common
            self._event_ch.send_nowait(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.PREFLIGHT_TRANSCRIPT,
                    alternatives=[stt.SpeechData(language=lang, text=common)],
                )
            )
            print(f"QWEN3_ASR_PREFLIGHT chars={len(common)}", flush=True)

    async def _finish_session(self) -> tuple[str, str]:
        sid = self._session_id
        self._session_id = None
        tail = bytes(self._pending)
        self._pending.clear()
        if not sid:
            return "", ""
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{self._stt_._base_url}/api/finish",
                    params={"session_id": sid},
                    content=tail if tail else None,
                    headers={"Content-Type": "application/octet-stream"} if tail else None,
                )
                r.raise_for_status()
                data = r.json()
                text = str(data.get("text") or "")
                lang = _normalize_asr_language(str(data.get("language") or ""), text)
                print(
                    f"QWEN3_ASR_TEXT {repr(text[:120])} {lang} "
                    f"ASR_MS={(time.monotonic() - t0) * 1000:.0f}(stream)",
                    flush=True,
                )
                return text, lang
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print("QWEN3_ASR_FINISH_ERROR", repr(exc), flush=True)
            return "", ""

    def _reset(self) -> None:
        self._session_id = None
        self._pending.clear()
        self._last_partial = ""
        self._prev_partial = ""
        self._stable = ""
        self._last_post = 0.0


class Qwen3ASRLiveSTT(stt.STT):
    """「VAD + 滑窗 partial」的本地 ASR 包装（Qwen3-ASR 专用，替代官方 StreamAdapter）。

    recognize() 委托内层 Qwen3ASRSTT（保留官方重试/metrics）；stream() 返回带
    INTERIM/PREFLIGHT 的实时流。能力声明 streaming=True + interim_results=True。
    """

    def __init__(self, *, stt_: Qwen3ASRSTT, vad_):
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True,
                diarization=False,
                aligned_transcript=False,
                offline_recognize=stt_.capabilities.offline_recognize,
                keyterms=False,
                chat_context=False,
            )
        )
        self._vad = vad_
        self._stt = stt_
        stt_.on("metrics_collected", self._on_metrics_collected)

    @property
    def model(self) -> str:
        return self._stt.model

    @property
    def provider(self) -> str:
        return self._stt.provider

    def _on_metrics_collected(self, *args, **kwargs) -> None:
        self.emit("metrics_collected", *args, **kwargs)

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        return await self._stt.recognize(buffer=buffer, language=language, conn_options=conn_options)

    def stream(self, *, language=None, conn_options=None):
        return _Qwen3ASRLiveStream(self._stt, vad=self._vad, conn_options=conn_options or APIConnectOptions())
