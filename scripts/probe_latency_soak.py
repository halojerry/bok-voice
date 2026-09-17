"""多轮多样话术延迟测试台（2026-09-17）：真实客户多轮×每轮不同措辞，量「讲到出声」。

与 e2e_real_customer（单场景对话形状验收）不同，本探针专测**延迟与竞争态**：
  - 每轮不同措辞（同意图换说法），三语场景各 7 轮正常 + 4 轮对抗（拆句/快语速/
    连环打断/报号拆两截）——对抗轮专打竞争态防线（join-hold/累积/风暴退避/账本）；
  - 逐轮三口径对照：墙钟「推完→首声」（RMS onset）｜agent.log 按 call_id 窗口抓
    PERCEIVED_MS 三段（eou/llm/tts）｜turns 账本 perceived_ms/latency_ms/gen；
  - 逐轮异常旗：拆轮（一句被收成 N>1 个 user 轮）、哑轮、canceled 回复、风暴退避
    触发、LLM 兜底直念、PERCEIVED 预算超标；
  - 汇总：逐指标 p50/p95/max + 异常计数 + 逐轮明细表 + JSON 落盘。

退出码：正常轮哑 ≥2 或全部轮无回复 → FAIL(1)；对抗轮异常只记旗不强 FAIL
（拆轮被防线救起 = split=0 也是合法结果，看报告判读）。

用法：<python> scripts/probe_latency_soak.py [--scenario soak-canto|soak-zh|soak-en|all]
      [--no-adversarial] [--budget-first-ms 2500] [--budget-perceived-ms 3000]
前置：`python tools/bok.py serve`（CP 8000 / LiveKit 7880 / ASR 8787 / TTS 8788）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
from pathlib import Path

import httpx
from livekit import rtc

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))

import e2e_real_customer as erc  # noqa: E402  复用骨架:建通/推流/收音/turns/日志窗口

REPORT_DIR = Path(__file__).resolve().parents[1] / "reports" / "latency-soak"

# ---------------------------------------------------------------------------
# 话术库：每轮不同措辞（同意图换说法），op=normal|split|fast|interrupt|digits2
# 句形铁律照守：正常句 ≤12 字、无逗号；split/digits2 由探针主动劈半推流。
# ---------------------------------------------------------------------------
SOAK_SCENARIOS: dict[str, dict] = {
    "soak-canto": {
        "label": "小九（粤语·延迟 soak+对抗）",
        "lang": "cantonese",
        "persona_voice": "Cantonese_GentleLady",
        "rounds": [
            {"text": "你好", "op": "normal"},
            {"text": "我個件遲咗成個禮拜", "op": "normal"},
            {"text": "拼多多買嘅", "op": "normal"},
            {"text": "可以點樣賠", "op": "normal"},
            {"text": "幾時處理得好", "op": "normal"},
            {"text": "接受呀", "op": "normal"},
            {"text": "加你WhatsApp得唔得", "op": "normal"},
            {"text": "我個件上個禮拜寄出", "op": "split"},
            {"text": "唔該幫我跟進吓個件", "op": "fast"},
            {"text": "你知唔知我個件去咗邊", "op": "interrupt",
             "interject": ["喂", "你聽我講", "唔係咁講"]},
            {"text": "單號係八六五三二七四零", "op": "digits2"},
        ],
    },
    "soak-zh": {
        "label": "小普（普通话·延迟 soak+对抗）",
        "lang": "zh",
        "persona_voice": "Chinese_crisp_podcaster_nv1",
        "rounds": [
            {"text": "你好", "op": "normal"},
            {"text": "我的快递拖了一个星期", "op": "normal"},
            {"text": "拼多多买的", "op": "normal"},
            {"text": "怎么赔偿", "op": "normal"},
            {"text": "什么时候能处理好", "op": "normal"},
            {"text": "好的可以", "op": "normal"},
            {"text": "加你们微信行吗", "op": "normal"},
            {"text": "我的件上星期就寄出了", "op": "split"},
            {"text": "麻烦帮我跟进一下件", "op": "fast"},
            {"text": "你知道我的件到哪了吗", "op": "interrupt",
             "interject": ["喂", "你听我说", "不是这样"]},
            {"text": "单号是八六五三二七四零", "op": "digits2"},
        ],
    },
    "soak-en": {
        "label": "Elen（英语·延迟 soak+对抗）",
        "lang": "en",
        "persona_voice": "socialmedia_female_2_v1",
        "rounds": [
            {"text": "hi there", "op": "normal"},
            {"text": "my parcel is a week late", "op": "normal"},
            {"text": "bought it on Pinduoduo", "op": "normal"},
            {"text": "how do I get compensated", "op": "normal"},
            {"text": "when will it be resolved", "op": "normal"},
            {"text": "okay fine", "op": "normal"},
            {"text": "can I add your whatsapp", "op": "normal"},
            {"text": "my parcel was shipped last week", "op": "split"},
            {"text": "please follow up on it", "op": "fast"},
            {"text": "do you know where my parcel is", "op": "interrupt",
             "interject": ["hey", "listen to me", "that is not right"]},
            {"text": "the tracking number is eight six five three two seven four zero", "op": "digits2"},
        ],
    },
}

# 打断轮首声等待上限:超过即照推插话(无回复=更要打,风暴退避的刺激形态)。
INTERRUPT_FIRST_WAIT_S = 5.0

# agent.log 哨兵（本通窗口内计数；哨兵语义见 docs/LATENCY_BUDGETS.md §5）
RE_PERCEIVED = re.compile(r"PERCEIVED_MS total=(\d+) \(eou=(\d+) llm=(\d+) tts=(\d+)\)")
RE_BUDGET = re.compile(r"PERCEIVED_BUDGET_EXCEEDED total=(\d+)")
COUNT_MARKERS = (
    "MINIMAX_TTS_BIDI_STALL",
    "QWEN3_ASR_REDECODE_DROP",
    "QWEN3_ECHO_SELF_HEARD_DROP",
    "QWEN3_HOTWORD_ECHO_DROP",
    "LLM_FALLBACK_TEXT",
    "LLM_FIRST_TOKEN_TIMEOUT",
    "LLM_LATE_ANSWER",
    "[storm] engage",
    "[storm] listening",
    "[watchdog]",
    "[digit-accum] stash",
    "[digit-accum] flush captured",
    "interrupted reply ledgered",
    "starve-ack",
    "QA_FASTPATH hit=1",
    "BOK_FILLER fired",
    "[heartbeat] silent",
    "[whatsapp] captured",
)


# ---------------------------------------------------------------------------
# 报告纯函数（tests/test_latency_soak_report.py 直测）
# ---------------------------------------------------------------------------
def _norm_text(s: str) -> str:
    return re.sub(r"[\s。，,．.！!？?～~、；;：:'\"()（）]", "", str(s or "")).lower()


def percentile(values: list[float], p: float) -> float | None:
    """最近邻百分位（p∈[0,100]）；空表回 None。"""
    if not values:
        return None
    xs = sorted(values)
    idx = max(0, min(len(xs) - 1, math.ceil(p / 100 * len(xs)) - 1))
    return xs[idx]


def assign_user_turns(expected_texts: list[str], user_rows: list[str],
                      multi: list[bool] | None = None) -> list[int]:
    """把账本 user 轮按顺序分配给预期轮（启发式，纯函数）。

    每轮从游标起**锚行搜索**：首个「像本轮」的行（首 2 字相等，或任一方前缀）
    即锚定——ASR 改写/回声隐藏（轮根本没落行）都不推进游标，count=0 报
    「未对齐」而唔会串位。锚定后仅 `multi[i]`（拆段刺激轮 split/digits2）允许
    续吃 ≤3 行凑整句，续吃行撞未来轮开头即停。非拆段轮 count>1 只可能来自
    锚行前无法归属的行——唔会（锚行搜索跳过的行唔分配）。
    返回每轮 user_turn_count（0=该轮话被吞/未能对齐）。
    """
    counts: list[int] = []
    cur = 0
    future_heads = [[_norm_text(t)[:2] for t in expected_texts[i + 1:]]
                    for i in range(len(expected_texts))]
    for i, text in enumerate(expected_texts):
        want = _norm_text(text)
        head = want[:2]
        j = cur
        while j < len(user_rows):
            got = _norm_text(user_rows[j])
            if head and got[:2] == head:
                break
            if head and got and (want.startswith(got[:2]) or got.startswith(head)):
                break
            j += 1
        if j >= len(user_rows):
            counts.append(0)  # 本轮被吞（回声隐藏等）/ASR 面目全非:未对齐,唔耗游标
            continue
        count = 1
        j += 1
        if multi and multi[i]:
            while j < len(user_rows) and count < 4:
                got = _norm_text(user_rows[j])
                if got[:2] and got[:2] in future_heads[i]:
                    break  # 呢行像未来某轮开头 → 唔吃
                j += 1
                count += 1
        cur = j
        counts.append(count)
    return counts


def summarize_report(measures: list[dict], perceived: list[dict], counts: dict[str, int],
                     budgets: dict[str, float]) -> dict:
    """聚合：逐指标 p50/p95/max + 超标数 + 异常旗汇总（纯函数）。"""
    first = [m["first_audio_ms"] for m in measures if m.get("first_audio_ms") is not None]
    totals = [p["total"] for p in perceived]
    over_first = [v for v in first if v > budgets["first_ms"]]
    over_perceived = [v for v in totals if v > budgets["perceived_ms"]]
    mute = sum(1 for m in measures if not m.get("answered"))
    return {
        "rounds": len(measures),
        "answered": len(measures) - mute,
        "mute": mute,
        "first_audio": {
            "n": len(first),
            "p50": percentile(first, 50),
            "p95": percentile(first, 95),
            "max": max(first) if first else None,
            "over_budget": len(over_first),
        },
        "perceived": {
            "n": len(totals),
            "p50": percentile(totals, 50),
            "p95": percentile(totals, 95),
            "max": max(totals) if totals else None,
            "over_budget": len(over_perceived),
        },
        "markers": counts,
    }


# ---------------------------------------------------------------------------
# 推流驱动
# ---------------------------------------------------------------------------
def split_pcm(pcm: bytes, gap_s: float = 0.6) -> list[bytes]:
    """一句劈两半，中间垫 gap_s 真静音（>VAD min_silence 0.45 → 结构性劈轮刺激）。"""
    half = len(pcm) // 2
    half -= half % 640  # 对齐 20ms 帧
    silence = b"\x00" * (int(16000 * gap_s) * 2)
    return [pcm[:half], silence, pcm[half:]]


async def push_all(audio_source: rtc.AudioSource, chunks: list[bytes]) -> None:
    for c in chunks:
        await erc.push_pcm(audio_source, c)


async def wait_first_audio(agent_audio: bytearray, mark: int, timeout_s: float) -> float | None:
    """推完后等 AI 首声（RMS onset，≥0.12s 累计），返回等待 ms；超时 None。"""
    t0 = time.perf_counter()
    probe = mark
    speech = 0.0
    deadline = t0 + timeout_s
    while time.perf_counter() < deadline:
        s, _sil, probe = erc.speech_stats(bytes(agent_audio), probe)
        speech += s
        if speech >= 0.12:
            return (time.perf_counter() - t0) * 1000
        await asyncio.sleep(0.1)
    return None


async def run_interrupt_round(audio_source: rtc.AudioSource, agent_audio: bytearray,
                              trigger_pcm: bytes, interject_pcms: list[bytes]) -> dict:
    """连环打断轮：触发问句 → 等回复出声（上限 5s——久等唔係风暴刺激,首跑
    30s 干等令三连打变隔 30s 散打,风暴退避结构性无法触发）→ 连打 3 次 → 等收场。"""
    out: dict = {}
    mark = len(agent_audio)
    await erc.push_pcm(audio_source, trigger_pcm)
    t_done = time.perf_counter()
    out["first_audio_ms"] = await wait_first_audio(agent_audio, mark, INTERRUPT_FIRST_WAIT_S)
    if out["first_audio_ms"] is not None:
        out["first_audio_ms"] = (time.perf_counter() - t_done) * 1000
    # 三连打：每次等 1.2s（回复已在播/在生成中）再插下一句。
    for pcm in interject_pcms:
        await asyncio.sleep(1.2)
        await erc.push_pcm(audio_source, pcm)
    # 等收场：出现过语音后再静默 ANSWER_SILENCE_S。
    probe = mark
    speech = 0.0
    silent = 0.0
    processed = probe
    deadline = time.perf_counter() + erc.ANSWER_TIMEOUT_S
    while time.perf_counter() < deadline:
        s, sil, processed = erc.speech_stats(bytes(agent_audio), processed)
        speech += s
        if s > 0:
            silent = 0.0
        else:
            silent += sil
        if speech >= 0.12 and silent >= erc.ANSWER_SILENCE_S:
            break
        await asyncio.sleep(0.1)
    out["speech_s"] = speech
    out["answered"] = speech >= erc.MUTE_SPEECH_S
    return out


async def run_scenario(key: str, persona_id: str | None, *, adversarial: bool,
                       budgets: dict[str, float]) -> dict:
    sc = SOAK_SCENARIOS[key]
    lang = sc["lang"]
    rounds = [r for r in sc["rounds"] if adversarial or r["op"] == "normal"]
    texts = [r["text"] for r in rounds]
    print(f"\n[latency-soak] 场景 {key} · {sc['label']} —— 预合成 {len(rounds)} 轮…", flush=True)

    def _pcm(text: str) -> bytes:
        return erc.tts_pcm(text, lang)

    pcms: dict[int, bytes] = {}
    interject_pcms: dict[int, list[bytes]] = {}
    for i, r in enumerate(rounds):
        pcm = _pcm(r["text"])
        if r["op"] == "fast":
            from probe_fast_speech import speedup_pcm

            pcm = speedup_pcm(pcm, 1.4)
        pcms[i] = pcm
        if r["op"] == "interrupt":
            interject_pcms[i] = [_pcm(t) for t in r.get("interject", [])]

    call_id, voice = erc.create_call(lang, persona_id, sc.get("persona_voice", ""))
    log_offset = erc.LOG_PATH.stat().st_size if erc.LOG_PATH.exists() else 0
    print(f"[latency-soak] call={call_id} persona_voice={voice!r} (log offset {log_offset})", flush=True)

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
        ).json()
        await room.connect(data["serverUrl"], data["participantToken"])
        audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track("customer-src", audio_source)
        await room.local_participant.publish_track(
            src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        setup_ok = await erc.wait_greeting(agent_audio)
        if not setup_ok:
            print("[latency-soak] WARN 开场白 45s×3 未出声，照常推进", flush=True)
        agent_audio.clear()
        await asyncio.sleep(0.5)

        for i, r in enumerate(rounds):
            op = r["op"]
            if op == "interrupt":
                m = await run_interrupt_round(audio_source, agent_audio, pcms[i], interject_pcms.get(i, []))
            else:
                pcm = pcms[i]
                chunks = split_pcm(pcm) if op in ("split", "digits2") else [pcm]
                m = await erc.play_and_listen(audio_source, agent_audio, b"".join(chunks))
            m.update({"text": r["text"], "op": op})
            measures.append(m)
            first = f"{m['first_audio_ms'] / 1000:.2f}s" if m.get("first_audio_ms") is not None else "-"
            flag = "✓" if m.get("answered") else "✗哑"
            print(
                f"    轮{i + 1}[{op}] 「{r['text'][:16]}」 → 首声 {first} · 语音 {m.get('speech_s', 0):.1f}s · {flag}",
                flush=True,
            )
            await asyncio.sleep(0.8)
    except Exception as exc:
        print(f"[latency-soak] 场景 {key} 异常中断: {exc!r}", flush=True)
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
    # 对齐只认「真对话行」:starve-ack/storm-listen/wa-stash 等机制行带 provider
    # 标签,混进来会把游标顶歪(首跑轮8/11 拆轮旗误报实证)。
    user_rows = [
        str(t.get("transcript") or "")
        for t in turns
        if t.get("role") == "user" and not (t.get("provider") or "").strip()
    ]
    turn_counts = assign_user_turns(
        texts, user_rows,
        multi=[r["op"] in ("split", "digits2") for r in rounds],
    )
    for m, c in zip(measures, turn_counts):
        m["user_turns"] = c
        m["split"] = c > 1 and m["op"] in ("normal", "fast", "split", "digits2")

    # 日志窗口：PERCEIVED 三段 + 哨兵计数。
    perceived: list[dict] = []
    counts = {k: 0 for k in COUNT_MARKERS}
    budget_hits: list[int] = []
    try:
        window = erc.LOG_PATH.read_bytes()[log_offset:]
        for raw in window.splitlines():
            line = raw.decode("utf-8", errors="replace")
            mt = RE_PERCEIVED.search(line)
            if mt:
                perceived.append({
                    "total": int(mt.group(1)), "eou": int(mt.group(2)),
                    "llm": int(mt.group(3)), "tts": int(mt.group(4)),
                })
                continue
            mb = RE_BUDGET.search(line)
            if mb:
                budget_hits.append(int(mb.group(1)))
                continue
            for k in COUNT_MARKERS:
                if k in line:
                    counts[k] += 1
    except Exception:
        pass

    summary = summarize_report(measures, perceived, counts, budgets)
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
    print(f"{'轮':>3} {'op':<9} {'首声':>7} {'拆轮':>4} {'哑':>3}  文本", flush=True)
    for i, m in enumerate(res["measures"], start=1):
        first = f"{m['first_audio_ms'] / 1000:.2f}s" if m.get("first_audio_ms") is not None else "无"
        uc = int(m.get("user_turns") or 0)
        flags = []
        if m.get("split"):
            flags.append("拆轮!")
        if not m.get("answered"):
            flags.append("哑")
        if m.get("first_audio_ms") is not None and m["first_audio_ms"] > budgets["first_ms"]:
            flags.append("超首声预算")
        print(
            f"{i:>3} {m['op']:<9} {first:>7} {uc if uc else '?':>4} "
            f"{'哑' if not m.get('answered') else '':>3}  {m['text'][:18]}"
            + ("  [" + ",".join(flags) + "]" if flags else ""),
            flush=True,
        )
    fa, pd = s["first_audio"], s["perceived"]
    print(
        f"\n墙钟首声 n={fa['n']} p50={fa['p50']:.0f}ms p95={fa['p95']:.0f}ms max={fa['max']:.0f}ms "
        f"超标(>{budgets['first_ms']:.0f})={fa['over_budget']}",
        flush=True,
    )
    if pd["n"]:
        print(
            f"PERCEIVED n={pd['n']} p50={pd['p50']:.0f}ms p95={pd['p95']:.0f}ms max={pd['max']:.0f}ms "
            f"超标(>{budgets['perceived_ms']:.0f})={pd['over_budget']} (预算哨兵={len(res['budget_hits'])})",
            flush=True,
        )
    else:
        print("PERCEIVED 无样本（全旁路轮?）", flush=True)
    active = {k: v for k, v in s["markers"].items() if v}
    print(f"哨兵计数：{active if active else '（无）'}", flush=True)
    print(f"小计[{res['key']}]: {s['rounds']} 轮 · 有答 {s['answered']} · 哑 {s['mute']}", flush=True)


async def main() -> int:
    parser = argparse.ArgumentParser(description="多轮多样话术延迟测试台")
    parser.add_argument("--scenario", choices=[*SOAK_SCENARIOS, "all"], default="soak-canto")
    parser.add_argument("--persona-id", default=None)
    parser.add_argument("--no-adversarial", action="store_true", help="只跑正常轮（基线对照）")
    parser.add_argument("--budget-first-ms", type=float, default=2500.0)
    parser.add_argument("--budget-perceived-ms", type=float, default=3000.0)
    args = parser.parse_args()
    budgets = {"first_ms": args.budget_first_ms, "perceived_ms": args.budget_perceived_ms}
    keys = list(SOAK_SCENARIOS) if args.scenario == "all" else [args.scenario]

    results = []
    for key in keys:
        results.append(await run_scenario(
            key, args.persona_id, adversarial=not args.no_adversarial, budgets=budgets
        ))

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"{int(time.time())}-{'-'.join(keys)}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[latency-soak] JSON 报告 → {out}", flush=True)

    total_mute = sum(r["summary"]["mute"] for r in results)
    total_rounds = sum(r["summary"]["rounds"] for r in results)
    fail = total_mute >= erc.FAIL_MUTE_ROUNDS or any(not r["setup_ok"] for r in results)
    print(
        f"LATENCY_SOAK 场景={len(results)} 总轮数={total_rounds} 哑轮={total_mute} → "
        f"{'FAIL' if fail else 'PASS'}",
        flush=True,
    )
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
