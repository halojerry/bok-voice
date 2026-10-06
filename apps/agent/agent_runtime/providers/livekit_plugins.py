"""LiveKit-compatible provider plugins: OpenAI-compatible LLMs + offline fakes."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import difflib
import json
import os
import re
import time
import unicodedata
import uuid
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlparse

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

from bok_voice_core.deepseek_llm import is_deepseek_endpoint, thinking_extra_body

# 模型路由共享契约(2026-09-25 阶段 0):只消费,解析/校验逻辑全在 packages/core。
from bok_voice_core.model_routes import LaneRoute, PROVIDER_OPENAI

# 编造号码输出守卫(2026-10-01,call-231aa92a):LLM 流出口逐句校验号码确认,
# 纯函数在 packages/core;本模块只做接线(BOK_NUMBER_GUARD 开关)。
from bok_voice_core.output_guard import guard_fabricated_number, number_guard_pending

# smart-turn 语义闸（V1，2026-09-26）：VAD 停嘴处判「说完没」的 ONNX 小模型
# （pipecat smart-turn-v3.2-cpu，~12ms/次）。BOK_SMART_TURN=1 才启用（默认关，
# 未验收特性不默认开），fail-open 语义见 providers/smart_turn.py。
from . import smart_turn as _smart_turn

from ..voice_style import NATURALNESS_BLOCK, a_line_tags_supported, strip_voice_style
from ..asr_polish_runtime import polish_enabled as _polish_layer_on
from ..asr_polish_runtime import sync_polish as _polish_sync_text
from ..flow import STEP_DISCIPLINE_RULE, split_step_text, stable_step_key
from ..slot_actor import build_slot_task_block, compose_slot_user_message

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


def route_llm_kwargs(
    route: LaneRoute,
    *,
    env_base_url: str,
    cfg_model: str,
) -> dict:
    """模型路由车道 → MlxLlmLLM 构造参数映射(纯函数,单测直喂;2026-09-25 阶段 0)。

    openai 档=路由表四件套(base_url/model/api_key + enable_thinking 请求体旗,
    Qwen3.5 家族云端思考陷阱,LANE-AB 实测不传该旗 5.85s 全 <think>);api_key 空
    (routing source「保留旧值」语义)回落构造器缺省哨兵,等价不带 key 的既有请求。
    local routing 档=只覆盖 base_url(显式改端点,如 LM Studio),model 非空才覆盖,
    不带 key/思考旗。env 档=调用方传入的原读法**原样回传**——base_url 来源保持
    既有 env/settings 链,不经本函数改写(零漂移保证:kill-switch/空表时构造参数
    与改造前逐字节同)。"""
    if route.provider == PROVIDER_OPENAI:
        return {
            "base_url": route.base_url,
            "model": route.model,
            "api_key": route.api_key or "mlx",
            "enable_thinking": route.enable_thinking,
        }
    if route.source == "routing":
        return {"base_url": route.base_url, "model": route.model or cfg_model}
    return {"base_url": env_base_url, "model": cfg_model}


# ---- mlx 生成中止（abort）客户端（2026-10-01 W-ABORT）----
# 服务端 = 同仓 services/llm-mlx/bok_mlx_server.py（mlx_lm server 的薄 wrapper，
# 生成循环按请求身份查 abort 旗，``POST /v1/abort`` 置位即从解码循环退出放槽）。
# 客户端两个动作：①生成请求带 ``X-Bok-Req-Id``（uuid，经 ContextVar 随流任务
# 上下文传递，官方流的 create 包装读取）；②取消/弃流确定点 fire-and-forget 发
# ``POST {base}/v1/abort``（0.5s 超时，全吞）。BOK_MLX_ABORT=0 或 base_url 非
# 本机（云端 OpenAI 兼容车道）→ 零注入零请求，出站字节面同旧。
_MLX_REQ_ID_HEADER = "X-Bok-Req-Id"
_MLX_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MLX_ABORT_CLIENT: httpx.AsyncClient | None = None
_MLX_REQ_ID_VAR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "bok_mlx_req_id", default=""
)


def _mlx_abort_on_for(base_url: str) -> bool:
    """abort 总闸：BOK_MLX_ABORT=1 且 base_url 指向本机 mlx（云端车道不打扰）。"""
    if os.environ.get("BOK_MLX_ABORT", "1") != "1":
        return False
    try:
        host = (urlparse(str(base_url or "")).hostname or "").lower()
    except Exception:  # noqa: BLE001 - 解不出=非本地，退回旧行为
        return False
    return host in _MLX_LOCAL_HOSTS


def _is_local_mlx_url(base_url: str) -> bool:
    """base_url 是否指向本机 mlx（纯 host 判据,与 abort 总闸 env 解耦——
    BOK_MLX_ABORT=0 不该把本地 warmup 一并关掉;2026-10-03 I1c）。"""
    try:
        host = (urlparse(str(base_url or "")).hostname or "").lower()
    except Exception:  # noqa: BLE001 - 解不出=非本地
        return False
    return host in _MLX_LOCAL_HOSTS


def _mlx_abort_url(base_url: str) -> str:
    """abort 端点 URL：base_url 以 /v1 结尾（OpenAI 约定）时同挂 /v1/abort。"""
    base = str(base_url or "").rstrip("/")
    return f"{base}/abort" if base.endswith("/v1") else f"{base}/v1/abort"


def _abort_http_client() -> httpx.AsyncClient:
    global _MLX_ABORT_CLIENT
    if _MLX_ABORT_CLIENT is None:
        _MLX_ABORT_CLIENT = httpx.AsyncClient(timeout=httpx.Timeout(0.5))
    return _MLX_ABORT_CLIENT


async def _send_mlx_abort(base_url: str, req_id: str) -> None:
    """fire-and-forget POST /v1/abort（0.5s 超时；任何失败全吞，绝不外抛）。"""
    if not base_url or not req_id:
        return
    try:
        await _abort_http_client().post(
            _mlx_abort_url(base_url), json={"request_id": req_id}
        )
    except Exception:  # noqa: BLE001 - 中止是尽力语义
        pass


def _fire_mlx_abort(base_url: str, req_id: str) -> None:
    if base_url and req_id:
        _spawn_bg(_send_mlx_abort(base_url, req_id))


def _attach_mlx_abort(stream, base_url: str, req_id: str) -> None:
    """给官方流挂 abort 钩子：aclose 且内芯生成未完结 → 发 abort（幂等一次）。

    只服务无兜底壳的路径（B 线 MT 等）；主回复路径由 _LlmFallbackStream 自持
    挂钩（它对 aclose 有 drain/兜底语义，需要自己的判据）。"""
    if not (base_url and req_id):
        return
    orig_aclose = stream.aclose
    fired = False

    async def _aclose():
        nonlocal fired
        if not fired:
            fired = True
            task = getattr(stream, "_task", None)
            if task is None or not task.done():
                _fire_mlx_abort(base_url, req_id)
        await orig_aclose()

    try:
        stream.aclose = _aclose
    except Exception:  # noqa: BLE001 - 挂不上=退化为无 abort
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
    max_tokens 同治理（2026-10-02 刀1）：构造参优先，None=现行 env 读（缺省 160）
    ——B 线整句翻译显式 512，不再靠 entrypoint setdefault 写进程 env 下发。
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
        enable_thinking: bool | None = None,
        max_tokens: int | None = None,
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
            # max_tokens:构造参优先(缺省 None=现行 env 读,既有调用零漂移);
            # B 线整句翻译经此显式 512,不再依赖进程 env setdefault(刀1 卫生)。
            "max_tokens": int(
                max_tokens if max_tokens is not None else os.environ.get("LLM_MAX_TOKENS", "160")
            ),
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
        if is_deepseek_endpoint(base_url):
            # DeepSeek 云端点（2026-10-03 云腿波）：Qwen 家族 enable_thinking 旗
            # 它不认——思考开关走官方 thinking{type} 契约（缺省 enabled 会把小
            # max_tokens 烧空出空串，见 bok_voice_core.deepseek_llm）。端点判据
            # 与 DeepSeekLLM 同源；本地/其它云端端点逐字节零漂移。
            extra_body.update(
                thinking_extra_body(
                    base_url,
                    "enabled" if enable_thinking else os.environ.get("DEEPSEEK_THINKING", ""),
                )
            )
        elif enable_thinking is not None:
            # 模型路由 openai 档(2026-09-25):思考旗随请求体下发(Qwen3.5 家族云端
            # 思考陷阱——不传该旗思考全开,LANE-AB 实证)。extra_body 经官方 openai
            # SDK 合并进请求体顶层(既有 max_tokens/stop 同通道)。缺省 None=请求体
            # 不含该键,本地档逐字节同旧(零漂移保证)。
            extra_body["enable_thinking"] = bool(enable_thinking)
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
        # reply 车道标记（2026-09-26 根治 mlx 解码争用）：:1235 前置队列代理按
        # X-Bok-Lane 分道——回复请求插队,后台消费者(settle/qa-cluster/judge)排队。
        # 直连 mlx 时该头被无害忽略(mlx_lm 不读未知头),零拓扑耦合;与下方两个
        # 调试包装同姿势(包 _client.chat.completions.create)。
        _lane_client = self._client
        _lane_raw = _lane_client.chat.completions.create

        async def _lane_create(**kw):
            headers = dict(kw.get("extra_headers") or {})
            headers.setdefault("X-Bok-Lane", "reply")
            kw["extra_headers"] = headers
            return await _lane_raw(**kw)

        _lane_client.chat.completions.create = _lane_create
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
            # 阶段1·P1(2026-09-25):出站请求的 assistant 消息剥语气/停顿标记
            # ——LLM 不见自己上轮的标记(防 4B 复制引力放大用量),连续请求同剥
            # =严格前缀契约两侧一致;快照回调拿到的也是剥后列表,投机预热与
            # 真实请求逐字节同源。user/system 不动。
            _msgs = kw.get("messages")
            if _msgs:
                _clean: list[dict] = []
                for _m in _msgs:
                    if isinstance(_m, dict) and _m.get("role") == "assistant" and isinstance(_m.get("content"), str):
                        _m = {**_m, "content": strip_voice_style(_m["content"])}
                    _clean.append(_m)
                kw["messages"] = _clean
            _cb = self.on_request_messages
            if _cb is not None:
                try:
                    _cb(list(kw.get("messages") or []))
                except Exception:  # noqa: BLE001 - 快照失败唔阻真实请求
                    pass
            return await _sraw(**kw)

        _sclient.chat.completions.create = _snapshot_create

        # mlx 生成中止（2026-10-01 W-ABORT）：本地车道出站请求带 X-Bok-Req-Id，
        # 取消/弃流点发 POST /v1/abort——mlx_lm server 单生成线程零取消路径，
        # 被弃请求照解码到底（直打 :1237 实测断连后新请求 TTFT=2334ms；生产
        # 放大形态=打断轮 call-231aa92a TTFT 35.6s 级联）。req_id 经 ContextVar
        # 传递：chat() 里 set、super().chat() 构造流任务时随上下文进流，
        # 官方流的 create 包装（本层，最外）读取上头。云端 base_url 零注入。
        self._bok_abort_base = str(getattr(self._client, "base_url", "") or "")
        self._bok_abort_on = _mlx_abort_on_for(self._bok_abort_base)
        if self._bok_abort_on:
            _rclient = self._client
            _rraw = _rclient.chat.completions.create

            async def _reqid_create(**kw):
                rid = _MLX_REQ_ID_VAR.get()
                if rid:
                    headers = dict(kw.get("extra_headers") or {})
                    headers.setdefault(_MLX_REQ_ID_HEADER, rid)
                    kw["extra_headers"] = headers
                return await _rraw(**kw)

            _rclient.chat.completions.create = _reqid_create

    async def _prewarm_impl(self) -> None:
        # 真实 1-token 生成：暖 mlx 模型（冷启动的 KV 分配/首 token 占首包大头）。
        # 官方 prewarm 只验连接；AgentSession 构造时会自动调用本钩子。
        # 文本须 >11 token：mlx_lm 0.31.3 server 对 has_thinking 模型固定
        # rfind_think_start(prompt, start=len-11)，prompt 更短时负数索引直接
        # IndexError（包成 404 "list index out of range"）——Hy-MT2 实证，
        # warmup 因此整年白跳。
        if os.environ.get("LLM_WARMUP", "1") != "1":
            return
        # host 门（2026-10-03 I1c）：云端 OpenAI 兼容车道没有本地 mlx 的冷启动
        # KV 语义,28-token warmup 纯噪音——只在本机端点发。
        if not _is_local_mlx_url(self._bok_abort_base):
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
                # 车道标记（2026-10-03 I1）：预热=bg,不抢交互回复的队列位（:1237
                # 前门闸与 :1235 闸同判 X-Bok-Lane;无代理拓扑下头被 mlx 无害忽略）。
                extra_headers={"X-Bok-Lane": "bg"},
            )
            print("[agent] llm warmup done", flush=True)
        except Exception as exc:  # pragma: no cover - warmup 失败不致命
            print(f"[agent] llm warmup skipped: {exc!r}", flush=True)

    async def prefix_prewarm(self, messages: list[dict]) -> None:
        """真实 prompt 形状的 1-token 预热（会话首轮前，agent.py 发起；speculator
        每轮投机预热同走本方法）。

        与 _prewarm_impl（只暖模型/连接）不同：这里喂「真实 merged system +
        fake user 轮」，mlx_lm server 会把该前缀的 KV 留喺 prompt cache——
        turn-1 真请求共享整段 system 前缀 → cached≈system 长度，免 ~1.4s
        全量 prefill（会话首轮 cached=0 的专项解法）。失败由调用方吞掉。
        冷启动竞态：预热请求会排在官方 prewarm/开场白 prefill 后面，共享
        client 的 read=5s 会提前放弃（实测 APITimeoutError）——per-request
        放宽 read=30s，让服务端把前缀 prefill 跑完入 cache（client 等耐些，
        反正 fire-and-forget 唔阻塞任何人）。

        W-ABORT 接线（2026-10-02 实机验证波）：speculator 的投机预热被 FINAL
        即断（new_turn cancel）时，客户端断连对 mlx **prefill 期不可见**（十三
        波刀B 定案）——投机请求残余 prefill（带上一条真实请求的全前缀，miss 时
        2-3k tok）继续独占单生成线程，真 reply 的 ctx 排其后=秒级「请求到达→
        prefill 开始」空窗（FLOW20 首 token 超时链的最后一环，settle-llm.log
        first_progress 8-16s/prompt_window 仅 60ms 的实录形状）。修=带
        X-Bok-Req-Id，CancelledError 时显式 POST /v1/abort 令 server 立即弃
        prefill 放槽。正常完成/其他异常不 abort（max_tokens=1 自完）。
        """
        req_id = str(uuid.uuid4())
        try:
            await self._client.chat.completions.create(
                model=self._opts.model,
                messages=messages,
                max_tokens=1,
                # 车道标记（2026-10-03 I1）：前缀预热/投机预热=bg——:1237 前门闸下
                # 让 reply 插队;req-id 同头共存,取消即 abort 的语义不变。
                extra_headers={_MLX_REQ_ID_HEADER: req_id, "X-Bok-Lane": "bg"},
                timeout=httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0),
            )
        except asyncio.CancelledError:
            if _mlx_abort_on_for(self._bok_abort_base):
                _fire_mlx_abort(self._bok_abort_base, req_id)
                print(
                    f"BOK_PREFILL_SPEC abort-fired (server-side) req={req_id[:8]} "
                    f"base={self._bok_abort_base}",
                    flush=True,
                )
            raise

    # ---- 主回复 deadline + 兜底直念（2026-09-17,治「LLM 卡死整轮哑火」）----
    # 客服口径（用户拍板 3.0s,可再收紧）:等 8s/重试链=这通电话已废。三层:
    # ①首 token 截止——_LlmFallbackStream 对第一块 ChatChunk 计时
    #   （LLM_FIRST_TOKEN_TIMEOUT_S 默认 3.0,0=关）,超时立即出三语兜底句,
    #   但**唔弃流**:后台 drain 继续消费本流收晚到真答案(次级截止
    #   LLM_LATE_ANSWER_DEADLINE_S 默认 8s,0=回 aclose+regen 旧行为)。
    #   注（2026-10-02 注释同步）:W-ABORT 落地后 aclose/cancel 已能中止服务端
    #   解码（_attach_mlx_abort→POST /v1/abort,生成循环立即放槽）——drain 是
    #   **策略选择**而非无奈:原流慢但可能仍活,继续读严格优于杀掉重排(同参
    #   regen 全量重 prefill,饥荒中负载×2);只有超过次级截止仍无产出才真弃
    #   流重生(且饥荒档禁 regen,见 _spawn_regen)。
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
    # W-ABORT 实例面默认（__init__ 里按 base_url 覆写；类级兜底防裸构造）
    _bok_abort_on: bool = False
    _bok_abort_base: str = ""

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
        # 8→22（2026-09-28 定时器普查）:这是传输层 read-gap 粗后盾,赌的是
        # 「字节流死了」——但冷/缓存失配 prefill 实测 p95=18.9s（3926 轮全量:
        # TTFT p95=3.46s/p99=7.04s,cache-miss p95=18.85s）,8s 会把「慢」误杀成
        # 「死」→ APITimeoutError → regen 再超时（drain/regen 143 次失败恢复的
        # 主死因）。22s 盖住冷 prefill p95;真死流由首-token 闸+watchdog 先出手。
        try:
            timeout = float(os.environ.get("LLM_REQUEST_TIMEOUT_S", "22") or 0)
        except ValueError:  # pragma: no cover - 配错回默认
            timeout = 22.0
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
            v = float(os.environ.get("LLM_FIRST_TOKEN_TIMEOUT_S", "3.0") or 0)
        except ValueError:  # pragma: no cover - 配错回默认
            v = 3.0
        if llm_famine_active():
            # 饥荒自适应（2026-10-01 第十五波,call-dc54f542）:机器级首 token 慢
            # (交换/GPU 争用,实测窗口 3-25s)时,3s 健康档常数把「慢但活着」误杀
            # 成「死」——拉长到饥荒档,等原流严格优于杀掉重来。
            v = max(v, _famine_first_timeout_s())
        return v

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
        # W-ABORT：req_id 必须在 super().chat() 之前 set——官方 LLMStream 基类
        # 在构造时 create_task，ContextVar 在那刻被 copy 进流任务上下文；流内
        # 的 create 包装（_reqid_create）据此把 id 上头。reset 在 finally，绝不
        # 泄漏给后续无关请求；BOK_MLX_ABORT=0=零 set 零头（字节面同旧）。
        req_id = ""
        _req_token = None
        if self._bok_abort_on:
            req_id = uuid.uuid4().hex
            _req_token = _MLX_REQ_ID_VAR.set(req_id)
        try:
            stream = super().chat(
                chat_ctx=chat_ctx,
                tools=tools,
                conn_options=conn_options,
                parallel_tool_calls=parallel_tool_calls,
                tool_choice=tool_choice,
                extra_kwargs=extra_kwargs,
            )
        finally:
            if _req_token is not None:
                _MLX_REQ_ID_VAR.reset(_req_token)
        # 兜底壳只包主回复路径(注入档);自带 conn_options 的调用方
        # (prefix_prewarm 30s 档等)失败照旧被调用方吞,唔出兜底句。
        if injected and self._fallback_text and isinstance(stream, llm.LLMStream):
            if req_id:
                try:
                    stream._bok_req_id = req_id
                except Exception:  # noqa: BLE001 - 标不上=退化为无 abort
                    pass
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
                req_id=req_id,
                abort_base=self._bok_abort_base,
            )
        if req_id and isinstance(stream, llm.LLMStream):
            # 无兜底壳路径（B 线 MT 等）：取消即弃流点挂在官方流 aclose 上。
            _attach_mlx_abort(stream, self._bok_abort_base, req_id)
        return stream


# ---- LLM 饥荒自适应（2026-10-01 第十五波,call-dc54f542 根修） -----------------
# 饥荒=机器级首 token 慢：交换挤压（实测 26GB swap/权重页出）或 GPU 争用把
# 首 token 推到 3-25s（健康档 0.2-0.6s）。按健康档调的恢复常数在饥荒中全是
# 负贡献——实弹时间线（call-dc54f542 23:01）：原流 7.9s 本有答案 → 3s 首
# token 超时垫话盖耳 → drain 8s 差 1-2 秒没等到 → abort 杀原流 → regen 全量
# 重 prefill（饥荒中负载×2）→ 25.3s>22s 传输超时 → 客户 37s 零答案；干等
# 原流 10s 即有答案。信号=本 worker 最近流的首 token 延迟 EMA（机器级状态
# 跨通话共享）；饥荒时：首 token 超时拉长、drain 拉长、禁 regen。
# 复原=快样本把 EMA 拉回阈值下，自动回健康档常数。

_FAMINE_STATE: dict = {"ema": 0.0, "n": 0}


def _famine_enabled() -> bool:
    return os.environ.get("BOK_LLM_FAMINE", "1") == "1"


def _famine_threshold_s() -> float:
    try:
        return float(os.environ.get("BOK_LLM_FAMINE_TTFT_S", "4") or 0)
    except ValueError:  # pragma: no cover - 配错回默认
        return 4.0


def _famine_first_timeout_s() -> float:
    try:
        return float(os.environ.get("BOK_LLM_FAMINE_FIRST_S", "15") or 0)
    except ValueError:  # pragma: no cover
        return 15.0


def _famine_drain_s() -> float:
    try:
        return float(os.environ.get("BOK_LLM_FAMINE_DRAIN_S", "15") or 0)
    except ValueError:  # pragma: no cover
        return 15.0


def record_llm_first_token(ttft_s: float) -> None:
    """喂一次首 token 延迟样本（EMA α=0.4；超时样本也喂=饥荒加深信号）。

    纯模块级账本：worker 进程内跨通话共享（饥荒係机器级状态）。测试用
    ``_FAMINE_STATE.clear()+update`` 重置。"""
    if ttft_s <= 0:
        return
    s = _FAMINE_STATE
    s["n"] = int(s.get("n") or 0) + 1
    s["ema"] = ttft_s if s["n"] == 1 else float(s.get("ema") or 0.0) * 0.6 + ttft_s * 0.4


def llm_famine_active() -> bool:
    """饥荒判定：≥2 个样本且 EMA ≥ 阈值（默认 4s）。kill-switch 整体关。"""
    if not _famine_enabled():
        return False
    if int(_FAMINE_STATE.get("n") or 0) < 2:
        return False
    return float(_FAMINE_STATE.get("ema") or 0.0) >= _famine_threshold_s()


def _famine_reset_for_tests() -> None:
    _FAMINE_STATE.clear()
    _FAMINE_STATE.update({"ema": 0.0, "n": 0})


def _late_answer_deadline_s() -> float:
    """drain(原流续读)次级截止秒数:首 token 超时出兜底后,本流最多再等多久。

    默认 8s(mlx 4B 出满答案远快于此;超时基本=真死流)。0=关 → 回立即
    aclose+factory 重生旧行为(kill-switch)。
    饥荒自适应（第十五波）:饥荒时拉长(call-dc54f542 实弹:原流首 token 7.9s
    撞 8s drain 窗差 1-2 秒判死——窗口盖住慢-但-活的流)。"""
    try:
        v = float(os.environ.get("LLM_LATE_ANSWER_DEADLINE_S", "8") or 0)
    except ValueError:  # pragma: no cover - 配错回默认
        v = 8.0
    if llm_famine_active():
        v = max(v, _famine_drain_s())
    return v


class _LlmFallbackStream(llm.LLMStream):
    """主回复出口闸：首 token 截止 + 失败兜底直念（LLM 出口单点拦截）。

    三条路都汇到同一句本地兜底（零模型调用）:
    - 首 token 超时（LLM_FIRST_TOKEN_TIMEOUT_S,默认 3.0s）:
      首 chunk 计时到点立即出兜底句——但**唔弃流**(2026-09-17 RC4):
      drain 继续消费本流收集剩余文本,晚到真答案经回调补答;次级截止
      （LLM_LATE_ANSWER_DEADLINE_S,默认 8s,0=关→回立即 aclose+factory 重生
      旧行为）仍无产出才真弃流重生(最后手段)。注释同步（2026-10-02）:RC4 当
      年「mlx 服务端无断连中止、弃流只换僵尸解码税」的被迫理由已被 W-ABORT
      推翻（aclose/cancel 会经 _attach_mlx_abort 发 POST /v1/abort,服务端
      生成循环立即放槽）——现在 drain 是对「慢但可能仍活」的**策略选择**:
      继续读原流严格优于杀掉重排（同参 regen 全量重 prefill,饥荒中负载×2,
      见 _spawn_regen 饥荒禁 regen 档）;
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
                 fallback_gate=None, req_id: str = "", abort_base: str = ""):
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
        # W-ABORT：本流对应服务端请求身份 + abort 端点（空=不接中止线）。
        self._bok_req_id = req_id or ""
        self._bok_abort_base = abort_base or ""
        self._abort_fired = False
        # 首 token 超时后 drain 接手内芯（设计上继续读，见类注释）——此时框架
        # aclose 只关本层泵、不代表弃内芯；abort 只在真弃流点（_aclose_inner）
        # 强制触发，别把 drain 语义误杀。
        self._drain_owns = False
        # 刀1 打断弃流（2026-10-02 call-4e8d58c1 R4 实证）：用户开口打断=答案
        # 过时——abandon() force abort 服务端 + 熔断 drain/regen 补答交付；
        # 纯超时（机器慢、答案仍相关）drain 语义不变。_bok_created 供打断侧
        # 时序门（创建早于打断时刻=本轮僵尸；晚于=下一轮新流，绝不误杀）。
        self._abandoned = False
        self._bok_created = time.monotonic()

    def _fire_abort(self, force: bool = False) -> None:
        """弃流中止（幂等一次）：内芯生成未完结才发（已完结=服务端早放槽，免扰）。

        ``force=False``（框架 aclose / cancel 路径）时若 drain 已接手则不发——
        drain 的存在意义就是继续读同一条流，abort 会把它截断成 regen。"""
        if self._abort_fired or not (self._bok_req_id and self._bok_abort_base):
            return
        if self._drain_owns and not force:
            return
        self._abort_fired = True
        task = getattr(self._inner, "_task", None)
        if task is None or not task.done():
            _fire_mlx_abort(self._bok_abort_base, self._bok_req_id)

    async def aclose(self) -> None:
        """取消即弃流（框架 ``async with`` 出口/打断收尸/会话收尾）先发 abort。

        这是「服务端单线程被弃生成照解码到底」的客户端侧出口：框架只关本层泵，
        内芯 httpx 流与 mlx 生成任务本会解到自然完稿（call-9af18da5 双句打断后
        新回复 TTFT 5.3/6.8s 的机理）；abort 置位后生成循环立即放槽。幂等，
        内芯已完结时为纯 no-op；drain 接手时交 _aclose_inner 收口。"""
        self._fire_abort()
        await super().aclose()

    async def abandon(self) -> None:
        """打断弃流（force，幂等）：用户已开口、本轮答案过时。

        call-4e8d58c1 R4 病理：打断后框架 speech_handle 给 5s 宽限才硬 cancel，
        首token超时 drain 又接管压制了 cancel 路径的 abort——僵尸 prefill 与下一轮
        回复在同块 9B 上互抢（6110ms 里 96% 是白等）。打断时刻调本方法：
        ① force abort 服务端（绕开 drain 压制，生成循环立即放槽）；
        ② 置 _abandoned——drain/regen 后续一切补答交付熔断（晚到答案对已打断
        的轮=重复内容，正是 17:28:08 重复交付的半个根因）；
        ③ 内芯限时收口。纯超时（机器慢）路径永不调本方法，drain 语义零变化。"""
        self._abandoned = True
        self._fire_abort(force=True)
        try:
            await asyncio.wait_for(self._inner.aclose(), timeout=1.0)
        except Exception:  # noqa: BLE001 - 弃流失败唔阻打断路径
            pass

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
        """真弃流(限时 1s):失败唔阻兜底/重生。W-ABORT：先发 abort 再关内芯
        （force——drain 交接后的最后放弃点，正是要中止服务端僵尸解码的地方）。"""
        self._fire_abort(force=True)
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
        if self._abandoned:
            return
        try:
            parts: list[str] = []
            # regen 换新身份(2026-10-02 审计修):原请求的 X-Bok-Req-Id 经
            # ContextVar 随任务继承——旧版 regen 复用它,而本方法的调用点都先
            # _aclose_inner() 对**同一 id** 发过 /v1/abort,服务端 AbortRegistry
            # 对该 id 的登记项 event 已置位,regen 请求 attach 即被停(call 现场
            # 形状=「LLM_LATE_ANSWER source=regen empty — skip」)。此处 mint 新
            # id(任务内 set 只影响本任务上下文),regen 才真正活到生成完。
            _MLX_REQ_ID_VAR.set(uuid.uuid4().hex)
            async for ev in self._stream_factory():
                delta = getattr(ev, "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
                if isinstance(content, str) and content:
                    parts.append(content)
            text = "".join(parts).strip()
            if not text:
                print("LLM_LATE_ANSWER source=regen empty — skip", flush=True)
                return
            if self._abandoned:
                print(f"LLM_LATE_ANSWER dropped chars={len(text)} (interrupted regen)", flush=True)
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
        if self._abandoned:
            print(
                f"LLM_LATE_ANSWER dropped chars={len(''.join(parts).strip())} (interrupted drain)",
                flush=True,
            )
            return
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
        # 饥荒自适应（第十五波,call-dc54f542 实弹）：regen=同参全量重 prefill,
        # 饥荒中纯负贡献（原流+regen+judge 三重排队,25.3s>22s 传输超时零答案）。
        # 饥荒时禁 regen——原流已被 drain 接住/兜底已出声,等机器缓过来。
        if llm_famine_active():
            print(
                f"LLM_FAMINE regen_skipped ema={_FAMINE_STATE.get('ema', 0):.1f}s"
                " (机器级慢,等原流/兜底,禁重生)",
                flush=True,
            )
            return
        self._spawn_attached(self._regen_late_answer())

    async def _run(self):
        timeout = self._first_deadline
        first_task: asyncio.Task | None = None
        drain_owns = False
        _t_req0 = time.monotonic()
        try:
            # 首 chunk 任务化(RC4):截止计时用 asyncio.wait(唔 cancel 任务)——
            # wait_for(__anext__) 超时会 cancel 掉内芯 tee_peer(async generator
            # 对 cancellation 不免疫,peer 一死=原流续读结构性拿唔到后续 chunk)。
            # 任务留活,drain 接手先收佢再续读;kill-switch 分支才真取消。
            first_task = asyncio.ensure_future(self._inner.__anext__())
            if timeout > 0:
                done, _pending = await asyncio.wait({first_task}, timeout=timeout)
                if first_task not in done:
                    # 饥荒信号(2026-10-02 审计修):超时样本喂 timeout×2 而非
                    # timeout 本身——喂 3.0(s)的话 EMA 上限=3.0,数学上永远够不着
                    # BOK_LLM_FAMINE_TTFT_S 缺省 4.0 的激活线,十五波饥荒自适应
                    # **从未激活过**(swap 抖动机上依旧 3s 超时→drain→regen 负载
                    # ×2 的病态链)。×2=声明性深饥荒信号:首 token 超 3s 的轮,
                    # 真实感知 TTFT 至少是 deadline+兜底/晚到链;drain 交付点另
                    # 补真实样本(见 _drain_late_answer)。
                    record_llm_first_token(timeout * 2.0)
                    if self._late_deadline > 0 and self._late_answer_cb is not None:
                        # 原流续读:唔 aclose——策略选择(2026-10-02 注释同步):
                        # W-ABORT 已能给服务端生成循环止损,但原流「慢但可能仍活」,
                        # 继续读严格优于杀掉重排(同参 regen 全量重 prefill);兜底
                        # 先出声,后台 drain 收晚到真答案;截止无产出才真弃流重生。
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
                                    self._drain_owns = True
                                    self._spawn_attached(self._drain_late_answer(first_task))
                                    return
                            except Exception:  # noqa: BLE001 - 闸回调失败=照常兜底
                                pass
                        self._emit_fallback()
                        drain_owns = True
                        self._drain_owns = True
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
            record_llm_first_token(time.monotonic() - _t_req0)  # 饥荒信号:健康样本
            self._event_ch.send_nowait(await first_task)
            first_task = None
            async for ev in self._inner:
                self._event_ch.send_nowait(ev)
        except asyncio.CancelledError:
            # W-ABORT：框架直 cancel（打断/会话收尾，未必经 aclose 链）——
            # 内芯生成可能还在跑，同点发 abort（与 aclose 幂等共享一旗）。
            self._fire_abort()
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
    """DeepSeek 云端（OpenAI 兼容契约，与本地 MlxLlmLLM 同一官方内芯）。

    max_tokens 构造参优先（缺省 None=现行 env 读）——与 MlxLlmLLM 同治理：
    B 线整句翻译显式 512，不再靠 entrypoint setdefault 写进程 env 下发。
    """

    provider = "deepseek"

    def __init__(
        self,
        api_key="",
        model="deepseek-flash",
        base_url="https://api.deepseek.com/v1",
        thinking: str = "",
        max_tokens: int | None = None,
    ):
        # 思考档位：DeepSeek 端点缺省**关**（官方默认 enabled，而本类 max_tokens 走
        # LLM_MAX_TOKENS 默认 160——思考会把预算烧光、正文出空串，通话侧=静默哑火；
        # 契约与实测见 bok_voice_core.deepseek_llm）。`DEEPSEEK_THINKING=enabled`
        # 可显式开（需同时给足 LLM_MAX_TOKENS）。非 DeepSeek 端点该字段为空 dict。
        # max_tokens 参数化（B 线 MT 车道,2026-10-02 b-line 波）：显式参数 >
        # env LLM_MAX_TOKENS——翻译长文要 1024 级,通话侧不传走缺省。
        if max_tokens is None:
            max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "160"))
        body: dict = {"max_tokens": int(max_tokens)}
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


class _CascadeCloseStreamMixin:
    """包装流级联关闭内层(2026-09-30 A 线官方对账 Critical-1)。

    官方姿势是 ``async with llm.chat(...) as stream``(官方 llm/fallback_
    adapter.py 同款),框架只在最外层调 ``aclose``——此前包装流只关自己的
    泵任务,内层**原生 MLX/MiniMax 流继续解码到自然完稿**:被掐回复的生成
    盗占 GPU(call-9af18da5 双句打断后新回复 TTFT 5.3/6.8s、tps 崩 5.8 的
    机理),文本全进无人读的 channel。级联链:外层 aclose → cancel 本层泵 →
    内层 aclose → httpx 断连 → 队列代理放闸+上游断开 → 服务端中止解码。
    内层关闭尽力而为(异常吞掉),幂等(重复 aclose 安全)。

    位置注记(2026-10-02):定义必须先于全部使用者(_StripMTQuoteStream/
    _ExprPrependStream 等)——类定义立即求值基类列表,后置=导入期 NameError。"""

    async def aclose(self) -> None:
        try:
            await super().aclose()
        finally:
            _inner = getattr(self, "_inner", None)
            if _inner is not None:
                with contextlib.suppress(BaseException):
                    await _inner.aclose()


class _StripMTQuoteStream(_CascadeCloseStreamMixin, llm.LLMStream):
    """剥离 MT 输出包裹引号(StatelessMTLLM 出口单点,TTS/字幕/历史全干净)。

    首个非空增量剥前引号;末字符扣住待定——流结束时是闭合引号则吞、否则补发
    (一字符 hold,延迟≈一个 chunk)。壳照抄 _ExprPrependStream:metrics 由内芯
    发出经 _bind_metrics_forward 转发,此处只排空监视分支。

    级联 mixin(2026-10-02 审计修):B 线 MT 超时路径(_mt_collect 的
    wait_for 超时)从不关流——本类不带级联时,内层 _attach_mlx_abort 挂的
    aclose 补丁**不可达**,每次超时留一条全量 512-token 解码僵尸占 :1236
    (AGENTS 记 21 次/通实证,「句堆积」的形状)。"""

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


class _ExprPrependStream(_CascadeCloseStreamMixin, llm.LLMStream):
    """在真实 LLM 流之前先发一个 <expr type="expression" label="..."/> 标记块。

    级联 mixin(2026-10-02 审计修):本类曾在 A 线回复链
    (_PartialCapture→_RepeatGuard→_StripAnchor→**本类**→_LlmFallback→native)
    唯一断掉 aclose 级联——框架只关最外层,断在这里令 _LlmFallbackStream.aclose
    (关闭路径唯一的 mlx server-abort 触发点)在会话/收线关闭时**永不可达**
    (打断路径靠 agent 侧 abandon() 绕路才活着;call-9af18da5「双句打断后
    TTFT 5.3/6.8s」的同族残余)。"""

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
# 「一、一、二、二」,每个顿号一次 TTS 停顿=机器人感;顿号剥离时长实验
# 6.54s vs 6.40s 等价,故此处只剥分隔符、不改数字形态。)
_DIGIT_PAUSE_RE = re.compile(r"(?<=[零〇一二三四五六七八九0-9])[、，]\s*(?=[零〇一二三四五六七八九0-9])")
_DIGIT_CHAR_RE = re.compile(r"[零〇一二三四五六七八九0-9]")

# ---- 标识符数字槽逐位读（2026-09-27 取证修正）----
# 旧注释「MiniMax 对阿拉伯数字串本来就逐位读」**只对 ≥5 位成立**：缓存罐头音频过
# ASR 复核实测——"1459"(尾号) 被读成 一千四百五十九、"6699"(tracking ending)
# 读成 六千六百九十九；顺串 ≥5 位才是逐位。标识符槽（单号/尾号/电话/热线索/连字符
# 号段）必须逐位读，否则客户听到的号码是错的。金额/数量读数值才对（最低 300 蚊 →
# 三百蚊），一律不碰。
# 只动**标识符语境**里的 3/4 位串：2 位以内数值读==逐位读无需动；≥5 位本就逐位。
# 汉字映射风格与 flow.py:383 `_CANTONESE_DIGITS` 同款（逐位、零→零、1-9 逐位汉字），
# MiniMax 按脚本自行渲染语种，故 zh/cantonese 共用同一套汉字、无需按通话语言分支。
_ID_DIGIT_HANZI = {
    "0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
    "5": "五", "6": "六", "7": "七", "8": "八", "9": "九",
}
# 完整 3/4 位数字 run（前后不得再是数字，否则 ≥5 位顺串会被截 4 位改写）。
_ID_DIGIT_RUN_RE = re.compile(r"(?<![0-9])[0-9]{3,4}(?![0-9])")
# 中文标识符前缀（紧邻数字串之前，允许夹空白/冒号顿号/系词「是·係·为·為」）。
_ID_ZH_PREFIX_RE = re.compile(
    r"(?:尾号|尾號|单号|單號|编号|編號|号码|號碼|电话|電話|热线|熱線|单是|單係)"
    r"[\s:：，,、]*(?:是|係|为|為)?[\s:：，,、]*$"
)
# 英文标识符引导词（"tracking ending 6699" / "order number 1459"）；
# \b 边界的 id/no 防「valid/rapid」类词尾误命中。
_ID_EN_CUE_RE = re.compile(
    r"\b(?:ending|number|no|id|code|ref|reference|order|tracking|hotline|phone|contact)\b"
    r"\.?[\s:：#]*$",
    re.IGNORECASE,
)
# 金额/数量语境后缀（读数值才对）——命中即跳过，绝不改。
_ID_QTY_TAIL_RE = re.compile(
    r"\s*(?:元|蚊|塊|块|美金|美元|dollar|dollars|usd|hkd|"
    r"个|個|次|天|日|工作日|小时|小時|倍|年|月)",
    re.IGNORECASE,
)


def _digitize_id_slots(text: str) -> str:
    """标识符语境的 3/4 位阿拉伯数字串逐位转汉字，其余原样（纯函数）。

    语境判据（任一命中即标识符）：① 中文前缀（尾号/单号/电话…，可夹「是/係」）；
    ② 英文引导词（ending/number/order…）；③ 紧邻连字符（852-1234-5678 /
    E2E-陳小明-2170）。金额/数量后缀（元/蚊/个/天…）优先否决。≥5 位与 1/2 位
    结构上不匹配本 run 正则，天然不动。同输入恒同输出，零 I/O、零实例状态。
    """
    s = str(text or "")
    if not s:
        return s

    def _repl(m: re.Match[str]) -> str:
        run = m.group(0)
        start, end = m.start(), m.end()
        # 金额/数量语境：读数值才对，先否决（.match 在 pos 处锚定，模式无需 ^）。
        if _ID_QTY_TAIL_RE.match(s, end):
            return run
        before = s[:start]
        prev_ch = s[start - 1] if start > 0 else ""
        next_ch = s[end] if end < len(s) else ""
        if not (
            _ID_ZH_PREFIX_RE.search(before)
            or _ID_EN_CUE_RE.search(before)
            or prev_ch == "-"
            or next_ch == "-"
        ):
            return run
        return "".join(_ID_DIGIT_HANZI.get(ch, ch) for ch in run)

    return _ID_DIGIT_RUN_RE.sub(_repl, s)


# (_CascadeCloseStreamMixin 已前移至 _StripMTQuoteStream 之前——2026-10-02
#  审计修:_ExprPrependStream/_StripMTQuoteStream 两个使用者补级联,类定义立即
#  求值基类列表,原位置(此处之后)会令导入期 NameError。)


class _StripTailAnchorStream(_CascadeCloseStreamMixin, llm.LLMStream):
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


def _repeat_cross_turn_on() -> bool:
    """跨轮复读防线总闸(EX-2;默认开,与 BOK_REPEAT_GUARD 同读法)。"""
    return os.environ.get("BOK_REPEAT_CROSS_TURN", "1") == "1"


def _repeat_cross_turn_sim() -> float:
    """跨轮整段相似阈值(EX-2;BOK_REPEAT_CROSS_TURN_SIM 默认 0.85)。"""
    try:
        v = float(os.environ.get("BOK_REPEAT_CROSS_TURN_SIM", "0.85") or 0.85)
    except ValueError:  # pragma: no cover - 配错回默认
        return 0.85
    return v if v > 0 else 0.85


def _repeat_head_max_hold() -> int:
    """D1 终修(2026-09-30):repeat-head 冻结的有界持有上限,字。

    病理(call-3b776663/25 天 24 例):冻结命中的首段在「无句界长首句」下可
    无限期扣住——官方链路 ``_produce_segments`` 只在**非空 chunk**才起 TTS
    段(agent_activity.py:3576-3587),空文本=零 push_text+零 TTS 任务+框架
    干净完稿,死寂直到 watchdog 6-8s 强断。本层有界化:冻结攒到该字数即强制
    经同一 ``_first_chunk_cut`` 放行(数字/拉丁 run 铁闸复用,切点安全面不
    变);换头复读主体仍由「片段+句」拼合纵深在下一句界剥除。宁可 22 字早放
    不零句死。默认 22 字;0=无界旧行为(逐字节回退)。"""
    try:
        v = int(os.environ.get("BOK_REPEAT_HEAD_MAX_HOLD", "22") or 22)
    except ValueError:  # pragma: no cover - 配错回默认
        return 22
    return v if v >= 0 else 22


class _RepeatSelfGuardStream(_CascadeCloseStreamMixin, llm.LLMStream):
    """LLM 流出口逐句剥复读:缓冲到句边界,复读句吞掉、新内容照发。

    全剥空 → 流空收尾(罕见;渐进披露治源头后这里只兜底,真发生时垫话/心跳
    补位)。壳与 _StripTailAnchorStream 同款:metrics 由内芯转发,此处排空。

    EX-2(2026-09-28)跨轮扩展:除比对上一条回复(REPEAT_SELF),再比对
    ContextState 跨轮账本 reply_ledger()(只含 gen=llm 历史回复)——句级相似
    ≥0.9(reuse _is_parrot_sentence)或整段滚动相似 ≥threshold → 剥,治「同一通
    内隔轮复述」(已读乱回)。客户复述/追问轮(allow_repeat)整段放行。

    编造号码守卫(2026-10-01,call-231aa92a):同流逐句过
    ``guard_fabricated_number``(number_on=True 时)——无捕获的号码确认句替换为
    索取句、错号改正为捕获号码。修改发生在**放行前**,TTS/历史/turns 三方拿到
    同一份文本(原文单轨);命中风险句时首段早发让位到句界(整句在手才动)。"""

    def __init__(
        self,
        plugin,
        inner: "llm.LLMStream",
        last_reply: str,
        *,
        bypass: bool = False,
        ledger: list[str] | None = None,
        threshold: float = 0.85,
        allow_repeat: bool = False,
        number_on: bool = False,
        number_lang: str = "",
        number_captured: str | None = None,
        number_turn_text: str | Callable[[], str | None] | None = None,
        number_known_text: str | Callable[[], str | None] | None = None,
        on_full_swallow: "Callable[[str], Awaitable[None]] | None" = None,
    ):
        super().__init__(llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions())
        self._inner = inner
        self._last_reply = last_reply
        self._bypass = bypass or not last_reply
        self._buf = ""
        # 跨轮账本只收非空条目;allow_repeat=true 整段放行(复问)。
        self._ledger = [str(x) for x in (ledger or []) if str(x or "").strip()]
        self._threshold = float(threshold)
        self._cross_on = bool(self._ledger) and not allow_repeat
        self._emitted: list[str] = []
        self._cross_suppressed = 0
        # FIX-3(D2-4,2026-10-01):全吞上抛。被剥句子逐条记账(证据保全);
        # 流收尾时若无一句放出(_emitted 空)且确有剥除 → on_full_swallow(全文)。
        # agent 侧接住=拆响应看门狗+落 turns provider=repeat-suppressed,不再把
        # 「4B 复读全吞」记成「AI 死了」(starve 计数+6s watchdog 道歉)。
        self._swallowed: list[str] = []
        self._on_full_swallow = on_full_swallow
        # 首 chunk 早发(2026-09-28):本回复首段是否已放行(句界或早切任一)。
        self._first_sent = False
        # D1 有界持有(2026-09-30):强制放行观测只打一次,防日志风暴。
        self._head_force_released = False
        # 已早放的片段(换头复读防线):下一句界判定时前缀拼合比对,判复读
        # 只剥余段(片段已出声不可回收,但复读主体不得再播)。
        self._released_head = ""
        # 编造号码守卫接线(2026-10-01):开关+语言+合法数字源(捕获账本 ∪ 本轮
        # 客户原话转写 ∪ 对象档案已知事实);默认关(直接构造的旧调用点零变化,
        # ContextAwareLLM.chat 才按 env 打开)。
        self._number_on = bool(number_on)
        self._number_lang = str(number_lang or "")
        self._number_captured = number_captured
        self._number_turn_text = number_turn_text
        self._number_known_text = number_known_text
        if self._ledger and allow_repeat:
            print("REPEAT_CROSS_TURN_SKIPPED reason=reask", flush=True)

    async def _metrics_monitor_task(self, event_aiter) -> None:
        async for _ in event_aiter:
            pass

    def _is_cross_turn(self, sentence: str) -> bool:
        """句子与本通历史 LLM 回复的复述判定(EX-2)。

        句级 reuse _is_parrot_sentence(阈值 0.9,含子串前缀截断);另叠整段滚动
        相似:已产出句+当前句 与某条历史整体 SequenceMatcher ≥threshold(可配)。"""
        s = _norm_for_similarity(sentence)
        if len(s) < 6:
            return False
        combined = _norm_for_similarity("".join(self._emitted) + sentence)
        for entry in self._ledger:
            if _is_parrot_sentence(sentence, entry):
                return True
            e = _norm_for_similarity(entry)
            if len(e) >= 6 and len(combined) >= 6:
                if difflib.SequenceMatcher(a=combined, b=e).ratio() >= self._threshold:
                    return True
        return False

    def _fragment_is_repeat_head(self, fragment: str) -> bool:
        """首片段是否复读句的头(2026-09-28 早发安全闸)。

        两道命中皆扣住等句界整句判定——复读句的头 10 字漏播不可接受:
        ①前缀命中:片段与语料(last_reply/跨轮账本)归一前缀相同(原句复读);
        ②后缀交叠:片段尾 ≥4 字窗口命中语料任一子串(「换头复读」——头部
        几字不同+整段复读主体,前缀检查接不住;交叠命中宁可多等一轮句界,
        只是丢速度不丢正确性)。
        """
        frag = _norm_for_similarity(fragment)
        if len(frag) < 6:
            return False
        corpus: list[str] = []
        if not self._bypass:
            corpus.append(_norm_for_similarity(self._last_reply))
        if self._cross_on:
            corpus.extend(_norm_for_similarity(e) for e in self._ledger)
        for entry in corpus:
            if not entry:
                continue
            if entry.startswith(frag):
                return True
            # 后缀交叠:片段尾与语料中任一 4 字窗口相同。
            for k in range(4, len(frag) + 1):
                tail = frag[-k:]
                if len(tail) >= 4 and tail in entry:
                    return True
        return False

    def _feed(self, text: str) -> str:
        """缓冲到句边界;完整句非复读才放行,复读句整句吞掉。

        首 chunk 早发(2026-09-28):本回复**首段**在无句界且攒够
        BOK_TTS_FIRST_CHUNK_CHARS 时提前放行(长首句在 tps ~20 下的出声
        闸门在本层——token 攒到句界才放,首句 20+ 字=1 秒级干等)。铁闸
        复用 _first_chunk_cut(数字/拉丁 run 不劈、句界 N+6 容差内让位
        自然断点);前缀命中复读语料的片段扣住(见 _fragment_is_repeat_head)。
        只动首段:后续句仍按句界对齐,复读判定面零变化;env=0 逐字节旧行为。

        编造号码守卫(2026-10-01):number_on 时缓冲里出现数字/语境词残件
        (_number_hold)→ 首段早发让位到句界,整句在手才交给
        guard_fabricated_number 校验(放行前改,TTS/账本同文本)。"""
        if self._bypass and not self._cross_on and not self._number_on:
            return text
        self._buf += text
        out: list[str] = []
        while True:
            m = _SENT_END_RE.search(self._buf)
            if not m:
                break
            sentence = self._buf[: m.end()]
            self._buf = self._buf[m.end() :]
            # 换头复读防线:已早放片段存在时,整句判定用「片段+句」拼合单元
            # (剩余段独立比对相似度不够会漏);判复读只吞句,片段已出声不回收。
            check_unit = (self._released_head or "") + sentence if self._released_head else sentence
            if not self._bypass and _is_parrot_sentence(check_unit, self._last_reply):
                print(f"REPEAT_SELF_SUPPRESSED sent={sentence!r}", flush=True)
                self._swallowed.append(sentence)  # FIX-3:全吞证据保全
                self._released_head = ""
                continue
            if self._cross_on and self._is_cross_turn(check_unit):
                self._cross_suppressed += 1
                print(f"REPEAT_CROSS_TURN_SUPPRESSED sent={sentence!r}", flush=True)
                self._swallowed.append(sentence)  # FIX-3:全吞证据保全
                self._released_head = ""
                continue
            self._released_head = ""
            if self._number_on:
                # 放行前过号码守卫:改后文本=TTS 念的=turns 账本记的(原文单轨)。
                sentence = guard_fabricated_number(
                    sentence,
                    self._number_lang,
                    self._number_captured,
                    self._number_turn_source(),
                    self._number_known_source(),
                )
            out.append(sentence)
            self._emitted.append(sentence)
        if out:
            self._first_sent = True
        elif not self._first_sent:
            n = _tts_first_chunk_chars()
            cut = _first_chunk_cut(self._buf, n) if n > 0 else None
            if (
                cut is not None
                and not self._fragment_is_repeat_head(self._buf[:cut])
                and not self._number_hold()
            ):
                head = self._buf[:cut]
                self._buf = self._buf[cut:]
                self._first_sent = True
                self._released_head = head
                out.append(head)
                self._emitted.append(head)
            elif cut is not None:
                # D1 有界持有(2026-09-30):repeat-head 冻结攒到 BOK_REPEAT_HEAD
                # _MAX_HOLD(默认 22 字)强制放行——无句界长首句在此前可无限期
                # 扣住=零句死(见 _repeat_head_max_hold 档案)。放行走同一切点
                # 函数(数字/拉丁 run 不劈),复读主体仍由下一句界拼合纵深剥。
                # 号码守卫扣留(numeric pending)连强制放行也压住:半截号码出声
                # 不可回收,等句界整句校验(LLM 纪律单句≤24字,等窗短)。
                hold = _repeat_head_max_hold()
                if (
                    0 < hold <= len(self._buf)
                    and not self._head_force_released
                    and not self._number_hold()
                ):
                    self._head_force_released = True
                    head = self._buf[:cut]
                    self._buf = self._buf[cut:]
                    self._first_sent = True
                    self._released_head = head
                    out.append(head)
                    self._emitted.append(head)
                    print(
                        f"REPEAT_GUARD_HEAD_FORCE_RELEASE chars={len(head)} hold={hold}",
                        flush=True,
                    )
        return "".join(out)

    def _number_hold(self) -> bool:
        """编造号码守卫的首段早发扣留判据(number_on 才生效)。

        缓冲里可能出现号码确认(数字/语境词残件)=守卫必须整句在手才能替换/
        改正 → 早发(含 D1 强制放行)让位到句界;number_on=False 恒 False
        (旧路径逐字节零变化)。"""
        return self._number_on and number_guard_pending(self._buf)

    def _number_turn_source(self) -> str | None:
        """本轮客户原话(守卫合法数字源之二)。

        惰性取:抢跑(preemptive)流的构造早于 turn 钩子写完本轮文本,构造期
        快照会拿到上一轮——每次求值现取,流真正喂文本时已是本轮值。取不到
        (异常/非文本)=None,守卫按「无权威源」只认捕获账本。"""
        v = self._number_turn_text
        if callable(v):
            try:
                v = v()
            except Exception:  # pragma: no cover - 取不到=按无权威源
                return None
        if v is None:
            return None
        return v if isinstance(v, str) else str(v)

    def _number_known_source(self) -> str | None:
        """对象档案等系统已知事实(守卫合法数字源之三,2026-10-01 补)。

        AI 念读系统已知数据(快递单号等)做确认係合法确认环——客户当场可纠正,
        与「复述用户真说过的话」同权。惰性取,同 turn_source 姿势。"""
        v = self._number_known_text
        if callable(v):
            try:
                v = v()
            except Exception:  # pragma: no cover - 取不到=无此源
                return None
        if v is None:
            return None
        return v if isinstance(v, str) else str(v)

    def _flush_at_end(self) -> str:
        if (self._bypass and not self._cross_on and not self._number_on) or not self._buf:
            return self._buf
        rest = self._buf
        self._buf = ""
        # 换头复读纵深补面(P3,2026-10-02):流末无句界,已早放片段(_released_head)在场的
        # 判定必须用「片段+余段」拼合单元——与 _feed 句界路径同款(1966)。单看余段时
        # D1 强制放行后的换头残段(头部几字不同、相似度不够)会漏剥。片段已出声不回收,
        # 命中的只是余段。
        check_unit = (self._released_head or "") + rest if self._released_head else rest
        if not self._bypass and _is_parrot_sentence(check_unit, self._last_reply):
            print(f"REPEAT_SELF_SUPPRESSED sent={rest!r}", flush=True)
            self._released_head = ""
            self._swallowed.append(rest)  # FIX-3:全吞证据保全
            return ""
        if self._cross_on and self._is_cross_turn(check_unit):
            self._cross_suppressed += 1
            print(f"REPEAT_CROSS_TURN_SUPPRESSED sent={rest!r}", flush=True)
            self._released_head = ""
            self._swallowed.append(rest)  # FIX-3:全吞证据保全
            return ""
        self._released_head = ""
        if self._number_on:
            # 流末余段同样过守卫(放行前改;TTS 与账本同文本)。
            rest = guard_fabricated_number(
                rest,
                self._number_lang,
                self._number_captured,
                self._number_turn_source(),
                self._number_known_source(),
            )
        self._emitted.append(rest)
        return rest

    @property
    def pending_buffer(self) -> str:
        """cancel 后残留的未播缓冲（P2.a，spec 2026-09-29 v2 §5）。

        barge-in / watchdog force-interrupt 令 _run 在句界前被取消时，_buf 攒着
        的文本曾随协程蒸发（call-ed6aa9b8 三轮 chars=10 实证）——本属性供
        agent 侧 interrupted 补账点读取拼入 turns，账本证据不丢。"""
        return self._buf

    async def _run(self):
        try:
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
            if self._cross_suppressed and not self._emitted:
                print(
                    f"REPEAT_CROSS_TURN_EMPTY suppressed={self._cross_suppressed}",
                    flush=True,
                )
            # FIX-3(D2-4,2026-10-01):复读全吞 ≠ 死火。全吞(一句未放出)且确有
            # 剥除时把被吞全文上抛——agent 侧拆响应看门狗+落 turns 证据行
            # (provider=repeat-suppressed),账本不再把「4B 复读」记成「AI 死了」
            # (starve+1、6s watchdog 强断道歉)。部分剥除(_emitted 非空)=正常
            # 说话中剥复读句,不上抛;回调失败绝不被流收尾(证据记账是尽力而为)。
            if not self._emitted and self._swallowed and self._on_full_swallow is not None:
                try:
                    await self._on_full_swallow("".join(self._swallowed))
                except Exception as exc:  # noqa: BLE001 - 回调异常不破流收尾
                    print(f"REPEAT_SUPPRESSED callback_error={exc!r}", flush=True)
        except asyncio.CancelledError:
            # P2.a（2026-09-29 v2 §5）：cancel 不再令缓冲静默蒸发——打点留痕，
            # agent 侧 interrupted 补账点读 pending_buffer 拼入 turns。
            # first_sent 判别子(2026-09-30 D1):0=整条回复一字未出(冻结/空产)
            # vs 1=已出过声被拦腰掐——两种病理的下一步排查面不同。
            if self._buf:
                print(
                    f"REPEAT_GUARD_CANCEL_DROP chars={len(self._buf)} first_sent={int(self._first_sent)}",
                    flush=True,
                )
            elif not self._first_sent:
                print("REPEAT_GUARD_CANCEL_EMPTY first_sent=0", flush=True)
            raise


# 对象档案行边界=调用方给的显式换行(每个输入行是一个语义单元,如一行背景
# +一行备注);绝不在句号处二次切分——多句背景若被句号切碎,第 2 行(备注)
# 会被静默挤掉,档案失真。


def _context_mem_legacy() -> bool:
    """P1.2a(2026-09-21)记忆压缩 kill-switch:1=回旧档(drop-oldest+上限 1200)。

    进 `_FORWARD_ENV`(tests/test_forward_env 门禁)。"""
    return os.environ.get("BOK_CONTEXT_MEM_LEGACY", "") == "1"


def _memory_cap_from_env() -> int:
    """F2/F5b 生产记忆帽(尾部手术③,实弹定档):BOK_MEMORY_CHARS 默认 180。

    坏值/空回 180;`BOK_CONTEXT_MEM_LEGACY=1`(P1.2a kill-switch)上位——返回 0
    (=不注入,交 `ContextState.__init__` 缺省旧档 1200)。只有 env 装配口
    `ContextState.from_env()`(生产装配点)消费本口,裸构造保持 P1.2a 缺省档。
    """
    if _context_mem_legacy():
        return 0
    try:
        return max(0, int(os.environ.get("BOK_MEMORY_CHARS", "180") or 180))
    except ValueError:
        return 180


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
        # P1.2a(2026-09-21 记忆压缩):显式传参(测试/嵌入方)优先;缺省按 kill-switch
        # 定——新档 400(尾部有界=每轮新 prefill 有界,§48 P1),legacy 档回旧 1200。
        # F2/F5b(2026-09-28/30 尾部手术③)的生产定档(默认 180,BOK_MEMORY_CHARS
        # 可覆盖)走 env 装配口 `ContextState.from_env()`(生产装配点);裸构造
        # (测试/嵌入方)保留本 P1.2a 缺省档,legacy kill-switch 两路同效。
        self._max_summary_chars = (
            max_summary_chars
            if max_summary_chars and max_summary_chars > 0
            else (1200 if _context_mem_legacy() else 400)
        )
        # F1 两段化（2026-09-28 尾部手术③）：当前步文本拆稳定段/增量段。
        # 稳定段只在每步首条消息进尾部；增量段每轮都发。revision 只跟稳定段
        # 身份键走（步内恒定，防止每轮 verdict/底稿波动虚增 revision、令
        # slim/投机门失效）。_applied_stable_keys 与 _applied_tails 平行，
        # 记录每条已冻结尾部当时是否带稳定段（重试重建据此复现同一决定）。
        self._flow_stable: str = ""
        self._flow_delta: str = ""
        self._stable_key: str = ""
        self._applied_stable_keys: list[str] = []
        self._last_emit_stable_key: str = ""
        # 尾部节食（第十一波 2026-09-29）：尾部骑在新 user 消息后=全新位置，前缀
        # 缓存对它零命中，**每轮全量 uncached**（实测 uncached 中位 ~310 tok 里尾部
        # 占 ~250）。记忆块降频（每 K 轮带一次）+ facts 封顶只影响 slim 轮字节量。
        # 与 _applied_tails 平行的第三条账本：每条已发尾部当时是否带记忆块——
        # 降频决定必须是账本纯函数（F3 重试重渲染同一账本状态 → 同一决定 →
        # 逐字节复现，否则 identical_skipped 判定假断裂、前缀真裂）。
        self._memory_in_tails: list[bool] = []
        self._last_tail_had_memory: bool = False
        self._snippets: list[str] = []
        self._summary_lines: list[str] = []
        self._user_lang: str = ""
        self._web: list[str] = []
        self._flow_overview: str = ""
        self._flow_current: str = ""
        self._object_brief: str = ""
        # 说话自然度块渲染门(阶段1·P1,2026-09-25):装配时按实际 TTS 模型置位
        # (voice_style_enabled_for_tts——persona 覆写非 2.8 档自动熄火),
        # 置位才把【说话自然度】块进静态前缀;标记剥离永远执行(与门无关)。
        self._voice_style_on: bool = False
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
        # EX-2(2026-09-28)跨轮复读账本:最近 3 条 AI 回复 (text, gen)。出口复读
        # 防线只对 gen=="llm" 条目比对(脚本直念/罐头结构性不在 LLM 流内,不参与
        # 跨轮比对——见 _RepeatSelfGuardStream)。治「已读乱回」:同一通内隔轮复述。
        self._reply_ledger: list[tuple[str, str]] = []
        # EX-2 复问放行闸:客户复述/追问上一问、或 verdict==REPEAT 时,模型复讲
        # 关键内容正确——跨轮防线放行。**不依赖 has_steps**(旧 repeat_requested
        # 只在 has_steps 块内写,无模板通话结构性失效)。
        self.allow_repeat: bool = False
        # ASR 受限润色映射（2026-09-27,原文单轨契约见 set_polished 注释）。
        self._polished_map: dict[str, str] = {}
        # 本轮客户原话转写(编造号码守卫的合法数字源之二,2026-10-01):turn 钩子
        # 在文本定稿(净化/累积合并完)后写入,LLM 流装配时读——复述内容源唯一=
        # 用户真说过的话;缺省空串=拿不到权威源(守卫只认捕获账本)。
        self._turn_user_text: str = ""

        # D1 槽位化 actor（2026-10-01,第一性原理重构）：闸 BOK_SLOT_ACTOR 由 A 线
        # 装配点（agent.py entrypoint）读一次置位；缺省 False=旧路径逐字节不变
        # （B 线 interpret 不接）。置位后静态前缀换角色卡（set_slot_system）、
        # 每轮尾部换任务块（set_slot_step→slot_actor.build_slot_task_block）。
        self.slot_mode: bool = False
        self._slot_system: str = ""
        self._slot_view: dict = {}
        self._slot_key: str = ""

    @property
    def revision(self) -> int:
        return self._revision

    def set_turn_user_text(self, text: str) -> None:
        """记本轮客户原话转写(逐轮覆盖,不进尾部/revision——纯守卫读面)。"""
        self._turn_user_text = str(text or "")

    @property
    def turn_user_text(self) -> str:
        """只读出口:本轮客户原话转写(编造号码守卫数字源;空=未写)。"""
        return self._turn_user_text

    @property
    def object_brief(self) -> str:
        """只读出口:对象档案(编造号码守卫数字源之三——系统已知事实念读)。"""
        return self._object_brief

    def set_whatsapp_note(self, num: str) -> None:
        v = num or ""
        if v != self._whatsapp_note:
            self._whatsapp_note = v
            self._revision += 1

    @property
    def whatsapp_note(self) -> str:
        """只读出口：本通已捕获的客户号码文本（编造号码守卫比对基准）。

        空串=本通尚未捕获（守卫按「无捕获」处理）。"""
        return self._whatsapp_note

    def set_flow_current(self, current: str) -> None:
        """每轮更新当前步约束(flow controller 推进后调用)。

        F1 两段化（2026-09-28 手术③）：拆稳定段/增量段；revision 只跟稳定段
        身份键走——步内 verdict/底稿波动（增量段）不再虚增 revision，slim 与
        投机门据此稳定。真换步（稳定键变）才 +revision，重建轮据此把末条 user
        尾部对齐新步；无步骤头的任意文本（收尾/测试串）键=整段，旧语义保留。
        """
        current = current or ""
        self._flow_current = current
        stable, delta = split_step_text(current)
        self._flow_stable = stable
        self._flow_delta = delta
        key = stable_step_key(stable)
        if key != self._stable_key:
            self._stable_key = key
            self._revision += 1

    def set_slot_system(self, text: str) -> None:
        """槽位化 actor（D1）角色卡注入——装配点一次写入，整场字节不变
        （render_instruction_prefix 缺省档之一；KV 前缀稳定区）。"""
        self._slot_system = str(text or "")

    def set_slot_step(self, view: dict | None) -> None:
        """编排器给槽（D1）：当步结构化槽位（flow.FlowController.slot_step_view()）。

        revision 跟随步身份键（换步/收尾态切换 +1）：重建轮尾部字节比对与
        prefill 投机 F6 稳定门（snapshot_revision vs now）据此判尾部是否已分叉。
        渲染侧只格式化本视图（render_context_tail 槽位分支），不再有稳定段/
        增量段/slim 账本——任务块每轮随最新 user 消息冻结入史，追加式契约由
        ContextAwareLLM.chat 既有 record_applied_tail 机制承担。
        """
        self._slot_view = dict(view or {})
        key = f"{self._slot_view.get('state') or ''}:{self._slot_view.get('step_no') or ''}"
        if key != self._slot_key:
            self._slot_key = key
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

    def supersede_call_fact(self, needle: str, new_text: str, limit: int = 4) -> None:
        """更正覆盖(D2,2026-09-30 前提推翻更正 lane):移除含 needle 的旧事实
        (新条目自身豁免)后 append 新条目——同槽新值压倒旧值,矛盾事实不再并排
        每轮喂模型。旧事实仍留在旧消息冻结尾部里(≤8 轮自然截断),由新尾部
        的「以X为准」显式压倒——**冻结尾部永不回溯重写(KV 严格前缀契约,
        test_context_append_only 钉死)**。revision 照 bump(slim 门/speculator
        F6 门自动跟随)。"""
        t = str(new_text or "").strip()
        if not t or not str(needle or "").strip():
            return
        kept = [s for s in self._call_facts if needle not in s or s == t]
        if t not in kept:
            kept.append(t)
        new_list = kept[-limit:]
        if new_list == self._call_facts:
            return  # 幂等:无实际变化(重复 supersede)不 bump revision
        self._call_facts = new_list
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

    def record_reply(self, text: str, gen: str) -> None:
        """跨轮复读账本写入(EX-2,2026-09-28):有界 last 3。空文本忽略。

        LLM 轮由 _on_item_for_context 记;脚本/罐头车道由 agent chokepoint 登记
        时点记(即使随后被打断也在案)。"""
        t = str(text or "").strip()
        if not t:
            return
        self._reply_ledger.append((t, str(gen or "")))
        if len(self._reply_ledger) > 3:
            self._reply_ledger = self._reply_ledger[-3:]

    def reply_ledger(self) -> list[str]:
        """只读出口:gen=="llm" 的历史回复(跨轮复读防线比对面)。"""
        return [t for t, g in self._reply_ledger if g == "llm"]

    def set_allow_repeat(self, on: bool) -> None:
        """复问/REPEAT 放行闸置位(EX-2;见 allow_repeat 字段注释)。"""
        self.allow_repeat = bool(on)

    def record_applied_tail(self, orig: str, final: str, *, bare: bool = False) -> None:
        """冻结尾部账本。`bare=True`=非 LLM 回复车道（say 直念/QA 罐头/graph play/
        分支罐头）补进历史的洞消息：只冻裸体、稳定键恒记 ""（EX-1 尾部封洞）。

        稳定键必须恒空——若把当前 `_last_emit_stable_key` 记进洞消息，紧随其后的
        最新消息会误判稳定段已发（`_applied_stable_keys[-1] == _stable_key`）而丢掉
        【现在这一步】指引；且下轮又会因末条稳定键空而重发一次（稳定段付两遍）。"""
        self._applied_tails.append((orig, final, self._revision))
        # F1：与 _applied_tails 平行记本条尾部当时是否带稳定段（""=未带）。
        self._applied_stable_keys.append("" if bare else self._last_emit_stable_key)
        # 尾部节食：平行记本条尾部当时是否带记忆块（洞消息恒 False=裸体无尾部，
        # 计入距离但不重置降频节奏）。
        self._memory_in_tails.append(False if bare else self._last_tail_had_memory)

    def rewrite_last_applied_tail(self, orig: str, final: str) -> None:
        """抢跑重建轮把末条 user 尾部重渲染成当前版后,同步账本(保持 FIFO 对齐)。"""
        if self._applied_tails:
            self._applied_tails[-1] = (orig, final, self._revision)
            if self._applied_stable_keys:
                self._applied_stable_keys[-1] = self._last_emit_stable_key
            if self._memory_in_tails:
                self._memory_in_tails[-1] = self._last_tail_had_memory

    def applied_tails(self) -> list[tuple[str, str, int]]:
        return list(self._applied_tails)

    def prune_applied_tails(self, keep: int) -> None:
        if keep < 0:
            keep = 0
        if len(self._applied_tails) > keep:
            self._applied_tails = self._applied_tails[-keep:]
        if len(self._applied_stable_keys) > keep:
            self._applied_stable_keys = self._applied_stable_keys[-keep:]
        if len(self._memory_in_tails) > keep:
            self._memory_in_tails = self._memory_in_tails[-keep:]

    def _slim_memory_due(self, *, exclude_last: bool = False) -> bool:
        """slim 轮是否携带记忆块——**账本纯函数**（尾部节食,第十一波）。

        距上一次「带记忆的尾部」≥BOK_TAIL_MEMORY_EVERY(默认 3)条 → 到期。
        账本里从未带过（开局/截断重锚）→ 账本第 EVERY 条起带（开局首条通常是
        步首条 emit_stable=True 全量尾，天然带过）。=1 → 每轮都带（旧字节）。
        exclude_last=True（F3 重建复现语境）：末条账本=被复现条自身,回看须排
        自身——否则距界轮（原渲染时距离 2、记账后 3）复现会翻案 → 字节不等 →
        content_changed 假断裂前缀。
        """
        try:
            every = int(os.environ.get("BOK_TAIL_MEMORY_EVERY", "3"))
        except ValueError:
            every = 3
        if every <= 1:
            return True
        ledger = self._memory_in_tails[:-1] if exclude_last else self._memory_in_tails
        n = len(ledger)
        for i in range(n - 1, -1, -1):
            if ledger[i]:
                return (n - 1 - i) >= every
        return n >= every

    def _stable_refresh_span(self) -> int:
        """稳定段重发回看窗口(条)。默认 max(2, LLM_HISTORY_TURNS-2)——载条距
        窗口底留 2 条余量(截断按消息计,bare 洞消息同占一格);BOK_TAIL_STABLE_SPAN
        显式覆盖(≥1)。"""
        try:
            explicit = int(os.environ.get("BOK_TAIL_STABLE_SPAN", "0"))
        except ValueError:
            explicit = 0
        if explicit >= 1:
            return explicit
        try:
            hist = int(os.environ.get("LLM_HISTORY_TURNS", "6"))
        except ValueError:
            hist = 6
        return max(2, hist - 2)

    def _stable_stale_in_window(self) -> bool:
        """当前步稳定段是否需要重发——**账本纯函数**（窗口纪律,第十一波修）。

        回看最近 _stable_refresh_span() 条冻结尾部:任何一条带当前稳定键 →
        指引仍在截断窗口内,不重发;一条都不带(换步首条/载条即将被淘汰) → 重发。
        slim/bare 条目记 ""(不携带),天然不匹配。"""
        for k in self._applied_stable_keys[-self._stable_refresh_span():]:
            if k == self._stable_key:
                return False
        return True

    def tail_emit_stable_for_rebuild(self) -> bool:
        """重试/重建轮（n_new==0）复现末条尾部当时的稳定段发出决定（F3）。

        窗口纪律复现（第十一波修）：末条账本项**自身入列前**的回看结果——
        等价于「键变（新步在窗内无载条）→必带新稳定段；键未变→复现末条自己
        的决定 bool(last)」。旧实现 `key != last` 把「末条=同步 slim 条(记 "")」
        误判成换步 → 重建必带稳定段 → 与冻结尾部字节不等 → content_changed
        假断裂 → 前缀真裂（slim 轮后的每次 F3 重建都在裂）。
        """
        if os.environ.get("BOK_TAIL_SLIM", "1") == "0":
            return True
        if not self._applied_stable_keys:
            return True
        for k in self._applied_stable_keys[-(self._stable_refresh_span() + 1):-1]:
            if k == self._stable_key:
                return bool(self._applied_stable_keys[-1])
        return True

    @classmethod
    def from_env(cls, account_id: str = "") -> "ContextState":
        """env 装配口:CONTEXT_RAG=1 → 打开知识/联网节的尾部渲染(默认关)。

        与 agent.py 的取数门控 _context_rag_enabled 同一 env 开关;装配处换用
        本口即可让「取数开」与「渲染开」永远同源,不留两套判定。
        记忆总长同口注入(F2/F5b 尾部手术③生产定档 180,BOK_MEMORY_CHARS 可
        覆盖;legacy kill-switch 上位时由 __init__ 缺省旧档 1200 接管,见
        _memory_cap_from_env)。
        """
        st = cls(account_id=account_id, max_summary_chars=_memory_cap_from_env())
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
        self.set_flow_current(current)

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

    @property
    def user_language(self) -> str:
        """装配时钉死的通话语言（润色层车道判定用，A 线恒非空）。"""
        return self._user_lang

    # ---- ASR 受限润色（2026-09-27）：raw→polished 映射，只喂 LLM 上下文 ----
    # 原文单轨铁律：本 map 的存在不改变任何业务判据——FlowController/WA 收号/
    # QA 快路读的都是框架 chat_ctx 里的 raw；polished 只在 ContextAwareLLM
    # 冻结点（user 消息首次拼尾部时）替换请求侧文本。确定性映射保证同 raw
    # 恒同 polished，账本重放期间不重算、严格前缀契约不破。
    def set_polished(self, raw: str, polished: str) -> None:
        # 首写为准（确定性契约：同 raw 恒同 polished——轮处理器预计算与冻结点
        # 兜底两条写入路径对同一 raw 必然产出相同结果；万一不一致，保先到的
        # 预计算结果，账本与映射永不漂移）。
        if raw and polished and polished != raw and raw not in self._polished_map:
            self._polished_map[raw] = polished

    def polished_of(self, raw: str) -> str | None:
        return self._polished_map.get(raw)

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

    def set_voice_style(self, on: bool) -> None:
        """【说话自然度】块渲染门(agent.py 装配时置位;见 voice_style.py)。"""
        self._voice_style_on = bool(on)

    def render_instruction_prefix(self) -> str:
        """【稳定指令前缀】——放最前、紧贴人设 base。

        含：用户语言规则 / 回复节奏 / 应答准则 / 话术流程总览(整通不变) /
        对象档案(set_object_brief 会话装配时一次性注入,整通不变)。
        不变量：前缀整场字节不变（步骤推进只改尾部）→ mlx KV-cache 整场命中；
        当前步约束已移到尾部（推进若改前缀,token0 起整段重 prefill,实测卡 3-5s）。
        真正每轮变的当前步/检索资料/记忆都放 render_context_tail()。

        D1 槽位化（2026-10-01）：slot_mode 置位时整段换成装配点注入的角色卡
        （slot_actor.build_slot_system——人设压缩+facts+语言块+口吻+范例）——
        旧协议族（总览/共享规则/纪律/范例三段/对象档案）全部退出 prompt。
        """
        if self.slot_mode:
            return self._slot_system
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
        # 步骤纪律（2026-09-28 prompt 手术②）：原挂在 flow.current_step_text()
        # 尾部、每轮逐字复读 ~180 字符——S5「重复控制」同款手术：无条件文本
        # 上移稳定前缀，整场 KV 命中零成本，每全量轮省 ~130 token ≈ 0.17s
        # prefill。语义不变；「当前这一步」由尾部【现在这一步】块逐轮点名。
        parts.append("【步骤纪律】" + STEP_DISCIPLINE_RULE)
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
        if self._voice_style_on:
            # 静态字节(整场不变,KV 安全);非 2.8 合成档装配侧不置位=块缺席,
            # 且 TTS transform 全剥标记——双保险防标记被当文本念出。
            parts.append(NATURALNESS_BLOCK)
        if self._flow_overview:
            parts.append("【话术流程总览(别照读,按进度推进)】\n" + self._flow_overview)
        # 对象档案:静态、整场不变,放总览之后(先懂流程再看客户是谁)。有界
        # (2 行×150 字,set_object_brief 保证),前缀体积影响一次性 prefill 可忽略。
        if self._object_brief:
            parts.append("【对象档案】\n" + self._object_brief)
        return "\n\n".join(parts)

    def _last_reply_anchor(self) -> str:
        """【你上一句】截短锚:只示开头 8 字（F5 由 12 收窄——尾部每轮 prefill
        的直接组分）。禁止重述的指令已上移稳定前缀【重复控制】
        （render_instruction_prefix 已含同义句），尾部不再复读，只留引文；
        标签【你上一句】字面不变（_StripTailAnchorStream 靠它剥拟声复刻）。"""
        head = self._last_reply[:8]
        ell = "…" if len(self._last_reply) > 8 else ""
        return "【你上一句】「" + head + ell + "」"

    def render_context_tail(self, *, emit_stable: bool | None = None) -> str:
        """【易变参考尾部】——每轮变的当前步/检索资料/记忆，垫在 system 最末。

        前缀(稳定指令+话术总览+对象档案)+人设 base 在前且整场字节不变，flow
        步骤推进只改这段尾部（短、逐轮重渲染）→ 前缀 KV-cache 照命中，每轮只
        prefill 尾部增量。当前步放尾部最前，让「推进=换一小段尾部」而非动前缀。
        知识/联网两节仅在 rag_enabled=True 时渲染(默认关:封闭话术流程不做检索,
        单对象只上话术+对象档案;CONTEXT_RAG=1/开放人设由装配处置 True)。

        F1 两段化（2026-09-28 手术③，替代旧紧凑标签档）：
        - 稳定段【现在这一步】=步身份/目标/底稿/注意/身份后备/禁讲清单，只在
          每步**首条消息**进尾部（emit_stable 由 _stable_key vs 已冻结尾部键
          _applied_stable_keys 决定）；后续消息不带——旧步指引原样留在该步首条
          消息的历史里，逐轮重 prefill 是纯浪费。
        - 增量段（verdict 指引/数字/命中分支）每轮都发（_flow_delta）。
        - BOK_TAIL_SLIM=0 回退旧行为：稳定段恒进每条尾部。slim 档（revision
          与上条冻结尾部相同）另发一行紧凑「继续」标签。
        - 稳定段发出决定记进账本（record/rewrite），重试/重建轮（F3）据此复现
          同一条尾部的字节，只有真换步或真内容变化才发生语义必需的断裂。
        【你上一句】的固定指令文本已上移稳定前缀（【重复控制】）,尾部只留引文。

        D1 槽位化（2026-10-01）：slot_mode 置位时整段换成任务块（slot_actor.
        build_slot_task_block——当前步/命中分支/事实槽/8 字锚）——总览/共享规则/
        纪律/verdict 指引/状态标记/记忆摘要全部不进 prompt（规格铁律）。
        稳定段/增量段/slim 账本不参与槽位分支：任务块每轮随最新 user 消息冻结
        入史（record_applied_tail 机制原样复用），历史重放逐字节原样。
        """
        if self.slot_mode:
            return build_slot_task_block(
                view=self._slot_view,
                object_brief=self._object_brief,
                call_facts=self._call_facts,
                whatsapp_note=self._whatsapp_note,
                anchor=self._last_reply_anchor() if self._last_reply else "",
            )
        _last_rev = self._applied_tails[-1][2] if self._applied_tails else None
        _explicit_stable = emit_stable is not None  # F3 重建复现语境（显式传入）
        slim = (
            os.environ.get("BOK_TAIL_SLIM", "1") == "1"
            and _last_rev is not None
            and _last_rev == self._revision
        )
        if emit_stable is None:
            # 窗口纪律（第十一波修,2026-09-29）:稳定段只在 (a)换步 或 (b)载有
            # 本步稳定段的最近冻结尾部即将被截断窗口淘汰 时重发。旧行为=与
            # **末条**账本键比对——slim 轮记 "" → 键≠"" → 隔轮重发(实测 uncached
            # 167↔642 交替形状的来源,一半轮白付 ~220 tok 步底稿)。
            emit_stable = (
                os.environ.get("BOK_TAIL_SLIM", "1") == "0"
                or self._stable_stale_in_window()
            )
        # 供 record_applied_tail/rewrite_last_applied_tail 记账（本条尾部带稳定段否）。
        self._last_emit_stable_key = self._stable_key if emit_stable else ""
        parts: list[str] = []
        _diet = slim and not emit_stable  # 尾部节食只作用于 slim 轮（同步未推进）
        if self._customer_intent and not _diet:
            # P2.4 意图喂下游:当轮客户意图(graph 命中 → 规则归类)。**只在全量档**
            # 渲染(_diet=瘦身档刻意不含它)——slim 紧凑档的语义是「状态无实质
            # 变化」,意图属实质信息,变化即 +revision 逼本轮走全量档(见
            # set_customer_intent)。kill-switch=0 时字段恒空,本行不出现。
            parts.append("【客户意图】" + self._customer_intent)
        if self._whatsapp_note:
            parts.append(
                "【已记录客户 WhatsApp】" + self._whatsapp_note +
                "（复述号码必须逐位以此为准，不要凭记忆或猜测）"
            )
        if self._call_facts:
            # 会中事实沉淀(append-only 有界≤4,add_call_fact,变化轮 bump revision
            # → 自动落在全量尾部轮):治「重复问已答过的事」。
            parts.append(
                "【通话中客户已讲（已确认过，不要再问）】\n"
                + "\n".join(f"- {s}" for s in self._call_facts)
            )
        if self._flow_stable and emit_stable:
            # 稳定段(随步推进而变的步身份/底稿):只在每步首条消息发,推进只改这里、
            # 前缀字节不动。
            parts.append("【现在这一步】\n" + self._flow_stable)
        elif slim and self._flow_current:
            _step_head = (self._flow_current.strip().splitlines() or [""])[0]
            parts.append(f"【{_step_head or '流程'}·继续】状态无实质变化，按上文同一步要求继续。")
        if self._flow_delta:
            # 每轮增量段(verdict 指引/数字核对/命中分支):每轮都发。
            parts.append(self._flow_delta)
        if self._last_reply:
            # 重复锚(截短版,2026-09-12 P0/F5):旧版把上一句全文引在尾部,等于把
            # 抄袭素材递到 4B 嘴边(call-8fa17d2b 两轮回复一字不差实证)。只示
            # 开头 8 字——全文在对话历史里,对照能力不丢;标签【你上一句】字面不变
            # (_StripTailAnchorStream 靠它剥拟声复刻),【重复控制】在前缀兜禁令。
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
            # 只带最近几轮记忆(默认 3,F2 由 6 收窄):尾部每轮 prefill 只吃增量,
            # 行数是 TTFT 杠杆;更早的上下文由原始历史截断(LLM_HISTORY_TURNS)与
            # 当前步约束兜底。尾部真实预算(RAG 关,默认档) = 稳定段(每步首条,
            # 典型 ~321 字) 或 紧凑标签 + 增量段(每轮) + 【本通对话记忆】节头 +
            # 摘要总长 ≤BOK_MEMORY_CHARS(默认 250 字,原 600;3 行×每行 ≤201 字
            # 上限,总长先裁);RAG 开另加知识 2×~151 字 + 联网 1×~151 字。
            # 节食(第十一波):slim 轮降频——距上次带过 ≥BOK_TAIL_MEMORY_EVERY
            # (默认 3)条才带;全量轮(稳定段/非 slim)恒带。质量逻辑:尾部骑在新
            # user 消息后=每轮全新 uncached(前缀缓存零命中),而最近对话本来就在
            # LLM_HISTORY_TURNS 原始历史窗口里,记忆块职责=窗口外的旧事实,隔 K
            # 轮不带不丢信息。=1 → 每轮都带(旧字节)。emit_stable 显式传入=F3
            # 重建复现语境,回看排末条(被复现条自身)。
            _mem_due = (not _diet) or self._slim_memory_due(
                exclude_last=_explicit_stable
            )
            self._last_tail_had_memory = _mem_due
            if _mem_due:
                keep = max(1, int(os.environ.get("REPLY_MEMORY_LINES", "3")))
                parts.append("【本通对话记忆】\n" + "\n".join(self._summary_lines[-keep:]))
        else:
            self._last_tail_had_memory = False
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
        # FIX-3(D2-4,2026-10-01):复读防线全吞回调(装配时注入;None=不接=旧行为)。
        # 见 set_full_swallow_cb 与 _RepeatSelfGuardStream._run。
        self._full_swallow_cb: "Callable[[str], Awaitable[None]] | None" = None
        # P2.a：最近一次 guard 流（chat() 时更新；agent 侧 interrupted 补账读
        # pending_buffer 用。None 安全：bypass 档/测试替身路径无 guard）。
        self._last_guard_stream: "_RepeatSelfGuardStream | None" = None
        _bind_metrics_forward(inner, self)

    async def _prewarm_impl(self) -> None:
        # 官方对账(2026-09-30 High-3):官方每通 activity start 的 llm.prewarm()
        # 只认 _prewarm_impl 覆写——包装层不透传=真预热死在链上(agent.py 注释
        # "由 AgentSession 自动调用"与事实不符)。照 StatelessMTLLM 的委托形状。
        inner_prewarm = getattr(self._inner, "_prewarm_impl", None)
        if inner_prewarm is not None:
            await inner_prewarm()

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
                    # D1 槽位化（2026-10-01）：slot_mode 置位时角色卡即整个人格面
                    # （人设 base 已压缩进卡，见 slot_actor.build_slot_system），
                    # 不并 head——旧路径零变化（slot_mode 缺省 False）。
                    if self._ctx.slot_mode and prefix:
                        merged: list = [prefix]
                    elif isinstance(head, str):
                        merged = [_join_system(prefix, head, "")]
                    else:
                        merged = [*([prefix] if prefix else []), *head]
                    items[0] = llm.ChatMessage(role="system", content=merged)
                else:
                    items.insert(0, llm.ChatMessage(role="system", content=[_join_system(prefix, "", "")]))
                # 截断历史(摊销式,见 _truncate_chat_items):先剪后对齐,账本自尾映射。
                # 5b(2026-09-30 soak A/B 定档):8→6——TTFT p50 1297→1107/max
                # 2940→1295、commit_to_audio 中位 ~2070→~1430(增量尖峰 1252→628);
                # 摊销截断+账本自尾对齐机制原样,前缀安全。env 一键回 8。
                # 合并注记:origin/main P1.3「缺省 40=通话内不截断」与 5b 实测档
                # 冲突——按 HEAD 实测定档保留 6(tests/test_voice_mode 合并树重算);
                # main 档意图可用 env 显式 `LLM_HISTORY_TURNS=40` 取回。
                max_turns = int(os.environ.get("LLM_HISTORY_TURNS", "6"))
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

                def _polish_body(raw: str) -> str:
                    # LLM 上下文侧润色替换（2026-09-27 ASR 受限纠错层,原文单轨:
                    # raw 是账本键永不改;确定性映射,同 raw 恒同 polished——先查
                    # 轮处理器 async 预计算(含 CSC 二道)的 map,miss 才本地同步
                    # 兜底吸附)。关档直通 raw。
                    if not raw or not _polish_layer_on():
                        return raw
                    hit = self._ctx.polished_of(raw)
                    if hit is not None:
                        return hit
                    body = _polish_sync_text(raw, self._ctx.user_language or None)
                    if body != raw:
                        self._ctx.set_polished(raw, body)
                    return body

                def _replay(idx: int, orig: str, final: str) -> bool:
                    it = items[idx]
                    if isinstance(it, llm.ChatMessage) and _text_of(it) == orig:
                        items[idx] = llm.ChatMessage(role="user", content=[final])
                        return True
                    # 原文对不上(极端改写)→ 跳过该条,损失局部缓存也好过乱拼。
                    return False

                def _compose(body: str, tail: str) -> str:
                    # user 消息拼装单点：旧路径「客户话+尾部」（逐字节同旧）；
                    # D1 槽位化路径「任务块+客户话」（规格形状，slot_actor.
                    # compose_slot_user_message——与投机预热共用同一序防分叉）。
                    if self._ctx.slot_mode:
                        return compose_slot_user_message(tail, body)
                    return f"{body}\n\n{tail}" if (body and tail) else (body or tail)

                if n_new > 0:
                    # 旧的 applied 对应 users 前 |applied| 条(时序一致),逐条重放;
                    # 新增的尾部 user 从最后一条起各拼当前尾部并入账。
                    for k, (orig, final, _rev) in enumerate(applied):
                        _replay(users[k], orig, final)
                    # EX-1 尾部封洞（2026-09-28）：非 LLM 回复车道（flow say 直念/
                    # QA 罐头/graph play/分支罐头）把客户消息 append 进 chat 上下文后
                    # 从不调 LLM，该消息永不入尾部账本；下一真轮 n_new≥2，旧循环给
                    # 每条新消息都渲染尾部——稳定段落在「已被替答」的洞消息上，紧随
                    # 其后的最新消息只拿紧凑标签，且末条账本稳定键为空 → 下轮又重发
                    # 一次稳定段（稳定段付两遍，实测 ~1975 未缓存 token ≈ 2.2-2.4s）。
                    # 修=除最后一条外全部冻结裸体（无尾部）：洞消息从不渲染尾部，稳定
                    # 段只由最新消息照常发出一次。裸体条目仍是普通账本项
                    # (orig, final, rev)，原样重放；稳定键恒空（bare=True）。
                    new_indices = users[len(applied):]
                    for idx in new_indices[:-1]:
                        it = items[idx]
                        orig = _text_of(it) if isinstance(it, llm.ChatMessage) else ""
                        final = _polish_body(orig)
                        if final != orig:
                            items[idx] = llm.ChatMessage(role="user", content=[final])
                        self._ctx.record_applied_tail(orig, final, bare=True)
                    for idx in new_indices[-1:]:
                        it = items[idx]
                        orig = _text_of(it) if isinstance(it, llm.ChatMessage) else ""
                        tail = self._ctx.render_context_tail()
                        body = _polish_body(orig)
                        final = _compose(body, tail)
                        if tail or body != orig:
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
                            body = _polish_body(actual)
                            rebased = _compose(body, tail)
                            items[users[offset + k]] = llm.ChatMessage(role="user", content=[rebased])
                            last_orig = actual
                            self._ctx.rewrite_last_applied_tail(actual, rebased)
                            ok = True
                        if k == len(tail_window) - 1:
                            last_replayed = ok
                    if last_replayed and tail_window:
                        # F3 重试字节稳定（2026-09-28 手术③）：重渲染当前尾部与冻结尾部
                        # 逐字节比对——相同则原样重放（不动请求=前缀不裂），只有真换步/
                        # 真内容变化才重写。旧版按 revision 无条件重写，是同轮重试
                        # 19.9% 尾部断裂的来源。重渲染用重建档发出决定（复现末条尾部
                        # 当时的稳定段带否），否则会把已带的稳定段重渲染成不带=误判变化。
                        tail = self._ctx.render_context_tail(
                            emit_stable=self._ctx.tail_emit_stable_for_rebuild()
                        )
                        body = _polish_body(last_orig)
                        final = _compose(body, tail)
                        if final == tail_window[-1][1]:
                            print("TAIL_REWRITE identical_skipped", flush=True)
                        else:
                            items[users[-1]] = llm.ChatMessage(role="user", content=[final])
                            self._ctx.rewrite_last_applied_tail(last_orig, final)
                            print("TAIL_REWRITE content_changed", flush=True)
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
            # 编造号码守卫(2026-10-01)总闸:BOK_NUMBER_GUARD 默认 "1",="0" 零行为
            # 变化。与复读防线共用同一条句级流(两条防线互相独立——复读闸关时
            # 号码守卫仍要跑,故这里是 or)。
            _number_on = os.environ.get("BOK_NUMBER_GUARD", "1") == "1"
            _repeat_on = os.environ.get("BOK_REPEAT_GUARD", "1") == "1"
            if (_repeat_on or _number_on) and self._ctx is not None:
                # 出口复读防线(2026-09-12):逐句比对上一句回复,拟声复读句剥掉
                # (call-8fa17d2b 两轮一字不差实证);客户要求重讲轮放行。
                # EX-2:再叠跨轮账本(gen=llm 历史回复),治同通隔轮复述;
                # BOK_REPEAT_CROSS_TURN=0 或复问放行(allow_repeat)时账本喂空。
                _cross_ledger = (
                    self._ctx.reply_ledger()
                    if _repeat_on
                    and _repeat_cross_turn_on()
                    and not self._ctx.allow_repeat
                    else []
                )
                out = _RepeatSelfGuardStream(
                    self, _stripped, self._ctx.last_reply,
                    bypass=(not _repeat_on)
                    or self._ctx.repeat_requested
                    or self._ctx.allow_repeat,
                    ledger=_cross_ledger,
                    threshold=_repeat_cross_turn_sim(),
                    allow_repeat=self._ctx.allow_repeat,
                    number_on=_number_on,
                    number_lang=self._ctx.user_language,
                    number_captured=self._ctx.whatsapp_note,
                    # 惰性读:抢跑流构造早于 turn 钩子写完本轮原话(见
                    # _number_turn_source)。
                    number_turn_text=lambda: self._ctx.turn_user_text,
                    # 合法源之三:对象档案已知事实(快递单号等系统数据念读)。
                    number_known_text=lambda: self._ctx.object_brief,
                    on_full_swallow=self._full_swallow_cb,
                )
                # P2.a（2026-09-29 v2 §5）：持最近 guard 流引用——agent 侧 speech
                # watcher 在 interrupted 补账时读 pending_buffer，cancel 轮的
                # 未播缓冲（tee 捕不到的部分）不再蒸发。
                self._last_guard_stream = out
            else:
                out = _stripped
            # 部分文本 tee(2026-09-17,治「打断轮零账本」):把本回复已生成的文本
            # 逐段记进 agent 注入的 capture dict——回复被框架打断时(item 永不
            # added)agent 侧 speech watcher 用佢补记 gen=interrupted 账本行;
            # 正常走完自动清空(item_added 照常上报,零双记)。
            if self._partial_capture is not None:
                out = _PartialCaptureStream(self, out, self._partial_capture)
                # D1（2026-09-30 Phase 0 定案）：最外层回复流引用——interrupted
                # 收尸点 aclose 用（guard 层杀缓冲任务树根，最外层解框架消费链
                # 的悬死等待；病理形态=下一轮 LLM 完成但零 push 到 TTS）。
                self._last_reply_stream = out
            return out
        return inner_stream

    def set_partial_capture(self, capture: dict | None) -> None:
        """注入 per-turn 部分文本 tee({"text": str})。None=关闭。"""
        self._partial_capture = capture

    def set_full_swallow_cb(self, cb) -> None:
        """FIX-3(D2-4,2026-10-01):复读防线全吞回调注入(装配时,agent 侧)。

        cb(text)=本轮回复被复读防线全吞时收到被吞全文(guard 流收尾处 await
        调用);None(B 线等)=不接=旧行为(全吞照旧静默收尾,账本不补证)。"""
        self._full_swallow_cb = cb


class _PartialCaptureStream(_CascadeCloseStreamMixin, llm.LLMStream):
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
    现在:dialog 涨到 6×max_turns 对才动手、一次剪回 max_turns 对——之后最多
    6×max_turns 轮纯追加(缓存逐轮命中),每 6×max_turns 轮才重锚一次。F4
    (2026-09-28 手术③)由 2×→(触发 4×)改为触发 6×:历史截断整段重锚实测
    5-9s/次、占 6.6% 轮次,触发点抬高一半=重锚频率减半,坍塌成本减半;剪回
    目标不变(2×max_turns 条=最近 max_turns 对)。更早的信息由 ContextState
    「本通对话记忆」摘要承担,剪掉不丢上下文。
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
    # 滞回:超过 6×max_turns 对(12×max_turns 条)才截,剪回 2×max_turns 条。
    if len(dialog) <= max_turns * 6:
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
        # LLMMetrics 转发必须在构造期绑定(RCA §0.3):内芯流监视器 emit 在
        # 创建流的对象上,包装层不转发则 session 永远收不到——2026-09-29 勘误:
        # 本行曾因 _prewarm_impl 插入接缝被吞进方法体(且 `inner` 作用域不存在
        # →NameError 被预热兜底吞=LLM_TTFT_MS/PRECEIVED 全灭一整天),归位。
        _bind_metrics_forward(inner, self)

    async def _prewarm_impl(self) -> None:
        # 同 ContextAwareLLM(2026-09-30 High-3):包装层透传官方 prewarm。
        inner_prewarm = getattr(self._inner, "_prewarm_impl", None)
        if inner_prewarm is not None:
            await inner_prewarm()

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
        pronunciation: list[str] | None = None,
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
        # 请求级发音词典(人名/专名读准):每条 `原词/读法`(读法=拼音/IPA/粤拼/
        # 纯文本替换)。粤语拼法由调用方给(本层不做 g2p);空/None=完全不下发键,
        # 现行为零变化(2026-09-27)。
        self._pronunciation = [str(e) for e in (pronunciation or []) if str(e).strip()]

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

    def _prep_outbound(self, text: str) -> str:
        """实例侧出站文本预处：标识符数字槽逐位化 + 非 2.8 档剥自然度标记。

        数字槽逐位化先做（与标记无交互，:`func:`_digitize_id_slots`）：标识符语境
        的 3/4 位阿拉伯串按逐位读，修 MiniMax 数值读法事故（1459→一千四百五十九）。
        标记只係 2.8 系的合成语义；会话级 transform 门按**主档**判（agent.py
        `_tts_primary.resolved_model()`），FallbackAdapter 切到 2.6 备档时那层
        管不到——(emm)/<#0.3#> 会被 2.6 当文本照念。本层按**本实例**档位兜底
        剥干净（2026-09-27，test_minimax_prep_outbound_strips_tags_on_26 钉住）。"""
        s = _digitize_id_slots(text)
        if a_line_tags_supported(self._model()):
            return s
        return strip_voice_style(s)

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

    def _apply_pronunciation(self, payload: dict) -> dict:
        """非空发音词典就加 `pronunciation_dict` 键,空=完全不加(现行为零变化)。

        官方 schema 只一个 `tone: string[]`,每条 `原词/读法`;多条同时生效。
        task_start(bidi/classic 两处)与 HTTP 整段三路共用本单点,防漏接。
        """
        if self._pronunciation:
            payload["pronunciation_dict"] = {"tone": list(self._pronunciation)}
        return payload

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
        # 请求级发音词典(空=不加键)。
        self._apply_pronunciation(start)
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
        text = self._prep_outbound(lecture_guard(str(text), self._speech_lang()))
        return _MiniMaxTTSStream(self, text, conn_options or APIConnectOptions())

    def _speech_lang(self) -> str | None:
        """罐头回应的语言:会话锚定语言(zh/cantonese)优先,其它(如 en)留 None 自动判。"""
        lang = self._language_state.lang
        return lang if lang in ("zh", "cantonese") else None

    async def prewarm(self) -> bool:
        """预热一次 WS 会话入池。返回 True=池里现在有暖会话;False=不可预热(HTTP 车道/未配置/失败)。绝不 raise。

        冻结契约(W-TTS,会话装配单点调用):bidi(默认)=预连持久会话
        (connect+task_start,首段合成零握手段;连接整通复用);classic=预连
        「处女连接」入池(只 connect,容量 1,合成取用免 TCP+TLS 握手)。池是
        纯快路径:取唔到/陈旧一律回退流内自连,合成永不因预热失败而慢或错。
        `BOK_TTS_PREWARM=0` 整闸回退:立即 False,池逻辑全旁路(旧行为逐字节不变)。
        """
        if not _minimax_prewarm_enabled():
            return False
        try:
            # HTTP 整段车道(无 WS 握手可摊销):不可预热,零行为变化。
            if os.environ.get("MINIMAX_WS", "1") != "1":
                return False
            if not self._api_key() or not self._resolve_voice():
                return False
            if self._ws_mode() == "bidi":
                # W1b(2026-10-06):bidi 主档也并行暖 1 条 classic 池连接——主档
                # 异常切 FallbackAdapter backup(classic 档)时 pool=hit 免冷握手段
                #(实弹 fresh ws_connect_ms≈2468ms,是切换黑窗 4-7s 的主成分)。
                # fire-and-forget:绝不阻塞本预热;池深仍 1、单飞幂等
                #(_minimax_pool_schedule 自带在飞/已有连接去重);纯快路径,失败
                # 静默,合成取唔到池照旧流内自连。沿用 BOK_TTS_PREWARM +
                # MINIMAX_WS_POOL 双闸(经 _minimax_pool_enabled)。
                _minimax_pool_schedule(self._endpoint_ws(), self._api_key())
                return await self._bidi_session().prewarm_wait()
            return await _minimax_pool_prewarm(self._endpoint_ws(), self._api_key())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 预热尽力而为:失败静默,合成路径自会流内自连
            return False

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
# 命中即**立刻**后台补一条（池深 1，不等本段合成收尾）——每一轮都免握手，
# 唔止第一轮。`BOK_TTS_PREWARM=0` 总闸（prewarm() 立即 False + 池逻辑全旁路）；
# MINIMAX_WS_POOL=0 旧开关保留（恒走流内自连）。
_MINIMAX_POOL_WS = None  # 热连接（websockets 客户端实例）
_MINIMAX_POOL_KEY: tuple[str, str] | None = None  # 入池时 (endpoint, api_key)
_MINIMAX_POOL_AT = 0.0  # 入池时刻（monotonic）
_MINIMAX_POOL_TTL_S = 240.0  # 超龄弃用（服务端对空闲连接的生命周期未文档化）
_MINIMAX_POOL_TASK: asyncio.Task | None = None  # 补池任务（单飞）


class PrewarmFallbackTTS(tts.FallbackAdapter):
    """官方 FallbackAdapter 的预热兼容垫（2026-09-28）。

    官方 ``FallbackAdapter.prewarm()``（livekit 1.8）按**同步**约定直调 primary
    child——``MiniMaxTTS.prewarm`` 已 async 化（W-TTS 会话预热池），官方一跳产生
    未 await 协程（RuntimeWarning + 经适配器链的预热静默失效）。覆写：child 同步
    签名照旧直调；协程签名挂当前 loop 后台跑，失败静默（合成路径自会流内自连）。
    构造签名与官方完全一致，装配点零漂移换类名即可。
    """

    def prewarm(self) -> None:
        instances = getattr(self, "_tts_instances", None) or []
        if not instances:
            return
        child_pw = getattr(instances[0], "prewarm", None)
        if child_pw is None:
            return
        try:
            result = child_pw()
        except Exception:  # noqa: BLE001 - 预热尽力而为,失败零影响
            return
        if not asyncio.iscoroutine(result):
            return
        try:
            # FIRE_FORGET_EXEMPT: 预热纯增益——被 GC 掐掉=首次合成就地握手回退。
            asyncio.get_running_loop().create_task(result)
        except RuntimeError:
            result.close()


def _minimax_prewarm_enabled() -> bool:
    """BOK_TTS_PREWARM 总闸（默认 "1"）："0"=prewarm() 立即 False + 池逻辑全旁路。"""
    return os.environ.get("BOK_TTS_PREWARM", "1") == "1"


def _minimax_pool_enabled() -> bool:
    return _minimax_prewarm_enabled() and os.environ.get("MINIMAX_WS_POOL", "1") == "1"


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
    """取热连接；无池/参数变/超龄/已闭 → None（调用方回退全新连接）。

    命中即打点 `MINIMAX_PREWARM hit=1` 并**立刻**后台补一条（池深 1）——补池
    不等本段合成收尾，下一轮（第 N+1 轮）同样免握手；陈旧弃置打点
    `MINIMAX_PREWARM stale=1`（握手期才发现服务端静默关闭的另一处见调用方）。
    """
    global _MINIMAX_POOL_WS, _MINIMAX_POOL_KEY, _MINIMAX_POOL_AT
    ws = _MINIMAX_POOL_WS
    pooled_key = _MINIMAX_POOL_KEY
    pooled_at = _MINIMAX_POOL_AT
    _MINIMAX_POOL_WS = None
    _MINIMAX_POOL_KEY = None
    _MINIMAX_POOL_AT = 0.0
    if ws is None:
        return None
    if pooled_key != (endpoint, key):  # 参数变(换 key/端点):配置性弃置,非陈旧
        _minimax_pool_discard(ws)
        return None
    if (time.monotonic() - pooled_at) > _MINIMAX_POOL_TTL_S:
        print("MINIMAX_PREWARM stale=1", flush=True)
        _minimax_pool_discard(ws)
        return None
    try:
        from websockets.protocol import State

        if getattr(ws, "state", State.OPEN) != State.OPEN:
            print("MINIMAX_PREWARM stale=1", flush=True)
            _minimax_pool_discard(ws)
            return None
    except Exception:  # noqa: BLE001 - 判不了状态就信任之（握手失败另有回退）
        pass
    print("MINIMAX_PREWARM hit=1", flush=True)
    _minimax_pool_schedule(endpoint, key)
    return ws


def _minimax_pool_ready(endpoint: str, key: str) -> bool:
    """池里是否有一条 (endpoint,key) 匹配、未超龄、OPEN 的暖连接（不取用）。

    不可用（参数变/超龄/已闭）即就地弃置——prewarm() 随后补一条新的；判不了
    状态就信任之（取用方握手失败另有弃池全新重连回退）。
    """
    global _MINIMAX_POOL_WS, _MINIMAX_POOL_KEY, _MINIMAX_POOL_AT
    ws = _MINIMAX_POOL_WS
    if ws is None:
        return False
    if _MINIMAX_POOL_KEY != (endpoint, key) or (
        time.monotonic() - _MINIMAX_POOL_AT
    ) > _MINIMAX_POOL_TTL_S:
        _MINIMAX_POOL_WS = None
        _MINIMAX_POOL_KEY = None
        _MINIMAX_POOL_AT = 0.0
        _minimax_pool_discard(ws)
        return False
    try:
        from websockets.protocol import State

        if getattr(ws, "state", State.OPEN) != State.OPEN:
            _MINIMAX_POOL_WS = None
            _MINIMAX_POOL_KEY = None
            _MINIMAX_POOL_AT = 0.0
            _minimax_pool_discard(ws)
            return False
    except Exception:  # noqa: BLE001 - 判不了状态就信任之
        pass
    return True


async def _minimax_pool_prewarm(endpoint: str, key: str) -> bool:
    """等一条暖连接入池（池深 1）：已有=True；在飞=共享同一任务（单飞防重复握手）。

    绝不 raise —— 补池任务自身吞异常，本函数只回报最终池态；无池/未配置=False。
    """
    global _MINIMAX_POOL_TASK
    if not _minimax_pool_enabled() or not key or not endpoint:
        return False
    if _minimax_pool_ready(endpoint, key):
        return True
    task = _MINIMAX_POOL_TASK
    if task is None or task.done():
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 调用点都在 loop 内
            return False
        task = loop.create_task(_minimax_pool_replenish(endpoint, key))
        _MINIMAX_POOL_TASK = task
    try:
        await task
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 补池任务自身已吞,双保险
        return False
    return _minimax_pool_ready(endpoint, key)


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
    except Exception:  # noqa: BLE001 - 预热尽力而为:补池失败静默(纯快路径,合成自会流内自连)
        pass
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


# ---- W8 首子句起播（2026-09-24，MiniMax/Qwen3 两家 TTS 共用） ----
# 首个 task_continue/首段 POST 是首音频的门。overlap 档门槛 12 字
# （MINIMAX_TTS_OVERLAP_CHARS）在慢生成轮（GPU 争用实测 tps 13-18，
# call-ff96795c 族 commit_to_audio 1.9-2.6s）把首送推后 ~0.7-0.9s。
# 首送走快车道：≥N 字（默认 6）即可送，且不要求「软停顿过半」——
# 短首子句（「好的，我帮您查」）的软停顿本来到不了半程；送出后
# 后续增量回 overlap 档原节奏，句子路径（。！？）照旧恒优先。
_TTS_SENT_END = "。！？!?"
_TTS_SOFT_BREAK = "，、；;：:"


def _tts_first_clause_config() -> tuple[bool, int]:
    """W8 首送快车道配置：(开关, 字数门槛)。纯函数，两家 TTS 共用。

    - ``BOK_TTS_FIRST_CLAUSE``（默认 "1"）：总闸，0=回纯 overlap 档。
    - ``BOK_TTS_FIRST_CLAUSE_CHARS``（默认 6，钳 ≥1）：首送门槛。
    - 前提仍是各家 overlap 开着（MINIMAX/QWEN3_TTS_OVERLAP=0 = 运营
      刻意回「整句才送」保守档，快车道不越权激活）。
    """
    on = os.environ.get("BOK_TTS_FIRST_CLAUSE", "1") == "1"
    try:
        chars = int(os.environ.get("BOK_TTS_FIRST_CLAUSE_CHARS", "6"))
    except ValueError:
        chars = 6
    return on, max(1, chars)


# ---- bidi 首 chunk 提前切（W-TTS 首音频,2026-09-28） ----------------------------
# 生产回复= bidi 持久连接,输入循环逐块原样透传、服务端攒句合成。首个 continue
# 若等整句标点才发(长首句),该句音频要等服务端攒够整句才开始合成 → 客户干等。
# 首个 continue 按字数提前切:≥N 字且句内无标点时先发一段(切点不落数字/拉丁
# run 内),后续 continue 照旧按句界对齐——只移动首块切点,韵律影响最小。
# ``BOK_TTS_FIRST_CHUNK_CHARS`` 缺省 "6"（2026-09-30 Ethan 耳测定档：AB 三臂
# 0/6/10 全 MiniMax bidi 实声对照，2_long_early6 最好、10 也不错——取 6 最快）;
# "0"=整段关闭(旧行为逐字节同)。
def _bidi_head_flush_enabled() -> bool:
    """头段催产 task_flush(2026-09-29,官方文档+直连台架定案)。

    MiniMax bidi 服务端只在句末标点(或攒够/兜底窗)才起合成——首 6-10 字早发
    的 continue 会被扣住(C 场景实测无标点兜底窗 2.4s),云嘴首声实际=首句句号
    到达+~240ms 地板(生产 637-1017ms 的构成)。官方 task_flush=已缓冲文本
    立即合成且会话不关;早切头段后立刻 flush,台架实测首声 918-962→210-343ms
    (压到服务端地板)。"0" 一键回退=只早发不催产(第八波原行为)。
    """
    return os.environ.get("MINIMAX_BIDI_HEAD_FLUSH", "1") == "1"


def _tts_first_chunk_chars() -> int:
    """首个 bidi continue 提前切门槛(字数)。0=关闭;坏值回默认 6。"""
    try:
        v = int(os.environ.get("BOK_TTS_FIRST_CHUNK_CHARS", "6"))
    except Exception:  # pragma: no cover - 配错回默认
        return 6
    return v if v > 0 else 0


# 保护 run 字符集:数字(ASCII/全角/中文数字词)与拉丁字母——这些连续串绝不能被
# 提前切点拦腰截断(号码/专名拆两段会在拼接边界停顿/重读)。放一起当同一类 run,
# 保守但安全(多延伸永远不伤语义)。
_FIRST_CHUNK_RUN_CHARS = frozenset(
    "0123456789"
    "０１２３４５６７８９"
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "〇零一二三四五六七八九俩两"
)


def _first_chunk_cut(text: str, min_chars: int) -> int | None:
    """首个 bidi continue 的提前切点 → 安全切点下标,或 None=走既有路径。

    纯函数(零依赖,单测可隔离)。返回 None 的情形:功能关闭(min_chars<=0)/
    空串/首个句末标点落在 min_chars+6 容差内(自然切点就近,按句路径优先,
    韵律更优)/去空白后不足 N 字/切点延伸后无剩余(整段都在保护 run 内,
    切了等于没切)。命中时返回的下标保证:text[:cut] 既不落在数字 run 内
    也不落在拉丁/数字词 run 内。

    注意(2026-09-28 A/B 实弹勘误):旧版「全文任何位置有句界就让位」会令
    58 字远处的一个句号压制 10 字早切——早切本就为长首句而生,远处句界
    不该挡;只有「句界就快自然到达」(N+6 内)才值得多等几个字换自然断点。
    """
    if min_chars <= 0 or not text:
        return None
    # 首个句末标点就近(min_chars+6 容差内)→ 按句路径的自然切点优先。
    b = _first_sentence_end(text)
    if b is not None and b <= min_chars + 6:
        return None
    if len(text.strip()) < min_chars:
        return None
    cut = min_chars
    if cut >= len(text):
        return None
    # IRON GUARD:切点若把同一 run 劈开(text[cut-1] 与 text[cut] 都是保护字符),
    # 延伸到该 run 结束(数字/拉丁词整段同行,延伸永远安全)。
    if (
        0 < cut < len(text)
        and text[cut - 1] in _FIRST_CHUNK_RUN_CHARS
        and text[cut] in _FIRST_CHUNK_RUN_CHARS
    ):
        while cut < len(text) and text[cut] in _FIRST_CHUNK_RUN_CHARS:
            cut += 1
    if cut >= len(text):
        return None
    if not text[:cut].strip():
        return None
    return cut


def _first_sentence_end(text: str) -> int | None:
    """首个句末标点之后的下标(含标点),无则 None。尾块按句界对齐用。"""
    best = -1
    for ch in _TTS_SENT_END:
        i = text.find(ch)
        if i != -1 and (best == -1 or i < best):
            best = i
    return best + 1 if best != -1 else None


def _tts_overlap_send_now(
    buf: str,
    *,
    sent_any: bool,
    overlap_on: bool,
    first_lane_on: bool,
    first_lane_chars: int,
    overlap_chars: int,
    time_up: bool,
) -> tuple[bool, bool]:
    """overlap 增量此刻是否送出 → (send_now, via_first_lane)。纯函数。

    与旧档的分别只在首送：门槛降为 first_lane_chars、软停顿过半门豁免。
    time_up 恒可送（旧档同语义）；数字/字母尾的拦腰保护（_flushable）
    与纯标点段检查由调用方把关（两家尾串处理同款，留在循环内）。
    """
    s = buf.strip()
    if not s or not overlap_on:
        return False, False
    first_lane = first_lane_on and not sent_any
    need = first_lane_chars if first_lane else overlap_chars
    if len(s) < need:
        return False, False
    soft_idx = -1
    for ch in _TTS_SOFT_BREAK:
        pos = s.rfind(ch)
        if pos != -1:
            soft_idx = max(soft_idx, pos)
    if soft_idx != -1 and (first_lane or soft_idx >= len(s) // 2):
        return True, first_lane
    if time_up:
        return True, first_lane
    return False, False


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

    def push_text(self, text: str = "", *args, **kwargs):
        # 框架文本入口统一过 _prep_outbound:非 2.8 备档实例剥自然度标记
        # (会话级 transform 门按主档判,FallbackAdapter 换档这层管不到)。
        return super().push_text(self._tts_._prep_outbound(str(text or "")), *args, **kwargs)

    async def _emit_beep(self, output_emitter):
        import math

        # beep 旗标(tts_cache tee 用):合成失败兜 beep 时置位,外层据此拒绝落盘——
        # 错误提示音一旦入缓存,该行文本之后永远播 beep。多类同款实现,统一置位。
        self._emitted_beep = True
        # W1a(2026-10-06):beep 自己先 initialize+start_segment(_qwen3_tts_beep
        # 同款修法)——旧行为 push 喺未启动 emitter 上抛 "AudioEmitter isn't
        # started" 被外层 except 吞掉,beep 从未播出且 _run 以零音频正常返回,
        # 框架收尾 end_input 再炸一次(生产崩形,见 _minimax_zero_audio_pad)。
        if _minimax_emitter_unstarted(output_emitter):
            try:
                output_emitter.initialize(
                    request_id="minimax-tts-beep",
                    sample_rate=self._tts_.sample_rate,
                    num_channels=self._tts_.num_channels,
                    mime_type="audio/pcm",
                    stream=True,
                )
                output_emitter.start_segment(segment_id="minimax-tts-beep")
            except Exception:  # noqa: BLE001 - 已启动竞态:照旧直推
                pass
        try:
            sr = self._tts_.sample_rate
            n = int(sr * 0.4)
            pcm = bytearray()
            for i in range(n):
                v = int(12000 * math.sin(2 * math.pi * 440 * i / sr))
                pcm += v.to_bytes(2, "little", signed=True)
            output_emitter.push(bytes(pcm))
            output_emitter.flush()
            output_emitter.end_segment()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - beep 尽力而为,唔好踩成新崩溃源
            print("MINIMAX_TTS_BEEP_FAIL", repr(exc), flush=True)

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
            # 请求级发音词典(空=不加键),与 bidi/HTTP 三路同单点。
            self._tts_._apply_pronunciation(start)
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
                print("MINIMAX_PREWARM stale=1", flush=True)
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

            def _note_first_send(s: str = "") -> None:
                nonlocal t_first_text
                if t_first_text == 0.0:
                    t_first_text = time.monotonic()
                    # 官方 SynthesizeStream 契约(同 bidi _send_text):首段文本交
                    # provider 时 _mark_started()——漏调则基座 metrics 监视器因
                    # _started_time==0 永不 emit,本流 tts_metrics 整条哑。
                    self._mark_started()
                    # W8 A/B 秒表(两腿共通):首个 task_continue 的时刻与字数。
                    # 首送是首音频的门;该读数=「流启动→首送」纯文本等待面,
                    # 与 cloud RTT(TTS_FIRST_AUDIO_MS)/watchdog 收割解耦。
                    print(
                        f"MINIMAX_TTS_FIRST_SEND_MS {(t_first_text - t0) * 1000:.0f} chars={len(s)}",
                        flush=True,
                    )

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
            _first_lane_on, _first_lane_chars = _tts_first_clause_config()
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
                        _note_first_send(s)
                        if _reconnecting.is_set():
                            await _reconnecting.wait()
                        await ws.send(
                            json.dumps({"event": "task_continue", "text": lecture_canned(self._tts_._speech_lang())})
                        )
                        sent_any = True
                    return
                if is_lecture_text(s):
                    return  # 已触发过,课程延续句照丢
                _note_first_send(s)
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
                        (sent_buf.find(ch) for ch in _TTS_SENT_END if sent_buf.find(ch) != -1),
                        default=-1,
                    )
                    if idx == -1:
                        break
                    sentence = sent_buf[: idx + 1]
                    sent_buf = sent_buf[idx + 1 :]
                    if sentence.strip():
                        await _send_text(sentence.strip())
                # overlap:句号之间的增量,满足「≥N 字且有软停顿/距上次够久」就提前送。
                # 首送走 W8 快车道(≥6 字即可、软停顿过半门豁免);后续回 overlap 档。
                if overlap_on and not self._lecture_fired and sent_buf.strip():
                    send_now, via_first = _tts_overlap_send_now(
                        sent_buf,
                        sent_any=sent_any,
                        overlap_on=overlap_on,
                        first_lane_on=_first_lane_on,
                        first_lane_chars=_first_lane_chars,
                        overlap_chars=_overlap_chars,
                        time_up=(time.monotonic() - _last_send) * 1000 >= _overlap_ms,
                    )
                    if send_now:
                        frag = sent_buf.strip()
                        if _flushable(frag):
                            if via_first:
                                print(f"MINIMAX_TTS_FIRST_CLAUSE chars={len(frag)}", flush=True)
                            await _send_text(frag)
                            sent_buf = ""
                if self._lecture_fired:
                    sent_buf = ""  # 已触发 → 清掉未分句的课程尾部
            if sent_buf.strip():
                if not self._lecture_fired and is_lecture_text(sent_buf.strip()):
                    self._lecture_fired = True
                    if not sent_any:
                        _note_first_send(sent_buf.strip())
                        if _reconnecting.is_set():
                            await _reconnecting.wait()
                        await ws.send(
                            json.dumps({"event": "task_continue", "text": lecture_canned(self._tts_._speech_lang())})
                        )
                elif not self._lecture_fired:
                    _note_first_send(sent_buf.strip())
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
            # W1a(2026-10-06):零音频正常收尾(服务端哑火/连接死亡)垫 ~20ms 静音
            # 把 emitter 正常启动——防框架 SynthesizeStream._main_task 收尾
            # end_input() 在未启动 emitter 上炸 RuntimeError 经 __anext__ 上抛 →
            # FallbackAdapter 误切 backup(崩形与修法见 _minimax_zero_audio_pad)。
            if not init_done:
                await _minimax_zero_audio_pad(self._tts_, output_emitter, stream=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("MINIMAX_TTS_WS_ERR", repr(exc), flush=True)
            # W1a:吞异常收尾同样唔可以零音频返回(同上,防框架 end_input 炸)。
            if not init_done:
                await _minimax_zero_audio_pad(self._tts_, output_emitter, stream=True)
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
# ---- F-10(2026-09-23 生产就绪修复波):bidi 限流守卫 ----
# 官方 t2a_v2_bidi 指引:task_failed 事件必须关闭连接并处理错误。实弹(T3 报告
# F-10 Blocker):1002(RPM 限流)呼叫开局突发即触后,适配器不关连接、不退避、
# 不回落——死会话上继续 task_continue → 794 行「no audio frames were pushed」,
# 且失败重试自我维持限流(同通后续轮全灭)。
_MINIMAX_BIDI_RATE_LIMIT_STATUSES = frozenset({1002, 1039, 2205})
# 指数退避重试序列(1039/2205 task_failed 用;1002 首击即回落 HTTP 不重试)。
_MINIMAX_BIDI_RATE_LIMIT_RETRY_DELAYS = (1.0, 2.0)
# 同通连续 N 轮限流 → 熔断:本通剩余轮直接 HTTP 不再碰 WS(防重试风暴)。
_MINIMAX_BIDI_RATE_LIMIT_MAX_STREAK = 3


def _bidi_guard_enabled() -> bool:
    """F-10 kill-switch:BOK_MINIMAX_BIDI_GUARD=0 回旧行为(不关连接不回落)。"""
    return os.environ.get("BOK_MINIMAX_BIDI_GUARD", "1") == "1"


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
        # F-10 限流熔断计数:本通(=本 job/本 TTS 实例)连续限流轮数。限流轮 +1,
        # 任何一轮 WS 成功出声归零;达 _MINIMAX_BIDI_RATE_LIMIT_MAX_STREAK →
        # 后续轮跳过 WS 直接 HTTP(BOK_MINIMAX_BIDI_GUARD=0 整闸回旧行为)。
        self.rate_limit_streak = 0

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

    async def prewarm_wait(self) -> bool:
        """（W-TTS 冻结契约）等本次预热落地并返回连接是否可用。

        已连且参数指纹一致=True（零握手）;在飞预热=共享同一任务（单飞,不会重复
        握手）;否则拉起 prewarm() 再等。自身异常已被 _prewarm_run 吞（绝不上抛）
        ——失败=False,首个真实轮 ensure_ready 自会流内连接,行为同旧。
        """
        params = self._tts_._bidi_params_key()
        if self._alive() and self._params == params:
            return True
        task = self._prewarm_task
        if task is None or task.done():
            self.prewarm()
            task = self._prewarm_task
        if task is not None and not task.done():
            try:
                await task
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 预热任务自身已吞,双保险
                pass
        return bool(self._alive() and self._params == params)

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
        # F-10 限流守卫态(本轮内):收到限流族故障 / 最近一次故障码 / 首包信号
        # (WS 退避重试后,是否出声由 recv_loop 在首推时置)。
        self._rate_limited = False
        self._last_rl_status = 0
        self._first_audio_evt = asyncio.Event()
        # orch2-C 中途死亡可见性:连接死亡时音频已开始(=客户听到截断回复)置真,
        # 收尾 PERF 行据此带 truncated_mid_reply=1(否则死亡在日志/账本零痕)。
        self._truncated_mid_reply = False

    def push_text(self, text: str = "", *args, **kwargs):
        # 同 _MiniMaxSynthesizeStream.push_text:非 2.8 档实例剥自然度标记
        # (会话级 transform 门按主档判,FallbackAdapter 换档这层管不到)。
        return super().push_text(self._tts_._prep_outbound(str(text or "")), *args, **kwargs)

    async def _emit_beep(self, output_emitter):
        import math

        # beep 旗标(tts_cache tee 用):合成失败兜 beep 时置位,外层据此拒绝落盘——
        # 错误提示音一旦入缓存,该行文本之后永远播 beep。多类同款实现,统一置位。
        self._emitted_beep = True
        # W1a(2026-10-06):beep 自己先 initialize+start_segment(_qwen3_tts_beep
        # 同款修法)——旧行为 push 喺未启动 emitter 上抛 "AudioEmitter isn't
        # started" 被外层 except 吞掉,beep 从未播出且 _run 以零音频正常返回,
        # 框架收尾 end_input 再炸一次(生产崩形,见 _minimax_zero_audio_pad)。
        if _minimax_emitter_unstarted(output_emitter):
            try:
                output_emitter.initialize(
                    request_id="minimax-tts-beep",
                    sample_rate=self._tts_.sample_rate,
                    num_channels=self._tts_.num_channels,
                    mime_type="audio/pcm",
                    stream=True,
                )
                output_emitter.start_segment(segment_id="minimax-tts-beep")
            except Exception:  # noqa: BLE001 - 已启动竞态:照旧直推
                pass
        try:
            sr = self._tts_.sample_rate
            n = int(sr * 0.4)
            pcm = bytearray()
            for i in range(n):
                v = int(12000 * math.sin(2 * math.pi * 440 * i / sr))
                pcm += v.to_bytes(2, "little", signed=True)
            output_emitter.push(bytes(pcm))
            output_emitter.flush()
            output_emitter.end_segment()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - beep 尽力而为,唔好踩成新崩溃源
            print("MINIMAX_TTS_BEEP_FAIL", repr(exc), flush=True)

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
            # F-10 限流熔断(2026-09-23):本通连续限流达上限 → 本轮跳过 WS,文本
            # 只记账、收尾直接 HTTP(防「重试风暴自我维持限流」——同通后续轮
            # 撞同一线)。BOK_MINIMAX_BIDI_GUARD=0 整闸回旧行为。
            circuit_open = (
                _bidi_guard_enabled()
                and session.rate_limit_streak >= _MINIMAX_BIDI_RATE_LIMIT_MAX_STREAK
            )
            if circuit_open:
                print(
                    f"MINIMAX_BIDI_CIRCUIT_OPEN streak={session.rate_limit_streak} "
                    "— 本轮直接 HTTP 合成,不碰 WS",
                    flush=True,
                )
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
                # orch2-C:本回复曾向 emitter 推过音频(粘性)——stall 自愈重连会把
                # first_pushed 复位(新一轮首包含义),但已出声的历史唔可以丢。
                "audio_ever": False,
                "t_first_continue": 0.0,
                "t_last_audio": 0.0,
                "stale_msgs": 0,
                "stale_bytes": 0,
                "sentences": 0,
                "stall_heals": 0,
            }

            def _note_conn_died(reason: str, exc: BaseException | None = None) -> None:
                """orch2-C 连接死亡可见性:音频已开始(=客户听到截断回复)置截断旗,
                无论首包前后都打一行标记(first_audio=0|1)——死亡唔再零痕。"""
                mid = bool(state.get("audio_ever"))
                if mid:
                    self._truncated_mid_reply = True
                print(
                    f"MINIMAX_TTS_BIDI_DIED_MID_REPLY first_audio={int(mid)} "
                    f"reason={reason} sentences={state['sentences']} epoch={my_epoch}"
                    + (f" err={repr(exc)[:120]}" if exc is not None else ""),
                    flush=True,
                )

            t_flush = 0.0
            # 重连窗口发送闸(看门狗换连接期间):输入循环若继续 task_continue 会发到
            # 已弃旧 ws → 整轮音频丢失。所有发送点先 is_set() 再 wait()(Event.wait()
            # 对未 set 事件挂起,无条件 await 会把正常轮首句卡到重连后,classic 同款)。
            # 闸亮时间=重连 ~0.2-0.65s,期间文本排队唔丢唔乱序:闸清前已 append 进
            # _sent_text_parts 的由看门狗合并重发覆盖,闸清后照常直发新连接。
            _reconnecting = asyncio.Event()
            try:
                if circuit_open:
                    # 熔断轮:零握手段,文本由 _send_text 记账、收尾 HTTP 直落。
                    reused = False
                    connect_ms = 0.0
                else:
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
                        except Exception as exc:
                            # 连接死亡(2201/网络):标记死连接 + 解锁等待方;
                            # 死亡即后台重预热,下个真实轮零冷启动
                            _note_conn_died("recv-exc", exc)
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
                                    f"first_chunk={state.get('first_chunk') or 'sentence'} "
                                    f"chars={state.get('first_chunk_chars', 0)} "
                                    f"total_ms={(t_first - t0) * 1000:.0f}",
                                    flush=True,
                                )
                                output_emitter.push(bytes(buf))
                                output_emitter.flush()
                                buf.clear()
                                state["first_pushed"] = True
                                state["audio_ever"] = True  # orch2-C:粘性中途死亡判据
                                self._first_audio_evt.set()  # F-10:WS 退避重试的成功信号
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
                            state["audio_ever"] = True  # orch2-C:尾块推清也算已出声
                        if event == "task_flushed":
                            if state.get("head_flush_pending"):
                                # 头段催产 flush 的 ack(非收尾):唔收摊,recv 继续
                                # 30s 等待窗照常吃余句音频。流已收尾(头段=整条
                                # 回复)时,这次 ack 同时兼任收尾 ack。
                                state["head_flush_pending"] = False
                                if state.get("stream_ended"):
                                    self._flushed_evt.set()
                            else:
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
                        # F-10 限流守卫(2026-09-23):官方指引 task_failed 必须关连接
                        # 并处理错误。1002=RPM/1039=TPM/2205=请求超限族 → 关当前
                        # WS 弃会话,由收尾段退避重试或回落 HTTP(非限流族维持现状
                        # 记日志)。注意 2205 双形态:非 task_failed 事件携带的 2205
                        # 仍是软背压,保留既有原样重发路径(唔好重连),唔入守卫。
                        rl_hit = status in _MINIMAX_BIDI_RATE_LIMIT_STATUSES
                        if rl_hit and status == 2205 and event != "task_failed":
                            rl_hit = False
                        if rl_hit and _bidi_guard_enabled():
                            print(
                                f"MINIMAX_BIDI_RATE_LIMIT status={status} "
                                f"event={event or '-'} first_pushed={int(state['first_pushed'])} "
                                f"streak={session.rate_limit_streak}",
                                flush=True,
                            )
                            self._last_rl_status = status
                            if not state.get("rl_counted"):
                                state["rl_counted"] = True
                                session.rate_limit_streak += 1
                            if state["first_pushed"]:
                                # 已出过音频:只弃毒化连接(死会话继续 continue=零帧
                                # 根因),本轮已推音频照常收尾,唔重播唔回落。
                                await session.invalidate()
                                self._flushed_evt.set()
                                self._canceled_evt.set()
                                return
                            self._rate_limited = True
                            if stall_task is not None:
                                stall_task.cancel()  # 守卫接管,看门狗唔好抢着重连
                            await session.invalidate()
                            # 本流不再有音频:置 flush 旗标让各等待方即时收摊
                            # (守卫收尾走 HTTP 时跳过 flush 路径;竞态窗口若外层
                            # 已选了 flush 路径,也唔好干等 15s)。
                            self._flushed_evt.set()
                            self._canceled_evt.set()
                            return
                        if status == 2205:
                            self._resend_evt.set()  # 软背压:重发协程稍后原样重发
                        elif status == 2204:
                            print("MINIMAX_TTS_BIDI_2204_TEXT_SKIPPED", flush=True)
                        elif status in (2201, 2206):
                            print(f"MINIMAX_TTS_BIDI_{status}", flush=True)
                            _note_conn_died(f"status-{status}")
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

                if not circuit_open:
                    _start_loops()  # 熔断轮:无连接,收发协程不起

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
                        self._mark_started()  # 官方契约:文本交 provider(幂等;见 _send_text 注释)
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

                def _guard_triggered() -> bool:
                    """F-10 守卫触发态(C-1,2026-09-23):限流已弃会话——此后 LLM
                    仍在流式上产的 chunk 只准记账,再 send 必抛 ConnectionClosed
                    且被外层 except 吞掉=_guard_finalize 整段旁路(退避重试+HTTP
                    回落全跳过)→ 该轮零音频零 beep 静默。
                    只认守卫自身旗标 `_rate_limited`:flushed/canceled 也会被
                    既有死亡路径与 stall 看门狗自愈场景置位/残留(见 I-1),拿它们
                    当判据会把自愈后健康连接上的 chunk 误转记账(实测回归)。"""
                    return self._rate_limited

                async def _send_text(s: str) -> None:
                    if not self._lecture_fired and is_lecture_text(s):
                        # 开场即教学 → 播一次罐头的「请再报单号」,唔好照读课程;
                        # 若前面已出过正常音频,课程句静默丢弃,唔追加罐头(避免二重声)。
                        self._lecture_fired = True
                        if not state["sent_any"]:
                            canned = lecture_canned(self._tts_._speech_lang())
                            if circuit_open or _guard_triggered():
                                # F-10 熔断/守卫已触发:文本只记账(收尾合并重发/
                                # HTTP 合成用),绝不碰 WS。
                                self._sent_text_parts.append(canned)
                                return
                            if _reconnecting.is_set():
                                await _reconnecting.wait()
                            self._last_continue = canned
                            self._sent_text_parts.append(canned)  # 看门狗重连合并重发用
                            if state["t_first_continue"] == 0.0:
                                state["t_first_continue"] = time.monotonic()
                                # 官方契约:首段文本交 provider(见 _send_text 注释)
                                self._mark_started()
                                _arm_stall_watch()  # 首条 task_continue 起看门狗计时
                            await ws.send(json.dumps({"event": "task_continue", "text": canned}))
                            state["sent_any"] = True
                            session.active_epoch = my_epoch  # 认领纪元:此后残留门禁对本流放行
                        return
                    if is_lecture_text(s):
                        return  # 已触发过,课程延续句照丢
                    if circuit_open or _guard_triggered():
                        # F-10 熔断/守卫已触发(2026-09-23 C-1):后续 chunk 只记账
                        # ——自然并入 finalize 的合并重发/HTTP 回落文本。
                        self._sent_text_parts.append(s)
                        return
                    # bidi:逐块原样透传,唔切句——服务端自己按标点/长度切句合成。
                    if _reconnecting.is_set():
                        await _reconnecting.wait()
                    if state["t_first_continue"] == 0.0:
                        state["t_first_continue"] = time.monotonic()
                        # 官方 SynthesizeStream 契约(livekit 1.8.2 tts.py
                        # _emit_metrics):基座 metrics 监视器以 _started_time 为闸,
                        # 而 _started_time 只由子类在「首段文本交给 provider」时调
                        # _mark_started() 设置(官方 stream_adapter.py:132 /
                        # inference/tts.py:690 同款)——漏调=本流 tts_metrics 永不
                        # emit(CP Provider 卡 TTS 行灰)。锚点=首条 task_continue
                        # 发出,ttfb=首送→首帧墙钟,与 TTS_FIRST_AUDIO_MS 同口径。
                        self._mark_started()
                        _arm_stall_watch()  # 首条 task_continue 起看门狗计时
                    self._last_continue = s
                    self._sent_text_parts.append(s)  # 看门狗重连合并重发用
                    try:
                        await ws.send(json.dumps({"event": "task_continue", "text": s}))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # 守卫竞态兜底(C-1):检查与 send 之间连接被收侧判死——
                        # 同款记账,异常绝不抛出输入循环(外层 except 会吞掉守卫
                        # 收尾=整轮静默);文本由 finalize 合并重发/HTTP 回落承接。
                        return
                    state["sent_any"] = True
                    session.active_epoch = my_epoch  # 认领纪元:此后残留门禁对本流放行

                # W-TTS 首 chunk 提前切:仅本流(=本回复)首个 continue 可句内提前
                # 送(≥N 字),此后回按句界对齐(尾块攒到句界或收尾才发)。env "0"
                # 或不足 N 字时逐块原样透传=旧行为逐字节同。
                first_chunk_chars = _tts_first_chunk_chars()
                first_chunk_done = False
                first_tail = ""
                async for item in self._input_ch:
                    if isinstance(item, self._FlushSentinel):
                        continue
                    text = str(item or "")
                    if not text.strip():
                        continue
                    if first_tail:
                        # 首块切走后的尾块:攒到句界一次发(后续 continue 按句对齐)。
                        first_tail += text
                        seg_end = _first_sentence_end(first_tail)
                        if seg_end is not None:
                            seg, first_tail = first_tail[:seg_end], first_tail[seg_end:]
                            if seg.strip():
                                await _send_text(seg)
                        continue
                    if not first_chunk_done:
                        first_chunk_done = True
                        if first_chunk_chars > 0 and not is_lecture_text(text):
                            cut = _first_chunk_cut(text, first_chunk_chars)
                            if cut is not None:
                                state["first_chunk"] = "early"
                                state["first_chunk_chars"] = len(text[:cut].strip())
                                print(
                                    f"MINIMAX_TTS_BIDI_FIRST_CHUNK early "
                                    f"chars={state['first_chunk_chars']} cut={cut}",
                                    flush=True,
                                )
                                await _send_text(text[:cut])
                                # 头段催产(2026-09-29):服务端对无句末标点的缓冲
                                # 不起合成(兜底窗 2.4s),早发的头段要 task_flush
                                # 催一声才有声——台架 918-962→210-343ms。ack 在
                                # recv 循环按 head_flush_pending 区分,唔收摊。
                                if (
                                    _bidi_head_flush_enabled()
                                    and not circuit_open
                                    and not _guard_triggered()
                                ):
                                    try:
                                        if _reconnecting.is_set():
                                            await _reconnecting.wait()
                                        state["head_flush_pending"] = True
                                        await ws.send(json.dumps({"event": "task_flush"}))
                                        print(
                                            f"MINIMAX_BIDI_HEAD_FLUSH sent "
                                            f"chars={state['first_chunk_chars']}",
                                            flush=True,
                                        )
                                    except asyncio.CancelledError:
                                        state["head_flush_pending"] = False
                                        raise
                                    except Exception:
                                        # 发唔出去=无事发生(余句句号到自然起合成)
                                        state["head_flush_pending"] = False
                                if text[cut:].strip():
                                    first_tail = text[cut:]
                                continue
                        state["first_chunk"] = "sentence"
                        state["first_chunk_chars"] = len(text.strip())
                    await _send_text(text)
                if first_tail.strip():
                    # 回复结束尾块未攒到句界:收尾前补发(唔可以丢——task_flush 只
                    # 吐服务端已收到的文本)。
                    await _send_text(first_tail)
                    first_tail = ""
                # 输入已尽(头段催产 ack 与收尾 ack 可能是同一发,recv 侧靠此标记判)
                state["stream_ended"] = True

                # ---- F-10 限流守卫收尾(2026-09-23):触发限流(或熔断轮)且零音频
                # → 退避重试 WS(1039/2205)或回落既有 HTTP 路径(1002 首击/熔断/
                # 重试耗尽)。返回 True=HTTP 已推完整音频,外层跳过 task_flush 路径。
                async def _guard_finalize() -> bool:
                    nonlocal ws, reused, connect_ms
                    if not self._sent_text_parts:
                        # 无文本可说(罕见:未发 continue 就被限流):跳过 flush 等待。
                        self._flushed_evt.set()
                        self._canceled_evt.set()
                        return True
                    if stall_task is not None:
                        stall_task.cancel()  # 守卫接管,看门狗唔好抢着重连撞同一线
                    for task in (recv_task, resend_task):
                        if task:
                            task.cancel()
                    self._resend_evt.clear()
                    status = self._last_rl_status
                    # ① WS 退避重试:仅 1039/2205(1002 RPM 首击即回落——退避 1-2s
                    # 撞同一条限流窗口只会白烧预算);熔断轮连试都唔试。
                    if not circuit_open and status != 1002:
                        for delay in _MINIMAX_BIDI_RATE_LIMIT_RETRY_DELAYS:
                            await asyncio.sleep(delay)
                            try:
                                # 每次重试都全新会话:上一发重试的会话服务端可能已
                                # 积累同文,复用会造成恢复后重复播两遍。
                                await session.invalidate()
                                ws = await session.ensure_ready()
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                print(f"MINIMAX_BIDI_RATE_LIMIT_RETRY_FAIL {exc!r}", flush=True)
                                continue
                            reused = session.last_reused
                            connect_ms = session.last_connect_ms
                            merged = "".join(self._sent_text_parts)[:10000]  # 官方单条 ≤10k
                            self._rate_limited = False
                            self._first_audio_evt.clear()
                            # I-1(2026-09-23):守卫分支已置 flushed/canceled——
                            # 唔清掉,新连接 recv 窗按「flushed 后 0.5s 排干」跑,
                            # 首包/句隙 >0.5s 即误判收摊(重试恒失败落 HTTP,
                            # flush 等待也立即返回截尾)。全新会话=全新收摊语义。
                            self._flushed_evt.clear()
                            self._canceled_evt.clear()
                            if merged:
                                self._mark_started()  # 官方契约:文本交 provider(守卫重发可能正是首送)
                                await ws.send(json.dumps({"event": "task_continue", "text": merged}))
                                self._last_continue = merged
                            session.active_epoch = my_epoch
                            _start_loops()  # 新连接新收发协程(ws 已重绑进闭包)
                            print(
                                f"MINIMAX_BIDI_RATE_LIMIT_RETRY delay={delay}s "
                                f"chars={len(merged)}", flush=True,
                            )
                            try:
                                await asyncio.wait_for(self._first_audio_evt.wait(), timeout=3.0)
                                session.rate_limit_streak = 0  # 出声=恢复,连续限流断链
                                print("MINIMAX_BIDI_RATE_LIMIT_RECOVERED", flush=True)
                                return False  # WS 救返:外层照常 flush 收尾
                            except asyncio.TimeoutError:
                                pass  # 这档重试没救回:下一档退避(或 HTTP)
                    # ② HTTP 回落:既有 classic HTTP 合成路径(同 key/voice/模型档),
                    # 文本=本流已发全部合并(截 10k,与看门狗重发同上限)。
                    # 弃当前 WS 会话:最后一次重试的会话可能已积累同文未 flush,
                    # 复用会让下一轮 task_continue 叠加文本=重复播两遍。
                    await session.invalidate()
                    fallback_text = "".join(self._sent_text_parts)[:10000]
                    print(
                        f"MINIMAX_BIDI_RATE_LIMIT_FALLBACK_HTTP status={status} "
                        f"circuit={int(circuit_open)} chars={len(fallback_text)}", flush=True,
                    )
                    self._mark_started()  # 官方契约:文本已交 HTTP provider(熔断轮零 WS 的首送)
                    ok = await _minimax_http_synth(
                        self._tts_, fallback_text, output_emitter,
                        key=key, voice=voice, sample_rate=sample_rate, stream_mode=True,
                    )
                    if not ok:
                        await self._emit_beep(output_emitter)
                    return True

                http_done = False
                if _bidi_guard_enabled() and not state["first_pushed"]:
                    # 让在飞 recv 协程先跑一步(M-3:仅守卫启用档加窗,guard=0
                    # 逐字节回旧):限流消息与输入排空并发到达时(纯排空无 yield
                    # 的窄窗口),先取到守卫判定再决策。
                    await asyncio.sleep(0.01)
                    if self._rate_limited or circuit_open:
                        http_done = await _guard_finalize()
                if (
                    state["first_pushed"]
                    and not http_done
                    and not self._rate_limited
                    and session.rate_limit_streak
                ):
                    session.rate_limit_streak = 0  # WS 正常出声=连续限流断链(guard=0 恒 0)

                # 文本结束:task_flush 强制吐出无标点尾巴,会话唔结束(连接保留)。
                if not http_done:
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
                    # orch2-C:中途死亡(音频已开始)时补截断旗,否则 PERF 行与
                    # 正常收线无法区分(死亡分支已置 flushed,这里照常走到)。
                    dead = " truncated_mid_reply=1" if self._truncated_mid_reply else ""
                    if state["t_last_audio"] > 0.0 and state["t_first_continue"] > 0.0:
                        print(
                            f"MINIMAX_BIDI_PERF sentences={state['sentences']} "
                            f"canceled={int(self._canceled_evt.is_set())} "
                            f"first_audio_ms="
                            f"{(state['t_last_audio'] - state['t_first_continue']) * 1000:.0f}"
                            f"{dead}",
                            flush=True,
                        )
                    else:
                        print(
                            f"MINIMAX_BIDI_PERF sentences=0 "
                            f"canceled={int(self._canceled_evt.is_set())} (no audio this turn)"
                            f"{dead}",
                            flush=True,
                        )

                # W1a(2026-10-06):零音频正常收尾(服务端哑火/连接死亡/守卫无文本/
                # 打断竞态下输入通道先闭)垫 ~20ms 静音把 emitter 正常启动——防框架
                # SynthesizeStream._main_task 收尾 end_input() 在未启动 emitter 上
                # 炸 RuntimeError 经 __anext__ 上抛 → FallbackAdapter 误切 backup
                #(崩形与修法见 _minimax_zero_audio_pad)。任务已被 cancel 时走
                # CancelledError 分支 raise(消费者本就见 StopAsyncIteration),唔经此。
                if not init_done:
                    await _minimax_zero_audio_pad(self._tts_, output_emitter, stream=True)
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
                # pushed=D1 判别字段(2026-09-30)：本流是否向服务端推过文本——
                # 0=文本从未到 TTS(调度/转发层悬死),1=推过但零响应(连接/服务端)。
                print(
                    f"MINIMAX_BIDI_PERF sentences={state['sentences']} "
                    f"canceled={int(self._canceled_evt.is_set())} "
                    f"pushed={int(bool(state.get('sent_any')))} (interrupted)",
                    flush=True,
                )
                # 保持 re-raise:任务以 cancelled 收场时框架 __anext__ 走
                # task.cancelled() 分支回 StopAsyncIteration=消费者视角干净收尾,
                # 绝唔触框架 end_input();吞掉反而令 _run「正常返回」+零音频=崩形本体。
                raise
            except Exception as exc:
                print("MINIMAX_TTS_BIDI_ERR", repr(exc), flush=True)
                # W1a:吞异常收尾同样唔可以零音频返回(同上,防框架 end_input 炸)。
                if not init_done:
                    await _minimax_zero_audio_pad(self._tts_, output_emitter, stream=True)
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


async def _minimax_http_synth(
    tts_: "MiniMaxTTS",
    text: str,
    output_emitter,
    *,
    key: str,
    voice: str,
    sample_rate: int,
    stream_mode: bool = False,
) -> bool:
    """HTTP 整段合成核心(F-10,2026-09-23 自 classic `_run_http` 抽出)。

    classic ChunkedStream(WS 失败兜底)与 bidi 限流回落共用同一条路径;返回
    True=已推完整音频。stream_mode=False=classic emitter 口径(initialize
    stream=False,无 segment);True=bidi SynthesizeStream 口径(initialize
    stream=True + start/end_segment)。重试/日志与原 `_run_http` 逐字节同。
    """
    endpoint = tts_._endpoint()
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                payload = {
                    "model": tts_._model(),
                    "text": _inject_pauses(text),
                    "voice_setting": tts_._ws_voice_setting(voice),
                    "audio_setting": {"sample_rate": sample_rate, "format": "pcm", "channel": 1},
                }
                # language_boost 与 WS 路径同源(env 注入,空则完全不带该键)。
                boost = tts_._language_boost()
                if boost:
                    payload["language_boost"] = boost
                # 请求级发音词典(空=不加键):HTTP 整段兜底路径与 WS 同份语义。
                tts_._apply_pronunciation(payload)
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
                    num_channels=tts_.num_channels,
                    mime_type="audio/pcm",
                    stream=stream_mode,
                )
                if stream_mode:
                    output_emitter.start_segment(segment_id=utils.shortuuid())
                output_emitter.push(pcm)
                print("MINIMAX_TTS_BYTES", len(pcm), flush=True)
                if stream_mode:
                    output_emitter.end_segment()
                return True
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            print("MINIMAX_TTS_RETRY", attempt + 1, repr(exc), flush=True)
            await asyncio.sleep(0.5 * (attempt + 1))
    print("MINIMAX_TTS_ERROR", repr(last_exc), flush=True)
    return False


def _minimax_emitter_unstarted(output_emitter) -> bool:
    """AudioEmitter 是否仍未 initialize（W1a,2026-10-06）。

    框架 SynthesizeStream._main_task 喺 `_run` 正常返回后**无条件**调
    ``output_emitter.end_input()``（livekit-agents 1.8.2 tts.py:601），emitter
    未启动就抛 RuntimeError("AudioEmitter isn't started")（tts.py:1008-1010）。
    `_started` 系框架私有态，此处只读判断；判不了（属性缺席）按已启动处理
    （保守：宁可唔补也绝不二次 initialize 撞 "already started"）。
    """
    return not bool(getattr(output_emitter, "_started", True))


async def _minimax_zero_audio_pad(tts_: "MiniMaxTTS", output_emitter, *, stream: bool) -> None:
    """零音频兜底段（W1a,2026-10-06）：首音频尚未产出即收尾时垫 ~20ms 静音。

    生产崩形（2026-09-22 ×342 / 09-23 ×912 / 10-06 ×9）：打断/服务端哑火/连接
    死亡（2201、recv-exc）令流喺「零音频」下**正常返回**→ 框架 end_input() 喺
    未启动 emitter 上炸 → 异常经 SynthesizeStream.__anext__ 上抛 →
    FallbackAdapter 误判主档死亡「switching to next TTS」→ backup 冷连
    （实测 ws_connect_ms≈2468）= 4-7s 黑窗，主档被无谓下线。
    修法=收尾前把 emitter 正常启动（initialize + 静音段）——workaround：框架
    锁版 1.8.2 唔改，官方无「跳过 end_input」开关。打点 MINIMAX_TTS_ZERO_AUDIO_PAD
    供观测。已启动则跳过；全程尽力而为，绝不 raise。
    """
    if not _minimax_emitter_unstarted(output_emitter):
        return
    try:
        print("MINIMAX_TTS_ZERO_AUDIO_PAD", flush=True)
        output_emitter.initialize(
            request_id="minimax-tts-zero-audio-pad",
            sample_rate=tts_.sample_rate,
            num_channels=tts_.num_channels,
            mime_type="audio/pcm",
            stream=stream,
        )
        if stream:
            output_emitter.start_segment(segment_id="minimax-tts-zero-audio-pad")
        n = max(1, int(tts_.sample_rate * 0.02))
        output_emitter.push(bytes(n * tts_.num_channels * 2))
        output_emitter.flush()
        if stream:
            output_emitter.end_segment()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 兜底段尽力而为,唔好踩成新崩溃源
        print("MINIMAX_TTS_ZERO_AUDIO_PAD_FAIL", repr(exc), flush=True)


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
            # 请求级发音词典(空=不加键),与 bidi/HTTP 三路同单点。
            self._tts_._apply_pronunciation(start)
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
                print("MINIMAX_PREWARM stale=1", flush=True)
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
        """HTTP 整段合成(WS 不可用时的降级)。核心抽至 `_minimax_http_synth`
        (F-10:bidi 限流回落共用同一条路径),失败兜 beep 语义不变。"""
        ok = await _minimax_http_synth(
            self._tts_, self._text, output_emitter,
            key=key, voice=voice, sample_rate=sample_rate,
        )
        if not ok:
            await self._emit_beep(output_emitter)

    async def _emit_beep(self, output_emitter):
        import math

        # beep 旗标(tts_cache tee 用):合成失败兜 beep 时置位,外层据此拒绝落盘——
        # 错误提示音一旦入缓存,该行文本之后永远播 beep。多类同款实现,统一置位。
        self._emitted_beep = True
        # W1a(2026-10-06):beep 自己先 initialize(_qwen3_tts_beep 同款修法)——
        # 旧行为 push 喺未启动 emitter 上抛 "AudioEmitter isn't started" 被外层
        # except 吞掉,beep 从未播出且 _run 以零音频正常返回,框架 ChunkedStream.
        # _main_task 收尾 end_input 再炸一次(生产崩形,见 _minimax_zero_audio_pad)。
        # ChunkedStream 口径 stream=False(无 segment,同 _minimax_http_synth)。
        if _minimax_emitter_unstarted(output_emitter):
            try:
                output_emitter.initialize(
                    request_id="minimax-tts-beep",
                    sample_rate=self._tts_.sample_rate,
                    num_channels=self._tts_.num_channels,
                    mime_type="audio/pcm",
                    stream=False,
                )
            except Exception:  # noqa: BLE001 - 已启动竞态:照旧直推
                pass
        try:
            sr = self._tts_.sample_rate
            n = int(sr * 0.4)
            pcm = bytearray()
            for i in range(n):
                v = int(12000 * math.sin(2 * math.pi * 440 * i / sr))
                pcm += v.to_bytes(2, "little", signed=True)
            output_emitter.push(bytes(pcm))
            output_emitter.flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - beep 尽力而为,唔好踩成新崩溃源
            print("MINIMAX_TTS_BEEP_FAIL", repr(exc), flush=True)


_QWEN3_TTS_PRESETS = frozenset(
    {"aiden", "dylan", "eric", "ono_anna", "ryan", "serena", "sohee", "uncle_fu", "vivian"}
)

# 语言回落档（2026-09-25 实测九个预置 speaker 均可发 zh/cantonese/en 三语，
# 缺省只定音色；env QWEN3_TTS_VOICE_<LANG> 覆写，运营可换耳感更好的预置）。
_QWEN3_TTS_LANG_FALLBACK: dict[str, tuple[str, str]] = {
    "zh": ("QWEN3_TTS_VOICE_ZH", "vivian"),
    "cantonese": ("QWEN3_TTS_VOICE_CANTONESE", "vivian"),
    "en": ("QWEN3_TTS_VOICE_EN", "serena"),
}

_QWEN3_SPEAKER_CACHE: dict[str, set[str]] = {}
_QWEN3_FALLBACK_SEEN: set[str] = set()


def _qwen3_speaker_union(base_url: str) -> set[str]:
    """sidecar 预置 ∪ 已注册克隆音色 id（懒取进程级缓存，2s 超时失败退纯预置）。
    克隆 id 是任意串（/v1/voices/register 自选），词法上与 MiniMax 音色 ID
    无法区分——必须查表判定「本地认识这个名字」。"""
    key = base_url.rstrip("/")
    cached = _QWEN3_SPEAKER_CACHE.get(key)
    if cached is not None:
        return cached
    union = set(_QWEN3_TTS_PRESETS)
    try:
        with httpx.Client(timeout=2.0) as client:
            for path in ("/v1/speakers", "/v1/voices"):
                resp = client.get(key + path)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    for item in data:
                        vid = item if isinstance(item, str) else str(item.get("voice_id") or item.get("id") or "")
                        if vid:
                            union.add(vid)
    except Exception:
        pass  # sidecar 不答=按纯预置判，宁回落勿哑轮
    _QWEN3_SPEAKER_CACHE[key] = union
    return union


def _resolve_local_voice(picked: str, lang: str, base_url: str) -> str:
    """本地档音色解析（2026-09-25 本地 TTS 立法）。

    persona/设置三键里常驻的是 MiniMax 音色 ID（Cantonese_GentleLady 这类）——
    直传 sidecar 会 _validate_speakers ValueError → 整轮 0 字节哑轮（实弹踩到，
    5/7 轮哑）。解析链：①预置名/sidecar 注册 id → 原样；②空或未知（MiniMax 类
    ID）→ 语言回落档（env 覆写 > 预置缺省）。回落只对每个陌生值打一次日志。"""
    raw = (picked or "").strip()
    if raw and raw in _qwen3_speaker_union(base_url):
        return raw
    env_key, default = _QWEN3_TTS_LANG_FALLBACK.get(lang, _QWEN3_TTS_LANG_FALLBACK["zh"])
    resolved = os.environ.get(env_key, "").strip() or default
    tag = f"{raw or '<empty>'} -> {resolved} (lang={lang})"
    if tag not in _QWEN3_FALLBACK_SEEN:
        _QWEN3_FALLBACK_SEEN.add(tag)
        print(f"[qwen3-tts] voice fallback: {tag}", flush=True)
    return resolved


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
            picked = str(self._voice.get(self._language_state.lang) or self._voice.get("zh") or "")
        else:
            raw = str(self._voice or "")
            if raw.startswith("{"):
                try:
                    mapping = json.loads(raw)
                    picked = str(mapping.get(self._language_state.lang) or mapping.get("zh") or "")
                except Exception:
                    picked = raw
            else:
                picked = raw
        # 本地档音色闸（2026-09-25）：MiniMax 类 ID 直传 sidecar=哑轮，
        # 未知一律回落语言档，预置/已注册克隆原样放行。
        return _resolve_local_voice(picked, self._language_state.lang, self._base_url)


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

    MiniMax 标记剥离单点(2026-09-28):本车道两条流(直念 ChunkedStream/LLM
    SynthesizeStream)全部经此 POST——Qwen3-TTS 无标记解析层(源码证:文本纯
    拼接进 tokenizer),正稿直念里的 `(breath)`、历史残留的 `<#0.3#>` 会逐字
    照念;NATURALNESS_BLOCK 由模型门("2.8"判据)保证不注入本地车道,此处再
    兜底剥净。剥后纯空白直接跳过(无音节输入会 hallucinate 爆段)。
    """
    text = strip_voice_style(text)
    if not text or not text.strip():
        return False
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
            _first_lane_on, _first_lane_chars = _tts_first_clause_config()
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
            sent_any = False

            def _flushable(s: str) -> bool:
                """overlap 增量可否送出：不能把连续的号码/数字串拦腰截断。"""
                if not s:
                    return False
                tail = s.rstrip("。！？!?，、；;：: \t")
                # 只拦 latin/数字结尾(词/号码可能被拦腰截断)。唔可以用裸 isalpha():
                # CJK 汉字 isalpha()==True → 中文片段全被拦,overlap 对中文全死。
                return not (tail and tail[-1].isascii() and tail[-1].isalnum())

            def _mark_first_send() -> None:
                # 官方 SynthesizeStream 契约(同 _MiniMaxBidiStream._send_text):
                # 首段文本交 provider(本车道=HTTP POST sidecar)时 _mark_started()
                # ——本地车道装配为裸 provider(无 CachedTTS/Relay 兜底),漏调则
                # 基座 metrics 监视器因 _started_time==0 永不 emit,整通 tts_metrics
                # 结构性为零(CP Provider 卡 TTS 行恒灰)。幂等:三个「可能是首段」
                # 的 POST 入口共用一个点。
                self._mark_started()

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
                        (sent_buf.find(ch) for ch in _TTS_SENT_END if sent_buf.find(ch) != -1),
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
                    _mark_first_send()
                    ok = await _qwen3_tts_post_frames(
                        self._tts_, sentence.strip(), output_emitter, state,
                        end_segment=False,
                    )
                    _last_send = time.monotonic()
                    sent_any = True
                    if not ok:
                        broken = True
                        break
                if broken:
                    break
                # overlap:句号之间的增量提前送(与 MiniMax 同款节奏)。
                # 首送走 W8 快车道(≥6 字即可、软停顿过半门豁免)。
                if overlap_on and not broken and sent_buf.strip():
                    send_now, via_first = _tts_overlap_send_now(
                        sent_buf,
                        sent_any=sent_any,
                        overlap_on=overlap_on,
                        first_lane_on=_first_lane_on,
                        first_lane_chars=_first_lane_chars,
                        overlap_chars=_overlap_chars,
                        time_up=(time.monotonic() - _last_send) * 1000 >= _overlap_ms,
                    )
                    if send_now:
                        frag = sent_buf.strip()
                        if _flushable(frag) and _tts_segment_has_word_char(frag):
                            if via_first:
                                print(f"QWEN3_TTS_FIRST_CLAUSE chars={len(frag)}", flush=True)
                            _mark_first_send()
                            ok = await _qwen3_tts_post_frames(
                                self._tts_, frag, output_emitter, state,
                                end_segment=False,
                            )
                            _last_send = time.monotonic()
                            if not ok:
                                broken = True
                                break
                            sent_any = True
                            sent_buf = ""
            if not broken:
                # 收尾残句:全场文本结束,把没凑够一句的尾巴合成掉。
                # 纯标点尾巴(唔沾正字)直接丢弃,绝唔 POST(P4-B 爆段源)。
                final_text = sent_buf.strip()
                if _tts_segment_has_word_char(final_text):
                    _mark_first_send()
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


def _asr_engine_from_cfg(asr_cfg: dict) -> str:
    """P1(2026-10-01)ASR 引擎车道解析(纯函数,A/B 线共用)。

    优先序:env ``BOK_ASR_ENGINE``(终极覆盖,一键回滚键)> ``asr_json.engine``
    > 缺省 ``"sensevoice"``。返回 ``"sensevoice"`` 或 ``""``(旧 Qwen3-ASR)。
    **缺省已翻 sensevoice**(2026-10-01 验证门全绿:soak 11/11 首声 p50
    1014ms/双通 B PASS+yield 命中/FLOW20 pass^3 零坏标记);回滚=设
    ``BOK_ASR_ENGINE=qwen3`` 或 asr_json.engine=qwen3。"""
    v = str(os.environ.get("BOK_ASR_ENGINE", "") or "").strip().lower()
    if v in ("sensevoice", "sv"):
        return "sensevoice"
    if v in ("qwen3", "mlx"):
        return ""
    cfg = str((asr_cfg or {}).get("engine") or "").strip().lower()
    if cfg in ("qwen3", "mlx"):
        return ""
    return "sensevoice"


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
        engine: str = "",
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
        # 【P1 SV-CPU 引擎车道(2026-10-01)】""/qwen3=旧 Qwen3 路径;sensevoice=
        # 纯 CPU 三语过门车道(40-48ms/句,MPS 只剩 LLM)。值来自 asr_json 设置
        # 通道(agent/interpret 装配点传入);sidecar 缺模型 fail-open 回旧引擎。
        self._engine = str(engine or "").strip()
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
                    # start 参数:language hint + 热词 context + 引擎车道(P1,都有先例
                    # 可空,空则不下发;getattr 鸭型访问——测试 fake 与旧设置面无此
                    # 属性时等同空)
                    start_params: dict[str, str] = {}
                    if lang_hint:
                        start_params["language"] = lang_hint
                    if getattr(self._stt_, "_hotword_context", ""):
                        start_params["context"] = self._stt_._hotword_context
                    if getattr(self._stt_, "_engine", ""):
                        start_params["engine"] = self._stt_._engine
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
                    # 整段路径同款词表回声补口(2026-09-28):一次性转写无流内闸口,
                    # 直接过守卫(一次性语义,echo_seen 不必跨请求持有)。
                    if os.environ.get("QWEN3_HOTWORD_ECHO_GUARD", "1") == "1":
                        text, _echo_seen = _vocab_echo_guard(
                            text,
                            getattr(self._stt_, "_hotword_context", "") or "",
                            echo_seen=False,
                        )
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
# 2026-10-06 B 线延迟压刀:常量改 env 可调(缺省 1.5 零漂移)——demo-cloud 实弹
# 分段账显示 perceived 尾巴(perceived_ms 838→3878 同输入方差)大头是本限速
# 的排队等待,非模型腿(mt_ms=260 恒定);B 线 _interp_env 收 1.0,A 线不动。
_SENTENCE_MIN_INTERVAL_S_DEFAULT = 1.5


def _sentence_commit_min_interval_s() -> float:
    """两次句级提交最少间隔(env QWEN3_ASR_COMMIT_MIN_INTERVAL_S,缺省 1.5=
    旧行为逐字节零漂移;坏值回缺省,负数钳 0)。"""
    try:
        return max(0.0, float(os.environ.get("QWEN3_ASR_COMMIT_MIN_INTERVAL_S",
                                             _SENTENCE_MIN_INTERVAL_S_DEFAULT)))
    except ValueError:
        return _SENTENCE_MIN_INTERVAL_S_DEFAULT
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


def _late_final_hotword_guard_on() -> bool:
    """词表幻听否决层总门（默认开；0=整层否决不评估，行为回 F2 现状）。

    AI 忙时停嘴 finish 整窗重解把词表热词抄成独立迟到 FINAL（「打错电话。」
    恰好整条是词表词、4 字连「极短追加」门都够不着）会掐断在播罐头——本层
    只否决「按词表贪心剥离后严格为空」的尾巴，真插话必有词表外残留照放行。
    """
    return os.environ.get("BOK_LATE_FINAL_HOTWORD_GUARD", "1") == "1"


def _late_final_max_tail_chars() -> int:
    """「极短追加」字数上限（BOK_LATE_FINAL_MAX_TAIL_CHARS，默认 2，地板 1）。

    幻听碎片实测 1-2 字（「那」「嗰」）；2026-09-07 豁免的真实短应答普遍
    ≥3 字（「我唔知」）——默认 2 正好夹住两者，唔后悔可调大。"""
    try:
        return max(1, int(os.environ.get("BOK_LATE_FINAL_MAX_TAIL_CHARS", "2")))
    except ValueError:
        return 2


def _vocab_only_net(net: str, vocab_terms) -> bool:
    """净文按词表贪心最长匹配剥离后**严格为空**（整条全由词表词首尾相接组成）。

    归一口径与 `_vocab_words_from_context`/`_to_simp` 同款（逐词剥标点+繁→简，
    两侧同表归一）；词表入参=流级 `_vocab_terms`（`_parse_vocab_terms` 反解的
    原始 token）。判定核与 `_is_hotword_vocab_echo` 同款贪心取最长命中，但无
    总长下限（「打错电话」4 字也要拦）、判「整条全覆盖」而非回声顺串。"""
    terms: set[str] = set()
    for t in vocab_terms or ():
        w = re.sub(r"[^\w\u4e00-\u9fff]+", "", _to_simp(str(t or "")))
        if w:
            terms.add(w)
    if not terms:
        return False
    remaining = re.sub(r"[^\w\u4e00-\u9fff]+", "", _to_simp(str(net or "")))
    if not remaining:
        return False
    ordered = sorted(terms, key=len, reverse=True)
    while remaining:
        hit = next((w for w in ordered if remaining.startswith(w)), None)
        if hit is None:
            return False  # 有一段唔係词表词 → 真人话,唔拦
        remaining = remaining[len(hit):]
    return True


def late_final_is_new_speech(
    payload: str,
    committed: str,
    *,
    agent_busy: bool,
    max_tail_chars: int = 2,
    closing_say: bool = False,
    vocab_terms: tuple = (),
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
    - 词表幻听 hotword_only（AI 忙+净文无数字字母 run+按词表贪心最长匹配
      剥离后严格为空）→ False：停嘴整窗重解把词表热词抄成独立迟到 FINAL
      （「打错电话。」恰好整条是词表词，4 字连「极短追加」门都够不着，却
      会掐断在播罐头）——真插话剥后必有词表外残留（「打错电话啊」剩「啊」），
      照放行；
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
    # 词表幻听否决层（hotword_only）：AI 忙 + 净文无数字/字母 run（上一行已
    # 放行带 run 的尾巴，数字零降级豁免同 L5611 口径）+ 按词表贪心最长匹配
    # 剥离后严格为空 → 整条只是词表词顺串的重解幻听，唔成轮唔打断。刻意比
    # F3 的「剩余 ≤2 字」严：只认剥后严格为空，真插话「打错电话啊」（剩
    # 「啊」）照放行。vocab_terms 默认空=层短路，逐字节旧行为。
    if agent_busy and vocab_terms and _vocab_only_net(norm, vocab_terms):
        return False
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


# join 门尾剥集:句尾标点/空白剥掉后看「最后说出口的字」——hold 判定只关心
# 尾部形状(号码是否被拦腰),唔关心句中内容。
_JOIN_TAIL_STRIP = "。，,．.！!？?～~…；;、 \t"


def _join_worthy(text: str) -> bool:
    """续接可能句:**句尾是数字**(汉字/全角数字字符、或英文数字词收尾——号码可能
    被停顿拦腰,等续段并入同一会话)、或以系词收尾(係/系/是/is,英文只认独立词)。
    呢类句每轮多等一个 hold 窗;其余普通陈述句零加迟。

    判据收窄(2026-10-02,B 线连珠炮 blob 链实证):旧版「句中任意位置 ≥2 位数字」
    把「…有三百多名员工。」(数字在句中、句尾是字)也扣 hold——数字后还有字=
    号码已完整讲完,无拦腰风险;hold 却在 0.8s 窗内把下一句粘进同一 sidecar 会话
    (START 取消 flush 计时),链式粘串直到断线,尾巴 finish 才整块吐出——
    probe_interp_backlog 正压臂 orig=4/6、drops/skips=0 FAIL 的根因(商务同传里
    价格/数量/日期满地 ≥2 位数字,任何带数字句在自然停顿下都会触发粘串)。
    新判据只认尾部:剥尾标点后末字符经 _JOIN_DIGIT_TRANS 归一是数字(「我的号码
    係一七二」停嘴=拦腰,hold),或英文末词 ∈_JOIN_EN_DIGIT_MAP(「code is
    three」)。数字后带量词/名词收尾(「三百多」「300多名」「三百六十八块」)照常
    提交。09-06 报号粘接修复的目的场景(逐位报号停嘴)一字不损。"""
    t = (text or "").strip()
    if not t:
        return False
    core = t.rstrip(_JOIN_TAIL_STRIP)
    if core and _join_norm_digits(core[-1:]):
        return True
    _m = re.search(r"[A-Za-z]+$", core or "")
    if _m and _m.group(0).lower() in _JOIN_EN_DIGIT_MAP:
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
        # smart-turn 滚动尾部 PCM（V1，BOK_SMART_TURN=1 才消费）：本会话最近
        # ≤8s 的 16kHz int16，喂语义闸判「说完没」。与会话同生命周期——_reset
        # 清零；join-hold 续段**不清**（跨段积累正係判定所需上下文）。
        self._smart_pcm = bytearray()
        # 车道关闭打点标志（cantonese 车道恒关时每流只打一次；流级，不随 _reset 清）：
        self._smart_lane_off_logged = False

    def _append_turn_pcm(self, data: bytes) -> None:
        """滚动尾部缓冲：追加并裁到 8s 上限（留尾）。"""
        self._smart_pcm.extend(data)
        if len(self._smart_pcm) > _smart_turn.PCM_BYTES_8S:
            del self._smart_pcm[: len(self._smart_pcm) - _smart_turn.PCM_BYTES_8S]

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
            _dbg = os.environ.get("BOK_ASR_FRAME_DEBUG", "") == "1"
            _dbg_t0 = time.monotonic()
            _dbg_n = 0
            _dbg_samples = 0
            _dbg_sr = 0
            async for input in self._input_ch:
                if isinstance(input, self._FlushSentinel):
                    vad_stream.flush()
                    continue
                if _dbg:
                    _dbg_n += 1
                    _dbg_samples += len(input.data) // 2
                    _dbg_sr = input.sample_rate
                    _dt = time.monotonic() - _dbg_t0
                    if _dt >= 1.0:
                        # 帧到达节奏观测行(诊断用):samples_ms/s≈1000=满速;显著
                        # <1000=上游丢帧(事件环溢出/重采样链路丢失),frames 只作
                        # 粒度参考——B 线正压臂吃头定位仪器(2026-10-02)。
                        print(
                            f"QWEN3_ASR_FRAME_DEBUG frames={_dbg_n} samples_ms={_dbg_samples//16} "
                            f"sr={_dbg_sr} gap={_dt:.2f}s",
                            flush=True,
                        )
                        _dbg_t0 = time.monotonic()
                        _dbg_n = 0
                        _dbg_samples = 0
                vad_stream.push_frame(input)
            vad_stream.end_input()

        async def _recognize() -> None:
            started = False
            async for event in vad_stream:
                if event.type == vad.VADEventType.START_OF_SPEECH:
                    started = True
                    if _frame_dbg_on():
                        print(
                            f"QWEN3_ASR_VAD_DEBUG START_OF_SPEECH finishing={self._finishing} "
                            f"session={bool(self._session_id)}",
                            flush=True,
                        )
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
                                _preroll_pcm = bytes(utils.merge_frames(event.frames).data)
                                if _frame_dbg_on():
                                    print(
                                        f"QWEN3_ASR_VAD_DEBUG preroll_ms={len(_preroll_pcm)//32} "
                                        f"frames_n={len(event.frames)}",
                                        flush=True,
                                    )
                                self._pending.extend(_preroll_pcm)
                                self._append_turn_pcm(_preroll_pcm)
                            except Exception:  # noqa: BLE001 - pre-roll 合帧失败不致命
                                pass
                elif event.type == vad.VADEventType.INFERENCE_DONE:
                    if not started or self._finishing:
                        continue
                    # 1.7 utils.merge_frames=rtc.combine_audio_frames:返回【单个】
                    # rtc.AudioFrame(不可迭代,官方 StreamAdapter 同款用法)。
                    _window_pcm = bytes(utils.merge_frames(event.frames).data)
                    self._pending.extend(_window_pcm)
                    self._append_turn_pcm(_window_pcm)
                    await self._maybe_partial()
                elif event.type == vad.VADEventType.END_OF_SPEECH:
                    if _frame_dbg_on():
                        print(
                            f"QWEN3_ASR_VAD_DEBUG END_OF_SPEECH started={started} "
                            f"finishing={self._finishing} pending_ms={len(self._pending)//32}",
                            flush=True,
                        )
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
                    # ---- smart-turn 语义闸（V1，BOK_SMART_TURN=1；默认关）--------
                    # VAD 0.35s 静音只证明「停了 0.35s」——句间喘气与真停嘴同形，
                    # 停嘴即提交会把没讲完的半句拆成碎轮。语义闸补第二判据：本会话
                    # 最近 ≤8s 尾部 PCM 喂 smart-turn-v3，p<0.5=「话没说完」→ 复用
                    # 既有 join-hold（不新造机制）等下一段并入，hold 超时真停嘴照旧
                    # finish 兜底；p≥0.5 或模型不可判 → 旧路径逐字节不变。fail-open
                    # 铁律：模型缺位/推理异常 smart_turn_prob 返回 None=pass（skip/
                    # failopen 的原因打点在 smart_turn 模块内）。
                    # 车道门（V1 定案 2026-09-26）：smart-turn-v3.2 无粤语校准，
                    # cantonese 通话恒关——本块不进=旧路径逐字节照走（join-hold
                    # 等下游逻辑零变化）；zh/en 才吃语义闸。
                    _st_lang = str(getattr(self._stt_._language_state, "lang", "") or "")
                    if (
                        _smart_turn.smart_turn_enabled()
                        and _smart_turn.smart_turn_lane_allowed(_st_lang)
                    ):
                        _st_t0 = time.monotonic()
                        _st_prob = await _smart_turn.smart_turn_prob(bytes(self._smart_pcm))
                        _st_verdict = _smart_turn.smart_turn_decide(_st_prob)
                        _st_ms = int((time.monotonic() - _st_t0) * 1000)
                        if _st_verdict == "hold":
                            print(
                                f"SMART_TURN verdict=held p={_st_prob:.3f} ms={_st_ms} "
                                f"chars={len(self._last_partial)}",
                                flush=True,
                            )
                            self._join_hold_active = True
                            self._finishing = False  # hold 期间 partial 继续滚
                            self._join_task = asyncio.create_task(self._hold_flush())
                            continue
                        if _st_verdict == "commit":
                            print(
                                f"SMART_TURN verdict=committed p={_st_prob:.3f} ms={_st_ms} "
                                f"chars={len(self._last_partial)}",
                                flush=True,
                            )
                        # pass（None）→ 零干预照旧；闸关时整块跳过（零成本）。
                    elif _smart_turn.smart_turn_enabled():
                        # 闸开但车道关（cantonese）：只打点，不走判定——每流一次防刷屏。
                        if not self._smart_lane_off_logged:
                            self._smart_lane_off_logged = True
                            print(f"SMART_TURN lane_off lang={_st_lang}", flush=True)
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
                        # 词表幻听否决层（hotword_only）词表：流级 _vocab_terms
                        # 已在 __init__ 从 _hotword_context 反解好（注意挂在流
                        # 包装层 self 上，不在 _stt_）；kill-switch 关=传空=层
                        # 不评估。
                        _vt = (
                            getattr(self, "_vocab_terms", ())
                            if _late_final_hotword_guard_on()
                            else ()
                        )
                        if _cs or not late_final_is_new_speech(
                            payload,
                            committed_before,
                            agent_busy=bool(getattr(self._stt_, "_reply_busy", False)),
                            max_tail_chars=_late_final_max_tail_chars(),
                            vocab_terms=_vt,
                        ):
                            _hw = bool(_vt) and _vocab_only_net(payload, _vt)
                            print(
                                f"QWEN3_ASR_LATE_FINAL_DROP reason="
                                f"{'closing_say' if _cs else 'tail_append' if not _hw else 'hotword_only'} "
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
            # 词表幻听否决层（hotword_only）词表：流级 self._vocab_terms（__init__
            # 反解位，不在 _stt_ 上）；kill-switch 关=传空=层不评估（与 _run
            # 停嘴分支同一把尺）。
            _vt = (
                getattr(self, "_vocab_terms", ())
                if _late_final_hotword_guard_on()
                else ()
            )
            if _cs or not late_final_is_new_speech(
                payload,
                committed_before,
                agent_busy=bool(getattr(self._stt_, "_reply_busy", False)),
                max_tail_chars=_late_final_max_tail_chars(),
                vocab_terms=_vt,
            ):
                _hw = bool(_vt) and _vocab_only_net(payload, _vt)
                print(
                    f"QWEN3_ASR_LATE_FINAL_DROP reason="
                    f"{'closing_say' if _cs else 'tail_append' if not _hw else 'hotword_only'} "
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
        # start 参数:language hint + 热词 context + 引擎车道(P1;同 offline 路径,
        # 空则不下发;getattr 鸭型访问——测试 fake 无此属性时等同空)+ partial
        # 间隔档(agent 生成中抑制,GPU 竞态专项;None=不下发用 env 默认)。
        start_params: dict[str, str] = {}
        if lang_hint:
            start_params["language"] = lang_hint
        if getattr(self._stt_, "_hotword_context", ""):
            start_params["context"] = self._stt_._hotword_context
        if getattr(self._stt_, "_engine", ""):
            start_params["engine"] = self._stt_._engine
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
        - 限速：距上次提交 < _sentence_commit_min_interval_s() 唔提交（连珠句防机关枪）。
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
        if time.monotonic() - self._last_sentence_commit_at < _sentence_commit_min_interval_s():
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
                # 词表回声守卫补口(2026-09-28):finish 增量捷径丢失/全量重解时,
                # 整表抄词直穿此路径(四个流内闸口都管不到)——13 通实证转写带
                # 110-120 字词表尾巴直入流程判定,两通随即被 stall-degrade 吞答。
                text = self._echo_filter(text, "finish")
                lang = _normalize_asr_language(str(data.get("language") or ""), text)
                # 句级置信度（2026-09-27 sidecar stream_generate per-token top1 概率
                # 聚合）:暴露位给轮处理器做 CSC 门控;None=sidecar 关档/回退/旧版。
                conf = data.get("confidence")
                self._stt_.last_confidence = conf if isinstance(conf, dict) else None
                _conf_log = ""
                if isinstance(conf, dict):
                    _conf_log = (
                        f" conf_mean={conf.get('mean')} conf_min={conf.get('min')}"
                        f" low_tokens={conf.get('low_tokens')}/{conf.get('n_tokens')}"
                    )
                print(
                    f"QWEN3_ASR_TEXT {repr(text[:120])} {lang} "
                    f"ASR_MS={(time.monotonic() - t0) * 1000:.0f}(stream){_conf_log}",
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
        self._smart_pcm.clear()  # smart-turn 尾部随段清零（hold 续段不走 reset）
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


# 帧/VAD 事件观测行总闸(诊断仪器,默认关):B 线正压臂吃头取证 2026-10-02。
def _frame_dbg_on() -> bool:
    return os.environ.get("BOK_ASR_FRAME_DEBUG", "") == "1"


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
        # 最近一次 /api/finish 的句级置信度(2026-09-27):sidecar 按 stream_generate
        # per-token top1 概率聚合;None=关档/回退/旧版 sidecar。轮处理器在
        # on_user_turn_completed 读它做 CSC 触发门(FINAL 先于 turn 钩子,时序成立)。
        self.last_confidence: dict | None = None

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
