#!/usr/bin/env python3
"""MiniMax chatcompletion_v2 → OpenAI /v1/chat/completions 代理（测试腿）。

B 机云端 LLM 腿专用：agent 的 OpenAI 客户端只会打 `{base}/chat/completions`，
MiniMax 的文本端点在 `/v1/text/chatcompletion_v2` 且路径不兼容——本代理在
:1236 收 OpenAI 形状，转发 MiniMax（固定上游 api.minimax.cn，启动时做
host 白名单校验），SSE 逐行透传（chunk 形状与 OpenAI delta 兼容，多余
字段客户端自忽略），缺 [DONE] 时补一条。

启动（B 机，key 不落盘到脚本）：
  MM_LLM_KEY=$(python -c '...读 DB tts_json...') python scripts/ops/mm_llm_shim.py
选型：默认 abab6.5s-chat（非思考模型，首 content token ~450ms）；
M2/M2.5 恒先流 reasoning_content，电话腿不适用。
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


import ipaddress
import json
import os
import socket
import urllib.parse
import urllib.request

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

# 上游是编译期常量，唯一的合法目标；运行期不做任何用户可控的 URL 拼接。
UPSTREAM = "https://api.minimax.cn/v1/text/chatcompletion_v2"
ALLOWED_HOST = "api.minimax.cn"
MODEL = os.environ.get("MM_LLM_MODEL", "abab6.5s-chat")
KEY = os.environ.get("MM_LLM_KEY", "")
PORT = int(os.environ.get("MM_LLM_PORT", "1236"))


def _assert_upstream_safe() -> None:
    """SSRF 护栏：上游必须是 https + 固定域名，解析出的 IP 不得为
    私网/环回/链路本地（防 DNS rebinding 指向内网元数据面）。"""
    parsed = urllib.parse.urlparse(UPSTREAM)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
        raise SystemExit(f"upstream 不合规: {UPSTREAM}")
    for info in socket.getaddrinfo(parsed.hostname, 443):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise SystemExit(f"upstream 解析到内网地址 {ip}，拒绝启动")


app = FastAPI()


def _build_request(payload: dict) -> urllib.request.Request:
    return urllib.request.Request(
        UPSTREAM,
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
    )


@app.get("/v1/models")
def list_models() -> dict:
    return {"object": "list", "data": [{"id": MODEL, "object": "model"}]}


@app.get("/health")
def health() -> dict:
    return {"ok": bool(KEY), "model": MODEL}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    out: dict = {
        "model": MODEL,
        "messages": body.get("messages") or [],
        "stream": bool(body.get("stream")),
    }
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    # OpenAI SDK 会把 extra_body 平铺进顶层：max_tokens 保留（预热 max_tokens=1
    # 便宜），stop/top_k/repetition_penalty 等 mlx 专属字段一律剥掉不透传。
    mt = body.get("max_tokens")
    if isinstance(mt, int) and mt > 0:
        out["max_tokens"] = mt

    req = _build_request(out)

    if not out["stream"]:
        # SSRF 闸门（与 urlopen 同函数体就地校验）：上游只允许 https + 固定域名。
        _parts = urllib.parse.urlsplit(req.full_url)
        _host = (_parts.hostname or "").lower()
        if not (
            _parts.scheme == "https"
            and _host == ALLOWED_HOST
            and not _parts.username
            and not _parts.password
        ):
            raise PermissionError(f"SSRF 护栏拒绝非白名单目标: {req.full_url}")
        _opener = urllib.request.build_opener()
        with _opener.open(req, timeout=120) as resp:
            raw = resp.read()
        payload = json.loads(raw)
        base = payload.get("base_resp") or {}
        if base.get("status_code", 0) != 0:
            return JSONResponse({"error": {"message": str(base)}}, status_code=502)
        return JSONResponse(payload)

    def gen():
        saw_done = False
        # SSRF 闸门（与 urlopen 同函数体就地校验）：上游只允许 https + 固定域名。
        _parts = urllib.parse.urlsplit(req.full_url)
        _host = (_parts.hostname or "").lower()
        if not (
            _parts.scheme == "https"
            and _host == ALLOWED_HOST
            and not _parts.username
            and not _parts.password
        ):
            raise PermissionError(f"SSRF 护栏拒绝非白名单目标: {req.full_url}")
        _opener = urllib.request.build_opener()
        with _opener.open(req, timeout=120) as resp:
            for line in resp:
                stripped = line.strip()
                if stripped == b"data: [DONE]":
                    saw_done = True
                elif stripped.startswith(b"data:") and not stripped.endswith(b"[DONE]"):
                    payload = stripped[5:].strip()
                    if payload:
                        try:
                            j = json.loads(payload)
                            base = j.get("base_resp") or {}
                            if base.get("status_code", 0) != 0:
                                yield b"data: " + json.dumps(
                                    {"error": {"message": str(base)}}
                                ).encode() + b"\n\n"
                                yield b"data: [DONE]\n\n"
                                return
                        except Exception:
                            pass
                yield line
        if not saw_done:
            yield b"data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    if not KEY:
        raise SystemExit("MM_LLM_KEY 未设置（从 DB 读出后经环境变量传入，勿写死）")
    _assert_upstream_safe()  # 起服务前才做 DNS 解析——import 面(冒烟/文档引用)零网络零副作用
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
