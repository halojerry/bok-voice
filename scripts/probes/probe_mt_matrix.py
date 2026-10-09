"""B 线 MT 车道选型矩阵探针（2026-10-08 W1,用户指令:三端点实弹定车道）。

同 payload × 四臂逐 delta 计时 + 译文质量抽查（普→粤,含术语句）:
  1. deepseek 官方（现役基线,设置库 mt 车道）
  2. qwen-mt-flash（DashScope;原生 translation_options.terms 术语注入;厂商
     同传同款——请求形状镜像 m芯片 openai-client.ts:单 user 消息+顶层
     translation_options;RPM 60 紧约束。厂商 .env 指工作台专属域名,
     并列通用 dashscope 域对照臂）
  3. 火山方舟 deepseek-v4.1-flash（ARK OpenAI 兼容;key=env ARK_API_KEY;
     豆包 ASR 同账号 key 已实证 401 不通用——此臂需控制台真 ARK key）
  4. opencode zen deepseek-v4.1-flash（env OPENCODE_API_KEY;对照臂,
     合规面存疑不建议生产,只测速度面;CF 拦 urllib 指纹→UA 对齐 SDK 形状;
     go 网关路由需 x-opencode-session 头,HTTP400 实测）

判读口径同 probe_deepseek_stream:first=喂 TTS 解锁时刻;span=展宽;
quality 列人工眼测（术语是否落地/是否普通话冒充粤语）。晚峰 18:00-22:00
复测记录时段。

用法:
  OPENCODE_API_KEY=... .venv312/bin/python scripts/probes/probe_mt_matrix.py
凭据:厂商 .env(金喜同传还原)的 QWEN_*、设置库路由 mt 车道、env 补充;
全部服务端读,绝不打印明文。出站唯一 chokepoint=_post_sse:https 白名单
五域精确匹配(拒端口/凭据注入),不过闸即 SystemExit。
"""
from __future__ import annotations
# --- scripts import bootstrap (G1) ---
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))

import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DB = Path.home() / "Library/Application Support/BokVoice/bok_voice.db"
VENDOR_ENV = Path("/Users/halo/Documents/m芯片/.env")
# 精确主机白名单(五域,无通配/后缀):厂商 .env 工作台域名与通用域并列枚举。
_ALLOWED_HOSTS = frozenset(
    {
        "api.deepseek.com",
        "ark.cn-beijing.volces.com",
        "dashscope.aliyuncs.com",
        "opencode.ai",
        "llm-fhr9fp1rvliem7dd.cn-beijing.maas.aliyuncs.com",
    }
)
_UA = "OpenAI/Python 1.99.1 bok-probe/1.0"

SYSTEM = (
    "你是粤语同声传译译员。把用户提供的普通话口语即时翻译成地道粤语口语。"
    "只输出译文,不解释不回答;ASR 转写可能含同音误听,结合上下文修正明显误识后翻译;"
    "绝不虚构内容;语气词照译。"
)
SENTS = [
    "你好呀我想问一下,你们这个集运怎么收费?",
    "帮我查一下顺丰的单号,还有多久到广州?",   # 术语句(顺丰=SF Express)
    "如果数量比较大的话,运费能不能再便宜一点点,毕竟我们是长期合作的老客户了。",
]
TERMS = [("顺丰", "SF Express")]


def _url_ok(url: str) -> bool:
    """SSRF 白名单(probe_voice_style_gate 同款纪律):https+五域精确匹配。"""
    parts = urllib.parse.urlsplit(str(url or ""))
    return (
        parts.scheme == "https"
        and (parts.hostname or "").lower() in _ALLOWED_HOSTS
        and parts.port in (None, 443)
        and not parts.username
        and not parts.password
    )


def _load_deepseek_lane() -> tuple[str, str, str]:
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        row = conn.execute("SELECT model_routing_json FROM global_settings LIMIT 1").fetchone()
        conn.close()
        entry = ((json.loads(row[0]) if row else {}).get("lanes") or {}).get("mt") or {}
        return (
            str(entry.get("base_url") or "").strip().rstrip("/"),
            str(entry.get("model") or "").strip(),
            str(entry.get("api_key") or "").strip(),
        )
    except Exception:  # noqa: BLE001
        return "", "", os.environ.get("DEEPSEEK_API_KEY", "").strip()


def _load_vendor_env() -> dict:
    """金喜同传还原的 .env(QWEN_*);只回字典,调用方绝不打印值。"""
    out = {}
    try:
        for line in VENDOR_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        pass
    return out


def _post_sse(base: str, key: str, body: dict):
    """唯一出站 chokepoint:base+路径拼 URL → SSRF 白名单闸 → POST SSE。"""
    url = str(base or "").rstrip("/") + "/chat/completions"
    if not _url_ok(url):
        raise SystemExit(f"[probe] endpoint failed SSRF allowlist: {url}")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": _UA,  # CF 1010 拦 urllib 默认指纹(opencode 臂实测)
    }
    if "opencode.ai" in url:
        # zen go 网关路由键(HTTP400 MissingSessionID 实测):任意会话标识即可。
        headers["x-opencode-session"] = "bok-mt-matrix-probe"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST",
    )
    return urllib.request.urlopen(req, timeout=90)


def run_arm(name: str, base: str, key: str, model: str, sent: str, *, qwen: bool) -> None:
    """单臂单句:逐 delta 计时+译文抽样打印。qwen 臂走 translation_options。"""
    if qwen:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": sent}],
            "stream": True, "temperature": 0.1,
            "translation_options": {
                "source_lang": "auto", "target_lang": "yue",
                "terms": [{"source": s, "target": t} for s, t in TERMS],
            },
        }
    else:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": sent},
            ],
            "stream": True, "max_tokens": 256, "temperature": 0.3,
            "thinking": {"type": "disabled"},
        }
    t0 = time.perf_counter()
    deltas: list[tuple[float, str]] = []
    err = ""
    try:
        resp = _post_sse(base, key, body)
        for raw in resp:
            t = time.perf_counter() - t0
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                j = json.loads(line[6:])
            except Exception:  # noqa: BLE001
                continue
            for ch in j.get("choices") or []:
                piece = (((ch.get("delta") or {}).get("content")) or "")
                if piece:
                    deltas.append((t, piece))
    except urllib.error.HTTPError as e:
        err = f"HTTP{e.code}:{e.read()[:120]!r}"
    except Exception as exc:  # noqa: BLE001
        err = repr(exc)[:120]
    total = time.perf_counter() - t0
    if err:
        print(f"  {name}: FAILED {err}", flush=True)
        return
    text = "".join(t for _, t in deltas)
    if not deltas:
        print(f"  {name}: NO DELTAS total={total*1000:.0f}ms text={text[:40]!r}", flush=True)
        return
    first, last = deltas[0][0], deltas[-1][0]
    spread = sum(
        1 for i in range(len(deltas) - 1) if deltas[i + 1][0] - deltas[i][0] > 0.03
    )
    print(
        f"  {name}: first={first*1000:.0f}ms span={(last-first)*1000:.0f}ms "
        f"total={total*1000:.0f}ms deltas={len(deltas)} spread30ms={spread} "
        f"text={text[:44]}",
        flush=True,
    )


def main() -> int:
    vendor = _load_vendor_env()
    ds_base, ds_model, ds_key = _load_deepseek_lane()
    ark_key = os.environ.get("ARK_API_KEY", "").strip()
    oc_key = os.environ.get("OPENCODE_API_KEY", "").strip()
    qwen_key = vendor.get("QWEN_API_KEY", "")
    qwen_base = (vendor.get("QWEN_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    qwen_model = vendor.get("QWEN_MODEL") or "qwen-mt-flash"
    if qwen_model and "mt" not in qwen_model.lower():
        qwen_model = "qwen-mt-flash"  # 厂商 .env 若指对话模型,MT 臂仍用 mt 档

    arms: list[tuple[str, str, str, str, bool]] = []
    if ds_key:
        arms.append(("deepseek官方 ", ds_base, ds_key, ds_model or "deepseek-flash", False))
    if qwen_key:
        arms.append(("qwen-mt-flash", qwen_base, qwen_key, qwen_model, True))
        arms.append(("qwen-mt-通用域", "https://dashscope.aliyuncs.com/compatible-mode/v1",
                     qwen_key, qwen_model, True))
    else:
        print("qwen arm: no key (vendor .env QWEN_API_KEY missing)", flush=True)
    if ark_key:
        arms.append(("方舟v4.1-flash", "https://ark.cn-beijing.volces.com/api/v3", ark_key,
                     os.environ.get("ARK_MODEL", "deepseek-v4.1-flash"), False))
    else:
        print("ark arm: no ARK_API_KEY (豆包 ASR key 已实证 401 不通用,需控制台方舟 key)", flush=True)
    if oc_key:
        arms.append(("opencode zen", "https://opencode.ai/zen/go/v1", oc_key,
                     os.environ.get("OPENCODE_MODEL", "deepseek-v4.1-flash"), False))
    else:
        print("opencode arm: no key (OPENCODE_API_KEY env missing)", flush=True)

    print(f"arms={len(arms)} round=1 (fresh) @ {time.strftime('%H:%M')}", flush=True)
    for sent in SENTS:
        print(f"payload: {sent}", flush=True)
        for name, base, key, model, qwen in arms:
            run_arm(name, base, key, model, sent, qwen=qwen)
    print("round=2 (repeat, stability arm)", flush=True)
    for sent in SENTS[:1]:
        for name, base, key, model, qwen in arms:
            run_arm(name, base, key, model, sent, qwen=qwen)
    print(
        "\n判读:first 最低且 span 有展宽=可解锁「LLM 边生成边喂 TTS」;术语句看"
        "『顺丰』是否译成 SF Express(qwen 臂应原生落地);晚峰 18:00-22:00 复测"
        "记录时段对比。",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
