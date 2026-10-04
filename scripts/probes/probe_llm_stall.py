#!/usr/bin/env python3
"""probe_llm_stall.py — 慢速注入诊断探针（spec 2026-09-29 v2 P0.2，纯诊断）。

目的：可控复现「2.6 tps 慢速 LLM 流下 agent 全链行为」，钉死「tee 捕到 10 字、
bidi 零 task_continue」的未闭案环（call-ed6aa9b8 三轮实证，正常 tps 复现不出）。
**纯诊断**：不导向修复处置（v1 三窗口方案已否决），产出留档。

接线：本探针内嵌限速 SSE 代理（:12399 → 上游 :1235），worker 须经
`MLX_LLM_BASE_URL=http://127.0.0.1:12399/v1` 起栈。复现时探针打印 job pid
与 py-spy 命令行（本探针不代执行 py-spy——由操作员在另端跑，命令行直接复制）。

退出码：0=复现 / 2=未复现（正常形态记录）/ 1=环境不满足。
报告恒落 <repo>/reports/llm-stall-replay-<时间戳>/（常量派生路径）。
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

_SCRIPTS = Path(__file__).resolve().parents[1]  # G1c 入桶后 scripts/ 根=parents[1]
sys.path.insert(0, str(_SCRIPTS))
import e2e_real_customer as erc  # noqa: E402  复用:CONTROL_PLANE_URL/CP_HEADERS/tts_pcm/LOG_PATH/push_pcm

_REPO_ROOT = _SCRIPTS.parent
REPORTS_ROOT = _REPO_ROOT / "reports"
PROXY_PORT = 12399
UPSTREAM = os.environ.get("LLM_UPSTREAM", "http://127.0.0.1:1235")
ROUNDS = ("你好", "吃什么包裹啊？", "系什么包裹？")  # 坏通 call-ed6aa9b8 同款轮次
BAD_RE = re.compile(r"MINIMAX_BIDI_PERF sentences=0 canceled=1")
PID_RE = re.compile(r'"pid": (\d+)')


def _report_dir() -> Path:
    d = REPORTS_ROOT / ("llm-stall-replay-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _latest_job_pid(agent_off: int) -> str:
    """从 agent.log 窗口解析本通 job 的 pid（复现时打印 py-spy 命令行用）。"""
    win = erc.log_slice_markers(agent_off) or []
    pids: list[str] = []
    try:
        raw = erc.LOG_PATH.read_bytes()[agent_off:].decode("utf-8", errors="ignore")
        pids = PID_RE.findall(raw)
    except Exception:
        pass
    return pids[-1] if pids else "?"


async def throttled_proxy(tps: float, stop: asyncio.Event) -> None:
    """SSE 限速透明代理：/v1/chat/completions 流式响应逐 SSE event 注入 delay。

    delay = event 字符数 / (tps × 1.6)（中文 ~1.6 char/token 均摊）；其他路径
    （/v1/models、健康检查、非流式）原样直通。aiohttp 双端。
    """
    from aiohttp import ClientSession, ClientTimeout, web

    delay_per_char = 1.0 / (tps * 1.6)

    async def relay(request: web.Request) -> web.StreamResponse:
        body = await request.read()
        url = f"{UPSTREAM}{request.rel_url}"
        hdrs = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
        sess = request.app["client"]
        resp = await sess.request(
            request.method, url, data=body, headers=hdrs, timeout=ClientTimeout(total=600)
        )
        ct = resp.headers.get("content-type", "application/json")
        if "text/event-stream" not in ct:
            payload = await resp.read()
            return web.Response(status=resp.status, body=payload, content_type=ct.split(";")[0])
        # SSE：逐 event 限速转发
        out = web.StreamResponse(status=resp.status, headers={"Content-Type": ct})
        await out.prepare(request)
        buf = b""
        async for chunk in resp.content.iter_any():
            buf += chunk
            while b"\n\n" in buf:
                evt, buf = buf.split(b"\n\n", 1)
                await asyncio.sleep(len(evt) * delay_per_char)
                await out.write(evt + b"\n\n")
        if buf:
            await asyncio.sleep(len(buf) * delay_per_char)
            await out.write(buf)
        await out.write_eof()
        return out

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", relay)

    async def _client(app):
        app["client"] = ClientSession()
        yield
        await app["client"].close()

    app.cleanup_ctx.append(_client)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PROXY_PORT)
    await site.start()
    print(f"[stall] 限速代理 :{PROXY_PORT} → {UPSTREAM} @ {tps} tps", flush=True)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


def doctor() -> bool:
    """前置：CP/LiveKit 可达（栈在跑）。"""
    try:
        httpx.get(f"{erc.CONTROL_PLANE_URL}/api/calls?account_id=acc-001", timeout=5, headers=erc.CP_HEADERS)
    except Exception as exc:
        print(f"[stall] doctor FAIL：CP 不可达（{exc!r}）——先起栈", flush=True)
        return False
    print(
        f"[stall] doctor WARN：确保以 MLX_LLM_BASE_URL=http://127.0.0.1:{PROXY_PORT}/v1 "
        f"BOK_LOCAL_TTS=1 python tools/bok.py serve 起栈——否则本跑不限速（对照臂亦有效）",
        flush=True,
    )
    return True


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tps", type=float, default=2.6, help="注入目标 tps（坏通实测 2.6）")
    args = ap.parse_args()

    if not doctor():
        return 1
    out = _report_dir()
    stop = asyncio.Event()
    proxy_task = asyncio.create_task(throttled_proxy(args.tps, stop))

    agent_off = erc.LOG_PATH.stat().st_size if erc.LOG_PATH.exists() else 0
    call_id, _voice = erc.create_call("cantonese", None)
    print(f"[stall] call={call_id}（log offset {agent_off}）", flush=True)

    room = rtc.Room()
    read_tasks: list[asyncio.Task] = []
    reproduced = False
    timeline_lines: list[str] = [f"# 慢速注入时间线（tps={args.tps}）", f"- call={call_id}", ""]

    try:
        data = httpx.post(
            f"{erc.CONTROL_PLANE_URL}/api/token",
            json={"account_id": "acc-001", "call_id": call_id},
            timeout=10, headers=erc.CP_HEADERS,
        ).json()
        await room.connect(data["serverUrl"], data["participantToken"])
        src = rtc.AudioSource(sample_rate=16000, num_channels=1)
        track = rtc.LocalAudioTrack.create_audio_track("customer-src", src)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        pcms = [erc.tts_pcm(t, "cantonese") for t in ROUNDS]
        print(f"[stall] 预合成 {len(pcms)} 轮完成", flush=True)
        # 等开场白播完再推（同 FLOW20 姿势）：不等的话推入音频落在 opening
        # 播放期，VAD/endpointing 全程不触发=零转写（T2 首跑实证）。
        agent_audio = bytearray()

        def _collect(track) -> None:
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

            read_tasks.append(asyncio.get_running_loop().create_task(_read()))

        @room.on("track_subscribed")
        def _on_track2(track, _pub, _part):
            _collect(track)

        setup_ok = await erc.wait_greeting(agent_audio)
        print(f"[stall] 开场白 {'OK' if setup_ok else '45s 未出声（照常推进）'}", flush=True)
        agent_audio.clear()
        for i, pcm in enumerate(pcms, 1):
            await erc.push_pcm(src, pcm)
            t_push = time.perf_counter()
            reproduced_now = False
            while time.perf_counter() - t_push < 30:
                await asyncio.sleep(2)
                win = erc.log_slice_markers(agent_off)
                if BAD_RE.search("\n".join(win)):
                    reproduced_now = True
                    break
            timeline_lines.append(f"- 轮{i} 「{ROUNDS[i - 1]}」 坏标记={'Y' if reproduced_now else 'N'}")
            print(f"[stall] 轮{i} 完成 坏标记={'复现' if reproduced_now else '未见'}", flush=True)
            if reproduced_now:
                reproduced = True
                pid = _latest_job_pid(agent_off)
                print(
                    f"[stall] *** 复现 *** job pid={pid} —— 立即另端执行：\n"
                    f"    /opt/homebrew/bin/py-spy dump --pid {pid} > {out / ('pyspy_r' + str(i) + '_' + pid + '.txt')}",
                    flush=True,
                )
                break  # 抓到即收工（栈由操作员当场 dump；探针不再推进轮次）
            await asyncio.sleep(2.0)
    except Exception as exc:
        timeline_lines.append(f"- 异常: {exc!r}")
        print(f"[stall] 异常中断: {exc!r}", flush=True)
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
        stop.set()
        proxy_task.cancel()

    # 时间线归档（含 agent.log 窗口标记）
    try:
        win = erc.log_slice_markers(agent_off)[:200]
        timeline_lines += ["", "## agent.log 窗口（前 200 行标记）", "```"] + win + ["```"]
    except Exception:
        pass
    (out / "timeline.md").write_text("\n".join(timeline_lines), encoding="utf-8")
    print(f"[stall] 报告落 {out / 'timeline.md'}；复现={reproduced}", flush=True)
    return 0 if reproduced else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
