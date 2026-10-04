"""Volcengine 火山 TTS V3 单向流式 手动验证脚本（不泄漏密钥）。

用法（在项目根目录）：
    .venv312/bin/python scripts/test_volcano_v3.py
    .venv312/bin/python scripts/test_volcano_v3.py --modes oneshot,bidir_with_task

脚本只打印事件摘要和音频总字节数，不打印任何密钥 / token。
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
import uuid
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "agent"))

from agent_runtime.providers.volc_v3_protocol import (  # noqa: E402
    EventType,
    MsgType,
    MsgTypeFlagBits,
    Message,
    receive_message,
)

URI = "wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream"


def _load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def build_req_params(text: str, speaker: str, language: str = "") -> dict:
    params: dict = {
        "text": text,
        "speaker": speaker,
        "audio_params": {"format": "pcm", "sample_rate": 24000},
        "speech_rate": 0,
        "loudness_rate": 0,
    }
    if language:
        params["explicit_language"] = language
    if language == "yue":
        params["explicit_dialect"] = "yue"
    return params


async def collect_text(ws, label: str, timeout: float = 30.0) -> int:
    """收集会话中的消息，返回音频累计字节数。"""
    audio_bytes = 0
    while True:
        try:
            msg = await asyncio.wait_for(receive_message(ws), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"[{label}] 收包超时，结束。累计音频 {audio_bytes} 字节")
            break
        if msg.type == MsgType.Error:
            print(f"[{label}] Error: {msg}")
            break
        if msg.type == MsgType.AudioOnlyServer or msg.event == EventType.TTSResponse:
            audio_bytes += len(msg.payload)
        print(
            f"[{label}] type={msg.type.name} flag={msg.flag.name} "
            f"event={getattr(msg.event, 'name', msg.event)} seq={msg.sequence} "
            f"payload={len(msg.payload)}"
        )
        if msg.event in (EventType.SessionFinished, EventType.ConnectionFinished):
            print(f"[{label}] {getattr(msg.event, 'name', msg.event)}，结束。总音频 {audio_bytes} 字节")
            break
    return audio_bytes


def _make_ssl(insecure: bool):
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


async def run_flow(mode: str, language: str = "", insecure: bool = False) -> int:
    app_id = os.environ.get("VOLC_APP_ID", "")
    token = os.environ.get("VOLC_ACCESS_TOKEN", "")
    resource_id = os.environ.get("VOLC_RESOURCE_ID", "seed-tts-2.0")
    speaker = os.environ.get("VOLC_SPEAKER", "zh_female_vv_uranus_bigtts")
    if not app_id or not token:
        print("缺少 VOLC_APP_ID / VOLC_ACCESS_TOKEN，跳过。")
        return 1

    session_id = str(uuid.uuid4())
    req_params = build_req_params("你好，欢迎咨询我们的产品。", speaker, language)
    label = f"mode={mode} lang={language or 'zh'}"
    print(f"\n===== 流程：{label} =====")
    headers = {
        "X-Api-App-Id": app_id,
        "X-Api-Access-Key": token,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": str(uuid.uuid4()),
    }
    async with websockets.connect(
        URI,
        additional_headers=headers,
        open_timeout=15,
        max_size=20_000_000,
        ssl=_make_ssl(insecure),
    ) as ws:
        from agent_runtime.providers.volc_v3_protocol import (  # noqa: PLC0415
            start_connection,
            start_session,
            task_request,
            finish_session,
            finish_connection,
        )

        if mode == "oneshot":
            # 单向流式：一帧 FullClientRequest（无事件号 flag），携带 user + req_params。
            body = json.dumps(
                {"user": {"uid": "bok-voice"}, "req_params": req_params},
                ensure_ascii=False,
            ).encode("utf-8")
            msg = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.NoSeq, payload=body)
            await ws.send(msg.marshal())
        else:
            # 官方握手：StartConnection -> StartSession -> TaskRequest。
            await start_connection(ws)
            await start_session(
                ws,
                json.dumps({"req_params": req_params}).encode("utf-8"),
                session_id,
            )
            if mode == "bidir_with_task":
                await task_request(
                    ws,
                    json.dumps({"req_params": req_params}).encode("utf-8"),
                    session_id,
                )
        await collect_text(ws, label)
        try:
            await finish_session(ws, session_id)
            await finish_connection(ws)
        except Exception as exc:  # noqa: BLE001
            print(f"[{label}] 收尾异常（忽略）：{exc}")
    return 0


async def main() -> int:
    root = Path(__file__).resolve().parent.parent
    env = _load_env(root / ".env")
    os.environ.setdefault("VOLC_APP_ID", env.get("VOLC_APP_ID", ""))
    os.environ.setdefault("VOLC_ACCESS_TOKEN", env.get("VOLC_ACCESS_TOKEN", ""))
    os.environ.setdefault("VOLC_API_KEY", env.get("VOLC_API_KEY", ""))
    os.environ.setdefault("VOLC_RESOURCE_ID", env.get("VOLC_RESOURCE_ID", "seed-tts-2.0"))
    os.environ.setdefault("VOLC_SPEAKER", env.get("VOLC_SPEAKER", "zh_female_vv_uranus_bigtts"))

    insecure = "--insecure" in sys.argv
    modes = (
        sys.argv[sys.argv.index("--modes") + 1].split(",")
        if "--modes" in sys.argv
        else ["oneshot"]
    )
    for mode in modes:
        await run_flow(mode, insecure=insecure)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
