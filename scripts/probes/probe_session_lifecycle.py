#!/usr/bin/env python3
"""probe_session_lifecycle.py — 会话生命周期审计探针（spec 2026-09-29 v2 P0.1）。

复刻真实使用连打 N 通 simulation call，审计「挂断→CP ended→LiveKit 房删→
agent job 退」全链时延与资源归位：

  --mode hangup   按钮路径：连房→说一轮→POST /hangup→断开（既有链路应达标）
  --mode abandon  关页/刷新路径：连房→说一轮→**直接断开不上报**（病灶复刻臂，
                  修前预期抓 jobs_per_room=2——job1 退→看门狗 3s 补 job2 空转）

审计项（修复目标）：
  cp_ended_s    挂断→CP status=ended        < 1s（hangup 臂）
  room_gone_s   挂断→LiveKit 房间消失       < 3s（hangup 臂）
  job_exit_s    挂断→agent job process exiting < 5s
  jobs_per_room 每房 agent job 数            == 1（abandon 臂修前预期违规）
  cache_peak    llm.log Prompt Cache 峰值    ≤ 定档值（观测项，不判红）

报告恒落 <repo>/reports/session-lifecycle-<时间戳>/report.md（路径常量派生，
不接受用户路径输入）。GPU 采样不代起（powermetrics 需 root；cache A/B 定档时
操作员手动同步观测）。退出码 0=达标 1=违规。
前置：栈已起（BOK_LOCAL_TTS=1，:8788 供刺激合成）；CP auth-off 或 BOK_CP_TOKEN。
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
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from livekit import rtc
from livekit.api import LiveKitAPI, ListRoomsRequest

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))
import e2e_real_customer as erc  # noqa: E402  复用:CONTROL_PLANE_URL/CP_HEADERS/tts_pcm/LOG_PATH

_REPO_ROOT = _SCRIPTS.parent
REPORTS_ROOT = _REPO_ROOT / "reports"
LLM_LOG = erc._default_log_dir() / "llm.log"  # noqa: SLF001  与 erc 同源日志根（App Support/logs）
TARGETS = {"cp_ended_s": 1.0, "room_gone_s": 3.0, "job_exit_s": 5.0, "jobs_per_room": 1}
STIM_TEXT = "你好"
CACHE_RE = re.compile(r"Prompt Cache: (\d+) sequences, ([\d.]+) GB")


def _report_dir() -> Path:
    """报告目录：reports/session-lifecycle-<时间戳>——常量派生零用户输入。"""
    d = REPORTS_ROOT / ("session-lifecycle-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_window(path: Path, offset: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", errors="ignore")
    except (FileNotFoundError, OSError):
        return ""


def _lk() -> LiveKitAPI:
    url = os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
    return LiveKitAPI(
        url=url,
        api_key=os.environ.get("LIVEKIT_API_KEY", "devkey"),
        api_secret=os.environ.get("LIVEKIT_API_SECRET", "secret"),
    )


def _create_call(lang: str) -> str:
    """建单（对象前缀 LC- 非 E2E 豁免——生命周期审计要真实心跳语义）。"""
    ts = int(time.time() * 1000) % 100000

    # 模板必填闸（BOK_REQUIRE_TEMPLATE）：live 档须绑正牌话术（同 erc.create_call
    # 的挑选逻辑：按语言取非测试模板）。
    template_id = ""
    try:
        tpls = httpx.get(
            f"{erc.CONTROL_PLANE_URL}/api/templates?account_id=acc-001",
            timeout=10, headers=erc.CP_HEADERS,
        ).json()
        tpls = tpls.get("items", tpls) if isinstance(tpls, dict) else tpls
        tpl = next(
            (
                t
                for t in tpls
                if str(t.get("language")) == lang
                and "e2e" not in str(t.get("name", "")).lower()
                and "probe" not in str(t.get("name", "")).lower()
            ),
            None,
        )
        template_id = str(tpl.get("id") or "") if tpl else ""
    except Exception:  # noqa: BLE001 - 模板拉不到=退无模板链路（会撞必填闸即报错暴露）
        template_id = ""

    def _post(path: str, **kw) -> dict:
        resp = httpx.post(f"{erc.CONTROL_PLANE_URL}{path}", timeout=15, headers=erc.CP_HEADERS, **kw)
        resp.raise_for_status()
        return resp.json()

    obj = _post(
        "/api/objects?account_id=acc-001",
        json={
            "display_name": f"LC-陳小明-{ts}",
            "role_template": "buyer",
            "language": lang,
            "background": "lifecycle probe",
            "template_id": template_id,
        },
    )
    persona = _post(
        "/api/personas?account_id=acc-001",
        json={
            "name": f"LC-生命周期{lang}",
            "language": lang,
            "tone": "礼貌专业",
            "reference_audio": "",
        },
    )
    call = _post(
        "/api/calls",
        json={
            "account_id": "acc-001",
            "object_id": obj["id"],
            "persona_id": persona["id"],
            "mode": "live",
            "direction": "webrtc",
            "language": lang,
        },
    )
    return str(call["id"])


async def _wait_call_ended(call_id: str, deadline_s: float) -> float | None:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < deadline_s:
        try:
            r = httpx.get(
                f"{erc.CONTROL_PLANE_URL}/api/calls/{call_id}",
                timeout=5, headers=erc.CP_HEADERS,
            )
            if r.status_code == 200 and str(r.json().get("status")) == "ended":
                return time.perf_counter() - t0
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return None


async def _wait_room_gone(lk: LiveKitAPI, call_id: str, deadline_s: float) -> float | None:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < deadline_s:
        try:
            rooms = await lk.room.list_rooms(ListRoomsRequest())
            if not any(r.name == call_id for r in rooms.rooms):
                return time.perf_counter() - t0
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return None


def _count_jobs(win: str, call_id: str) -> int:
    """窗口内该房 received job request 次数（行级匹配，避免子串误计）。"""
    n = 0
    for line in win.splitlines():
        if '"received job request"' in line and f'"room": "{call_id}"' in line:
            n += 1
    return n


async def _one_call(lk: LiveKitAPI, mode: str, dwell_s: float = 4.0) -> dict:
    call_id = _create_call("cantonese")
    agent_off = erc.LOG_PATH.stat().st_size if erc.LOG_PATH.exists() else 0
    llm_off = LLM_LOG.stat().st_size if LLM_LOG.exists() else 0
    row: dict = {"call": call_id, "mode": mode, "violations": []}

    room = rtc.Room()
    observer: rtc.Room | None = None
    try:
        data = httpx.post(
            f"{erc.CONTROL_PLANE_URL}/api/token",
            json={"account_id": "acc-001", "call_id": call_id},
            timeout=10, headers=erc.CP_HEADERS,
        ).json()
        await room.connect(data["serverUrl"], data["participantToken"])
        if mode == "abandon":
            # 复刻病灶形态：主客户断开后房里仍有真人（2026-09-29 三例 job2
            # 复活时 Ethan web 端没关页）——observer 以 purpose=listen 旁听身份
            # 连房滞留（独立 token：同 token 二连会挂起；listen 不排看门狗=
            # 零副作用），令看门狗看到「真人在场 + 无 agent」的补派窗。
            ldata = httpx.post(
                f"{erc.CONTROL_PLANE_URL}/api/token",
                json={"account_id": "acc-001", "call_id": call_id, "purpose": "listen"},
                timeout=10, headers=erc.CP_HEADERS,
            ).json()
            observer = rtc.Room()
            await asyncio.wait_for(
                observer.connect(ldata["serverUrl"], ldata["participantToken"]),
                timeout=10,
            )
        src = rtc.AudioSource(sample_rate=16000, num_channels=1)
        track = rtc.LocalAudioTrack.create_audio_track("customer-src", src)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        # 等 agent 入房开场（session_started 标记=job 已活）
        for _ in range(60):
            win = _log_window(erc.LOG_PATH, agent_off)
            if "session_started" in win and call_id in win:
                break
            await asyncio.sleep(0.5)
        # 收 agent 音轨等开场白播完（同 FLOW20 姿势——不等就推=推入音频落在
        # opening 播放期，VAD 全程不触发=零转写，2026-09-30 探针首跑实证）。
        agent_audio = bytearray()
        read_tasks: list[asyncio.Task] = []

        def _collect(t) -> None:
            if int(t.kind) != int(rtc.TrackKind.KIND_AUDIO):
                return
            if getattr(t, "name", "") not in ("roomio_audio", "background_audio"):
                return

            async def _read() -> None:
                stream = rtc.AudioStream(t, sample_rate=16000, num_channels=1)
                try:
                    async for event in stream:
                        frame = getattr(event, "frame", event)
                        agent_audio.extend(bytes(frame.data))
                except Exception:
                    pass

            read_tasks.append(asyncio.get_running_loop().create_task(_read()))

        @room.on("track_subscribed")
        def _on_track(t, _pub, _part):
            _collect(t)

        await erc.wait_greeting(agent_audio)
        # 推一轮话（真实会话活动，令 ASR/LLM/TTS 资源真实占用）
        await erc.push_pcm(src, erc.tts_pcm(STIM_TEXT, "cantonese"))
        await asyncio.sleep(dwell_s)  # 听答+真实停留（--dwell 长停留复刻连打中的长通话）
    finally:
        t_hangup = time.perf_counter()
        for _t in read_tasks:
            _t.cancel()
        try:
            await asyncio.wait_for(room.disconnect(), timeout=10)
        except Exception:
            pass
        if mode == "hangup":
            httpx.post(
                f"{erc.CONTROL_PLANE_URL}/api/calls/{call_id}/hangup",
                timeout=10, headers=erc.CP_HEADERS,
            )

    # 审计窗（回收器 5 分钟兜底不等待满——60s 足够暴露补派）
    if mode == "hangup":
        row["cp_ended_s"] = await _wait_call_ended(call_id, 15.0)
        row["room_gone_s"] = await _wait_room_gone(lk, call_id, 15.0)
    else:
        row["cp_ended_s"] = None  # abandon: 未上报，预期一直 active（记录不判红）
        row["room_gone_s"] = await _wait_room_gone(lk, call_id, 20.0)
    # job exit：等 process exiting（abandon 臂 job1 也会因 disconnect 退）
    job_exit = None
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 60.0:
        win = _log_window(erc.LOG_PATH, agent_off)
        if f'"room": "{call_id}"' in win and '"process exiting"' in win:
            job_exit = time.perf_counter() - t_hangup
            break
        await asyncio.sleep(0.5)
    row["job_exit_s"] = job_exit

    # room_gone 复测（hangup 臂）：job 退出后房间才真正空——首轮并行测的
    # None 是探针缺陷（job 15s 收尾期房间必然在），job 退后再等房删。
    if mode == "hangup" and row.get("room_gone_s") is None:
        row["room_gone_s"] = await _wait_room_gone(lk, call_id, 15.0)

    # 结算窗后再数 job/补派（补派发生在 job1 退后 ~3s，abandon 臂等 25s 足够）
    await asyncio.sleep(25.0 if mode == "abandon" else 5.0)
    if observer is not None:
        try:
            await asyncio.wait_for(observer.disconnect(), timeout=10)  # 补派窗观测完，observer 撤场
        except Exception:
            pass
    win = _log_window(erc.LOG_PATH, agent_off)
    row["jobs"] = _count_jobs(win, call_id)
    lwin = _log_window(LLM_LOG, llm_off)
    peaks = [(int(a), float(b)) for a, b in CACHE_RE.findall(lwin)]
    row["cache_peak"] = f"{max((p[1] for p in peaks), default=0.0):.2f}GB/{max((p[0] for p in peaks), default=0)}seq"

    # 清尾：确保本通彻底收摊（hangup 兜底 + 等房亡），防污染下一通
    try:
        httpx.post(f"{erc.CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", timeout=10, headers=erc.CP_HEADERS)
    except Exception:
        pass
    await _wait_room_gone(lk, call_id, 30.0)
    await asyncio.sleep(3.0)

    # 判定
    if mode == "hangup":
        for k, tgt in (("cp_ended_s", TARGETS["cp_ended_s"]), ("room_gone_s", TARGETS["room_gone_s"])):
            v = row.get(k)
            if v is None or v > tgt:
                row["violations"].append(f"{k}={v if v is None else round(v, 1)}s > {tgt}s")
    if row["job_exit_s"] is None or row["job_exit_s"] > TARGETS["job_exit_s"]:
        row["violations"].append(f"job_exit_s={row['job_exit_s']} > {TARGETS['job_exit_s']}s")
    if row["jobs"] != TARGETS["jobs_per_room"]:
        row["violations"].append(f"jobs_per_room={row['jobs']} != {TARGETS['jobs_per_room']}")
    return row


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", type=int, default=10)
    ap.add_argument("--mode", choices=["hangup", "abandon"], default="hangup")
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--dwell", type=float, default=4.0, help="每通停留秒数（长停留复刻双 job 病灶时窗）")
    args = ap.parse_args()

    # 前置检查
    try:
        httpx.get(f"{erc.CONTROL_PLANE_URL}/api/calls?account_id=acc-001", timeout=5, headers=erc.CP_HEADERS)
    except Exception as exc:
        print(f"[lc] CP 不可达（{exc!r}）——先起栈", flush=True)
        return 1

    out = _report_dir()
    lk = _lk()
    rows: list[dict] = []
    try:
        for i in range(args.calls):
            print(f"[lc] 通 {i + 1}/{args.calls} mode={args.mode}", flush=True)
            rows.append(await _one_call(lk, args.mode, dwell_s=args.dwell))
            r = rows[-1]
            print(
                f"    call={r['call'][-8:]} jobs={r['jobs']} job_exit={r['job_exit_s'] and round(r['job_exit_s'], 1)}s "
                f"ended={r['cp_ended_s'] and round(r['cp_ended_s'], 1)}s room_gone={r['room_gone_s'] and round(r['room_gone_s'], 1)}s "
                f"cache_peak={r['cache_peak']} {'⚠ ' + '; '.join(r['violations']) if r['violations'] else 'OK'}",
                flush=True,
            )
            if i < args.calls - 1:
                await asyncio.sleep(args.interval)
    finally:
        await lk.aclose()

    bad = sum(1 for r in rows if r["violations"])
    lines = [
        "# 会话生命周期审计报告",
        f"- 时间：{datetime.now().isoformat(timespec='seconds')}  模式：{args.mode}  通数：{len(rows)}",
        f"- 目标：cp_ended<={TARGETS['cp_ended_s']}s room_gone<={TARGETS['room_gone_s']}s "
        f"job_exit<={TARGETS['job_exit_s']}s jobs/room={TARGETS['jobs_per_room']}",
        "",
        "| call | mode | jobs | job_exit_s | cp_ended_s | room_gone_s | cache_peak | violations |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['call']} | {r['mode']} | {r['jobs']} | {r['job_exit_s'] and round(r['job_exit_s'], 1)} "
            f"| {r['cp_ended_s'] and round(r['cp_ended_s'], 1)} | {r['room_gone_s'] and round(r['room_gone_s'], 1)} "
            f"| {r['cache_peak']} | {'; '.join(r['violations']) or '-'} |"
        )
    lines += ["", f"**结论：{len(rows) - bad}/{len(rows)} 达标，{bad} 违规**", ""]
    report_path = out / "report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[lc] 报告落 {report_path}；违规 {bad}/{len(rows)}", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
