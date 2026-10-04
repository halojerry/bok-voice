"""跑题拉回探针（2026-09-25）：客户中途问流程完全无关的问题，A 线会不会被带飞。

专测「脱离对话主体」轮：算术（一加一等於幾）、天气、闲聊（识唔识唱歌）——
FlowController 判唔出推进，全靠 LLM 自由应答。判读面：
  ① 跑题轮唔可以哑（无应答=FAIL）；
  ② 流程唔锁死——配合轮照常推进（turns template_step 有位移）；
  ③ 拉回质量（informational）：跑题应答里有无流程钩子（答完/安抚完返主线），
     算术轮有无顺带答「二」（理想=简答+带回；纯转移话题=可接受；答错数=缺陷）。
实录逐轮打印供人工判读。

退出码：任一跑题轮哑 / 跑题轮空答 / 流程零位移 → FAIL(1)。

用法：<python> scripts/probe_offtopic_recovery.py [--lang cantonese|zh] [--persona-id X]
前置：`python tools/bok.py serve`（auth-on 栈照 offscript soak 同款 env）。
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
import re
import sys
import time
from pathlib import Path

import httpx
from livekit import rtc

_SCRIPTS = Path(__file__).resolve().parents[1]  # G1c 入桶后 scripts/ 根=parents[1]
sys.path.insert(0, str(_SCRIPTS))

import e2e_real_customer as erc  # noqa: E402  复用骨架:建通/推流/收音/turns/日志窗口
import probe_latency_soak as pls  # noqa: E402  复用:对齐/哨兵/首声等待
import probe_offscript_soak as pos  # noqa: E402  复用:属性归轮/质量旗/轮循环形态

REPORT_DIR = Path(__file__).resolve().parents[2] / "reports" / "offtopic-recovery"

# ---------------------------------------------------------------------------
# 一套 8 轮：配合 → 跑题(算术) → 配合 → 跑题(天气) → 配合 → 跑题(闲聊) → 配合 ×2。
# 句形铁律照守：无逗号、单口气、≥10 字单口气句免疫劈轮。
# 2026-10-02 三语化：旧版题面硬编码粤语字打进 zh/en 人设（en 腿全灭成
# garbled-reask/空答、zh 腿答非所问——探针伪影非栈回归）；题面按 --lang 取。
# ---------------------------------------------------------------------------
ROUNDS_BY_LANG: dict[str, list[dict]] = {
    "cantonese": [
        {"text": "係呀我係陳大文呀", "kind": "coop"},
        {"text": "我想問下你一加一等於幾呀", "kind": "offtopic", "topic": "math"},
        {"text": "你講啦我聽緊呀", "kind": "coop"},
        {"text": "聽日香港會唔會落雨呀", "kind": "offtopic", "topic": "weather"},
        {"text": "好呀你繼續講啦", "kind": "coop"},
        {"text": "你識唔識唱歌㗎你", "kind": "offtopic", "topic": "chat"},
        {"text": "嗯冇問題呀我配合你", "kind": "coop"},
        {"text": "咁你講啦我等你講完", "kind": "coop"},
    ],
    "zh": [
        {"text": "对呀我是陈大文", "kind": "coop"},
        {"text": "我想问一下你一加一等于几呀", "kind": "offtopic", "topic": "math"},
        {"text": "你说吧我听着呢", "kind": "coop"},
        {"text": "明天香港会不会下雨呀", "kind": "offtopic", "topic": "weather"},
        {"text": "好的你继续说吧", "kind": "coop"},
        {"text": "你会不会唱歌呀你", "kind": "offtopic", "topic": "chat"},
        {"text": "嗯没问题我配合你", "kind": "coop"},
        {"text": "那你说吧我等你说完", "kind": "coop"},
    ],
    "en": [
        {"text": "Yes this is John Chan", "kind": "coop"},
        {"text": "By the way what is one plus one", "kind": "offtopic", "topic": "math"},
        {"text": "Go ahead I am listening", "kind": "coop"},
        {"text": "Will it rain in Hong Kong tomorrow", "kind": "offtopic", "topic": "weather"},
        {"text": "Okay please continue", "kind": "coop"},
        {"text": "Can you sing a song", "kind": "offtopic", "topic": "chat"},
        {"text": "Sure no problem I will cooperate", "kind": "coop"},
        {"text": "Go ahead I will wait", "kind": "coop"},
    ],
}


def _rounds_for(lang: str) -> list[dict]:
    return ROUNDS_BY_LANG.get(lang) or ROUNDS_BY_LANG["cantonese"]


# 跑题应答的「流程钩子」词面（informational——有无把话题带回主线）
FLOW_HOOK_WORDS = ("賠", "快遞", "件", "電話", "身份", "公司", "單號", "通知", "處理", "核实", "核实好", "compensat", "parcel", "package", "order", "WhatsApp", "refund")
MATH_ANSWER_WORDS = ("二", "两", "兩", "2", "two", "Two")


def offtopic_flags(attributed: list[dict], rounds: list[dict] | None = None) -> list[dict]:
    """跑题轮判读（纯函数）：哑/空答/流程钩子/算术正答。"""
    rounds = rounds if rounds is not None else _rounds_for("cantonese")
    out = []
    for i, spec in enumerate(rounds):
        if spec["kind"] != "offtopic":
            continue
        replies = [t.strip() for t in attributed[i].get("assistant_texts", []) if t.strip()] if i < len(attributed) else []
        joined = "".join(replies)
        out.append({
            "round": i + 1,
            "topic": spec["topic"],
            "asked": spec["text"],
            "n_replies": len(replies),
            "empty": not replies,
            "flow_hook": any(w in joined for w in FLOW_HOOK_WORDS),
            "math_ok": spec["topic"] == "math" and any(w in joined for w in MATH_ANSWER_WORDS),
            "replies": replies,
        })
    return out


async def run_probe(args: argparse.Namespace) -> int:
    lang = args.lang
    rounds = _rounds_for(lang)
    texts = [r["text"] for r in rounds]
    print(f"[offtopic] 预合成 {len(rounds)} 轮（{lang}）…", flush=True)
    pcms = {i: erc.tts_pcm(r["text"], lang) for i, r in enumerate(rounds)}

    call_id, voice = erc.create_call(lang, args.persona_id, "")
    log_offset = erc.LOG_PATH.stat().st_size if erc.LOG_PATH.exists() else 0
    print(f"[offtopic] call={call_id} persona_voice={voice!r} (log offset {log_offset})", flush=True)

    room = rtc.Room()
    agent_audio = bytearray()
    read_tasks: list[asyncio.Task] = []

    def attach(track) -> None:
        if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO):
            return
        if getattr(track, "name", "") not in ("roomio_audio", "background_audio"):
            return

        async def _read() -> None:
            stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
            try:
                async for event in stream:
                    frame = getattr(event, "frame", event)
                    agent_audio.extend(bytes(frame.data))
            except Exception:
                pass
            finally:
                try:
                    await stream.aclose()
                except Exception:
                    pass

        read_tasks.append(asyncio.get_running_loop().create_task(_read()))

    room.on("track_subscribed", lambda track, _p, _pt: attach(track))
    for participant in room.remote_participants.values():
        for pub in participant.track_publications.values():
            track = getattr(pub, "track", None)
            if track is not None:
                attach(track)

    measures: list[dict] = []
    setup_ok = False
    try:
        data = httpx.post(
            f"{erc.CONTROL_PLANE_URL}/api/token",
            json={"account_id": "acc-001", "call_id": call_id},
            timeout=10,
            headers=erc.CP_HEADERS,
        ).json()
        await room.connect(data["serverUrl"], data["participantToken"])
        audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track("customer-src", audio_source)
        await room.local_participant.publish_track(
            src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        setup_ok = await erc.wait_greeting(agent_audio)
        if not setup_ok:
            print("[offtopic] WARN 开场白 45s×3 未出声，照常推进", flush=True)
        agent_audio.clear()
        await asyncio.sleep(0.5)

        for i, r in enumerate(rounds):
            m = await erc.play_and_listen(audio_source, agent_audio, pcms[i])
            m.update({"text": r["text"], "kind": r["kind"]})
            measures.append(m)
            first = f"{m['first_audio_ms'] / 1000:.2f}s" if m.get("first_audio_ms") is not None else "-"
            flag = "✓" if m.get("answered") else "✗哑"
            tag = r["kind"].upper()
            print(
                f"    轮{i + 1:>2} [{tag:<7}] 「{r['text'][:16]}」 → 首声 {first} · {flag}",
                flush=True,
            )
            await asyncio.sleep(0.8)
    except Exception as exc:
        print(f"[offtopic] 异常中断: {exc!r}", flush=True)
    finally:
        try:
            await room.disconnect()
        except Exception:
            pass
        for t in read_tasks:
            t.cancel()
        try:
            httpx.post(f"{erc.CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", timeout=10, headers=erc.CP_HEADERS)
            httpx.post(f"{erc.CONTROL_PLANE_URL}/api/calls/{call_id}/settle", timeout=30, headers=erc.CP_HEADERS)
        except Exception:
            pass

    turns = await erc.fetch_turns(call_id)
    user_rows = [
        str(t.get("transcript") or "")
        for t in turns
        if t.get("role") == "user" and not (t.get("provider") or "").strip()
    ]
    # 跑题轮短句 + ASR 方言转写乱（「係呀我係陳大文」→「我喺春大永」），文字对齐
    # （pls.assign_user_turns）结构性失败——本探针轮次=推送顺序，行数吻合时直接
    # 按序 zip；不吻合才退文字对齐（届时打印警示）。
    if len(user_rows) == len(rounds):
        turn_counts = [1] * len(rounds)
    else:
        print(f"[offtopic] WARN user 行数 {len(user_rows)} ≠ 轮数 {len(rounds)}，退文字对齐", flush=True)
        turn_counts = pls.assign_user_turns(texts, user_rows)
    attributed = pos.attribute_replies(turns, turn_counts)

    # 流程位移：assistant turns 的 template_step 序列（None 併排除）
    steps = [t.get("template_step") for t in turns if t.get("role") == "assistant" and t.get("template_step") is not None]
    step_span = (max(steps) - min(steps)) if steps else 0

    gen_counts: dict[str, int] = {}
    for t in turns:
        if t.get("role") == "assistant":
            g = (t.get("gen") or "llm").strip() or "llm"
            gen_counts[g] = gen_counts.get(g, 0) + 1

    # 日志窗口哨兵 + PERCEIVED（轻量，复用 soak 正则）
    perceived: list[dict] = []
    counts = {k: 0 for k in pls.COUNT_MARKERS}
    try:
        window = erc.LOG_PATH.read_bytes()[log_offset:]
        for raw in window.splitlines():
            line = raw.decode("utf-8", errors="replace")
            mt = pls.RE_PERCEIVED.search(line)
            if mt:
                perceived.append({
                    "total": int(mt.group(1)), "eou": int(mt.group(2)),
                    "llm": int(mt.group(3)), "tts": int(mt.group(4)),
                })
                continue
            for k in pls.COUNT_MARKERS:
                if k in line:
                    counts[k] += 1
    except Exception:
        pass

    # ---------------- 判读 ----------------
    flags = offtopic_flags(attributed, rounds)
    print("\n" + "=" * 72)
    print(f"对话实录（call={call_id} · 开场白{'✓' if setup_ok else '✗'} · template_step 序列={steps} · 位移={step_span}）")
    print("=" * 72)
    for i, r in enumerate(attributed):
        kind = rounds[i]["kind"]
        mark = " ⟪跑题⟫" if kind == "offtopic" else ""
        for ut in r["user_texts"]:
            print(f"  [客{i + 1}]{mark} {ut[:70]}")
        if not r["assistant_texts"]:
            print(f"        （AI 无应答）")
        for at in r["assistant_texts"]:
            print(f"  [AI{i + 1}] {at[:110]}")

    print("\n跑题轮判读:")
    fail_reasons: list[str] = []
    for f in flags:
        hook = "带回主线✓" if f["flow_hook"] else "无流程钩子"
        math = ""
        if f["topic"] == "math":
            math = " · 算术正答✓" if f["math_ok"] else " · 未答数（转移话题）"
        print(
            f"  轮{f['round']} [{f['topic']}] 问「{f['asked'][:18]}」 → {f['n_replies']} 句 · {hook}{math}"
        )
        for rep in f["replies"]:
            print(f"        ↳ {rep[:100]}")
        if f["empty"]:
            fail_reasons.append(f"轮{f['round']}({f['topic']}) 跑题轮空答")

    mute_rounds = sum(1 for m in measures if not m.get("answered"))
    if mute_rounds:
        fail_reasons.append(f"哑轮 {mute_rounds} 次")
    if not steps or step_span < 2:
        fail_reasons.append(f"流程零/低位移（span={step_span}）——跑题把话术锁死")

    active = {k: v for k, v in counts.items() if v and k in pos.SENTINEL_KEYS}
    if perceived:
        totals = sorted(p["total"] for p in perceived)
        mid = totals[len(totals) // 2]
        print(f"\nPERCEIVED n={len(totals)} p50={mid}ms max={totals[-1]}ms · 生成源={gen_counts}")
    print(f"哨兵：{active if active else '（无）'}")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "call_id": call_id,
        "ts": time.time(),
        "lang": lang,
        "setup_ok": setup_ok,
        "steps": steps,
        "step_span": step_span,
        "gen_counts": gen_counts,
        "flags": flags,
        "measures": measures,
        "perceived": perceived,
        "markers": active,
    }
    out_path = REPORT_DIR / f"{int(time.time())}-offtopic-{lang}.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"\n落盘 {out_path}")

    if fail_reasons:
        print("FAIL: " + "；".join(fail_reasons))
        return 1
    print("PASS: 跑题轮全部有应答且流程未锁死（拉回质量见上方实录判读）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lang", default="cantonese", choices=["cantonese", "zh", "en"])
    ap.add_argument("--persona-id", default=None)
    args = ap.parse_args()
    return asyncio.run(run_probe(args))


if __name__ == "__main__":
    raise SystemExit(main())
