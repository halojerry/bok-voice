"""话术外问题集锦测试台（2026-09-17）：客户不讲「剧本里的话」时 A 线怎么接。

与 probe_latency_soak（延迟+竞争态对抗）不同，本探针专测**话术外应答质量**：
真实客服里客户大量轮次是流程步之外的提问/情绪/质疑/推搪——「你係咪詐騙」
「幫我轉人工」「幾時送到」「我冇時間」——这些轮 FlowController 判不出推进，
全靠 LLM 自由应答。本探针 5 套主题 × 10 轮（默认全粤语 + 1 套普通话），逐轮量
首声延迟、哑轮，并在结束后拉 turns 账本输出**对话实录**（客户原话 vs AI 应答
逐轮对照），供人工判读：答非所问 / 复读 / 直念锁死 / 兜底直念占比。

退出码：总哑轮 ≥2 或任一通开场白未出声 → FAIL(1)。

用法：<python> scripts/probe_offscript_soak.py [--set all|off-identity|...]
      [--budget-first-ms 2500] [--budget-perceived-ms 3000]
前置：`python tools/bok.py serve`（CP 8000 / LiveKit 7880 / ASR 8787 / TTS 8788）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

import httpx
from livekit import rtc

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))

import e2e_real_customer as erc  # noqa: E402  复用骨架:建通/推流/收音/turns/日志窗口
import probe_latency_soak as pls  # noqa: E402  复用:对齐/汇总/哨兵/首声等待

REPORT_DIR = Path(__file__).resolve().parents[1] / "reports" / "offscript-soak"

# ---------------------------------------------------------------------------
# 话术外问题集锦：5 套主题 × 10 轮。句形铁律照守（无逗号、单口气）。
# 主题设计=真实客服高频话术外轮次分类：
#   off-identity  质疑身份/防诈（「你係咪詐騙」族——不敢信、要验证）
#   off-human     转人工+情绪（搵真人/投訴/嬲——流程外诉求与情绪宣泄）
#   off-detail    追问细节（幾時到/賠幾多/倉喺邊——知识与承诺边界）
#   off-deflect   推搪拖延（冇時間/遲啲/唔記得——社交拖延与拒绝）
#   off-mix-zh    混合实战·普通话（含记忆测试:引用 AI 早轮讲过的赔偿数字）
# ---------------------------------------------------------------------------
OFFSCRIPT_SCENARIOS: dict[str, dict] = {
    "off-identity": {
        "label": "質疑身份·防詐（粤语）",
        "lang": "cantonese",
        "persona_voice": "Cantonese_GentleLady",
        "rounds": [
            {"text": "你好邊位呀"},
            {"text": "你係咪詐騙集團嚟㗎"},
            {"text": "點證明你唔係呃人嘅"},
            {"text": "你哋係邊間公司嚟㗎"},
            {"text": "點解你會有我嘅電話號碼"},
            {"text": "我點知你講嘅嘢係真嘅"},
            {"text": "你哋有冇官方網站可以查證"},
            {"text": "唔該你講多次你係邊個"},
            {"text": "我怕上當唔敢亂信人"},
            {"text": "咁你哋客服熱線係幾多號"},
        ],
    },
    "off-human": {
        "label": "轉人工+情緒（粤语）",
        "lang": "cantonese",
        "persona_voice": "Cantonese_GentleLady",
        "rounds": [
            {"text": "喂你係邊位呀"},
            {"text": "我要搵真人嚟傾"},
            {"text": "唔該幫我轉返人工客服"},
            {"text": "你係咪機器人嚟㗎"},
            {"text": "同你講嘢真係好難頂"},
            {"text": "我已經嬲咗好耐喇"},
            {"text": "我要正式投訴你哋服務"},
            {"text": "你哋主管嘅電話係幾多"},
            {"text": "唔該你哋唔好再打嚟喇"},
            {"text": "仲有咩好講呀你話我知"},
        ],
    },
    "off-detail": {
        "label": "追問細節（粤语）",
        "lang": "cantonese",
        "persona_voice": "Cantonese_GentleLady",
        "rounds": [
            {"text": "我個件而家去咗邊度"},
            {"text": "幾時先會送到我度㗎"},
            {"text": "賠償實際賠到幾多錢"},
            {"text": "點解咁耐都仲未到"},
            {"text": "我可唔可以自己去倉攞件"},
            {"text": "你哋個倉喺邊度㗎"},
            {"text": "退款要等幾多日先到"},
            {"text": "你哋查唔查到我個單號"},
            {"text": "我件嘢寄咗幾耐喇依家"},
            {"text": "咁你話而家應該點算好"},
        ],
    },
    "off-deflect": {
        "label": "推搪拖延（粤语）",
        "lang": "cantonese",
        "persona_voice": "Cantonese_GentleLady",
        "rounds": [
            {"text": "我而家冇咩時間呀"},
            {"text": "你遲啲先再講啦"},
            {"text": "我唔記得我有落過單"},
            {"text": "遲啲再打嚟搵我啦"},
            {"text": "等我問埋屋企人先"},
            {"text": "我唔係好明你講乜"},
            {"text": "你係咪打錯電話呀"},
            {"text": "你頭先講嘢太快我聽唔切"},
            {"text": "唔好意思你頭先講咩嚟㗎"},
            {"text": "咁算喇唔使搞喇"},
        ],
    },
    "off-mix-zh": {
        "label": "混合實戰·普通話（含记忆测试）",
        "lang": "zh",
        "persona_voice": "Chinese_crisp_podcaster_nv1",
        "rounds": [
            {"text": "你好请问哪位"},
            {"text": "没有啊我没收到件"},
            {"text": "我不太明白你刚说什么"},
            {"text": "你等我一下我找找看"},
            {"text": "找不到啊这怎么办"},
            {"text": "那你帮我查一下吧"},
            {"text": "你刚才说赔偿多少来着"},
            {"text": "行那你尽快帮我处理"},
            {"text": "你加我微信别打错号"},
            {"text": "那就麻烦你了谢谢"},
        ],
    },
}

RE_WHOLE = re.compile(r"[\s。，,．.！!？?～~、；;：:'\"()（）]")

SENTINEL_KEYS = (
    "LLM_FALLBACK_TEXT",
    "LLM_FIRST_TOKEN_TIMEOUT",
    "LLM_LATE_ANSWER",
    "[storm] engage",
    "[watchdog]",
    "[digit-accum] stash",
    "[digit-accum] flush captured",
    "starve-ack",
    "QA_FASTPATH hit=1",
    "BOK_FILLER fired",
    "[heartbeat] silent",
    "[whatsapp] captured",
    "TAIL_ANCHOR_MIMIC_SUPPRESSED",
)


# ---------------------------------------------------------------------------
# 报告纯函数
# ---------------------------------------------------------------------------
def attribute_replies(turns: list[dict], turn_counts: list[int]) -> list[dict]:
    """按时间序把 assistant 应答归到各预期轮（近似：用户真行吃满本轮配额进下轮）。

    机制行（provider 标签的 storm-listen/starve-ack 等 user 行）不占客户口、
    不推进游标；assistant 全收（脚本直念/LLM/兜底都係 AI 嘴）。turn_counts=0
    （用户行未对齐）的轮唔推进游标——后续应答堆该轮，实录按时间序另列兜底。
    推进係惰性：配额吃满后唔急切切轮（本轮 AI 应答喺时间线上落喺用户行后），
    下一条真用户行到来时先切——保证「用户行+佢嘅应答」同轮。纯函数，
    tests/test_offscript_report.py 直测。
    """
    rounds: list[dict] = [{"user_texts": [], "assistant_texts": []} for _ in turn_counts]
    cur = 0
    eaten = 0
    for t in turns:
        text = str(t.get("transcript") or "").strip()
        if not text:
            continue
        role = t.get("role")
        if role == "user":
            if (t.get("provider") or "").strip():
                continue
            quota = turn_counts[cur] if cur < len(turn_counts) else 0
            if quota and eaten >= quota and cur < len(rounds) - 1:
                cur += 1
                eaten = 0
            if cur < len(rounds):
                rounds[cur]["user_texts"].append(text)
            eaten += 1
        elif role == "assistant":
            if cur < len(rounds):
                rounds[cur]["assistant_texts"].append(text)
    return rounds


def reply_quality_flags(rounds: list[dict]) -> dict[str, int]:
    """逐轮应答质量旗（纯函数）：空答/全复读/直念模板感由哨兵与实录判读，
    这里只做机器可判的：empty_reply、repeat_reply（与上一轮任一应答整句相同）。"""
    flags = {"empty_reply": 0, "repeat_reply": 0}
    seen_all: list[str] = []
    for r in rounds:
        texts = [t.strip() for t in r.get("assistant_texts", []) if t.strip()]
        if not texts:
            flags["empty_reply"] += 1
            continue
        norm = [RE_WHOLE.sub("", t).lower() for t in texts]
        if any(n and n in seen_all for n in norm):
            flags["repeat_reply"] += 1
        seen_all.extend(norm)
    return flags


def summarize_all(results: list[dict], budgets: dict[str, float]) -> dict:
    """跨通聚合（纯函数）。"""
    first: list[float] = []
    totals: list[float] = []
    markers: dict[str, int] = {}
    gens: dict[str, int] = {}
    for r in results:
        first.extend(m["first_audio_ms"] for m in r["measures"] if m.get("first_audio_ms") is not None)
        totals.extend(p["total"] for p in r["perceived"])
        for k, v in r["summary"]["markers"].items():
            if v:
                markers[k] = markers.get(k, 0) + v
        for g, n in r.get("gen_counts", {}).items():
            gens[g] = gens.get(g, 0) + n
    over = sum(1 for v in first if v > budgets["first_ms"])
    return {
        "calls": len(results),
        "rounds": sum(r["summary"]["rounds"] for r in results),
        "answered": sum(r["summary"]["answered"] for r in results),
        "mute": sum(r["summary"]["mute"] for r in results),
        "first_audio": {
            "n": len(first),
            "p50": pls.percentile(first, 50),
            "p95": pls.percentile(first, 95),
            "max": max(first) if first else None,
            "over_budget": over,
        },
        "perceived": {
            "n": len(totals),
            "p50": pls.percentile(totals, 50),
            "p95": pls.percentile(totals, 95),
            "max": max(totals) if totals else None,
        },
        "markers": markers,
        "gen_counts": gens,
    }


# ---------------------------------------------------------------------------
# 跑一通
# ---------------------------------------------------------------------------
async def run_set(key: str, persona_id: str | None, budgets: dict[str, float]) -> dict:
    sc = OFFSCRIPT_SCENARIOS[key]
    lang = sc["lang"]
    rounds = sc["rounds"]
    texts = [r["text"] for r in rounds]
    print(f"\n[offscript] 场景 {key} · {sc['label']} —— 预合成 {len(rounds)} 轮…", flush=True)
    pcms = {i: erc.tts_pcm(r["text"], lang) for i, r in enumerate(rounds)}

    call_id, voice = erc.create_call(lang, persona_id, sc.get("persona_voice", ""))
    log_offset = erc.LOG_PATH.stat().st_size if erc.LOG_PATH.exists() else 0
    print(f"[offscript] call={call_id} persona_voice={voice!r} (log offset {log_offset})", flush=True)

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
            headers=erc._CP_HEADERS,
        ).json()
        await room.connect(data["serverUrl"], data["participantToken"])
        audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track("customer-src", audio_source)
        await room.local_participant.publish_track(
            src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        setup_ok = await erc.wait_greeting(agent_audio)
        if not setup_ok:
            print("[offscript] WARN 开场白 45s×3 未出声，照常推进", flush=True)
        agent_audio.clear()
        await asyncio.sleep(0.5)

        for i, r in enumerate(rounds):
            m = await erc.play_and_listen(audio_source, agent_audio, pcms[i])
            m.update({"text": r["text"]})
            measures.append(m)
            first = f"{m['first_audio_ms'] / 1000:.2f}s" if m.get("first_audio_ms") is not None else "-"
            flag = "✓" if m.get("answered") else "✗哑"
            print(
                f"    轮{i + 1:>2} 「{r['text'][:16]}」 → 首声 {first} · 语音 {m.get('speech_s', 0):.1f}s · {flag}",
                flush=True,
            )
            await asyncio.sleep(0.8)
    except Exception as exc:
        print(f"[offscript] 场景 {key} 异常中断: {exc!r}", flush=True)
    finally:
        try:
            await room.disconnect()
        except Exception:
            pass
        for t in read_tasks:
            t.cancel()
        try:
            httpx.post(f"{erc.CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", timeout=10)
            httpx.post(f"{erc.CONTROL_PLANE_URL}/api/calls/{call_id}/settle", timeout=30)
        except Exception:
            pass

    turns = await erc.fetch_turns(call_id)
    user_rows = [
        str(t.get("transcript") or "")
        for t in turns
        if t.get("role") == "user" and not (t.get("provider") or "").strip()
    ]
    turn_counts = pls.assign_user_turns(texts, user_rows)
    for m, c in zip(measures, turn_counts):
        m["user_turns"] = c
        m["split"] = c > 1

    gen_counts: dict[str, int] = {}
    for t in turns:
        if t.get("role") == "assistant":
            g = (t.get("gen") or "llm").strip() or "llm"
            gen_counts[g] = gen_counts.get(g, 0) + 1
    attributed = attribute_replies(turns, turn_counts)

    # 日志窗口：PERCEIVED 三段 + 哨兵计数（复用 soak 的正则/哨兵集）。
    perceived: list[dict] = []
    counts = {k: 0 for k in pls.COUNT_MARKERS}
    budget_hits: list[int] = []
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
            mb = pls.RE_BUDGET.search(line)
            if mb:
                budget_hits.append(int(mb.group(1)))
                continue
            for k in pls.COUNT_MARKERS:
                if k in line:
                    counts[k] += 1
    except Exception:
        pass

    summary = pls.summarize_report(measures, perceived, counts, budgets)
    summary["markers"] = {k: v for k, v in counts.items() if k in SENTINEL_KEYS or v}
    quality = reply_quality_flags(attributed)
    result = {
        "key": key,
        "label": sc["label"],
        "lang": lang,
        "call_id": call_id,
        "setup_ok": setup_ok,
        "measures": measures,
        "perceived": perceived,
        "budget_hits": budget_hits,
        "turn_counts": turn_counts,
        "turns_user": user_rows,
        "attributed": attributed,
        "quality": quality,
        "gen_counts": gen_counts,
        "summary": summary,
        "ts": int(time.time()),
    }
    print_report(result, budgets)
    return result


def print_report(res: dict, budgets: dict[str, float]) -> None:
    s = res["summary"]
    print("\n" + "═" * 72, flush=True)
    print(f"场景 {res['key']} · {res['label']}   call={res['call_id']}", flush=True)
    print("═" * 72, flush=True)
    print(f"{'轮':>3} {'首声':>7} {'客户行':>5} {'哑':>3}  文本", flush=True)
    for i, m in enumerate(res["measures"], start=1):
        first = f"{m['first_audio_ms'] / 1000:.2f}s" if m.get("first_audio_ms") is not None else "无"
        uc = int(m.get("user_turns") or 0)
        flags = []
        if m.get("split"):
            flags.append("拆轮!")
        if not m.get("answered"):
            flags.append("哑")
        if m.get("first_audio_ms") is not None and m["first_audio_ms"] > budgets["first_ms"]:
            flags.append("超预算")
        print(
            f"{i:>3} {first:>7} {uc if uc else '?':>5} "
            f"{'哑' if not m.get('answered') else '':>3}  {m['text'][:20]}"
            + ("  [" + ",".join(flags) + "]" if flags else ""),
            flush=True,
        )
    fa, pd = s["first_audio"], s["perceived"]
    if fa["n"] and fa["p50"] is not None:
        print(
            f"\n墙钟首声 n={fa['n']} p50={fa['p50']:.0f}ms p95={fa['p95']:.0f}ms max={fa['max']:.0f}ms "
            f"超标(>{budgets['first_ms']:.0f})={fa['over_budget']}",
            flush=True,
        )
    else:
        # 组中断/零量测：报表不炸（一组失败不该毁掉整轮其余组的输出）。
        print("\n墙钟首声：无有效量测（该组中断或全哑）", flush=True)
    if pd["n"]:
        print(
            f"PERCEIVED n={pd['n']} p50={pd['p50']:.0f}ms p95={pd['p95']:.0f}ms max={pd['max']:.0f}ms "
            f"(预算哨兵={len(res['budget_hits'])})",
            flush=True,
        )
    q = res["quality"]
    print(f"质量旗：空答轮={q['empty_reply']} 整句复读轮={q['repeat_reply']} · 生成源={res['gen_counts']}")
    active = {k: v for k, v in s["markers"].items() if v}
    print(f"哨兵计数：{active if active else '（无）'}", flush=True)
    print("\n对话实录（逐轮对照）:", flush=True)
    for i, r in enumerate(res["attributed"], start=1):
        for ut in r["user_texts"]:
            print(f"  [客{i}] {ut[:60]}", flush=True)
        for at in r["assistant_texts"]:
            print(f"  [AI{i}] {at[:80]}", flush=True)
        if not r["user_texts"] and not r["assistant_texts"]:
            print(f"  [轮{i}] （无账本行）", flush=True)
    print(f"\n小计[{res['key']}]: {s['rounds']} 轮 · 有答 {s['answered']} · 哑 {s['mute']}", flush=True)


async def main() -> int:
    parser = argparse.ArgumentParser(description="话术外问题集锦测试台")
    parser.add_argument("--set", choices=[*OFFSCRIPT_SCENARIOS, "all"], default="all")
    parser.add_argument("--persona-id", default=None)
    parser.add_argument("--budget-first-ms", type=float, default=2500.0)
    parser.add_argument("--budget-perceived-ms", type=float, default=3000.0)
    args = parser.parse_args()
    budgets = {"first_ms": args.budget_first_ms, "perceived_ms": args.budget_perceived_ms}
    keys = list(OFFSCRIPT_SCENARIOS) if args.set == "all" else [args.set]

    results = []
    for key in keys:
        results.append(await run_set(key, args.persona_id, budgets))

    agg = summarize_all(results, budgets)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"{int(time.time())}-{'-'.join(keys)}.json"
    out.write_text(json.dumps({"aggregate": agg, "sets": results}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print("\n" + "█" * 72, flush=True)
    print(f"汇总：{agg['calls']} 通 · {agg['rounds']} 轮 · 有答 {agg['answered']} · 哑 {agg['mute']}", flush=True)
    fa = agg["first_audio"]
    if fa["n"]:
        print(
            f"首声 n={fa['n']} p50={fa['p50']:.0f}ms p95={fa['p95']:.0f}ms max={fa['max']:.0f}ms 超标={fa['over_budget']}",
            flush=True,
        )
    pd = agg["perceived"]
    if pd["n"]:
        print(f"PERCEIVED n={pd['n']} p50={pd['p50']:.0f}ms p95={pd['p95']:.0f}ms max={pd['max']:.0f}ms", flush=True)
    print(f"生成源合计={agg['gen_counts']}", flush=True)
    print(f"哨兵合计={agg['markers'] if agg['markers'] else '（无）'}", flush=True)
    print(f"[offscript] JSON 报告 → {out}", flush=True)
    fail = agg["mute"] >= erc.FAIL_MUTE_ROUNDS or any(not r["setup_ok"] for r in results)
    print(f"OFFSCRIPT_SOAK 通数={agg['calls']} 总轮={agg['rounds']} 哑轮={agg['mute']} → {'FAIL' if fail else 'PASS'}",
          flush=True)
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
