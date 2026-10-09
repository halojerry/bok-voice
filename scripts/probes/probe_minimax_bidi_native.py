#!/usr/bin/env python3
"""MiniMax t2a_v2_bidi 服务端攒句可行性探针（W7 · P1）。

问题：官方 bidi 的「服务端攒句」能否替代我们客户端手搓的攒句/早切机械
（classic 按句 task_continue + 头段催产 flush 那套）。只做测量，不改产品代码。

契约（官方文档钉死，镜像产品实现 apps/agent/agent_runtime/providers/livekit_plugins.py
MiniMaxTTS/_MiniMaxBidiSession 的取钥与载荷形状）：
  connect → connected_success → task_start → task_started → task_continue{text 任意粒度}
  → sentence_start → task_continued{data.audio=hex, is_final} [+data.subtitle word_streaming]
  → sentence_end；task_flush 催残留回 task_flushed（不关会话）；task_cancel 打断回
  task_canceled（会话回 task_started 可继续）；task_finish 收尾关连接。
  2201=120s 空闲断连（客户端自管 ws ping）；2205=软背压重发同条；2204=单条>10k 字。

场景（全部真 API 实弹，毫秒级数字）：
  A 逐字直喂    3 句（12-20 字含句末标点）按 1-3 字片、100ms 间隔 task_continue，
                逐句等 sentence_end 再喂下一句：①末字符发出→首音频块 ②句内音频块数/总时长
                ③word_streaming subtitle 在场且时间戳递增 ④碎裂判定（<1.5s 或块间隔异常）
  B flush 催尾  无句末标点短尾「好的没问题」：①干等首音频（8s 上限）②重发一次+task_flush
                后首音频耗时
  C cancel 存活 3 句长文流式喂入，首句音频到达即 task_cancel：①task_canceled 延迟
                ②cancel 后新 task_continue 能否出音频（会话存活）
  D ping/pong   空闲连接 ws ping RTT
  E 早切对照    A 第 1 句旧形状喂法：前 6 字立即发+task_flush，余文整句后发；首音频
                延迟 vs A，看「不早切不劣于早切」还是「早切更快但碎」
  F HTTP 对照   同句非流式 HTTP t2a_v2 字节数锚定（urllib+已验证 URL，零动态拼接）

凭据（零打印零字面量）：MINIMAX_API_KEY env 优先，缺省回读设置库
  ~/Library/Application Support/BokVoice/bok_voice.db global_settings.tts_json.api_key
  （与 probe_cloud_asr.minimax_key / 产品 interpret.py 装配同源姿势）。
  音色：--voice > MINIMAX_VOICE env > 设置库 tts_json.speaker_zh/speaker >
  产品 zh 默认（_MINIMAX_DEFAULT_VOICES["zh"]）> task_start 失败回退 Chinese_wenrounvxing。
出站安全：wss only + host frozenset allowlist（api.minimax.cn / api.minimax.chat）；
  F 场景 HTTP 端点=固定常量表按已验证 host 选取（probe_mt_matrix 同款 urllib 形状）。

用法：
  .venv312/bin/python scripts/probes/probe_minimax_bidi_native.py [--scenarios A,B,C,D,E,F]
报告：reports/w7probe/p1-bidi.md（+p1-bidi.json 原始数字；reports/ 全目录 gitignore）。
"""
from __future__ import annotations
# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))


import argparse
import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse

import websockets

# ---- 安全：出站 URL allowlist（wss only；host 只准 MiniMax 官方两域） ----
ALLOWED_HOSTS = frozenset({"api.minimax.cn", "api.minimax.chat"})
# F 场景 HTTP 对照端点=固定常量表（host 键过同一 allowlist；不做动态 URL 拼接）
_HTTP_T2A_BY_HOST = {
    "api.minimax.cn": "https://api.minimax.cn/v1/t2a_v2",
    "api.minimax.chat": "https://api.minimax.chat/v1/t2a_v2",
}
DEFAULT_ENDPOINT_CN = "wss://api.minimax.cn/ws/v1/t2a_v2_bidi"
DEFAULT_ENDPOINT_INTL = "wss://api.minimax.chat/ws/v1/t2a_v2_bidi"

# ---- 取钥/取音色（镜像产品姿势，零字面量密钥） ----
SETTINGS_DB = Path.home() / "Library" / "Application Support" / "BokVoice" / "bok_voice.db"

DEFAULT_MODEL = "speech-2.8-hd"  # 产品 MiniMaxTTS._model() 缺省（MINIMAX_MODEL 覆盖）
# 产品 zh 默认音色（agent.py _MINIMAX_DEFAULT_VOICES["zh"]，2026-09-12 拍板克隆音色）。
# 音色 ID 是不透明标识符（MiniMax 云端枚举，术语门禁白名单范畴）。
PRODUCT_ZH_VOICE = "moss_audio_aaa1346a-7ce7-11f0-8e61-2e6e3c7ee85d"
VOICE_LAST_RESORT = "Chinese_wenrounvxing"  # task_start 失败（音色无效）时的最后兜底
LANG_BOOST_ZH = "Chinese"  # MiniMax API 外部字面量（产品 zh 车道 language_boost 同款）

SAMPLE_RATE = 24000  # pcm 16bit mono → 时长秒 = bytes / 2 / 24000
SUBTITLE_ENABLE = True
SUBTITLE_TYPE = "word_streaming"

SENTENCES = [
    "今天我们聊一聊这个方案的整体节奏。",
    "第二批货大概下周三可以到仓。",
    "麻烦帮我把尾款的单号发我一下。",
]
TAIL_TEXT = "好的没问题"  # B：无句末标点短尾
LONG_TEXT = "".join(SENTENCES)  # C：3 句长文
CANCEL_NEW_TEXT = "那我们先把这一批的数量确认一下。"  # C：cancel 后新文本（含句末标点）

FEED_PIECE_PATTERN = (1, 3, 2)  # A/E：1-3 字片粒度（确定性循环）
FEED_INTERVAL_S = 0.100
FRAGMENT_MIN_S = 1.5  # 碎裂判定：单句音频总时长 < 1.5s = 碎
CHUNK_GAP_MAX_S = 1.0  # 碎裂判定：句内相邻音频块到达间隔 > 1.0s = 异常


def resolve_endpoint() -> str:
    """镜像 MiniMaxTTS._endpoint_ws_bidi()：MINIMAX_WS_URL 覆盖（补 _bidi 后缀）> region。"""
    base = os.environ.get("MINIMAX_WS_URL", "").strip()
    if base:
        return base if base.endswith("_bidi") else base + "_bidi"
    region = os.environ.get("MINIMAX_REGION", "cn").strip().lower()
    return DEFAULT_ENDPOINT_INTL if region in {"intl", "global", "chat"} else DEFAULT_ENDPOINT_CN


def check_url(url: str) -> str:
    u = urlparse(url)
    if u.scheme != "wss":
        raise SystemExit(f"[probe] 拒绝非 wss 端点: {u.scheme}://…（安全纪律 wss only）")
    if u.hostname not in ALLOWED_HOSTS:
        raise SystemExit(
            f"[probe] 拒绝非 allowlist host: {u.hostname}（只准 {sorted(ALLOWED_HOSTS)}）"
        )
    return url


def settings_tts_json() -> dict:
    """设置库 tts 段（只读；缺失/损坏回空 dict，保守降级）。"""
    try:
        con = sqlite3.connect(f"file:{SETTINGS_DB}?mode=ro", uri=True)
        raw = con.execute("select tts_json from global_settings").fetchone()[0]
        con.close()
        return json.loads(raw) or {}
    except Exception:  # noqa: BLE001 - 保守：读不到就当未配置
        return {}


def minimax_key() -> str:
    """env 优先（部署级覆盖）→ 设置库 tts_json.api_key（持久化，产品装配同源）。"""
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if key:
        return key
    return str(settings_tts_json().get("api_key") or "").strip()


def resolve_voice(explicit: str) -> str:
    """--voice > MINIMAX_VOICE env > 设置库 speaker_zh/speaker > 产品 zh 默认。"""
    if explicit.strip():
        return explicit.strip()
    env = os.environ.get("MINIMAX_VOICE", "").strip()
    if env:
        return env
    tts = settings_tts_json()
    for k in ("speaker_zh", "speaker"):
        v = str(tts.get(k) or "").strip()
        if v:
            return v
    return PRODUCT_ZH_VOICE


def extract_time_fields(obj, out: list | None = None) -> list[tuple[str, float]]:
    """递归收集 subtitle 载荷里名字含 time/ts 的数值字段（保文档序）。"""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and ("time" in kl or kl.endswith("ts")):
                out.append((str(k), float(v)))
            else:
                extract_time_fields(v, out)
    elif isinstance(obj, list):
        for v in obj:
            extract_time_fields(v, out)
    return out


def subtitle_stats(sub) -> dict:
    """word_streaming subtitle 消息形状：每条消息重发本句**累积**词表（句子本地 ms 时间戳）。
    返回 {n_words, words_increasing, seg_time_end}——递增判据只看词表内部
    （段级 time_begin=0 与词级时间戳混排会假阴，2026-10-09 首跑教训）。"""
    segs = sub if isinstance(sub, list) else [sub]
    n_words = 0
    increasing = True
    seg_time_end = None
    for seg in segs:
        if not isinstance(seg, dict):
            continue
        words = seg.get("timestamped_words") or []
        n_words = max(n_words, len(words))
        tbs = [float(w.get("time_begin") or 0) for w in words if isinstance(w, dict)]
        if len(tbs) > 1 and not all(a <= b for a, b in zip(tbs, tbs[1:])):
            increasing = False
        try:
            seg_time_end = float(seg.get("time_end"))
        except (TypeError, ValueError):
            pass
    return {"n_words": n_words, "words_increasing": increasing, "seg_time_end": seg_time_end}


def feed_pieces(text: str, pattern: tuple = FEED_PIECE_PATTERN) -> list[str]:
    """确定性 1-3 字切片（模拟 LLM token 流粒度）。"""
    out: list[str] = []
    i = 0
    pi = 0
    while i < len(text):
        n = pattern[pi % len(pattern)]
        pi += 1
        out.append(text[i : i + n])
        i += n
    return out


class BidiSession:
    """单条 bidi 连接：reader 协程做全部会计（音频块/句界/subtitle/状态码），
    场景代码轮询计数器并读取 reader 打的时间戳——避开辟事件竞态。"""

    def __init__(self, *, endpoint: str, key: str, model: str, voice: str,
                 language_boost: str = LANG_BOOST_ZH, subtitle: bool = True):
        self.endpoint = endpoint
        self.key = key
        self.model = model
        self.voice = voice
        self.language_boost = language_boost
        self.subtitle = subtitle
        self.ws = None
        self.t0 = 0.0  # 场景计时零点（open 完成时设）
        # 会计
        self.chunks: list[dict] = []  # {t, t_rel, bytes, dur_s, is_final, subtitle, sub_times}
        self.sent_starts: list[float] = []
        self.sent_ends: list[float] = []
        self.t_first_audio: float = 0.0
        self.t_flushed_seen: float = 0.0
        self.t_canceled_seen: float = 0.0
        self.t_finished_seen: float = 0.0
        self.statuses: list[dict] = []
        self.subtitle_sample: list = []
        self.dead: str = ""
        self.open_info: dict = {}
        self._reader_task: asyncio.Task | None = None

    # ---- 生命周期 ----
    def _task_start_payload(self) -> dict:
        """镜像 MiniMaxTTS._task_start_payload()（zh 档：speed 1.2 / pcm 24k / 排除聚合音频）。"""
        start = {
            "event": "task_start",
            "model": self.model,
            "voice_setting": {"voice_id": self.voice, "speed": 1.2, "vol": 1.0, "pitch": 0},
            "audio_setting": {"sample_rate": SAMPLE_RATE, "format": "pcm", "channel": 1},
            "stream_options": {"exclude_aggregated_audio": True},
        }
        if self.language_boost:
            start["language_boost"] = self.language_boost
        if SUBTITLE_ENABLE and self.subtitle:
            start["subtitle_enable"] = True
            start["subtitle_type"] = SUBTITLE_TYPE
        return start

    async def _on_msg(self, msg: dict) -> None:
        t = time.monotonic()
        ev = str(msg.get("event") or "")
        data = msg.get("data") or {}
        base = msg.get("base_resp") or {}
        try:
            status = int(base.get("status_code") or 0)
        except (TypeError, ValueError):
            status = 0
        if status:
            self.statuses.append(
                {"event": ev or "-", "status": status, "msg": str(base.get("status_msg"))[:80]}
            )
        audio_hex = str(data.get("audio") or "")
        sub = data.get("subtitle")
        is_final = bool(msg.get("is_final") or data.get("is_final"))
        if audio_hex:
            try:
                # bytes.fromhex 即 PCM16 原始字节数（勿再 //2——2026-10-09 首跑曾双重
                # 除 2 把所有时长砍半，误报「碎裂」；HTTP 同句对照 115916B=2.415s 钉死）。
                nbytes = len(bytes.fromhex(audio_hex))
            except ValueError:
                nbytes = 0
            rec = {
                "t": t,
                "t_rel": t - self.t0,
                "bytes": nbytes,
                "dur_s": nbytes / 2 / SAMPLE_RATE,
                "is_final": is_final,
                "subtitle": bool(sub),
                "sub_stats": subtitle_stats(sub) if sub else None,
                "sub_times": extract_time_fields(sub) if sub else [],
            }
            self.chunks.append(rec)
            if sub and len(self.subtitle_sample) < 3:
                self.subtitle_sample.append(sub if isinstance(sub, (dict, list)) else str(sub)[:200])
            if not self.t_first_audio:
                self.t_first_audio = t
        if ev == "sentence_start":
            self.sent_starts.append(t)
        elif ev == "sentence_end":
            self.sent_ends.append(t)
        elif ev == "task_flushed":
            self.t_flushed_seen = self.t_flushed_seen or t
        elif ev == "task_canceled":
            self.t_canceled_seen = t
        elif ev == "task_finished":
            self.t_finished_seen = t

    async def _reader(self) -> None:
        try:
            while True:
                raw = await self.ws.recv()
                try:
                    msg = json.loads(raw)
                except Exception:  # noqa: BLE001 - 非 JSON 心跳类忽略
                    continue
                await self._on_msg(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 连接死亡：记账并唤醒等待方
            self.dead = repr(exc)[:160]

    async def open(self) -> bool:
        """connect → connected_success → task_start → task_started。失败回退兜底音色一次。"""
        info = await self._open_with_voice(self.voice)
        if not info["ok"] and self.voice != VOICE_LAST_RESORT:
            print("[probe] task_start 失败（音色无效?），回退兜底音色重试一次", flush=True)
            self.voice = VOICE_LAST_RESORT
            await self.close()
            info = await self._open_with_voice(self.voice)
        self.open_info = info
        return bool(info["ok"])

    async def _open_with_voice(self, voice: str) -> dict:
        t_conn = time.monotonic()
        try:
            # ping_interval=None：关库自带 keepalive（D 场景自测 pong；语义同产品自管 ping）
            self.ws = await websockets.connect(
                self.endpoint,
                additional_headers={"Authorization": f"Bearer {self.key}"},
                open_timeout=10,
                max_size=20_000_000,
                ping_interval=None,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"connect: {exc!r}"[:160], "connect_ms": None}
        connect_ms = (time.monotonic() - t_conn) * 1000
        # connected_success（丢帧不致命，产品同语义）
        t0 = time.monotonic()
        greeting = ""
        try:
            greeting = str(json.loads(await asyncio.wait_for(self.ws.recv(), timeout=10)).get("event") or "")
        except Exception:  # noqa: BLE001
            pass
        conn_ack_ms = (time.monotonic() - t0) * 1000
        self.voice = voice
        t_start = time.monotonic()
        await self.ws.send(json.dumps(self._task_start_payload()))
        try:
            resp = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=15))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"task_start recv: {exc!r}"[:160],
                    "connect_ms": connect_ms, "conn_ack_ms": conn_ack_ms}
        start_ms = (time.monotonic() - t_start) * 1000
        ok = resp.get("event") == "task_started"
        base = resp.get("base_resp") or {}
        self.t0 = time.monotonic()
        self._reader_task = asyncio.get_running_loop().create_task(self._reader())
        return {
            "ok": ok,
            "connect_ms": round(connect_ms),
            "conn_ack_ms": round(conn_ack_ms),
            "task_start_ms": round(start_ms),
            "greeting_event": greeting,
            "ack_event": str(resp.get("event") or ""),
            "status": int(base.get("status_code") or 0),
            "status_msg": str(base.get("status_msg") or "")[:80],
            "error": "" if ok else f"task_start ack: {str(resp)[:160]}",
            "voice": voice,
        }

    # ---- 发送 ----
    async def send_text(self, text: str) -> None:
        await self.ws.send(json.dumps({"event": "task_continue", "text": text}))

    async def send_flush(self) -> None:
        await self.ws.send(json.dumps({"event": "task_flush"}))

    async def send_cancel(self) -> None:
        await self.ws.send(json.dumps({"event": "task_cancel"}))

    # ---- 等待（轮询 reader 计数器，避开辟事件竞态） ----
    def _last_chunk_t(self) -> float:
        return self.chunks[-1]["t"] if self.chunks else 0.0

    async def wait_first_audio(self, timeout: float, after_count: int = 0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.chunks) > after_count:
                return True
            if self.dead:
                return False
            await asyncio.sleep(0.01)
        return False

    async def wait_sentence_complete(self, n: int, timeout: float) -> bool:
        """第 n 句完成 = sentence_end 计数到位，或对应 is_final 块已到且 0.8s 无新块。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.sent_ends) >= n:
                return True
            finals = sum(1 for c in self.chunks if c["is_final"])
            if finals >= n and time.monotonic() - self._last_chunk_t() > 0.8:
                return True
            if self.dead:
                return False
            await asyncio.sleep(0.02)
        return False

    async def wait_predicate(self, pred, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            if self.dead:
                return False
            await asyncio.sleep(0.01)
        return False

    async def drain(self, quiet_s: float = 1.0) -> None:
        """静默窗收集残余事件（记账不丢），供收尾统计。"""
        end = time.monotonic() + quiet_s
        while time.monotonic() < end:
            await asyncio.sleep(0.05)
            end = min(end, self._last_chunk_t() + quiet_s) if self.chunks else end

    async def close(self) -> None:
        task = self._reader_task
        self._reader_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
        try:
            if self.ws is not None:
                await self.ws.close()
        except Exception:  # noqa: BLE001
            pass


# ---- 场景 ----
async def scenario_a(endpoint: str, key: str, model: str, voice: str) -> dict:
    """A 逐字直喂：3 句顺序喂（逐句等完再喂下一句），测首音频/句内形状/subtitle/碎裂。"""
    s = BidiSession(endpoint=endpoint, key=key, model=model, voice=voice, subtitle=True)
    if not await s.open():
        return {"scenario": "A", "error": s.open_info.get("error", "open failed"),
                "open": s.open_info}
    out: dict = {"scenario": "A", "open": s.open_info, "sentences": []}
    for si, sent in enumerate(SENTENCES):
        pieces = feed_pieces(sent)
        t_first_send = time.monotonic()
        t_last_send = t_first_send
        n_chunks_before = len(s.chunks)
        for j, piece in enumerate(pieces):
            await s.send_text(piece)
            if j == len(pieces) - 1:
                t_last_send = time.monotonic()
            await asyncio.sleep(FEED_INTERVAL_S)
        await s.wait_sentence_complete(si + 1, timeout=15.0)
        got = s.chunks[n_chunks_before:]
        if got:
            first_audio_ms = (got[0]["t"] - t_last_send) * 1000
        else:
            first_audio_ms = None
        sent_end_ms = (
            (s.sent_ends[si] - t_last_send) * 1000 if len(s.sent_ends) > si else None
        )
        total_dur = sum(c["dur_s"] for c in got)
        gaps = [
            (got[i + 1]["t"] - got[i]["t"]) * 1000 for i in range(len(got) - 1)
        ]
        sub_stats = [c["sub_stats"] for c in got if c.get("sub_stats")]
        sub_times_by_field: dict[str, list[float]] = {}
        for c in got:
            for k, v in c["sub_times"]:
                sub_times_by_field.setdefault(k, []).append(v)
        rec = {
            "text": sent,
            "chars": len(sent),
            "pieces": len(pieces),
            "feed_ms": round((t_last_send - t_first_send) * 1000),
            "first_audio_ms_from_last_char": round(first_audio_ms) if first_audio_ms is not None else None,
            "sentence_end_ms_from_last_char": round(sent_end_ms) if sent_end_ms is not None else None,
            "chunks": len(got),
            "total_audio_s": round(total_dur, 3),
            "max_chunk_gap_ms": round(max(gaps)) if gaps else 0,
            "subtitle_msgs": len(sub_stats),
            "subtitle_word_growth": [s["n_words"] for s in sub_stats],
            "subtitle_final_seg_ms": sub_stats[-1]["seg_time_end"] if sub_stats else None,
            "subtitle_times_increasing": (
                all(s["words_increasing"] for s in sub_stats) and bool(sub_stats)
            ),
            "fragment": bool(got) and total_dur < FRAGMENT_MIN_S,
            "abnormal_gap": any(g > CHUNK_GAP_MAX_S * 1000 for g in gaps),
        }
        out["sentences"].append(rec)
        print(
            f"[A] 句{si + 1} 末字→首音频 {rec['first_audio_ms_from_last_char']}ms "
            f"chunks={rec['chunks']} dur={rec['total_audio_s']}s "
            f"subtitle msgs={rec['subtitle_msgs']} 词表增长={rec['subtitle_word_growth']} "
            f"词内时间戳递增={rec['subtitle_times_increasing']}",
            flush=True,
        )
    out["statuses"] = s.statuses
    out["dead"] = s.dead
    out["subtitle_sample"] = s.subtitle_sample
    await s.drain(0.8)
    await s.close()
    frags = [r for r in out["sentences"] if r["fragment"] or r["abnormal_gap"]]
    out["fragmentation_verdict"] = (
        "PASS 无碎裂（无 <1.5s 短句、无 >1s 块间隔）" if not frags
        else f"FAIL 碎裂嫌疑 {len(frags)} 句: " + "; ".join(
            f"{r['text'][:8]}… dur={r['total_audio_s']}s gap={r['max_chunk_gap_ms']}ms" for r in frags
        )
    )
    return out


async def _feed_until_first_audio(s: BidiSession, text: str) -> float:
    """按 100ms/片喂文本，首音频到达即停喂；返回 t0（首片发出时刻）。"""
    pieces = feed_pieces(text)
    t0 = time.monotonic()
    for piece in pieces:
        await s.send_text(piece)
        if s.t_first_audio:
            break
        await asyncio.sleep(FEED_INTERVAL_S)
    return t0


async def scenario_b(endpoint: str, key: str, model: str, voice: str) -> dict:
    """B flush 催尾：无句末标点短尾 ①干等 8s ②重发一次+task_flush。"""
    s = BidiSession(endpoint=endpoint, key=key, model=model, voice=voice, subtitle=False)
    if not await s.open():
        return {"scenario": "B", "error": s.open_info.get("error", "open failed"),
                "open": s.open_info}
    out: dict = {"scenario": "B", "text": TAIL_TEXT, "open": s.open_info}
    t_send = time.monotonic()
    await s.send_text(TAIL_TEXT)
    got_b1 = await s.wait_first_audio(timeout=8.0)
    if got_b1:
        out["b1_no_flush_first_audio_ms"] = round((s.t_first_audio - t_send) * 1000)
        # 干等段音频形状（服务端无标点空闲兜底是否自己合完整句）
        await s.wait_predicate(
            lambda: time.monotonic() - s._last_chunk_t() > 1.0, timeout=6.0
        )
        b1_chunks = s.chunks[:]
        out["b1_chunks"] = len(b1_chunks)
        out["b1_total_audio_s"] = round(sum(c["dur_s"] for c in b1_chunks), 3)
        out["b1_sentence_events"] = {"start": len(s.sent_starts), "end": len(s.sent_ends)}
    else:
        out["b1_no_flush_first_audio_ms"] = ">8000"
    print(f"[B1] 不发 flush 干等 8s：首音频 {out['b1_no_flush_first_audio_ms']} "
          f"chunks={out.get('b1_chunks')} dur={out.get('b1_total_audio_s')}s", flush=True)
    # ② 重发一次 + 立即 flush（2205 同款「原样重发」姿势）
    n_before = len(s.chunks)
    t_resend = time.monotonic()
    await s.send_text(TAIL_TEXT)
    await s.send_flush()
    t_flush_sent = time.monotonic()
    got_b2 = await s.wait_first_audio(timeout=8.0, after_count=n_before)
    # 等 flush 收尾：task_flushed ack 或 1s 音频静默（ack 常在音频后到，勿在首块即采样）
    await s.wait_predicate(
        lambda: bool(s.t_flushed_seen) or time.monotonic() - s._last_chunk_t() > 1.0,
        timeout=6.0,
    )
    await s.drain(0.8)
    if got_b2:
        new_chunk_t = next(c["t"] for c in s.chunks[n_before:])
        out["b2_resend_flush_first_audio_ms_from_resend"] = round((new_chunk_t - t_resend) * 1000)
        out["b2_resend_flush_first_audio_ms_from_flush"] = round(
            (new_chunk_t - t_flush_sent) * 1000
        )
    else:
        out["b2_resend_flush_first_audio_ms_from_resend"] = ">8000"
        out["b2_resend_flush_first_audio_ms_from_flush"] = ">8000"
    out["flushed_seen"] = bool(s.t_flushed_seen)
    out["flushed_after_ms"] = (
        round((s.t_flushed_seen - t_resend) * 1000) if s.t_flushed_seen else None
    )
    out["b2_total_audio_s"] = round(sum(c["dur_s"] for c in s.chunks[n_before:]), 3)
    print(
        f"[B2] 重发+task_flush：首音频 {out['b2_resend_flush_first_audio_ms_from_resend']}ms "
        f"(flushed ack {out['flushed_after_ms']}ms, 音频 {out['b2_total_audio_s']}s)",
        flush=True,
    )
    out["statuses"] = s.statuses
    out["dead"] = s.dead
    await s.drain(0.8)
    await s.close()
    return out


async def scenario_c(endpoint: str, key: str, model: str, voice: str) -> dict:
    """C cancel 存活：长文流式喂入，首句音频到达即 cancel；随后新文本验证会话存活。"""
    s = BidiSession(endpoint=endpoint, key=key, model=model, voice=voice, subtitle=False)
    if not await s.open():
        return {"scenario": "C", "error": s.open_info.get("error", "open failed"),
                "open": s.open_info}
    out: dict = {"scenario": "C", "open": s.open_info}
    t0 = await _feed_until_first_audio(s, LONG_TEXT)
    await s.wait_first_audio(timeout=10.0)
    if not s.chunks:
        out["error"] = "首音频未到（10s），无法测 cancel"
        await s.close()
        return out
    out["first_audio_ms_from_feed_start"] = round((s.t_first_audio - t0) * 1000)
    out["pre_cancel_audio_s"] = round(sum(c["dur_s"] for c in s.chunks), 3)
    t_cancel = time.monotonic()
    await s.send_cancel()
    await s.wait_predicate(lambda: bool(s.t_canceled_seen), timeout=5.0)
    out["cancel_to_task_canceled_ms"] = (
        round((s.t_canceled_seen - t_cancel) * 1000) if s.t_canceled_seen else ">5000"
    )
    n_at_cancel = len(s.chunks)
    print(f"[C] cancel→task_canceled {out['cancel_to_task_canceled_ms']}ms", flush=True)
    # cancel 后新文本（会话存活证明）
    t_new = time.monotonic()
    await s.send_text(CANCEL_NEW_TEXT)
    alive = await s.wait_first_audio(timeout=10.0, after_count=n_at_cancel)
    if alive:
        new_chunk_t = next(c["t"] for c in s.chunks[n_at_cancel:])
        out["post_cancel_new_text_first_audio_ms"] = round((new_chunk_t - t_new) * 1000)
        await s.wait_predicate(
            lambda: len(s.sent_ends) >= 1 and time.monotonic() - s._last_chunk_t() > 0.8,
            timeout=10.0,
        )
    else:
        out["post_cancel_new_text_first_audio_ms"] = None
    post = s.chunks[n_at_cancel:]
    out["post_cancel_audio_s"] = round(sum(c["dur_s"] for c in post), 3)
    out["session_alive"] = bool(alive)
    out["statuses"] = s.statuses
    out["dead"] = s.dead
    print(
        f"[C] cancel 后新文本首音频 {out['post_cancel_new_text_first_audio_ms']}ms "
        f"音频 {out['post_cancel_audio_s']}s 存活={out['session_alive']}",
        flush=True,
    )
    await s.drain(0.8)
    await s.close()
    return out


async def scenario_d(endpoint: str, key: str, model: str, voice: str) -> dict:
    """D ping/pong：空闲连接 ws ping RTT（服务端永不 ping 的保活成本测量）。"""
    s = BidiSession(endpoint=endpoint, key=key, model=model, voice=voice, subtitle=False)
    if not await s.open():
        return {"scenario": "D", "error": s.open_info.get("error", "open failed"),
                "open": s.open_info}
    out: dict = {"scenario": "D", "open": s.open_info, "pings": []}
    await asyncio.sleep(0.3)  # 进入空闲态
    for i in range(3):
        t0 = time.monotonic()
        pong_waiter = await s.ws.ping()
        try:
            _latency_s = await asyncio.wait_for(pong_waiter, timeout=5.0)
            rtt_ms = (time.monotonic() - t0) * 1000
            out["pings"].append(round(rtt_ms))
        except Exception as exc:  # noqa: BLE001
            out["pings"].append(f"timeout/{exc!r}"[:40])
        await asyncio.sleep(0.2)
    ok = [p for p in out["pings"] if isinstance(p, (int, float))]
    out["pong_rtt_ms_median"] = round(sorted(ok)[len(ok) // 2]) if ok else None
    print(f"[D] ws ping RTT：{out['pings']} (median {out['pong_rtt_ms_median']}ms)", flush=True)
    out["dead"] = s.dead
    await s.close()
    return out


async def scenario_e(endpoint: str, key: str, model: str, voice: str, a_result: dict) -> dict:
    """E 早切对照：A 第 1 句旧形状喂法（前 6 字立即发+flush，余文整句后发）vs A。"""
    s = BidiSession(endpoint=endpoint, key=key, model=model, voice=voice, subtitle=False)
    if not await s.open():
        return {"scenario": "E", "error": s.open_info.get("error", "open failed"),
                "open": s.open_info}
    sent = SENTENCES[0]
    head, rest = sent[:6], sent[6:]
    out: dict = {"scenario": "E", "head": head, "rest": rest, "open": s.open_info}
    # ① 前 6 字立即发 + 紧跟 task_flush（客户端早切旧形状）
    t0 = time.monotonic()
    await s.send_text(head)
    t_head_sent = time.monotonic()
    await s.send_flush()
    got_head = await s.wait_first_audio(timeout=8.0)
    if got_head:
        out["head_first_audio_ms_from_t0"] = round((s.t_first_audio - t0) * 1000)
        out["head_first_audio_ms_from_head_sent"] = round((s.t_first_audio - t_head_sent) * 1000)
    else:
        out["head_first_audio_ms_from_t0"] = None
        out["head_first_audio_ms_from_head_sent"] = None
    # 头段说完再切账（flushed ack 或 1s 静默）——首块即切会把头段余音频错记给余文
    await s.wait_predicate(
        lambda: bool(s.t_flushed_seen) or time.monotonic() - s._last_chunk_t() > 1.0,
        timeout=6.0,
    )
    n_before_rest = len(s.chunks)
    # ② 余下文本整句后发（含句末标点，服务端自己合成）
    t_rest = time.monotonic()
    await s.send_text(rest)
    await s.wait_predicate(lambda: time.monotonic() - s._last_chunk_t() > 1.0, timeout=10.0)
    rest_chunks = s.chunks[n_before_rest:]
    rest_first = rest_chunks[0]["t"] if rest_chunks else None
    out["rest_first_audio_ms_from_rest_sent"] = (
        round((rest_first - t_rest) * 1000) if rest_first else None
    )
    head_dur = sum(c["dur_s"] for c in s.chunks[:n_before_rest])
    rest_dur = sum(c["dur_s"] for c in rest_chunks)
    out["head_audio_s"] = round(head_dur, 3)
    out["rest_audio_s"] = round(rest_dur, 3)
    out["head_fragment"] = bool(s.chunks[:n_before_rest]) and head_dur < FRAGMENT_MIN_S
    out["segments"] = 1 + (1 if rest_chunks else 0)
    a1 = (a_result.get("sentences") or [{}])[0]
    out["a_sent1_first_audio_ms_from_last_char"] = a1.get("first_audio_ms_from_last_char")
    out["a_sent1_feed_ms"] = a1.get("feed_ms")
    out["verdict_head_vs_a"] = (
        f"E 头段首音频 {out['head_first_audio_ms_from_t0']}ms vs A 整句首音频 "
        f"末字后 {out['a_sent1_first_audio_ms_from_last_char']}ms（A 含喂入 {out['a_sent1_feed_ms']}ms）；"
        f"E 头段音频 {out['head_audio_s']}s "
        f"{'<1.5s=碎句' if out['head_fragment'] else '≥1.5s=非碎'}"
    )
    print(f"[E] {out['verdict_head_vs_a']}", flush=True)
    out["statuses"] = s.statuses
    out["dead"] = s.dead
    await s.drain(0.8)
    await s.close()
    return out


def scenario_f(endpoint: str, key: str, model: str, voice: str) -> dict:
    """F 一致性对照：同句非流式 HTTP t2a_v2 字节数对照 bidi 流（时长口径锚定）。
    urllib+固定常量表端点（host 键过同一 allowlist；probe_mt_matrix 同款先例形状）。"""
    import urllib.error
    import urllib.request

    host = (urlparse(endpoint).hostname or "").lower()
    url = _HTTP_T2A_BY_HOST.get(host) if host in ALLOWED_HOSTS else None
    if not url:
        return {"scenario": "F", "error": f"非 allowlist http 端点: {host}"}
    body = {
        "model": model,
        "text": SENTENCES[0],
        "voice_setting": {"voice_id": voice, "speed": 1.2, "vol": 1.0, "pitch": 0},
        "audio_setting": {"sample_rate": SAMPLE_RATE, "format": "pcm", "channel": 1},
        "language_boost": LANG_BOOST_ZH,
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {key}")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        status = 0
    except urllib.error.HTTPError as exc:  # noqa: BLE001
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"scenario": "F", "error": f"HTTP {exc.code} 非 JSON"}
        status = int(exc.code)
    except Exception as exc:  # noqa: BLE001
        return {"scenario": "F", "error": f"{exc!r}"[:160]}
    ms = round((time.monotonic() - t0) * 1000)
    audio_hex = str((payload.get("data") or {}).get("audio") or "")
    nbytes = len(bytes.fromhex(audio_hex)) if audio_hex else 0
    out = {
        "scenario": "F",
        "text": SENTENCES[0],
        "http_ms": ms,
        "status": int((payload.get("base_resp") or {}).get("status_code") or status),
        "bytes": nbytes,
        "total_audio_s": round(nbytes / 2 / SAMPLE_RATE, 3),
    }
    print(f"[F] HTTP 同句对照：{out['bytes']}B = {out['total_audio_s']}s ({out['http_ms']}ms)", flush=True)
    return out


def fmt(v) -> str:
    return "—" if v is None else str(v)


def render_markdown(results: dict, meta: dict) -> str:
    lines: list[str] = []
    lines.append("# W7-P1 MiniMax t2a_v2_bidi 服务端攒句可行性探针")
    lines.append("")
    lines.append(f"- 运行时刻：{meta['ts']}")
    lines.append(f"- 端点 host：`{meta['host']}`（allowlist 内；key 已找到={meta['key_found']}，值不落盘）")
    lines.append(
        f"- model=`{meta['model']}` voice=`{meta['voice']}` speed=1.2 "
        f"pcm/{SAMPLE_RATE}Hz subtitle={SUBTITLE_TYPE}"
    )
    lines.append(f"- 任务开始 ack：{fmt(meta.get('task_start_ms'))}ms（connect {fmt(meta.get('connect_ms'))}ms）")
    lines.append("")
    a = results.get("A") or {}
    lines.append("## A 逐字直喂（1-3 字片 × 100ms，逐句等完再喂）")
    lines.append("")
    if a.get("sentences"):
        lines.append(
            "| 句 | 字数 | 喂入ms | 末字→首音频ms | 末字→句尾ms | 块数 | 总音频s | "
            "最大块间隔ms | subtitle消息 | 词表增长 | 词内时间戳递增 | 碎裂(<1.5s) |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(a["sentences"], 1):
            lines.append(
                f"| {i} | {r['chars']} | {r['feed_ms']} | {fmt(r['first_audio_ms_from_last_char'])} "
                f"| {fmt(r['sentence_end_ms_from_last_char'])} | {r['chunks']} | {r['total_audio_s']} "
                f"| {r['max_chunk_gap_ms']} | {r['subtitle_msgs']} | {r['subtitle_word_growth']} "
                f"| {r['subtitle_times_increasing']} "
                f"| {r['fragment']} |"
            )
        lines.append("")
        lines.append(f"**碎裂判定**：{a.get('fragmentation_verdict')}")
        s1 = a["sentences"][0]
        lines.append(
            f"**subtitle 形状**：word_streaming 每条消息重发本句**累积**词表（句子本地 ms 时间戳，"
            f"词表随合成进度增长）；句 1 末段词数 "
            f"{s1['subtitle_word_growth'][-1] if s1['subtitle_word_growth'] else '—'}、"
            f"句末 seg time_end={fmt(s1['subtitle_final_seg_ms'])}ms（与收到的音频时长同量级=对得上）。"
        )
    else:
        lines.append(f"失败：{a.get('error')}")
    lines.append("")
    b = results.get("B") or {}
    lines.append("## B task_flush 催尾（无标点短尾「" + TAIL_TEXT + "」）")
    lines.append("")
    if not b.get("error"):
        lines.append("| 项 | 数字 |")
        lines.append("|---|---|")
        lines.append(f"| 不发 flush 干等首音频 | {fmt(b.get('b1_no_flush_first_audio_ms'))} |")
        lines.append(f"| 干等段音频（块数/总时长 s） | {fmt(b.get('b1_chunks'))} / {fmt(b.get('b1_total_audio_s'))} |")
        lines.append(
            f"| 重发一次+task_flush → 首音频（自重发起） | {fmt(b.get('b2_resend_flush_first_audio_ms_from_resend'))} |"
        )
        lines.append(f"| task_flushed ack（自重发起） | {fmt(b.get('flushed_after_ms'))} |")
        lines.append(f"| flush 段音频总时长 s | {fmt(b.get('b2_total_audio_s'))} |")
    else:
        lines.append(f"失败：{b.get('error')}")
    lines.append("")
    c = results.get("C") or {}
    lines.append("## C task_cancel 存活")
    lines.append("")
    if not c.get("error"):
        lines.append("| 项 | 数字 |")
        lines.append("|---|---|")
        lines.append(f"| 长文喂入起点→首音频 ms | {fmt(c.get('first_audio_ms_from_feed_start'))} |")
        lines.append(f"| task_cancel → task_canceled ms | {fmt(c.get('cancel_to_task_canceled_ms'))} |")
        lines.append(f"| cancel 后新文本首音频 ms | {fmt(c.get('post_cancel_new_text_first_audio_ms'))} |")
        lines.append(f"| cancel 后新文本音频 s | {fmt(c.get('post_cancel_audio_s'))} |")
        lines.append(f"| 会话存活 | {c.get('session_alive')} |")
    else:
        lines.append(f"失败：{c.get('error')}")
    lines.append("")
    d = results.get("D") or {}
    lines.append("## D ping/pong")
    lines.append("")
    lines.append(f"- ws ping RTT 3 次：{d.get('pings')}，median {fmt(d.get('pong_rtt_ms_median'))}ms")
    lines.append("")
    e = results.get("E") or {}
    lines.append("## E 早切对照（A 第 1 句旧形状：前 6 字+flush → 余文整句）")
    lines.append("")
    if not e.get("error"):
        lines.append("| 项 | 数字 |")
        lines.append("|---|---|")
        lines.append(f"| E 头段（6字+flush）首音频 ms（自 t0） | {fmt(e.get('head_first_audio_ms_from_t0'))} |")
        lines.append(f"| E 余文整句首音频 ms（自余文发出） | {fmt(e.get('rest_first_audio_ms_from_rest_sent'))} |")
        lines.append(f"| E 头段音频 s（<1.5s=碎） | {fmt(e.get('head_audio_s'))} |")
        lines.append(f"| E 余文音频 s | {fmt(e.get('rest_audio_s'))} |")
        lines.append(f"| A 句1 末字→首音频 ms（对照） | {fmt(e.get('a_sent1_first_audio_ms_from_last_char'))} |")
        lines.append(f"| A 句1 喂入耗时 ms（对照） | {fmt(e.get('a_sent1_feed_ms'))} |")
        lines.append("")
        lines.append(f"**对照**：{e.get('verdict_head_vs_a')}")
    else:
        lines.append(f"失败：{e.get('error')}")
    lines.append("")
    f = results.get("F") or {}
    lines.append("## F 一致性对照（同句非流式 HTTP t2a_v2，时长口径锚定）")
    lines.append("")
    if not f.get("error"):
        lines.append(f"- HTTP 同句 `{f.get('text')}`：{f.get('bytes')}B = **{fmt(f.get('total_audio_s'))}s**"
                     f"（{f.get('http_ms')}ms）——bidi 流式逐句音频应与此同量级")
    else:
        lines.append(f"失败：{f.get('error')}")
    lines.append("")
    lines.append("## 附带侦查（读产品码结论，静态）")
    lines.append("")
    lines.append("- 客户端 ws ping 保活：**有**——`_MiniMaxBidiSession._ping_loop` 默认 60s 一发"
                 "（`MINIMAX_BIDI_PING_S`），连失 ≥2 次（`MINIMAX_BIDI_PING_MAX_MISS`）强断重预热；"
                 "连接时 `ping_interval=None` 关库自带 keepalive，符合官方「客户端自管 ping」。")
    lines.append("- 2205 处理：**双形态**——非 task_failed 携带的 2205=软背压，`_resend_loop` "
                 "0.2s 后**原样重发**同条 task_continue（不重连）；task_failed 携带的 2205 入 "
                 "F-10 限流守卫（1002/1039/2205 族）：关连接弃会话 + 退避（1s/2s）重试或回落 HTTP，"
                 "连续 3 轮熔断本通直走 HTTP。")
    lines.append("")
    return "\n".join(lines) + "\n"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--endpoint", default="", help="覆盖 WS 端点（仍受 wss+allowlist 校验）")
    ap.add_argument("--model", default=os.environ.get("MINIMAX_MODEL", "").strip() or DEFAULT_MODEL)
    ap.add_argument("--voice", default="", help="覆盖音色（缺省走解析链，见 docstring）")
    ap.add_argument("--scenarios", default="A,B,C,D,E,F", help="要跑的场景，如 A,B,E")
    args = ap.parse_args()

    endpoint = check_url(args.endpoint.strip() or resolve_endpoint())
    key = minimax_key()
    voice = resolve_voice(args.voice)
    host = urlparse(endpoint).hostname or "?"
    print(
        f"[probe] endpoint host={host} key={'已找到' if key else '未找到'} "
        f"voice={voice} model={args.model}", flush=True
    )
    if not key:
        print("[probe] 无凭据（MINIMAX_API_KEY env 或设置库 tts_json.api_key），退出。", flush=True)
        return 2

    want = {x.strip().upper() for x in args.scenarios.split(",") if x.strip()}
    results: dict = {}

    async def run(name: str, coro_fn, *fn_args):
        """单场景失败不拖垮整场：错误记进 results 继续跑其余场景。"""
        try:
            results[name] = await coro_fn(*fn_args)
        except Exception as exc:  # noqa: BLE001 - 探针尽力而为
            results[name] = {"scenario": name, "error": f"{exc!r}"[:200]}

    # A 先跑（E 的对照数字取自 A 句 1）
    if "A" in want:
        await run("A", scenario_a, endpoint, key, args.model, voice)
    if "B" in want:
        await run("B", scenario_b, endpoint, key, args.model, voice)
    if "C" in want:
        await run("C", scenario_c, endpoint, key, args.model, voice)
    if "D" in want:
        await run("D", scenario_d, endpoint, key, args.model, voice)
    if "E" in want:
        await run("E", scenario_e, endpoint, key, args.model, voice, results.get("A") or {})
    if "F" in want:
        results["F"] = scenario_f(endpoint, key, args.model, voice)

    meta = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": host,
        "key_found": bool(key),
        "model": args.model,
        "voice": voice,
        "connect_ms": (results.get("A") or {}).get("open", {}).get("connect_ms"),
        "task_start_ms": (results.get("A") or {}).get("open", {}).get("task_start_ms"),
    }

    out_dir = Path(__file__).resolve().parents[2] / "reports" / "w7probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "p1-bidi.md"
    json_path = out_dir / "p1-bidi.json"
    md = render_markdown(results, meta)
    md_path.write_text(md, encoding="utf-8")
    slim = json.loads(json.dumps(results, ensure_ascii=False, default=str))
    json_path.write_text(json.dumps({"meta": meta, "results": slim}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[probe] 报告已写 {md_path}", flush=True)
    print(f"[probe] 原始数字已写 {json_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
