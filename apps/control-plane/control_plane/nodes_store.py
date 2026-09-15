"""节点注册表（P0 最小版 + P1 节点鉴权，spec §4.2）：engine 有则走 SQL(nodes/
node_licenses 表)、无则内存 dict（dev/tests，与 build_repository 的双模一致）。
token/license key 明文只在注册/签发响应出现一次，库内恒为 sha256。
commands 通道 P3 填充（L1/L2/L3），当前恒返回空表。

P1 节点鉴权（加固模式=BOK_AUTH_REQUIRED=1 或 BOK_CP_TOKEN 已设）：
register 必须携带有效 license_key；同一 (license_id, fingerprint) 重注册幂等
复用 node_id（换 token 防泄漏）；吊销 license=名下节点 token 即刻失效；心跳
指纹与注册指纹不符=克隆/挪机检出，该节点自动吊销。"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone

HEARTBEAT_INTERVAL_S = 60
ONLINE_WINDOW_S = HEARTBEAT_INTERVAL_S * 3


def _as_utc(dt: datetime | None) -> datetime | None:
    """SQL DateTime 列无时区（SQLite/Postgres 静默丢 tz）→ naive 值按 UTC 归一。

    库里恒存 `_utcnow()` 的 UTC 墙钟，naive 即 UTC；aware 原样透传。"""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def effective_status(last_seen_at, now, revoked: bool = False) -> str:
    if revoked:
        return "revoked"
    last_seen_at = _as_utc(last_seen_at)
    if last_seen_at is None:
        return "offline"
    return "online" if (now - last_seen_at).total_seconds() <= ONLINE_WINDOW_S else "offline"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LicenseError(Exception):
    """license 校验失败（register 闸用；message 直接进 401/403 detail）。"""

    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


def _license_public(row: dict) -> dict:
    """license 出参（永不带 key_hash；key 明文只在签发响应一次性出现）。"""
    return {
        "license_id": row["license_id"], "org_id": row["org_id"],
        "account_id": row["account_id"], "max_nodes": row["max_nodes"],
        "note": row["note"], "status": row["status"],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else "",
    }


class NodeStore:
    def __init__(self, engine) -> None:
        self._engine = engine
        self._rows: dict[str, dict] = {}  # 内存模式（engine=None）
        self._licenses: dict[str, dict] = {}  # 内存模式（engine=None）
        self._session_factory = None
        if engine is not None:
            from sqlalchemy.orm import sessionmaker

            self._session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    # ---- license 签发/查询/吊销 ----

    def create_license(self, *, org_id: str = "", account_id: str = "",
                       max_nodes: int = 1, note: str = "") -> dict:
        """签发 license：返回 _license_public + 一次性明文 key。"""
        license_id = f"lic-{secrets.token_hex(6)}"
        key = f"bokn_{secrets.token_urlsafe(24)}"
        now = _utcnow()
        row = {
            "license_id": license_id, "org_id": org_id, "account_id": account_id,
            "key_hash": _hash(key), "max_nodes": int(max_nodes), "note": note,
            "status": "active", "created_at": now,
        }
        if self._session_factory is None:
            self._licenses[license_id] = row
        else:
            from bok_voice_business_db import models

            with self._session_factory() as session:
                session.add(models.NodeLicense(
                    id=license_id, org_id=org_id, account_id=account_id,
                    key_hash=row["key_hash"], max_nodes=int(max_nodes), note=note,
                    status="active", created_at=now,
                ))
                session.commit()
        out = _license_public(row)
        out["license_key"] = key
        return out

    def list_licenses(self) -> list[dict]:
        if self._session_factory is None:
            rows = list(self._licenses.values())
        else:
            from bok_voice_business_db import models

            with self._session_factory() as session:
                rows = [
                    {"license_id": l.id, "org_id": l.org_id, "account_id": l.account_id,
                     "max_nodes": l.max_nodes, "note": l.note, "status": l.status,
                     "created_at": l.created_at}
                    for l in session.query(models.NodeLicense).all()
                ]
        return [_license_public(r) for r in sorted(rows, key=lambda r: r["license_id"])]

    def find_license(self, license_key: str) -> dict | None:
        """按明文 key 查 license（含 status/max_nodes/占用数，注册闸用）。"""
        if not license_key:
            return None
        key_hash = _hash(license_key)
        if self._session_factory is None:
            row = next((r for r in self._licenses.values() if r["key_hash"] == key_hash), None)
            if row is None:
                return None
            return {**row, "nodes_used": self._count_nodes(row["license_id"])}
        from bok_voice_business_db import models

        with self._session_factory() as session:
            lic = session.query(models.NodeLicense).filter(
                models.NodeLicense.key_hash == key_hash).first()
            if lic is None:
                return None
            used = session.query(models.Node).filter(
                models.Node.license_id == lic.id,
                models.Node.status != "revoked",
            ).count()
            return {"license_id": lic.id, "org_id": lic.org_id, "account_id": lic.account_id,
                    "key_hash": lic.key_hash, "max_nodes": lic.max_nodes, "note": lic.note,
                    "status": lic.status, "created_at": lic.created_at, "nodes_used": used}

    def revoke_license(self, license_id: str) -> dict | None:
        """吊销 license 及其名下全部节点（未找到返回 None）。"""
        if self._session_factory is None:
            row = self._licenses.get(license_id)
            if row is None:
                return None
            row["status"] = "revoked"
            revoked_nodes = 0
            for n in self._rows.values():
                if n.get("license_id") == license_id and not n["revoked"]:
                    n["revoked"] = True
                    revoked_nodes += 1
            out = _license_public(row)
            out["nodes_revoked"] = revoked_nodes
            return out
        from sqlalchemy import update

        from bok_voice_business_db import models

        with self._session_factory() as session:
            lic = session.get(models.NodeLicense, license_id)
            if lic is None:
                return None
            lic.status = "revoked"
            result = session.execute(
                update(models.Node)
                .where(models.Node.license_id == license_id,
                       models.Node.status != "revoked")
                .values(status="revoked")
            )
            session.commit()
            out = {"license_id": lic.id, "org_id": lic.org_id, "account_id": lic.account_id,
                   "max_nodes": lic.max_nodes, "note": lic.note, "status": lic.status,
                   "created_at": lic.created_at}
            out["nodes_revoked"] = result.rowcount
            return out

    def _count_nodes(self, license_id: str) -> int:
        """内存模式配额占用（未吊销节点数）。"""
        return sum(1 for n in self._rows.values()
                   if n.get("license_id") == license_id and not n["revoked"])

    def find_node_by_fingerprint(self, license_id: str, fingerprint: str) -> str | None:
        """幂等重注册：同 license 同指纹 → 既有 node_id（换 token 复用身份）。"""
        if not fingerprint:
            return None
        # 不排除 revoked：克隆检测自动吊销后，原机指纹重注册复活同一 node_id
        # （换新 token）——被吊销身份的恢复路径必须存在，机器重装不至于累积死行。
        if self._session_factory is None:
            for node_id, row in self._rows.items():
                if (row.get("license_id") == license_id
                        and row.get("fingerprint") == fingerprint):
                    return node_id
            return None
        from bok_voice_business_db import models

        with self._session_factory() as session:
            row = session.query(models.Node).filter(
                models.Node.license_id == license_id,
                models.Node.fingerprint == fingerprint,
            ).first()
            return row.id if row else None

    def validate_license_for_register(self, license_key: str, fingerprint: str) -> dict:
        """注册闸三查：key 有效/状态 active/配额未超（同指纹重注册不吃配额）。

        抛 LicenseError(401/403)；通过返回 license dict（含 license_id/org_id）。
        """
        lic = self.find_license(license_key)
        if lic is None:
            raise LicenseError(401, "unknown or missing license key")
        if lic["status"] != "active":
            raise LicenseError(401, "license revoked")
        existing = self.find_node_by_fingerprint(lic["license_id"], fingerprint or " ")
        if existing is None and lic["nodes_used"] >= lic["max_nodes"]:
            raise LicenseError(
                403, f"license quota exhausted ({lic['nodes_used']}/{lic['max_nodes']})")
        return lic

    # ---- 注册/心跳 ----

    def register(self, name: str, platform: str, org_id: str = "", version: str = "",
                 license_id: str = "", fingerprint: str = "") -> tuple[str, str]:
        """签发 node_token；带 license_id 时同指纹幂等复用 node_id。"""
        reuse_id = (self.find_node_by_fingerprint(license_id, fingerprint)
                    if license_id and fingerprint else None)
        node_id = reuse_id or f"node-{secrets.token_hex(6)}"
        token = secrets.token_urlsafe(32)
        row = {
            "node_id": node_id, "org_id": org_id, "name": name, "platform": platform,
            "version": version, "token_hash": _hash(token), "metrics_json": "{}",
            "last_seen_at": _utcnow(), "revoked": False,
            "license_id": license_id, "fingerprint": fingerprint,
        }
        if self._session_factory is None:
            self._rows[node_id] = row
        else:
            from bok_voice_business_db import models

            with self._session_factory() as session:
                if reuse_id:
                    # 幂等重注册=换 token（旧 token 随覆盖失效，防泄漏长期复用）。
                    existing = session.get(models.Node, reuse_id)
                    if existing is not None:
                        existing.token_hash = row["token_hash"]
                        existing.last_seen_at = row["last_seen_at"]
                        existing.status = "online"
                        existing.name = name or existing.name
                        existing.platform = platform or existing.platform
                        existing.version = version or existing.version
                        session.commit()
                        return node_id, token
                session.add(models.Node(
                    id=node_id, org_id=org_id, name=name, platform=platform,
                    version=version, token_hash=row["token_hash"], status="online",
                    last_seen_at=row["last_seen_at"],
                    license_id=license_id, fingerprint=fingerprint,
                ))
                session.commit()
        return node_id, token

    def _get_node_for_token(self, token_hash: str) -> dict | None:
        """按 token_hash 取节点行（双模；返回含 license_id/fingerprint/revoked）。"""
        if self._session_factory is None:
            for row in self._rows.values():
                if row["token_hash"] == token_hash:
                    return dict(row)
            return None
        from bok_voice_business_db import models

        with self._session_factory() as session:
            n = session.query(models.Node).filter(
                models.Node.token_hash == token_hash).first()
            if n is None:
                return None
            return {"node_id": n.id, "token_hash": n.token_hash,
                    "revoked": n.status == "revoked", "status": n.status,
                    "license_id": n.license_id or "", "fingerprint": n.fingerprint or ""}

    def _revoke_node(self, node_id: str) -> None:
        if self._session_factory is None:
            if node_id in self._rows:
                self._rows[node_id]["revoked"] = True
            return
        from sqlalchemy import update

        from bok_voice_business_db import models

        with self._session_factory() as session:
            session.execute(
                update(models.Node).where(models.Node.id == node_id)
                .values(status="revoked"))
            session.commit()

    def heartbeat(self, token: str, metrics: dict | None = None,
                  fingerprint: str = "") -> tuple[bool, str]:
        """心跳三验：token 有效 / 所属 license 仍 active / 指纹（若上报）与注册一致。

        返回 (ok, reason)；reason ∈ {"", "unknown_token", "revoked",
        "license_revoked", "fingerprint_mismatch"}——后两者节点被自动吊销，
        供 CP 侧审计克隆/挪机与吊销面。
        """
        token_hash = _hash(token)
        node = self._get_node_for_token(token_hash)
        if node is None:
            return False, "unknown_token"
        # license 先查：license 吊销会把节点也置 revoked，先查 license 才能
        # 报出更具体的 license_revoked（而非笼统 revoked）。
        if node.get("license_id"):
            lic = None
            if self._session_factory is None:
                lic = self._licenses.get(node["license_id"])
            else:
                from bok_voice_business_db import models

                with self._session_factory() as session:
                    row = session.get(models.NodeLicense, node["license_id"])
                    lic = {"status": row.status} if row else None
            if lic is None or lic["status"] != "active":
                self._revoke_node(node["node_id"])
                return False, "license_revoked"
        if node["revoked"] or node.get("status") == "revoked":
            return False, "revoked"
        if (node.get("fingerprint") and fingerprint
                and fingerprint != node["fingerprint"]):
            # 克隆/挪机：token 被另一台机器持有——自动吊销该 token（原机指纹重注册
            # 幂等复用 node_id 换新 token，恢复路径存在）。
            self._revoke_node(node["node_id"])
            return False, "fingerprint_mismatch"
        if self._session_factory is None:
            row = self._rows.get(node["node_id"])
            if row is not None:
                row["last_seen_at"] = _utcnow()
                row["metrics_json"] = json.dumps(metrics or {})
        else:
            from sqlalchemy import update

            from bok_voice_business_db import models

            with self._session_factory() as session:
                session.execute(
                    update(models.Node)
                    .where(models.Node.token_hash == token_hash,
                           models.Node.status != "revoked")
                    .values(last_seen_at=_utcnow(), metrics_json=json.dumps(metrics or {}))
                )
                session.commit()
        return True, ""

    def list_nodes(self) -> list[dict]:
        now = _utcnow()
        if self._session_factory is None:
            rows = list(self._rows.values())
        else:
            from bok_voice_business_db import models

            with self._session_factory() as session:
                rows = [
                    {"node_id": n.id, "org_id": n.org_id, "name": n.name, "platform": n.platform,
                     "version": n.version, "revoked": n.status == "revoked",
                     "last_seen_at": n.last_seen_at,
                     "license_id": n.license_id or "",
                     "fingerprint": (n.fingerprint or "")[:12]}
                    for n in session.query(models.Node).all()
                ]
        out = []
        for r in rows:
            last_seen = _as_utc(r.get("last_seen_at"))  # 双模 isoformat 同形（aware UTC）
            out.append({
                "node_id": r["node_id"], "org_id": r["org_id"], "name": r["name"],
                "platform": r["platform"], "version": r["version"],
                "status": effective_status(last_seen, now, r.get("revoked", False)),
                "last_seen_at": last_seen.isoformat() if last_seen else None,
                "license_id": r.get("license_id", ""),
                # 指纹只出前 12 位 hex（可辨识、不可还原完整机器标识）。
                "fingerprint_prefix": (r.get("fingerprint") or "")[:12],
            })
        return out
