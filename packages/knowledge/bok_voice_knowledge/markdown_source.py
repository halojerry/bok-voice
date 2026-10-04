from __future__ import annotations

import os
import urllib.parse
from pathlib import Path
from typing import Optional

from bok_voice_core.providers import MarkdownSource


class LocalMarkdownSource:
    """MarkdownSource backed by a local vault directory.

    Paths are always namespaced under `accounts/{account_id}/...` so the caller
    must pass an explicit account-scoped path. This keeps data isolated by
    account from birth, and later can be swapped for a Bok HTTP client.
    """

    def __init__(self, vault_root: str | os.PathLike[str]):
        self.root = Path(vault_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, path: str) -> Path:
        candidate = (self.root / path).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("path escapes vault")
        return candidate

    def read(self, path: str) -> str:
        return self._resolve(path).read_text(encoding="utf-8")

    def write(self, path: str, content: str) -> dict:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": path, "bytes": len(content.encode())}

    def versions(self, path: str) -> list[dict]:
        # Local MVP: single version; Bok-backed production can expose real version history.
        target = self._resolve(path)
        return [{"path": path, "version": 1, "exists": target.exists()}]

    def forget(self, path: str) -> dict:
        target = self._resolve(path)
        if target.exists():
            target.unlink()
        return {"path": path, "forgotten": True}


class BokMarkdownSource:
    """Optional MarkdownSource that proxies to a running Bok service via HTTP.

    Only used when BOK_URL is configured (later milestone). Kept as a concrete
    implementation so the KnowledgeService does not change when Bok is enabled.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8771/v1", token: str = ""):
        # scheme 白名单（2026-10-02 安全分流跟进）：base_url 是操作员 env 配置
        # （BOK_URL），但 urllib 对 file:// 等非预期 scheme 会照单全收——构造期
        # 就拒绝，配置错误在启动面炸而不是请求面静默读本地文件。
        from urllib.parse import urlsplit

        scheme = urlsplit(base_url).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                f"BokMarkdownSource base_url 仅支持 http/https，收到 {scheme!r}：{base_url!r}")
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"} if self.token else {"Content-Type": "application/json"}

    def read(self, path: str) -> str:
        import urllib.request

        with urllib.request.urlopen(f"{self.base_url}/documents/read?path={path}", headers=self._headers()) as resp:
            return resp.read().decode()

    def write(self, path: str, content: str) -> dict:
        import urllib.request

        import json

        url = f"{self.base_url}/documents/write"
        req = urllib.request.Request(
            url,
            data=json.dumps({"path": path, "content": content}).encode(),
            headers=self._headers(),
            method="POST",
        )
        # 出站闸门（sink 级就地校验）：仅 http/https、host 非空、无 userinfo。
        # base_url 是操作员 env 配置（BOK_URL），非请求派生——仍按护栏校验，
        # 配置错误在请求面显式拒绝而不是静默打到非预期目标。
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if not (
            parts.scheme in ("http", "https")
            and bool(host)
            and not parts.username
            and not parts.password
        ):
            raise PermissionError(f"出站 URL 未过护栏（拒发）: {url}")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def versions(self, path: str) -> list[dict]:
        return [{"path": path, "version": 1}]

    def forget(self, path: str) -> dict:
        return {"path": path, "forgotten": True}
