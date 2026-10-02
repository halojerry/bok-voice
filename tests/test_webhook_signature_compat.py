"""M-28（fix-wave-3，task-8 §4-B Major B）：LiveKit webhook 验签兼容 OSS 实发格式。

实弹取证（2026-09-24，/tmp/m28/captured.jsonl + livekit.log）：livekit-server
1.13.6 实发的 webhook JWT claims 只有 `iss/exp/nbf/iat/sha256`——**没有 `video`
claim**，且 sha256 是 **base64(raw digest)**；CP 旧验签器要求
`claims["video"]["webhook"]` grant + **hex** 摘要 → 真 webhook 1464/1464 全 401
（control-plane.log 按 UTC 小时分桶 2026-09-22T18→2026-09-23T18 全 401）。
对照官方轮子 livekit-api `WebhookReceiver.receive`：验签 = 签名 + sha256 body
绑定（base64），**不要求 video.webhook grant**。

修法：摘要比对同时认 hex 与 base64（新旧两代 LiveKit 官方格式）；带 `video`
claim 的 token 必须 `video.webhook` 才放行（room-join 类用户 token 与 server 同
secret 签名，摘要是唯一防重放锚——用户 token 无 sha256 claim，摘要比对天然拒）；
无 video claim 的 OSS 形状照官方 WebhookReceiver 语义放行（摘要绑定在场）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import warnings

warnings.filterwarnings("ignore")

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-webhook-compat")

import jwt as pyjwt
import pytest

SECRET = "devsecret"

BODY = json.dumps({
    "event": "participant_left",
    "room": {"name": "call-probe"},
    "participant": {"identity": "agent-AJ_x"},
}).encode()

# livekit-server 1.13.6 实发形状（/tmp/m28/captured.jsonl 取证）：无 video claim、
# sha256=base64(raw digest)、iss=api_key、nbf/exp 齐全。
OSS_SHAPE_CLAIMS = {
    "iss": "devkey",
    "sub": "",
    "nbf": int(time.time()),
    "iat": int(time.time()),
    "exp": int(time.time()) + 300,
    "sha256": base64.b64encode(hashlib.sha256(BODY).digest()).decode(),
}

# 新一代协议形状（docs/AGENTS 记载）：video.webhook grant + sha256 hex。
NEW_SHAPE_CLAIMS = {
    **OSS_SHAPE_CLAIMS,
    "video": {"webhook": True},
    "sha256": hashlib.sha256(BODY).hexdigest(),
}


def _token(claims: dict, secret: str = SECRET) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pyjwt.encode(claims, secret, algorithm="HS256")


def _post(client, token: str, body: bytes = BODY):
    return client.post(
        "/api/webhook/livekit",
        content=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from bok_voice_business_db.repository import InMemoryBusinessRepository
    from control_plane import main as cp_main

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)  # 验签语义与 auth 开关无关
    monkeypatch.setattr(cp_main.app.state, "lk_secret", SECRET, raising=False)
    return TestClient(cp_main.app)


def test_oss_shape_webhook_accepted(client):
    """livekit-server 1.13.6 实发形状（无 video claim + base64 摘要）→ 非 401。"""
    r = _post(client, _token(OSS_SHAPE_CLAIMS))
    assert r.status_code == 200, r.text


def test_new_protocol_shape_webhook_accepted(client):
    """新版协议形状（video.webhook grant + hex 摘要）→ 非 401（旧行为保留）。"""
    r = _post(client, _token(NEW_SHAPE_CLAIMS))
    assert r.status_code == 200, r.text


def test_bad_digest_rejected(client):
    """摘要不匹配 body → 拒（防篡改/防重放的核心锚，两格式同严）。"""
    claims = {**OSS_SHAPE_CLAIMS, "sha256": base64.b64encode(hashlib.sha256(b"other").digest()).decode()}
    assert _post(client, _token(claims)).status_code == 401
    claims2 = {**NEW_SHAPE_CLAIMS, "sha256": "0" * 64}
    assert _post(client, _token(claims2)).status_code == 401


def test_missing_digest_rejected(client):
    """无 sha256 claim（如泄漏的 room-join 用户 token）→ 拒。"""
    claims = {k: v for k, v in OSS_SHAPE_CLAIMS.items() if k != "sha256"}
    claims["video"] = {"roomJoin": True, "room": "call-probe"}
    assert _post(client, _token(claims)).status_code == 401


def test_video_grant_without_webhook_rejected(client):
    """带 video grant 但非 webhook（用户 token 翻新出摘要也拒）——签名不是唯一闸。"""
    claims = {**OSS_SHAPE_CLAIMS, "video": {"roomJoin": True, "room": "call-probe"}}
    assert _post(client, _token(claims)).status_code == 401


def test_wrong_secret_rejected(client):
    assert _post(client, _token(OSS_SHAPE_CLAIMS, secret="not-the-secret")).status_code == 401
