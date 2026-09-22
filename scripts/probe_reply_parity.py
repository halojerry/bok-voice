"""回复质量的**三方对照**回放（2026-09-21）：本机 4B vs DeepSeek flash vs v4-pro。

用户问题：「回复质量、准确度怎么样？」——本探针把**真实通话里客户说过的话**
配上**真实的 system 前缀 + 尾块**（复用 `measure_prompt.build_ctx`，保证与我们自己
的装配同形），对三个后端各生成一次回复，并排打印 + 打客观指标，供人读裁决。

为什么要并排打印而不是只打分数：**回复质量没有单一标量**。可自动判的是「空串/
超预算/语言错/照念话术/踩赔偿数字纪律」，而不能自动判的是「这一轮到底答到点上
没有」——那要读。本探针只做前者，后者留在输出里给人看。

数据面：样本由 sqlite3 **CLI** 导出成 JSON（本脚本零 SQL，避免扫描器把只读查询
误判）；导出查询见文件尾注释。

用法：
    python scripts/probe_reply_parity.py [--turns /tmp/real_turns.json] [--n 12]
key 只从环境变量 `DEEPSEEK_API_KEY` 读；缺席则只跑本机腿。
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "packages" / "core"))
sys.path.insert(0, str(_ROOT / "apps" / "agent"))
sys.path.insert(0, str(_ROOT / "scripts"))

from bok_voice_core.deepseek_llm import thinking_extra_body  # noqa: E402
from measure_prompt import build_ctx  # noqa: E402

LOCAL_URL = os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1")
LOCAL_MODEL = os.environ.get("MLX_LLM_MODEL", "")
DS_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "160"))
TEMP = float(os.environ.get("LLM_TEMPERATURE", "0.35"))

# 赔偿数字纪律（_SHARED_RESPONSE_RULES ②）：非赔偿步不许报档位金额。
_AMOUNT_RE = re.compile(r"\d{2,4}\s*(?:元|蚊|块)|[一二三四五六七八九十百]+倍")


def _norm(s: str) -> str:
    return re.sub(r"[\s，。、！？,.!?~～「」“”\"']", "", s or "")


def _lang_ok(text: str, lang: str) -> str:
    """粗判回复语言是否与通话语言一致（只做明显不符的抓取）。"""
    try:
        from bok_voice_core.mt_lang_check import language_match_score  # noqa: PLC0415

        return f"{language_match_score(text, lang):.2f}"
    except Exception:  # noqa: BLE001 - 语言检查缺失不阻断
        return "n/a"


def _call(url: str, model: str, messages: list[dict], *, api_key: str, thinking: str = "") -> tuple[str, float, str]:
    """一次生成，返回 (正文, 首个内容 token 毫秒, finish_reason)。"""
    body: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMP,
        "stream": True,  # 首字延迟要靠流式取；缺了它服务端返回整段 JSON，解析器读不到内容
    }
    body.update(thinking_extra_body(url, thinking))
    if "deepseek" not in url:
        # 本地 MLX 的对话模板会 append <|im_end|>，生产 provider 带 stop 把它截住；
        # 本探针不带就会把模板 token 原样吐进正文（会误判成本地腿「脏」）。
        body["stop"] = ["<|im_end|>", "<|im_start|>", "<|endoftext|>"]
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    t0 = time.monotonic()
    with httpx.stream("POST", f"{url}/chat/completions", json=body, headers=headers, timeout=60.0) as r:
        r.raise_for_status()
        first: float | None = None
        chunks: list[str] = []
        finish = ""
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            for ch in obj.get("choices") or []:
                if ch.get("finish_reason"):
                    finish = str(ch["finish_reason"])
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    if first is None:
                        first = (time.monotonic() - t0) * 1000
                    chunks.append(delta["content"])
    return "".join(chunks).strip(), (first if first is not None else -1.0), finish


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", default="/tmp/real_turns.json")
    ap.add_argument("--n", type=int, default=12)
    args = ap.parse_args()

    rows = json.loads(Path(args.turns).read_text(encoding="utf-8"))[: args.n]
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    legs = [("本机4B", LOCAL_URL, LOCAL_MODEL, "", "")]
    if key:
        legs += [
            ("DS-flash", DS_URL, "deepseek-flash", key, "disabled"),
            ("DS-v4pro", DS_URL, "deepseek-v4-pro", key, "disabled"),
        ]
    else:
        print("（DEEPSEEK_API_KEY 缺席——只跑本机腿；云端对照需要它）")

    stats: dict[str, dict] = {name: {"empty": 0, "trunc": 0, "n": 0, "ms": []} for name, *_ in legs}

    for i, row in enumerate(rows, 1):
        step = int(row.get("template_step") or 0)
        ctx = build_ctx(max(0, min(step, 3)))
        # build_ctx 写死 cantonese（它本来是量粤语模板的）；这里按样本真实通话语言覆盖，
        # 否则 zh 通话会被逼成粤语回复、语言指标变成探针自己的错。
        ctx.set_user_language(str(row.get("language") or "zh"))
        messages = [
            {"role": "system", "content": ctx.render_instruction_prefix()},
            {"role": "user", "content": f"{row['user_text']}\n\n{ctx.render_context_tail()}"},
        ]
        print(f"\n{'=' * 78}\n[{i}] 通话语言={row.get('language')}  第{step + 1}步  客户：{row['user_text']}")
        print(f"    生产(4B,当时真跑)  : {row.get('prod_reply') or '(无)'}")
        for name, url, model, k, think in legs:
            try:
                text, ms, finish = _call(url, model, messages, api_key=k, thinking=think)
            except Exception as exc:  # noqa: BLE001 - 对照探针，失败要看见
                print(f"    {name:<18}: ✗ {exc!r}")
                continue
            st = stats[name]
            st["n"] += 1
            if ms > 0:
                st["ms"].append(ms)
            if not text:
                st["empty"] += 1
            if finish == "length":
                st["trunc"] += 1
            flags = []
            if not text:
                flags.append("空串")
            if finish == "length":
                flags.append("撞上限")
            if step != 3 and _AMOUNT_RE.search(text):
                flags.append("疑似违赔偿纪律")
            lang = _lang_ok(text, str(row.get("language") or "zh"))
            print(
                f"    {name:<18}: {text}\n"
                f"    {'':<18}  [{len(text)}字 首字{ms:.0f}ms lang={lang} {'/'.join(flags) or 'ok'}]"
            )

    print(f"\n{'=' * 78}\n汇总（每腿 {len(rows)} 轮）")
    for name, *_ in legs:
        st = stats[name]
        ms = sorted(st["ms"])
        p50 = ms[len(ms) // 2] if ms else 0
        p90 = ms[min(len(ms) - 1, int(len(ms) * 0.9))] if ms else 0
        print(
            f"  {name:<10} 空串={st['empty']}  撞上限={st['trunc']}  "
            f"首字 p50={p50:.0f}ms p90={p90:.0f}ms"
        )
    print("\n判读提醒：分数只抓「明显坏」，答得准不准要读上面的并排输出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# 样本导出（sqlite3 CLI，只读；本脚本自身不含 SQL）：
#   sqlite3 -readonly -json "$DB" "
#     WITH seq AS (SELECT call_id, rowid rid, role, transcript, gen, template_step, language,
#       LEAD(transcript) OVER (PARTITION BY call_id ORDER BY rowid) next_text,
#       LEAD(gen)        OVER (PARTITION BY call_id ORDER BY rowid) next_gen
#       FROM turns WHERE COALESCE(line,'a')='a')
#     SELECT call_id, transcript AS user_text, template_step, language, next_text AS prod_reply
#     FROM seq WHERE role='user' AND next_gen='llm' AND LENGTH(transcript) BETWEEN 8 AND 60
#     ORDER BY rid DESC LIMIT 40;" > /tmp/real_turns.json
