from __future__ import annotations

import io
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

try:
    import soundfile as sf
except Exception:  # pragma: no cover
    sf = None

SAMPLE_RATE = int(os.environ.get("QWEN3_ASR_SAMPLE_RATE", "16000"))
BACKEND = os.environ.get("QWEN3_ASR_BACKEND", "transformers").lower()
MODEL_PATH = os.environ.get("QWEN3_ASR_MODEL", "Qwen/Qwen3-ASR-0.6B")

app = FastAPI(title="Bok Qwen3-ASR Sidecar")

# ---- 并发竞态让位(2026-10-01 双通实测定案) ----
# 一机多通共享 MPS:一通的 finish 整段重解/regular partial 在飞时,另一通的
# 关键解码(chunk partial/EOU finish)只能排队,延迟到窗截断=转写乱字
# (双探针并发 soak 第二句被听成「…拜拜」实证)。本计数覆盖三条 MLX generate
# 入口,让位两档:①finish 短轮强制整句(置信度专用加菜)在别人在飞时跳过——
# 落回增量路径,文本质量零损只缺置信度;②partial 解码间隔在别人在飞时×2
# (降突发密度,final 链不受影响)。kill-switch QWEN3_ASR_CONTENTION_YIELD=0。
# GIL 下 int += 原子,免锁。
_INF_INFLIGHT = {"n": 0}


class _inflight:
    def __enter__(self) -> "_inflight":
        _INF_INFLIGHT["n"] += 1
        return self

    def __exit__(self, *exc) -> bool:
        _INF_INFLIGHT["n"] -= 1
        return False


def _others_inflight() -> bool:
    """调用点在进 with 之前查:计数 ≥1=另有推理在飞(自己尚未计入)。"""
    return _INF_INFLIGHT["n"] >= 1


def _contention_yield_on() -> bool:
    return os.environ.get("QWEN3_ASR_CONTENTION_YIELD", "1") == "1"


def _partial_should_skip(elapsed_ms: float, interval_ms: float, others: bool) -> bool:
    """partial 解码节流决策(纯函数):未到间隔跳;并发在飞时间隔×2 再判。"""
    eff = interval_ms * 2 if others else interval_ms
    return elapsed_ms < eff


# ---- P1 SV-CPU 引擎车道(2026-10-01 三层解耦计划:CPU 耳朵/MPS 大脑) ----
# SenseVoice-small int8 ONNX 纯 CPU:三语过门(zh 2.8%/en 5.4%/canto 8.6%,
# WA 数字 16/16 clean+窄带,40-48ms/句;reports/sensevoice-eval/)。模型目录
# 由 bok download --only sensevoice 落位(不进 git);缺席 fail-open 回 Qwen3。
SV_MODEL_DIR = os.environ.get(
    "QWEN3_ASR_SV_MODEL_DIR",
    str(Path.home() / "Library" / "Application Support" / "BokVoice" / "models" / "sensevoice"),
)
# 识别器语言档:auto≈yue 钉死(评估语料同分);要钉死设 yue/zh/en。
SV_LANGUAGE = os.environ.get("QWEN3_ASR_SV_LANGUAGE", "auto")


def _norm_engine(engine: str) -> str:
    """会话引擎归一:"" / qwen3 / mlx = 旧全局路径;sensevoice / sv = CPU 车道。

    未知值保守回缺省(旧行为)。"""
    e = str(engine or "").strip().lower()
    if e in ("sensevoice", "sv"):
        return "sensevoice"
    return ""


def _boot_engine() -> str:
    """启动期引擎档:env ``BOK_ASR_ENGINE``(bok 起 :8787 时透传)归一。

    缺省/未知 → ``"sensevoice"``(与 agent ``_asr_engine_from_cfg`` 的缺省档
    对齐——默认部署 CPU 耳朵跑 SV,sidecar 无需 eager 加载 Qwen3-1.7B GPU
    权重,~1.9GB 纯占卡零消费);显式 ``qwen3``/``mlx`` → ``""``=旧路径,
    启动 eager 加载,回滚档行为逐字节不变。
    """
    v = str(os.environ.get("BOK_ASR_ENGINE", "") or "").strip().lower()
    if v in ("qwen3", "mlx"):
        return ""
    return "sensevoice"


def _sv_model_dir_ok() -> bool:
    d = Path(SV_MODEL_DIR)
    return (d / "model.int8.onnx").is_file() and (d / "tokens.txt").is_file()


def _sv_lang_label(session_lang: str) -> str:
    """会话语言(cantonese/zh/en)→ 插件契约的 language 标签(与 Qwen3 路径一致)。"""
    s = str(session_lang or "").strip().lower()
    if s in ("cantonese", "yue", "粵", "粤"):
        return "Cantonese"
    if s in ("zh", "chinese", "mandarin"):
        return "Chinese"
    if s in ("en", "english"):
        return "English"
    return ""


def _resample(wav: np.ndarray, sr: int, target_sr: int) -> np.ndarray:
    if sr == target_sr:
        return wav
    try:
        import librosa

        return librosa.resample(wav, orig_sr=sr, target_sr=target_sr)
    except Exception:
        duration = wav.shape[0] / float(sr)
        out_len = int(round(duration * target_sr))
        x_old = np.linspace(0.0, duration, num=wav.shape[0], endpoint=False)
        x_new = np.linspace(0.0, duration, num=out_len, endpoint=False)
        return np.interp(x_new, x_old, wav).astype(np.float32)

def _wav_from_pcm16(pcm: bytes) -> tuple[np.ndarray, int]:
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, SAMPLE_RATE

def _trim_trailing_silence(pcm: bytes) -> bytes:
    """finish 前裁掉尾部静音帧(简单能量门限)。

    VAD min_silence 0.45s + 端点推理意味着 finish buffer 天然带 ~0.5s 静音尾巴,
    整句/增量解码都在为它烧 GPU。20ms 帧逐帧 RMS,从尾往前吞掉低于门限的帧;
    最多裁 TRIM_MAX_SEC(防整段皆静音被裁光),裁穿即住手交原 buffer。
    """
    if not pcm or TRIM_MAX_SEC <= 0:
        return pcm
    frame = SAMPLE_RATE // 50  # 20ms
    total_frames = (len(pcm) // 2) // frame
    if total_frames == 0:
        return pcm
    x = np.frombuffer(pcm[: total_frames * frame * 2], dtype=np.int16).astype(np.float32) / 32768.0
    rms = np.sqrt((x.reshape(total_frames, frame) ** 2).mean(axis=1))
    max_silent = int(TRIM_MAX_SEC * 50)
    k = 0
    for i in range(total_frames - 1, -1, -1):
        if rms[i] >= TRIM_RMS or k >= max_silent:
            break
        k += 1
    if k == 0:
        return pcm  # 尾部无静音:原样返回(唔顺手裁掉非帧对齐余数)
    keep = (total_frames - k) * frame * 2
    if keep <= 0 or keep >= len(pcm):
        return pcm  # 整段皆静音:不动,交模型与旧路径判空
    return pcm[:keep]

# 短轮整句解码强开阈值(2026-09-27)：confidence 功能开启时,裁剪后语音 ≤ 本秒数的
# finish 跳过增量 fast path 直接走整句 `_generate_out_with_conf`——增量路径不产
# token 级置信度(响应带 None),而**短碎片轮恰是新重问车道需要置信度信号的地方**
# (短句正是增量 fast path 的高频命中面,却恰好无置信度)。长 buffer 增量快路不变。
_FULL_DECODE_MAX_S = 2.0


def _needs_full_decode(pcm_seconds: float) -> bool:
    """纯函数:裁剪后语音 ≤ 阈值 → 需整句解码(取真置信度)。fail-open 见调用点。

    阈值 2.0s 覆盖「短碎片轮」的典型长度(重问车道关心的正是这些轮);仅当
    confidence 功能开启时调用方才用它强开整句——kill-switch 关=完全不消费。
    """
    return float(pcm_seconds) <= _FULL_DECODE_MAX_S


def _has_latin_or_digit(text: str) -> bool:
    """尾巴文本含任何数字/拉丁字符(连续串即 WhatsApp 捕获高危)。

    尾段解码缺左上下文,号码/英文被缝腰斩或听错是最高危场景——凡出现一律
    回退整句高精度转写,保「停嘴整句兜底」铁律。
    """
    return any(ch.isascii() and ch.isalnum() for ch in text)


# ---- M-24 泰文字形幻觉守卫(2026-09-23 修复波#4,task-4 M3 实弹)----
# Qwen3-ASR 偶发语种混淆:粤/中/英语音的 finish 输出整段带泰文字形
# (「兩日內賠到」→「เล่าอย่างหน่อย陪到。」,task-4 WER 批 4 渲染 3 次)。
# 判据=输出含泰文块(U+0E00–U+0E7F)——本产品三语(zh/cantonese/en)任何位置
# 都不可能合法出现泰文,零误伤面。动作=触发时以 temperature 重解一次(greedy
# 重解同音频恒同结果,采样才有变化);重解干净即采用,仍带泰文则剥泰文串保留
# 余文——缓解非根除(诚实边界:模型缺陷,头部内容可能已损)。重解只在罕见
# 触发时发生:干净输出零开销零行为变化。
_SCRIPT_RETRY_TEMP = float(os.environ.get("QWEN3_ASR_SCRIPT_RETRY_TEMP", "0.25"))


def _has_thai_glyphs(text: str) -> bool:
    return any("\u0e00" <= ch <= "\u0e7f" for ch in str(text or ""))


def _strip_thai_runs(text: str) -> str:
    out = re.sub(r"[\u0e00-\u0e7f]+", "", str(text or ""))
    out = re.sub(r"^[，,、。.\s]+", "", out)  # 剥完开头残留标点/空白
    return out.strip()


def _strip_punct_space(text: str) -> str:
    """去标点/空白,只留正字——用于「文本长度 vs 覆盖音频秒数」可信度比较。"""
    import unicodedata

    return "".join(ch for ch in str(text or "")
                   if not ch.isspace() and not unicodedata.category(ch).startswith("P"))


# 增量 finish 覆盖可信度下限(字/秒):语音正常节奏 ≥4 字/秒,取极保守的 2 字/秒
# 当门——只有「partial 窗起点切在语音中间 / 模型只解到后半截」这类坏窗会被挡下
# (2026-09-12 外呼 E2E 实证:2 字 partial 覆盖 2.6s → FINAL 丢首字)。
# 挡下即退回整句高精度兜底,只损失提速不损正确性。QWEN3_ASR_INC_MIN_CPS 可调。
_INC_PARTIAL_MIN_CHARS_PER_SEC = float(os.environ.get("QWEN3_ASR_INC_MIN_CPS", "2"))


def _seam_risky(partial_text: str) -> bool:
    """接缝安全检查:partial 尾字符是 latin/数字 → 词/号码可能被缝截断,不可验证。

    CJK 字素自成音节,缝前后按序拼接基本无损(粤语/普通话转写以字为单位);
    latin 词跨缝被劈开则无法从文本侧验证 → 宁可回退整段。
    """
    if not partial_text:
        return True
    ch = partial_text[-1]
    return ch.isascii() and ch.isalnum()


def _join_stitched(left: str, right: str) -> str:
    """拼接 partial 与尾段:CJK 边界直接相连,含空格/标点边界保持自然间隔。"""
    if not left:
        return right
    if not right:
        return left
    if left[-1].isascii() and left[-1].isalnum():
        return f"{left} {right}"
    return f"{left}{right}"


def _fallback_language(text: str) -> str:
    """Qwen3-ASR's raw output sometimes omits the `language ...<asr_text>`
    metadata tag (observed on CPU), leaving the language field empty. Fall
    back to a character-set heuristic so the agent's LanguageState can still
    pick the right TTS voice/language. Cantonese text is detected via its
    distinctive characters and returned as `Cantonese`."""
    if not text:
        return ""
    # Conservative Cantonese-specific characters (avoid common Mandarin
    # particles like 呢/啦/嘛 which would misclassify zh text).
    cantonese_markers = set(
        "冇唔嘅係哋佢喺嚟啲嗰喎㗎冚瞓攞揾搵嘥咗乜嘢咩"
        "傾偈倾偈倾下傾下唔該而家依家啱啱咁睇嚟睇来同我哋"
    )
    if any(ch in cantonese_markers for ch in text):
        return "Cantonese"
    han = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if han >= latin and han > 0:
        return "Chinese"
    if latin > 0:
        return "English"
    return ""

def _session_context(session: dict) -> str | None:
    """热词/context 透传值(Qwen3-ASR 官方 customizable context = system message
    词汇表软偏置)。运行时读 env:QWEN3_ASR_CONTEXT=0 一键回退(行为同旧=None);
    空串同样 None(不进 prompt)。"""
    if os.environ.get("QWEN3_ASR_CONTEXT", "1") == "1":
        return str(session.get("context") or "") or None
    return None


def _finish_language_hint(start_lang: str | None) -> str | None:
    """finish 整句/增量解码的语言提示(与会话 start 语言同源,唔重跑 auto-LID)。

    en 会话显式归一成 "English"(接受 "en"/"english" 两种写法):整句 decode 若交
    auto 检测,英语轮要多花语言判定且短轮易摇摆;显式 hint 消掉呢截。cantonese
    原样透传(mlx 层大小写不敏感回填模型 config 规范名 Cantonese);zh 同样原样
    透传(回填规范名 Chinese)——A 线每通对话语言固定(per-call fixed),zh 通话也
    整场下发 Chinese hint,实测夹英文 code-switching 词保得住;只有完全冇 hint
    (空)才 None=auto。
    """
    raw = str(start_lang or "").strip()
    key = raw.lower()
    if key in {"en", "english"}:
        return "English"
    return raw or None


# ---- 句级置信度(2026-09-27,A 线 ASR 质量闸素材)----
# Qwen3-ASR mlx 后端 stream_generate 每步已 yield 全词表 log-softmax 的 logprobs,
# 旧路径 `for token, _` 直接丢弃——置信度白算。这里逐 token 采 top1 概率、句级
# 聚合 mean/min/low_tokens,供 agent 侧(后续接线)判低质转写。partial 高频不接,
# 只在 finish 整句解码时算。QWEN3_ASR_CONFIDENCE=0 一键回退旧 generate() 且不带键。
_LOW_CONF_P = 0.3  # 单 token top1 概率低于此值计一个 low token


def _make_greedy_sampler():
    """贪心采样器(temperature=0)= 旧 generate() 默认档,保文本逐字节一致。

    单独抽函数:mlx_audio 只在真实推理时才可导入(主 venv 单测无 mlx),调用点
    可被测试替身替换。
    """
    from mlx_audio.lm.sample_utils import make_sampler

    return make_sampler(0.0)


def _top1_prob(logprobs):
    """全词表 log-softmax → top1 概率(标量 float)。

    铁律:整词表张量绝不搬 CPU——`mx.max` 取标量、`mx.eval` 求值后取 float,每步
    只有 1 个 Python float 越界(词表 15 万级,搬整表会拖垮逐 token 解码)。
    """
    import mlx.core as mx

    p = mx.exp(mx.max(logprobs))
    mx.eval(p)
    return float(p)


def _aggregate_conf(probs: list[float]) -> dict | None:
    """句级置信度聚合:token top1 概率均值/最低值 + 低置信 token 计数。

    空 token 流(整段静音/解码零产出)→ None(响应带键但值为 None,agent 侧把
    「无置信度」与「低置信」分开处理)。
    """
    if not probs:
        return None
    return {
        "mean": round(sum(probs) / len(probs), 4),
        "min": round(min(probs), 4),
        "low_tokens": int(sum(1 for p in probs if p < _LOW_CONF_P)),
        "n_tokens": len(probs),
    }


class ASRService:
    # 会话 TTL 与总量上限(2026-09-17 全量 debug F5):sidecar 是长命进程服务所有
    # 通话,_sessions 只在 /api/finish 时 pop——client 取消/崩溃致 finish 永不到达
    # 时,会话连同累积 PCM bytearray 永久驻留,跨通话无界泄漏。懒清扫(每次 start
    # 顺带跑)零新增任务;TTL 180s 远超最长轮窗(≤12s 滑窗+hold flush 余量)。
    SESSION_TTL_S = 180.0
    SESSION_MAX = 512

    def __init__(self) -> None:
        self._model: Any | None = None
        self._sessions: dict[str, dict[str, Any]] = {}
        self._load_error: str | None = None
        # P1 SV-CPU 引擎:按语言键缓存的识别器表(空表=未加载;首次 sensevoice
        # 会话按钉定语言触发懒加载)。
        self._sv_models: dict[str, Any] = {}
        # 任务 B(2026-10-01):BOK_ASR_ENGINE=sensevoice 档启动不加载 Qwen3 权重,
        # 首个真走 qwen3 路径的请求(_ensure_loaded)触发懒加载——回滚
        # BOK_ASR_ENGINE=qwen3 免重启。_load_lock+_loading 单飞:加载期间到达的
        # 并发请求在锁上排队,不静默丢请求、不重复加载。
        self._qwen3_deferred = False
        self._loading = False
        self._load_lock = threading.Lock()

    def _sweep_sessions(self, now: float | None = None) -> None:
        """过期/超量会话清扫:TTL 到期先清,总量超限再按 created_at 清最旧。

        调用方必须在插入新会话**之后**调用(保证 len ≤ SESSION_MAX 的不变量:
        先插再清,溢出数含新会话;先清再插会收敛到 SESSION_MAX+1)。"""
        now = time.time() if now is None else now
        stale = [
            sid for sid, s in self._sessions.items()
            if now - float(s.get("created_at") or 0.0) > self.SESSION_TTL_S
        ]
        for sid in stale:
            self._sessions.pop(sid, None)
        overflow = len(self._sessions) - self.SESSION_MAX
        if overflow > 0:
            oldest = sorted(
                self._sessions.items(),
                key=lambda kv: float(kv[1].get("created_at") or 0.0),
            )[:overflow]
            for sid, _s in oldest:
                self._sessions.pop(sid, None)

    def load(self) -> None:
        if os.environ.get("QWEN3_ASR_DISABLE_LOAD") == "1":
            return
        try:
            if BACKEND == "mlx":
                from mlx_audio.stt.utils import load as mlx_load

                self._model = mlx_load(MODEL_PATH)
                try:
                    import mlx.core as mx

                    print(f"[qwen3-asr] loaded {MODEL_PATH} device={mx.default_device()}", flush=True)
                except Exception:  # pragma: no cover
                    pass
                return
            import torch
            from qwen_asr import Qwen3ASRModel

            if BACKEND == "vllm":
                self._model = Qwen3ASRModel.LLM(
                    model=MODEL_PATH,
                    gpu_memory_utilization=float(
                        os.environ.get("QWEN3_ASR_GPU_UTIL", "0.7")
                    ),
                    max_new_tokens=int(os.environ.get("QWEN3_ASR_MAX_TOKENS", "256")),
                )
            else:
                device = self._resolve_device()
                dtype = torch.bfloat16 if device == "cuda" else torch.float32
                self._model = Qwen3ASRModel.from_pretrained(
                    MODEL_PATH,
                    dtype=dtype,
                    device_map=device,
                    max_new_tokens=int(os.environ.get("QWEN3_ASR_MAX_TOKENS", "256")),
                )
        except Exception as exc:  # pragma: no cover - model load can fail
            self._load_error = repr(exc)

    @staticmethod
    def _resolve_device() -> str:
        override = os.environ.get("QWEN3_ASR_DEVICE", "").strip().lower()
        if override in {"cpu", "cuda", "mps"}:
            return override
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    def _ensure_loaded(self) -> None:
        """Qwen3 权重就绪闸(懒加载档单飞)。

        任务 B:启动跳载档(_qwen3_deferred)下,首个真走 qwen3 路径的请求在此
        触发加载——_load_lock 单飞,并发请求在锁上排队等加载完成(不硬拒、不
        静默丢请求);加载失败照旧 503+原因。eager 档(显式 BOK_ASR_ENGINE=
        qwen3)路径零行为变化。
        """
        if self._load_error:
            raise HTTPException(status_code=503, detail=f"model not ready: {self._load_error}")
        # 等待条件含 _loading:加载窗口内(首个请求已置 deferred=False)到达的并发
        # 请求仍须进锁排队——只认 deferred 会把窗口内的请求直接 503 掉(竞态)。
        if self._model is None and (self._qwen3_deferred or self._loading):
            with self._load_lock:
                if self._model is None and self._qwen3_deferred:
                    self._loading = True
                    self._qwen3_deferred = False
                    print("ASR_QWEN3_LAZY_LOAD engine=qwen3", flush=True)
                    try:
                        self.load()
                    finally:
                        self._loading = False
            if self._load_error:
                raise HTTPException(status_code=503, detail=f"model not ready: {self._load_error}")
        if self._model is None:
            raise HTTPException(status_code=503, detail="model not loaded")

    def start(self, language: str = "", context: str = "", partial_ms: str = "", engine: str = "") -> str:
        session_id = uuid.uuid4().hex
        pm = str(partial_ms or "").strip()
        # 【P1 SV-CPU 引擎车道(2026-10-01 计划定案)】engine=会话级引擎选择:
        #   ""/"qwen3"/"mlx" = 全局 BACKEND 旧路径(Qwen3-ASR,逐字节不变);
        #   "sensevoice"/"sv" = SenseVoice-small int8 ONNX,**纯 CPU**——三语实测
        #   zh 2.8%/en 5.4%/canto 8.6%、WA 数字 16/16(clean+窄带)、40-48ms/句
        #   (reports/sensevoice-eval/);MPS 从此只跑 LLM,ASR/LLM 竞态结构性终结。
        #   SV 无 context 热词通道(域词靠 agent 侧 asr_polish 吸附层兜)、无
        #   token 置信度(confidence 键缺席=插件回旧行为)。模型缺席 fail-open
        #   回旧引擎+一行告警。
        _eng = _norm_engine(engine)
        if _eng == "sensevoice" and not _sv_model_dir_ok():
            _eng = ""
            print(
                f"[qwen3-asr] sensevoice engine fallback: model dir missing "
                f"({SV_MODEL_DIR}) — set QWEN3_ASR_SV_MODEL_DIR or bok download --only sensevoice",
                flush=True,
            )
        self._sessions[session_id] = {
            "chunks": bytearray(),
            "text": "",
            "language": language,
            # 热词/context(Qwen3-ASR 官方 customizable context = system message
            # 词汇表软偏置):partial/finish 每次解码透传 system_prompt。
            "context": str(context or "").strip(),
            # 会话级 partial 解码间隔档(2026-09-08 GPU 竞态专项):agent 在回复
            # 生成/播报中抬高此值抑制全窗重解抢 Metal 时间片,None=用 env 默认。
            "partial_ms": int(pm) if pm.isdigit() and int(pm) > 0 else None,
            "partial": False,
            "created_at": time.time(),
            "vllm_state": None,
            "partial_text": "",
            "partial_lang": "",
            "partial_covered": None,
            "last_partial_at": 0.0,
            "partials_done": False,
            "inf_lock": threading.Lock(),
            "engine": _eng,
        }
        # 懒清扫在插入后跑(不变量见 _sweep_sessions docstring)。
        self._sweep_sessions()
        return session_id

    def chunk(self, session_id: str, pcm: bytes) -> dict[str, str | bool]:
        session = self._sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        session["chunks"].extend(pcm)
        session["last_seen"] = time.time()

        if BACKEND == "vllm":
            self._ensure_loaded()
            wav, sr = _wav_from_pcm16(pcm)
            state = session["vllm_state"]
            if state is None:
                state = self._model.init_streaming_state(
                    unfixed_chunk_num=2,
                    unfixed_token_num=5,
                    chunk_size_sec=2.0,
                )
                session["vllm_state"] = state
            self._model.streaming_transcribe(_resample(wav, sr, SAMPLE_RATE), state)
            session["text"] = getattr(state, "text", "") or ""
            session["language"] = getattr(state, "language", "") or _fallback_language(session["text"])
            return {
                "text": session["text"],
                "language": session["language"],
                "partial": True,
            }
        # 【P1】SV 引擎:partial 滑窗同款语义,但解码在 CPU(并发让位不适用——
        # 不同芯片,不吃 MPS 时间片,partial 只按普通间隔门跑)。
        if session.get("engine") == "sensevoice":
            return self._partial_sv(session)
        if BACKEND == "mlx" and STREAM_PARTIAL:
            return self._partial_mlx(session)

        # transformers backend: batch transcribe at finish(); chunks just buffer.
        return {
            "text": session["text"],
            "language": session["language"],
            "partial": False,
        }

    # ---- P1 SV-CPU 引擎(CPU 耳朵):识别器懒加载 + 滑窗 partial + finish ----

    def _sv_model(self, lang_key: str = "auto"):
        """SenseVoice 识别器缓存(按语言键):auto/zh/en/yue 各一份懒加载。

        实弹勘误(2026-10-01 soak 首跑):auto 档把粤语短句听成日语(「おへ君か」)
        ——识别器 language 是构造期参数,按会话钉定语言各建一份(cantonese→yue),
        内存 ~4×230MB 纯 RAM(CPU)可承受;auto 仅兜未钉语言。"""
        key = lang_key if lang_key in ("auto", "zh", "en", "ja", "ko", "yue") else "auto"
        cached = self._sv_models.get(key)
        if cached is not None:
            return cached
        import sherpa_onnx

        d = Path(SV_MODEL_DIR)
        rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(d / "model.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            use_itn=True,
            language=key,
            num_threads=int(os.environ.get("QWEN3_ASR_SV_THREADS", "2")),
        )
        if not self._sv_models:
            print(
                f"[qwen3-asr] sensevoice engine ready dir={SV_MODEL_DIR} "
                f"first_lang={key} (cpu)",
                flush=True,
            )
        self._sv_models[key] = rec
        return rec

    @staticmethod
    def _sv_lang_key(session_lang: str) -> str:
        """会话语言 → SenseVoice 识别器语言键(缺省 auto)。"""
        s = str(session_lang or "").strip().lower()
        if s in ("cantonese", "yue", "粵", "粤"):
            return "yue"
        if s in ("zh", "chinese", "mandarin"):
            return "zh"
        if s in ("en", "english"):
            return "en"
        return SV_LANGUAGE if SV_LANGUAGE in ("auto", "zh", "en", "ja", "ko", "yue") else "auto"

    def _sv_decode(self, pcm: bytes, session_lang: str = "") -> str:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        rec = self._sv_model(self._sv_lang_key(session_lang))
        stream = rec.create_stream()
        stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=x)
        rec.decode_stream(stream)
        return str(getattr(stream.result, "text", "") or "")

    def _partial_sv(self, session: dict) -> dict[str, str | bool]:
        """SV 滑窗 partial:语义与 _partial_mlx 逐条对齐(同锁/同账本字段/同间隔门),
        差异两点——①解码在 CPU:不吃 MPS 时间片,不参与并发让位(×2 退避不适用);
        ②无 token 置信度(partial 本就不带,响应形状一致)。partial_text/partial
        _covered/last_partial_at 同款落账 → 增量 finish 的 zero-tail 判据面不变。
        """
        cached = {
            "text": session.get("partial_text", ""),
            "language": session.get("partial_lang", ""),
            "partial": True,
        }
        if session.get("partials_done"):
            return cached
        lock = session.setdefault("inf_lock", threading.Lock())
        if not lock.acquire(blocking=False):
            return cached
        try:
            now = time.monotonic()
            elapsed_ms = (now - float(session.get("last_partial_at") or 0.0)) * 1000
            pcm = bytes(session["chunks"])
            dur_sec = len(pcm) / 2 / SAMPLE_RATE
            interval_ms = float(session.get("partial_ms") or PARTIAL_INTERVAL_MS)
            if elapsed_ms < interval_ms:
                return cached
            if dur_sec < 0.6:
                return cached  # 太短没有转写价值,等下一窗
            capped = dur_sec > PARTIAL_MAX_SEC
            if capped:
                pcm = pcm[-int(PARTIAL_MAX_SEC * SAMPLE_RATE) * 2 :]
            text = self._sv_decode(pcm, session.get("language"))
            session["partial_prev_text"] = str(session.get("partial_text") or "")
            session["partial_text"] = text
            session["partial_lang"] = _sv_lang_label(session.get("language")) or _fallback_language(text)
            session["partial_covered"] = None if capped else len(pcm)
            session["last_partial_at"] = now
            return {"text": text, "language": session["partial_lang"], "partial": True}
        except Exception:  # noqa: BLE001 - partial 失败回 cached,finish 全量兜底
            return cached
        finally:
            lock.release()

    def _finish_sv(self, session: dict, pcm: bytes) -> dict[str, Any]:
        """SV finish:整段全量解码(45ms 级,无需增量/置信度加菜);语言标签由会话
        钉定语言映射(与 Qwen3 路径的 language 契约一致)。"""
        text = self._sv_decode(pcm, session.get("language"))
        language = _sv_lang_label(session.get("language")) or _fallback_language(text)
        return {"text": text, "language": language, "partial": False}

    def _partial_mlx(self, session: dict) -> dict[str, str | bool]:
        """滑窗 partial：说话期间每 PARTIAL_INTERVAL_MS 对累积 buffer 重推一次。

        - 忙时跳过：上一窗没跑完直接回上一窗结果，唔排队叠窗（GPU 串行，叠了也白排）。
        - FINAL 后停发：finish 打了 partials_done 标记的会话直接回缓存（见 finish 注释）。
        - buffer 裁最近 ~PARTIAL_MAX_SEC：长句重算成本线性涨；超长独白的 partial
          只反映最近窗口，/api/finish 整句高精度转写兜底唔受影响。
        - 语言提示与会话一致（cantonese 等），partial 与 final 唔会各说各话。
        """
        cached = {
            "text": session.get("partial_text", ""),
            "language": session.get("partial_lang", ""),
            "partial": True,
        }
        # FINAL 后停发:会话已了结(finish 摘除/标记),喺途迟到的 chunk 唔再排 GPU
        # 解码——防 FINAL 之后冒出过期 INTERIM/PREFLIGHT 抢跑事件。
        if session.get("partials_done"):
            return cached
        lock = session.setdefault("inf_lock", threading.Lock())
        if not lock.acquire(blocking=False):
            return cached
        try:
            # 任务 B:懒加载档下 partial 是 qwen3 路径的首个解码入口——先收敛
            # 权重就绪(_ensure_loaded 单飞;失败由其 503 抛出让下面 except 回
            # cached,finish 全量兜底),否则首个回滚会话静默丢全部 partial。
            self._ensure_loaded()
            now = time.monotonic()
            elapsed_ms = (now - float(session.get("last_partial_at") or 0.0)) * 1000
            # 解码快照:喺持锁期间、generate 之前拍照。incremental finish 的
            # partial_covered 必须以呢份快照长度为准(睇下面赋值处注释)。
            pcm = bytes(session["chunks"])
            dur_sec = len(pcm) / 2 / SAMPLE_RATE
            # 会话级档位(agent 生成中抑制)优先,无则用 env 默认。
            interval_ms = float(session.get("partial_ms") or PARTIAL_INTERVAL_MS)
            # 并发让位②:另一通推理在飞 → 间隔×2(降突发密度;final 链不受影响)。
            if _contention_yield_on() and _partial_should_skip(
                elapsed_ms, interval_ms, _others_inflight()
            ):
                return cached
            if dur_sec < 0.6:
                return cached  # 太短没有转写价值,等下一窗
            capped = dur_sec > PARTIAL_MAX_SEC
            if capped:
                pcm = pcm[-int(PARTIAL_MAX_SEC * SAMPLE_RATE) * 2 :]
            wav, sr = _wav_from_pcm16(pcm)
            with _inflight():
                out = self._model.generate(
                    _resample(wav, sr, SAMPLE_RATE),
                    language=session.get("language") or None,
                    system_prompt=_session_context(session),
                    max_tokens=int(os.environ.get("QWEN3_ASR_MAX_TOKENS", "256")),
                )
            text = getattr(out, "text", "") or ""
            langs = getattr(out, "language", None) or []
            language = str(langs[0] or "") if isinstance(langs, list) and langs else ""
            if not language:
                language = _fallback_language(text)
            # W6 稳定性账本:记上一窗文本——VAC 直转只在「连续两窗解码收敛」时放行
            # (当前窗是上一窗的延伸/相等)。单词 partial(短句只有一窗,无收敛证据)
            # 与劣化重解(快语速下 4 字跟在更全的解码后)都唔够格直转,落回整句
            # 兜底=旧守卫的质量优先行为(fast_speech 探针 3/3 FAIL 实证后加)。
            session["partial_prev_text"] = str(session.get("partial_text") or "")
            session["partial_text"] = text
            session["partial_lang"] = language
            # covered=解码快照长度(上面 bytes(chunks) 拍照),【唔系】解码完成后的
            # len(chunks):/api/finish 的 PCM body 会在锁外追加进 chunks(端点收 body
            # 唔使锁),若记当刻长度会「多认覆盖」→ finish 见 covered==len(pcm) →
            # 尾巴被当已解码直接转正 partial → 尾段语音/号码静默丢失。
            # 裁剪窗(>PARTIAL_MAX_SEC)只解了尾部 12s,头段从未进 partial →
            # covered=None,增量路径让位整句兜底(唔得被本次赋值翻案)。
            session["partial_covered"] = None if capped else len(pcm)
            session["last_partial_at"] = now
            return {"text": text, "language": language, "partial": True}
        except Exception as exc:
            # partial 失败不影响主链路：回 cached；finish 整句高精度兜底。
            return cached
        finally:
            lock.release()

    def _try_incremental_finish(
        self, session: dict, pcm: bytes, hint: str | None
    ) -> dict[str, str | bool] | None:
        """增量 finish:新鲜 partial 已覆盖 buffer 主体时,只解码尾部小段再拼接。

        partial 滑窗 ≤700ms 前刚解码过几乎同一份 buffer,finish 再整段重推是
        0.5-1.2s 的纯重复 GPU 时间。这里在 inf_lock 纪律下只解码
        partial_covered 之后的尾部(通常 ≤2-3s),FINAL = partial_text + tail_text。
        任一前提不成立 → 返回 None → 调用方整句高精度兜底:
        - 未开 QWEN3_ASR_INC_FINISH / 无 fresh partial / 窗口被 PARTIAL_MAX_SEC 裁过
        - partial 不新鲜(> FINISH_PARTIAL_FRESH_SEC)或尾段超 FINISH_TAIL_MAX_SEC
        - 锁忙超过 FINISH_LOCK_WAIT_SEC(说明还有 partial 在飞,接缝未定)
        - 接缝可疑(partial 尾字符是 latin/数字,词/号码可能被缝截断)
        - 尾段文本含数字/拉丁串(WhatsApp 捕获零降级,绝不赌号码转写)
        """
        if not INC_FINISH:
            return None
        lock = session.get("inf_lock")
        if lock is None:
            return None
        # 小等一把:让在飞的 partial 跑完(它的结果才覆盖接缝前的音频)。
        if not lock.acquire(timeout=FINISH_LOCK_WAIT_SEC):
            return None
        try:
            partial_text = str(session.get("partial_text") or "")
            covered = session.get("partial_covered")
            last_at = float(session.get("last_partial_at") or 0.0)
            if not partial_text or covered is None:
                return None
            # 抑制档(partial_ms 会话级抬高)下 partial 天生陈旧,旧 partial +
            # 无上下文长尾独立解码会拼出幻觉尾巴(2026-09-08 实证)——收紧:
            # partial 必须 ≤1.2s 新鲜、尾段 ≤1s,否则整句兜底(正确性优先)。
            fresh_sec, tail_max_sec = FINISH_PARTIAL_FRESH_SEC, FINISH_TAIL_MAX_SEC
            if session.get("partial_ms"):
                fresh_sec, tail_max_sec = min(fresh_sec, 1.2), min(tail_max_sec, 1.0)
            if (time.monotonic() - last_at) > fresh_sec:
                return None
            if covered <= 0:
                return None
            # VAC 即时提交(W6,2026-09-24):covered 越过裁剪后末端(快照落在尾部
            # 静音区)=partial 输入已含全部语音(语音结束点 ≤ trimmed_len ≤ covered)
            # → 零 GPU 直转下方 `if not tail` 分支。旧守卫 `covered <= len(pcm)` 恰
            # 把这个黄金场景打成整句兜底 0.5-1.2s(==len 精确相等反而是罕见巧合;
            # VAD 停嘴等窗 0.45s 内起解的 partial,快照天然带一段后来被 trim 掉的
            # 静音)。竞态不变量不动:covered 恒为解码快照长度(_partial_mlx 注释),
            # 快照后还有**语音**时 len(pcm)>covered 照走尾段解码——本分支只在
            # 快照之后全是被裁掉的静音时成立(trim 只裁静音,语音结束点之前唔动)。
            tail = pcm[covered:] if covered < len(pcm) else b""
            tail_sec = len(tail) / 2 / SAMPLE_RATE
            if tail_sec > tail_max_sec:
                return None
            if _seam_risky(partial_text):
                return None
            # 覆盖可信度门（2026-09-12 外呼 E2E 实证）：partial 声称覆盖 covered
            # 秒音频，但文本短到不可能是这段音频的转写 → 该窗起点落在语音中间
            # （模型只解到后半截 / 滑窗起点切片），拿它当 FINAL 头部会**静默丢掉
            # 前半句**。实证：号码句「六四三二零一一一」被听成「四三二零一一一」
            # （首字六丢失）、tail 拼接出「他。的再见。」（partial=2 字覆盖 2.6s）。
            # 语音正常节奏 ≥4 字/秒，取极保守下限 2 字/秒——正常 partial 远超此线,
            # 只有「窗起点切在语音中间」的坏窗会被挡下并退回整句高精度兜底。
            # 直转 case 的 covered 含尾部静音:以裁剪后语音长度为下限基(保守方向
            # ——静音不产字,唔可以用它抬高 min_chars 把真 partial 错杀)。
            partial_sec = min(covered, len(pcm)) / 2 / SAMPLE_RATE
            min_chars = int(partial_sec * _INC_PARTIAL_MIN_CHARS_PER_SEC)
            if len(_strip_punct_space(partial_text)) < min_chars:
                print(
                    f"[qwen3-asr] incremental rejected: partial too short "
                    f"({len(_strip_punct_space(partial_text))}ch < {min_chars}ch for "
                    f"{partial_sec:.1f}s) — falling back to full decode",
                    flush=True,
                )
                return None
            if not tail:
                # 尾巴为零的两种形态分流:
                # - covered == len(pcm):旧零尾直转(2026-09-12 行为,保持零变化);
                # - covered > len(pcm):W6 VAC 直转(快照越过裁剪后末端=落在尾部
                #   静音区)——必须有两窗收敛证据:快语速/劣化窗的 partial 解码可以
                #   比整句差(实测 1.2s 语音出 4 字,fast_speech 探针 3/3 FAIL 实证),
                #   当前窗是上一窗的延伸/相等才算收敛,否则整句兜底质量优先。
                if covered > len(pcm):
                    _prev = _strip_punct_space(str(session.get("partial_prev_text") or ""))
                    _cur = _strip_punct_space(partial_text)
                    _stable = bool(_prev) and (
                        _cur == _prev or _cur.startswith(_prev) or _prev.startswith(_cur)
                    )
                    if not _stable:
                        return None
                    _vac = " stable"
                else:
                    _vac = ""
                print(
                    f"[qwen3-asr] incremental finish: zero-tail direct "
                    f"(covered={covered / 2 / SAMPLE_RATE:.1f}s "
                    f"trimmed={len(pcm) / 2 / SAMPLE_RATE:.1f}s{_vac})",
                    flush=True,
                )
                return {
                    "text": partial_text,
                    "language": str(session.get("partial_lang") or "") or _fallback_language(partial_text),
                    "partial": False,
                }
            wav, sr = _wav_from_pcm16(tail)
            with _inflight():
                out = self._model.generate(
                    _resample(wav, sr, SAMPLE_RATE),
                    language=hint,
                    system_prompt=_session_context(session),
                    max_tokens=int(os.environ.get("QWEN3_ASR_MAX_TOKENS", "256")),
                )
            tail_text = getattr(out, "text", "") or ""
            if not tail_text.strip():
                # 有音频却转不出字:接缝可能劈在音节中间,不可信 → 整句兜底。
                return None
            if _has_latin_or_digit(tail_text):
                return None
            langs = getattr(out, "language", None) or []
            language = str(langs[0] or "") if isinstance(langs, list) and langs else ""
            if not language:
                language = str(session.get("partial_lang") or "")
            stitched = _join_stitched(partial_text, tail_text)
            if not language:
                language = _fallback_language(stitched)
            print(
                f"[qwen3-asr] incremental finish: partial={len(partial_text)}ch "
                f"tail={tail_sec:.2f}s (skipped {covered / 2 / SAMPLE_RATE:.1f}s re-decode)",
                flush=True,
            )
            return {
                "text": stitched,
                "language": language,
                "partial": False,
            }
        except Exception:
            # 增量任何一步失手都退回整句兜底,绝不因提速牺牲正确性。
            return None
        finally:
            lock.release()

    def _script_confusion_guard(
        self, out: dict, session: dict, pcm: bytes, hint: str | None
    ) -> dict:
        """泰文字形幻觉守卫(M-24,纯出口单元,finish 各路径统一收口)。

        输出含泰文块 → mlx 后端以 temperature 重解一次(同 hint/context,采样
        变化搏一次干净解);重解干净即采用,仍带泰文/无重解能力 → 剥泰文串保留
        余文。干净输出原样返回(结构性零行为变化)。QWEN3_ASR_SCRIPT_GUARD=0 关。
        """
        text = str(out.get("text") or "")
        if not _has_thai_glyphs(text):
            return out
        if os.environ.get("QWEN3_ASR_SCRIPT_GUARD", "1") != "1":
            return out
        _t0 = time.monotonic()
        retry_text = ""
        if BACKEND == "mlx" and self._model is not None:
            try:
                wav, sr = _wav_from_pcm16(pcm)
                retry = self._model.generate(
                    _resample(wav, sr, SAMPLE_RATE),
                    language=hint,
                    system_prompt=_session_context(session),
                    max_tokens=int(os.environ.get("QWEN3_ASR_MAX_TOKENS", "256")),
                    temperature=_SCRIPT_RETRY_TEMP,
                )
                retry_text = getattr(retry, "text", "") or ""
            except TypeError as exc:
                # I-2(2026-09-24 评审返工):采样 kwarg 不被当前 mlx_audio generate
                # 签名接受(升级改签名族)→ 重解路结构性不可用。真模型实证该 kwarg
                # 被接受(2026-09-24 实载 1.7B-MLX-8bit,temperature=0.25 无
                # TypeError、重解 ~0.7s)——此分支只防未来签名漂移,显式告警令
                # 降级可观测(评审项:静默退剥串=死路无痕)。
                print(
                    f"[qwen3-asr] script_guard: retry unavailable reason=TypeError "
                    f"detail={exc!r} — sampling kwarg not accepted, strip-only",
                    flush=True,
                )
                retry_text = ""
            except Exception as exc:  # noqa: BLE001 - 其余重解失败同样可观测
                print(
                    f"[qwen3-asr] script_guard: retry unavailable reason=error "
                    f"detail={exc!r} — strip-only",
                    flush=True,
                )
                retry_text = ""
        if retry_text.strip() and not _has_thai_glyphs(retry_text):
            print(
                f"[qwen3-asr] script_guard: thai confusion → retry clean "
                f"({(time.monotonic() - _t0) * 1000:.0f}ms) text={retry_text[:24]!r}",
                flush=True,
            )
            return {
                "text": retry_text,
                "language": str(out.get("language") or "") or _fallback_language(retry_text),
                "partial": False,
            }
        cleaned = _strip_thai_runs(text)
        print(
            f"[qwen3-asr] script_guard: thai confusion stripped "
            f"chars={len(text)}→{len(cleaned)} retry={'still_thai' if retry_text else 'n/a'}",
            flush=True,
        )
        return {
            "text": cleaned,
            "language": str(out.get("language") or ""),
            "partial": False,
        }

    def _generate_conf(
        self,
        audio,
        *,
        language: str | None,
        system_prompt: str | None,
        max_tokens: int,
    ) -> tuple[str, str, dict | None]:
        """整句解码并逐 token 采集 top1 概率,返回 (text, language, confidence)。

        直调 stream_generate(sampler=greedy) 复刻旧 generate() 的单 chunk 顺序路径:
        同 language hint / system_prompt / max_tokens,同样 skip_special_tokens 解码,
        A 线恒带 language hint → 文本与旧路径逐字节一致;hint 为空(auto 档)时同款
        extract_language 解析剥离 language 前缀。任何签名不符/模型不支持由调用方
        fail-open(本函数不吞异常,让 _generate_out_with_conf 归因打点)。
        """
        model = self._model
        # 复刻旧 generate() 的单 chunk 输入准备:split_audio_into_chunks 对
        # <min_chunk_duration(1.0s) 的片段补零到 1s(≥1s 原样)。A 线 finish 片段
        # 恒 ≤ chunk_duration(1200s),故此分支等价于整条 chunk 路径,保文本一致。
        sr_model = int(getattr(model, "sample_rate", 0) or 0) or SAMPLE_RATE
        arr = np.asarray(audio)
        if arr.shape[0] < sr_model:
            arr = np.pad(arr, (0, sr_model - arr.shape[0]))
        sampler = _make_greedy_sampler()
        tokens: list[int] = []
        probs: list[float] = []
        for token, logprobs in model.stream_generate(
            arr,
            max_tokens=max_tokens,
            sampler=sampler,
            language=language,
            system_prompt=system_prompt,
        ):
            tokens.append(int(token))
            probs.append(_top1_prob(logprobs))
        text = model._tokenizer.decode(tokens, skip_special_tokens=True)
        language_out = str(language or "")
        if language is None:
            # 与 generate() 同款:auto 档输出带 `language X<asr_text>` 前缀,解析剥离。
            language_out, text = model.extract_language(text)
        return text, language_out, _aggregate_conf(probs)

    def _generate_out_with_conf(
        self, wav, sr: int, session: dict, hint: str | None
    ) -> tuple[str, str, dict | None]:
        """finish 整句解码单点:置信度开→流式采集;任何失手→fail-open 回旧路。

        fail-open 覆盖签名漂移/模型不支持/推理异常:打 `ASR_CONF fallback` 一行后
        回退旧 `self._model.generate()`,置信度 None。kill-switch 关时直接走旧路且
        调用方不带键(响应形状与旧版逐字节相同的保证在 _with_confidence)。
        """
        max_tokens = int(os.environ.get("QWEN3_ASR_MAX_TOKENS", "256"))
        ctx = _session_context(session)
        audio = _resample(wav, sr, SAMPLE_RATE)
        if _CONF_ENABLED:
            try:
                with _inflight():
                    return self._generate_conf(
                        audio, language=hint, system_prompt=ctx, max_tokens=max_tokens
                    )
            except Exception as exc:  # noqa: BLE001 - 任何失手都 fail-open
                print(
                    f"[qwen3-asr] ASR_CONF fallback reason={exc!r}",
                    flush=True,
                )
        with _inflight():
            out = self._model.generate(
                audio, language=hint, system_prompt=ctx, max_tokens=max_tokens
            )
        text = getattr(out, "text", "") or ""
        langs = getattr(out, "language", None) or []
        language = ""
        if isinstance(langs, list) and langs:
            language = str(langs[0] or "")
        return text, language, None

    @staticmethod
    def _with_confidence(result: dict, confidence: dict | None) -> dict:
        """响应加 confidence 键(纯加键向后兼容);kill-switch 关=旧形状零变化。

        新建 dict 不原地改 result——调用方可能复用同一 dict(script_guard 等),
        原地加键会污染其形状。
        """
        if not _CONF_ENABLED:
            return result
        return {**result, "confidence": confidence}

    def finish(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.pop(session_id, None)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        # FINAL 后停发:会话已从 _sessions 摘除(新 chunk 会 404),呢度再打标记,
        # 让「pop 前已拿到 session 引用」的喺途 chunk 调用也停发 partial——
        # FINAL 之后唔再有解码排 GPU,也唔会吐过期 INTERIM 抢跑事件。
        session["partials_done"] = True
        # 【P1】SV 车道不依赖 Qwen3 权重(独立识别器)——ensure 只对旧引擎跑,
        # 免得「只装了 SV 模型」的部署被 503 拦住。
        if session.get("engine") != "sensevoice":
            self._ensure_loaded()

        if BACKEND == "vllm":
            state = session["vllm_state"]
            if state is not None:
                self._model.finish_streaming_transcribe(state)
                text = getattr(state, "text", "") or ""
                language = getattr(state, "language", "") or ""
                if not language:
                    language = _fallback_language(text)
                # M-2(2026-09-24 评审返工):vllm 出口同款收口进泰文守卫
                # (剥串档;重解路 guard 内 BACKEND==mlx 才有,pcm 传空即不适用)。
                return self._with_confidence(
                    self._script_confusion_guard(
                        {"text": text, "language": language, "partial": False},
                        session, b"", _finish_language_hint(session.get("language")),
                    ),
                    None,  # vllm 流式路径不产 token 级置信度
                )

        pcm = bytes(session["chunks"])
        if len(pcm) < 2:
            return self._with_confidence(
                {"text": "", "language": "", "partial": False}, None
            )
        # EOT 卫生:先裁掉尾部静音再解码(VAD min_silence 0.45s + 推理尾巴不该烧 GPU),
        # 整句与增量两条路径都受益。
        pcm = _trim_trailing_silence(pcm)
        wav, sr = _wav_from_pcm16(pcm)
        # 语言提示:由会话 start 语言归一(en/english→"English",cantonese 透传,
        # zh/空=auto);整句与增量尾段两条 generate 路径共用同一 hint。
        hint = _finish_language_hint(session.get("language"))
        # 【P1】SV 引擎 finish:整段全量解码(CPU 45ms 级,无增量/置信度分支),
        # 泰文串档守卫同款收口(pcm 真值在手)。
        if session.get("engine") == "sensevoice":
            return self._with_confidence(
                self._script_confusion_guard(
                    self._finish_sv(session, pcm), session, pcm, hint
                ),
                None,  # SV 无 token 级置信度(插件按缺席回旧行为)
            )
        if BACKEND == "mlx":
            # 增量 fast path:新鲜 partial 已覆盖 buffer 主体 → 只解码尾巴再拼接;
            # 任一前提不成立则回退整句高精度兜底(WhatsApp 捕获零降级)。
            # 短轮强开整句(仅 confidence 开启时):增量路径不产置信度,而短碎片轮
            # 恰是新重问车道需要真信号的地方 → 裁剪后语音 ≤ _FULL_DECODE_MAX_S 时
            # 跳过增量直走整句 `_generate_out_with_conf`。决策异常一律 fail-open
            # 回原增量行为;长 buffer 增量快路逐字节不变。
            try:
                _force_full = _CONF_ENABLED and _needs_full_decode(
                    len(pcm) / 2 / SAMPLE_RATE
                )
            except Exception:  # noqa: BLE001 - fail-open 回原行为
                _force_full = False
            # 并发让位①:短轮强制整句是置信度专用加菜(增量路径文本质量等价,
            # 只缺置信度)——另一通推理在飞时跳过,把 GPU 槽让给对方的关键解码。
            if _force_full and _contention_yield_on() and _others_inflight():
                print(
                    f"[qwen3-asr] finish contention-skip short-full "
                    f"pcm_s={len(pcm) / 2 / SAMPLE_RATE:.1f}",
                    flush=True,
                )
                _force_full = False
            if not _force_full:
                incremental = self._try_incremental_finish(session, pcm, hint)
                if incremental is not None:
                    # 增量路径无整句解码(partial 已覆盖主体),不产置信度——带键为 None,
                    # 响应形状与整句路径统一。
                    return self._with_confidence(
                        self._script_confusion_guard(incremental, session, pcm, hint), None
                    )
            text, language, confidence = self._generate_out_with_conf(
                wav, sr, session, hint
            )
            if not language:
                language = _fallback_language(text)
            result = self._script_confusion_guard(
                {"text": text, "language": language, "partial": False},
                session, pcm, hint,
            )
            return self._with_confidence(result, confidence)
        result = self._model.transcribe(
            audio=(_resample(wav, sr, SAMPLE_RATE), SAMPLE_RATE),
            language=hint,
        )
        if not result:
            return self._with_confidence(
                {"text": "", "language": "", "partial": False}, None
            )
        first = result[0]
        text = getattr(first, "text", "") or ""
        language = getattr(first, "language", "") or ""
        if not language:
            language = _fallback_language(text)
        return self._with_confidence(
            self._script_confusion_guard(
                {"text": text, "language": language, "partial": False},
                session, pcm, hint,
            ),
            None,  # transformers 后端不产 token 级置信度
        )

service = ASRService()

# 滑窗 partial(说话期间边说边出文字):mlx 后端每 PARTIAL_INTERVAL_MS 对累积
# buffer 重推一次。QWEN3_ASR_STREAM=0 一键回退纯离线模式(只缓冲,finish 才转写)。
STREAM_PARTIAL = os.environ.get("QWEN3_ASR_STREAM", "1") == "1"
# 400→700ms:partial 只喂字幕/抢跑前缀,滑窗每次都近乎整段重算,400ms 档说话
# 中途就叠窗排队烧 GPU、还把 FINAL 挤到后面(增量 finish 接缝也要小等在飞窗);
# 700ms 让每窗解码充分跑完,FINAL 到达反而更早。
PARTIAL_INTERVAL_MS = float(os.environ.get("QWEN3_ASR_PARTIAL_MS", "700"))
# 长句重算成本随 buffer 线性涨:partial 只裁最近 ~12s(25→12:超 12s 的独白
# partial 解码已贴着秒级预算,再长纯烧 GPU,字幕价值为负);finish 整句高精度
# 兜底不受影响(裁剪窗 covered=None,增量让位整句)。
PARTIAL_MAX_SEC = float(os.environ.get("QWEN3_ASR_PARTIAL_MAX_SEC", "12"))
# 增量 finish:partial 已覆盖 buffer 主体时只解码尾部小段再拼接(RCA-1 主刀)。
# QWEN3_ASR_INC_FINISH=0 一键回退整句重解码;安全阀全部 env 可调。
INC_FINISH = os.environ.get("QWEN3_ASR_INC_FINISH", "1") == "1"
FINISH_PARTIAL_FRESH_SEC = float(os.environ.get("QWEN3_ASR_PARTIAL_FRESH_SEC", "2.5"))
FINISH_TAIL_MAX_SEC = float(os.environ.get("QWEN3_ASR_FINISH_TAIL_MAX_SEC", "3.0"))
FINISH_LOCK_WAIT_SEC = float(os.environ.get("QWEN3_ASR_FINISH_LOCK_WAIT", "1.0"))
# 0.3→1.0(2026-09-28):锁忙超时=放弃增量捷径整段重解 ≤12s buffer——EOU 抖刺
# 实测相关 r=0.82,长句 partial 在飞普遍 >0.3s(6 通 EOU 尾程 1-2.2s 增长);
# finish 本就终态轮次,多等 ≤0.7s 换掉 1-2s 的整段重解,净赚。
# finish 尾部静音裁剪:20ms 帧 RMS 门限(≈-54dBFS),最多裁 TRIM_MAX_SEC。
TRIM_RMS = float(os.environ.get("QWEN3_ASR_TRIM_RMS", "0.002"))
TRIM_MAX_SEC = float(os.environ.get("QWEN3_ASR_TRIM_MAX_SEC", "2.0"))
# 句级置信度 kill-switch(默认 "1" 开):"0"=finish 走旧 generate() 路径,且响应
# 不带 confidence 键(形状与旧版逐字节相同的快速回退档)。读法同全仓惯例。
_CONF_ENABLED = os.environ.get("QWEN3_ASR_CONFIDENCE", "1") == "1"

@app.on_event("startup")
def _startup() -> None:
    # 任务 B（2026-10-01）:BOK_ASR_ENGINE=sensevoice 档（含缺省/未设——与 agent
    # `_asr_engine_from_cfg` 缺省对齐）跳过 Qwen3-1.7B GPU 权重 eager 加载
    # （审计实锤 asr.log "loaded ... device=gpu"，~1.9GB 纯占卡零消费——默认
    # 部署 ASR 全走 CPU 车道）。首个真走 qwen3 路径的请求由 _ensure_loaded
    # 懒加载，回滚 BOK_ASR_ENGINE=qwen3 免重启；显式 qwen3=旧行为逐字节不变。
    if _boot_engine() == "sensevoice":
        service._qwen3_deferred = True
        print("ASR_QWEN3_SKIPPED engine=sensevoice", flush=True)
    else:
        service.load()

@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "backend": BACKEND,
        "model": MODEL_PATH,
        "model_ready": service._model is not None,
        "load_error": service._load_error,
        # 任务 B:跳载档=true（model_ready=False 是设计态不是故障）;首个 qwen3
        # 路径请求触发懒加载后翻 false。
        "qwen3_deferred": service._qwen3_deferred,
    }

@app.post("/api/start")
async def start(
    language: str = "",
    context: str = "",
    partial_ms: str = "",
    engine: str = "",
) -> dict[str, str]:
    # language: 可选转写语言提示("cantonese"/"Chinese"/"English")。agent 按每通
    # 对话钉定语言传入(A 线通话/B 线同传三语全钉),强制模型按该语言转写
    # (cantonese 不钉会被 auto 误判成普通话);留空 = 交给模型 auto。
    # context: 热词/词汇表(system message 软偏置,Qwen3-ASR 官方 customizable
    # context 通道),agent 按话术领域词+对象文字字段组装;QWEN3_ASR_CONTEXT=0 关。
    # partial_ms: 会话级 partial 解码间隔档(agent 生成中抑制,GPU 竞态专项);
    # 空值=env 默认。
    # engine: P1(2026-10-01) 会话级引擎——""/qwen3=旧 Qwen3 路径,
    # sensevoice=CPU 车道(三语过门,见 _norm_engine 档案)。
    return {
        "session_id": service.start(
            language=language.strip(),
            context=context.strip(),
            partial_ms=partial_ms.strip(),
            engine=engine.strip(),
        )
    }


@app.post("/api/partial_ms")
async def tune_partial_ms(session_id: str, ms: str = "") -> dict[str, object]:
    """已开会话即时调 partial 解码间隔档(2026-09-08 GPU 竞态专项)。

    agent 在回复生成/播报中(thinking/speaking)抬档抑制,listening 恢复。
    ms 空/非法=恢复 env 默认;会话不存在=404。
    """
    session = service._sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    pm = str(ms or "").strip()
    session["partial_ms"] = int(pm) if pm.isdigit() and int(pm) > 0 else None
    return {"ok": True, "partial_ms": session["partial_ms"]}

@app.post("/api/chunk")
async def chunk(session_id: str, request: Request) -> dict[str, str | bool]:
    pcm = await request.body()
    # mlx partial 推理是秒级内的阻塞计算,丢线程池跑,唔阻塞事件循环(并发会话共用 loop)。
    return await run_in_threadpool(service.chunk, session_id, pcm)

@app.post("/api/finish")
async def finish(session_id: str, request: Request) -> dict[str, Any]:
    # 兼容两种调用:①逐块 chunk 攒到会话缓冲,finish 无 body;②agent 整包上传——
    # PCM body 直接在 finish 带过来,优先用 body(避免 2-6s 语音被拆成几十次小 HTTP)。
    body = await request.body()
    if body:
        session = service._sessions.get(session_id)
        if session is not None:
            session["chunks"].extend(body)
    # 增量路径要在 inf_lock 上小等在飞 partial(≤FINISH_LOCK_WAIT_SEC),解码本身也是
    # 秒级阻塞计算——与 /api/chunk 同款丢线程池,唔阻塞事件循环(并发会话共用 loop)。
    # 墙钟打点(W6,2026-09-24):finish 往返是 EOU 延迟的直接组分(plugin 侧实测
    # ~0.15-0.3s),分布观测/AB 归因靠它——zero-tail direct 应见个位数 ms。
    _t0 = time.monotonic()
    out = await run_in_threadpool(service.finish, session_id)
    print(
        f"[qwen3-asr] finish wall={(time.monotonic() - _t0) * 1000:.0f}ms "
        f"text={len(out.get('text') or '')}ch",
        flush=True,
    )
    return out
