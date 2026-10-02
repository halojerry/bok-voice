#!/usr/bin/env python3
"""粤语音系补位层实弹验收探针(2026-09-22)。

四腿真通话(hit/literal/zh/kill),复用 e2e_real_customer 骨架(建通/推流/收音/
turns/日志切窗,同 probe_branch_action 姿势):

- hit     粤语:触发语「你要怎么陪我呢」(陪→赔 同音,真库实证 0.838,字面必 miss)
           → 硬判据:触发窗 QA_FASTPATH phonetic=1 + 触发轮后 assistant gen=qa_fastpath + 非哑轮
- literal 粤语:触发语「要点样赔」(词条原文,字面 1.0)
           → 硬判据:触发窗 QA_FASTPATH hit=1 且全程无 phonetic=1 + gen=qa_fastpath(parity)
- zh      普通话:同一句「你要怎么陪我呢」→ 硬判据:全程无 phonetic=1 + 全程无
           qa_fastpath(zh 无音系档,字面 0.314 miss)+ 非哑轮(LLM 兜底)
- kill    粤语,须先以 BOK_QA_PHONETIC=0 重启 agent worker(--expect-off 语义):
           → 硬判据:全程无 phonetic=1 + 触发轮后无 qa_fastpath + 非哑轮(LLM 兜底)

词条零变更:目标条目(qa-1789151462259-8「要点样赔」)是 acc-001 共享条目、罐头
音频已物化(canned-status ok,音色 Cantonese_crisp_news_anchor_vv2)——hit/literal
腿必须 --persona-voice 与物化音色一致,运行时 PCM 查键才命中。

离线锚定(2026-09-22 真库副本重放,判定基础):
  你要怎么陪我呢@cantonese → phonetic 0.838(字面 miss)
  你要怎么陪我呢@zh        → None 0.314
  要点样赔@cantonese        → 字面 1.000
  两句 warmup                → 全 miss(0.131/0.109)
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from livekit import rtc

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent
sys.path.insert(0, str(_SCRIPTS))

import e2e_real_customer as erc  # noqa: E402  复用骨架:建通/推流/收音/turns/日志路径

REPORT_DIR = _ROOT / "reports" / "qa-phonetic"

_CP_HEADERS: dict[str, str] = {}
if os.environ.get("BOK_CP_TOKEN", "").strip():
    _CP_HEADERS["Authorization"] = f"Bearer {os.environ['BOK_CP_TOKEN'].strip()}"

ACCOUNT_ID = os.environ.get("BOK_PROBE_ACCOUNT", "acc-001")
# hit/literal 腿人设音色 = QA 罐头物化音色(canned-status 实查),运行时 PCM 键才对得上
CANTONESE_CANNED_VOICE = "Cantonese_crisp_news_anchor_vv2"

TRAILING_SILENCE_S = 0.8
PCM_BYTES_PER_S = 32000  # 16kHz·16bit·单声道
ROUND_SETTLE_S = 1.2

RE_PHONETIC = re.compile(r"QA_FASTPATH phonetic=1\b")
RE_HIT = re.compile(r"QA_FASTPATH hit=1\b")
RE_BYPASS = re.compile(r"QA_FASTPATH bypass reason=(\S+)")

# 探针模板(三步:身份/赔偿 say 步/收尾;无分支行无热词——热词会 bias ASR 朝词条
# 原文写、把音系腿变成字面腿)。第 2 步 say=1 直念步:纯提问原地答不推进——
# 触发轮(赔偿问句)才能进 QA 快路(hit 腿首跑实证:非 say 步被 rule=auto 推进,
# advanced 旁路是引擎正确行为,不是缺陷)。名字含 probe 前缀走测试卫生惯例。
PROBE_STEPS_CANTONESE: list[dict] = [
    {"goal": "確認身份", "ref": "你好，請問係{姓名}嗎？"},
    {"goal": "說明賠償方案", "say": 1, "ref": "如果確認丟件，我哋會按平台規則賠付。"},
    {"goal": "收尾確認", "ref": "好嘅，感謝你嘅時間，再見。"},
]
PROBE_STEPS_ZH: list[dict] = [
    {"goal": "确认身份", "ref": "你好，请问是{姓名}吗？"},
    {"goal": "说明赔偿方案", "say": 1, "ref": "如果确认丢件，我们会按平台规则赔付。"},
    {"goal": "收尾确认", "ref": "好的，感谢您的时间，再见。"},
]

# 轮次话术(句形铁律:无逗号、<10 字单口气;触发语离线锚定见模块 docstring)
# warmup 不含否定词(首跑「又唔系」被 OBJECTION 旁路——无害但噪声,换净)。
WARMUP_CANTONESE = "係我本人呀請講啦"
WARMUP_ZH = "对的我就是本人有话请讲"
TRIGGER = "你要怎么陪我呢"          # 陪→赔 同音(cantonese 音系 0.838 / zh miss)
TRIGGER_LITERAL = "要点样赔"        # 词条原文(cantonese 字面 1.0)

LEG_TEXTS: dict[str, list[tuple[str, str]]] = {
    "hit":     [("warmup", WARMUP_CANTONESE), ("trigger", TRIGGER)],
    "literal": [("warmup", WARMUP_CANTONESE), ("trigger", TRIGGER_LITERAL)],
    "zh":      [("warmup", WARMUP_ZH), ("trigger", TRIGGER)],
    "kill":    [("warmup", WARMUP_CANTONESE), ("trigger", TRIGGER)],
}
# 触发语转写归因(信息位:转写没吐出触发词时,失配不可归因于音系层,是 ASR 侧)
TRIGGER_KEYWORDS: dict[str, list[str]] = {
    "hit": ["陪", "赔"], "literal": ["赔"], "zh": ["陪", "赔"], "kill": ["陪", "赔"],
}


def _cp(path: str, *, method: str = "GET", **kw) -> httpx.Response:
    """CP 请求单点(探针只打本机回环 CP——显式边界校验,拒绝任何非本机目标)。"""
    base = urlparse(erc.CONTROL_PLANE_URL)
    if base.scheme not in ("http", "https") or base.hostname not in (
        "127.0.0.1", "localhost", "::1",
    ):
        raise ValueError(f"probe 只允许本机 CP,拒绝目标: {erc.CONTROL_PLANE_URL!r}")
    kw.setdefault("timeout", 15)
    return httpx.request(method, f"{erc.CONTROL_PLANE_URL}{path}", headers=_CP_HEADERS, **kw)


def create_probe_template(lang: str) -> str:
    steps = PROBE_STEPS_CANTONESE if lang == "cantonese" else PROBE_STEPS_ZH
    resp = _cp("/api/templates", method="POST", json={
        "account_id": ACCOUNT_ID,
        "name": f"probe-qa-phonetic-{lang}-{int(time.time())}",
        "language": lang,
        "steps_json": json.dumps(steps, ensure_ascii=False),
    })
    resp.raise_for_status()
    return str(resp.json().get("id") or "")


def create_probe_call(lang: str, template_id: str, voice: str) -> tuple[str, str]:
    ts = int(time.time() * 1000) % 100000
    obj = _cp("/api/objects", method="POST", params={"account_id": ACCOUNT_ID}, json={
        "display_name": f"E2E-陳小明-{ts}",
        "role_template": "buyer",
        "language": lang,
        "background": "qa phonetic probe",
        "template_id": template_id,
    })
    obj.raise_for_status()
    persona = _cp("/api/personas", method="POST", json={
        "account_id": ACCOUNT_ID,
        "name": f"E2E音系{lang}",
        "language": lang,
        "tone": "礼貌专业",
        "reference_audio": voice,
    })
    persona.raise_for_status()
    call = _cp("/api/calls", method="POST", json={
        "account_id": ACCOUNT_ID,
        "object_id": obj.json()["id"],
        "persona_id": persona.json()["id"],
        "template_id": template_id,
        "mode": "live",
        "direction": "webrtc",
        "language": lang,
    })
    call.raise_for_status()
    return str(call.json()["id"]), str(persona.json().get("id") or "")


def delete_template(template_id: str) -> None:
    with contextlib.suppress(Exception):
        _cp(f"/api/templates/{template_id}", method="DELETE")


# 日志窗口工具已收编 erc 共享件（2026-09-22 三探针单点）：语义=「marks[0]=
# 通话前日志大小、窗口 k=第 k 轮」。本地旧版 log_windows 曾把窗口 0 当成
# 0→起始偏移=整个历史日志（他通话行污染 absence 判据+轮名错位一位），实弹
# 修复后按共享契约收编。
wait_log_stable = erc.wait_log_stable
log_windows = erc.log_windows


async def fetch_turns(call_id: str, settle_s: float = 12.0) -> list[dict]:
    last: list[dict] = []
    stable = 0
    deadline = time.perf_counter() + settle_s
    while time.perf_counter() < deadline:
        rows = _cp(f"/api/calls/{call_id}/turns").json()
        if not isinstance(rows, list):
            rows = []
        if rows and len(rows) == len(last):
            stable += 1
            if stable >= 2:
                return rows
        else:
            stable = 0
        last = rows
        await asyncio.sleep(1.5)
    return last


async def wait_playout_end(agent_audio: bytearray, *, quiet_s: float = 2.0,
                           timeout_s: float = 30.0) -> tuple[float, bool]:
    """等 agent 语音静默(答完判据同 erc 口径:连续 quiet_s 无新增音频)。"""
    last_len = len(agent_audio)
    frozen = None
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout_s:
        await asyncio.sleep(0.2)
        if len(agent_audio) == last_len:
            if frozen is None:
                frozen = time.perf_counter()
            elif time.perf_counter() - frozen >= quiet_s:
                return time.perf_counter() - t0 - quiet_s, True
        else:
            last_len = len(agent_audio)
            frozen = None
    return timeout_s, False


def fastpath_rows(turns: list[dict]) -> list[dict]:
    return [t for t in turns if str(t.get("gen") or "") == "qa_fastpath"]


def trigger_user_row(turns: list[dict], leg: str) -> tuple[dict | None, int]:
    """找触发轮 user 行(归因关键词),返回 (行, 索引)。"""
    kws = TRIGGER_KEYWORDS.get(leg, [])
    for i, t in enumerate(turns):
        if str(t.get("role") or "") != "user":
            continue
        txt = str(t.get("transcript") or "")
        if any(k in txt for k in kws):
            return t, i
    return None, -1


def assistant_gen_after(turns: list[dict], idx: int) -> list[str]:
    """触发 user 行之后的 assistant 轮 gen 序列(截 4 行)。"""
    return [str(t.get("gen") or "") for t in turns[idx + 1: idx + 5]
            if str(t.get("role") or "") == "assistant"]


async def run_leg(*, leg: str, lang: str, voice: str, keep_template: bool) -> dict:
    rounds = LEG_TEXTS[leg]
    print(f"\n[qa-phonetic] 腿={leg} lang={lang} voice={voice} rounds={[n for n, _ in rounds]}",
          flush=True)
    template_id = create_probe_template(lang)
    print(f"[qa-phonetic] template={template_id}", flush=True)
    try:
        call_id, persona_id = create_probe_call(lang, template_id, voice)
        print(f"[qa-phonetic] call={call_id} persona={persona_id}", flush=True)
        pcms = {
            name: erc.tts_pcm(text, lang) + b"\x00" * int(TRAILING_SILENCE_S * PCM_BYTES_PER_S)
            for name, text in rounds
        }
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
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    with contextlib.suppress(Exception):
                        await stream.aclose()

            read_tasks.append(asyncio.get_running_loop().create_task(_read()))

        room.on("track_subscribed", lambda track, _p, _pt: attach(track))
        measures: list[dict] = []
        setup_ok = False
        marks: list[int] = [erc.LOG_PATH.stat().st_size if erc.LOG_PATH.exists() else 0]
        try:
            data = _cp("/api/token", method="POST",
                       json={"account_id": ACCOUNT_ID, "call_id": call_id}).json()
            await room.connect(data["serverUrl"], data["participantToken"])
            audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
            src = rtc.LocalAudioTrack.create_audio_track("customer-src", audio_source)
            await room.local_participant.publish_track(
                src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
            setup_ok = await erc.wait_greeting(agent_audio)
            if not setup_ok:
                print("[qa-phonetic] WARN 开场白 45s×3 未出声，照常推进", flush=True)
            await wait_playout_end(agent_audio)
            await asyncio.sleep(0.3)
            for name, text in rounds:
                await wait_playout_end(agent_audio)
                m = await erc.play_and_listen(audio_source, agent_audio, pcms[name])
                m.update({"name": name, "text": text})
                measures.append(m)
                marks.append(await wait_log_stable())
                first = f"{m['first_audio_ms'] / 1000:.2f}s" if m.get("first_audio_ms") is not None else "-"
                print(f"    {name:>8} 「{text}」 → 首声 {first} · 语音 {m.get('speech_s', 0):.1f}s · "
                      f"{'✓' if m.get('answered') else '✗哑'} · log+{marks[-1] - marks[-2]}B", flush=True)
                await asyncio.sleep(0.3 + ROUND_SETTLE_S)
        except Exception as exc:  # noqa: BLE001
            print(f"[qa-phonetic] 腿 {leg} 异常中断: {exc!r}", flush=True)
        finally:
            with contextlib.suppress(Exception):
                await room.disconnect()
            for t in read_tasks:
                t.cancel()
            with contextlib.suppress(Exception):
                _cp(f"/api/calls/{call_id}/hangup", method="POST")
                _cp(f"/api/calls/{call_id}/settle", method="POST", timeout=60)

        turns = await fetch_turns(call_id)
        # 共享 log_windows 契约（erc）：marks[0]=通话前日志大小 → 窗口 k=第 k 轮。
        # （本地旧版曾把窗口 0 切成 0→起始偏移=整个历史日志，他通话行污染
        # absence 判据+轮名错位一位——首跑实弹修复，现随共享件收编消失。）
        windows = log_windows(marks)
        names = [n for n, _ in rounds]
        win_by_name = {name: windows[i] for i, name in enumerate(names) if i < len(windows)}
        all_lines = [ln for w in windows for ln in w]
        phonetic_lines = [ln for ln in all_lines if RE_PHONETIC.search(ln)]
        hit_lines = [ln for ln in all_lines if RE_HIT.search(ln)]
        bypass_lines = [ln for ln in all_lines if RE_BYPASS.search(ln)]
        trig_win = win_by_name.get("trigger", [])
        trig_phonetic = [ln for ln in trig_win if RE_PHONETIC.search(ln)]
        trig_hit = [ln for ln in trig_win if RE_HIT.search(ln)]
        trig_measure = next((m for m in measures if m["name"] == "trigger"), {})
        trow, tidx = trigger_user_row(turns, leg)
        gens_after = assistant_gen_after(turns, tidx) if trow else []

        # ---- 判据 ----
        checks: dict[str, bool] = {}
        answered = bool(trig_measure.get("answered"))
        if leg == "hit":
            checks["phonetic_log"] = bool(trig_phonetic)
            checks["fastpath_gen"] = ("qa_fastpath" in gens_after) if trow else False
            checks["answered"] = answered
        elif leg == "literal":
            checks["hit_log"] = bool(trig_hit)
            checks["no_phonetic"] = not phonetic_lines
            checks["fastpath_gen"] = ("qa_fastpath" in gens_after) if trow else False
        elif leg == "zh":
            checks["no_phonetic"] = not phonetic_lines
            checks["no_fastpath"] = not fastpath_rows(turns)
            checks["answered"] = answered
        else:  # kill
            checks["no_phonetic"] = not phonetic_lines
            checks["no_fastpath_after_trigger"] = ("qa_fastpath" not in gens_after) if trow else False
            checks["answered"] = answered
        evidence_ok = bool(turns) and erc.LOG_PATH.exists()
        passed = evidence_ok and all(checks.values())

        result = {
            "leg": leg, "lang": lang, "call_id": call_id, "template_id": template_id,
            "setup_ok": setup_ok, "evidence_ok": evidence_ok,
            "measures": [{k: m.get(k) for k in ("name", "text", "first_audio_ms",
                                                "speech_s", "answered")} for m in measures],
            "phonetic_lines": phonetic_lines[:6],
            "hit_lines": hit_lines[:6],
            "bypass_lines": bypass_lines[:6],
            "trigger_user_transcript": str(trow.get("transcript") or "")[:80] if trow else None,
            "gens_after_trigger": gens_after,
            "turn_rows": [{
                "role": str(t.get("role") or ""), "gen": str(t.get("gen") or ""),
                "template_step": t.get("template_step"),
                "transcript": str(t.get("transcript") or "")[:100],
            } for t in turns],
            "checks": checks, "pass": passed, "ts": int(time.time()),
        }
        print(f"[qa-phonetic] checks={ {k: int(v) for k, v in checks.items()} } → "
              f"{'PASS' if passed else 'FAIL'}"
              + ("" if evidence_ok else "（观测面不足,absence 判据不计 PASS）"), flush=True)
        if result["trigger_user_transcript"]:
            print(f"[qa-phonetic] 触发轮转写={result['trigger_user_transcript']!r}", flush=True)
        return result
    finally:
        if not keep_template:
            delete_template(template_id)


async def main() -> int:
    parser = argparse.ArgumentParser(description="粤语音系补位层实弹验收探针")
    parser.add_argument("--leg", required=True, choices=["hit", "literal", "zh", "kill"])
    parser.add_argument("--persona-voice", default="",
                        help="空=hit/literal 用罐头物化音色 Cantonese_crisp_news_anchor_vv2,"
                             "zh 用 erc 默认")
    parser.add_argument("--keep-template", action="store_true")
    args = parser.parse_args()
    lang = "zh" if args.leg == "zh" else "cantonese"
    voice = args.persona_voice or (
        CANTONESE_CANNED_VOICE if lang == "cantonese" else erc.SCENARIOS["zh"]["persona_voice"]
    )
    res = await run_leg(leg=args.leg, lang=lang, voice=voice, keep_template=args.keep_template)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"{int(time.time())}-{res['leg']}.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[qa-phonetic] JSON 报告 → {out}", flush=True)
    if args.leg == "kill":
        print("（kill 腿:须先以 BOK_QA_PHONETIC=0 重启 agent worker;跑完请起回默认档）",
              flush=True)
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
