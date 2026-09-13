"""LLM 慢生成轮垫话(2026-09-09 PR-2;2026-09-10 资产化改版;task-14a 人设音色双层)。

垫话=随源码分发的固定资产:固定音色+固定参数(见 scripts/gen_filler_assets.py)
经 MiniMax 预生成 wav,连同 manifest.json 提交在 assets/fillers/——运行时只播
文件,与人设音色/tts_cache 状态解耦(人设一换也永不哑)。

人设音色双层(task-14a,铁律「全场同一音色」):选句后先查 tts_cache 按运行时
人设 voice/model 物化的版本(cache.lookup,命中=与通话完全同人声,打点
voice_hit=1);miss 播源码资产兜底(永不哑,打点 voice_fallback=运行时提醒
「新人设缺垫话物化」)+ asyncio 后台调 backfill(注入的 CachedTTS.synthesize
消费 tee 自动落盘,本模块不碰云 API)把该句用运行时音色补物化进缓存——
同人设下一通起命中。cache/resolver/backfill 任一缺失=纯资产模式(旧行为)。

万能话术原则(2026-09-10):垫话随机触发,语境永不匹配——只保留注意力应承
(好的/收到/明白/嗯)与等待邀请(稍等/我看下),零动作动词;改话术=改生成脚本
重新生成,一个 PR。

播放排序(2026-09-10 用户拍板):垫话一旦开播必须播完。真回复首音频到达时
不再掐垫话,而是 _RelaySynthesizeStream 向 hold_if_playing() 询问扣压时长
(垫话剩余+BOK_FILLER_GAP_MS 默认 300ms),帧缓冲到点再放——垫话→静默→回复
自然衔接。新用户轮(cancel)仍立即掐垫话:用户插话优先,放完旧垫话反而怪。

链发(2026-09-10):首条垫话播完、回复首音频仍未到 → 垫话后残余静默照旧,
BOK_FILLER_GAP_MS 呼吸后自动补第二发——挂「播完观察者」等官方
PlayHandle.wait_for_playout()(播完即醒,精确补位,不靠估时长)。每轮封顶
1 次(_chain_depth),链发消耗 BOK_FILLER_MAX 同一计数;BOK_FILLER_CHAIN=0 关。

通道铁律(不变):垫话走 BackgroundAudioPlayer out-of-band 音轨,绝不能走
session.say()——livekit 1.8 speech 队列严格串行,垫话必排回复后(实机实证)。
垫话不进 LLM 上下文(out-of-band 不入 chat_ctx,KV 前缀/回声锚零污染)。
每通限次 BOK_FILLER_MAX(默认 3)+随机不重样;BOK_FILLER=0 一键全关。

语言铁律(2026-09-10 实证修复):垫话语言=会话装配时钉死的通话语言,构造时
由调用方捕获传入——en 通话曾因运行时 lang 状态漂移落回 zh 池,英国腔通话里
放出普通话「我看一下哈」(用户实测怪声根因)。池缺失=明文日志跳过,绝不跨
语言发声。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import wave
from pathlib import Path

FILLER_ASSETS_DIR = Path(__file__).resolve().parent / "assets" / "fillers"

# livekit BackgroundAudioPlayer 内部音轨固定 48k(AudioSource(48000)+AudioMixer(48000),
# agents 1.8.0 源码),且 AudioMixer 无重采样——垫话两层(资产 wav 24k/tts-cache pcm 24k)
# 直进=2 倍速升调「机器人声」(2026-09-11 实机实证),播放前必须对齐此速率。
BACKGROUND_PLAYER_RATE = 48000


def resample_pcm(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    """s16le 单声道线性插值重采样(from==to/空输入原样返回)。

    24k→48k 偶数位保原样本、奇数位插值;任意比率按时长守恒。numpy 向量化
    (~50ms/条,off 关键路径——arm 后 500ms 定时器期外的一次性开销)。"""
    if from_rate == to_rate or not pcm:
        return pcm
    import numpy as np

    s = np.frombuffer(pcm, dtype=np.int16)
    n = len(s)
    if n == 0:
        return pcm
    out_n = max(1, int(round(n * to_rate / from_rate)))
    if out_n == 1:
        return s[:1].tobytes()
    # 时间基索引(pos=i*from/to,非跨度基):2× 上采样时 step 恰为 0.5,偶数位
    # 严格保原样本——零相位偏移,时轴与源严格对齐。
    idx = np.arange(out_n, dtype=np.float64) * (from_rate / to_rate)
    i0 = np.minimum(idx.astype(np.int64), n - 1)
    frac = np.clip(idx - i0, 0.0, 1.0)
    i1 = np.minimum(i0 + 1, n - 1)
    out = (s[i0].astype(np.float64) * (1.0 - frac) + s[i1].astype(np.float64) * frac)
    return out.astype(np.int16).tobytes()


def filler_enabled() -> bool:
    return os.environ.get("BOK_FILLER", "1") == "1"


def filler_delay_s() -> float:
    try:
        return max(0.0, int(os.environ.get("BOK_FILLER_DELAY_MS", "500")) / 1000)
    except ValueError:
        return 0.5


def filler_gap_s() -> float:
    """垫话播完 → 回复衔接前的静默间隔。

    默认 300-600ms 均匀随机(2026-09-12 用户定档:固定值机械感,逐次随机更
    自然;垫话播完才放行回复由 hold 时间轴契约保证,间隔只是呼吸窗)。
    BOK_FILLER_GAP_MS 显式设置=固定值 kill-switch(测试/调档用)。"""
    env = os.environ.get("BOK_FILLER_GAP_MS", "").strip()
    if env:
        try:
            return max(0.0, int(env) / 1000)
        except ValueError:
            pass
    return random.uniform(0.3, 0.6)


_PAUSE_MARK_RE = re.compile(r"<#\d+(?:\.\d+)?#>")


def _strip_pause_marks(text: str) -> str:
    """剥 MiniMax 停顿标记(<#0.3#>)——字幕/turns 账本等展示面用。

    标记是合成指令(部分垫话句内嵌微停顿),缓存键/backfill 查找必须用原文
    (物化时同文本同键);原样进字幕或 turns =用户可见的指令泄漏。
    """
    return _PAUSE_MARK_RE.sub("", str(text or ""))


def filler_max_per_call() -> int:
    # 默认 6(2026-09-13 乙节罐头体系:确定性命中后 12 次只会放大复读感——垫话
    # 是补丁不是台词;6 发覆盖最差慢轮,同条目 per_call_cap 再防同语境连击)。
    try:
        return max(0, int(os.environ.get("BOK_FILLER_MAX", "6")))
    except ValueError:
        return 6


def filler_match_enabled() -> bool:
    """垫话罐头确定性匹配总闸(BOK_FILLER_MATCH,默认开;0=回退纯分类器随机池)。"""
    return os.environ.get("BOK_FILLER_MATCH", "1") == "1"


def filler_match_threshold() -> float:
    # 0.42(2026-09-13 实机校准:ASR 变体「张单↔账单」下最佳 trigger 得分 0.48,
    # 旧 0.55 全 miss;垫话有分类器+资产双层兜底,宁 hit 勿 miss——比 QA 快路
    # 的 0.90 宽松一个档位是设计本意)。
    try:
        return max(0.0, min(1.0, float(os.environ.get("BOK_FILLER_MATCH_THRESHOLD", "0.42"))))
    except ValueError:
        return 0.42


# ---- 五类分类器(2026-09-13 乙节回退层) ----
# 只看客户上一句(垫话在回复生成前就要选出,那是唯一可靠上下文)。优先级
# B安抚 > A确认接收 > C查证 > D短应承 > E默认。铁律:A/B/D/E 类垫话零动作
# 动词(治 285 条配对实证「问赔多少→马上查」式穿帮,动作词只准进 C 类——
# 客户真的在要进度时才承诺查)。
_FILLER_CAT_EMPATHY_RE = re.compile(
    r"(投诉|投訴|嬲|闹|鬧|爛|烂|冇到|未到|太耐|太长|太長|激气|激氣|生气|生氣|着急|著急|过分|過分|"
    r"complain|unacceptable|too slow|frustrat)",
    re.IGNORECASE,
)
_FILLER_CAT_ACK_RE = re.compile(
    r"(WhatsApp|微信|WeChat|單號|单号|运单|運單|淘宝|淘寶|拼多多|京東|京东|天猫|天貓|"
    r"小红书|小紅書|亚马逊|亞馬遜|顺丰|順豐|eBay|Amazon|Temu|UnionPay|银联|銀聯)",
    re.IGNORECASE,
)
_FILLER_CAT_CHECK_RE = re.compile(
    r"(查询|查詢|查下|查一下|边度|邊度|几时|幾時|几多|幾多|多久|点解|點解|点样|點樣|进度|進度|到未|"
    r"怎么|如何|为什么|為什麼|check|track|where.*order|when|how much|how many|how long)",
    re.IGNORECASE,
)
_FILLER_CAT_MINIMAL_RE = re.compile(
    r"^[嗯啊哦好的呀呢嘅啦喇系係得对對\s。.!！~～]+$", re.IGNORECASE
)


def classify_filler_category(user_text: str) -> str:
    """客户上一句 → 垫话场景五类之一(empathy/ack/check/minimal/default)。

    纯函数、三语共用;数字串(报号码轮)归 ack——确认收到,别催别查。
    minimal=纯应承词/语气粒子组合(「嗯」「好的呀」「好啦」)。
    """
    t = str(user_text or "").strip()
    if not t:
        return "default"
    if len(t) <= 6 and _FILLER_CAT_MINIMAL_RE.match(t):
        return "minimal"
    if _FILLER_CAT_EMPATHY_RE.search(t):
        return "empathy"
    if re.search(r"\d{4,}", t) or _FILLER_CAT_ACK_RE.search(t):
        return "ack"
    if _FILLER_CAT_CHECK_RE.search(t):
        return "check"
    return "default"


def filler_chain_enabled() -> bool:
    """首条垫话播完回复仍未出声 → 自动补第二条。

    默认关(2026-09-12 用户实测「重复的垫话」):暖轮回复 1.5-2s,首条垫话
    ~1.5s 播完时回复几乎必未到,链发=每轮固定双发,体感重复啰嗦;单条+随机
    gap 已足够衔接。BOK_FILLER_CHAIN=1 恢复。"""
    return os.environ.get("BOK_FILLER_CHAIN", "0") == "1"


def filler_backfill_enabled() -> bool:
    """miss 播资产后异步把该句用运行时人设音色补物化进缓存(BOK_FILLER_BACKFILL,默认开)。"""
    return os.environ.get("BOK_FILLER_BACKFILL", "1") == "1"


def load_manifest(assets_dir: Path) -> dict[str, list[dict]]:
    """manifest.json → {lang: [{text, file, dur_s}, ...]}。"""
    p = Path(assets_dir) / "manifest.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    return {
        str(lang): [e for e in entries if e.get("file")]
        for lang, entries in data.items()
        if entries
    }


def load_wav_pcm(path: Path) -> tuple[bytes, int]:
    """stdlib wave → (pcm16 bytes, sample_rate)。只收 mono/16bit(生成端保证)。"""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError(f"unexpected wav layout: {path.name}")
        return w.readframes(w.getnframes()), w.getframerate()


def _hybrid_cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return num / max(1e-9, na * nb)


class FillerEntryIndex:
    """垫话罐头索引(镜像 qa_gate.QaIndex):triggers=用户口吻示例句,HybridLexical
    打分(0.6×余弦+0.4×子串,与 QA 快路同款词法),分类器场景一致给 +0.15 加分。

    确定性铁律(用户拍板:随机抽签才是机器感):同输入永远同条目——打分取
    (score, priority, id) 严格排序的 top-1,并列时 priority 高、id 字典序小者胜。
    """

    def __init__(self, entries: list[dict]):
        from bok_voice_core.embeddings import HybridLexicalEmbedding
        from bok_voice_core.qa_text import normalize_question

        self._embed = HybridLexicalEmbedding(512)
        self._norm = normalize_question
        self._items: list[dict] = []
        for e in entries or []:
            text = str(e.get("text") or "").strip()
            if not text:
                continue
            try:
                triggers = json.loads(str(e.get("triggers") or "[]"))
            except Exception:  # noqa: BLE001 - triggers 坏 JSON 当空
                triggers = []
            trig: list[str] = []
            vecs: list[list[float]] = []
            for t in triggers:
                q = normalize_question(str(t or ""))
                if not q:
                    continue
                try:
                    vecs.append(self._embed.embed([q])[0])
                except Exception:  # noqa: BLE001 - 单条向量失败跳过该 trigger
                    continue
                trig.append(q)
            self._items.append(
                {
                    "e": e,
                    "text": text,
                    "trig": trig,
                    "vecs": vecs,
                    "priority": int(e.get("priority") or 0),
                    "cap": max(1, int(e.get("per_call_cap") or 2)),
                    "lang": str(e.get("lang") or ""),
                    "cat": str(e.get("category") or "default"),
                }
            )

    def __len__(self) -> int:
        return len(self._items)

    def match(
        self,
        user_text: str,
        *,
        lang: str = "",
        classifier_cat: str = "",
        used: dict | None = None,
        threshold: float | None = None,
    ) -> tuple[dict | None, float]:
        """返回 (命中条目原 dict, 得分);used={id: 已用次数} 做 per_call_cap 淘汰。"""
        q = self._norm(str(user_text or ""))
        if not q or not self._items:
            return None, 0.0
        try:
            qv = self._embed.embed([q])[0]
        except Exception:  # noqa: BLE001 - embed 失败=回退分类器
            return None, 0.0
        q_low = q.lower()
        thr = filler_match_threshold() if threshold is None else threshold
        best: dict | None = None
        best_key: tuple | None = None
        best_score = 0.0
        for it in self._items:
            if lang and it["lang"] and it["lang"] != lang:
                continue
            eid = str(it["e"].get("id") or "")
            if used is not None and eid and used.get(eid, 0) >= it["cap"]:
                continue
            score = 0.0
            for tq, tv in zip(it["trig"], it["vecs"]):
                s = 0.6 * _hybrid_cos(qv, tv)
                # 子串奖励双向(2026-09-13 实机实证:只认 user⊂entry 是把 QA 快路的
                # 「超集句」缺口照搬过来——垫话场景恰恰相反,词条短、用户句长
                # (「唔该帮我查下张单到边度」⊃trigger「帮我查下张单」),单向恒 0 分)。
                tq_low = tq.lower()
                if q_low and q_low in tq_low:
                    s += 0.4 * (len(q_low) / max(1, len(tq)))
                elif tq_low and tq_low in q_low:
                    s += 0.4 * (len(tq_low) / max(1, len(q_low)))
                score = max(score, s)
            if classifier_cat and classifier_cat == it["cat"]:
                score += 0.15
            key = (round(score, 6), it["priority"], "-" + eid)  # id 取负字典序=小者胜
            if score >= thr and (best_key is None or key > best_key):
                best, best_key, best_score = it["e"], key, score
        if best is not None:
            return best, best_score
        return None, best_score


class FillerDirector:
    """垫话编排:arm(轮提交后) → 定时器 → 资产命中即播;首音频回调 → 只作废
    定时器(垫话播完),回复帧由 tts_cache hold 到「垫话完+gap」。

    纯编排无业务判定——「该不该垫」的轮次资格由调用方决定(hook 正常返回才
    arm;closing/WA 步由 guards 回调在开火前最后一刻复核)。
    """

    def __init__(
        self,
        session,
        *,
        lang_resolver,
        player=None,
        guards=None,
        assets_dir: Path | None = None,
        cache=None,
        voice_model_resolver=None,
        backfill=None,
        speed_resolver=None,
        report=None,
        caption=None,
        entries_index=None,
        user_text_provider=None,
        entry_hit=None,
    ) -> None:
        self._session = session
        self._lang_resolver = lang_resolver
        # BackgroundAudioPlayer(livekit 官方 out-of-band 音轨):垫话唯一合法通道。
        # None=垫话整体失效。
        self._player = player
        self._guards = guards or (lambda: False)
        self._assets = Path(assets_dir) if assets_dir else FILLER_ASSETS_DIR
        # 缓存键语速维度(W2):resolver 取合成时点语速(zh/粤 1.2),缺省 1.0=
        # 旧键语义;miss 落资产层与语速无关。
        self._speed_resolver = speed_resolver or (lambda: 1.0)
        # turns 账本上报(W3):开火即报(line, dur_s)——垫话 out-of-band 不进
        # 转写/字幕,账本是它唯一的可见性出口(gen=filler)。None=零行为变化。
        self._report = report
        # 字幕回调(F4,2026-09-11 用户点名):开火即发文本——agent 侧转
        # lk.transcription 数据包(官方组件聚合通道),不进 chat_ctx 零 LLM 污染。
        self._caption = caption
        # 垫话罐头确定性匹配(2026-09-13 乙节):entries_index=FillerEntryIndex
        # (CP filler_entries,装配时拉取);user_text_provider=客户上一句(开火
        # 时点取值);entry_hit=命中计数回调(fire-and-forget)。任一缺失或
        # BOK_FILLER_MATCH=0 → 纯分类器+资产池(既有行为)。
        self._entries_index = entries_index
        self._user_text_provider = user_text_provider
        self._entry_hit = entry_hit
        self._entry_used: dict[str, int] = {}  # 条目 id → 本通已用次数(per_call_cap)
        # 人设音色双层(task-14a):cache=TtsAudioCache(lookup 运行时人设 voice/model
        # 的物化版,命中=与通话完全同人声);voice_model_resolver=() -> (voice, model),
        # 异常/空值=纯资产;backfill=async(text) 补物化执行体——由 agent 侧注入
        # CachedTTS.synthesize 消费(miss tee 自动落盘),本模块不碰云 API。
        # 三者任一缺失=纯资产模式(既有行为零变化)。
        self._cache = cache
        self._voice_model_resolver = voice_model_resolver
        self._backfill = backfill
        self._backfill_tasks: set[asyncio.Task] = set()  # 持强引用防 GC,完成自弃
        self._manifest: dict[str, list[dict]] | None = None
        self._timer: asyncio.Task | None = None
        self._handle = None
        self._cur_dur = 0.0
        self._play_started = 0.0
        self._fired_lines: list[str] = []  # 已实际播放(审计/探针断言用)
        self._recent: list[str] = []  # 已选取(含未播出),防相邻重复
        self._count = 0
        self._chain_task: asyncio.Task | None = None
        self._chain_depth = 0  # 本轮已链发次数(每轮封顶 1)
        self._reply_audio_seen = False  # on_reply_first_audio 置位,新轮/arm 重置

    # ---- 生命周期 ----

    def arm(self) -> None:
        """轮提交、确认走 LLM 正常路径后调用;重复 arm 先作废旧定时器/链发。"""
        self._cancel_timer()
        self._cancel_chain()
        if not filler_enabled() or self._count >= filler_max_per_call():
            return
        if self._player is None:
            return
        delay = filler_delay_s()
        if delay <= 0:
            return
        self._timer = asyncio.create_task(self._fire(delay))

    def on_reply_first_audio(self) -> None:
        """真回复首音频(CachedTTS stream 回调):只作废定时器。

        在播垫话**不掐**——播放排序契约=垫话播完→gap→回复;扣压由
        tts_cache._RelaySynthesizeStream 向 hold_if_playing() 询时实现。
        置位 _reply_audio_seen:链发观察者醒来时据此放弃补第二发。
        """
        self._reply_audio_seen = True
        self._cancel_timer()

    def hold_if_playing(self) -> float:
        """回复首帧应扣压的秒数:垫话时间轴(开播+时长+gap)内=剩余量。

        2026-09-11 用户复测实证「垫话→回复衔接生硬」:回复恰在垫话播完后到达时,
        旧实现(handle done 即 0)零间隔硬接——现在播完后仍保住余下 gap 窗,
        最小间隔契约=垫话结束→回复出声 ≥ gap;cancel(用户插话)清窗不扣压。"""
        if not self._play_started:
            return 0.0
        hold = self._play_started + self._cur_dur + filler_gap_s() - time.monotonic()
        return max(0.0, hold)

    def cancel(self) -> None:
        """新用户轮到达等场景:作废定时器/链发并停掉在播垫话——用户插话优先,
        out-of-band 音轨不受框架打断机制管理,必须自己停。"""
        self._cancel_timer()
        self._cancel_chain()
        # 时间轴清零:插话后的回复不再被旧垫话的 gap 窗扣压。
        self._play_started = 0.0
        self._stop_playing()

    def reset_per_call(self) -> None:
        self._count = 0
        self._fired_lines.clear()
        self._recent.clear()
        self._entry_used.clear()
        self._cancel_timer()
        self._cancel_chain()
        self._stop_playing()
        self._handle = None

    # ---- 内部 ----

    def _cancel_timer(self) -> None:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        self._timer = None

    def _cancel_chain(self) -> None:
        """取消链发观察者并复位本轮链发状态(arm/cancel/reset 三处共用)。"""
        if self._chain_task is not None and not self._chain_task.done():
            self._chain_task.cancel()
        self._chain_task = None
        self._chain_depth = 0
        self._reply_audio_seen = False

    def _stop_playing(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            done = getattr(handle, "done", None)
            if callable(done) and done():
                return
            handle.stop()
        except Exception:  # noqa: BLE001 - 停播失败让垫话自然播完(短语 ≤1.5s)
            pass

    def _spawn_chain(self) -> None:
        """首条起播即挂「播完观察者」——官方 PlayHandle.wait_for_playout 精确补位。"""
        if not filler_chain_enabled() or self._chain_depth > 0:
            return
        self._chain_task = asyncio.create_task(self._chain_wait())

    async def _chain_wait(self) -> None:
        try:
            handle = self._handle
            if handle is None:
                return
            wait = getattr(handle, "wait_for_playout", None)
            if callable(wait):
                await wait()  # 官方 API:播完即醒(async def,须调用后 await)
            else:  # 测试替身无官方 API:按已知时长等(少量过等由后续门复核兜住)
                await asyncio.sleep(self._cur_dur + 0.05)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - 观察者失败=退回单发行为,唔阻通话
            return
        self._handle = None  # 已播完:清档,放行 _fire 的「上一句还在播」门
        if self._reply_audio_seen:
            return  # 回复首音频已到,hold 契约自会衔接,唔使链发
        if not filler_enabled() or self._player is None:
            return
        if self._guards() or filler_max_per_call() <= self._count:
            return
        state = str(getattr(self._session, "agent_state", "") or "")
        if state not in ("listening", "thinking", ""):
            return
        await asyncio.sleep(filler_gap_s())  # 垫话→垫话同款呼吸
        if self._reply_audio_seen or self._handle is not None:
            return  # gap 中回复出声/新开火——让位,唔叠音
        self._chain_depth += 1
        await self._fire(0.0)  # 复用开火路径(门在 _fire 内再复核;计数同源)

    def _pools(self) -> dict[str, list[dict]]:
        if self._manifest is None:
            try:
                self._manifest = load_manifest(self._assets)
            except Exception as exc:  # noqa: BLE001 - 资产缺失=垫话停用(响亮降级)
                print(
                    f"BOK_FILLER manifest unavailable dir={self._assets} err={exc!r} — 垫话停用",
                    flush=True,
                )
                self._manifest = {}
        return self._manifest

    def _pick(self, lang: str, category: str = "") -> dict | None:
        pool = self._pools().get(lang)
        if not pool:
            # 语言铁律:宁可不垫,绝不跨语言发声(池缺失响亮日志,不落其他语言池)。
            print(f"BOK_FILLER no pool lang={lang!r} — 跳过", flush=True)
            return None
        # 分类器回退层(2026-09-13):按场景类过滤 manifest 池(manifest 条目带
        # cat 标签);该类无条目 → default 标签池 → 整池(既有行为)。
        if category:
            cat_pool = [e for e in pool if e.get("cat") == category]
            if not cat_pool:
                cat_pool = [e for e in pool if e.get("cat") == "default"]
            if cat_pool:
                pool = cat_pool
        # 随机不重样(同垫话连续两轮最刺耳):池里剔除上两句后随机,池小才允许重复。
        recent = set(self._recent[-2:])
        candidates = [e for e in pool if e["file"] not in recent] or list(pool)
        entry = random.choice(candidates)
        self._recent.append(entry["file"])
        return entry

    def _select(self, lang: str) -> tuple[dict | None, str]:
        """选取链:①罐头确定性匹配(客户上一句) ②分类器→资产池回退。

        返回 (条目, 场景类)。罐头条目 {"text":..., "file": None}(音频只能来自
        tts-cache 人设物化,miss 在 _fire 里落资产兜底);资产条目带 file。
        """
        if (
            self._entries_index is not None
            and self._user_text_provider is not None
            and filler_match_enabled()
        ):
            try:
                user_text = str(self._user_text_provider() or "").strip()
            except Exception:  # noqa: BLE001 - provider 失败=回退
                user_text = ""
            if user_text:
                cat = classify_filler_category(user_text)
                entry, score = self._entries_index.match(
                    user_text, lang=lang, classifier_cat=cat, used=self._entry_used
                )
                if entry is not None:
                    eid = str(entry.get("id") or "")
                    self._entry_used[eid] = self._entry_used.get(eid, 0) + 1
                    print(
                        f"BOK_FILLER_MATCH hit entry={eid} score={score:.2f} cat={cat}",
                        flush=True,
                    )
                    if self._entry_hit is not None and eid:
                        try:
                            self._entry_hit(eid)
                        except Exception:  # noqa: BLE001 - 计数唔阻垫话
                            pass
                    return {"text": str(entry.get("text") or ""), "file": None}, cat
                print(f"BOK_FILLER_MATCH miss best={score:.2f} cat={cat}", flush=True)
                return self._pick(lang, cat), cat
        return self._pick(lang), ""

    async def _fire(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._timer = None
        try:
            # 直调防御:kill-switch/player 缺失在 arm 已挡,这里再挡一次
            # (定时器任务与状态翻转存在竞态窗口)。
            if not filler_enabled() or self._player is None:
                return
            if self._guards() or filler_max_per_call() <= self._count:
                return
            state = str(getattr(self._session, "agent_state", "") or "")
            if state not in ("listening", "thinking", ""):
                return  # 别的东西在播/状态不明,唔叠音
            if self._handle is not None:
                return  # 上一句垫话还在播(理论到唔到:cancel 已清),唔叠音
            lang = self._lang_resolver()
            entry, cat = self._select(lang)
            if not entry:
                return
            # 双层选源(task-14a):先查运行时人设物化版,miss 落源码资产兜底。
            voice = model = ""
            cached: bytes | None = None
            if self._cache is not None and self._voice_model_resolver is not None:
                try:
                    voice, model = self._voice_model_resolver()
                except Exception:  # noqa: BLE001 - resolver 失败=纯资产
                    voice = model = ""
                if voice and model:
                    try:
                        speed = 1.0
                        try:
                            speed = float(self._speed_resolver() or 1.0)
                        except Exception:  # noqa: BLE001
                            speed = 1.0
                        cached = self._cache.lookup(entry["text"], voice=voice, model=model, speed=speed)
                    except TypeError:
                        # 旧签名替身(测试/嵌入方)无 speed 形参:按 1.0 旧语义查
                        cached = self._cache.lookup(entry["text"], voice=voice, model=model)
                    except Exception:  # noqa: BLE001 - 缓存读取失败当未命中
                        cached = None
            if cached is None and not entry.get("file"):
                # 罐头条目只能出自 tts-cache 人设物化(无源码资产);miss=新人设
                # 缺物化 → 落分类器资产兜底(永不哑),并异步补物化罐头原文——
                # 同人设下一通起命中(补的是罐头文本,唔係兜底资产文本)。
                print(
                    f"BOK_FILLER_MATCH voice_miss entry_text={entry['text'][:20]!r}"
                    " — 罐头未物化,分类器资产兜底",
                    flush=True,
                )
                self._maybe_backfill(entry["text"], voice, model)
                entry = self._pick(lang, cat)
                if not entry:
                    return
            from .tts_cache import frames_aiter, pcm_to_frames

            if cached is not None:
                # 命中层:与通话完全同人声(cache.sample_rate 恒等 meta sample_rate,
                # 键里就含它)。dur 按实际 PCM 算(物化版时长≠资产 manifest dur_s)。
                rate = int(getattr(self._cache, "sample_rate", 24000))
                pcm = cached
                voice_mark = "voice_hit=1"
            else:
                pcm, rate = load_wav_pcm(self._assets / entry["file"])
                voice_mark = "voice_hit=0"
            # W1:两层都对齐 BackgroundAudioPlayer 的固定 48k(混音器无重采样,
            # 24k 直进=2 倍速升调「机器人声」);时长口径仍按源速率算。
            frames = pcm_to_frames(resample_pcm(pcm, rate, BACKGROUND_PLAYER_RATE), BACKGROUND_PLAYER_RATE)
            self._count += 1
            self._fired_lines.append(entry["text"])
            print(f"BOK_FILLER fired count={self._count} line={entry['text']!r} {voice_mark}", flush=True)
            # 展示/账本文本剥 MiniMax 停顿标记——<#0.3#> 是合成指令,原样进字幕
            # 与 turns 账本=用户可见的指令泄漏(缓存键/backfill 仍用原文,勿动)。
            display_text = _strip_pause_marks(str(entry["text"]))
            # 时长恒用实际 PCM 口径(2026-09-12 call-55c6fb1f「垫音重叠」根因:
            # manifest dur_s 是资产层音色的时长,人设物化版可差到 1.11s→2.72s,
            # hold 按旧值提前放行回复=回复压着垫话尾巴出声)——账本 dur 同源
            # 实际播放时长,勿回落 manifest dur_s。
            self._cur_dur = round(len(pcm) / 2 / rate, 2)
            if self._report is not None:
                # W3:开火即上报账本(异常零影响——上报失败绝不阻垫话)。
                try:
                    self._report(display_text, self._cur_dur)
                except Exception:  # noqa: BLE001
                    pass
            if self._caption is not None:
                # F4:字幕(同上异常零影响)。
                try:
                    self._caption(display_text)
                except Exception:  # noqa: BLE001
                    pass
            if cached is None:
                # miss=「新人设缺垫话物化」的运行时提醒信号;资产兜底永不哑。
                print(
                    f"BOK_FILLER voice_fallback voice={voice!r} model={model!r}"
                    " — 人设垫话未物化,资产兜底",
                    flush=True,
                )
                self._maybe_backfill(entry["text"], voice, model)
            # out-of-band 播放:即刻出声,唔排 speech 队列。fade_in 防咔哒(同
            # MiniMax 首包修剪 15ms 姿势),fade_out 令 stop() 有 50ms 淡出。
            try:
                from livekit.agents import AudioConfig

                source = AudioConfig(source=frames_aiter(frames), fade_in=0.015, fade_out=0.05)
            except Exception:  # noqa: BLE001 - 无 livekit(测试替身)直接喂裸帧迭代
                source = frames_aiter(frames)
            self._play_started = time.monotonic()
            self._handle = self._player.play(source)
            self._spawn_chain()  # 挂播完观察者:回复没来就链发第二发
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 垫话失败唔阻通话
            print(f"BOK_FILLER error err={exc!r}", flush=True)

    def _maybe_backfill(self, text: str, voice: str, model: str) -> None:
        """miss 后异步把该句用运行时人设音色云合成进缓存(off 关键路径,task-14a)。

        backfill 执行体由 agent 侧注入(CachedTTS.synthesize 消费,未命中 tee
        完整消费自动落盘);同一 (text,voice) 不显式去重——下次 lookup 命中即
        自然止。resolver 失败/空音色已在上游挡(纯资产),这里再挡一次。
        """
        if not filler_backfill_enabled() or self._backfill is None:
            return
        if not (voice and model):
            return
        task = asyncio.create_task(self._backfill_safe(text))
        self._backfill_tasks.add(task)
        task.add_done_callback(self._backfill_tasks.discard)

    async def _backfill_safe(self, text: str) -> None:
        try:
            await self._backfill(text)
            print(f"BOK_FILLER backfill_done line={text!r}", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 补物化失败零影响(下通照样资产兜底)
            print(f"BOK_FILLER backfill_fail line={text!r} err={exc!r}", flush=True)
