"""云端 ASR A/B 探针(2026-09-12,用户提议 MiniMax asr-1.0 / Qwen-Audio-3 ASR flash)。

同一批本地 TTS 渲染音频喂三家,量「已知短板命中率 + 整段转写延迟」:
  - local   : Qwen3-ASR sidecar :8787(现役,language 钉定,无热词 context=基线口径)
  - minimax : MiniMax asr-1.0(multipart 文件上传,BCP-47 language 头;先打 intl
              api.minimaxi.com 再回 CN api.minimax.cn,报告实际可用域)
  - dashscope: qwen-audio-3.0-asr-flash(多模态 generation,base64 data URI;
              无 language 参数=测 auto 行为,粤语会不会被普化本身是重要观察项)

语料覆盖现役已知弱点:品牌词(金≠京族)/分组数字(call-5f8bef6b 形态)/三语自然句。
**局限**:TTS 合成音非真人客户声(引擎 A/B 第一道筛选;真人音频复测是二阶段)。
云端两路都是整段上传式——本探针只测「整段转写」,不代表可直接顶进流式轮次路径。

用法:
  .venv312/bin/python scripts/probe_cloud_asr_ab.py [--only local,minimax,dashscope] [--tag ab1]
  MiniMax key 读设置 DB tts.api_key;DashScope key 走 env DASHSCOPE_API_KEY(绝不落盘)。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sqlite3
import sys
import time
import wave
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))
from agent_runtime.flow import _digit_normalize  # noqa: E402  英文数字词归一与运行时同源

TTS_URL = os.environ.get("TTS_URL", "http://127.0.0.1:8788")
ASR_URL = os.environ.get("ASR_URL", "http://127.0.0.1:8787")
# 运行时同款钉定提示(Qwen3-ASR 模型 config 规范名);MiniMax 走 BCP-47 外部枚举。
LOCAL_HINT = {"zh": "Chinese", "cantonese": "Cantonese", "en": "English"}
MM_LANG = {"zh": "zh", "cantonese": "yue", "en": "en"}  # yue=MiniMax BCP-47 外部字面量
MM_BASES = ["https://api.minimaxi.com", "https://api.minimax.cn"]
DS_URL = os.environ.get(
    "DASHSCOPE_ASR_URL",
    "https://llm-fhr9fp1rvliem7dd.cn-beijing.maas.aliyuncs.com"
    "/api/v1/services/aigc/multimodal-generation/generation",
)
DS_MODEL = os.environ.get("DASHSCOPE_ASR_MODEL", "qwen-audio-3.0-asr-flash")

# 语料:check 类型 keyword=品牌词含检(繁简归一),digits=数字串含检(词归一+分组折叠)。
CORPUS: list[dict] = [
    {"lang": "zh", "text": "我在拼多多买的东西还没有到货", "kind": "keyword", "want": "拼多多"},
    {"lang": "zh", "text": "我上一单就是淘宝买的", "kind": "keyword", "want": "淘宝"},
    {"lang": "zh", "text": "这是我的快递单号", "kind": "keyword", "want": "快递单号"},
    {"lang": "zh", "text": "我的单号是三七七八九零", "kind": "digits", "want": "377890"},
    {"lang": "zh", "text": "我的WhatsApp号码是九八五二六六三三", "kind": "digits", "want": "98526633"},
    {"lang": "cantonese", "text": "我件貨係京東買的", "kind": "keyword", "want": "京东"},
    {"lang": "cantonese", "text": "我喺拼多多買嘅嘢仲未到", "kind": "keyword", "want": "拼多多"},
    {"lang": "cantonese", "text": "我個單號係三七七八九零", "kind": "digits", "want": "377890"},
    {"lang": "cantonese", "text": "我嘅WhatsApp係六四三二五四三", "kind": "digits", "want": "6432543"},
    {"lang": "cantonese", "text": "唔好意思我聽唔清楚你可以再講一次嗎", "kind": "keyword", "want": "清楚"},
    {"lang": "en", "text": "I bought it on eBay last month", "kind": "keyword", "want": "ebay"},
    {"lang": "en", "text": "My WhatsApp number is seven five one two two zero", "kind": "digits", "want": "751220"},
    {"lang": "en", "text": "tracking number is nine eight seven six five four three two one", "kind": "digits", "want": "987654321"},
    {"lang": "en", "text": "The parcel went missing at your warehouse", "kind": "keyword", "want": "missing"},
]

_TRAD2SIMP = str.maketrans({
    "貨": "货", "係": "系", "東": "东", "買": "买", "喺": "在", "嘅": "的", "個": "个",
    "單": "单", "號": "号", "們": "们", "講": "讲", "聽": "听", "請": "请", "運": "运",
})


def _norm_text(t: str) -> str:
    """繁->简 + 去空白/标点 + 拉丁小写:文本含检口径。"""
    out = t.translate(_TRAD2SIMP).lower()
    return "".join(ch for ch in out if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _norm_digits(t: str) -> str:
    """数字含检口径:英文/汉字数字词归一成 ASCII,再剔非数字。"""
    return "".join(ch for ch in _digit_normalize(t) if ch.isdigit())


def _cert_env() -> None:
    """venv 无系统 CA:云端调用前把 certifi 固化进 SSL_CERT_FILE(同 bok.py 姿势)。"""
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi

        os.environ["SSL_CERT_FILE"] = certifi.where()
    except Exception:
        pass


def load_minimax_key() -> str:
    db = Path.home() / "Library" / "Application Support" / "BokVoice" / "bok_voice.db"
    row = sqlite3.connect(db).execute("SELECT tts_json FROM global_settings LIMIT 1").fetchone()
    # DB 里 tts_json 就是 tts 配置 dict 本身(CP /api/settings 才外包 {"tts": …})。
    cfg = json.loads((row[0] if row else "") or "{}")
    key = str(cfg.get("api_key") or "").strip()
    return key or os.environ.get("MINIMAX_API_KEY", "")


def tts_pcm(text: str, lang: str) -> bytes:
    r = httpx.post(
        f"{TTS_URL}/v1/audio/speech",
        json={"input": text, "language": lang, "voice": "Vivian", "sample_rate": 16000},
        timeout=60,
    )
    r.raise_for_status()
    return r.content


def pcm_to_wav(pcm: bytes, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def run_local(case: dict, pcm: bytes) -> tuple[str, float]:
    t0 = time.monotonic()
    sid = httpx.post(f"{ASR_URL}/api/start", params={"language": LOCAL_HINT[case["lang"]]}, timeout=30).json()["session_id"]
    r = httpx.post(f"{ASR_URL}/api/finish", params={"session_id": sid}, content=pcm, timeout=120).json()
    text = str(r.get("text") or "")
    return text, (time.monotonic() - t0) * 1000


def run_minimax(case: dict, wav: bytes, client: httpx.Client, base: str) -> tuple[str, float]:
    t0 = time.monotonic()
    r = client.post(
        f"{base}/v1/speech_to_text",
        headers={"language": MM_LANG[case["lang"]]},
        data={"model": "asr-1.0", "response_format": "json"},
        files={"file": ("audio.wav", wav, "audio/wav")},
        timeout=120,
    )
    r.raise_for_status()
    text = str(r.json().get("text") or "")
    return text, (time.monotonic() - t0) * 1000


def run_dashscope(case: dict, wav: bytes, client: httpx.Client) -> tuple[str, float]:
    t0 = time.monotonic()
    body = {
        "model": DS_MODEL,
        "input": {"messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": "data:audio/wav;base64," + base64.b64encode(wav).decode()}},
        ]}]},
        "parameters": {"format": "wav", "sample_rate": "16000"},
    }
    r = client.post(DS_URL, json=body, timeout=120)
    r.raise_for_status()
    data = r.json()
    if isinstance(data.get("sentence"), dict):
        # 转写专用响应形:{"sentence": {"text": …, "words": […]}}(非 multimodal choices 形)
        text = str(data["sentence"].get("text") or "")
    else:
        msg = data["output"]["choices"][0]["message"]
        content = msg.get("content")
        text = content if isinstance(content, str) else "".join(
            str(p.get("text") or "") for p in (content or []) if isinstance(p, dict)
        )
    return text.strip(), (time.monotonic() - t0) * 1000


def score(case: dict, text: str) -> str:
    if not text.strip():
        return "MISS"
    if case["kind"] == "digits":
        want = _norm_digits(case["want"])
        return "HIT" if want in _norm_digits(text) else "MISS"
    want = _norm_text(case["want"])
    return "HIT" if want in _norm_text(text) else "MISS"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="local,minimax,dashscope", help="逗号分隔引擎子集")
    ap.add_argument("--tag", default="ab1", help="结果 JSON 后缀")
    args = ap.parse_args()
    engines = [e.strip() for e in args.only.split(",") if e.strip()]
    _cert_env()

    mm_key = load_minimax_key()
    ds_key = os.environ.get("DASHSCOPE_API_KEY", "")
    clients: dict[str, httpx.Client | None] = {
        "local": None,
        "minimax": httpx.Client(headers={"Authorization": f"Bearer {mm_key}"}) if (mm_key and "minimax" in engines) else None,
        "dashscope": httpx.Client(headers={"Authorization": f"Bearer {ds_key}"}) if (ds_key and "dashscope" in engines) else None,
    }
    if "minimax" in engines and not mm_key:
        print("minimax: 缺 key(设置 DB tts.api_key / MINIMAX_API_KEY)——跳过", file=sys.stderr)
        engines.remove("minimax")
    if "dashscope" in engines and not ds_key:
        print("dashscope: 缺 DASHSCOPE_API_KEY(env)——跳过", file=sys.stderr)
        engines.remove("dashscope")

    # MiniMax 域探测:CN key 对 intl 域可能 401,逐域打一条直到通。
    mm_base = ""
    if "minimax" in engines:
        for base in MM_BASES:
            try:
                probe = clients["minimax"].post(
                    f"{base}/v1/speech_to_text",
                    headers={"language": "zh"},
                    data={"model": "asr-1.0", "response_format": "json"},
                    files={"file": ("audio.wav", pcm_to_wav(tts_pcm("你好", "zh")), "audio/wav")},
                    timeout=60,
                )
                if probe.status_code == 200:
                    mm_base = base
                    break
                print(f"minimax {base} -> HTTP {probe.status_code}: {probe.text[:120]}")
            except Exception as exc:
                print(f"minimax {base} -> {exc!r}")
        if not mm_base:
            print("minimax: 两个域都不可用——剔除", file=sys.stderr)
            engines.remove("minimax")

    results: list[dict] = []
    for case in CORPUS:
        pcm = tts_pcm(case["text"], case["lang"])
        wav = pcm_to_wav(pcm)
        row: dict = {"lang": case["lang"], "text": case["text"], "kind": case["kind"], "want": case["want"], "sec": round(len(pcm) / 2 / 16000, 2)}
        for eng in engines:
            try:
                if eng == "local":
                    text, ms = run_local(case, pcm)
                elif eng == "minimax":
                    text, ms = run_minimax(case, wav, clients["minimax"], mm_base)
                else:
                    text, ms = run_dashscope(case, wav, clients["dashscope"])
                row[eng] = {"text": text, "ms": round(ms), "verdict": score(case, text)}
            except Exception as exc:
                row[eng] = {"text": "", "ms": -1, "verdict": "ERR", "err": repr(exc)[:200]}
        results.append(row)
        cells = " | ".join(
            f"{eng}:{row[eng]['verdict']}" + (f"({row[eng]['ms']}ms)" if row[eng]["ms"] >= 0 else "")
            for eng in engines
        )
        print(f"[{cells}] {case['lang']:9} want={case['want']}")
        for eng in engines:
            if row[eng]["text"]:
                print(f"    {eng:9} {row[eng]['text'][:60]}")

    print("\n===== SUMMARY =====")
    for eng in engines:
        verdicts = [r[eng]["verdict"] for r in results]
        lats = sorted(r[eng]["ms"] for r in results if r[eng]["ms"] >= 0)
        p50 = lats[len(lats) // 2] if lats else -1
        print(
            f"{eng:9} hit={verdicts.count('HIT')}/{len(results)} err={verdicts.count('ERR')} "
            f"latency p50={p50}ms max={lats[-1] if lats else -1}ms"
            + (f" base={mm_base}" if eng == "minimax" and mm_base else "")
        )
    out = ROOT / "scripts" / f".probe_cloud_asr_ab.{args.tag}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"saved -> {out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
