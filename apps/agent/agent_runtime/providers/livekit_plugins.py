"""LiveKit-compatible provider plugins: OpenAI-compatible LLMs + offline fakes."""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import time
import unicodedata
import weakref
from dataclasses import dataclass

import httpx
from livekit.agents import (
    APIConnectOptions,
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    llm,
    stt,
    tts,
    utils,
    vad,
)
from livekit.plugins.openai import LLM as _OpenAICompatBase

from bok_voice_core.deepseek_llm import thinking_extra_body

# 后台任务强引用池(2026-09-17 全量 debug P2-A):事件循环对 task 只持弱引用,
# GC 可中途回收仍在跑的 fire-and-forget 任务——与本仓 _duration_fuse 注释、
# MiniMax 孤儿 invalidate、agent.py _SETTLE_TASKS 是同一实证 bug 类。本模块无
# entrypoint 闭包,用模块级 set + done-callback 自清;两处调用点(MiniMax 池
# 连接弃置 / ASR partial 调档)本就是 best-effort,只补引用零行为改动。
_BACKGROUND_TASKS: set = set()


def _spawn_bg(coro) -> None:
    """fire-and-forget 但不裸奔:入强引用池,done-callback 自清(防池无界增长)。"""
    task = asyncio.ensure_future(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


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

    采样档显式传参（temperature/top_p/top_k/repetition_penalty）优先，None 回落
    env 现状——调用方（B 线 MT 分支）显式传值时不再依赖写进程 env 下发（评审
    P2-3：env setdefault 会在同 worker 跨会话驻留，泄漏给回退主 LLM）。
    """

    provider = "mlx"

    def __init__(
        self,
        api_key="mlx",
        model=None,
        base_url="http://127.0.0.1:1235/v1",
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        repetition_penalty: float | None = None,
    ):
        # mlx_lm server requires the real model path in requests; "local" is
        # only a last-resort placeholder when no env/settings provide one.
        if model in (None, "", "local"):
            model = os.environ.get("MLX_LLM_MODEL") or "local"
        if model == "local":
            # 占位符发 server 会被当 repo-id 走 HF hub 解析,断网时持锁挂死整个
            # server——A 线正常装配恒解析出真实路径,落到占位符即装配配置异常。
            print(
                "[mlx-llm] WARNING: model placeholder 'local' — server may hang on HF resolution",
                flush=True,
            )
        extra_body = {
            "max_tokens": int(os.environ.get("LLM_MAX_TOKENS", "160")),
            # Qwen3 对话模板以 <|im_end|> 收尾:唔传 stop 个 server 会当文字输出
            # (转录/TTS 见住 <|im_end|>),喺源头截停最干净;下游再剥多一重保险。
            "stop": ["<|im_end|>", "<|im_start|>", "<|endoftext|>"],
        }
        # 定制采样(显式传参直接进 extra_body;None 回落 env,env 未设不进请求,
        # A 线默认路径零变化):B 线 MT 档要 top_p/top_k/重复惩罚收窄采样,防翻译
        # 小模型自由发挥/复读。top_k 收整数(mlx_lm server 按 int 校验),其余收浮点。
        for key, env_key, explicit in (
            ("top_p", "LLM_TOP_P", top_p),
            ("top_k", "LLM_TOP_K", top_k),
            ("repetition_penalty", "LLM_REPETITION_PENALTY", repetition_penalty),
        ):
            if explicit is not None:
                extra_body[key] = int(explicit) if key == "top_k" else float(explicit)
                continue
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
            temperature=float(
                temperature if temperature is not None else os.environ.get("LLM_TEMPERATURE", 0.35)
            ),
            extra_body=extra_body,
        )
        # BOK_LLM_MSG_DEBUG=1：逐请求消息指纹（sha1+长度+头尾片段），定位
        # 「缓存锚点后即分叉」是哪条消息每轮在变（W0 诊断工具，默认关）。
        if os.environ.get("BOK_LLM_MSG_DEBUG", "") == "1":
            import hashlib as _hashlib

            _client = self._client
            _raw_create = _client.chat.completions.create

            async def _create(**kw):
                msgs = kw.get("messages") or []
                tag = f"{id(kw):x}"[-6:]
                for i, m in enumerate(msgs):
                    c = m.get("content")
                    text = c if isinstance(c, str) else "".join(
                        x.get("text", "") for x in (c or []) if isinstance(x, dict)
                    )
                    fp = _hashlib.sha1(text.encode()).hexdigest()[:10]
                    print(
                        f"[llmmsg] req={tag} msg{i} role={m.get('role')} len={len(text)} "
                        f"sha={fp} head={text[:36]!r} tail={text[-36:]!r}",
                        flush=True,
                    )
                return await _raw_create(**kw)

            _client.chat.completions.create = _create

        # S5 排队定罪（2026-09-09）:请求级计时。header=server 受理并回响应头,
        # 官方 TTFT=首 chunk。TTFT-LLM_REQ_MS header ≈ server 内等待（模型锁/
        # prefill/解码）——配 llm.log 的 prefill progress 窗口可把「排队常数」
        # 定罪到具体环节（实测 p50 ~0.65s 待拆）。
        _qclient = self._client
        _qraw = _qclient.chat.completions.create

        async def _timed_create(**kw):
            _t0 = time.perf_counter()
            _resp = await _qraw(**kw)
            _t_hdr = time.perf_counter()
            if kw.get("stream"):
                print(f"LLM_REQ_MS header={(_t_hdr - _t0) * 1000:.0f} msgs={len(kw.get('messages') or [])}", flush=True)
            return _resp

        _qclient.chat.completions.create = _timed_create

        # PrefillSpeculator 快照钩子（agent.py 会设 on_request_messages 回调）:
        # 抓「逐字节就是本次真实请求 messages」的快照,投机预热按下一条请求的
        # 严格前缀组装(见 prefill_speculator.py)。回调未设=零开销直通。
        _sclient = self._client
        _sraw = _sclient.chat.completions.create
        self.on_request_messages = None  # Callable[[list[dict]], None] | None

        async def _snapshot_create(**kw):
            _cb = self.on_request_messages
            if _cb is not None:
                try:
                    _cb(list(kw.get("messages") or []))
                except Exception:  # noqa: BLE001 - 快照失败唔阻真实请求
                    pass
            return await _sraw(**kw)

        _sclient.chat.completions.create = _snapshot_create

    async def _prewarm_impl(self) -> None:
        # 真实 1-token 生成：暖 mlx 模型（冷启动的 KV 分配/首 token 占首包大头）。
        # 官方 prewarm 只验连接；AgentSession 构造时会自动调用本钩子。
        # 文本须 >11 token：mlx_lm 0.31.3 server 对 has_thinking 模型固定
        # rfind_think_start(prompt, start=len-11)，prompt 更短时负数索引直接
        # IndexError（包成 404 "list index out of range"）——Hy-MT2 实证，
        # warmup 因此整年白跳。
        if os.environ.get("LLM_WARMUP", "1") != "1":
            return
        try:
            await self._client.chat.completions.create(
                model=self._opts.model,
                messages=[
                    {
                        "role": "user",
                        "content": "Hello, this is a warmup request to the local model, please ignore.",
                    }
                ],
                max_tokens=1,
            )
            print("[agent] llm warmup done", flush=True)
        except Exception as exc:  # pragma: no cover - warmup 失败不致命
            print(f"[agent] llm warmup skipped: {exc!r}", flush=True)

    async def prefix_prewarm(self, messages: list[dict]) -> None:
        """真实 prompt 形状的 1-token 预热（会话首轮前，agent.py 发起）。

        与 _prewarm_impl（只暖模型/连接）不同：这里喂「真实 merged system +
        fake user 轮」，mlx_lm server 会把该前缀的 KV 留喺 prompt cache——
        turn-1 真请求共享整段 system 前缀 → cached≈system 长度，免 ~1.4s
        全量 prefill（会话首轮 cached=0 的专项解法）。失败由调用方吞掉。
        冷启动竞态：预热请求会排在官方 prewarm/开场白 prefill 后面，共享
        client 的 read=5s 会提前放弃（实测 APITimeoutError）——per-request
        放宽 read=30s，让服务端把前缀 prefill 跑完入 cache（client 等耐些，
        反正 fire-and-forget 唔阻塞任何人）。
        """
        await self._client.chat.completions.create(
            model=self._opts.model,
            messages=messages,
            max_tokens=1,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0),
        )

    # ---- 主回复 deadline + 兜底直念（2026-09-17,治「LLM 卡死整轮哑火」）----
    # 客服口径（用户拍板 3.0s,可再收紧）:等 8s/重试链=这通电话已废。三层:
    # ①首 token 截止——_LlmFallbackStream 对第一块 ChatChunk 计时
    #   （LLM_FIRST_TOKEN_TIMEOUT_S 默认 3.0,0=关）,超时立即出三语兜底句,
    #   但**唔弃流**:后台 drain 继续消费本流收晚到真答案(次级截止
    #   LLM_LATE_ANSWER_DEADLINE_S 默认 8s,0=回 aclose+regen 旧行为)——
    #   aclose 弃流换不来服务端停解码(mlx 无断连中止),二发只排其后抬 TTFT;
    # ②插件级重试默认归零（LLM_REQUEST_RETRIES 默认 0——官方 _main_task
    #   默认 3 次重试×10s=最坏 46s 静默,兜底壳取代它做恢复,更快且有声）;
    # ③传输层 read-gap（LLM_REQUEST_TIMEOUT_S 默认 8）只作字节流死流的粗后盾,
    #   首包后的流中卡死由它兜。
    # 兜底文本由 agent 装配时按通话语言注入(set_fallback_text);未注入的
    # worker(B 线 MT 等)零跨线影响。BOK_LLM_FALLBACK=0=整闸关(回旧行为)。
    # prefix_prewarm 等自带 conn_options 的调用方不受覆写影响(只认框架默认值身份)。
    _conn_opts: APIConnectOptions | None = None
    _fallback_text: str = ""
    _late_answer_cb = None  # Callable[[str], Awaitable[None]] | None(agent 注入)
    _fallback_gate = None  # Callable[[], bool] | None:True=本轮垫话已盖耳,抑制流内兜底

    def set_late_answer_cb(self, cb) -> None:
        """晚到答案交付回调(装配时注入):弃流兜底后后台重生成功 → cb(text) 补答。
        None(B 线等)=不重生。"""
        self._late_answer_cb = cb

    def set_fallback_text(self, text: str) -> None:
        """装配时注入兜底直念文本(空串=兜底关闭,超时行为回到整轮失败)。"""
        self._fallback_text = (text or "").strip()

    def set_fallback_gate(self, cb) -> None:
        """装配时注入「兜底抑制闸」(2026-09-17 call-11132bdd):cb()=True 表示
        本轮垫话已出声盖耳——3s 首 token 超时的道歉句再出声=「垫话+道歉+晚到
        真答案」三连叠音。闸合时跳过流内兜底 chunk,靠 drain/晚到补答交付;
        drain 无产出落 watchdog 兜底(顺延过,6s)。None=不抑制(旧行为)。"""
        self._fallback_gate = cb

    @staticmethod
    def _request_conn_options() -> APIConnectOptions:
        try:
            timeout = float(os.environ.get("LLM_REQUEST_TIMEOUT_S", "8") or 0)
        except ValueError:  # pragma: no cover - 配错回默认
            timeout = 8.0
        try:
            max_retry = int(os.environ.get("LLM_REQUEST_RETRIES", "0"))
        except ValueError:  # pragma: no cover - 配错回默认
            max_retry = 0
        if max_retry < 0:
            max_retry = 0
        if timeout <= 0:
            # 0=回官方默认(旧行为逃生口,连覆写都唔要)
            return DEFAULT_API_CONNECT_OPTIONS
        return APIConnectOptions(timeout=timeout, max_retry=max_retry)

    @staticmethod
    def _first_token_timeout_s() -> float:
        try:
            return float(os.environ.get("LLM_FIRST_TOKEN_TIMEOUT_S", "3.0") or 0)
        except ValueError:  # pragma: no cover - 配错回默认
            return 3.0

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
        if self._conn_opts is None:
            self._conn_opts = self._request_conn_options()
        injected = conn_options is None or conn_options is DEFAULT_API_CONNECT_OPTIONS
        if injected:
            conn_options = self._conn_opts
        stream = super().chat(
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
        )
        # 兜底壳只包主回复路径(注入档);自带 conn_options 的调用方
        # (prefix_prewarm 30s 档等)失败照旧被调用方吞,唔出兜底句。
        if injected and self._fallback_text and isinstance(stream, llm.LLMStream):
            # 弃流重生工厂:同参重建内芯流(官方流,唔套兜底壳),单次后台补答。
            def _stream_factory(_ctx=chat_ctx, _tools=tools, _co=conn_options,
                                _ptc=parallel_tool_calls, _tc=tool_choice,
                                _ek=extra_kwargs):
                return _OpenAICompatBase.chat(
                    self, chat_ctx=_ctx, tools=_tools, conn_options=_co,
                    parallel_tool_calls=_ptc, tool_choice=_tc, extra_kwargs=_ek,
                )

            return _LlmFallbackStream(
                self, stream, self._fallback_text,
                first_token_timeout_s=self._first_token_timeout_s(),
                stream_factory=_stream_factory,
                late_answer_cb=self._late_answer_cb,
                fallback_gate=self._fallback_gate,
            )
        return stream


def _late_answer_deadline_s() -> float:
    """drain(原流续读)次级截止秒数:首 token 超时出兜底后,本流最多再等多久。

    默认 8s(mlx 4B 出满答案远快于此;超时基本=真死流)。0=关 → 回立即
    aclose+factory 重生旧行为(kill-switch)。"""
    try:
        return float(os.environ.get("LLM_LATE_ANSWER_DEADLINE_S", "8") or 0)
    except ValueError:  # pragma: no cover - 配错回默认
        return 8.0


class _LlmFallbackStream(llm.LLMStream):
    """主回复出口闸：首 token 截止 + 失败兜底直念（LLM 出口单点拦截）。

    三条路都汇到同一句本地兜底（零模型调用）:
    - 首 token 超时（LLM_FIRST_TOKEN_TIMEOUT_S,默认 3.0s）:
      首 chunk 计时到点立即出兜底句——但**唔弃流**(2026-09-17 RC4):
      mlx 服务端无断连中止,被 aclose 的请求照解码到完才放锁,二发重生只排其后
      (僵尸解码税,实测 [watchdog] 后紧跟 TTFT 3872ms)。改为后台 drain 继续消费
      本流收集剩余文本,晚到真答案经回调补答;次级截止
      （LLM_LATE_ANSWER_DEADLINE_S,默认 8s,0=关→回立即 aclose+factory 重生
      旧行为）仍无产出才真弃流重生(最后手段);
    - 传输层超时/重试耗尽仍失败（APIError 浮出）;
    - 首 token 后流中卡死被传输层掐断（部分真答案已在途→兜底句跟在后面,
      好过死寂）。
    哨兵标记 LLM_FIRST_TOKEN_TIMEOUT / LLM_FALLBACK_TEXT / LLM_LATE_ANSWER
    供日志/探针归因(晚到答案 source=drain 原流续读 / source=regen factory 二发)。
    壳照抄 _StripTailAnchorStream:metrics 由内芯层转发,此处只排空监视分支。
    """

    def __init__(self, plugin, inner: "llm.LLMStream", fallback_text: str,
                 first_token_timeout_s: float = 0.0, stream_factory=None,
                 late_answer_cb=None, late_deadline_s: float | None = None,
                 fallback_gate=None):
        super().__init__(llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions())
        self._plugin_ref = plugin  # 基类不保底存 plugin:重生任务强引用集挂它身上
        self._inner = inner
        self._fallback = fallback_text
        self._first_deadline = first_token_timeout_s
        self._stream_factory = stream_factory
        self._late_answer_cb = late_answer_cb
        self._fallback_gate = fallback_gate
        self._late_deadline = (
            _late_answer_deadline_s() if late_deadline_s is None else late_deadline_s
        )
        self._got_first = False

    async def _metrics_monitor_task(self, event_aiter) -> None:
        # 内芯官方流自带 metrics(或失败时无 metrics),转发链上层负责;本壳只排空。
        async for _ in event_aiter:
            pass

    def _emit_fallback(self) -> None:
        print(f"LLM_FALLBACK_TEXT text={self._fallback!r}", flush=True)
        self._event_ch.send_nowait(
            llm.ChatChunk(
                id="llm-fallback",
                delta=llm.ChoiceDelta(content=self._fallback, role="assistant"),
            )
        )

    async def _aclose_inner(self) -> None:
        """真弃流(限时 1s):失败唔阻兜底/重生。"""
        try:
            await asyncio.wait_for(self._inner.aclose(), timeout=1.0)
        except Exception:  # noqa: BLE001 - 弃流失败唔阻后续
            pass

    async def _regen_late_answer(self) -> None:
        """弃流重生(单次,后台):drain 截止/内芯异常后的最后手段——同参二发新
        请求,成功即经 agent 注入的回调走正常 speech 队列补答(客户插话可打断)。
        服务端此刻多半已空闲(旧请求解码尾+垫话/兜底句吃掉几秒),二发命中率可观。"""
        if self._stream_factory is None or self._late_answer_cb is None:
            return
        if os.environ.get("BOK_LLM_REGEN", "1") != "1":
            return
        try:
            parts: list[str] = []
            async for ev in self._stream_factory():
                delta = getattr(ev, "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
                if isinstance(content, str) and content:
                    parts.append(content)
            text = "".join(parts).strip()
            if not text:
                print("LLM_LATE_ANSWER source=regen empty — skip", flush=True)
                return
            print(f"LLM_LATE_ANSWER source=regen chars={len(text)} — 补答", flush=True)
            await self._late_answer_cb(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 重生失败:兜底句已在,唔追二发
            print(f"LLM_LATE_ANSWER source=regen failed: {exc!r}", flush=True)

    async def _drain_late_answer(self, first_task: "asyncio.Task") -> None:
        """原流续读(单次,后台):首 token 超时后唔 aclose,继续消费本流收集剩余
        文本(复用 _regen_late_answer 骨架,数据源=现成内芯而非 factory 二发)——
        服务端本就解得完,白收。first_task=超时分支留下的活首 chunk 任务
        (绝不 cancel——内芯 tee_peer 对 cancellation 不免疫,peer 一死后续
        chunk 结构性拿唔到),本任务先收佢再续 async-for。次级截止
        (self._late_deadline)仍无产出 → 真弃流 aclose + factory 重生(最后
        手段);内芯中途抛异常同落 regen;已收到的部分文本优先交付(salvage,
        regen 会重复问同一条=又一轮僵尸解码)。"""
        if self._late_answer_cb is None:  # pragma: no cover - spawn 点已闸
            first_task.cancel()
            return
        parts: list[str] = []

        def _take(ev) -> None:
            delta = getattr(ev, "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if isinstance(content, str) and content:
                parts.append(content)

        async def _collect() -> str:
            try:
                _take(await first_task)
            except asyncio.CancelledError:
                raise
            except StopAsyncIteration:
                pass  # 内芯一个 chunk 都冇就收线:落 clean-empty 分支
            async for ev in self._inner:
                _take(ev)
            return "".join(parts)

        note = ""
        try:
            text = (await asyncio.wait_for(_collect(), timeout=self._late_deadline)).strip()
        except asyncio.TimeoutError:
            note = f"deadline={self._late_deadline:g}s"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 内芯中途死(含 boom 异常透传)
            note = f"err={exc!r}"
        await self._reap_first_task(first_task)
        await self._aclose_inner()
        salvage = "".join(parts).strip()
        if note:
            if salvage:
                print(
                    f"LLM_LATE_ANSWER source=drain chars={len(salvage)} — 补答"
                    f"(partial {note} salvage)",
                    flush=True,
                )
                await self._late_answer_cb(salvage)
                return
            print(f"LLM_LATE_ANSWER source=drain {note} 无产出 — aclose+regen", flush=True)
            self._spawn_regen()
            return
        if not text:
            print("LLM_LATE_ANSWER source=drain empty — aclose+regen", flush=True)
            self._spawn_regen()
            return
        print(f"LLM_LATE_ANSWER source=drain chars={len(text)} — 补答", flush=True)
        await self._late_answer_cb(text)

    @staticmethod
    async def _reap_first_task(first_task: "asyncio.Task") -> None:
        """收尾首 chunk 任务(取消+取回结果)防 Task exception was never retrieved。"""
        if not first_task.done():
            first_task.cancel()
        try:
            await first_task
        except BaseException:  # noqa: BLE001 - 收尾取回,结果唔关心
            pass

    def _spawn_attached(self, coro) -> None:
        """后台任务挂 plugin 长寿命强引用集(裸 create_task 事件循环只持弱引用,
        会被 GC 中途回收——本仓 dial fuse/bidi invalidate 同款教训);完成自弃。"""
        plugin = self._plugin_ref
        if plugin is None:
            coro.close()  # 防裸协程 never-awaited 警告
            return
        if not hasattr(plugin, "_bg_regen_tasks"):
            plugin._bg_regen_tasks = set()
        tasks = plugin._bg_regen_tasks
        t = asyncio.create_task(coro)
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    def _spawn_regen(self) -> None:
        self._spawn_attached(self._regen_late_answer())

    async def _run(self):
        timeout = self._first_deadline
        first_task: asyncio.Task | None = None
        drain_owns = False
        try:
            # 首 chunk 任务化(RC4):截止计时用 asyncio.wait(唔 cancel 任务)——
            # wait_for(__anext__) 超时会 cancel 掉内芯 tee_peer(async generator
            # 对 cancellation 不免疫,peer 一死=原流续读结构性拿唔到后续 chunk)。
            # 任务留活,drain 接手先收佢再续读;kill-switch 分支才真取消。
            first_task = asyncio.ensure_future(self._inner.__anext__())
            if timeout > 0:
                done, _pending = await asyncio.wait({first_task}, timeout=timeout)
                if first_task not in done:
                    if self._late_deadline > 0 and self._late_answer_cb is not None:
                        # 原流续读:唔 aclose——服务端无断连中止,弃流只换僵尸
                        # 解码税。兜底先出声,后台 drain 收晚到真答案;截止无
                        # 产出才真弃流重生。
                        print(
                            f"LLM_FIRST_TOKEN_TIMEOUT deadline={timeout}s — 兜底先出,"
                            f"原流续读(drain deadline={self._late_deadline:g}s)",
                            flush=True,
                        )
                        if self._fallback_gate is not None:
                            try:
                                if self._fallback_gate():
                                    # 垫话已盖耳:道歉句係叠床架屋(call-11132bdd
                                    # 「垫话+道歉+晚到真答案」三连),抑制后客户
                                    # 听到垫话→(静)→晚到真答案;drain 无产出落
                                    # watchdog 兜底(已被垫话开播顺延)。
                                    print(
                                        "LLM_FALLBACK suppressed (filler covered)"
                                        " — 靠 drain/晚到补答",
                                        flush=True,
                                    )
                                    drain_owns = True
                                    self._spawn_attached(self._drain_late_answer(first_task))
                                    return
                            except Exception:  # noqa: BLE001 - 闸回调失败=照常兜底
                                pass
                        self._emit_fallback()
                        drain_owns = True
                        self._spawn_attached(self._drain_late_answer(first_task))
                    else:
                        # 旧行为(kill-switch LLM_LATE_ANSWER_DEADLINE_S=0,或无
                        # 交付回调):立即 aclose 弃流 + factory 重生。
                        print(
                            f"LLM_FIRST_TOKEN_TIMEOUT deadline={timeout}s — 弃流出兜底(aclose+regen)",
                            flush=True,
                        )
                        await self._reap_first_task(first_task)
                        first_task = None
                        await self._aclose_inner()
                        self._emit_fallback()
                        self._spawn_regen()
                    return
            self._got_first = True
            self._event_ch.send_nowait(await first_task)
            first_task = None
            async for ev in self._inner:
                self._event_ch.send_nowait(ev)
        except asyncio.CancelledError:
            if first_task is not None and not first_task.done() and not drain_owns:
                first_task.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 - 重试耗尽/流中卡死:兜底句接住
            print(f"LLM_FALLBACK_TEXT err={exc!r} text={self._fallback!r}", flush=True)
            self._emit_fallback()
            if not self._got_first:
                # 首字都未出:重生有意义(传输层瞬断族)。已出过部分真答案就唔重念。
                self._spawn_regen()


class DeepSeekLLM(_OpenAICompatBase):
    """DeepSeek 云端（OpenAI 兼容契约，与本地 MlxLlmLLM 同一官方内芯）。"""

    provider = "deepseek"

    def __init__(
        self,
        api_key="",
        model="deepseek-flash",
        base_url="https://api.deepseek.com/v1",
        thinking: str = "",
    ):
        # 思考档位：DeepSeek 端点缺省**关**（官方默认 enabled，而本类 max_tokens 走
        # LLM_MAX_TOKENS 默认 160——思考会把预算烧光、正文出空串，通话侧=静默哑火；
        # 契约与实测见 bok_voice_core.deepseek_llm）。`DEEPSEEK_THINKING=enabled`
        # 可显式开（需同时给足 LLM_MAX_TOKENS）。非 DeepSeek 端点该字段为空 dict。
        body: dict = {"max_tokens": int(os.environ.get("LLM_MAX_TOKENS", "160"))}
        body.update(thinking_extra_body(base_url, thinking or os.environ.get("DEEPSEEK_THINKING", "")))
        super().__init__(
            model=model or "deepseek-flash",
            api_key=api_key,
            base_url=base_url,
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.35")),
            extra_body=body,
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


def _mt_prompt(text: str, target_lang: str, glossary: str = "", retry: bool = False) -> str:
    """官方 Hy-MT2 中文翻译模板:只要译文,不解释。

    glossary 非空时在模板前插一行术语块(会话级常量 → 每轮请求前缀稳定,KV
    缓存友好);缺省空串=逐字节同旧模板,零行为变化。术语走 prompt 唔改解码
    ——glossary 走 prompt 是工程界共识(SimulStreaming static_init_prompt /
    WhisperLive hotwords 同路,2026-09-16 B 线 P0-2 调研定案)。

    语气词标记(2026-09-16 实测)不走 prompt:Hy-MT2 是翻译特化模型,模板外的
    指示一概无视(前缀位/后缀位都试过,规则行原样无视或被当内容翻译),交给
    interpret._apply_voice_tags 在 say 前做确定性替换。

    ``retry=True``(E5 增补,仅语言校验失败后的单次重试用):按 type4me
    TranslationOutputValidator 形态加 ``IMPORTANT RETRY:`` 前缀 + 强化指示,并把
    最吃重的约束**写进模板句内**——Hy-MT2 对模板外文字大概率无视(上面的实测
    结论),把「必须整句只用目标语、不得保留原文」塞进模板本身才有指望生效。
    仅重试轮注入,正常轮逐字节不变(KV 前缀不受影响)。"""
    name = _MT_PROMPT_NAMES.get(target_lang, target_lang)
    prefix = f"术语表（保持一致）：{glossary}\n\n" if glossary else ""
    if retry:
        body = (
            f"将以下文本翻译为 `{name}`（必须整句只用{name}输出，"
            f"不得保留原文），注意只需要输出翻译后的结果，不要额外解释"
        )
        return f"IMPORTANT RETRY: {prefix}{body}：\n\n`{text}`"
    return f"{prefix}将以下文本翻译为 `{name}`，注意只需要输出翻译后的结果，不要额外解释：\n\n`{text}`"


# MT 输出包裹引号(2026-09-16 实证两类):①弯引号「“…”」(E2E call 实证);
# ②反引号「`…`」(模型镜像模板的 ` 包裹,:1236 直打 100% 复现)——比弯引号更高频。
# TTS 读引号=怪停顿、字幕带杂质。首尾独立剥——整段包裹场景全覆盖;译文内容
# 本身以引号开头的罕见场景会被误剥(spoken-style MT 输出几乎不含,可接受)。
_MT_QUOTES_OPEN = "\"“「『'`"
_MT_QUOTES_CLOSE = "\"”」』'`"


class _StripMTQuoteStream(llm.LLMStream):
    """剥离 MT 输出包裹引号(StatelessMTLLM 出口单点,TTS/字幕/历史全干净)。

    首个非空增量剥前引号;末字符扣住待定——流结束时是闭合引号则吞、否则补发
    (一字符 hold,延迟≈一个 chunk)。壳照抄 _ExprPrependStream:metrics 由内芯
    发出经 _bind_metrics_forward 转发,此处只排空监视分支。
    """

    def __init__(self, plugin, inner: "llm.LLMStream"):
        super().__init__(llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions())
        self._inner = inner
        self._lead_done = False
        self._held: str | None = None  # 扣住的末字符;None=无

    async def _metrics_monitor_task(self, event_aiter) -> None:
        async for _ in event_aiter:
            pass

    def _transform(self, text: str) -> str:
        if not self._lead_done:
            stripped = text.lstrip()
            if not stripped:
                return ""
            if stripped[0] in _MT_QUOTES_OPEN:
                print("MT_QUOTE_LEAD_STRIPPED", flush=True)
                stripped = stripped[1:].lstrip()
                if not stripped:
                    return ""
            self._lead_done = True
            text = stripped
        if self._held is not None:
            text = self._held + text
            self._held = None
        if text:
            self._held = text[-1]
            text = text[:-1]
        return text

    async def _run(self):
        async for ev in self._inner:
            delta = getattr(ev, "delta", None)
            content = getattr(delta, "content", None)
            if isinstance(content, str) and content:
                new_text = self._transform(content)
                if new_text != content:
                    ev = ev.model_copy(update={"delta": delta.model_copy(update={"content": new_text})})
            self._event_ch.send_nowait(ev)
        # 收尾:被扣末字符是闭合引号 → 吞;否则补发
        if self._held is not None:
            if self._held in _MT_QUOTES_CLOSE:
                print("MT_QUOTE_TAIL_STRIPPED", flush=True)
            else:
                self._event_ch.send_nowait(
                    llm.ChatChunk(id="mt-quote-tail", delta=llm.ChoiceDelta(content=self._held, role="assistant"))
                )
            self._held = None


class StatelessMTLLM(llm.LLM):
    """逐句无状态 MT 包装(Hy-MT2 翻译小模型,B 线同传专用)。

    MT 模型逐句无状态:每次调用只取进来 chat_ctx 的最后一条 user 文本,套官方
    模板压成一条 user 消息发内芯。丢历史有两个理由——历史会污染译文(前文术语/
    译法串味,翻译要每句独立);且无状态请求前缀恒定,prefill 不随通话增长,
    TTFT 全场稳定(第 100 句同第 1 句快)。glossary 非空时进模板术语槽——
    术语一致性靠每轮显式注入,唔靠历史(与无状态铁律自洽)。

    滚动上下文(BOK_INTERP_MT_CONTEXT,默认 0=关):非零时从 chat_ctx 抽最近 N 对
    「源→译」做「上文参考」块(LLMA/RALCP 式,治代词/指代断裂)——参考段每次
    现场重抽,内芯零内部状态,与无状态自洽;代价是该段逐轮位移→prefix 从参考
    段起失效(术语槽/模板头仍命中),N 小时 prefill 增量可忽略(probe 实测)。
    """

    def __init__(self, inner: llm.LLM, target_lang: str, glossary: str = "", context_turns: int = 0):
        super().__init__()
        self._inner = inner
        self._target_lang = target_lang
        self._glossary = str(glossary or "").strip()
        self._context_turns = max(0, int(context_turns))
        _bind_metrics_forward(inner, self)

    @property
    def model(self) -> str:
        # model/provider 跟内芯走(usage/metrics 面板显示真实内芯,不是包装层)。
        return str(getattr(self._inner, "model", "unknown"))

    @property
    def provider(self) -> str:
        return str(getattr(self._inner, "provider", "unknown"))

    def _rolling_pairs(self, chat_ctx) -> list[tuple[str, str]]:
        """从 chat_ctx 抽最近 N 对「源→译」(不含当前句,旧→新序;纯函数式,零内部状态)。

        chat_ctx 帧架自动累积 user/assistant 原文(无「原文：/译文：」前缀——
        那是 add_turn 落库口径,不进上下文)。倒序找 assistant,再回找其 user;
        单对截 120 字(参考段是 prompt 一部分,防长句膨胀)。"""
        items = list(getattr(chat_ctx, "items", []) or [])
        last_user = None
        for i in range(len(items) - 1, -1, -1):
            if getattr(items[i], "role", None) == "user":
                last_user = i
                break
        if last_user is None:
            return []
        pairs: list[tuple[str, str]] = []
        i = last_user - 1
        while i >= 0 and len(pairs) < self._context_turns:
            if getattr(items[i], "role", None) == "assistant":
                tgt = str(getattr(items[i], "text_content", None) or "").strip()
                j = i - 1
                src = ""
                while j >= 0:
                    if getattr(items[j], "role", None) == "user":
                        src = str(getattr(items[j], "text_content", None) or "").strip()
                        break
                    j -= 1
                if src and tgt:
                    pairs.append((src[:120], tgt[:120]))
                    i = j - 1
                    continue
            i -= 1
        pairs.reverse()
        return pairs

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
        return self._chat_impl(
            chat_ctx,
            tools=tools,
            conn_options=conn_options,
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            retry=False,
        )

    def chat_retry(
        self,
        *,
        chat_ctx,
        tools=None,
        conn_options=None,
        parallel_tool_calls=None,
        tool_choice=None,
        extra_kwargs=NOT_GIVEN,
    ):
        """E5 增补:语言校验失败后的**单次强化重试**入口(同模板+强化指示)。

        与 ``chat`` 唯一差别=进 ``_mt_prompt(retry=True)``(IMPORTANT RETRY 前缀+
        模板句内「整句只用目标语」约束)。签名/透传/引号剥离与 ``chat`` 逐字一致,
        调用方(interpret._mt_once)照 ``chat`` 的 max_retry=1 + wait_for 超时纪律
        用它——本方法自身**无循环**,重试次数由调用方控制在一次。"""
        return self._chat_impl(
            chat_ctx,
            tools=tools,
            conn_options=conn_options,
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            retry=True,
        )

    def _chat_impl(
        self,
        chat_ctx,
        *,
        tools=None,
        conn_options=None,
        parallel_tool_calls=None,
        tool_choice=None,
        extra_kwargs=NOT_GIVEN,
        retry: bool = False,
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
        content = _mt_prompt(last_user, self._target_lang, self._glossary, retry=retry)
        if self._context_turns:
            pairs = self._rolling_pairs(chat_ctx)
            if pairs:
                lines = ["上文参考（保持译名与指代一致，勿输出本段）："]
                for src, tgt in pairs:
                    lines.append(f"源：{src}")
                    lines.append(f"译：{tgt}")
                content = "\n".join(lines) + "\n\n" + content
        mt_ctx = llm.ChatContext()
        mt_ctx.add_message(role="user", content=content)
        inner_stream = self._inner.chat(
            chat_ctx=mt_ctx,
            tools=tools,
            conn_options=conn_options,
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            extra_kwargs=_forward_extra_kwargs(extra_kwargs),
        )
        # 非流结果(测试假内芯/异常防御)原样透传,不硬包 LLMStream
        if isinstance(inner_stream, llm.LLMStream):
            return _StripMTQuoteStream(self, inner_stream)
        return inner_stream

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


# 尾部重复锚的标签原文（render_context_tail 渲染,4B 会拟声复刻进输出）。
_TAIL_ANCHOR_LABEL = "【你上一句】"


def strip_tail_anchor_text(text: str) -> str:
    """整段文本版的尾部锚剥离（L2,2026-09-21）。

    ``_StripTailAnchorStream`` 是流式状态机,只罩「框架消费的主回复流」;晚到补答
    （``late_answer_cb`` 直投,§20.5 实证把 ``【你上一句】「…`` 念出声）结构性绕过
    它——这里给完整文本一条单点纯函数:从**首个**标签出现处截到结尾。锚块是框架
    注入的尾部模板,模型输出里出现标签本身即拟声复刻,不存在「合法包含」场景,
    故首标签即截断点;只剥尾部（标签前若有正文,正文保留）。
    """
    idx = str(text or "").find(_TAIL_ANCHOR_LABEL)
    if idx < 0:
        return str(text or "")
    return str(text or "")[:idx].rstrip()

# 单字数字(汉字+阿拉伯)之间的顿/逗号——剥离后连续读;「拼多多、淘宝」等
# 普通列表不含数字字,不受影响。(2026-09-12「普通话念数字很奇怪」:4B 爱写
# 「一、一、二、二」,每个顿号一次 TTS 停顿=机器人感;MiniMax 对阿拉伯数字串
# 本来就逐位读,时长实验 6.54s vs 6.40s 等价,无需改写数字形态。)
_DIGIT_PAUSE_RE = re.compile(r"(?<=[零〇一二三四五六七八九0-9])[、，]\s*(?=[零〇一二三四五六七八九0-9])")
_DIGIT_CHAR_RE = re.compile(r"[零〇一二三四五六七八九0-9]")


class _StripTailAnchorStream(llm.LLMStream):
    """剥离模型输出里拟声复刻的「你上一句」锚块（LLM 流出口单点拦截）。

    2026-09-09 call-974d8da3 实证:S5 尾部瘦身令易变尾部以【你上一句】「…」
    模板块收尾,4B 模型把这个格式模式照抄进回复（答案后面追加标签+自引整块）,
    TTS 念出声、落 turns 库,进历史还会强化后续轮的模仿（反馈回路）。在
    ContextAwareLLM.chat 出口包一层,TTS/历史/turns/重复锚四个下游全部拿到
    干净文本;渲染本身不动（锚的重复控制功能保留）。壳照抄 _ExprPrependStream:
    metrics 由内芯发出经 _bind_metrics_forward 转发,此处只排空监视分支。

    2026-09-12 同流加数字顿号剥离:4B 复述号码爱写「一、一、二、二」——每个
    顿号一次 TTS 停顿=机器人感(call-a2705ed2 实证);剥成连续「一一二二」
    才自然。MiniMax 对阿拉伯数字串本来就逐位读(时长实验 6.54s vs 6.40s 等价),
    WA 直念线无需改写。
    """

    def __init__(self, plugin, inner: "llm.LLMStream"):
        super().__init__(llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions())
        self._inner = inner
        # HOLD:缓冲可能是标签前缀的尾段(防跨 chunk 劈开);DROP:已进块,吞到「」止。
        self._hold = ""
        self._drop = False
        # 上一段已发出的末字符(仅当是数字字):跨 chunk 配对「一|、一」用。
        self._prev_digit = ""
        # 末尾「数字+顿/逗」扣住的分隔符:未发出,待下段首字符配对。
        self._pending_sep = ""

    async def _metrics_monitor_task(self, event_aiter) -> None:
        async for _ in event_aiter:
            pass

    def _feed(self, text: str) -> str:
        """状态机过滤一段增量文本,返回可安全发出的部分。"""
        buf = self._hold + text
        self._hold = ""
        out: list[str] = []
        while buf:
            if self._drop:
                end = buf.find("」")
                if end == -1:
                    buf = ""
                    break  # 块未闭合,余下全吞
                self._drop = False
                buf = buf[end + 1 :]
                continue
            idx = buf.find(_TAIL_ANCHOR_LABEL)
            if idx != -1:
                out.append(buf[:idx])
                print("TAIL_ANCHOR_MIMIC_SUPPRESSED", flush=True)
                self._drop = True
                buf = buf[idx + len(_TAIL_ANCHOR_LABEL) :]
                continue
            # 无完整标签:只扣住可能是标签前缀的尾段(通常没有,零延迟透传)。
            keep = 0
            for n in range(min(len(buf), len(_TAIL_ANCHOR_LABEL) - 1), 0, -1):
                if _TAIL_ANCHOR_LABEL.startswith(buf[-n:]):
                    keep = n
                    break
            if keep < len(buf):
                out.append(buf[: len(buf) - keep])
            self._hold = buf[len(buf) - keep :] if keep else ""
            break
        return self._strip_digit_pauses("".join(out))

    def _strip_digit_pauses(self, text: str) -> str:
        """数字顿号剥离(跨 chunk 左右文配对):见类注释 2026-09-12 段。

        _prev_digit=上段末位数字(已发出,仅作左文);_pending_sep=末尾「数字+
        顿/逗」的分隔符(未发出,扣住等下段首字符配对——是数字则消,不是则照发)。"""
        if not text:
            return text
        work = (self._prev_digit or "") + (self._pending_sep or "") + text
        self._pending_sep = ""
        merged = _DIGIT_PAUSE_RE.sub("", work)
        if self._prev_digit and merged.startswith(self._prev_digit):
            merged = merged[1:]  # 左文锚字符已在上段发出,只留新合并结果
        m = re.search(r"[零〇一二三四五六七八九0-9][、，]$", merged)
        if m:
            self._pending_sep = merged[-1]
            merged = merged[:-1]
        last = merged[-1] if merged else ""
        self._prev_digit = last if _DIGIT_CHAR_RE.match(last) else ""
        return merged

    def _flush_at_end(self) -> str:
        # 流末仍 drop=块被截断,弃;尾段是残缺标签前缀(模仿起头没写完),同样弃
        # ——残缺「【你上一」念出去比丢掉更伤。
        if self._drop:
            return ""
        if self._hold and _TAIL_ANCHOR_LABEL.startswith(self._hold):
            return ""
        return self._hold

    async def _run(self):
        async for ev in self._inner:
            delta = getattr(ev, "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if not content:
                self._event_ch.send_nowait(ev)
                continue
            clean = self._feed(content)
            if not clean:
                continue
            if clean != content:
                ev = llm.ChatChunk(
                    id=getattr(ev, "id", ""),
                    delta=llm.ChoiceDelta(content=clean, role=getattr(delta, "role", "assistant")),
                    usage=getattr(ev, "usage", None),
                )
            self._event_ch.send_nowait(ev)
        tail = self._flush_at_end()
        if tail:
            self._event_ch.send_nowait(
                llm.ChatChunk(id="tail-flush", delta=llm.ChoiceDelta(content=tail, role="assistant"))
            )


# ---- 出口复读防线(2026-09-12 P0「会说话」兜底层) ----
# call-8fa17d2b 实证:两轮回复一字不差、连续两轮重念整段通知。渐进披露(话术
# 分支单条命中)+【你上一句】锚截短治的是源头;这里在 LLM 流出口逐句比对
# 上一句回复,拟声复读句剥掉不出声——确定性兜底,不赌 4B 听话。客户明确
# 要求重讲(REPEAT verdict,agent 钩子置 ctx.repeat_requested)时放行。
_SENT_END_RE = re.compile(r"[。！？!?]")


def _norm_for_similarity(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(text or ""))


def _is_parrot_sentence(sentence: str, last_reply: str, threshold: float = 0.9) -> bool:
    """句子与上一句回复的任一原句归一化相似 ≥threshold、或净文是其子串
    (前缀截断式复读:整句重念但剪短了,ratio 0.87 会漏) → 拟声复读。"""
    s = _norm_for_similarity(sentence)
    if len(s) < 6:
        return False
    for prev in _SENT_END_RE.split(last_reply):
        p = _norm_for_similarity(prev)
        if len(p) >= 6 and (s in p or difflib.SequenceMatcher(a=s, b=p).ratio() >= threshold):
            return True
    return False


class _RepeatSelfGuardStream(llm.LLMStream):
    """LLM 流出口逐句剥复读:缓冲到句边界,复读句吞掉、新内容照发。

    全剥空 → 流空收尾(罕见;渐进披露治源头后这里只兜底,真发生时垫话/心跳
    补位)。壳与 _StripTailAnchorStream 同款:metrics 由内芯转发,此处排空。"""

    def __init__(self, plugin, inner: "llm.LLMStream", last_reply: str, *, bypass: bool = False):
        super().__init__(llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions())
        self._inner = inner
        self._last_reply = last_reply
        self._bypass = bypass or not last_reply
        self._buf = ""

    async def _metrics_monitor_task(self, event_aiter) -> None:
        async for _ in event_aiter:
            pass

    def _feed(self, text: str) -> str:
        """缓冲到句边界;完整句非复读才放行,复读句整句吞掉。"""
        if self._bypass:
            return text
        self._buf += text
        out: list[str] = []
        while True:
            m = _SENT_END_RE.search(self._buf)
            if not m:
                break
            sentence = self._buf[: m.end()]
            self._buf = self._buf[m.end() :]
            if _is_parrot_sentence(sentence, self._last_reply):
                print(f"REPEAT_SELF_SUPPRESSED sent={sentence!r}", flush=True)
                continue
            out.append(sentence)
        return "".join(out)

    def _flush_at_end(self) -> str:
        if self._bypass or not self._buf:
            return self._buf
        rest = self._buf
        self._buf = ""
        if _is_parrot_sentence(rest, self._last_reply):
            print(f"REPEAT_SELF_SUPPRESSED sent={rest!r}", flush=True)
            return ""
        return rest

    async def _run(self):
        async for ev in self._inner:
            delta = getattr(ev, "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if not content:
                self._event_ch.send_nowait(ev)
                continue
            clean = self._feed(content)
            if not clean:
                continue
            if clean != content:
                ev = llm.ChatChunk(
                    id=getattr(ev, "id", ""),
                    delta=llm.ChoiceDelta(content=clean, role=getattr(delta, "role", "assistant")),
                    usage=getattr(ev, "usage", None),
                )
            self._event_ch.send_nowait(ev)
        tail = self._flush_at_end()
        if tail:
            self._event_ch.send_nowait(
                llm.ChatChunk(id="repeat-flush", delta=llm.ChoiceDelta(content=tail, role="assistant"))
            )


# 对象档案行边界=调用方给的显式换行(每个输入行是一个语义单元,如一行背景
# +一行备注);绝不在句号处二次切分——多句背景若被句号切碎,第 2 行(备注)
# 会被静默挤掉,档案失真。


def _context_mem_legacy() -> bool:
    """P1.2a(2026-09-21)记忆压缩 kill-switch:1=回旧档(drop-oldest+上限 1200)。

    进 `_FORWARD_ENV`(tests/test_forward_env 门禁)。"""
    return os.environ.get("BOK_CONTEXT_MEM_LEGACY", "") == "1"


def _intent_context_enabled() -> bool:
    """P2.4(§48)意图喂下游 kill-switch:默认 "1" 开,`0` 全关(=set 恒 no-op、
    【客户意图】行消失,尾部字节逐字同旧)。进 `_FORWARD_ENV`。"""
    return os.environ.get("BOK_INTENT_CONTEXT", "1") == "1"


class ContextState:
    """Shared per-call context memory: per-turn knowledge + running summary.

    Populated asynchronously by the agent's turn listener; read synchronously
    by ``ContextAwareLLM`` to inject a compact, progressive-disclosure system
    message (top-K snippets + bounded conversation summary) each turn.
    """

    def __init__(self, account_id: str = "", max_snippets: int = 2, max_summary_chars: int = 0):
        self.account_id = account_id
        self._max_snippets = max_snippets
        # P1.2a:显式传参(测试/嵌入方)优先;缺省按 kill-switch 定——新档 400
        # (尾部有界=每轮新 prefill 有界,§48 P1),legacy 档回旧 1200。
        self._max_summary_chars = (
            max_summary_chars
            if max_summary_chars > 0
            else (1200 if _context_mem_legacy() else 400)
        )
        self._snippets: list[str] = []
        self._summary_lines: list[str] = []
        self._user_lang: str = ""
        self._web: list[str] = []
        self._flow_overview: str = ""
        self._flow_current: str = ""
        self._object_brief: str = ""
        # RAG 检索段渲染门(默认关):绑分步话术的封闭流程不做知识库/联网检索
        # (单对象只上话术+对象档案),易变尾部只剩当前步+记忆,尾部预算最小化。
        # set_knowledge/set_web 仍可照常喂数据(开放人设场景),只有 rag_enabled=True
        # 时 tail 才渲染那两节。agent.py 装配时按自身 RAG 门控(_context_rag_enabled)
        # 置位,或直接改用 ContextState.from_env() 装配(CONTEXT_RAG=1 → True)。
        self.rag_enabled: bool = False
        # WhatsApp 已捕获号码（注入尾部,防 LLM 复述错号——2026-09-06 实测尾号读错）
        self._whatsapp_note: str = ""
        # 当轮客户意图（P2.4 意图喂下游,spec §48）:agent 钩子每轮把「graph 命中意图
        # 名 → 规则归类具名意图名」写进来(空=清位),render_context_tail 全量档渲染
        # 一行【客户意图】。有界 ≤40 字;变化才 +revision(意图属实质变化,须全量尾部
        # 才带得出——slim 紧凑档刻意不含它)。kill-switch BOK_INTENT_CONTEXT=0 时 set
        # 恒 no-op → 字段恒空 → 尾部字节同旧。
        self._customer_intent: str = ""
        # 追加式尾部账本（KV-cache 铁律 2026-09-05）：记录每个 user 消息被
        # ContextAwareLLM 拼上的易变尾部（原文, 原文+尾部, 当时 revision），FIFO
        # 对应历史里的 user 消息。下一轮请求把历史中的旧 user 重放成「原文+当时的
        # 尾部」——上一轮请求因此永远是下一轮的严格前缀（此前尾部只拼在最后一条
        # user、下轮即被剥掉 → 中途分叉 → 只有 system 锚点命中，对话历史每轮全量
        # 重 prefill，实测暖轮 cached 恒=锚点、TTFT 0.7-1.3s 的主因）。
        # revision：尾部内容实质变化计数（推进/WhatsApp 捕获）——只服务抢跑重建轮
        # 「末条 user 尾部重渲染」判定（ContextAwareLLM.chat n_new==0 分支）。
        self._applied_tails: list[tuple[str, str, int]] = []
        self._revision: int = 0
        # 会中客户已讲事实（平台/号码,append-only 有界）与 AI 上一句（重复锚）。
        # 都渲染进尾部，治「忘记早轮信息」「原句重复复述」（2026-09-06 行为取证）。
        self._call_facts: list[str] = []
        self._last_reply: str = ""
        # 本轮客户是否要求重讲（REPEAT verdict）——出口复读防线放行合法复述
        # (2026-09-12:agent 钩子每轮写入;客户明确要求重复时模型照讲上一句关键
        # 内容是正确行为,不能被当拟声复读剥掉)。
        self.repeat_requested: bool = False

    @property
    def revision(self) -> int:
        return self._revision

    def set_whatsapp_note(self, num: str) -> None:
        v = num or ""
        if v != self._whatsapp_note:
            self._whatsapp_note = v
            self._revision += 1

    def set_flow_current(self, current: str) -> None:
        """每轮更新当前步约束(flow controller 推进后调用)。

        内容实质变化才 +revision:每轮同值重复 set 唔虚增;【新一步】一次性提示
        在下一轮消失亦算变化(重建轮据此把末条 user 尾部对齐到当前版)。
        """
        current = current or ""
        if current != self._flow_current:
            self._flow_current = current
            self._revision += 1

    def add_call_fact(self, text: str, limit: int = 4) -> None:
        """沉淀一条会中事实(去重,有界 FIFO,≤limit 条)——渲染进尾部
        【通话中客户已讲】,治「模型重复问已答过的事」。

        事实属实质变化 → +revision（尾部瘦身门:变化轮才发全量尾部,
        BOK_TAIL_SLIM=1 时未变轮只发紧凑标签,新事实漏进紧凑尾=事实失明）。"""
        t = str(text or "").strip()
        if not t or t in self._call_facts:
            return
        self._call_facts.append(t)
        if len(self._call_facts) > limit:
            self._call_facts.pop(0)
        self._revision += 1

    def set_customer_intent(self, text: str) -> None:
        """设置当轮客户意图(截 40 字)— P2.4 意图喂下游(spec §48)。

        语义=**每轮覆盖**:调用即重写当轮意图,**空串=清位**(上一轮有意图、本轮
        无 → 不调用会令陈旧意图残留,下一轮任何实质变化触发全量尾部时带出误导
        信号)。意图属实质变化 → 值变化才 +revision(同 add_call_fact 纪律),令
        该轮走全量尾部、【客户意图】行才带得出(slim 紧凑档刻意不含它)。
        kill-switch `BOK_INTENT_CONTEXT=0`(=0 全关)=本方法恒 no-op → 字段恒空,
        尾部字节逐字同旧。"""
        if not _intent_context_enabled():
            return
        v = str(text or "").strip()[:40]
        if v != self._customer_intent:
            self._customer_intent = v
            self._revision += 1

    def set_last_reply(self, text: str) -> None:
        """记录 AI 最近一句回复(截 80 字)作尾部重复锚——模型看得见自己上一句,
        唔会原句再讲一次。空串忽略(保持上一条锚)。"""
        t = str(text or "").strip()
        if t:
            self._last_reply = t[:80]

    @property
    def last_reply(self) -> str:
        """只读出口:agent 回声守卫比对「AI 正在讲/刚讲过」的文本用。"""
        return self._last_reply

    def record_applied_tail(self, orig: str, final: str) -> None:
        self._applied_tails.append((orig, final, self._revision))

    def rewrite_last_applied_tail(self, orig: str, final: str) -> None:
        """抢跑重建轮把末条 user 尾部重渲染成当前版后,同步账本(保持 FIFO 对齐)。"""
        if self._applied_tails:
            self._applied_tails[-1] = (orig, final, self._revision)

    def applied_tails(self) -> list[tuple[str, str, int]]:
        return list(self._applied_tails)

    def prune_applied_tails(self, keep: int) -> None:
        if keep < 0:
            keep = 0
        if len(self._applied_tails) > keep:
            self._applied_tails = self._applied_tails[-keep:]

    @classmethod
    def from_env(cls, account_id: str = "") -> "ContextState":
        """env 装配口:CONTEXT_RAG=1 → 打开知识/联网节的尾部渲染(默认关)。

        与 agent.py 的取数门控 _context_rag_enabled 同一 env 开关;装配处换用
        本口即可让「取数开」与「渲染开」永远同源,不留两套判定。
        """
        st = cls(account_id=account_id)
        if os.environ.get("CONTEXT_RAG", "") == "1":
            st.rag_enabled = True
        return st

    def set_object_brief(self, text: str) -> None:
        """存入【对象档案】(会话装配时一次性注入,整场静态,进稳定指令前缀)。

        有界:最多 2 行 × 每行 150 字。行边界=输入里的显式换行——每个输入行是
        一个语义单元(如一行背景+一行备注),绝不在句号处二次切分(多句背景被
        句号切碎会把第 2 行备注静默挤掉,档案失真)。单行超长硬截断(149 字 +
        「…」)。切分/截断全部确定性:同一输入永远得到同一份档案字节,保证前缀
        KV-cache 不因档案措辞变化而断裂。空串清档。
        """
        raw = str(text or "").replace("\r\n", "\n")
        lines: list[str] = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            lines.append((line[:149] + "…") if len(line) > 150 else line)
            if len(lines) >= 2:
                break
        self._object_brief = "\n".join(lines)

    def set_flow(self, overview: str, current: str) -> None:
        """设置对话流程:overview 为基础注入(全貌),current 为每轮当前步约束。"""
        self._flow_overview = overview
        current = current or ""
        if current != self._flow_current:
            self._flow_current = current
            self._revision += 1

    def set_web(self, results: str | list[str]) -> None:
        """注入联网检索结果（Wikipedia/DDG 摘要），随 system 消息给 LLM 参考。

        注意:数据照常收(截到 1 条×150 字),但 tail 渲染受 rag_enabled 门控
        (默认关)——CONTEXT_RAG=1/开放人设装配时置 True 才会出现在尾部。
        """
        if isinstance(results, str):
            self._web = [results] if results.strip() else []
        elif results:
            self._web = list(results)
        else:
            self._web = []
        # 联网结果是最弱的参考源（开放域/wiki 杂音既挤占尾部预算又会带偏 4B 小模型
        # ——P4-C：「有冇人？知道。」检索回 Wikipedia → 回复幻觉成普通话乱语）。
        # 收紧到最多 1 条、单条 150 字：够给一句事实线索，唔够位带偏回复。
        self._web = [s.strip() for s in self._web[:1] if str(s).strip()]
        if self._web and len(self._web[0]) > 150:
            self._web[0] = self._web[0][:149] + "…"

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
        """注入知识库检索片段(单条 150 字截断)。渲染受 rag_enabled 门控(默认关),
        CONTEXT_RAG=1 路径由装配处置 True 后重新上尾。"""
        seen: set[str] = set()
        out: list[str] = []
        for s in snippets or []:
            text = str(s.get("text", "") or "").strip()
            # 单条截断：防超大文档整段进 system（单条无限长会撑爆每轮 prefill）。
            # 150 字/条是尾部预算的单条配额（瘦砍自 350）；真实尾部预算公式见
            # render_context_tail 注释（当前步 ~321 字 + 记忆 6×200 字为主体）。
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
        # P1.2a(2026-09-21,§48 P1「恒定轮延迟」):尾部=每轮新 prefill 的全部成本
        # (§46.1 受控实验:638 字尾≈1.1s/轮、876 字≈1.6s/轮,单调涨)——记忆行是
        # 唯一单调增长项。改**滚动压缩**:超上限时把最旧两行各取前半并成一行
        # (信息密度翻倍而非整行丢弃),行数有界→尾部字数有界→每轮 TTFT 有界。
        # kill-switch `BOK_CONTEXT_MEM_LEGACY=1` 回旧「drop-oldest」档(上限同旧 1200)。
        cap = self._max_summary_chars
        while len(self._summary_lines) > 1:
            joined = "\n".join(self._summary_lines)
            if len(joined) <= cap:
                break
            if _context_mem_legacy():
                self._summary_lines.pop(0)
            else:
                old = self._summary_lines
                merged = (old[0][:80].rstrip() + "；" + old[1][:80].rstrip())[:180]
                old[0:2] = [merged]

    def render_system_message(self) -> str:
        """完整 system 段（兼容旧调用/测试）：稳定指令前缀 + 易变参考尾部。"""
        prefix = self.render_instruction_prefix()
        tail = self.render_context_tail()
        if prefix and tail:
            return f"{prefix}\n\n{tail}"
        return prefix or tail

    def render_instruction_prefix(self) -> str:
        """【稳定指令前缀】——放最前、紧贴人设 base。

        含：用户语言规则 / 回复节奏 / 应答准则 / 话术流程总览(整通不变) /
        对象档案(set_object_brief 会话装配时一次性注入,整通不变)。
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
                rule = ("Reply in natural spoken English only (like on a phone call); "
                        "do not mix in any Chinese words (Mandarin or Cantonese); "
                        "do not explain or add notes.")
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
        # 重复控制(2026-09-09 S5 尾部瘦身):指令从每轮尾部上移稳定前缀——原先
        # ~90 字指令逐字重复在每轮尾部里逐轮重 prefill,是纯浪费;前缀整场缓存
        # 命中零成本。尾部只留【你上一句】引文。
        parts.append(
            "【重复控制】已讲过的内容绝不原句或近原句再讲一次；"
            "连续回答同类问题时必须换用不同的说法和角度，不得只改动个别字词；"
            "客户没有新异议就不要重复确认，停下来等他说。"
            "每轮尾部的【你上一句】即你最近一次回复原文，对照它避免重复。"
        )
        # 客服应答准则：永不主动说"不知道/查不到"，知识不够时用客服话术兜住。
        # 这是客服与聊天机器人的本质区别——客户要的是被接住，不是被拒绝。
        # 语言纯度：此段无条件进每通通话的前缀，必须用标准书面中文——写成粤语
        # 书面语会把 4B 模型的普通话回复带偏成夹粤语（2026-09-06 实证后改写）。
        parts.append(
            "【应答准则】你是客服，绝不能说「不知道」「查不到」「不清楚」「没这个资料」「我帮不了你」。"
            "资料/知识不够回答时，用客服的方式接住客户："
            "① 先给确定能给的（安抚、已确认信息、下一步动作）；"
            "② 需要查证/转办的，明确告诉客户你会跟进处理，或转给能处理的人/专员跟进；"
            "但「帮你核实/查一下再答复你」只适用于真的要查外部资料的情况——如果你正按话术流程"
            "推进（例如要向客户讲赔偿方案/引导办理），就直接按当前步讲，绝不要用「等我查下/几分钟内答复你」"
            "这类拖延话术，也不要在流程中途自作主张承诺回头再答复；"
            "③ 客户的问题超出当前业务，就用引导话术收住（如「这个问题我帮您转给专门跟进的同事，他会马上联系您」），绝不冷场、绝不空手。"
        )
        # A2b 回复长度铁律(2026-09-13,plan 甲.2):长独白=对话权锁死+打断饿死
        # (call-909744db 160 字稿 29.78s 播报,客户「太长啦」3 连轮零回复)。
        # 进静态前缀=整场 KV-cache 命中,零每轮成本。
        parts.append(
            "【回复长度】每轮最多讲两句短句（合计四十字以内），绝不一口气连讲超过三句。"
            "客户没有追问细节就不要展开——细节（如赔偿档位、办理步骤）等客户问到再分步讲，每次只讲一两句；"
            "讲完一两句就停下等客户回应，让客户保有插话的空间。"
        )
        # 回应范例(2026-09-12 P0「会说话」):4B 靠示例学风格远胜靠禁令——
        # call-8fa17d2b 实证「勿念原文」挡不住话术全文常驻的复制引力;话术已改
        # 渐进披露(分支按回应单条命中,见 flow.match_step_branch),这里再钉住
        # 「一句答所问+一句带回」的回复形态。示例用通用客服语,不绑具体模板
        # 事实;进静态前缀=整场 KV-cache 命中,零每轮成本。
        parts.append(
            "【回应范例（学这种答法，不要照抄例句本身）】客户问什么，你先用一句话直接答他问的那件事，"
            "再用一句把话题带回当前要办的事，两句收住。"
            "例：客户问「你们是哪里的」→「我们是帮你收发转运的集运仓库。」"
            "例：客户说「我不记得了」→「没关系，我这边帮您一起核对。」"
            "例：客户问「为什么是这个数」→「是按对应标准算的，您的情况适用这一档。」"
            "每个例子都一样：先答客户问的事，不念稿、不重复上一句，答完自然带回流程。"
        )
        # 情绪标签试点（专项 C4,EMOTION_TAG_PILOT=1 选入;EMOTION_TAG_PROMPT=0
        # 可单关 prompt 只留 TTS 剥除）:4B 每轮开头输出一个白名单情绪标签,
        # 先只验「出标签稳定性」,TTS 侧剥除,数据够格再接 voice_setting。
        if os.environ.get("EMOTION_TAG_PROMPT", os.environ.get("EMOTION_TAG_PILOT", "0")) == "1":
            parts.append(
                "【情绪标签试点】每次回复的最开头，先输出一个方括号情绪标签再说话。"
                "只能从这些里选一个：[关切] [抱歉] [耐心] [开心] [严肃]。"
                "示例：[关切]您别着急，我马上帮您查。标签只输出一次，不要念出来，不要用别的格式。"
            )
        if self._flow_overview:
            parts.append("【话术流程总览(别照读,按进度推进)】\n" + self._flow_overview)
        # 对象档案:静态、整场不变,放总览之后(先懂流程再看客户是谁)。有界
        # (2 行×150 字,set_object_brief 保证),前缀体积影响一次性 prefill 可忽略。
        if self._object_brief:
            parts.append("【对象档案】\n" + self._object_brief)
        return "\n\n".join(parts)

    def _last_reply_anchor(self) -> str:
        """【你上一句】截短锚:只示开头 12 字,带转换性指令(勿原样重述)。"""
        head = self._last_reply[:12]
        ell = "…" if len(self._last_reply) > 12 else ""
        return (
            "【你上一句】「" + head + ell + "」"
            "（只示开头，全文在对话历史；这句已讲过，禁止原样或只换个别字重述）"
        )

    def render_context_tail(self) -> str:
        """【易变参考尾部】——每轮变的当前步/检索资料/记忆，垫在 system 最末。

        前缀(稳定指令+话术总览+对象档案)+人设 base 在前且整场字节不变，flow
        步骤推进只改这段尾部（短、逐轮重渲染）→ 前缀 KV-cache 照命中，每轮只
        prefill 尾部增量。当前步放尾部最前，让「推进=换一小段尾部」而非动前缀。
        知识/联网两节仅在 rag_enabled=True 时渲染(默认关:封闭话术流程不做检索,
        单对象只上话术+对象档案;CONTEXT_RAG=1/开放人设由装配处置 True)。

        尾部瘦身（BOK_TAIL_SLIM=1 默认,0 回退;2026-09-09 S5）:revision 与上一
        条已冻结尾部相同（流程/事实/WhatsApp 均无实质变化）时,只发紧凑标签
        ——全量指引在上一轮尾部里原样可见,重复逐轮重 prefill 是纯浪费（未缓存
        后缀实测 238-315 tok/轮,是暖轮 TTFT 大头,0.4-0.6k tok/s 下≈0.4-0.6s）。
        【你上一句】的固定指令文本已上移稳定前缀（【重复控制】）,尾部只留引文。
        """
        _last_rev = self._applied_tails[-1][2] if self._applied_tails else None
        slim = (
            os.environ.get("BOK_TAIL_SLIM", "1") == "1"
            and _last_rev is not None
            and _last_rev == self._revision
        )
        parts: list[str] = []
        if slim:
            _step_head = (self._flow_current.strip().splitlines() or [""])[0]
            parts.append(f"【{_step_head or '流程'}·继续】状态无实质变化，按上文同一步要求继续。")
            if self._whatsapp_note:
                parts.append("【已记录客户 WhatsApp】" + self._whatsapp_note)
            if self._last_reply:
                parts.append(self._last_reply_anchor())
            return "\n".join(parts)
        if self._customer_intent:
            # P2.4 意图喂下游:当轮客户意图(graph 命中 → 规则归类)。**只在全量档**
            # 渲染——slim 紧凑档的语义是「状态无实质变化」,意图属实质信息,变化即
            # +revision 逼本轮走全量档(见 set_customer_intent)。kill-switch=0 时
            # 字段恒空,本行不出现。
            parts.append("【客户意图】" + self._customer_intent)
        if self._whatsapp_note:
            parts.append(
                "【已记录客户 WhatsApp】" + self._whatsapp_note +
                "（复述号码必须逐位以此为准，不要凭记忆或猜测）"
            )
        if self._call_facts:
            # 会中事实沉淀(append-only 有界,add_call_fact):客户早轮讲过的
            # 平台/号码唔随滚动记忆/历史截断蒸发,治「重复问已答过的事」。
            parts.append(
                "【通话中客户已讲（已确认过，不要再问）】\n"
                + "\n".join(f"- {s}" for s in self._call_facts)
            )
        if self._flow_current:
            # 当前步约束(随 flow 推进而变):放尾部最前,推进只改这里、前缀字节不动。
            parts.append("【现在这一步】\n" + self._flow_current)
        if self._last_reply:
            # 重复锚(截短版,2026-09-12 P0):旧版把上一句全文引在尾部,等于把
            # 抄袭素材递到 4B 嘴边(call-8fa17d2b 两轮回复一字不差实证)。只示
            # 开头 12 字+转换性指令——全文在对话历史里,对照能力不丢;标签
            # 【你上一句】字面不变(_StripTailAnchorStream 靠它剥拟声复刻)。
            parts.append(self._last_reply_anchor())
        if self.rag_enabled and self._snippets:
            parts.append("【实时检索到的资料（知识库）】\n" + "\n".join(f"- {s}" for s in self._snippets))
        if self.rag_enabled and self._web:
            parts.append(
                "【联网检索到的资料（来源：Wikipedia/即时答案，可能过时或不准）】\n"
                + "\n".join(f"- {s}" for s in self._web)
                + "\n这些资料可作参考；若与客户问题不直接相关或不足，按「应答准则」接住客户，"
                + "不要生硬说查不到。"
            )
        if self._summary_lines:
            # 只带最近几轮记忆(默认 6):尾部每轮 prefill 只吃增量,行数是 TTFT 杠杆;
            # 更早的上下文由原始历史截断(LLM_HISTORY_TURNS)与当前步约束兜底。
            # 尾部真实预算(RAG 关,默认档) = 【现在这一步】节头+当前步文本(典型
            # ~321 字) + 【本通对话记忆】节头 + 6 行×每行 ≤201 字(≈1206 字) ≈
            # 1.55-1.6k 字/轮逐轮重 prefill;RAG 开(rag_enabled=True)另加知识
            # 2×~151 字 + 联网 1×~151 字 + 两个节头。旧注释「≤~120 token 典型」
            # 是瘦砍前口径,早已失真,以此公式为准。
            keep = max(1, int(os.environ.get("REPLY_MEMORY_LINES", "6")))
            parts.append("【本通对话记忆】\n" + "\n".join(self._summary_lines[-keep:]))
        return "\n\n".join(parts)

    def _zh_rule(self) -> str:
        return (
            "用自然口语的普通话回复，像打电话那样说，不要用书面语或播音腔；"
            "全程只用普通话词汇和语法，不夹杂粤语或其它方言词；"
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
        self._partial_capture: dict | None = None
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
            # 新请求的严格前缀」才复用。2026-09-05 INFO 取证推翻旧设计:尾部每轮拼进
            # 【最后一条 user】→ 下一轮该消息变历史、尾部被剥 → 上一轮请求与新请求在
            # 该消息处分叉 → 只有 system 锚点命中(日志 cached 恒=锚点长),对话历史
            # 每轮全量重 prefill(暖轮 TTFT 0.7-1.3s 主因)。
            # 新设计=跨轮纯追加:每个 user 消息首次出现时拼上「当时」的尾部,并记入
            # ContextState._applied_tails 账本;此后每轮原样重放(原文+当时的尾部)。
            # 上一轮请求因此永远是下一轮的严格前缀 → 每轮只 prefill 新增的那句。
            # 尾部随消息冻结(历史里每轮看到的是当时的步骤/记忆,语义自洽);
            # 历史截断(摊销式)整对丢消息,账本自尾对齐,重锚每 2×N 轮一次。
            prefix = self._ctx.render_instruction_prefix()
            if prefix:
                copy = chat_ctx.copy()
                items = list(copy.items)
                if items and isinstance(items[0], llm.ChatMessage) and items[0].role == "system":
                    head = items[0].content
                    if isinstance(head, str):
                        merged: list = [_join_system(prefix, head, "")]
                    else:
                        merged = [*([prefix] if prefix else []), *head]
                    items[0] = llm.ChatMessage(role="system", content=merged)
                else:
                    items.insert(0, llm.ChatMessage(role="system", content=[_join_system(prefix, "", "")]))
                # 截断历史(摊销式,见 _truncate_chat_items):先剪后对齐,账本自尾映射。
                # P1.3(2026-09-21,§48):缺省 8→40=**通话内不截断**——历史早已全命中
                # KV 前缀(§46.1),截断的唯一产出是前缀断裂全量重 prefill(受控实验
                # 2.5× 尖峰)+基线 qwen3_5 ArraysCache 不可 trim,截断纯亏;40 对
                # (80 条)滞回线令典型 ≤20 轮通话零截断。逃生:LLM_HISTORY_TURNS=8
                # 回旧档(env 已在 _FORWARD_ENV)。
                max_turns = int(os.environ.get("LLM_HISTORY_TURNS", "40"))
                items = _truncate_chat_items(items, max_turns=max_turns)
                # 尾部重放+新消息追加(见上)。users=当前请求里的 user 消息下标(时序序)。
                users = [i for i, it in enumerate(items) if getattr(it, "role", "") == "user"]
                applied = self._ctx.applied_tails()
                n_new = len(users) - len(applied)

                def _text_of(it) -> str:
                    c = getattr(it, "content", "")
                    if isinstance(c, str):
                        return c
                    return "".join(x for x in (c or []) if isinstance(x, str))

                def _replay(idx: int, orig: str, final: str) -> bool:
                    it = items[idx]
                    if isinstance(it, llm.ChatMessage) and _text_of(it) == orig:
                        items[idx] = llm.ChatMessage(role="user", content=[final])
                        return True
                    # 原文对不上(极端改写)→ 跳过该条,损失局部缓存也好过乱拼。
                    return False

                if n_new > 0:
                    # 旧的 applied 对应 users 前 |applied| 条(时序一致),逐条重放;
                    # 新增的尾部 user 从最后一条起各拼当前尾部并入账。
                    for k, (orig, final, _rev) in enumerate(applied):
                        _replay(users[k], orig, final)
                    for idx in users[len(applied):]:
                        it = items[idx]
                        orig = _text_of(it) if isinstance(it, llm.ChatMessage) else ""
                        tail = self._ctx.render_context_tail()
                        final = f"{orig}\n\n{tail}" if (orig and tail) else (orig or tail)
                        if tail:
                            items[idx] = llm.ChatMessage(role="user", content=[final])
                        self._ctx.record_applied_tail(orig, final)
                elif users:
                    # 无新消息的重放(抢跑重试/重建):整段原样重放,保严格前缀;唯独
                    # 尾部在最后一条 user 拼上之后已实质变化(流程推进/WhatsApp 捕获
                    # → revision +1,随后框架按快照差异作废旧抢跑并重建)时,把末条
                    # user 的尾部重渲染成当前版——否则重建请求仍带旧步骤语境,回复
                    # 照旧步讲(2026-09-06 call-e6e5f18e:已推进第 4 步仍念第 3 步)。
                    # 只改末条 user:其前序列恒定,被取代的抢跑请求已作废,无前缀
                    # 契约;下一轮真实请求以本次重建结果为前缀,链路重新闭合。
                    tail_window = applied[-len(users):] if len(applied) >= len(users) else []
                    offset = len(users) - len(tail_window)
                    last_replayed = False
                    last_orig = tail_window[-1][0] if tail_window else ""
                    for k, (orig, final, _rev) in enumerate(tail_window):
                        ok = _replay(users[offset + k], orig, final)
                        if not ok and k == len(tail_window) - 1:
                            # 转写被修正(提交文本≠账本 orig):按当前文本+当前尾部
                            # 重冻结重锚定——跳过会令该轮连尾部都丢(回复冇步骤语境),
                            # 且账本 orig 永远对不上、其后每轮 replay 全跳过。
                            actual = _text_of(items[users[offset + k]])
                            tail = self._ctx.render_context_tail()
                            rebased = f"{actual}\n\n{tail}" if (actual and tail) else (actual or tail)
                            items[users[offset + k]] = llm.ChatMessage(role="user", content=[rebased])
                            last_orig = actual
                            self._ctx.rewrite_last_applied_tail(actual, rebased)
                            ok = True
                        if k == len(tail_window) - 1:
                            last_replayed = ok
                    if last_replayed and tail_window and tail_window[-1][2] < self._ctx.revision:
                        tail = self._ctx.render_context_tail()
                        final = f"{last_orig}\n\n{tail}" if (last_orig and tail) else (last_orig or tail)
                        items[users[-1]] = llm.ChatMessage(role="user", content=[final])
                        self._ctx.rewrite_last_applied_tail(last_orig, final)
                self._ctx.prune_applied_tails(keep=len(users))
                # 剔除框架一次性步骤标记([流程状态] system):它只服务抢跑失效判定,
                # 本轮在、下轮无 → 进了请求流会令下一轮喺同一位分叉,cached 钉死
                # 锚点、每轮全量重 prefill(TTFT 1.2s→8.5s 回归根因,call-b882cd69
                # 指纹实证)。标记在框架侧已完成任务(快照比对→作废旧抢跑)。
                items = [
                    it
                    for it in items
                    if not (getattr(it, "role", "") == "system" and str(_text_of(it)).startswith("[流程状态]"))
                ]
                copy.items = items
                chat_ctx = copy
        inner_stream = self._inner.chat(
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            extra_kwargs=_forward_extra_kwargs(extra_kwargs),
        )
        # 出口剥离拟声复刻的尾部锚块(见 _StripTailAnchorStream):测试替身返回
        # 非 LLMStream(单测 _CaptureInner 返回 "ok")时原样透传。
        if isinstance(inner_stream, llm.LLMStream):
            _stripped = _StripTailAnchorStream(self, inner_stream)
            if (
                os.environ.get("BOK_REPEAT_GUARD", "1") == "1"
                and self._ctx is not None
            ):
                # 出口复读防线(2026-09-12):逐句比对上一句回复,拟声复读句剥掉
                # (call-8fa17d2b 两轮一字不差实证);客户要求重讲轮放行。
                out = _RepeatSelfGuardStream(
                    self, _stripped, self._ctx.last_reply, bypass=self._ctx.repeat_requested
                )
            else:
                out = _stripped
            # 部分文本 tee(2026-09-17,治「打断轮零账本」):把本回复已生成的文本
            # 逐段记进 agent 注入的 capture dict——回复被框架打断时(item 永不
            # added)agent 侧 speech watcher 用佢补记 gen=interrupted 账本行;
            # 正常走完自动清空(item_added 照常上报,零双记)。
            if self._partial_capture is not None:
                out = _PartialCaptureStream(self, out, self._partial_capture)
            return out
        return inner_stream

    def set_partial_capture(self, capture: dict | None) -> None:
        """注入 per-turn 部分文本 tee({"text": str})。None=关闭。"""
        self._partial_capture = capture


class _PartialCaptureStream(llm.LLMStream):
    """记下本回复已生成的文本（打断轮补记账本的数据源，agent.py 注入）。

    正常走完 → 清空 capture(item_added 照常上报);异常/取消(=框架打断)→
    保留已生成文本供 speech watcher 补记 gen=interrupted 行。壳照抄
    _StripTailAnchorStream:metrics 由内芯转发,此处只排空监视分支。
    """

    def __init__(self, plugin, inner: "llm.LLMStream", capture: dict):
        super().__init__(llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions())
        self._inner = inner
        self._capture = capture

    async def _metrics_monitor_task(self, event_aiter) -> None:
        async for _ in event_aiter:
            pass

    async def _run(self):
        try:
            async for ev in self._inner:
                delta = getattr(ev, "delta", None)
                if delta is not None:
                    content = getattr(delta, "content", None)
                    if isinstance(content, str) and content:
                        self._capture["text"] = (self._capture.get("text") or "") + content
                self._event_ch.send_nowait(ev)
        except BaseException:
            # 取消(=打断)/异常:保留部分文本,agent 侧 watcher 决定补记。
            raise
        else:
            self._capture["text"] = ""


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
    out = system_part + dialog[-(max_turns * 2) :]
    # P0.3(2026-09-21,§48 仪器化):截断=KV 严格前缀断裂,该轮全量重 prefill
    # (§46.1 受控实验 2.5× 尖峰)——先计数观测,截断策略(P1.3)按此数据定。
    print(
        f"HISTORY_TRUNCATED items={len(items)}->{len(out)} max_turns={max_turns} (KV prefix re-anchor)",
        flush=True,
    )
    return out


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

        # beep 旗标(tts_cache tee 用):合成失败兜 beep 时置位,外层据此拒绝落盘——
        # 错误提示音一旦入缓存,该行文本之后永远播 beep。多类同款实现,统一置位。
        self._emitted_beep = True
        sr = self._tts_.sample_rate
        n = int(sr * 0.4)
        pcm = bytearray()
        for i in range(n):
            v = int(12000 * math.sin(2 * math.pi * 440 * i / sr))
            pcm += v.to_bytes(2, "little", signed=True)
        output_emitter.push(bytes(pcm))
        output_emitter.flush()


def minimax_speed_for(lang: str) -> float:
    """语言档语速(zh/cantonese 1.2、其余 1.0)——用户定档:中文/粤语音频 1.0 太慢。

    MINIMAX_SPEED 显式设置=全语言 kill-switch 覆盖(回退通道保留);缺省按语言。
    垫话资产层自始即 1.2(gen_filler_assets),本函数把同档铺到运行时回复/
    pregen 物化/backfill 三处,四层语速一致。"""
    env = os.environ.get("MINIMAX_SPEED", "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return 1.2 if str(lang or "").strip().lower() in ("zh", "cantonese") else 1.0


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
    # bidi 持久连接端点（官方 t2a_v2_bidi）：一连接一整个 call,task_continue 逐字喂,
    # 服务端按句合成;打断走 task_cancel（连接保留）,唔再拆连接。
    _ENDPOINT_WS_BIDI_CN = "wss://api.minimax.cn/ws/v1/t2a_v2_bidi"
    _ENDPOINT_WS_BIDI_INTL = "wss://api.minimax.chat/ws/v1/t2a_v2_bidi"

    def _ws_voice_setting(self, voice: str) -> dict:
        """任务级 voice_setting（三条合成路径共用同一构造,避免漏 pitch/漂移）。

        emotion 由 _resolve_emotion 决定:None(自动匹配,默认)时**整个键不下发**
        ——MiniMax 枚举校验吃不了空串,且缺键=模型按文本自动选情绪。
        """
        setting: dict = {
            "voice_id": voice,
            "speed": self.resolved_speed(),
            "vol": float(os.environ.get("MINIMAX_VOL", "1")),
            "pitch": int(os.environ.get("MINIMAX_PITCH", "0")),
        }
        emotion = self._resolve_emotion()
        if emotion:
            setting["emotion"] = emotion
        return setting

    def __init__(
        self,
        *,
        voice: str | dict = "",
        language_state: LanguageState | None = None,
        sample_rate: int = 24000,
        api_key: str = "",
        emotion_state=None,
        model_override: str = "",
        language_boost: str | None = None,
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
        # 回退链第二实例用(tts.FallbackAdapter hd→turbo 同音色换档):空=读 env,
        # 与主实例同 env 会拿同一档,回退链就失去意义。B 线主实例亦经它显式下发
        # 合成档(唔写进程 env,评审 follow-up)。
        self._model_override = model_override
        # 目标语 language_boost 显式档(None=未传→透传 env;空串=显式禁用)。
        self._language_boost_override = language_boost

    def _resolve_emotion(self) -> str | None:
        """emotion 策略(2026-09-07 翻默认):不指定 → MiniMax 按文本自动匹配。

        官方文档明示「模型会根据输入文本自动匹配合适的情绪,一般无需手动指定」,
        LiveKit 官方 minimax 插件默认 emotion=None 同款姿势;我们旧的 mood→emotion
        映射(task_start 显式指定)是轮间语气跳变/不自然的放大器,已废为选入档。
        MINIMAX_EMOTION: 不设=None(自动) | map=mood 映射旧行为 | calm 等枚举值直透。
        """
        raw = os.environ.get("MINIMAX_EMOTION", "").strip().lower()
        if not raw:
            return None
        # 旧部署残留防呆:语义翻面前 "1"=开映射、"0"=关(→calm 旧版实义);
        # 新代码里直接直透会成非法枚举(4xx)。归一:1→map、0/off→自动。
        if raw == "1":
            raw = "map"
        elif raw in ("0", "off", "false"):
            return None
        if raw == "map":
            if self._emotion_state is not None:
                try:
                    return self._emotion_state.minimax_emotion()
                except Exception:  # pragma: no cover - 情绪解析失败回落自动
                    return None
            return None
        return raw

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

    def _ws_mode(self) -> str:
        """TTS 流式模式:bidi(默认,持久连接,服务端攒句) | classic(MINIMAX_WS_MODE=classic 回退)。

        默认 bidi(2026-09-07 翻默认):官方文档定位 t2a_v2_bidi 就是「LLM 流式输出
        逐 token 转语音」——服务端自动攒句防碎裂(classic 客户端攒句/overlap 半句喂
        正是「一句话连不起来、前后语气不一」根因)、task_cancel 打断后连接可续、
        task_flush 催尾句。classic 保留 env 一键回退。
        """
        mode = os.environ.get("MINIMAX_WS_MODE", "bidi").strip().lower()
        return mode if mode in ("classic", "bidi") else "bidi"

    def _endpoint_ws_bidi(self) -> str:
        """bidi WebSocket 端点:复用 classic 的 region 逻辑,路径加 _bidi 后缀。

        MINIMAX_WS_URL 显式覆盖时同样补 _bidi 后缀(已是 _bidi 结尾则原样)。
        """
        base = os.environ.get("MINIMAX_WS_URL", "").strip()
        if base:
            return base if base.endswith("_bidi") else base + "_bidi"
        region = os.environ.get("MINIMAX_REGION", "cn").strip().lower()
        return self._ENDPOINT_WS_BIDI_INTL if region in {"intl", "global", "chat"} else self._ENDPOINT_WS_BIDI_CN

    def _model(self) -> str:
        return self._model_override or os.environ.get("MINIMAX_MODEL", "speech-2.8-hd")

    def _language_boost(self) -> str:
        """目标语 language_boost(B 线构造经 language_boost 显式下发;未传=env 透传,
        空=不下发)。

        枚举值是 MiniMax API 外部字面量(术语门禁白名单单点)。旧契约=interpret
        侧写 MINIMAX_LANGUAGE_BOOST、这里只透传——常驻 worker 里写入即跨会话
        驻留(先 zh 后 en 的会话 boost 停在首通的值),改构造显式传参唔写 env
        (评审 follow-up,与采样档 P2-3 同治理);未传参的 A 线 env 注入链路
        (job 进程隔离,agent.py 有留档)零变化。
        """
        if self._language_boost_override is not None:
            return self._language_boost_override.strip()
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

    def resolved_voice(self) -> str:
        """公开只读:当前语言锚定音色(tts_cache 缓存 key 取值用,唔碰私有成员)。"""
        return self._resolve_voice()

    def resolved_speed(self) -> float:
        """公开只读:当前语言档语速(zh/粤 1.2)——tts_cache 缓存 key 的速度维度。"""
        return minimax_speed_for(getattr(self._language_state, "lang", ""))

    def resolved_model(self) -> str:
        """公开只读:当前模型档(speech-2.8-hd/turbo)——档位变更即缓存 key 全量失效。"""
        return self._model()

    def _bidi_params_key(self) -> tuple:
        """bidi task_start 参数指纹：变了就重建会话(换声/换模型)。

        emotion 刻意唔入指纹——佢逐轮 mood 变,拿佢当指纹会每轮拆连接,
        bidi 省嘅就係 connect+task_start;情绪差一档係听感问题,唔係对错问题。
        speed/vol/pitch係 env 级常量,进程内不变,一并入指纹求稳。
        """
        return (
            self._model(),
            self._resolve_voice(),
            float(os.environ.get("MINIMAX_SPEED", "1")),
            float(os.environ.get("MINIMAX_VOL", "1")),
            int(os.environ.get("MINIMAX_PITCH", "0")),
            self._continuous_sound(),
        )

    def _continuous_sound(self) -> bool:
        """continuous_sound 实验档(仅 speech-2.8-hd/turbo 生效,默认关=官方默认)。

        true=模型侧不切分文本连续推理,长文本韵律更自然;false=切分并发推理,
        延迟更低。MINIMAX_CONTINUOUS_SOUND=1 选入,韵律 vs 延迟 A/B 用。
        """
        return os.environ.get("MINIMAX_CONTINUOUS_SOUND", "0") == "1"

    def _task_start_payload(self, voice: str, sample_rate: int) -> dict:
        """bidi task_start 载荷：参数与 classic 同源（_ws_voice_setting 一套构造）。"""
        start = {
            "event": "task_start",
            "model": self._model(),
            "voice_setting": self._ws_voice_setting(voice),
            "audio_setting": {"sample_rate": sample_rate, "format": "pcm", "channel": 1},
            # 官方参数:流式不回传聚合音频,显著降尾包体积与传输耗时。
            "stream_options": {"exclude_aggregated_audio": True},
        }
        # language_boost 锁语种(B 线同传按目标语注入 env):源语音常夹第三方
        # 词,显式锁死防合成语种漂移;枚举值是 MiniMax API 外部字面量。
        boost = self._language_boost()
        if boost:
            start["language_boost"] = boost
        # continuous_sound 实验档(仅 2.8 生效):true=模型侧不切分连续推理,
        # 长文本韵律更自然。默认关(官方默认 false=切分并发,延迟低)。
        if self._continuous_sound():
            start["continuous_sound"] = True
        return start

    def _bidi_session(self) -> "_MiniMaxBidiSession":
        """每实例(=每 job)一条 bidi 会话管理器:懒创建,整个 call 一条连接。"""
        sess = getattr(self, "_bidi_sess", None)
        if sess is None:
            sess = _MiniMaxBidiSession(self)
            self._bidi_sess = sess
        return sess

    def synthesize(self, text, *, conn_options=None):
        # 保留整段合成路径：livekit 某些非 stream 调用 / 测试仍会走 synthesize。
        # 整段齐晒先落 stream——喺度套教学形拦截最稳(逐句流式只喺 send 前拦)。
        text = lecture_guard(str(text), self._speech_lang())
        return _MiniMaxTTSStream(self, text, conn_options or APIConnectOptions())

    def _speech_lang(self) -> str | None:
        """罐头回应的语言:会话锚定语言(zh/cantonese)优先,其它(如 en)留 None 自动判。"""
        lang = self._language_state.lang
        return lang if lang in ("zh", "cantonese") else None

    def prewarm(self) -> None:
        """会话开始即后台预连（bidi: 预连持久会话; classic: keep-warm 池,容量 1）。

        classic 官方 t2a_v2 WS 一连接一任务（task_finish 后服务端关连接），用过的连接
        复用唔到；但 connect（TCP+TLS 握手，实测冷 ~0.65s/暖 ~0.2s）可以提前做。
        失败静默——首段合成回退流内自连，行为同旧。
        bidi 一条连接服务整个 call：预热 = 后台 connect+task_start，首段合成零握手段。
        """
        if self._ws_mode() == "bidi":
            self._bidi_session().prewarm()
            return
        _minimax_pool_schedule(self._endpoint_ws(), self._api_key())

    def stream(self, *, conn_options=None):
        """真流式 SynthesizeStream：classic 按句 task_continue;bidi 逐字透传服务端切句。"""
        if self._ws_mode() == "bidi":
            return _MiniMaxBidiStream(self, conn_options or APIConnectOptions())
        return _MiniMaxSynthesizeStream(self, conn_options or APIConnectOptions())

    async def aclose(self) -> None:
        """job 收尾：bidi 模式关掉持久连接（classic 池连接由 GC/TTL 兜底,行为不变）。"""
        if self._ws_mode() == "bidi":
            try:
                await self._bidi_session().aclose()
            except Exception:  # noqa: BLE001 - 收尾尽力而为
                pass
        await super().aclose()


# ---- MiniMax 热连接池（keep-warm）---------------------------------------------
# 官方 t2a_v2 WS「一连接一任务」：task_finish 后服务端关连接（官方文档明示），
# 用过的连接无法复用；但 connect（TCP+TLS 握手，实测冷 ~0.65s/暖 ~0.2-0.25s）
# 可以提前做——池容量 1 放「处女连接」（只 connect、不 task_start，
# connected_success 留喺接收缓冲由取用方握手逻辑照常收），下次合成直接取用，
# 免整个握手段。failure-safe：取池验 key/endpoint 匹配 + TTL + open 状态，
# 握手失败弃池全新重连一次；任何取唔到/失败都回退流内自连（旧行为）。
# MINIMAX_WS_POOL=0 关闭（恒走流内自连）。
_MINIMAX_POOL_WS = None  # 热连接（websockets 客户端实例）
_MINIMAX_POOL_KEY: tuple[str, str] | None = None  # 入池时 (endpoint, api_key)
_MINIMAX_POOL_AT = 0.0  # 入池时刻（monotonic）
_MINIMAX_POOL_TTL_S = 240.0  # 超龄弃用（服务端对空闲连接的生命周期未文档化）
_MINIMAX_POOL_TASK: asyncio.Task | None = None  # 补池任务（单飞）


def _minimax_pool_enabled() -> bool:
    return os.environ.get("MINIMAX_WS_POOL", "1") == "1"


async def _minimax_ws_silent_close(ws) -> None:
    try:
        await ws.close()
    except Exception:  # noqa: BLE001 - 收尾尽力而为
        pass


async def _minimax_classic_reconnect(tts, key: str, old_ws, handshake, sent_all: list) -> object:
    """classic 流 STALL 重连动作序列：闭旧连 → 新连 → 握手 → 按序重发已发文本。

    抽成模块级函数以便单测（synthesize 闭包内不可测）。任何一步抛错原样上抛——
    调用方（_stall_watch）必须 try/finally 清 _reconnecting 闸：闸悬挂会让所有
    发送点 `await _reconnecting.wait()` 永挂、整轮静默（2026-09-17 全量 debug
    F1/F2——classic 流缺 bidi 三件自愈的同款守卫，此处补齐动作序列单点）。
    """
    import websockets  # 与 synthesize 内一致:局部导入(websockets 启动重)

    await _minimax_ws_silent_close(old_ws)
    ws = await websockets.connect(
        tts._endpoint_ws(),
        additional_headers={"Authorization": f"Bearer {key}"},
        open_timeout=10,
        max_size=20_000_000,
    )
    await handshake(ws)
    for s in sent_all:
        await ws.send(json.dumps({"event": "task_continue", "text": s}))
    return ws


def _minimax_pool_discard(ws) -> None:
    """后台弃置池连接；无事件循环时直接放手（GC 兜底回收 socket）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    # 强引用入池(P2-A,2026-09-17):裸 create_task 只被事件循环弱引用,GC 中途
    # 回收=弃置关闭静默丢失、池连接半开残留。
    _spawn_bg(_minimax_ws_silent_close(ws))


def _minimax_pool_pop(endpoint: str, key: str):
    """取热连接；无池/参数变/超龄/已闭 → None（调用方回退全新连接）。"""
    global _MINIMAX_POOL_WS, _MINIMAX_POOL_KEY, _MINIMAX_POOL_AT
    ws = _MINIMAX_POOL_WS
    pooled_key = _MINIMAX_POOL_KEY
    pooled_at = _MINIMAX_POOL_AT
    _MINIMAX_POOL_WS = None
    _MINIMAX_POOL_KEY = None
    _MINIMAX_POOL_AT = 0.0
    if ws is None:
        return None
    if pooled_key != (endpoint, key) or (time.monotonic() - pooled_at) > _MINIMAX_POOL_TTL_S:
        _minimax_pool_discard(ws)
        return None
    try:
        from websockets.protocol import State

        if getattr(ws, "state", State.OPEN) != State.OPEN:
            _minimax_pool_discard(ws)
            return None
    except Exception:  # noqa: BLE001 - 判不了状态就信任之（握手失败另有回退）
        pass
    return ws


async def _minimax_pool_replenish(endpoint: str, key: str) -> None:
    """后台预连一条 WS 入池（容量 1）。失败静默——合成路径自会回退流内连接。"""
    global _MINIMAX_POOL_WS, _MINIMAX_POOL_KEY, _MINIMAX_POOL_AT, _MINIMAX_POOL_TASK
    try:
        if _MINIMAX_POOL_WS is not None:
            return
        import websockets

        t_conn0 = time.monotonic()
        ws = await websockets.connect(
            endpoint,
            additional_headers={"Authorization": f"Bearer {key}"},
            open_timeout=10,
            max_size=20_000_000,
        )
        connect_ms = (time.monotonic() - t_conn0) * 1000
        # connected_success 唔消费——留喺接收缓冲，取用方握手逻辑照常先收它。
        _MINIMAX_POOL_WS = ws
        _MINIMAX_POOL_KEY = (endpoint, key)
        _MINIMAX_POOL_AT = time.monotonic()
        print(f"MINIMAX_TTS_WS_POOL_PREWARM connect_ms={connect_ms:.0f}", flush=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 预热尽力而为
        print(f"MINIMAX_TTS_WS_POOL_PREWARM_FAIL {exc!r}", flush=True)
    finally:
        _MINIMAX_POOL_TASK = None


def _minimax_pool_schedule(endpoint: str, key: str) -> None:
    """空闲期补池（会话开始/每次合成收尾调用）。已在补/已有池/无 key/开关关 → 跳过。"""
    global _MINIMAX_POOL_TASK
    if not _minimax_pool_enabled() or not key or not endpoint:
        return
    if _MINIMAX_POOL_TASK is not None or _MINIMAX_POOL_WS is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 无事件循环（调用点都喺 loop 内，理论不可达）
        return
    _MINIMAX_POOL_TASK = loop.create_task(_minimax_pool_replenish(endpoint, key))


# 精简繁→简映射(词表回声守卫专用,2026-09-11 call-a2705ed2 实证:ASR 抄词表时
# 用了繁体「顺豐速運/賠償/單號」,词表是简体,逐字匹配断链)。覆盖商务中文常见
# 繁简差集字符;未映射字符原样透传——两比对侧同表归一,不依赖外部转换库。
_T2S_PAIRS = (
    "豐丰 運运 賠赔 償偿 單单 號号 費费 專专 員员 時时 蹤踪 實实 門门 倉仓"
    " 遞递 關关 係系 貨货 遺遗 請请 圖图 們们 個个 來来 裡里 後后 點点 問题"
    " 題题 發发 現现 經经 過过 務务 業业 確确 認认 訊讯 聯联 絡络 轉转 帳账"
    " 匯汇 錢钱 銀银 電电 訂订 額额 價价 樣样 麼么 嗎吗 與与 還还 這这 東东"
    " 車车 長长 頁页 雲云 絲丝 網网 讓让 討讨 傳传 嘆叹 觀观 覺觉 覽览 識识"
    " 計计 議议 記记 講讲 證证 許许 設设 訪访 評评 詞词 試试 誠诚 語语 誤误"
    " 說说 諸诸 讀读 課课 調调 謹谨 負负 貢贡 財财 責责 賢贤 敗败 質质 買买"
    " 賣卖 賺赚 賽赛 贈赠 輸输 達达 遠远 運运 較较 辦办 為为 風风 飛飞 馬马"
    " 鳥鸟 貝贝 開开 閉闭 閑闲 間间 鬧闹 聞闻 閱阅 陽阳 陰阴 陣阵 陳陈 險险"
    " 隨随 隱隐 難难 雙双 發发 戶户 據据 購购 輸运 輸输 國国 際际 韓韩 愛爱"
    "爾尔"
    " 順顺 寶宝 亞亚 遜逊"
)
_T2S_MAP = {ord(tok[0]): tok[1] for tok in _T2S_PAIRS.split() if len(tok) == 2}


def _to_simp(s: str) -> str:
    """繁→简(仅守卫比对用:逐字映射,未覆盖字符透传;与词表/转写两侧同表归一)。"""
    try:
        return str(s or "").translate(_T2S_MAP)
    except Exception:
        return str(s or "")


def _vocab_words_from_context(hotword_context: str) -> set[str]:
    """词表上下文 → 归一词集(去 Vocabulary: 头、剥标点、繁→简)。"""
    words: set[str] = set()
    for piece in re.split(r"[,，、;；\s]+", str(hotword_context or "")):
        piece = piece.replace("Vocabulary:", "").replace("Vocabulary：", "").strip()
        piece = re.sub(r"[^\w\u4e00-\u9fff]+", "", _to_simp(piece))
        if piece:
            words.add(piece)
    return words


def _is_hotword_vocab_echo(text: str, hotword_context: str) -> bool:
    """ASR 热词幻听判定(纯函数,单测用):极低内容音频把词表当转写整串抄出。

    2026-09-08/09 实机两连回归(call-feaf914c/dd40727c):开场白期间客户没说话,
    滑窗把「單號，運單，賠償…」词表顺串解成转写,字幕/轮次全被污染。判定:
    转写剥标点后**完全由词表词首尾相接组成**(贪心最长匹配全覆盖)且总长 ≥6
    ——真实用户话必有虚词/数字/词表外内容,不可能恰好全是词表词的顺串。
    STT 源头(字幕/句级提交/停嘴 FINAL)与 agent hook 双层共用本判定。
    2026-09-11:比对两侧同经 _to_simp(繁简归一)——call-a2705ed2 实证回声可被
    ASR 用繁体抄出,旧简体逐字匹配在首词即断链。
    """
    norm = re.sub(r"[^\w\u4e00-\u9fff]+", "", _to_simp(str(text or "")))
    if len(norm) < 6 or not hotword_context:
        return False
    words = _vocab_words_from_context(hotword_context)
    if not words:
        return False
    remaining = norm
    while remaining:
        hit = next((w for w in sorted(words, key=len, reverse=True) if w and remaining.startswith(w)), None)
        if hit is None:
            return False  # 有一段唔係词表词 → 真人话,唔拦
        remaining = remaining[len(hit):]
    return True


_VOCAB_ECHO_MIN_RUN = 4


def _strip_vocab_echo_tail(text: str, hotword_context: str) -> str:
    """剥离词表回声**尾部**、保住真话头(2026-09-11 call-a2705ed2 实证形态:
    「拼多多。顺豐速運，運通，理賠…」——真答案词恰在词表里,全有全无丢弃会
    连真实回答一起丢掉)。

    规则:按逗/顿/分号切段,从尾往头收「归一后整段 ∈ 词表词集」的连续尾段;
    收满 ≥_VOCAB_ECHO_MIN_RUN 段才剥(<4 段可能是平台选择类真实回答);整条
    全是回声 → 空串(调用方按噪声轮丢弃)。繁简两侧归一后比对。"""
    raw = str(text or "")
    if not raw or not hotword_context:
        return raw
    words = _vocab_words_from_context(hotword_context)
    if not words:
        return raw
    # 尾随分隔符剥掉(否则 split 产生尾空段,把回声尾的回收循环在第一步就打断)。
    # 分段含句读符(。！？)——回声可从句中开始(「拼多多。顺豐速運,運通…」实证:
    # 头段=真话+首个回声词同段,不按句读切就剥不干净)。
    _SEP = "[.。！？!,，、;；]"
    _SENT_FINAL = set(".。！？!?")
    trimmed = re.sub(rf"{_SEP}+[ \t]*$", "", raw)
    parts = re.split(rf"({_SEP})", trimmed)
    # split with capture → [seg, sep, seg, sep, ...];按段收集(尾段无分隔符)
    segments: list[str] = [p for i, p in enumerate(parts) if i % 2 == 0]
    seps: list[str] = [p for i, p in enumerate(parts) if i % 2 == 1]
    # 从尾回收词表段,但**不跨句界**:真话以句读收尾(「拼多多。」),回声串是
    # 逗号粘合的词表顺串——句读左边的完整句子是真人话,哪怕它恰好是词表词
    # (拼多多在词表里)也不能剥。
    run = 0
    for idx in range(len(segments) - 1, -1, -1):
        if idx < len(segments) - 1 and seps[idx] in _SENT_FINAL:
            break  # 左侧是完整句子,句界止步
        norm = re.sub(r"[^\w\u4e00-\u9fff]+", "", _to_simp(segments[idx]))
        if norm and norm in words:
            run += 1
        else:
            break
    if run < _VOCAB_ECHO_MIN_RUN:
        return raw  # 未命中回声:原文原样(含尾标点)——2026-09-12 call-aa86dfc9
        # 实证旧版误把剥过尾标点的 trimmed 返回,每轮都误改文本+误打 ECHO_STRIP。
    keep = len(segments) - run
    if keep <= 0:
        return ""
    out = segments[0]
    for i in range(1, keep):
        out += seps[i - 1] + segments[i]
    return out


def _is_lone_vocab_word(text: str, hotword_context: str) -> bool:
    """整条文本归一后恰好是单个词表词(词表残片形态)——call-1043de7c 第三轮
    实证:回声衰落成只抄出词表首词「顺豐速運」,无尾可剥、整条当正常轮过关。"""
    norm = re.sub(r"[^\w\u4e00-\u9fff]+", "", _to_simp(str(text or "")))
    if len(norm) < 2 or not hotword_context:
        return False
    return norm in _vocab_words_from_context(hotword_context)


# 纯犹豫残片(2026-09-12 call-46b94ebd「呃呃呃呃」成轮):剥尾余头/全新短段只由
# 语气字组成(呃/啊/哦…,不含 嗯/好/係/对——单字应承是合法确认轮),≥2 字即不成
# 轮——成轮必发垫话+生成 LLM+打断在途回复(canceled=1 三连的卡死体感)。
_HESITATION_CHARS = "呃啊哦噢唉诶嘛呗咯哼呣"
_HESITATION_RE = re.compile(r"^(?:[" + _HESITATION_CHARS + r"])+$")


def _pure_hesitation(text: str) -> bool:
    """净文(去标点空白)全部由犹豫语气字组成且 ≥2 字 → 纯犹豫不成轮。"""
    norm = re.sub(r"[^\w\u4e00-\u9fff]+", "", str(text or ""))
    return len(norm) >= 2 and bool(_HESITATION_RE.match(norm))


def _vocab_echo_guard(
    text: str, hotword_context: str, *, echo_seen: bool
) -> tuple[str, bool]:
    """词表回声守卫统一入口(纯函数,状态由调用方持有):返回 (净文, 新 echo_seen)。

    三层:①剥尾保头(真话头+词表尾,见 _strip_vocab_echo_tail);②剥动或纯回声
    → 置 echo_seen(本通已确认回声事件);③echo_seen 后的**词表单词残片**也丢弃
    ——首次出现的单词词表词保留(真人可能真讲「微信」),但同通已抄过整条词表
    之后再来孤词,是回声衰落残片(call-1043de7c「顺豐速運」×3 实证)。"""
    stripped = _strip_vocab_echo_tail(text, hotword_context)
    if stripped != text:
        return stripped, True
    # 无分隔符的纯顺串(「單號運單賠償」)剥尾看不见结构,用贪心全覆判定整条丢
    # ——**只在无分隔符时**进此门:有分隔符的短串(「拼多多，京东。」)剥尾的
    # <4 段宽容已判保留,纯回声判定会误杀平台选择类真实回答。
    if not re.search(r"[.。！？!,，、;；]", str(text or "")) and _is_hotword_vocab_echo(
        text, hotword_context
    ):
        return "", True
    if echo_seen and _is_lone_vocab_word(text, hotword_context):
        return "", True
    return text, echo_seen


def _trim_lead_silence(
    pcm: bytes,
    sample_rate: int,
    *,
    max_ms: int = 200,
    fade_ms: int = 15,
    rms_gate: int = 120,
) -> tuple[bytes, int]:
    """剪掉 PCM(s16le mono) 头部静音；剪过才对新的起始做 fade_ms 线性淡入。

    MiniMax 首包偶带前导静音，剪掉=可闻出声更早；剪口做淡入防咔哒声
    （RealtimeTTS base_engine 的 trim_silence_start/apply_fade_in 同款，2026-09-07
    借鉴）。max_ms 上限防误剪气声起句（RMS 低但係真语音）；一次调用最多剪
    max_ms，残余静音留给下次首帧检查继续剪。零静音时原样返回（字节不变，
    零害）。返回 (处理后 pcm, 剪掉的毫秒)。
    """
    frame = max(1, sample_rate // 50) * 2  # 20ms 字节数(s16le mono)
    buf = pcm
    trimmed_ms = 0
    while len(buf) >= frame and trimmed_ms < max_ms:
        n = frame // 2
        acc = 0
        for i in range(n):
            v = int.from_bytes(buf[i * 2 : i * 2 + 2], "little", signed=True)
            acc += v * v
        if (acc / n) ** 0.5 >= rms_gate:
            break
        buf = buf[frame:]
        trimmed_ms += 20
    if not trimmed_ms:
        return pcm, 0
    nf = max(1, sample_rate * fade_ms // 1000)
    body = bytearray(buf)
    for i in range(min(nf, len(body) // 2)):
        v = int.from_bytes(body[i * 2 : i * 2 + 2], "little", signed=True)
        v = int(v * (i + 1) / nf)
        body[i * 2 : i * 2 + 2] = max(-32768, min(32767, v)).to_bytes(2, "little", signed=True)
    return bytes(body), trimmed_ms


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

        # beep 旗标(tts_cache tee 用):合成失败兜 beep 时置位,外层据此拒绝落盘——
        # 错误提示音一旦入缓存,该行文本之后永远播 beep。多类同款实现,统一置位。
        self._emitted_beep = True
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

        t0 = time.monotonic()
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

            # 热连接池：空闲期预连的「处女连接」直接用（免 TCP+TLS 握手，实测冷
            # ~0.65s/暖 ~0.2s）；无池/状态异常一律回退流内自连（旧行为）。
            ws = None
            ws_from_pool = False
            if _minimax_pool_enabled():
                ws = _minimax_pool_pop(self._tts_._endpoint_ws(), key)
                ws_from_pool = ws is not None
            if ws is None:
                ws = await websockets.connect(
                    self._tts_._endpoint_ws(),
                    additional_headers={"Authorization": f"Bearer {key}"},
                    open_timeout=10,
                    max_size=20_000_000,
                )
            ws_connect_ms = 0.0 if ws_from_pool else (time.monotonic() - t0) * 1000
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

        async def _handshake(a_ws) -> dict:
            """connected_success（丢帧不致命，同旧语义）→ task_start → task_started。

            池连接的 connected_success 已喺接收缓冲，recv 即回零延迟。
            """
            try:
                await asyncio.wait_for(a_ws.recv(), timeout=10)
            except Exception:
                pass
            start = {
                "event": "task_start",
                "model": self._tts_._model(),
                "voice_setting": self._tts_._ws_voice_setting(voice),
                "audio_setting": {"sample_rate": sample_rate, "format": "pcm", "channel": 1},
                # 官方参数:流式不回传聚合音频,显著降尾包体积与传输耗时。
                "stream_options": {"exclude_aggregated_audio": True},
            }
            # language_boost 锁语种(B 线同传按目标语注入 env):源语音常夹第三方
            # 词,显式锁死防合成语种漂移;枚举值是 MiniMax API 外部字面量。
            boost = self._tts_._language_boost()
            if boost:
                start["language_boost"] = boost
            await a_ws.send(json.dumps(start))
            return json.loads(await asyncio.wait_for(a_ws.recv(), timeout=15))

        init_done = False
        first_pushed = False
        frame_bytes = int(sample_rate / 5) * 2  # 200ms 帧
        buf = bytearray()
        recv_task: asyncio.Task | None = None
        closed_ws = asyncio.Event()
        t_start = time.monotonic()
        try:
            try:
                resp = await _handshake(ws)
            except asyncio.CancelledError:
                raise
            except Exception:
                if not ws_from_pool:
                    raise
                # 池连接空闲期被服务端静默关闭 → 弃池，全新连接重握手一次
                # （再失败就走下方 START 失败路径 beep，同旧行为）。
                print("MINIMAX_TTS_WS_POOL_STALE retry_fresh", flush=True)
                await _minimax_ws_silent_close(ws)
                t_fresh = time.monotonic()
                ws = await websockets.connect(
                    self._tts_._endpoint_ws(),
                    additional_headers={"Authorization": f"Bearer {key}"},
                    open_timeout=10,
                    max_size=20_000_000,
                )
                ws_from_pool = False
                ws_connect_ms = (time.monotonic() - t_fresh) * 1000
                resp = await _handshake(ws)
            t_task = time.monotonic()
            if resp.get("event") != "task_started":
                print("MINIMAX_TTS_WS_START_FAIL", str(resp)[:200], flush=True)
                await _minimax_ws_silent_close(ws)
                try:
                    await self._emit_beep(output_emitter)
                except Exception:  # pragma: no cover
                    pass
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("MINIMAX_TTS_WS_START", repr(exc), flush=True)
            await _minimax_ws_silent_close(ws)
            try:
                await self._emit_beep(output_emitter)
            except Exception:  # pragma: no cover
                pass
            return

        try:
            async def _recv_loop():
                nonlocal init_done, buf, first_pushed
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
                            # 首包前导静音修剪(RealtimeTTS trim_silence_start 同款):
                            # MiniMax 首包偶带前导静音,剪掉=可闻出声更早;剪口
                            # fade-in 防咔哒。全静音(剪空)→ 唔推唔置位,等下一块
                            # 再检查;每次调用最多剪 max_ms,残余静音逐次收。
                            trimmed, trim_ms = _trim_lead_silence(bytes(buf), sample_rate)
                            buf.clear()
                            buf.extend(trimmed)
                            if trim_ms:
                                print(f"MINIMAX_TTS_LEAD_SILENCE_TRIM ms={trim_ms}", flush=True)
                            if not buf:
                                continue
                            # P0 首帧早推:不足 200ms 先推 ~40ms,早出声(照 Qwen3-TTS 同款)。
                            # P0 秒表:首个音频块推送时刻(距 task_start)。
                            t_first = time.monotonic()
                            print(
                                f"TTS_FIRST_AUDIO_MS {(t_first - t_start) * 1000:.0f}",
                                flush=True,
                            )
                            # P1.5 FIX 3 首包分解:握手段(池命中=0,已在空闲期摊销)/
                            # task_start→首音频段/连接起全程。
                            print(
                                f"MINIMAX_TTS_WS_PERF pool={'hit' if ws_from_pool else 'fresh'} "
                                f"ws_connect_ms={ws_connect_ms:.0f} "
                                f"task_start_to_audio_ms={(t_first - t_task) * 1000:.0f} "
                                f"total_ms={(t_first - t0) * 1000:.0f}",
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

            # ---- 卡死自愈（W0.5，2026-09-06；同日二修计时起点）：MiniMax 偶发
            # task_started 后长时无首包（实测 >8s，期间心跳顶替=「每问无答」体感）。
            # 看门狗从【首段文本发出】起计时——旧版从 task_started 起算,慢 LLM 轮
            # (冷启 TTFT 2.1-3.4s+凑句 0.3-0.9s)会在云侧无故障时误触发,还对空
            # sent_all 白重连。未出首包 → 断开旧 ws、重开一条并重发已发文本,
            # 一次性自愈;已出首包则不干预。暖首包实测 0.25-0.5s,4s 阈值余量 >8x。
            # MINIMAX_FIRST_AUDIO_TIMEOUT_S 可调（默认4,旧6）。
            stall_timeout = float(os.environ.get("MINIMAX_FIRST_AUDIO_TIMEOUT_S", "4"))
            stalled = False
            sent_all: list[str] = []
            closed_ws = asyncio.Event()
            # 重连窗口发送闸:_stall_watch 换连接期间,输入循环若继续 task_continue
            # 会发到已闭旧 ws(或未 task_started 的新 ws)→ MINIMAX_TTS_WS_ERR 整轮
            # 音频丢失。所有发送点在闸亮时等它清——注意必须先 is_set() 再 wait()
            # (Event.wait() 对未 set 的事件会挂起等 set,无条件 await 会把正常轮
            # 的首句卡到重连后,实测首包 +1.2s)。闸亮时间=重连 ~0.2-0.65s,文本
            # 排队不丢不乱序;闸清后积压重发完,新句子按序跟进。
            _reconnecting = asyncio.Event()
            t_first_text = 0.0

            def _note_first_send() -> None:
                nonlocal t_first_text
                if t_first_text == 0.0:
                    t_first_text = time.monotonic()

            async def _stall_watch() -> None:
                nonlocal ws, stalled, recv_task, init_done, first_pushed, t_task, t_start, t_first_text
                while not first_pushed:
                    await asyncio.sleep(0.5)
                    if (
                        closed_ws.is_set()
                        or t_first_text == 0.0
                        or time.monotonic() - t_first_text <= stall_timeout
                    ):
                        continue
                    print(
                        f"MINIMAX_TTS_STALL no_first_audio_{stall_timeout:.0f}s_after_first_text -> reconnect_retry",
                        flush=True,
                    )
                    stalled = True
                    _reconnecting.set()
                    try:
                        ws = await _minimax_classic_reconnect(
                            self._tts_, key, ws, _handshake, sent_all)
                        t_task = time.monotonic()
                        t_start = t_task
                        init_done = False
                        first_pushed = False
                        t_first_text = time.monotonic()
                        recv_task = asyncio.create_task(_recv_loop())
                    except Exception as exc:
                        # 重连失败也必须清闸:不清则发送点 await _reconnecting.wait()
                        # 永挂整轮静默;清闸后旧 ws send 抛 ConnectionClosed,走既有
                        # WS_ERR 有界收尾(轮次失败 ≠ 进程 hang)。
                        print(f"MINIMAX_TTS_STALL reconnect_failed {exc!r}", flush=True)
                    finally:
                        _reconnecting.clear()
                    return

            stall_task = asyncio.create_task(_stall_watch())

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
                        _note_first_send()
                        if _reconnecting.is_set():
                            await _reconnecting.wait()
                        await ws.send(
                            json.dumps({"event": "task_continue", "text": lecture_canned(self._tts_._speech_lang())})
                        )
                        sent_any = True
                    return
                if is_lecture_text(s):
                    return  # 已触发过,课程延续句照丢
                _note_first_send()
                if _reconnecting.is_set():
                    await _reconnecting.wait()
                await ws.send(json.dumps({"event": "task_continue", "text": s}))
                sent_all.append(s)
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
                        _note_first_send()
                        if _reconnecting.is_set():
                            await _reconnecting.wait()
                        await ws.send(
                            json.dumps({"event": "task_continue", "text": lecture_canned(self._tts_._speech_lang())})
                        )
                elif not self._lecture_fired:
                    _note_first_send()
                    if _reconnecting.is_set():
                        await _reconnecting.wait()
                    await ws.send(json.dumps({"event": "task_continue", "text": _inject_pauses(sent_buf.strip())}))
            # 文本结束:发 task_finish 让服务端吐完剩余音频并回 is_final
            try:
                if _reconnecting.is_set():
                    await _reconnecting.wait()
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
            # 停看门狗:closed_ws 此前从未被 set(死代码),且 stall_task 从不 cancel
            # ——barge-in 提前结束流后看门狗存活,4s 后重开 WS 重发文本,孤儿合成
            # 会话白烧云配额(2026-09-17 全量 debug F2)。双保险:set 停判定 + cancel。
            closed_ws.set()
            if stall_task:
                stall_task.cancel()
                try:
                    await stall_task
                except asyncio.CancelledError:
                    pass  # 自己 cancel 嘅——继续收尾
                except Exception:
                    pass
            if recv_task:
                recv_task.cancel()
                try:
                    await recv_task
                except asyncio.CancelledError:
                    pass  # 自己 cancel 嘅（唔係外层取消）——继续收尾，唔好吞掉收尾步骤
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
            # 一连接一任务（官方 t2a_v2：task_finish 后服务端关连接）：本连接已
            # 耗尽，后台补一条热连接给下一段合成（MINIMAX_WS_POOL=0 时不动作）。
            _minimax_pool_schedule(self._tts_._endpoint_ws(), self._tts_._api_key())


# ---- MiniMax bidi 持久连接（t2a_v2_bidi,MINIMAX_WS_MODE=bidi 选入）--------------
# 官方 bidi 语义（docs 校对）：connect → connected_success → task_start(参数同
# classic) → task_started → task_continue{text} 可任意粒度(逐字都行),服务端
# 累积文本、按句末标点立即合成(次级标点要够长才切/长度上限强制切/空闲兜底) →
# sentence_start / task_continued{data.audio hex, is_final=该句音频完} /
# sentence_end。收尾 task_flush 强制吐出无标点尾巴,回 task_flushed,会话继续;
# 只有 call 结束才 task_finish(服务端吐完剩余后关连接)。
# 打断 = task_cancel：服务端丢缓冲文本+停当前合成 → task_canceled,连接回到
# task_started 态可继续 task_continue——替代 classic 的拆连接做法。
# 服务端永不 ping;客户端要定期 ping(空闲 >120s → 2201 断连),此处 60s 一发。
# 2205 = 软背压：稍后原样重发同一条 task_continue,唔好重连;2204 单条 >10k 字
# 跳过;2206 重复 task_start 会关连接。一条连接一个合成会话。
class _MiniMaxBidiSession:
    """每 TTS 实例(=每 job)一条 bidi 连接的生命周期管理。

    框架对每个 speech 串行开一条 SynthesizeStream(一通电话同一时刻只有一路
    TTS),本类仍用 asyncio.Lock 兜底强制串行。连接懒建(或 prewarm 预建),
    task_start 一次,之后每个流只做 task_continue/task_flush/task_cancel。
    """

    def __init__(self, tts_: "MiniMaxTTS"):
        self._tts_ = tts_
        self._lock = asyncio.Lock()
        self._ws = None
        self._params: tuple | None = None  # 运行中 task_start 的参数指纹
        self._ping_task: asyncio.Task | None = None
        self._ping_misses = 0  # pong 连失计数(连失达上限强断重预热)
        self._prewarm_task: asyncio.Task | None = None
        # ping 连失强断甩出的孤儿 invalidate task——必须持引用:事件循环对 task 只持
        # 弱引用(裸 create_task 即弃可被 GC 中途回收),aclose 也要看得见先取消它。
        self._invalidate_task: asyncio.Task | None = None
        # 残留音频门禁（epoch 纪元）：连接打断后保留复用，上一流 cancel 超时
        # （MINIMAX_TTS_BIDI_CANCEL_TIMEOUT）时服务端可能没停稳，迟到的音频会落
        # 在同一条连接上。流在首个 task_continue 才认领 active_epoch=own_epoch；
        # 认领前 recv_loop 收到的一切音频都是上一流的残留 → 丢弃
        # （MINIMAX_BIDI_DROP_STALE），唔会漏进下一个流的 emitter。0=尚无认领。
        self.active_epoch = 0
        self._epoch_seq = 0
        # 供 PERF 打点:ensure_ready 本次是复用还是新连
        self.last_reused = False
        self.last_connect_ms = 0.0
        # prewarm 失败重试计数(官方 #6969 姿势,上限 1):连接成功即清零
        self._prewarm_retries = 0

    def alloc_epoch(self) -> int:
        """为本流分配纪元号（只占号，唔认领——认领发生在首个 task_continue）。"""
        self._epoch_seq += 1
        return self._epoch_seq

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    @staticmethod
    def _ping_interval() -> float:
        try:
            v = float(os.environ.get("MINIMAX_BIDI_PING_S", "60"))
        except Exception:  # pragma: no cover - 配错回默认
            v = 60.0
        return v if v > 0 else 0.0  # <=0 关闭自管 ping

    @staticmethod
    def _ping_max_miss() -> int:
        try:
            v = int(os.environ.get("MINIMAX_BIDI_PING_MAX_MISS", "2"))
        except Exception:  # pragma: no cover - 配错回默认
            v = 2
        return v if v > 0 else 2  # 非正数=配错回默认(0 会逢超时即断)

    def _alive(self) -> bool:
        if self._ws is None:
            return False
        try:
            from websockets.protocol import State

            return getattr(self._ws, "state", State.OPEN) == State.OPEN
        except Exception:  # noqa: BLE001 - 判不了状态就信任之
            return True

    async def _ping_loop(self, ws) -> None:
        """官方:服务端永不 ping,空闲 >120s 断连 → 客户端 60s 一发 WS ping。"""
        try:
            while self._ws is ws:
                interval = self._ping_interval()
                if interval <= 0:
                    return
                await asyncio.sleep(interval)
                try:
                    # ping 帧发出即算;不 await pong 到天荒地老——
                    # pong 静默由接收侧(读消息超时/ConnectionClosed)判死。
                    await asyncio.wait_for(ws.ping(), timeout=10)
                except asyncio.TimeoutError:
                    self._ping_misses += 1
                    print(f"MINIMAX_TTS_BIDI_PING_TIMEOUT miss={self._ping_misses}", flush=True)
                    if self._ping_misses >= self._ping_max_miss():
                        print("MINIMAX_TTS_BIDI_DEAD ping连失 — 强断重预热", flush=True)
                        # 唔可以在这里 await invalidate:invalidate 会 _stop_ping() 自cancel
                        # 当前 ping task,后续 close/prewarm 会在首个让出点(生产 close 的
                        # I/O)被 CancelledError 掀掉——强断重预热静默蒸发。甩独立 task 跑;
                        # 引用必须存 self._invalidate_task(loop 对 task 只持弱引用,裸即弃
                        # 可被 GC 中途回收;aclose 也要看得见它先取消,防 teardown 后重预热)。
                        self._invalidate_task = asyncio.get_running_loop().create_task(
                            self.invalidate(reprewarm=True)
                        )
                        return
                except Exception:
                    return  # 连接已死,接收侧会 invalidate
                else:
                    self._ping_misses = 0
        except asyncio.CancelledError:
            raise

    def _start_ping(self, ws) -> None:
        self._stop_ping()
        if self._ping_interval() > 0:
            self._ping_task = asyncio.get_running_loop().create_task(self._ping_loop(ws))

    def _stop_ping(self) -> None:
        task = self._ping_task
        self._ping_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _connect_and_start(self, params: tuple) -> None:
        import websockets

        t0 = time.monotonic()
        # ping_interval=None:关掉库自带 20s keepalive(它 ping_timeout 无 pong 会
        # 主动拆线),改用本类 60s 自管 ping,符合官方「客户端负责 ping」语义。
        ws = await websockets.connect(
            self._tts_._endpoint_ws_bidi(),
            additional_headers={"Authorization": f"Bearer {self._tts_._api_key()}"},
            open_timeout=10,
            max_size=20_000_000,
            ping_interval=None,
        )
        self.last_connect_ms = (time.monotonic() - t0) * 1000
        try:
            # connected_success:丢帧不致命,同 classic 语义
            await asyncio.wait_for(ws.recv(), timeout=10)
        except Exception:
            pass
        voice = self._tts_._resolve_voice()
        await ws.send(json.dumps(self._tts_._task_start_payload(voice, self._tts_.sample_rate)))
        resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        if resp.get("event") != "task_started":
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"MINIMAX bidi task_start failed: {str(resp)[:200]}")
        self._ws = ws
        self._params = params
        self._start_ping(ws)
        self._prewarm_retries = 0  # 连接成功,重试计数归零

    async def _synth_warmup(self, ws) -> None:
        """合成级预热:连接建好后立刻做一次真实合成把服务端会话焐热,音频全丢。
        task_continue 用顶层 text 字段(同生产发送代码,唔係 data 嵌套);收包至
        task_flushed 止,单次 recv 8s 上限防挂死。自身异常只打日志不 invalidate——
        预热文本合成失败≠连接坏,残留音频由首个真实流的纪元门禁兜底丢弃。"""
        try:
            t0 = time.monotonic()
            await ws.send(json.dumps({"event": "task_continue", "text": "好的，您稍等。"}))
            await ws.send(json.dumps({"event": "task_flush"}))
            while True:  # 丢弃音频至 task_flushed(上限 8s);纪元门禁由首个真实流兜底
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                if json.loads(raw).get("event") == "task_flushed":
                    break
            print(
                f"MINIMAX_BIDI_SYNTH_WARMUP ms={(time.monotonic() - t0) * 1000:.0f}",
                flush=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 预热失败≠连接坏,唔 invalidate
            print(f"MINIMAX_BIDI_SYNTH_WARMUP_FAIL {exc!r}", flush=True)

    async def ensure_ready(self):
        """返回可 task_continue 的连接（调用方已持 lock）。

        复用条件：连接活着且参数指纹没变。参数变了（换声/换模型）→ task_finish
        干净收旧任务再全新重连（官方 2206:重复 task_start 会关连接,唔可以偷懒复用）。
        """
        params = self._tts_._bidi_params_key()
        self.last_reused = False
        if self._alive() and self._params == params:
            self.last_reused = True
            return self._ws
        if self._alive():
            await self._graceful_finish()
        await self._connect_and_start(params)
        return self._ws

    async def _graceful_finish(self) -> None:
        ws = self._ws
        if ws is not None:
            try:
                await asyncio.wait_for(ws.send(json.dumps({"event": "task_finish"})), timeout=2)
            except Exception:  # noqa: BLE001 - 收尾尽力而为
                pass
        await self.invalidate()

    async def invalidate(self, *, reprewarm: bool = False) -> None:
        """弃置当前连接(2201/异常关闭/收尾);下个 ensure_ready 自动全新重连。
        reprewarm=True:死亡即后台重预热——唔等下一个真实轮先撞冷启动。"""
        ws, self._ws = self._ws, None
        self._params = None
        self._ping_misses = 0
        self._stop_ping()
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        if reprewarm and os.environ.get("MINIMAX_BIDI_AUTO_REWARM", "1") == "1":
            self.prewarm()

    async def aclose(self) -> None:
        # 防 teardown 期重预热(官方 #7050 形状):先取消在飞预热任务与 ping 连失
        # 甩出的孤儿 invalidate task(reprewarm=True)再 invalidate——唔然 aclose 的
        # 无参 invalidate 跑完后孤儿才执行,teardown 后拉起全新连接+ping 保活=泄漏。
        tasks = [
            t
            for t in (self._prewarm_task, self._invalidate_task)
            if t is not None and not t.done()
        ]
        self._prewarm_task = None
        self._invalidate_task = None
        for t in tasks:
            t.cancel()
        for t in tasks:  # 等取消落地,唔留悬空任务过 teardown
            try:
                await t
            except asyncio.CancelledError:
                pass  # 自己 cancel 嘅——继续收尾
            except Exception:
                pass
        await self.invalidate()

    def prewarm(self) -> None:
        """空闲期预连（会话开始调用）。已在连/已在补/无 key → 跳过;失败静默。"""
        if not self._tts_._api_key() or not self._tts_._resolve_voice():
            return
        if self._alive() or (self._prewarm_task is not None and not self._prewarm_task.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 调用点都在 loop 内
            return
        self._prewarm_task = loop.create_task(self._prewarm_run())

    async def _prewarm_run(self) -> None:
        try:
            async with self._lock:
                if not self._alive():
                    await self._connect_and_start(self._tts_._bidi_params_key())
                    # 合成级预热(T1 探针:处女连接首合成仅比二次慢 ~22ms,收益可忽略
                    # 故默认关):锁内执行——真实流 ensure_ready 排同一把锁,预热文本
                    # 唔会与真实轮 task_continue 在服务端同会话串台。
                    if os.environ.get("MINIMAX_BIDI_SYNTH_WARMUP", "0") == "1":
                        await self._synth_warmup(self._ws)
            print(
                f"MINIMAX_TTS_BIDI_PREWARM connect_ms={self.last_connect_ms:.0f}",
                flush=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 预热尽力而为,首段合成自会重试
            print(f"MINIMAX_TTS_BIDI_PREWARM_FAIL {exc!r}", flush=True)
            await self.invalidate()
            # 官方 #6969 姿势:失败 1s 后重试一次(上限 1,连接成功即清零计数)。
            # 休眠期间 _prewarm_task 仍指向本任务——teardown(aclose)的 cancel 会在
            # sleep 点掀掉重试,teardown 后绝不拉新连接;醒来先清自引用再走 prewarm()
            # 幂等门重建(门内 alive/在飞检查照常生效,唔破 T6 死亡重预热去重)。
            if (
                self._prewarm_retries < 1
                and os.environ.get("MINIMAX_BIDI_PREWARM_RETRY", "1") == "1"
            ):
                self._prewarm_retries += 1
                await asyncio.sleep(1.0)
                self._prewarm_task = None
                self.prewarm()


class _MiniMaxBidiStream(tts.SynthesizeStream):
    """MiniMax bidi 持久连接流：LLM 增量逐字 task_continue,服务端负责切句合成。

    与 classic `_MiniMaxSynthesizeStream` 的分别：
    - 唔做客户端切句/_flushable——服务端累积文本按句末标点立即合成,粒度越细
      首句越早到;经典路径的按句+overlap 逻辑全删。
    - 收尾 task_flush（唔係 task_finish）——强制吐出无标点尾巴但连接保留,
      下一个流(下一轮对话)零握手段直接 task_continue。
    - 打断（框架 cancel 本流）→ task_cancel,服务端丢缓冲停合成,连接保留。
    """

    def __init__(self, tts_: "MiniMaxTTS", conn_options):
        super().__init__(tts=tts_, conn_options=conn_options)
        self._tts_ = tts_
        # 教学形拦截已触发过就唔再重复播罐头(同段后续课程句静默丢弃)。
        self._lecture_fired = False
        self._flushed_evt = asyncio.Event()  # 收到 task_flushed
        self._canceled_evt = asyncio.Event()  # 收到 task_canceled
        self._resend_evt = asyncio.Event()  # 收到 2205 软背压
        self._last_continue: str | None = None  # 2205 重发用
        # 本流已发全部 task_continue 文本(按发送序)——看门狗僵死重连后单条合并重发
        # (官方 2204:单条 >10k 字跳过,故截 10k);2205 重发係重放已发文本,唔 append。
        self._sent_text_parts: list[str] = []

    async def _emit_beep(self, output_emitter):
        import math

        # beep 旗标(tts_cache tee 用):合成失败兜 beep 时置位,外层据此拒绝落盘——
        # 错误提示音一旦入缓存,该行文本之后永远播 beep。多类同款实现,统一置位。
        self._emitted_beep = True
        sr = self._tts_.sample_rate
        n = int(sr * 0.4)
        pcm = bytearray()
        for i in range(n):
            v = int(12000 * math.sin(2 * math.pi * 440 * i / sr))
            pcm += v.to_bytes(2, "little", signed=True)
        output_emitter.push(bytes(pcm))
        output_emitter.flush()

    @staticmethod
    def _cancel_wait_s() -> float:
        try:
            return float(os.environ.get("MINIMAX_BIDI_CANCEL_WAIT_S", "3"))
        except Exception:  # pragma: no cover
            return 3.0

    @staticmethod
    def _first_audio_timeout_s() -> float:
        """看门狗阈值:首段文本发出后 N 秒仍无首包 → 判连接僵死重连+重发。
        默认 6(bidi 服务端攒句,首包天然比 classic 按句慢,阈值放宽);"0" 关;
        配错回 6.0。"""
        try:
            return float(os.environ.get("MINIMAX_BIDI_FIRST_AUDIO_TIMEOUT_S", "6"))
        except Exception:  # pragma: no cover
            return 6.0

    @staticmethod
    def _stall_max_heals() -> int:
        """一流看门狗自愈上限(2026-09-17):旧版一流只自愈一次,第二次僵死要干等
        30s recv 超时=整轮静默。默认 2(第二次自愈失败才落回既有 fail-fast);
        "0"=旧一次行为;配错回 2。"""
        try:
            return max(1, int(os.environ.get("MINIMAX_BIDI_STALL_MAX_HEALS", "2")))
        except Exception:  # pragma: no cover
            return 2

    async def _cancel_on_server(self, ws) -> None:
        """打断：task_cancel 丢服务端缓冲+停当前合成;连接保留,下轮继续用。"""
        if ws is None:
            return
        self._canceled_evt.clear()
        try:
            await asyncio.wait_for(ws.send(json.dumps({"event": "task_cancel"})), timeout=2)
        except Exception:  # noqa: BLE001 - 连接已死:下个流 ensure_ready 自会重连
            return
        try:
            await asyncio.wait_for(self._canceled_evt.wait(), timeout=self._cancel_wait_s())
        except Exception:  # noqa: BLE001 - 等 task_canceled 超时:连接保留,残留音频
            # 由下一流的纪元门禁兜住(active_epoch 未认领前一律丢弃,唔漏进下轮)
            print("MINIMAX_TTS_BIDI_CANCEL_TIMEOUT", flush=True)

    async def _run(self, output_emitter):
        t0 = time.monotonic()
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

            session = self._tts_._bidi_session()
            await session.lock.acquire()
            my_epoch = session.alloc_epoch()  # 本流纪元:首个 task_continue 时认领
            ws = None
            recv_task: asyncio.Task | None = None
            resend_task: asyncio.Task | None = None
            stall_task: asyncio.Task | None = None
            self._flushed_evt.clear()
            self._canceled_evt.clear()
            self._resend_evt.clear()
            init_done = False
            frame_bytes = int(sample_rate / 5) * 2  # 200ms 帧
            buf = bytearray()
            state = {
                "sent_any": False,
                "first_pushed": False,
                "t_first_continue": 0.0,
                "t_last_audio": 0.0,
                "stale_msgs": 0,
                "stale_bytes": 0,
                "sentences": 0,
                "stall_heals": 0,
            }
            t_flush = 0.0
            # 重连窗口发送闸(看门狗换连接期间):输入循环若继续 task_continue 会发到
            # 已弃旧 ws → 整轮音频丢失。所有发送点先 is_set() 再 wait()(Event.wait()
            # 对未 set 事件挂起,无条件 await 会把正常轮首句卡到重连后,classic 同款)。
            # 闸亮时间=重连 ~0.2-0.65s,期间文本排队唔丢唔乱序:闸清前已 append 进
            # _sent_text_parts 的由看门狗合并重发覆盖,闸清后照常直发新连接。
            _reconnecting = asyncio.Event()
            try:
                try:
                    ws = await session.ensure_ready()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # 连唔到 WS 冇音频可推(livekit 会 APIError no audio frames →
                    # 静音吞回复)。播一声 beep 令客户知 AI 有反应过,同 classic。
                    print("MINIMAX_TTS_BIDI_CONNECT", repr(exc), flush=True)
                    await self._emit_beep(output_emitter)
                    return
                reused = session.last_reused
                connect_ms = session.last_connect_ms

                async def _recv_loop():
                    nonlocal init_done
                    # flush 前给足窗口(等 LLM 流式期间服务端句子音频);
                    # task_flushed 后转 0.5s 空闲判收——尾巴吐完即收摊。
                    while True:
                        timeout = 0.5 if self._flushed_evt.is_set() else 30.0
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            if self._flushed_evt.is_set():
                                return  # 尾巴排干净了
                            continue  # 还在等句子音频,继续等
                        except Exception:
                            # 连接死亡(2201/网络):标记死连接 + 解锁等待方;
                            # 死亡即后台重预热,下个真实轮零冷启动
                            await session.invalidate(reprewarm=True)
                            self._flushed_evt.set()
                            self._canceled_evt.set()
                            return
                        try:
                            msg = json.loads(raw)
                        except Exception:  # noqa: BLE001 - 非 JSON 心跳类,忽略
                            continue
                        event = msg.get("event")
                        data = msg.get("data") or {}
                        audio_hex = data.get("audio") or ""
                        if audio_hex and session.active_epoch != my_epoch:
                            # 纪元门禁:本流尚未发过 task_continue(active_epoch 还是
                            # 上一流的)→ 连接上此刻冒出的音频全是上一流打断(cancel
                            # 超时,服务端未停稳)的迟到残留——丢弃,绝唔喂进本轮
                            # emitter(上一句被打断的话漏进下一轮回复 = 残留泄漏)。
                            state["stale_msgs"] += 1
                            state["stale_bytes"] += len(audio_hex) // 2
                            if state["stale_msgs"] == 1:
                                print(
                                    f"MINIMAX_BIDI_DROP_STALE epoch={my_epoch} "
                                    f"active_epoch={session.active_epoch}",
                                    flush=True,
                                )
                            audio_hex = ""
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
                            state["t_last_audio"] = time.monotonic()
                            if not state["first_pushed"] and len(buf) >= frame_bytes // 5:
                                # P0 首帧早推:不足 200ms 先推 ~40ms,早出声(同 classic)。
                                t_first = time.monotonic()
                                fc = state["t_first_continue"]
                                print(f"TTS_FIRST_AUDIO_MS {(t_first - fc) * 1000:.0f}", flush=True)
                                print(
                                    f"MINIMAX_TTS_BIDI_PERF reused={int(reused)} "
                                    f"connect_ms={connect_ms:.0f} "
                                    f"first_continue_to_audio_ms={(t_first - fc) * 1000:.0f} "
                                    f"total_ms={(t_first - t0) * 1000:.0f}",
                                    flush=True,
                                )
                                output_emitter.push(bytes(buf))
                                output_emitter.flush()
                                buf.clear()
                                state["first_pushed"] = True
                            while len(buf) >= frame_bytes:
                                output_emitter.push(bytes(buf[:frame_bytes]))
                                output_emitter.flush()
                                del buf[:frame_bytes]
                        # is_final = 当前句/当前请求音频完(bidi 喺 data 喺顶层都有得给,
                        # 兼容两种位置)。只推清尾巴,唔退出——会话继续。
                        if (msg.get("is_final") or data.get("is_final")) and buf:
                            output_emitter.push(bytes(buf))
                            output_emitter.flush()
                            buf.clear()
                        if event == "task_flushed":
                            self._flushed_evt.set()
                        elif event == "task_canceled":
                            self._canceled_evt.set()
                        elif event == "task_finished":
                            return
                        elif event == "sentence_end":
                            # 服务端实际切句数(连贯性观测:整轮回复应≈句数,
                            # 远大于句数=服务端按长度强切,查标点是否被清洗)。
                            state["sentences"] += 1
                        base = msg.get("base_resp") or {}
                        status = int(base.get("status_code") or 0)
                        if status == 2205:
                            self._resend_evt.set()  # 软背压:重发协程稍后原样重发
                        elif status == 2204:
                            print("MINIMAX_TTS_BIDI_2204_TEXT_SKIPPED", flush=True)
                        elif status in (2201, 2206):
                            print(f"MINIMAX_TTS_BIDI_{status}", flush=True)
                            await session.invalidate()
                            self._flushed_evt.set()
                            self._canceled_evt.set()
                            return
                        elif status not in (0,):
                            print(f"MINIMAX_TTS_BIDI_STATUS {status} {str(base)[:120]}", flush=True)

                async def _resend_loop():
                    # 2205 软背压:稍后原样重发同一条 task_continue,唔重连。
                    try:
                        while True:
                            await self._resend_evt.wait()
                            self._resend_evt.clear()
                            await asyncio.sleep(0.2)
                            text = self._last_continue
                            if text is None:
                                continue
                            await ws.send(json.dumps({"event": "task_continue", "text": text}))
                            print(f"MINIMAX_TTS_BIDI_2205_RESEND chars={len(text)}", flush=True)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        return  # 连接已死,接收侧会 invalidate

                def _start_loops() -> None:
                    nonlocal recv_task, resend_task
                    recv_task = asyncio.create_task(_recv_loop())
                    resend_task = asyncio.create_task(_resend_loop())

                _start_loops()

                async def _stall_watch() -> None:
                    """首段文本发出后 N 秒无首包 → 判连接僵死:弃连接重连+重发已发文本。
                    classic MINIMAX_FIRST_AUDIO_TIMEOUT_S 同语义移植(_stall_watch);bidi
                    无此看门狗时只能干等 30s recv 超时(整轮回复静默)。一流自愈上限
                    MINIMAX_BIDI_STALL_MAX_HEALS(默认 2,2026-09-17 由硬编码 1 放宽):
                    旧版第二次僵死干等 30s recv——重连后唔重臂是防「6s 一轮的重连
                    风暴」,上限 2 保留该防线又给二次僵死一条自愈路;超限回落既有
                    fail-fast(后续轮自会撞死亡路径 invalidate+重预热)。"""
                    nonlocal ws, reused, connect_ms, stall_task
                    timeout = self._first_audio_timeout_s()
                    if timeout <= 0:
                        return
                    await asyncio.sleep(timeout)
                    if state["first_pushed"] or self._flushed_evt.is_set() or not state["sent_any"]:
                        return  # 已出首包/流已收尾(死亡分支置 flushed)/无已发文本:唔干预
                    print(f"MINIMAX_TTS_BIDI_STALL timeout={timeout}s — 重连重发", flush=True)
                    _reconnecting.set()
                    try:
                        # 先停两条收发协程再拆连接(只 cancel 唔 await:外层 cancel 唔会
                        # 俾内层 except 吞掉):防旧 recv 喺旧 ws 上判死走
                        # invalidate(reprewarm=True)——呢条无锁路径会杀死看门狗刚建好的
                        # 新连接(或与 ensure_ready 竞态双连);也防在飞 2205 重发跨连接
                        # 重放成重复文本。收流死亡分支若已喺 sleep 期间跑过(reprewarm
                        # 已排),flushed 已置 → 上面已让位,prewarm 排队喺本流锁后、
                        # 睇 _alive() 决定连唔连,两种到达顺序都唔会双连。
                        for task in (recv_task, resend_task):
                            if task:
                                task.cancel()
                        self._resend_evt.clear()
                        await session.invalidate()  # 不 reprewarm:本流自持锁,紧接 ensure_ready
                        ws = await session.ensure_ready()  # 全新连接(我们仍持 session.lock)
                        reused = session.last_reused
                        connect_ms = session.last_connect_ms
                        text = "".join(self._sent_text_parts)[:10000]  # 官方单条 ≤10k
                        if text:
                            await ws.send(json.dumps({"event": "task_continue", "text": text}))
                            # 新连接上「最后一条 continue」=合并重发,后续 2205 原样重发它
                            self._last_continue = text
                        state["t_first_continue"] = time.monotonic()
                        state["first_pushed"] = False
                        session.active_epoch = my_epoch  # 认领纪元:重发即本流首个 continue,同发送分支
                        _start_loops()  # 新连接新收发协程(ws 已重绑进闭包)
                        # 自愈预算内 → 重臂看门狗(新连接再僵死再救一次);超限即止
                        # (落回 30s recv fail-fast,防对端持续僵死时的重连风暴)。
                        state["stall_heals"] = int(state.get("stall_heals", 0)) + 1
                        if state["stall_heals"] < self._stall_max_heals():
                            stall_task = asyncio.create_task(_stall_watch())
                    except Exception as exc:  # noqa: BLE001 - 重连失败:闸清后下一发撞死连接走既有收摊
                        print(f"MINIMAX_TTS_BIDI_STALL_FAIL {exc!r}", flush=True)
                        self._flushed_evt.set()  # 流已救唔返:收尾 flush 唔好白等 15s
                    finally:
                        _reconnecting.clear()

                def _arm_stall_watch() -> None:
                    nonlocal stall_task
                    if stall_task is None and self._first_audio_timeout_s() > 0:
                        stall_task = asyncio.create_task(_stall_watch())

                async def _send_text(s: str) -> None:
                    if not self._lecture_fired and is_lecture_text(s):
                        # 开场即教学 → 播一次罐头的「请再报单号」,唔好照读课程;
                        # 若前面已出过正常音频,课程句静默丢弃,唔追加罐头(避免二重声)。
                        self._lecture_fired = True
                        if not state["sent_any"]:
                            canned = lecture_canned(self._tts_._speech_lang())
                            if _reconnecting.is_set():
                                await _reconnecting.wait()
                            self._last_continue = canned
                            self._sent_text_parts.append(canned)  # 看门狗重连合并重发用
                            if state["t_first_continue"] == 0.0:
                                state["t_first_continue"] = time.monotonic()
                                _arm_stall_watch()  # 首条 task_continue 起看门狗计时
                            await ws.send(json.dumps({"event": "task_continue", "text": canned}))
                            state["sent_any"] = True
                            session.active_epoch = my_epoch  # 认领纪元:此后残留门禁对本流放行
                        return
                    if is_lecture_text(s):
                        return  # 已触发过,课程延续句照丢
                    # bidi:逐块原样透传,唔切句——服务端自己按标点/长度切句合成。
                    if _reconnecting.is_set():
                        await _reconnecting.wait()
                    if state["t_first_continue"] == 0.0:
                        state["t_first_continue"] = time.monotonic()
                        _arm_stall_watch()  # 首条 task_continue 起看门狗计时
                    self._last_continue = s
                    self._sent_text_parts.append(s)  # 看门狗重连合并重发用
                    await ws.send(json.dumps({"event": "task_continue", "text": s}))
                    state["sent_any"] = True
                    session.active_epoch = my_epoch  # 认领纪元:此后残留门禁对本流放行

                async for item in self._input_ch:
                    if isinstance(item, self._FlushSentinel):
                        continue
                    text = str(item or "")
                    if not text.strip():
                        continue
                    await _send_text(text)

                # 文本结束:task_flush 强制吐出无标点尾巴,会话唔结束(连接保留)。
                try:
                    if state["t_first_continue"] > 0.0:
                        if _reconnecting.is_set():
                            await _reconnecting.wait()
                        t_flush = time.monotonic()
                        await ws.send(json.dumps({"event": "task_flush"}))
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await asyncio.wait_for(self._flushed_evt.wait(), timeout=15)
                except asyncio.TimeoutError:
                    print("MINIMAX_TTS_BIDI_FLUSH_TIMEOUT", flush=True)
                # 等 recv_loop 把尾巴音频排完(0.5s 空闲自动收,给 20s 上限兜底)
                if recv_task:
                    try:
                        await asyncio.wait_for(asyncio.shield(recv_task), timeout=20)
                    except asyncio.TimeoutError:
                        pass
                if t_flush > 0.0 and state["t_last_audio"] > 0.0:
                    print(
                        f"MINIMAX_TTS_BIDI_PERF flush_to_last_audio_ms="
                        f"{(state['t_last_audio'] - t_flush) * 1000:.0f}",
                        flush=True,
                    )
                # 轮级汇总:服务端切句数/打断/首包(首声=距首条 task_continue)。
                # 验收读数:典型 2-3 短句回复 sentences 应 2-4;first_audio_ms 稳定
                # 在数百 ms 且方差小于 classic overlap 时代。
                def _print_perf_summary() -> None:
                    if state["t_last_audio"] > 0.0 and state["t_first_continue"] > 0.0:
                        print(
                            f"MINIMAX_BIDI_PERF sentences={state['sentences']} "
                            f"canceled={int(self._canceled_evt.is_set())} "
                            f"first_audio_ms="
                            f"{(state['t_last_audio'] - state['t_first_continue']) * 1000:.0f}",
                            flush=True,
                        )
                    else:
                        print(
                            f"MINIMAX_BIDI_PERF sentences=0 "
                            f"canceled={int(self._canceled_evt.is_set())} (no audio this turn)",
                            flush=True,
                        )

                _print_perf_summary()
            except asyncio.CancelledError:
                # 打断(barge-in):通知服务端丢弃缓冲/停合成,连接保留给下一轮。
                try:
                    if _reconnecting.is_set():
                        await _reconnecting.wait()  # 等看门狗换完连接,cancel 落新连接
                    await self._cancel_on_server(ws)
                except Exception:  # noqa: BLE001 - 收尾尽力而为
                    pass
                # 打断轮也打 PERF(此前 CancelledError 跳过汇总,打断观测只能靠音频断言)。
                print(
                    f"MINIMAX_BIDI_PERF sentences={state['sentences']} "
                    f"canceled={int(self._canceled_evt.is_set())} (interrupted)",
                    flush=True,
                )
                raise
            except Exception as exc:
                print("MINIMAX_TTS_BIDI_ERR", repr(exc), flush=True)
            finally:
                for task in (resend_task, recv_task, stall_task):
                    if task:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass  # 自己 cancel 嘅——继续收尾
                        except Exception:
                            pass
                # 推完残余音频并结束 segment(同 classic 收尾)
                if init_done and buf:
                    try:
                        output_emitter.push(bytes(buf))
                        output_emitter.flush()
                        output_emitter.end_segment()
                    except Exception:  # noqa: BLE001
                        pass
                if state["stale_msgs"]:
                    print(
                        f"MINIMAX_BIDI_DROP_STALE_TOTAL msgs={state['stale_msgs']} "
                        f"bytes={state['stale_bytes']}",
                        flush=True,
                    )
                session.lock.release()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 外层兜底:同 classic 唔好炸框架
            print("MINIMAX_TTS_BIDI_FATAL", repr(exc), flush=True)
            try:
                await self._emit_beep(output_emitter)
            except Exception:  # pragma: no cover
                pass


class _MiniMaxTTSStream(tts.ChunkedStream):
    def __init__(self, tts_, text, conn_options):
        super().__init__(tts=tts_, input_text=text, conn_options=conn_options)
        self._text = text
        self._tts_ = tts_
        # beep 旗标(tts_cache tee 用):合成失败兜 beep 时置位,外层据此拒绝落盘——
        # 错误提示音一旦入缓存,该行文本之后永远播 beep。
        self._emitted_beep = False

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
        import ssl  # noqa: F401 - 历史保留

        import websockets

        # 修复：旧码 `self._endpoint_ws()` 喺 ChunkedStream 上 AttributeError →
        # 恒走 MINIMAX_TTS_FATAL，连 HTTP 回退都到不了。
        url = self._tts_._endpoint_ws()
        t0 = time.monotonic()
        # 热连接池（与 SynthesizeStream 路径同源）：取唔到/状态异常回退流内自连。
        ws = None
        ws_from_pool = False
        if _minimax_pool_enabled():
            ws = _minimax_pool_pop(url, key)
            ws_from_pool = ws is not None
        if ws is None:
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
        ws_connect_ms = 0.0 if ws_from_pool else (time.monotonic() - t0) * 1000

        async def _handshake(a_ws) -> dict:
            """connected_success（丢帧不致命）→ task_start → task_started。"""
            try:
                await asyncio.wait_for(a_ws.recv(), timeout=10)
            except Exception:
                pass
            start = {
                "event": "task_start",
                "model": self._tts_._model(),
                "voice_setting": self._tts_._ws_voice_setting(voice),
                "audio_setting": {"sample_rate": sample_rate, "format": "pcm", "channel": 1},
                # 官方参数:流式不回传聚合音频,显著降尾包体积与传输耗时。
                "stream_options": {"exclude_aggregated_audio": True},
            }
            # language_boost 锁语种(B 线同传按目标语注入 env):源语音常夹第三方
            # 词,显式锁死防合成语种漂移;枚举值是 MiniMax API 外部字面量。
            boost = self._tts_._language_boost()
            if boost:
                start["language_boost"] = boost
            await a_ws.send(json.dumps(start))
            return json.loads(await asyncio.wait_for(a_ws.recv(), timeout=15))

        try:
            try:
                resp = await _handshake(ws)
            except asyncio.CancelledError:
                raise
            except Exception:
                if not ws_from_pool:
                    raise
                # 池连接空闲期被服务端静默关闭 → 弃池，全新连接重握手一次。
                print("MINIMAX_TTS_WS_POOL_STALE retry_fresh", flush=True)
                await _minimax_ws_silent_close(ws)
                t_fresh = time.monotonic()
                ws = await websockets.connect(
                    url,
                    additional_headers={"Authorization": f"Bearer {key}"},
                    open_timeout=10,
                    max_size=20_000_000,
                )
                ws_from_pool = False
                ws_connect_ms = (time.monotonic() - t_fresh) * 1000
                resp = await _handshake(ws)
            t_start = time.monotonic()
            if resp.get("event") != "task_started":
                print("MINIMAX_TTS_WS_START_FAIL", str(resp)[:200], flush=True)
                return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("MINIMAX_TTS_WS_START", repr(exc), flush=True)
            return False
        try:
            await ws.send(json.dumps({"event": "task_continue", "text": _inject_pauses(self._text)}))
            pcm_total = 0
            frame_bytes = int(sample_rate / 5) * 2  # 200ms 帧
            buf = bytearray()
            init_done = False
            first_audio_ms = -1.0
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
                    # P1.5 FIX 3 首包分解：首音频块到达时刻（距 task_start/握手/全程）。
                    if first_audio_ms < 0:
                        t_first = time.monotonic()
                        first_audio_ms = (t_first - t_start) * 1000
                        print(
                            f"MINIMAX_TTS_WS_PERF pool={'hit' if ws_from_pool else 'fresh'} "
                            f"ws_connect_ms={ws_connect_ms:.0f} "
                            f"task_start_to_audio_ms={first_audio_ms:.0f} "
                            f"total_ms={(t_first - t0) * 1000:.0f}",
                            flush=True,
                        )
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
            # 本连接已耗尽（一连接一任务），后台补热连接给下次合成。
            _minimax_pool_schedule(url, key)

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
                        "voice_setting": self._tts_._ws_voice_setting(voice),
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

        # beep 旗标(tts_cache tee 用):合成失败兜 beep 时置位,外层据此拒绝落盘——
        # 错误提示音一旦入缓存,该行文本之后永远播 beep。多类同款实现,统一置位。
        self._emitted_beep = True
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


def _tts_segment_has_word_char(s: str) -> bool:
    r"""段内有没有任何「可朗读」字符（字母/数字/汉字/其他 \w）。

    纯标点/空白段（「。」「？！！」…）绝不能送 Qwen3-TTS：模型对无音节输入会
    连环 hallucinate 4-30s 音频爆段（P4 实证 QWEN3_TTS_BYTES 983040-1530240），
    爆段把整轮 playout 拖 20s+，下一轮回复被官方语音队列压住唔出声（P4-A 挂死根因）。
    """
    return _TTS_WORD_CHAR_RE.search(s) is not None


_TTS_WORD_CHAR_RE = re.compile(r"\w")


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
    # 单任务音频上限(秒):正常一段≤2-3 短句 ≤8s;TTS 对异常输入(纯标点/幻觉)
    # 会连环合成 20-30s 爆段(P4 实证 1MB+ 级),爆段霸住 playout 会把下一轮回复
    # 压喺官方语音队列后面(TTFT/回复延迟齐炸)。到顶即断流,唔再读剩余 body。
    # QWEN3_TTS_MAX_TASK_AUDIO_SEC=0 可关。
    try:
        _cap_sec = float(os.environ.get("QWEN3_TTS_MAX_TASK_AUDIO_SEC", "15"))
    except ValueError:  # pragma: no cover - 配错当没配
        _cap_sec = 15.0
    max_task_bytes = int(tts_.sample_rate * max(_cap_sec, 0.0)) * 2 if _cap_sec > 0 else 0
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                # client.stream():响应体逐块拉取。旧 client.post() 会先整体读满
                # body 先返回(内部 read()),下面 aiter_bytes() 只是读缓存——
                # sidecar 边合成边推嘅 ~83ms 模型块喺客户端全部积压,首个 40ms
                # 早推变零收益。stream 模式下 aiter_bytes() 先真逐块到。
                async with client.stream(
                    "POST",
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
                ) as resp:
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
                        # 单任务爆段护栏:音频时长到顶(默认 15s)即停读剩余 body,
                        # 本任务判失败(有音频落地 → 上层唔重试),防 20-30s 爆段霸 playout。
                        if max_task_bytes and pcm_total + len(buf) >= max_task_bytes:
                            keep = max(max_task_bytes - pcm_total, 0)
                            if keep:
                                output_emitter.push(bytes(buf[:keep]))
                                output_emitter.flush()
                                del buf[:keep]
                                pcm_total = max_task_bytes
                                pushed_any = True
                            print("QWEN3_TTS_TASK_CAP", pcm_total, "bytes (burst guard)", flush=True)
                            if end_segment:
                                output_emitter.end_segment()
                            return False
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
                    if not _tts_segment_has_word_char(sentence):
                        # 纯标点段(P4-B 实证 input='。' / '？'):Qwen3-TTS 对无音节
                        # 输入会 hallucinate 4-30s 爆段。直接丢弃,绝唔单独 POST
                        # (前句已带句末标点,丢呢段零语音损失)。
                        continue
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
                        if _flushable(frag) and _tts_segment_has_word_char(frag):
                            ok = await _qwen3_tts_post_frames(
                                self._tts_, frag, output_emitter, state,
                                end_segment=False,
                            )
                            _last_send = time.monotonic()
                            if not ok:
                                broken = True
                                break
                            sent_buf = ""
            if not broken:
                # 收尾残句:全场文本结束,把没凑够一句的尾巴合成掉。
                # 纯标点尾巴(唔沾正字)直接丢弃,绝唔 POST(P4-B 爆段源)。
                final_text = sent_buf.strip()
                if _tts_segment_has_word_char(final_text):
                    await _qwen3_tts_post_frames(
                        self._tts_, final_text, output_emitter, state,
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
# 每通对话语言固定(per-call fixed):生产两处调用(agent.py 会话装配 / interpret.py
# 同传)都 pin=True 三语全钉——zh 也整场下发 Chinese,实测夹英文 code-switching 词
# 保得住(「check/WhatsApp/order status」原样),auto 反而把粤语混英句误判成
# English。pin=False 只係构造缺省的旧口径逃生口(cantonese/en 钉、zh auto),生产唔走。
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
        hotword_context: str = "",
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
        # 热词/context(Qwen3-ASR 官方 customizable context = system message 词汇表
        # 软偏置):每通对话装配一次,随 /api/start 下发,session 级透传每次解码。
        self._hotword_context = str(hotword_context or "").strip()
        # 会话级 partial 解码间隔档(GPU 竞态专项):agent 回复生成/播报中抬高,
        # listening 恢复 None=env 默认。getattr 鸭型访问,勿删(测试 fake 无此属性)。
        self._partial_ms_override: int | None = None
        # 回复在途旗(F2 迟到 FINAL 尾巴护栏):thinking/speaking=True,listening=
        # False。agent 经 Qwen3ASRLiveSTT.set_reply_busy 写;流在读判定时刻现取。
        self._reply_busy = False
        # 收线/告别直念窗旗(F4 二修):agent 播分支动作【收线】台词期间 True,
        # 念完即撤。此窗内流静默丢弃一切成轮事件(句级提交/EOS/FINAL)。
        self._closing_say = False
        # 本轮 ASR 滑窗 partial 末稿(agent 侧 E2 热词泄漏清洗的 fallback_text 取口)。
        # 写点=实时流 _Qwen3ASRLiveStream._publish_turn_partial(每窗 partial 到达);
        # 清点=流 _reset(该段 FINAL 记账处,清后由 FINAL 发出点按 pre-reset 快照
        # 重贴给刚落库那条 FINAL)/_start_session(新语音段开场=新一轮)。
        # 故它是一个「本轮」值:新一轮开场即清,不会把上一轮的 partial 喂给下一轮。
        # 恒可安全读:纯属性、无 await、无 IO;offline recognize 路径不写=恒空。
        self._turn_partial_text: str = ""

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
        # mlx 层大小写不敏感)。每通对话语言固定(A 线通话装配时钉死、B 线同传用户
        # 选定):生产都 pin=True 三语全钉——cantonese 防 auto 误判成普通话(啱唔靈→
        # 难唔难),zh 下发 Chinese 夹英文词照样保得住(实测「check/WhatsApp/order
        # status」原样,auto 反而误判 English)。pin=False 构造缺省只係逃生口。
        lang_hint = _asr_language_hint(self._stt_._language_state.lang, self._stt_._pin_language)
        print(f"QWEN3_ASR_HINT {lang_hint or 'auto'} lang_state={self._stt_._language_state.lang}", flush=True)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    # start 参数:language hint + 热词 context(都有先例可空,空则不下发;
                    # getattr 鸭型访问——测试 fake 与旧设置面无此属性时等同空)
                    start_params: dict[str, str] = {}
                    if lang_hint:
                        start_params["language"] = lang_hint
                    if getattr(self._stt_, "_hotword_context", ""):
                        start_params["context"] = self._stt_._hotword_context
                    start = await client.post(
                        f"{self._stt_._base_url}/api/start",
                        params=start_params or None,
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

# ---- 句级提交(turn_detection="stt" 的 STT 侧事件源)----------------------------
# 强句标点:全角 。！？ 与半角 !?；ASCII 句点另判(见 _sentence_boundary,3.5 唔算边界)。
_SENTENCE_STRONG_PUNCT = "。！？!?"
# 句子(自上个提交边界起)最少字数:短句/连珠短答(好。係。)唔够格,排队并入下一边界。
_ASR_SENTENCE_MIN_CHARS = 6
# 限速:两次句级提交最少间隔(「好。係。唔該。」连珠句防机关枪式连发,
# 排队语义=剩余文本并入下一边界或 VAD 停嘴整句兜底)。
_ASR_SENTENCE_MIN_INTERVAL_S = 1.5
# 子句级提交(B 线同传档,2026-09-16):次级标点也作提交边界——译员按子句跟,
# 唔等整句讲完才翻。A 线 worker 唔带此 env,客服轮次仍按句。
_SENTENCE_WEAK_PUNCT = "，、；,;"


def _clause_commit_enabled() -> bool:
    """子句级提交总门:QWEN3_ASR_CLAUSE_COMMIT(默认 0,A 线零变化;B 线
    _interp_env setdefault 1)。关=行为逐字节同旧。"""
    return os.environ.get("QWEN3_ASR_CLAUSE_COMMIT", "0") == "1"


def _clause_commit_min_chars() -> int:
    """子句提交字数下限(默认 8>标点路径 6):逗号比句号密得多,6 字门槛会把
    说话切成机关枪碎片,TTS 段间开销反而拖慢整体;8 字≈一个自然短语组。
    QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS 可调。"""
    try:
        return int(os.environ.get("QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS", "8"))
    except ValueError:
        return 8


def _clause_len_commit_enabled() -> bool:
    """长度触发子句提交(B 线「边说边译」档,2026-09-17):QWEN3_ASR_CLAUSE_LEN_COMMIT
    (默认 0,A 线零变化;B 线 _interp_env setdefault 1)。

    三个提交事件源的补位:标点档靠说话人打逗号、停顿档靠 VAD 0.45s 停嘴——
    连续语流(一口气不带标点不带停顿)两者都哑火,译文要等 EOS 才开工。长度档
    在滑窗里盯未提交前缀:攒够字数且跨窗稳定就就地切句,译出声不等人讲完
    (SimulStreaming AlignAtt/LocalAgreement「稳定前缀才出」思想的工程化)。
    """
    return os.environ.get("QWEN3_ASR_CLAUSE_LEN_COMMIT", "0") == "1"


def _clause_len_commit_chars() -> int:
    """长度档切段字数(默认 10:4 字/秒语速≈2.5s 话音一翻;2026-09-17 二轮从 12
    收到 10——真人语音子句普遍 5-12 字,12 字门槛在停顿到来前常常攒不够,长度档
    空转;10 字+1.5s 限速的提交节奏仍对得上 TTS 串行播报)。QWEN3_ASR_CLAUSE_LEN_CHARS
    可调,地板 8(与标点档下限对齐)。"""
    try:
        return max(8, int(os.environ.get("QWEN3_ASR_CLAUSE_LEN_CHARS", "10")))
    except ValueError:
        return 10


def _pause_commit_min_chars() -> int:
    """vad-pause 路径提交的字数下限（默认 10 > 标点路径 6）。

    微停顿（≥0.45s）只证明「喘了口气」，证明唔了「一句话讲完」——6-9 字碎片
    （「你邊個啊。」）被当整轮提交，回复 TTS 出声前就被下一碎片新轮掐死
    （碎片提交饿死回复）。扣住后说话继续则并入下个边界、真停嘴则整句 finish
    兜底，轮唔会丢。QWEN3_ASR_PAUSE_COMMIT_MIN_CHARS=6 回退旧行为。
    （借鉴 KoljaB/RealtimeVoiceChat turndetect 的语义端点思想：提交前先看
    「像唔像说完」；官方 audio turn detector v1-mini 属架构级换件，另评估。）
    """
    try:
        return int(os.environ.get("QWEN3_ASR_PAUSE_COMMIT_MIN_CHARS", "10"))
    except ValueError:
        return 10
# VAD 微停顿候选句尾部的弱停顿符：滑窗 partial 说话期句尾只打逗号（p6 实测），
# 停嘴高精度 finish 会升级成句号——提交时剥掉弱尾符，让已提交前缀与 finish
# 整句做 startswith 匹配时唔会因「，vs。」错位（错位会触发 rfind 兜底返回
# 整段=重复转写）。强句标点（。！？!?）唔剥——嗰个係边界本身。
_PAUSE_TRAILING_WEAK_PUNCT = "，、；：,;…—~～ \t"
# _uncommitted 剩余的头部位弱停顿符：同理（committed 前缀剥了弱尾符后，
# finish 整句的「。佢聽日…」剩余唔应该带住句号开头进字幕/下一轮）。
_UNCOMMITTED_LEADING_WEAK_PUNCT = _PAUSE_TRAILING_WEAK_PUNCT + "。！？!?"

# finish 整句与已提交文本「重解修正」判定阈值：去标点归一化后相似度 ≥ 此值，
# 视作同一段话的更好转写 → 唔补发迟到 FINAL。补发会在框架 on_final_transcript
# 里 _interrupt_by_audio_activity() 掐死生成中的回复（call-58601bba 实测：
# 「这是我的牌」修正成「这是我的快递」补发 FINAL，gen=25 token 的回复被弃、
# 零音频零转写、8s 后心跳顶替——「每问无答」的另一根因）。
_ASR_REDECODE_DROP_RATIO = 0.55


def _strip_punct_space(s: str) -> str:
    """去标点与空白，只留正字——重解修正/续句的归一化对齐用。"""
    return "".join(
        ch for ch in s if not ch.isspace() and not unicodedata.category(ch).startswith("P")
    )


# 纯应承字表：短尾逐字都落喺呢个集 → 判纯语气（唔补发）；有任何集外字 → 真内容。
_PURE_ACK_TAIL_CHARS = frozenset("好係系是嗯哦喔啊得呀对啱啦喎喽咯嘛哈唉哎欸噢唔咩呀啦")


def _tail_carries_content(s: str) -> bool:
    """停嘴 <6 字短尾是否带真内容（纯函数，单测用）。

    True=数字/字母 run（补报的「四五七。」、英文词）或任何纯应承字表外的实词
    （「我唔知。」的「我」「知」）→ 短尾豁免照发成轮；False=逐字纯应承
    （「係。」「嗯嗯。」）→ 照旧丢弃（打断自噬保护）。"""
    t = _strip_punct_space(s)
    if not t:
        return False
    if re.search(r"[0-9A-Za-z]{2,}", t):
        return True
    return any(ch not in _PURE_ACK_TAIL_CHARS for ch in t)


# ---- 迟到 FINAL 尾巴护栏（F2，2026-09-20 验收实证）--------------------------
# 现象：句级提交已发 FINAL、回复（快路罐头/直念/QA 罐头）开播后，停嘴 finish
# 整窗重解在句尾幻听出「那。」级短碎片 → _uncommitted 的 startswith 分支把
# 它当真尾巴原样返回（追加，唔係修正）→「带内容短尾豁免」放行（「那」是实词、
# 唔在纯应承字表）→ 第二条 FINAL 成新用户轮 → 框架 _interrupt_by_audio_activity
# 掐断在播回复（0.4s 截断、assistant item 唔落库）。
# 既有两道闸为何没拦：
# - 重解丢弃（_ASR_REDECODE_DROP_RATIO）：只管「坐标失配的修正」——本例同头
#   「追加」，startswith 直接命中，相似度门结构性不进；
# - 短尾不补发（_tail_carries_content）：只丢纯应承字，「那」算实词 → 豁免。
# 新闸语义：AI 正在生成/播报（thinking/speaking）时，迟到 finish 尾巴相对已
# 提交文本只是「极短追加」→ 判幻听碎片，唔补发、唔成轮、唔打断；真·新话
# （足够长/含数字字母 run）与 AI 空闲轮照旧成轮——2026-09-07「带内容短尾
# 豁免」（「我唔知」「四五七」补报）在 AI 空闲与长度门槛之上全保留。
# BOK_LATE_FINAL_GUARD=0 回退旧行为；字数阈值 BOK_LATE_FINAL_MAX_TAIL_CHARS。
def _late_final_guard_on() -> bool:
    """迟到 finish 尾巴护栏总门（BOK_LATE_FINAL_GUARD，默认开；0=回退旧行为）。"""
    return os.environ.get("BOK_LATE_FINAL_GUARD", "1") == "1"


def _late_final_max_tail_chars() -> int:
    """「极短追加」字数上限（BOK_LATE_FINAL_MAX_TAIL_CHARS，默认 2，地板 1）。

    幻听碎片实测 1-2 字（「那」「嗰」）；2026-09-07 豁免的真实短应答普遍
    ≥3 字（「我唔知」）——默认 2 正好夹住两者，唔后悔可调大。"""
    try:
        return max(1, int(os.environ.get("BOK_LATE_FINAL_MAX_TAIL_CHARS", "2")))
    except ValueError:
        return 2


def late_final_is_new_speech(
    payload: str,
    committed: str,
    *,
    agent_busy: bool,
    max_tail_chars: int = 2,
    closing_say: bool = False,
) -> bool:
    """迟到 finish 尾巴是否够格当新客户话（纯函数，单测用）。

    True=照发 FINAL（可打断在播回复——真插话/补报号码是本分）；False=判
    finish 重解幻听尾巴，丢弃（快路/直念回复唔被打断）。`committed` 只进
    文档语义（判定点已由 _uncommitted 保证 payload 相对它是尾部追加/高度
    重叠），不参与运算——阈值判定只看尾巴自身形态：
    - closing_say（收线/告别直念窗，F4 二修）→ False：任何长度、含数字字母
      一律丢弃——电话本就要结束，把告别说完比什么都优先（半句道歉是最差
      听感），数字零降级在此窗让位；
    - 净文（去标点空白）为空 → False（空/纯标点永不成轮）；
    - 数字/字母 run ≥2 → True（数字零降级：补报单号永远送达）；
    - AI 未在生成/播报 → True（无回复可掐，维持带内容短尾豁免旧行为）；
    - 净文长 > max_tail_chars → True（足够长=真新话）；
    - 其余（AI 忙+极短追加）→ False。
    """
    norm = _strip_punct_space(payload)
    if not norm:
        return False
    if closing_say:
        return False
    if re.search(r"[0-9A-Za-z]{2,}", norm):
        return True
    if not agent_busy:
        return True
    return len(norm) > max_tail_chars


def _closing_say_active(stt_: "Qwen3ASRSTT") -> bool:
    """收线/告别直念窗旗（F4 二修）：agent 在播分支动作【收线】台词期间置位。

    此窗内流把一切成轮事件（句级提交/EOS/FINAL）静默丢弃——电话本就要结束，
    半句道歉比什么都差。getattr 鸭型访问（测试 fake/未接线的 STT 无此属性时
    同「旗未置」）。"""
    return bool(getattr(stt_, "_closing_say", False))


def sentence_commit_enabled() -> bool:
    """句级提交总门:QWEN3_ASR_SENTENCE_COMMIT(默认 1)且框架轮次判定=stt。

    TURN_DETECTION≠stt(EOT/vad kill-switch)时必须停发:非 stt 模式下框架忽略
    STT 的 END_OF_SPEECH(audio_recognition.py:1292 分支要求 mode=="stt"),说话
    中途的句子 FINAL 只会叠加进 _audio_transcript,与停嘴整段 FINAL 重复入历史。
    两个 env 单点各读一行:agent.py 的 TURN_DETECTION 默认值与此处保持同源(都
    默认 "stt"),唔共享 import(agent→providers 已是依赖方向,反向会成环)。
    """
    if os.environ.get("QWEN3_ASR_SENTENCE_COMMIT", "1") != "1":
        return False
    return os.environ.get("TURN_DETECTION", "stt").strip().lower() == "stt"


def _pause_trigger_enabled() -> bool:
    """VAD 微停顿句边界触发（QWEN3_ASR_SENTENCE_PAUSE_TRIGGER，默认 1）。

    p6 实测：mlx ASR 说话期滑窗 partial 对句间停顿只打逗号不打句号（MiniMax
    生产夹具同样）→ 强标点门在真实音频上结构性不触发。句边界需要非标点事件源
    ——本流内置 VAD 的 END_OF_SPEECH（≥min_silence 0.45s 真静音）就是句间停顿：
    人打电话句间停 0.3-0.8s。只在 sentence_commit_enabled() 之上叠加（总门关
    则全关，kill-switch 一并灭掉）。
    """
    return os.environ.get("QWEN3_ASR_SENTENCE_PAUSE_TRIGGER", "1") == "1"


def _hesitation_gate_on() -> bool:
    """纯犹豫残片门(QWEN3_ASR_HESITATION_GATE,默认开;0=回退成轮)。"""
    return os.environ.get("QWEN3_ASR_HESITATION_GATE", "1") == "1"


def _chunk_keep_enabled() -> bool:
    """D4 止血总门(QWEN3_ASR_CHUNK_KEEP,默认 1;0=回退旧「先清后发」档)。

    旧档:`_maybe_partial` 先 `_pending.clear()` 再 POST sidecar——瞬时不可用
    (连接拒/超时)时该窗 PCM 永久丢失且零日志。新档:「成功才清」,失败保留
    (见 `_chunk_keep_plan` 与调用处注释)。
    """
    return os.environ.get("QWEN3_ASR_CHUNK_KEEP", "1") == "1"


# D4 保留窗有界上限:12s(PCM16 单声道 16k)。对齐 sidecar partial 滑窗上限档
# (PARTIAL_MAX_SEC)——超出后最老音频连 partial 都不再覆盖,finish 整句重解
# 成本还线性涨,保留更老内容只有成本冇收益。有界截断丢最旧并打点,sidecar
# 长时不可用时 _pending 绝不无界增长。
_ASR_CHUNK_KEEP_MAX_BYTES = 16000 * 2 * 12


def _pcm_window_ms(nbytes: int) -> int:
    """PCM16 单声道 16k 字节数 → 毫秒(CHUNK_POST_ERR 打点可读面)。"""
    return int(nbytes / 2 / 16000 * 1000)


def _chunk_keep_plan(
    pending_len: int, keep_max_bytes: int = _ASR_CHUNK_KEEP_MAX_BYTES
) -> tuple[str, int]:
    """D4 POST 失败后 `_pending` 处置计划(纯函数,离线可测 tests/test_asr_chunk_keep.py)。

    返回 `(action, drop_bytes)`:action="keep"=整窗保留(drop=0);"trim"=超出
    上限,需从 _pending 头部(最旧)丢 drop_bytes 字节再保留。调用方据返回值
    `del self._pending[:drop]` 并打 action 点。

    **重复提交结论(读码证据,services/qwen3-asr-sidecar/app.py)**:sidecar
    `/api/chunk` 是**追加式**——`session["chunks"].extend(pcm)`,`/api/finish`
    对**整段累积 buffer** 解码。同一段 PCM 提交两次 → chunks 里双份音频 →
    转写重复。所以失败后**不主动重发**:保留在 _pending 头部,随下一窗
    POST/finish 尾段**自然带上**即补齐(常见失败=连接拒/DNS,服务端从未收到,
    带上恰好一份;「超时但服务端已收」的窄竞态下可能双写一次——概率与代价
    远小于旧版整窗永久丢失,且无法从客户端可靠区分,取舍记此)。
    """
    if pending_len <= keep_max_bytes:
        return "keep", 0
    return "trim", pending_len - keep_max_bytes


def _join_hold_s() -> float:
    """跨段拼接 hold 窗(秒):QWEN3_ASR_JOIN_HOLD_MS,默认 800;0=关(行为同旧)。

    报号句中微停顿(≥0.45s VAD 静音)会把一句 WhatsApp 号码切成两个 VAD 段、两条
    FINAL、两个用户轮(c4f6e4f1 实证:两 FINAL 仅差 103ms,语境「我的WhatsApp是」与
    数字「一七二二三三四」分居两轮→捕获结构性失败、AI 插话复读)。续接可能句喺
    END_OF_SPEECH 嗰刻 hold 唔发,sidecar session 续命等下一段并入同一会话。
    """
    try:
        return max(0.0, int(os.environ.get("QWEN3_ASR_JOIN_HOLD_MS", "800"))) / 1000.0
    except ValueError:
        return 0.8


_JOIN_DIGIT_TRANS = str.maketrans("零一二三四五六七八九０１２３４５６７８９", "01234567890123456789")
_JOIN_EN_DIGIT_RE = re.compile(r"\b(zero|oh|one|two|three|four|five|six|seven|eight|nine)\b", re.IGNORECASE)
_JOIN_EN_DIGIT_MAP = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
                      "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}


def _join_norm_digits(text: str) -> str:
    """join 门专用的轻量数字归一(汉字/全角/英文数字词→连续数字串)。

    唔直接 import flow._digit_normalize:providers 层保持 agent→providers 单向依赖
    (echo guard 懒 import 同一款理由),呢把尺只服务 join 门,唔参与号码捕获。
    非数字字符全剥(空格/字母)——门只关心「有冇数字 run」,唔做号码比对。
    """
    lowered = _JOIN_EN_DIGIT_RE.sub(lambda m: _JOIN_EN_DIGIT_MAP[m.group(1).lower()], str(text).lower())
    return re.sub(r"[^0-9]", "", lowered.translate(_JOIN_DIGIT_TRANS))


def _join_worthy(text: str) -> bool:
    """续接可能句:归一后有 ≥2 位数字(汉字数字/英文数字词都算,号码/价格/日期常见)、
    或以系词收尾(係/系/是/is,英文只认独立词)。呢类句每轮多等一个 hold 窗;
    其余普通陈述句零加迟。"""
    t = (text or "").strip()
    if not t:
        return False
    if len(_join_norm_digits(t)) >= 2:
        return True
    return bool(re.search(r"(?:係|系|是|\bis\b)\s*[。，,．.！!？?～~]*$", t, re.IGNORECASE))


def _join_hold_vocab_enabled() -> bool:
    """品牌/领域词防拆轮门(2026-09-09 S3 拼多多专项):partial 尾部是热词词表
    某词的【严格前缀】(词可能未讲完)→ hold 停嘴窗等续段并单会话,整句高精度
    解码——真实话音「京|東」微停顿把「京东」烂成「金东北」、「拼|多多」吞「拼」
    (probe_brand_words 基线 10/16 实证)。词表与 ASR context 软偏置同一份
    (模板 hotwords + 行业词 + 对象 courier/contact_channel),运营加词即生效。
    QWEN3_ASR_JOIN_HOLD_VOCAB=0 关(回退纯数字/系词门)。"""
    return os.environ.get("QWEN3_ASR_JOIN_HOLD_VOCAB", "1") == "1"


def _parse_vocab_terms(hotword_ctx: str) -> tuple[str, ...]:
    """从「Vocabulary: w1, w2, …」格式热词 context 反解词表(≥2 字词才有前缀信号)。"""
    if not hotword_ctx:
        return ()
    raw = str(hotword_ctx).split("Vocabulary:", 1)[-1]
    return tuple(
        t.strip() for t in raw.split(",") if len(t.strip()) >= 2
    )


def _vocab_prefix_hold(text: str, terms) -> bool:
    """partial 尾部是否词表某词的严格前缀。

    CJK:剥标点连写串取长 1-3 后缀——「件货喺京」→ 京 ⊂ 京东 → True(词可能被
    停顿拦腰);「查下單號」→ 號 唔係任何词开头 → False(词已完整,照常提交)。
    latin:只认【末词】整词(what ⊂ WhatsApp)——不取 1-2 字尾,否则任何 t/wh
    结尾的英文句都误扣 hold 窗。"""
    raw = str(text or "")
    if not raw:
        return False
    cjk = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", raw)
    candidates: list[str] = [cjk[-n:] for n in (1, 2, 3) if len(cjk) >= n]
    m = re.search(r"([A-Za-z][A-Za-z0-9']*)\s*[。，,．.！!？?～~]*$", raw)
    if m:
        candidates.append(m.group(1).lower())
    for tail in candidates:
        tl = tail.lower()
        for term in terms:
            t = str(term).lower()
            if t.startswith(tl) and len(tl) < len(t):
                return True
    return False


def _has_latin_or_digit_run(text: str, min_len: int = 2) -> bool:
    """句内含 ≥min_len 连续 ASCII 字母/数字 run(单号/WhatsApp 号码高危)。

    与 sidecar `_has_latin_or_digit`(services/qwen3-asr-sidecar/app.py)同一家族:
    号码/英文是转写最高危内容,句级 FINAL 一旦把半截号码当整句提交,框架按句落
    历史 + LLM 抢答,停嘴整句兜底都救唔返。≥2 连写才拦(单个字母夹句如「A 嘅」
    唔至于误提交号码)。
    """
    run = 0
    for ch in text:
        if ch.isascii() and ch.isalnum():
            run += 1
            if run >= min_len:
                return True
        else:
            run = 0
    return False


def _length_commit_cut(text: str, start: int, prev_full: str, min_chars: int) -> int | None:
    """说话中长度触发切点(纯函数,单测用):返回可提交边界(排他索引)或 None。

    门(缺一不可):
    - 切段 ≥ min_chars 字数口径按下述中英判;
    - 切点不劈 ASCII 字母/数字 run——单号/英文词保持完整(数字零降级铁律的
      B 线化身;整段含数字 run 唔再拦:B 线无 WhatsApp 捕获语义,号码子句照翻
      係译员本分,这是与标点档 `_has_latin_or_digit_run` 整段门的唯一分歧点);
    - 中英判:切段 CJK 字 ≥ ASCII 字母数字 → 门槛 = min_chars(中文 4 字/秒,
      12 字≈3s 话音一翻);否则(英/数字主导)门槛翻倍 2×min_chars(英文 ~5 字/词,
      防两词一翻的机关枪碎片);
    - 跨窗稳定:prev_full[start:cut] 与 text[start:cut] 逐字相同(滑窗 ~300ms
      一窗,提交的字至少已稳定一窗)。前缀不稳则更长前缀必不稳,直接 None 等
      下窗,唔使继续扫。
    """
    n = len(text)
    for cut in range(start + min_chars, n + 1):
        prev_ch = text[cut - 1]
        next_ch = text[cut] if cut < n else ""
        if prev_ch.isascii() and prev_ch.isalnum() and next_ch.isascii() and next_ch.isalnum():
            continue  # ASCII run 中间,不劈
        seg = text[start:cut]
        cjk = sum(1 for ch in seg if "\u3400" <= ch <= "\u9fff")
        latin = sum(1 for ch in seg if ch.isascii() and ch.isalnum())
        if cut - start < (min_chars if cjk >= latin else min_chars * 2):
            continue
        if prev_full[start:cut] != text[start:cut]:
            return None  # 该前缀未跨窗稳定,更长前缀必同 → 等下窗
        return cut
    return None


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
    - 句级提交（sentence_commit_enabled()，turn_detection="stt" 默认开）：partial
      稳定出现强句标点（。！？!?）且过门（≥6 字/无数字·字母连写/跨窗稳定/1.5s
      限速）→ 按句发 FINAL_TRANSCRIPT(句子)+END_OF_SPEECH，框架按句 commit 轮次
      （官方 stt 模式契约 audio_recognition.py:1292-1327），LLM+TTS 在客户仍在
      说话时就开跑——speech-end→first-audio 的唯一 ≤1s 路径。停嘴路径只补发
      未提交尾巴，已提交句子唔会随整段重复入历史。
    - VAD 微停顿触发（_pause_trigger_enabled()，默认开）：p6 实测真实音频滑窗
      partial 句间只出逗号、强标点门结构性不触发 → 内置 VAD END_OF_SPEECH
      （≥0.45s 真静音=句间停顿）作为第二边界源：停嘴那刻 partial 稳定够格就
      直接按句提交（免等 finish 往返），再走正常停嘴路径补尾巴。
    """

    def __init__(self, stt_, *, vad, conn_options):
        super().__init__(stt=stt_, conn_options=conn_options, sample_rate=16000)
        self._stt_ = stt_  # 内层 Qwen3ASRSTT（base_url/语言状态/钉定）
        self._vad = vad
        self._session_id: str | None = None
        self._pending = bytearray()  # 尚未 POST 给 sidecar 的增量 PCM
        self._last_partial = ""  # 上一窗全文（INTERIM 去重 + 句边界稳定性参照）
        self._prev_partial = ""  # 稳定前缀参照窗
        self._stable = ""  # 已发 PREFLIGHT 的最长稳定前缀（未提交剩余坐标系）
        self._last_post = 0.0
        self._finishing = False
        self._last_lang = ""  # 最后一窗 partial 的归一语言（VAD 停嘴提交锚定用）
        # 句级提交状态（sentence_commit_enabled() 关闭时恒空/0，行为同旧）：
        self._committed_text = ""  # 已按句提交的句子拼接（FINAL 已发、框架已 commit）
        self._last_sentence = ""  # 最后提交句（finish 整段坐标失配时的回退锚）
        self._commit_idx = 0  # 滑窗全文坐标的已提交位置（每句边界只发一次）
        self._last_sentence_commit_at = 0.0  # 上次句级提交时刻（monotonic，限速用）
        # 跨段拼接 hold 状态(join_worthy 句等续段;唔入 _reset——段间记忆係佢嘅存在意义):
        self._join_hold_active = False
        self._join_task: asyncio.Task | None = None
        # sidecar 会话纪元:每次 _start_session 成功 +1。hold 超时 flush 凭佢识别
        # 「finish 等待期间客户已续讲、新会话已开」——咁就唔可以 reset 新会话状态
        # (否则续讲段 partial/_session_id 被清,整轮无 FINAL,2026-09-07 审查实证)。
        self._session_epoch = 0
        # join-hold 词表前缀门用的热词词表(与 context 软偏置同一份,流级缓存);
        # fake/无 context → 空 tuple,门自动失效。
        self._vocab_terms = _parse_vocab_terms(getattr(stt_, "_hotword_context", ""))
        # 词表回声事件账本(call-46b94ebd/1043de7c):确认过一次剥尾/纯回声后,
        # 后续词表孤词残片按回声衰落丢弃——首现孤词保留(真人可能真讲「微信」)。
        self._vocab_echo_seen: bool = False

    def _turn_partial_for_fallback(self) -> str:
        """本段 partial 末稿(供 agent 侧 E2 fallback_text),**未提交坐标系**。

        终稿是同一坐标系(句级提交句 / 停嘴 tail payload):fallback 与终稿对同一
        段话比才有意义;整窗原文会把已提交句子也带上,而 ≥2 热词泄漏的 fallback
        回吐路径(sanitize 步骤 6)会把那段已入史的话当新话再喂一次。

        与 `_uncommitted` 的差别(故不复用它):①**零副作用零日志**——`_uncommitted`
        在重解修正分支会打 ``QWEN3_ASR_REDECODE_DROP``,每窗多叫一次=同一窗重复
        日志;本方法只做前缀剥离/定位。②坐标失配(窗口被重写、定位不到已提交前缀)
        时**不猜**:原样返回整窗——fallback 只是泄漏判据的辅助证据,给长了判据自然
        不成立,给错段会误剥。无人读时成本=两次字符串前缀比较。
        """
        text = self._last_partial
        if not text:
            return ""
        committed = self._committed_text
        if committed and text.startswith(committed):
            return text[len(committed):].lstrip(_UNCOMMITTED_LEADING_WEAK_PUNCT)
        if committed and self._last_sentence:
            pos = text.rfind(self._last_sentence)
            if pos >= 0:
                return text[pos + len(self._last_sentence):].lstrip(_UNCOMMITTED_LEADING_WEAK_PUNCT)
        return text

    def _publish_turn_partial(self, text: str) -> None:
        """把「本轮 partial 末稿」贴到内芯暴露位(agent 侧 E2 fallback_text 取口)。

        空串=no-op(不是清):空白窗(未过已提交前缀/重解修正丢弃)不该把上一条
        **有内容**的 partial 抹掉——fallback 要的正是「最近一条真 partial」。清零
        是 `_reset` 的职责。

        纯属性写:无 await、无 IO、无账本副作用,任何线程/协程时机都可调。
        """
        text = str(text or "")
        if text:
            self._stt_._turn_partial_text = text

    def _echo_filter(self, text: str, src: str) -> str:
        """词表回声统一闸(剥尾保头版,2026-09-12 call-46b94ebd P0)。

        旧版停嘴 FINAL 用纯回声判定(_is_hotword_vocab_echo)整条丢弃——真答案
        词恰在词表里(「拼多多。顺豐速運,運通…」平台答案)连真实回答一起丢,
        客户抱怨触发假推进。四个闸口(停嘴/join-flush/interim/句级提交)统一
        换 hook 层 _vocab_echo_guard 三件套语义:真话头+词表尾→剥尾保头;纯回声
        /echo_seen 后孤词残片→空串(调用方丢弃)。QWEN3_HOTWORD_ECHO_GUARD=0 关。"""
        if os.environ.get("QWEN3_HOTWORD_ECHO_GUARD", "1") != "1":
            return text
        clean, self._vocab_echo_seen = _vocab_echo_guard(
            text,
            getattr(self._stt_, "_hotword_context", "") or "",
            echo_seen=self._vocab_echo_seen,
        )
        if clean != text:
            if clean.strip("。，, 、;；"):
                print(f"QWEN3_HOTWORD_ECHO_STRIP src={src} payload={text!r} keep={clean!r}", flush=True)
            else:
                print(f"QWEN3_HOTWORD_ECHO_DROP src={src} payload={text!r}", flush=True)
        return clean

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
                    # join-hold 期间续段嚟到:取消超时 flush。sidecar session 仲生猛
                    # (hold 唔 finish),唔好重复 start——orphan 旧会话会令拼接变两段。
                    self._cancel_join_hold()
                    self._event_ch.send_nowait(stt.SpeechEvent(stt.SpeechEventType.START_OF_SPEECH))
                    if not self._session_id:
                        await self._start_session()
                        # VAD 起报前导喂会话(2026-09-12「快语速吃首字」修复):官方
                        # silero 把 prefix padding(0.5s)+min_speech 确认窗的音频挂在
                        # START 事件 frames 里交还(官方 StreamAdapter 在 END 用同一
                        # buffer 识别);旧版增量会话无视之,起报前的 INFERENCE_DONE
                        # 帧又被 not started 跳过——sidecar 从「确认说话」那刻才收
                        # 音频,快语速首 1-3 字结构性缺失、finish 重解也救不回音频。
                        # 新开会话时并入 _pending(下一个 INFERENCE_DONE 的
                        # _maybe_partial 自动喂走);hold 续段(会话存活、INFERENCE_
                        # DONE 全程在喂)不并入,防音频重复。
                        if event.frames:
                            try:
                                self._pending.extend(
                                    bytes(utils.merge_frames(event.frames).data)
                                )
                            except Exception:  # noqa: BLE001 - pre-roll 合帧失败不致命
                                pass
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
                    # ---- 收线/告别直念窗(F4 二修,call-179c7608):整段静默丢弃 --
                    # 分支动作【收线】台词正在播时,客户尾随片段(「我不是这个人。」)
                    # 成新轮会打断在播台词(半句道歉)+落 LLM 兜话。stt 模式下框架
                    # 收到 EOS 就会 commit 轮(audio_recognition _run_eou_detection
                    # trigger="stt"),所以此窗内 EOS 也不发——整段直接放弃:电话本
                    # 就要结束,把告别说完比什么都优先。非此窗语义逐字节不变。
                    if _late_final_guard_on() and _closing_say_active(self._stt_):
                        started = False
                        self._finishing = False
                        self._reset()
                        print("QWEN3_ASR_CLOSING_SAY_SUPPRESS src=segment_eos", flush=True)
                        continue
                    # ---- 跨段拼接 hold(治报号句被微停顿切碎,2026-09-06)----
                    # 数字/字母句被句级门有意排除(防半截号码提前提交)→ 永远走逐段
                    # 整句路径,VAD 微停顿即拆轮。续接可能句喺呢度唔发 END_OF_SPEECH、
                    # 唔 finish、唔 reset:sidecar session 续命,下一段音频继续入同一
                    # 会话(partial 继续滚),真正停嘴嗰刻一条 FINAL 覆盖全段——下游
                    # flow/侦测/LLM/KV-cache 全部只见单一轮。超时冇续段 → _hold_flush
                    # 走正常停嘴路径(该轮多等 HOLD_MS——含数字/系词句都算,唔止号码句)。0=回退同旧。
                    _vocab_hit = False
                    _join_hit = False
                    if self._join_hold_active:
                        # hold 中又嚟 EOS(冇 START 嘅边路)→ 当真停嘴,取消 flush 落埋正常路径。
                        self._cancel_join_hold()
                    else:
                        _vocab_hit = (
                            _join_hold_s() > 0
                            and _join_hold_vocab_enabled()
                            and bool(self._vocab_terms)
                            and (self._last_partial or "").strip()
                            and _vocab_prefix_hold(self._last_partial, self._vocab_terms)
                        )
                        _join_hit = (
                            not _vocab_hit
                            and _join_hold_s() > 0
                            and (self._last_partial or "").strip()
                            and _join_worthy(self._last_partial)
                        )
                    if _vocab_hit or _join_hit:
                        self._join_hold_active = True
                        self._finishing = False  # hold 期间 partial 继续滚(INFERENCE_DONE 唔 skip)
                        self._join_task = asyncio.create_task(self._hold_flush())
                        print(
                            f"QWEN3_ASR_JOIN_HOLD src={'vocab' if _vocab_hit else 'digits/copula'} "
                            f"chars={len(self._last_partial)} "
                            f"hold_ms={int(_join_hold_s() * 1000)}",
                            flush=True,
                        )
                        continue
                    self._finishing = True
                    speech_end_time = time.time() - event.silence_duration - event.inference_duration
                    # ---- VAD 微停顿句级提交（pause trigger，默认开）--------------
                    # 内置 VAD END_OF_SPEECH = 本段语音后已有 ≥min_silence(0.45s)
                    # 真静音——人打电话句间停 0.3-0.8s，这里多数就是句边界。此刻
                    # 滑窗 partial 已带着整段静音重解过（文本齐），直接按句提交：
                    # 框架 commit = max(停嘴+min_delay, FINAL 到达)——FINAL 免等
                    # /api/finish 往返（~0.15-0.3s），commit 同量提前。守卫照句级
                    # 门（≥6 字/无数字 run/跨窗稳定/1.5s 限速）；partial 空或不
                    # 稳定 → 不提交，下面正常 finish 路径整句兜底（零行为差）。
                    if sentence_commit_enabled() and _pause_trigger_enabled():
                        pause_commit = self._sentence_boundary(
                            self._last_partial, self._prev_partial, allow_eos=True
                        )
                        if pause_commit is not None:
                            sentence, end_idx = pause_commit
                            # 标点扫描分支的门槛是 6（说话中按句提交用）——vad-pause
                            # 语境必须再套 10 字碎片门：带句号的 6-9 字碎片会从 punct
                            # 分支漏出，在用户话音未落时提前成轮，回复立刻被自家尾巴
                            # 的音频活动掐死（multi_turn E2E 2026-09-07 实证：3 轮全
                            # 被吃掉只剩心跳）。唔够格就留给停嘴 finish 整句兜底。
                            if len(sentence) >= _pause_commit_min_chars():
                                self._emit_sentence_commit(
                                    sentence, end_idx, self._last_lang, time.monotonic(), source="vad-pause"
                                )
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH, speech_end_time=speech_end_time)
                    )
                    text, lang = await self._finish_session()
                    # 句级提交已发过的句子 FINAL（框架按句 commit 落了历史）唔可以随
                    # 整段再发一次（重复入转写）——只补「未提交尾巴」。句级门关时
                    # _committed_text 恒空，_uncommitted 原样返回 → 行为同旧。
                    committed_before = self._committed_text
                    payload = self._uncommitted(text) if (text and committed_before) else text
                    # 短尾唔补发第二条 FINAL（打断自噬修复，2026-09-05 粤语实测）：
                    # pause-commit 刚提交半句、回复生成中，紧跟的 <6 字纯语气短尾
                    # （「係。」「嗯。」）若再发一条 FINAL → 新用户轮把未出声的
                    # 回复 interrupt 掉 → 每问无答、8s 后心跳顶替——纯语气词照丢
                    # （信息量低）；**带内容的短尾豁免**（2026-09-07）：数字/字母
                    # 串（「四五七。」补报单号）或任何实词（「我唔知。」）照发成轮
                    # ——回复被新轮掐掉但新轮带着真内容，AI 直接回应它，好过吞掉
                    # 号码/答复（「说两句第二句被吞→AI 不回话」的根因）。
                    if committed_before and payload and len(payload) < _ASR_SENTENCE_MIN_CHARS and not _tail_carries_content(payload):
                        payload = ""
                    if payload:
                        payload = self._echo_filter(payload, "stop-mouth")
                    if payload and _hesitation_gate_on() and _pure_hesitation(payload):
                        print(f"QWEN3_ASR_HESITATION_DROP payload={payload!r}", flush=True)
                        payload = ""
                    # 迟到 FINAL 尾巴护栏（F2）：AI 生成/播报中，相对已提交文本
                    # 只是极短追加的 finish 尾巴 → 判重解幻听碎片，唔补发（否则
                    # 新用户轮会把在播的快路罐头/直念回复掐断、assistant item
                    # 不落库）。真·新话（足够长/含数字字母）照发可打断。
                    # closing_say 窗（F4 二修）：在播的係收线台词时任何长度都丢弃。
                    if payload and committed_before and _late_final_guard_on():
                        _cs = _closing_say_active(self._stt_)
                        if _cs or not late_final_is_new_speech(
                            payload,
                            committed_before,
                            agent_busy=bool(getattr(self._stt_, "_reply_busy", False)),
                            max_tail_chars=_late_final_max_tail_chars(),
                        ):
                            print(
                                f"QWEN3_ASR_LATE_FINAL_DROP reason="
                                f"{'closing_say' if _cs else 'tail_append'} "
                                f"committed={committed_before!r} tail={payload!r}",
                                flush=True,
                            )
                            payload = ""
                    started = False
                    self._finishing = False
                    # pre-reset 快照:本段 partial 末稿(_reset 会清暴露位,而这条
                    # FINAL 的 fallback 正是它——agent 的 on_user_turn_completed/
                    # _on_conversation_item 在本 FINAL 之后才跑,那时已是新的一段)。
                    fallback_tail = self._turn_partial_for_fallback()
                    self._reset()
                    if payload:
                        # 只给真发出去的 FINAL 重贴;短尾/纯 dump/迟到护栏丢弃=无
                        # FINAL → 保持空(no-op 亦不会把空贴上)。
                        self._publish_turn_partial(fallback_tail)
                        self._stt_._language_state.update(lang, payload)
                        self._event_ch.send_nowait(
                            stt.SpeechEvent(
                                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                                alternatives=[stt.SpeechData(language=self._stt_._language_state.lang, text=payload)],
                                speech_end_time=speech_end_time,
                            )
                        )

        try:
            await asyncio.gather(_forward_input(), _recognize())
        finally:
            # 流关闭时撤掉 hold flush,唔好留孤儿任务向已死 event_ch 发事件。
            # (暴露位**不在这里清**:收官 FINAL 的 agent 钩子可能仍在途,清掉就
            # 白丢这条 fallback;内芯 Qwen3ASRSTT 本就是每通一个,无跨通残留,
            # 清零交给 _reset/_start_session——那两处只影响「谁算本轮」。)
            self._cancel_join_hold()

    def _cancel_join_hold(self) -> None:
        self._join_hold_active = False
        task = self._join_task
        if task is not None and not task.done():
            task.cancel()
        self._join_task = None

    async def _hold_flush(self) -> None:
        """join-hold 超时:续段冇嚟(客户真停嘴)→ 走正常停嘴路径出 FINAL。
        与 _run 的 END_OF_SPEECH 分支同一套动作(EOS→finish→短尾规则→reset→FINAL),
        只是晚 _join_hold_s() 秒执行;期间新 START_OF_SPEECH 会 cancel 咗呢个任务。"""
        await asyncio.sleep(_join_hold_s())
        if not self._join_hold_active:
            return
        # 收线/告别直念窗(F4 二修):held 段同样静默放弃(见 _run EOS 分支注)。
        if _late_final_guard_on() and _closing_say_active(self._stt_):
            self._join_hold_active = False
            self._join_task = None
            print("QWEN3_ASR_CLOSING_SAY_SUPPRESS src=join_flush", flush=True)
            return
        self._join_hold_active = False
        self._join_task = None
        _epoch_at_hold = self._session_epoch
        self._finishing = True
        # 停嘴时钟锚点：hold 从原 EOS 时刻起算——真实停嘴 = 现在 - hold 窗。
        # 锚到 flush 时刻会令 min_delay 全额叠加在 hold 窗之后（白付 0.25s）。
        speech_end_time = time.time() - _join_hold_s()
        self._event_ch.send_nowait(
            stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH, speech_end_time=speech_end_time)
        )
        text, lang = await self._finish_session()
        committed_before = self._committed_text
        payload = self._uncommitted(text) if (text and committed_before) else text
        # 与 _run 正常 EOS 分支同一套短尾规则:带内容短尾(数字/字母/实词)豁免照发
        # ——「六四三二」补报号码好过吞掉(2026-09-07 审查:hold 路漏抄豁免)。
        if committed_before and payload and len(payload) < _ASR_SENTENCE_MIN_CHARS and not _tail_carries_content(payload):
            payload = ""
        if payload:
            payload = self._echo_filter(payload, "join-flush")
        if payload and _hesitation_gate_on() and _pure_hesitation(payload):
            print(f"QWEN3_ASR_HESITATION_DROP payload={payload!r}", flush=True)
            payload = ""
        # 迟到 FINAL 尾巴护栏（F2）：与 _run 停嘴分支同一把尺（join-flush 路的
        # finish 尾巴同样会成新轮掐断在播回复）。closing_say 窗（F4 二修）任何
        # 长度都丢弃。
        if payload and committed_before and _late_final_guard_on():
            _cs = _closing_say_active(self._stt_)
            if _cs or not late_final_is_new_speech(
                payload,
                committed_before,
                agent_busy=bool(getattr(self._stt_, "_reply_busy", False)),
                max_tail_chars=_late_final_max_tail_chars(),
            ):
                print(
                    f"QWEN3_ASR_LATE_FINAL_DROP reason="
                    f"{'closing_say' if _cs else 'tail_append'} "
                    f"committed={committed_before!r} tail={payload!r}",
                    flush=True,
                )
                payload = ""
        self._finishing = False
        if self._session_epoch == _epoch_at_hold:
            # pre-reset 快照(同上停嘴路径):本段 partial 末稿。
            fallback_tail = self._turn_partial_for_fallback()
            self._reset()
            if payload:
                self._publish_turn_partial(fallback_tail)
        # else: finish 等待期间 START 已开新 sidecar 会话——新会话状态属续讲段照常
        # 滚动,本 flush 只负责把上一段 FINAL 发出(纪元守卫,防成轮转写被清);
        # 暴露位同样归新会话(勿用旧段 partial 覆盖新段已贴上的值)。
        if payload:
            self._stt_._language_state.update(lang, payload)
            self._event_ch.send_nowait(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                    alternatives=[stt.SpeechData(language=self._stt_._language_state.lang, text=payload)],
                    speech_end_time=speech_end_time,
                )
            )
        print(f"QWEN3_ASR_JOIN_FLUSH chars={len(payload or '')}", flush=True)

    async def _start_session(self) -> None:
        # 新语音段开场 = 新一轮:`_reset` 已置 _session_id=None,故每段第一声必走
        # 这里——上一轮的 partial 末稿到此为止,本轮 partial 到达前暴露位恒空(短
        # 句无 partial 的轮也拿不到上一轮的话)。join-hold 续段会话存活、不走这里
        # =同一轮,暴露值照留(语义正确)。
        self._stt_._turn_partial_text = ""
        lang_hint = _asr_language_hint(self._stt_._language_state.lang, self._stt_._pin_language)
        # start 参数:language hint + 热词 context(同 offline 路径,空则不下发;
        # getattr 鸭型访问——测试 fake 无此属性时等同空)+ partial 间隔档
        # (agent 生成中抑制,GPU 竞态专项;None=不下发用 env 默认)。
        start_params: dict[str, str] = {}
        if lang_hint:
            start_params["language"] = lang_hint
        if getattr(self._stt_, "_hotword_context", ""):
            start_params["context"] = self._stt_._hotword_context
        _pm = getattr(self._stt_, "_partial_ms_override", None)
        if _pm:
            start_params["partial_ms"] = str(int(_pm))
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{self._stt_._base_url}/api/start",
                    params=start_params or None,
                )
                r.raise_for_status()
                self._session_id = r.json()["session_id"]
                self._session_epoch += 1
        except Exception as exc:  # noqa: BLE001 - 建会话失败 → 整句路径照样可用
            self._session_id = None
            print(f"QWEN3_ASR_PARTIAL start failed: {exc!r}", flush=True)

    async def _apply_partial_ms(self, ms: int | None) -> None:
        """已开的 sidecar 会话即时调 partial 档(生成中抑制/listening 恢复)。

        无开会话时静默跳过——下个 _start_session 会带上 override。
        """
        if not self._session_id:
            return
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"{self._stt_._base_url}/api/partial_ms",
                    params={"session_id": self._session_id, "ms": str(int(ms)) if ms else ""},
                )
        except Exception as exc:  # noqa: BLE001 - 调档失败不影响转写主链路
            print(f"QWEN3_ASR_PARTIAL tune failed: {exc!r}", flush=True)

    async def _maybe_partial(self) -> None:
        now = time.monotonic()
        # _ASR_PARTIAL_POST_MS 名义毫秒;monotonic() 是秒——不除 1000 会把
        # 节流当成 300 秒,首调还依赖系统运行时长>300s(CI 新 runner 恒早退,
        # preflight 单测空事件 flake 的根因)。除后=真正的 ≥300ms 节流。
        if not self._session_id or now - self._last_post < _ASR_PARTIAL_POST_MS / 1000.0:
            return
        if len(self._pending) < 16000 * 2 * 0.6:  # <0.6s 无转写价值
            return
        self._last_post = now
        pcm = bytes(self._pending)
        if not _chunk_keep_enabled():
            # 旧档(QWEN3_ASR_CHUNK_KEEP=0):先清后发——POST 失败整窗丢失(回退口)。
            self._pending.clear()
        # 新档(D4 止血,2026-09-20):「成功才清」。此处不删,POST 成功后再 del 已发
        # 前缀(await 期间 INFERENCE_DONE 新到的音频留在尾部,不误删);失败保留整窗
        # 随下一窗/finish 自然带上——sidecar /api/chunk 追加式,重复 POST 同段
        # PCM 会重复转写,故不主动重发(取证与取舍见 _chunk_keep_plan 注释)。
        # 停嘴时钟锚点（2026-09-09 S5）:本窗音频在 POST 发出时刻已讲完,句级提交
        # 的 END_OF_SPEECH 带上它——框架 min_delay 锚定 speech_end_time（停嘴时刻,
        # audio_recognition.py:1332-1336/1679-1681）,锚点缺失会坍缩为 now,令
        # min_delay 全额叠在解码延迟之后（白付 ~0.25s/句）。
        t_req_wall = time.time()
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
        except Exception as exc:  # noqa: BLE001 - partial 尽力而为,忙时/抖动不阻主链
            # D4:失败必须可见(旧版静默 return=丢转写无痕);保留面有界截断。
            if _chunk_keep_enabled():
                action, drop = _chunk_keep_plan(len(self._pending))
                if drop:
                    del self._pending[:drop]
                print(
                    f"QWEN3_ASR_CHUNK_POST_ERR window_ms={_pcm_window_ms(len(pcm))} "
                    f"kept_ms={_pcm_window_ms(len(self._pending))} action={action} "
                    f"err={exc!r}",
                    flush=True,
                )
            else:
                print(
                    f"QWEN3_ASR_CHUNK_POST_ERR window_ms={_pcm_window_ms(len(pcm))} "
                    f"kept_ms=0 err={exc!r}",
                    flush=True,
                )
            return
        if _chunk_keep_enabled():
            # 成功才清:只删已发前缀(新档);旧档已在 POST 前清空,勿再删尾部新音频。
            del self._pending[: len(pcm)]
        text = str(data.get("text") or "")
        if not text:
            return
        if text == self._last_partial:
            # 滑窗文本没变（静音期 partial 稳定重解同文）：参照窗同步到当前窗
            # ——VAD 微停顿提交的「跨窗稳定」门才能在静音期成立（否则 prev 停在
            # 说话最后一窗，EOS 时刻 prev≠last，pause trigger 永不满足）。
            self._prev_partial = self._last_partial
            return
        lang = _normalize_asr_language(str(data.get("language") or ""), text)
        self._last_lang = lang
        prev_full = self._last_partial
        self._last_partial = text
        # 滑窗 partial 末稿对外暴露(agent 侧 E2 fallback_text):与 INTERIM 字幕
        # 同坐标系(未提交剩余)。「文本没变」的早退分支不清也不算变化——同一段话;
        # 句级提交分支在下面才走,故这里贴的恒是**提交前**的那一版(该 FINAL 的
        # fallback 正是它)。
        self._publish_turn_partial(self._turn_partial_for_fallback())

        # ---- 句级提交（turn_detection="stt"，VAD 说话中才会走到这里）----------
        # 能进 _maybe_partial 即 VAD 仍在语音段（INFERENCE_DONE 且非 finishing）：
        # VAD 已停的场景由 _run 的 END_OF_SPEECH 分支整句兜底，双重提交天然排除。
        # 每句事件序：FINAL_TRANSCRIPT(句子) → END_OF_SPEECH。框架侧（官方契约
        # audio_recognition.py:1174-1238/1292-1327）：FINAL 在 speaking 中只累计
        # _audio_transcript + 喂抢跑；EOS 置 committed + _run_eou_detection(trigger
        # ="stt")（endpointing min_delay 仍适用）→ 按句建轮，LLM+TTS 与客户说话
        # 重叠。提交窗不发 PREFLIGHT（文本已权威提交，省一次投机预算）。
        # 收线/告别直念窗(F4 二修)跳过：提交伴发的 EOS 会令框架即刻 commit 轮、
        # 打断在播收线台词。
        if sentence_commit_enabled() and not _closing_say_active(self._stt_):
            commit = self._sentence_boundary(text, prev_full)
            if commit is not None:
                sentence, end_idx = commit
                # source 判别:标点档边界必终止于标点字符;长度档切在正字上。
                # 日志可分辨(QWEN3_ASR_SENTENCE_COMMIT source=partial-len)。
                _commit_src = (
                    "partial-punct"
                    if sentence and sentence[-1] in _SENTENCE_STRONG_PUNCT + _SENTENCE_WEAK_PUNCT
                    else "partial-len"
                )
                self._emit_sentence_commit(sentence, end_idx, lang, now, source=_commit_src)
                # 每句事件序：FINAL(句子) → END_OF_SPEECH（框架 EOS 才置 committed
                # + _run_eou_detection(trigger="stt")，见 _sentence_boundary 文档）。
                # EOS 带停嘴时钟锚点：句子音频最迟在 chunk POST 发出（本窗音频
                # 讲完）时已结束，min_delay 从那时起算——解码期间的时间框架自动
                # 抵扣，唔再全额叠加（同提交时机，只对齐时钟，零早切风险）。
                self._event_ch.send_nowait(
                    stt.SpeechEvent(
                        type=stt.SpeechEventType.END_OF_SPEECH,
                        speech_end_time=t_req_wall,
                    )
                )
                self._prev_partial = text  # 参照窗照常推进（与 _last_partial 同步）
                # 字幕续流：只发未提交剩余（框架 _audio_transcript 已含已提交句）。
                remainder = self._echo_filter(self._uncommitted(text), "interim-window")
                if remainder.strip("。，, 、;；"):
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(
                            type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                            alternatives=[stt.SpeechData(language=lang, text=remainder)],
                        )
                    )
                return

        # INTERIM：滑窗剩余文本（句级提交后坐标系=未提交尾巴，可能跳变，只供展示）
        display = self._uncommitted(text)
        if not display:
            return
        display = self._echo_filter(display, "interim")
        if not display.strip("。，, 、;；"):
            return
        self._event_ch.send_nowait(
            stt.SpeechEvent(
                type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                alternatives=[stt.SpeechData(language=lang, text=display)],
            )
        )
        # 稳定前缀：与上一窗【剩余】的公共前缀，首发 ≥6 字；再发须比上次发射多 ≥
        # _ASR_PREFLIGHT_MIN_GROWTH_CHARS 字（长度增长节流，保框架抢跑预算给 FINAL）。
        common = _common_prefix(self._uncommitted(prev_full), display)
        self._prev_partial = text
        # 语言门：partial 窗检测语言 ≠ 会话锚定语言（LanguageState.lang 此刻值）→
        # 跳过 PREFLIGHT（INTERIM 已照发，字幕不受影响）。PREFLIGHT 只喂框架
        # 抢跑生成：语言切换轮按旧锚语言前缀投机，必被 FINAL 的语言重锚作废重建
        # （twin 双请求并发 prefill 自伤——P5 实测 en 切换轮 TTFT 残留 4.1-4.2s）。
        # FINAL 路径不动：WhatsApp/话术推进用 FINAL，commit 正确性零影响。
        # QWEN3_ASR_PREFLIGHT_LANG_GATE=0 关门（永远发，回退旧行为）。
        lang_gate_open = (
            os.environ.get("QWEN3_ASR_PREFLIGHT_LANG_GATE", "1") == "1"
            and lang != self._stt_._language_state.lang
        )
        if (
            not lang_gate_open
            and len(common) >= 6
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
            # PrefillSpeculator 挂点（agent.py 按会话设 stable_prefix_listener）:
            # 稳定前缀=下一请求 user 文本的保守前缀,拿来做 out-of-band prefill
            # 预热。回调异常绝不影响 STT 事件流。
            _spec_cb = getattr(self._stt_, "stable_prefix_listener", None)
            if _spec_cb is not None:
                try:
                    _spec_cb(common)
                except Exception as exc:  # noqa: BLE001
                    print(f"BOK_PREFILL_SPEC listener error: {exc!r}", flush=True)

    def _emit_sentence_commit(self, sentence: str, end_idx: int, lang: str, now: float, source: str) -> None:
        """句级提交共用出口（partial-punct / vad-pause 两个事件源同一套记账）。

        记账：已提交前缀/最后句/滑窗坐标/限速时刻/PREFLIGHT 坐标系重置；语言锚定
        与 FINAL 路径同款（strong-evidence 判定在 LanguageState 内部，弱证据短句
        拉不走会话语言）。只发 FINAL——句末 END_OF_SPEECH 由调用方按各自契约补
        （partial 标点路径紧随发 EOS；vad-pause 路径后面本就有停嘴 EOS，不重复发）。
        """
        sentence = self._echo_filter(sentence, source)
        if not sentence.strip("。，, 、;；"):
            # 纯回声/残片:丢弃,不记账不发 FINAL(字幕/轮次/脑全链路不污染)
            return
        self._committed_text += sentence
        self._last_sentence = sentence
        self._commit_idx = end_idx
        self._stable = ""  # PREFLIGHT 换未提交坐标系，增长重新计
        self._last_sentence_commit_at = now
        self._stt_._language_state.update(lang, sentence)
        self._event_ch.send_nowait(
            stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(language=self._stt_._language_state.lang, text=sentence)],
            )
        )
        print(f"QWEN3_ASR_SENTENCE_COMMIT source={source} chars={len(sentence)} {sentence!r}", flush=True)

    def _sentence_boundary(self, text: str, prev_full: str, *, allow_eos: bool = False) -> tuple[str, int] | None:
        """滑窗全文里找下一个「可提交」强句边界；唔够格返回 None。

        边界=强标点（。！？!? 及连用）；ASCII 句点后跟字母/数字（3.5、e.g.）唔算
        边界。提交对象是【自上个提交边界起的整段】text[commit_idx:边界]——短句
        （好。係。）唔够 6 字就排队累积，并入下一个够长的边界（或停嘴兜底）。
        全部门（缺一不可）：
        - 句段 ≥ _ASR_SENTENCE_MIN_CHARS 字；
        - 句段无 ≥2 连续 ASCII 字母/数字 run（单号/WhatsApp 高危 → 整段留给停嘴
          整句兜底，数字句永唔句级提交）；
        - 稳定性：上一窗同坐标已是同一句段（首现唔提交，防滑窗跳变 flicker）；
        - 限速：距上次提交 < _ASR_SENTENCE_MIN_INTERVAL_S 唔提交（连珠句防机关枪）。
        allow_eos=True（VAD 微停顿触发）：标点扫描无果时，边界候选=当前滑窗文本
        末尾（pause≥0.45s 唔使标点都係句边界）。字数门槛比标点路径高
        （_pause_commit_min_chars，默认 10）——微停顿只证明喘气，6-9 字碎片当
        整轮提交会被下一碎片掐掉在途回复。稳定性用 prefix 级——上一窗剩余
        係当前剩余的严格前缀（已确认部分零改写）即过：静音期通常只有一窗重解，
        EOS 时刻最后一窗往往刚把句尾字补齐，严格相等会错过真实停顿。首窗该区间
        为空（prev_rem 空）唔提交——零跨窗证据唔赌。
        返回 (句段文本, 边界后坐标)；数字 run 门不过时继续往后扫也只会带着同一
        run 失败 → 自然落到停嘴兜底。
        """
        if time.monotonic() - self._last_sentence_commit_at < _ASR_SENTENCE_MIN_INTERVAL_S:
            return None
        start = self._commit_idx
        if start >= len(text):
            return None
        i = start
        while i < len(text):
            if text[i] in _SENTENCE_STRONG_PUNCT:
                j = i + 1
                while j < len(text) and text[j] in _SENTENCE_STRONG_PUNCT:
                    j += 1
                if text[i] == "." and (
                    # 小数点/缩写点：唔系边界，跳过整段标点继续扫。窗口末尾(j==len)
                # 无后继字符可判时,只要点前一字符是 ascii 字母数字同样视为小数/缩写——
                # 等「.5公斤」下个窗口到齐再定边界,免得半截数字句提交。
                    (j < len(text) and text[j].isascii() and text[j].isalnum())
                    or (i > start and text[i - 1].isascii() and text[i - 1].isalnum())
                ):
                    i = j
                    continue
                sentence = text[start:j]
                if (
                    len(sentence) >= _ASR_SENTENCE_MIN_CHARS
                    and not _has_latin_or_digit_run(sentence)
                    and prev_full[start:j] == sentence
                ):
                    return sentence, j
                # 呢个边界唔够格（太短/数字 run/未稳定）→ 唔喺度提交，继续扫下一
                # 个边界（短句排队累积；未稳定边界下窗自然变稳定）。
            elif _clause_commit_enabled() and text[i] in _SENTENCE_WEAK_PUNCT:
                # 子句边界(同传档):逗号/顿号/分号即提交——同一套门(长度/数字 run/
                # 稳定性/限速)全部照走,门槛用 _clause_commit_min_chars。唔够格照例
                # 继续扫(短子句并入下个边界,数字子句留给停嘴整句兜底)。
                j = i + 1
                while j < len(text) and text[j] in _SENTENCE_WEAK_PUNCT:
                    j += 1
                sentence = text[start:j]
                if (
                    len(sentence) >= _clause_commit_min_chars()
                    and not _has_latin_or_digit_run(sentence)
                    and prev_full[start:j] == sentence
                ):
                    return sentence, j
            i += 1
        # 长度档(边说边译,B 线):标点档哑火(连续语流无逗号)时的第三事件源——
        # 未提交前缀攒够字数且跨窗稳定即就地切句,唔使等 VAD 停嘴/EOS。切点保护
        # (不劈数字/英文词)在 _length_commit_cut 内;限速门在函数头已挡(与标点
        # 档同一把 1.5s 节流阀,提交节奏对齐 TTS 串行播报)。
        if _clause_len_commit_enabled():
            cut = _length_commit_cut(
                text, start, prev_full, max(_clause_len_commit_chars(), _clause_commit_min_chars())
            )
            if cut is not None:
                return text[start:cut], cut
        if allow_eos:
            sentence = text[start:].rstrip(_PAUSE_TRAILING_WEAK_PUNCT)
            prev_rem = prev_full[start:].rstrip(_PAUSE_TRAILING_WEAK_PUNCT)
            if (
                len(sentence) >= _pause_commit_min_chars()
                and not _has_latin_or_digit_run(sentence)
                and prev_rem
                and sentence.startswith(prev_rem)
            ):
                return sentence, len(text)
        return None

    def _uncommitted(self, text: str) -> str:
        """去掉已句级提交前缀后的剩余文本；坐标失配逐级回退，重解修正丢弃。

        主路径 startswith（滑窗是同一会话累积解码，已稳定前缀极少改写）；改写过
        就用最后提交句 rfind 定位（容前缀改写）；再唔准就分「重解修正」定「真续
        句」：归一化（去标点/空白）后 committed 係 finish 前缀 → 真续句，按归一
        化对齐截 raw 尾（标点改写唔算新内容）；相似度 ≥_ASR_REDECODE_DROP_RATIO
        → 同一段话的更好重解，返回 ""（唔补发）——迟到 FINAL 会喺框架
        on_final_transcript 里 _interrupt_by_audio_activity() 掐死生成中的回复
        （call-58601bba 实证，见 _ASR_REDECODE_DROP_RATIO 注）；极端跳变（低相
        似）原样返回兜底旧行为。
        剩余头部的弱停顿符照例剥掉（vad-pause 提交剥了「，」尾、finish 整句以
        「。」续写——剩余唔应该带住句号/逗号开头；只剥标点唔动正字）。
        """
        if not self._committed_text:
            return text
        if text.startswith(self._committed_text):
            return text[len(self._committed_text):].lstrip(_UNCOMMITTED_LEADING_WEAK_PUNCT)
        if self._last_sentence:
            pos = text.rfind(self._last_sentence)
            if pos >= 0:
                return text[pos + len(self._last_sentence):].lstrip(_UNCOMMITTED_LEADING_WEAK_PUNCT)
        norm_c = _strip_punct_space(self._committed_text)
        norm_t = _strip_punct_space(text)
        if norm_c and norm_t.startswith(norm_c):
            # 归一化对齐截尾：raw 里跳过标点/空白食掉 norm_c 长度的正字，剩余係真尾巴。
            need = len(norm_c)
            aligned_idx = -1
            for i, ch in enumerate(text):
                if ch.isspace() or unicodedata.category(ch).startswith("P"):
                    continue
                need -= 1
                if need == 0:
                    aligned_idx = i
                    break
            return text[aligned_idx + 1:].lstrip(_UNCOMMITTED_LEADING_WEAK_PUNCT) if aligned_idx >= 0 else ""
        sm = difflib.SequenceMatcher(a=norm_c, b=norm_t)
        if sm.ratio() < _ASR_REDECODE_DROP_RATIO:
            return text
        if len(norm_t) <= len(norm_c) + 3:
            # 长度相当:同一句话的更好重解,冇新内容——迟到 FINAL 会喺框架
            # on_final_transcript 里掐死生成中的回复(call-58601bba),唔补发。
            print(
                f"QWEN3_ASR_REDECODE_DROP committed={self._committed_text!r} finish={text!r}",
                flush=True,
            )
            return ""
        # 同头+新尾(2026-09-12 长度感知):finish 明显更长=客户快语速继续讲的内容
        # 在权威重解里,整条丢=真吃字(当日全量日志实测 19 次/387 字,单次最多 36
        # 字)。按 diff 定位 committed 末端,对齐 raw 截新尾照发——内容送达优先,
        # 碎片回复被 barge-in 是其本分。
        tail_start = None
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal" and i1 < i2 and i2 == len(norm_c):
                tail_start = j2  # committed 全文映射完之后=新尾巴起点(norm 坐标)
        if tail_start:
            cnt = 0
            for i, ch in enumerate(text):
                if ch.isspace() or unicodedata.category(ch).startswith("P"):
                    continue
                cnt += 1
                if cnt == tail_start:
                    _tail = text[i + 1 :].lstrip(_UNCOMMITTED_LEADING_WEAK_PUNCT)
                    print(
                        f"QWEN3_ASR_REDECODE_TAIL committed={self._committed_text!r} tail={_tail!r}",
                        flush=True,
                    )
                    return _tail
        # 高相似但 committed 末端对不齐(极端改写,理论边角):按重解丢弃。
        print(
            f"QWEN3_ASR_REDECODE_DROP committed={self._committed_text!r} finish={text!r}",
            flush=True,
        )
        return ""

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
        self._last_lang = ""
        # 句级提交状态随段重置（新语音段从零累计；限速时刻一并归零）。
        self._committed_text = ""
        self._last_sentence = ""
        self._commit_idx = 0
        self._last_sentence_commit_at = 0.0
        # 暴露位随段清零(本段已提交):FINAL 发出点会按 pre-reset 快照把「这条
        # FINAL 的 partial 末稿」重贴回来——只给真发出去的 FINAL,纯 dump/短尾
        # 等被丢弃=无 FINAL → 保持空,下一轮拿不到上一轮的话。
        self._stt_._turn_partial_text = ""


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
        # 在活流追踪(GPU 竞态专项):set_partial_ms 要即时转发到当前 stream 的
        # 开会话;WeakSet 随流 GC 自动清理,勿改强引用。
        self._live_streams: weakref.WeakSet = weakref.WeakSet()

    @property
    def model(self) -> str:
        return self._stt.model

    @property
    def provider(self) -> str:
        return self._stt.provider

    # 稳定前缀监听（PrefillSpeculator）：流对象持有的是内芯(_stt),监听经此
    # property 转发落位到内芯,agent.py 只见到包装。
    @property
    def stable_prefix_listener(self):
        return getattr(self._stt, "stable_prefix_listener", None)

    @stable_prefix_listener.setter
    def stable_prefix_listener(self, cb) -> None:
        self._stt.stable_prefix_listener = cb

    def _on_metrics_collected(self, *args, **kwargs) -> None:
        self.emit("metrics_collected", *args, **kwargs)

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        return await self._stt.recognize(buffer=buffer, language=language, conn_options=conn_options)

    def stream(self, *, language=None, conn_options=None):
        s = _Qwen3ASRLiveStream(self._stt, vad=self._vad, conn_options=conn_options or APIConnectOptions())
        self._live_streams.add(s)
        return s

    def set_partial_ms(self, ms: int | None) -> None:
        """会话级 partial 解码档(GPU 竞态专项,同步入口,事件钩子直接调)。

        记 override 给下个会话;在活流已开的 sidecar 会话用 ensure_future 即时
        调档——事件回调喺 event loop 线程,无 loop 时(纯单测)静默跳过转发。
        """
        self._stt._partial_ms_override = ms
        for s in list(self._live_streams):
            try:
                # 强引用入池(P2-A,2026-09-17):裸 ensure_future 同受 GC 弱引用回收。
                _spawn_bg(s._apply_partial_ms(ms))
            except RuntimeError:
                pass

    def set_reply_busy(self, busy: bool) -> None:
        """回复在途旗(F2 迟到 FINAL 尾巴护栏,同步入口,agent_state_changed 钩子调)。

        thinking/speaking=True、listening=False。旗落内芯即可:迟到尾巴判定
        发生在停嘴 finish 路径,流读判定时刻现取(`getattr(self._stt_,…)`)，
        无需转发到活流。护栏关(BOK_LATE_FINAL_GUARD=0)时旗没人读,零害。
        """
        self._stt._reply_busy = bool(busy)

    def set_closing_say(self, on: bool) -> None:
        """收线/告别直念窗旗(F4 二修,同步入口;agent 播【收线】台词前后置/撤)。

        True 期间流把一切成轮事件(句级提交/EOS/FINAL)静默丢弃——把告别说完。
        与 set_reply_busy 同款「旗落内芯、流读判定时刻现取」姿势。
        """
        self._stt._closing_say = bool(on)

    def last_partial_text(self) -> str:
        """本轮 ASR 滑窗 partial 末稿(E2 热词泄漏清洗的 ``fallback_text`` 取口)。

        语义:ASR 在把本轮终稿(句级提交句 / 停嘴 tail FINAL)交出去之前,滑窗
        partial 最后给出的那段文本(未提交坐标系)。终稿被热词 dump 污染时,它是
        「客户实际讲了什么」的独立证据——``hotword_leak.sanitize`` 的单词泄漏
        判据正需要它(无 fallback 时该判据结构性不成立,见模块 docstring 偏差②)。

        契约(写点在流内,见 ``_Qwen3ASRSTT._turn_partial_text`` 注释):
        - 只反映**本轮**:新一轮 VAD 语音段开场(``_start_session``)即清,上一轮
          的 partial 不会喂给下一轮;该轮无 FINAL 发出(纯 dump/短尾丢弃)时同样
          为空——宁可拿不到 fallback,不可拿错轮的话。
        - 读取恒安全:纯属性读,无 await/无网络/无状态变更,agent 异步钩子直接调。
        - 无 partial(短句 <0.6s 窗 / ``QWEN3_ASR_STREAM=0`` 的官方
          StreamAdapter / 假 STT)→ 空串:调用方行为与未接线时逐字节相同。
        """
        return str(getattr(self._stt, "_turn_partial_text", "") or "")
