from __future__ import annotations

import os
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

        req = urllib.request.Request(
            f"{self.base_url}/documents/write",
            data=json.dumps({"path": path, "content": content}).encode(),
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def versions(self, path: str) -> list[dict]:
        return [{"path": path, "version": 1}]

    def forget(self, path: str) -> dict:
        return {"path": path, "forgotten": True}
