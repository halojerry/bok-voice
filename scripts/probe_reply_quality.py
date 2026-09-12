"""回复质量离线回放探针（2026-09-12 P0「会说话」验收门）。

call-8fa17d2b 实证三症状：QA miss 落 LLM 后 ①「你说什么东西？」换来 11.9s
整段通知重念 ②连续两轮回复一字不差 ③「我先查一下」被照本重问。本探针用该
通真实轮次回放「新尾部形态」（渐进披露：首轮底稿/后续命中单分支），打本地
mlx LLM（:1235），断言：

  R1 长度 ≤80 字（治整段重念）
  R2 与当前步正稿首行归一化相似 <0.7（治照念话术）
  R3 与上一句真实回复归一化相似 <0.9（治自我复读）

用法（栈在跑、无通话时——GPU 竞态规矩）：
  .venv312/bin/python scripts/probe_reply_quality.py [--call call-8fa17d2b]
结果落 scripts/.probe_reply_quality.json；退出码非 0 = 有轮未过门。
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sqlite3
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.flow import FlowController  # noqa: E402
from agent_runtime.providers.livekit_plugins import ContextState  # noqa: E402

APP_DB = Path(os.environ.get("BOK_APP_DB", Path.home() / "Library/Application Support/BokVoice/bok_voice.db"))
LLM_URL = os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1")
LLM_MODEL = os.environ.get(
    "MLX_LLM_MODEL", "/Users/halo/.lmstudio/models/avan-ag/Qwen3.5-4B-Uncensored-MLX-4bit"
)

# 回放用例：来自 call-8fa17d2b 的真实 QA-miss 轮。(flow 步 0-based, verdict,
# 客户原话, 该轮前 AI 上一句回复, 期望回复应涉及的语义关键词集——任一命中)。
CASES = [
    {
        "name": "什么东西-不照念通知",
        "flow_step": 1,  # step2 通知(已直念)
        "verdict": "question",
        "user": "什么东西？你说什么东西？",
        "last_reply": "我们是集运中转仓，这次致电是想通知您，您有一件货件在中转仓打包期间遗失了。",
        "script_line": "我们是集运中转仓，这次致电是想通知您，您有一件货件在中转仓打包期间遗失了。",
        "expect_any": ["货", "丢", "遗失", "赔"],
    },
    {
        "name": "我先查一下-不照本重问",
        "flow_step": 2,  # step3 引导核实
        "verdict": "question",
        "user": "那我查一下。下先。",
        "last_reply": "好的，普哥。 那您是在拼多多、淘宝还是京东买的？",
        "script_line": "那您是在拼多多、淘宝还是京东买的？",
        "expect_any": ["好", "您", "查"],
    },
    {
        "name": "应该没有买吧-不复读上一句",
        "flow_step": 2,
        "verdict": "unclear",
        "user": "得了，我应该没有。购买吧。",
        "last_reply": "我们是集运中转仓，给您致电通知：您有一件货件，在打包期间遗失了。",
        "script_line": "那您是在拼多多、淘宝还是京东买的？",
        "expect_any": ["没", "关系", "帮", "核"],
    },
]


def _norm(t: str) -> str:
    import re

    return re.sub(r"[^\w\u4e00-\u9fff]+", "", t or "")


def _sim(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(a=na, b=nb).ratio()


def _load_flow(call_id: str) -> tuple[FlowController, dict, dict]:
    db = sqlite3.connect(f"file:{APP_DB}?mode=ro", uri=True)
    call = db.execute(
        "SELECT object_id, template_id FROM call_sessions WHERE id=?", (call_id,)
    ).fetchone()
    if not call:
        raise SystemExit(f"call {call_id} not found")
    obj_row = db.execute(
        "SELECT display_name, phone, tracking_no, courier, language FROM object_profiles WHERE id=? ",
        (call[0],),
    ).fetchone()
    tpl_row = db.execute(
        "SELECT steps_json FROM conversation_templates WHERE id=?", (call[1],)
    ).fetchone()
    card = {
        "display_name": obj_row[0], "phone": obj_row[1], "tracking_no": obj_row[2],
        "courier": obj_row[3], "language": obj_row[4],
    }
    tpl = {"steps_json": tpl_row[0]}
    db.close()
    return FlowController.from_template(tpl, card), tpl, card


def _build_messages(fc: FlowController, case: dict) -> list[dict]:
    ctx = ContextState(account_id="probe")
    ctx.set_user_language("zh")
    fc.current = case["flow_step"]
    fc.opening_played = True
    fc.said_steps.add(1)  # step2 通知已直念(回放语境)
    fc.last_verdict = case["verdict"]
    fc.last_user_text = case["user"]
    ctx.set_flow(fc.flow_overview(), fc.current_step_text())
    ctx.set_last_reply(case["last_reply"])
    ctx.set_object_brief("客户：普哥，顺丰集运件，尾号六七八九。")

    system = ctx.render_instruction_prefix() + "\n\n你是话术客服小普，语气亲切。"
    tail = ctx.render_context_tail()
    msgs = [{"role": "system", "content": system}]
    msgs.append({"role": "assistant", "content": "您好，请问是普哥吗？"})
    msgs.append({"role": "user", "content": "啊。"})
    msgs.append({"role": "assistant", "content": case["last_reply"]})
    msgs.append({"role": "user", "content": case["user"] + "\n\n" + tail})
    return msgs


def _chat(msgs: list[dict]) -> str:
    r = httpx.post(
        f"{LLM_URL}/chat/completions",
        json={"model": LLM_MODEL, "messages": msgs, "max_tokens": 200, "temperature": 0.7},
        timeout=60,
    )
    r.raise_for_status()
    raw = str(r.json()["choices"][0]["message"]["content"] or "")
    # mlx 非流式补全会带过聊天模板残迹(<|im_end|> 后续写)——按模板标记截断。
    for stop in ("<|im_end|>", "<|im_start|>"):
        raw = raw.split(stop)[0]
    return raw.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--call", default="call-8fa17d2b")
    ap.add_argument("--json-out", default=str(ROOT / "scripts" / ".probe_reply_quality.json"))
    args = ap.parse_args()

    fc, _, _ = _load_flow(args.call)
    results, failed = [], 0
    for case in CASES:
        reply = _chat(_build_messages(fc, case))
        n_chars = len(_norm(reply))
        r1 = n_chars <= 80
        r2 = _sim(reply, case["script_line"]) < 0.7
        r3 = _sim(reply, case["last_reply"]) < 0.9
        r4 = any(k in reply for k in case["expect_any"])
        ok = r1 and r2 and r3 and r4
        failed += 0 if ok else 1
        results.append(
            {
                "name": case["name"], "ok": ok, "reply": reply, "chars": n_chars,
                "len_ok": r1, "not_script": r2,
                "script_sim": round(_sim(reply, case["script_line"]), 3),
                "not_repeat": r3, "repeat_sim": round(_sim(reply, case["last_reply"]), 3),
                "on_topic": r4,
            }
        )
        print(
            f"[{'PASS' if ok else 'FAIL'}] {case['name']}: len={n_chars} "
            f"script_sim={_sim(reply, case['script_line']):.2f} "
            f"repeat_sim={_sim(reply, case['last_reply']):.2f} on_topic={r4}\n      reply={reply[:120]!r}"
        )
    Path(args.json_out).write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"{'ALL PASS' if not failed else f'{failed} CASE(S) FAILED'} → {args.json_out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
