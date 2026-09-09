"""节点注册表（P0 最小版，spec §4.2）：engine 有则走 SQL(nodes 表)、无则内存
dict（dev/tests，与 build_repository 的双模一致）。token 明文只在注册响应出现
一次，库内恒为 sha256。commands 通道 P3 填充（L1/L2/L3），P0 恒返回空表。"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

HEARTBEAT_INTERVAL_S = 60
ONLINE_WINDOW_S = HEARTBEAT_INTERVAL_S * 3


def effective_status(last_seen_at, now, revoked: bool = False) -> str:
    if revoked:
        return "revoked"
    if last_seen_at is None:
        return "offline"
    return "online" if (now - last_seen_at).total_seconds() <= ONLINE_WINDOW_S else "offline"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NodeStore:
    def __init__(self, engine) -> None:
        self._engine = engine
        self._rows: dict[str, dict] = {}  # 内存模式（engine=None）
        self._session_factory = None
        if engine is not None:
            from sqlalchemy.orm import sessionmaker

            self._session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def register(self, name: str, platform: str, org_id: str = "", version: str = "") -> tuple[str, str]:
        node_id = f"node-{secrets.token_hex(6)}"
        token = secrets.token_urlsafe(32)
        row = {
            "node_id": node_id, "org_id": org_id, "name": name, "platform": platform,
            "version": version, "token_hash": _hash(token), "metrics_json": "{}",
            "last_seen_at": _utcnow(), "revoked": False,
        }
        if self._session_factory is None:
            self._rows[node_id] = row
        else:
            from bok_voice_business_db import models

            with self._session_factory() as session:
                session.add(models.Node(
                    id=node_id, org_id=org_id, name=name, platform=platform,
                    version=version, token_hash=row["token_hash"], status="online",
                    last_seen_at=row["last_seen_at"],
                ))
                session.commit()
        return node_id, token

    def heartbeat(self, token: str, metrics: dict | None = None) -> bool:
        token_hash = _hash(token)
        import json

        if self._session_factory is None:
            for row in self._rows.values():
                if row["token_hash"] == token_hash and not row["revoked"]:
                    row["last_seen_at"] = _utcnow()
                    row["metrics_json"] = json.dumps(metrics or {})
                    return True
            return False
        from sqlalchemy import update

        from bok_voice_business_db import models

        with self._session_factory() as session:
            result = session.execute(
                update(models.Node)
                .where(models.Node.token_hash == token_hash, models.Node.status != "revoked")
                .values(last_seen_at=_utcnow(), metrics_json=json.dumps(metrics or {}))
            )
            session.commit()
            return result.rowcount > 0

    def list_nodes(self) -> list[dict]:
        now = _utcnow()
        if self._session_factory is None:
            rows = list(self._rows.values())
        else:
            from bok_voice_business_db import models

            with self._session_factory() as session:
                rows = [
                    {"node_id": n.id, "org_id": n.org_id, "name": n.name, "platform": n.platform,
                     "version": n.version, "revoked": n.status == "revoked", "last_seen_at": n.last_seen_at}
                    for n in session.query(models.Node).all()
                ]
        return [
            {
                "node_id": r["node_id"], "org_id": r["org_id"], "name": r["name"],
                "platform": r["platform"], "version": r["version"],
                "status": effective_status(r.get("last_seen_at"), now, r.get("revoked", False)),
                "last_seen_at": r["last_seen_at"].isoformat() if r.get("last_seen_at") else None,
            }
            for r in rows
        ]
