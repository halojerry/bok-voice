"""云端 Qwen3-ASR-Flash（阿里云百炼 DashScope）vs 本地 sidecar 同音频 A/B 探针。

背景：量化云端 qwen3-asr-flash 对本地 8bit 残余粤语硬混淆（嬲/賠償/單號/倉）
的提升与延迟代价。方法论纪律：TTS 每句只合成一次 PCM，同一段音频先发本地
:8787 再发云端——各跑各的再比数字是假 A/B（TTS 渲染跨次 ±2-3 句方差实证）。

（本文件 2026-09-17 重写，接替 2026-09-12 三引擎筛查版——旧语料/MiniMax 腿
见 git 历史；DashScope 响应形状兜底与 certifi CA 固化两件实战经验保留。）

三档照旧（none/current/extended）：本地走 sidecar context 串；云端把同一份
词表转成首位 system message（"Vocabulary: …"——Qwen3-ASR 同家族同格式，官方
口径上下文靠词表匹配生效、text 需含待识别原词）+ asr_options.language 恒钉
粤语枚举 "yue"（DashScope 外部接口真字面量，仓库规范拼写仍为 cantonese；
术语门禁按行豁免带引号字面量）。

链路：TTS sidecar(:8788) 合成 → WAV 封装 base64 data URL →
POST {base}/api/v1/services/aigc/multimodal-generation/generation
（DashScope 协议同步短音频，≤10MB；文本在 output.choices[0].message.content，
部分转写专用响应形为 {"sentence": {"text": …}}，两种都解）。

用法（key 只从 env 读，严禁写入任何文件/提交）：
  DASHSCOPE_API_KEY=sk-xxx .venv312/bin/python scripts/probe_cloud_asr_ab.py
可选 env：PROBE_TAG / PROBE_ROUNDS(默认2) / ASR_URL / TTS_URL /
  DASHSCOPE_ASR_URL(完整端点，默认官方域) / DASHSCOPE_ASR_MODEL(默认
  qwen3-asr-flash) / CLOUD_AB_LIMIT(冒烟截前 N 句) / CLOUD_AB_SKIP_DIGITS=1 /
  CLOUD_AB_WAV_DIR(默认 /tmp/bok_cloud_asr_ab，临时 wav 缓存)
结果落 JSON：scripts/.probe_cloud_asr_ab.<tag>.json
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
import wave
from pathlib import Path

import httpx

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))

import probe_asr_digits_ab as dab  # noqa: E402
import probe_hotword_ab as hab  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
API_KEY_ENV = "DASHSCOPE_API_KEY"
CLOUD_URL = os.environ.get(
    "DASHSCOPE_ASR_URL",
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
)
CLOUD_MODEL = os.environ.get("DASHSCOPE_ASR_MODEL", "qwen3-asr-flash")
# DashScope asr_options.language 粤语枚举值 "yue"（外部接口字面量，逐字不改）。
CLOUD_LANG = "yue"
WAV_DIR = Path(os.environ.get("CLOUD_AB_WAV_DIR", "/tmp/bok_cloud_asr_ab"))
LIMIT = int(os.environ.get("CLOUD_AB_LIMIT", "0") or 0)
SKIP_DIGITS = os.environ.get("CLOUD_AB_SKIP_DIGITS", "") == "1"
ROUNDS = int(os.environ.get("PROBE_ROUNDS", "2") or 2)
TAG = os.environ.get("PROBE_TAG", "cloud")

# 三档词表（云端 hints 档 = 同一份 merge_vocab 结果转 system message）。
TIER_VOCAB: dict[str, tuple[str, ...]] = {
    "none": (),
    "current": hab.merge_vocab(hab.VOCAB_INDUSTRY),
    "extended": hab.merge_vocab(hab.VOCAB_INDUSTRY, hab.VOCAB_EXTRA),
}


class CloudASRError(RuntimeError):
    """云端调用失败（HTTP 非 200 / 响应形状不符 / 缺 key），携带响应体。"""


def cert_env() -> None:
    """venv 无系统 CA：云端调用前把 certifi 固化进 SSL_CERT_FILE（同 bok.py 姿势）。"""
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi

        os.environ["SSL_CERT_FILE"] = certifi.where()
    except Exception:
        pass


# ---- 云端调用（只读 HTTP，key 仅内存）----

def pcm_to_wav_data_url(pcm: bytes, sr: int = 16000) -> str:
    """裸 PCM(s16le 单声道) → WAV 容器 → base64 data URL（DashScope Base64 档）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return "data:audio/wav;base64," + b64


def _extract_text(data: dict) -> str:
    """两种响应形状都解：multimodal choices 形 / 转写专用 {"sentence": …} 形。"""
    if isinstance(data.get("sentence"), dict):
        return str(data["sentence"].get("text") or "")
    msg = data["output"]["choices"][0]["message"]
    content = msg.get("content")
    if isinstance(content, str):
        return content
    return "".join(
        str(p.get("text") or "") for p in (content or []) if isinstance(p, dict)
    )


def cloud_decode(wav_data_url: str, vocab: tuple[str, ...]) -> tuple[str, float]:
    """同步短音频单次转写；返回 (转写, 墙钟秒)。失败抛 CloudASRError（含响应体）。"""
    key = os.environ.get(API_KEY_ENV, "")
    if not key:
        raise CloudASRError(f"missing env {API_KEY_ENV}")
    messages: list[dict] = []
    if vocab:
        # 与本地 context 同源同格式：「Vocabulary: w1, w2, …」
        messages.append({"role": "system", "content": [{"text": hab.build_context(vocab)}]})
    messages.append({"role": "user", "content": [{"audio": wav_data_url}]})
    payload = {
        "model": CLOUD_MODEL,
        "input": {"messages": messages},
        "parameters": {"asr_options": {"language": CLOUD_LANG, "enable_itn": False}},
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    t0 = time.perf_counter()
    with httpx.Client(timeout=60) as client:
        r = client.post(CLOUD_URL, json=payload, headers=headers)
    e2e = time.perf_counter() - t0
    if r.status_code != 200:
        raise CloudASRError(f"HTTP {r.status_code}: {r.text[:2000]}")
    try:
        text = _extract_text(r.json())
    except (KeyError, IndexError, TypeError, AttributeError):
        raise CloudASRError(
            f"unexpected response: {json.dumps(r.json(), ensure_ascii=False)[:2000]}")
    return text.strip(), e2e


def save_wav(name: str, pcm: bytes) -> None:
    try:
        WAV_DIR.mkdir(parents=True, exist_ok=True)
        (WAV_DIR / name).write_bytes(pcm)
    except OSError:
        pass  # 缓存是可选件，失败不影响探针


def pctl(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * (len(s) - 1)))]


def main() -> int:
    if not os.environ.get(API_KEY_ENV, ""):
        print(f"ERROR: 缺少 env {API_KEY_ENV}（key 只从 env 读，勿写文件）")
        return 2
    cert_env()
    sentences = hab.SENTENCES[:LIMIT] if LIMIT > 0 else hab.SENTENCES
    n = len(sentences)
    print(f"ASR={hab.ASR_URL} TTS={hab.TTS_URL} cloud={CLOUD_MODEL}")
    print(f"endpoint={CLOUD_URL}")
    print(f"rounds={ROUNDS} lang={hab.LANGUAGE} sentences={n} 云端语言枚举={CLOUD_LANG}")
    print("--- 词表（云端 system message 与本地 context 同串）---")
    for tier in hab.TIERS:
        print(f"  {tier:9} words={len(TIER_VOCAB[tier]):2} len={len(hab.CONTEXTS[tier]):3}")

    # 音频只合成一次，本地/云端共用（同音频对照纪律）
    print("--- TTS 合成（每句一次，双端共用）---")
    audios: list[bytes] = []
    for i, s in enumerate(sentences):
        pcm = hab.silence_pcm(0.2) + hab.tts_pcm(s["text"]) + hab.silence_pcm(0.2)
        audios.append(pcm)
        save_wav(f"sent_{i + 1:02d}.wav", pcm)
        print(f"  [{i + 1:02d}] {len(pcm) // 32}ms  {s['text']}")
        time.sleep(0.2)

    records: list[dict] = []
    cloud_errors = 0
    for rnd in range(1, ROUNDS + 1):
        print(f"--- Round {rnd}/{ROUNDS} ---")
        for i, s in enumerate(sentences):
            kws = hab.KEYWORDS_NORM[i]
            wav_url = pcm_to_wav_data_url(audios[i])
            row_parts = []
            for tier in hab.TIERS:
                # 本地先行（生产基线），云端随后——同一段 PCM。
                ltext, le2e = hab.asr_decode(audios[i], hab.CONTEXTS[tier])
                ltn = hab._norm_text(ltext)
                records.append({"backend": "local", "round": rnd, "tier": tier,
                                "idx": i + 1, "transcript": ltext,
                                "hits": hab.keyword_hits(ltn, kws),
                                "sim": hab.similarity(hab.SENTS_NORM[i], ltn),
                                "e2e_s": round(le2e, 3), "error": ""})
                lmark = "HIT " if records[-1]["hits"] else "miss"
                row_parts.append(f"local {tier:9} {lmark} sim={records[-1]['sim']:.2f} "
                                 f"{le2e * 1000:5.0f}ms | {ltext[:30]}")
                crec: dict
                try:
                    ctext, ce2e = cloud_decode(wav_url, TIER_VOCAB[tier])
                    ctn = hab._norm_text(ctext)
                    crec = {"backend": "cloud", "round": rnd, "tier": tier,
                            "idx": i + 1, "transcript": ctext,
                            "hits": hab.keyword_hits(ctn, kws),
                            "sim": hab.similarity(hab.SENTS_NORM[i], ctn),
                            "e2e_s": round(ce2e, 3), "error": ""}
                except CloudASRError as exc:
                    cloud_errors += 1
                    print(f"  [cloud-error] [{i + 1:02d} {tier}] {exc}")
                    crec = {"backend": "cloud", "round": rnd, "tier": tier,
                            "idx": i + 1, "transcript": "", "hits": [], "sim": 0.0,
                            "e2e_s": 0.0, "error": str(exc)[:500]}
                records.append(crec)
                if crec["error"]:
                    row_parts.append(f"cloud {tier:9} ERR  {crec['error'][:60]}")
                else:
                    cmark = "HIT " if crec["hits"] else "miss"
                    row_parts.append(f"cloud {tier:9} {cmark} sim={crec['sim']:.2f} "
                                     f"{ce2e * 1000:5.0f}ms | {ctext[:30]}")
            print(f"  [{i + 1:02d}] {s['text']}")
            for part in row_parts:
                print(f"       {part}")
            time.sleep(hab.SENT_GAP_S)
        time.sleep(hab.ROUND_GAP_S)

    # ---- 汇总（同档多轮取好成绩；轮次吸收解码方差，音频本身同一份）----
    print("=" * 72)
    print("逐句转写对照（多轮取好成绩轮）")
    for i, s in enumerate(sentences):
        print(f"  [{i + 1:02d}] {s['text']}")
        for backend in ("local", "cloud"):
            for tier in hab.TIERS:
                recs_ok = [r for r in records
                           if r["backend"] == backend and r["tier"] == tier
                           and r["idx"] == i + 1 and not r["error"]]
                if recs_ok:
                    b = hab.pick_best(recs_ok)
                    m = "✓" if b["hit"] else "✗"
                    print(f"       {backend:5} {tier:9} {m} sim={b['sim']:.2f} "
                          f"| {b['transcript']}")
                else:
                    print(f"       {backend:5} {tier:9} ERR | (全部轮次失败)")

    print("=" * 72)
    print("分档汇总（命中率=多轮取好；延迟=该档全部成功解码 p50/p95）")
    lat: dict[str, list[float]] = {"local": [], "cloud": []}
    tier_hits: dict[tuple[str, str], int] = {}
    for backend in ("local", "cloud"):
        for tier in hab.TIERS:
            recs = [r for r in records if r["backend"] == backend and r["tier"] == tier]
            bests = []
            for i in range(n):
                rr = [r for r in recs if r["idx"] == i + 1 and not r["error"]]
                if rr:
                    bests.append(hab.pick_best(rr))
            hits = sum(1 for b in bests if b["hit"])
            tier_hits[(backend, tier)] = hits
            mean_sim = (sum(b["sim"] for b in bests) / len(bests)) if bests else 0.0
            ok_lat = [r["e2e_s"] for r in recs if not r["error"]]
            lat[backend].extend(ok_lat)
            print(f"  {backend:5} {tier:9} 命中 {hits}/{n}  平均sim {mean_sim:.3f}  "
                  f"p50={pctl(ok_lat, 0.50) * 1000:.0f}ms "
                  f"p95={pctl(ok_lat, 0.95) * 1000:.0f}ms  解码N={len(ok_lat)}")
    print(f"  全档延迟  local p50={pctl(lat['local'], 0.50) * 1000:.0f}ms "
          f"p95={pctl(lat['local'], 0.95) * 1000:.0f}ms | "
          f"cloud p50={pctl(lat['cloud'], 0.50) * 1000:.0f}ms "
          f"p95={pctl(lat['cloud'], 0.95) * 1000:.0f}ms")

    # 结论行取 extended 档（生产候选配置；两后端同判口径），延迟取该档 p50。
    lh = tier_hits[("local", "extended")]
    ch = tier_hits[("cloud", "extended")]
    ext_l = [r["e2e_s"] for r in records if r["backend"] == "local" and r["tier"] == "extended"]
    ext_c = [r["e2e_s"] for r in records if r["backend"] == "cloud"
             and r["tier"] == "extended" and not r["error"]]
    vline = (f"CLOUD_AB local={lh}/{n} cloud={ch}/{n} "
             f"latency_local={pctl(ext_l, 0.50) * 1000:.0f}ms "
             f"latency_cloud={pctl(ext_c, 0.50) * 1000:.0f}ms")
    if cloud_errors:
        vline += f" cloud_errors={cloud_errors}"
    print(vline)

    # ---- 数字铁律（同一套音频双端验，判据=数字串逐位一致）----
    digits_pass = True
    digits_detail: list[dict] = []
    if not SKIP_DIGITS:
        print("=" * 72)
        print("--- 数字铁律（extended 档，同音频双端）---")
        for j, sent in enumerate(dab.DIGIT_SENTENCES):
            pcm = hab.silence_pcm(0.2) + hab.tts_pcm(sent) + hab.silence_pcm(0.2)
            save_wav(f"digit_{j + 1}.wav", pcm)
            wav_url = pcm_to_wav_data_url(pcm)
            want = dab.digit_runs(hab._norm_text(sent))
            row: dict = {"sentence": sent, "want": want}
            print(f"  「{sent}」")
            print(f"      期望={want}")
            for backend in ("local", "cloud"):
                best: tuple[bool, list[str], str, float] | None = None
                for _ in range(ROUNDS):
                    try:
                        if backend == "local":
                            text, e2e = hab.asr_decode(pcm, hab.CONTEXTS["extended"])
                        else:
                            text, e2e = cloud_decode(wav_url, TIER_VOCAB["extended"])
                    except CloudASRError as exc:
                        print(f"  [cloud-error] [digits {j + 1} {backend}] {exc}")
                        cloud_errors += 1
                        best = (False, [], f"ERR {str(exc)[:80]}", 0.0)
                        break
                    got = dab.digit_runs(hab._norm_text(text))
                    ok = got == want
                    if best is None or (ok and not best[0]) or (ok == best[0] and e2e < best[3]):
                        best = (ok, got, text, e2e)
                    time.sleep(hab.SENT_GAP_S)
                assert best is not None
                row[backend] = {"ok": best[0], "got": best[1],
                                "text": best[2], "e2e_s": round(best[3], 3)}
                digits_pass = digits_pass and best[0]
                m = "✓" if best[0] else "✗"
                print(f"      {backend:5} {m} 实得={best[1]}  "
                      f"{best[3] * 1000:.0f}ms  「{best[2][:40]}」")
            digits_detail.append(row)
        print(f"DIGITS_CLOUD_AB {'PASS' if digits_pass else 'FAIL'} (local+cloud 同判)")

    out = ROOT / "scripts" / f".probe_cloud_asr_ab.{TAG}.json"
    out.write_text(json.dumps(
        {"tag": TAG, "rounds": ROUNDS, "model": CLOUD_MODEL, "endpoint": CLOUD_URL,
         "cloud_lang": CLOUD_LANG, "sentences": sentences, "records": records,
         "digits": digits_detail, "digits_pass": digits_pass,
         "cloud_errors": cloud_errors, "verdict_line": vline},
        ensure_ascii=False, indent=1))
    print(f"saved -> {out.name}")
    return 0 if (digits_pass and cloud_errors == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
