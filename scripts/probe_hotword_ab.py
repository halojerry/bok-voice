"""ASR 热词 context 扩容 A/B 探针（粤语异议域词碎裂专项）。

背景：真实通话链路实测，粤语异议域句（诈骗/机器人/主管/仓库…）在现有行业
热词表下严重碎裂（「你係咪詐騙集團嚟㗎」→「田静宁，你们系一样嘅」）。本探针
验证「词表扩容」能否修复，并量化扩容后的 prefill 成本是否可感。

三档对照（每档全 10 句 × 2 轮取好成绩）：
  none      无 Vocabulary（复现碎裂基线）
  current   现有行业表（agent.py _ASR_HOTWORDS["cantonese"]，逐词一致）
  extended  行业表 + 候选追加词（与另一路落表完全一致）

链路：TTS sidecar(:8788) 合成客户音频（每句只合成一次、三档共用同一条音频=
公平 A/B），ASR sidecar(:8787) 直打——POST /api/start?language=cantonese&context=
开会话（context 即「Vocabulary: …」串，与 agent asr_hotword_context() 同格式），
POST /api/finish 整包 PCM body 一次性解码（sidecar 生产契约的整句路径）。
逐句独立会话，句间 sleep ≥0.6s；三档按句交错跑（同 GPU 条件，成本对比无漂移）。

判定 = 关键词命中（每句预登记 1-3 个，繁体登记、繁→简+变体归一后匹配） +
全句归一化相似度（SequenceMatcher，归一口径同 probe_latency_soak._norm_text，
外加繁→简与 ASR 简化渲染变体折叠）。耗时 = start→finish 端到端墙钟（含
Vocabulary prefill，extended 档词表更长即量此项；判据：增量 <15% 算无感）。

用法：
  .venv312/bin/python scripts/probe_hotword_ab.py
  PROBE_TAG=ext PROBE_ROUNDS=2 .venv312/bin/python scripts/probe_hotword_ab.py
结果落 JSON：scripts/.probe_hotword_ab.<tag>.json
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ASR_URL = os.environ.get("ASR_URL", "http://127.0.0.1:8787")
TTS_URL = os.environ.get("TTS_URL", "http://127.0.0.1:8788")
TAG = os.environ.get("PROBE_TAG", "ab")
ROUNDS = int(os.environ.get("PROBE_ROUNDS", "2") or 2)
SENT_GAP_S = float(os.environ.get("PROBE_SLEEP_S", "0.6") or 0.6)
ROUND_GAP_S = 0.5
LANGUAGE = "cantonese"
TTS_VOICE = "Vivian"  # 与 e2e_real_customer.CUSTOMER_VOICE 同源

# ---- 词表（只读常量，不改 agent.py）----

# 现有行业基线表 = agent.py _ASR_HOTWORDS["cantonese"] 逐词拷贝（2026-09-17 读数）。
VOCAB_INDUSTRY: tuple[str, ...] = (
    "單號", "運單", "賠償", "運費", "專員", "集運", "時效", "上門", "追蹤", "核實",
    "WhatsApp", "微信",
)
# 候选扩容词（与另一路 agent 落表完全一致）。
VOCAB_EXTRA: tuple[str, ...] = (
    "詐騙", "呃人", "證明", "機器人", "投訴", "主管", "人工", "轉接", "退款",
    "倉庫", "熱線", "官網",
)

TIERS: tuple[str, ...] = ("none", "current", "extended")


def merge_vocab(base: tuple[str, ...], extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """行业表 + 追加词去重合并（保序，大小写不敏感去重——对齐 agent 装配语义）。"""
    seen: set[str] = set()
    out: list[str] = []
    for w in base + tuple(extra):
        k = w.strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(w.strip())
    return tuple(out)


def build_context(words: tuple[str, ...]) -> str:
    """格式对齐官方模型卡/agent asr_hotword_context()：「Vocabulary: w1, w2, …」。"""
    if not words:
        return ""
    return "Vocabulary: " + ", ".join(words)


CONTEXTS: dict[str, str] = {
    "none": "",
    "current": build_context(merge_vocab(VOCAB_INDUSTRY)),
    "extended": build_context(merge_vocab(VOCAB_INDUSTRY, VOCAB_EXTRA)),
}

# ---- 用例：10 句粤语异议域句 + 关键词 ----
# 关键词繁体登记，匹配前与转写同口径归一（繁→简+变体折叠）。
# 句 5 特意避开「主管/電話」：真实通话已观测的坏转写「一旦主管打电话，系给」
# 两词俱全，作关键词会假绿；改用「问号码」语义词（幾多/多少/号码）判活。
SENTENCES: list[dict] = [
    {"text": "你係咪詐騙集團嚟㗎", "keywords": ("詐騙", "集團")},
    {"text": "點證明你唔係呃人嘅", "keywords": ("證明", "呃人")},
    {"text": "你係咪機器人嚟㗎", "keywords": ("機器人",)},
    {"text": "我已經嬲咗好耐喇", "keywords": ("嬲",)},
    {"text": "你哋主管嘅電話係幾多", "keywords": ("幾多", "多少", "號碼")},
    {"text": "仲有咩好講呀你話我知", "keywords": ("話我知", "好講")},
    {"text": "我個件而家去咗邊度", "keywords": ("邊度", "哪裡")},
    {"text": "賠償實際賠到幾多錢", "keywords": ("賠償", "賠到")},
    {"text": "你哋查唔查到我個單號", "keywords": ("單號", "查唔查")},
    {"text": "你哋個倉喺邊度㗎", "keywords": ("倉",)},
]

# ---- 归一化（纯函数）----
# 繁→简覆盖探针词域；ASR 简化渲染变体折叠（係/系→是、嘅→的、哋→们、
# 咗→了、嚟→来、喇/啦→啦、喺→在、唔→不、㗎/噶→嘎）——两侧同折叠，
# 只为对齐，不影响「谁的转写更准」的相对比较。
_FOLD: dict[str, str] = {
    "係": "是", "系": "是", "詐": "诈", "騙": "骗", "團": "团", "嚟": "来",
    "㗎": "嘎", "噶": "嘎", "點": "点", "證": "证", "嘅": "的", "機": "机",
    "經": "经", "咗": "了", "喇": "啦", "哋": "们", "電": "电", "話": "话",
    "幾": "几", "麼": "么", "講": "讲", "咩": "乜", "賠": "赔", "償": "偿",
    "實": "实", "際": "际", "錢": "钱", "單": "单", "號": "号", "倉": "仓",
    "喺": "在", "邊": "边", "個": "个", "運": "运", "費": "费", "專": "专",
    "員": "员", "時": "时", "門": "门", "蹤": "踪", "訴": "诉", "轉": "转",
    "熱": "热", "線": "线", "網": "网", "裡": "里", "唔": "不",
}
_PUNCT_RE = re.compile(r"[\s。，,．.！!？?～~、；;：:'\"()（）…—·「」『』《》]")


def _norm_text(s: str) -> str:
    s = "".join(_FOLD.get(ch, ch) for ch in str(s or ""))
    return _PUNCT_RE.sub("", s).lower()


KEYWORDS_NORM: list[list[str]] = [[_norm_text(k) for k in s["keywords"]] for s in SENTENCES]
SENTS_NORM: list[str] = [_norm_text(s["text"]) for s in SENTENCES]


def keyword_hits(transcript_norm: str, keywords_norm: list[str]) -> list[str]:
    """命中 = 任一登记关键词以归一化子串形式出现（返回命中的归一化词）。"""
    return [k for k in keywords_norm if k and k in transcript_norm]


def similarity(expected_norm: str, got_norm: str) -> float:
    if not expected_norm and not got_norm:
        return 1.0
    return SequenceMatcher(None, expected_norm, got_norm).ratio()


def pick_best(records: list[dict]) -> dict:
    """同档同句两轮取好成绩：命中优先（任一轮命中即 hit），相似度取最大轮。"""
    hit_any = any(r["hits"] for r in records)
    best = max(records, key=lambda r: (bool(r["hits"]), r["sim"]))
    return {
        "hit": hit_any,
        "sim": max(r["sim"] for r in records),
        "transcript": best["transcript"],
        "round": best["round"],
    }


def summarize_tier(records: list[dict], per_sentence: list[dict]) -> dict:
    hits = sum(1 for p in per_sentence if p["hit"])
    mean_sim = sum(p["sim"] for p in per_sentence) / len(per_sentence)
    mean_e2e = sum(r["e2e_s"] for r in records) / len(records)
    return {"tier": records[0]["tier"], "hits": hits, "mean_sim": mean_sim,
            "mean_e2e_s": mean_e2e}


# ---- sidecar 调用（只读 HTTP）----

def tts_pcm(text: str, lang: str = LANGUAGE) -> bytes:
    with httpx.Client(timeout=60) as client:
        r = client.post(
            f"{TTS_URL}/v1/audio/speech",
            json={"input": text, "language": lang, "voice": TTS_VOICE, "sample_rate": 16000},
        )
        r.raise_for_status()
        return r.content


def silence_pcm(seconds: float, sr: int = 16000) -> bytes:
    return b"\x00\x00" * int(sr * seconds)


def asr_decode(pcm: bytes, context: str) -> tuple[str, float]:
    """新会话整包解码；返回 (转写, start→finish 端到端秒)。"""
    t0 = time.perf_counter()
    with httpx.Client(timeout=180) as client:
        r = client.post(f"{ASR_URL}/api/start",
                        params={"language": LANGUAGE, "context": context})
        r.raise_for_status()
        sid = r.json()["session_id"]
        r = client.post(f"{ASR_URL}/api/finish", params={"session_id": sid}, content=pcm)
        r.raise_for_status()
        text = str(r.json().get("text") or "")
    return text, time.perf_counter() - t0


# ---- 主流程 ----

def main() -> int:
    print(f"ASR={ASR_URL} TTS={TTS_URL} rounds={ROUNDS} lang={LANGUAGE}")
    print("--- 词表长度 ---")
    for tier in TIERS:
        ctx = CONTEXTS[tier]
        n = len(merge_vocab(VOCAB_INDUSTRY)) if tier == "current" else (
            len(merge_vocab(VOCAB_INDUSTRY, VOCAB_EXTRA)) if tier == "extended" else 0)
        note = ""
        if tier == "extended":
            ok200 = "✓≤200" if len(ctx) <= 200 else "✗>200"
            ok120 = "✓≤120(现有agent护栏)" if len(ctx) <= 120 else "✗>120(agent现有护栏,装配会截)"
            note = f"  {ok200} {ok120}"
        print(f"  {tier:9} words={n:2} context_len={len(ctx):3}{note}")
    print(f"  extended context: {CONTEXTS['extended']}")

    # 音频只合成一次，三档共用（公平 A/B）
    print("--- TTS 合成（每句一次，三档共用）---")
    audios: list[bytes] = []
    for i, s in enumerate(SENTENCES):
        pcm = silence_pcm(0.2) + tts_pcm(s["text"]) + silence_pcm(0.2)
        audios.append(pcm)
        print(f"  [{i + 1:02d}] {len(pcm) // 32}ms  {s['text']}")
        time.sleep(0.2)

    records: list[dict] = []
    for rnd in range(1, ROUNDS + 1):
        print(f"--- Round {rnd}/{ROUNDS} ---")
        for i, s in enumerate(SENTENCES):
            kws = KEYWORDS_NORM[i]
            row_parts = []
            for tier in TIERS:
                text, e2e = asr_decode(audios[i], CONTEXTS[tier])
                tn = _norm_text(text)
                rec = {"round": rnd, "tier": tier, "idx": i + 1, "text": s["text"],
                       "transcript": text, "hits": keyword_hits(tn, kws),
                       "sim": similarity(SENTS_NORM[i], tn), "e2e_s": round(e2e, 3)}
                records.append(rec)
                mark = "HIT " if rec["hits"] else "miss"
                row_parts.append(f"{tier:9} {mark} sim={rec['sim']:.2f} {e2e * 1000:5.0f}ms | {text[:34]}")
            print(f"  [{i + 1:02d}] {s['text']}")
            for part in row_parts:
                print(f"       {part}")
            time.sleep(SENT_GAP_S)
        if rnd < ROUNDS:
            time.sleep(ROUND_GAP_S)

    # ---- 汇总（同档两轮取好成绩）----
    print("=" * 72)
    print("逐句转写对照（两轮取好成绩轮）")
    tier_best: dict[str, list[dict]] = {t: [] for t in TIERS}
    for i, s in enumerate(SENTENCES):
        print(f"  [{i + 1:02d}] {s['text']}")
        for tier in TIERS:
            recs = [r for r in records if r["tier"] == tier and r["idx"] == i + 1]
            best = pick_best(recs)
            tier_best[tier].append(best)
            mark = "✓" if best["hit"] else "✗"
            print(f"       {tier:9} {mark} sim={best['sim']:.2f} r{best['round']} | {best['transcript']}")

    print("=" * 72)
    stats: dict[str, dict] = {}
    for tier in TIERS:
        recs = [r for r in records if r["tier"] == tier]
        st = summarize_tier(recs, tier_best[tier])
        stats[tier] = st
        print(f"  {tier:9} 命中 {st['hits']}/10  平均句相似度 {st['mean_sim']:.3f}  "
              f"平均端到端 {st['mean_e2e_s'] * 1000:.0f}ms")
    base = stats["none"]["mean_e2e_s"]
    delta = (stats["extended"]["mean_e2e_s"] - base) / base * 100 if base else 0.0
    print(f"  cost_delta(extended vs none) = {delta:+.1f}%  ({'无感 <15%' if abs(delta) < 15 else '可感 ≥15%'})")

    verdict = (f"HOTWORD_AB none={stats['none']['hits']}/10 "
               f"current={stats['current']['hits']}/10 "
               f"extended={stats['extended']['hits']}/10 cost_delta={delta:.0f}%")
    print(verdict)

    out = ROOT / "scripts" / f".probe_hotword_ab.{TAG}.json"
    out.write_text(json.dumps(
        {"tag": TAG, "rounds": ROUNDS, "contexts": CONTEXTS,
         "sentences": SENTENCES, "records": records,
         "summary": stats, "cost_delta_pct": delta},
        ensure_ascii=False, indent=1))
    print(f"saved -> {out.name}")
    return 0 if stats["extended"]["hits"] >= stats["none"]["hits"] else 1


if __name__ == "__main__":
    sys.exit(main())
