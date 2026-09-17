"""节点注册表（P0 最小版 + P1 节点鉴权，spec §4.2）：engine 有则走 SQL(nodes/
node_licenses/node_commands 表)、无则内存 dict（dev/tests，与 build_repository
的双模一致）。token/license key 明文只在注册/签发响应出现一次，库内恒为 sha256。
commands 通道（P3，2026-09-17 落地）：root 经 CP 入队（白名单动作），节点心跳
领走（pop→delivered），update 靠心跳 version 收敛自动关单。

P1 节点鉴权（加固模式=BOK_AUTH_REQUIRED=1 或 BOK_CP_TOKEN 已设）：
register 必须携带有效 license_key；同一 (license_id, fingerprint) 重注册幂等
复用 node_id（换 token 防泄漏）；吊销 license=名下节点 token 即刻失效；心跳
指纹与注册指纹不符=克隆/挪机检出，该节点自动吊销。"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import datetime, timezone

HEARTBEAT_INTERVAL_S = 60
ONLINE_WINDOW_S = HEARTBEAT_INTERVAL_S * 3

# 指令动作白名单（CP 端点与 store 双闸——新增动作须两端同步，绝不经此通道
# 传任意 shell/命令行）。
NODE_COMMAND_ACTIONS = ("update", "restart", "shutdown")


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
        self._commands: dict[str, dict] = {}  # 内存模式（engine=None）
        self._session_factory = None
        if engine is not None:
            from sqlalchemy.orm import sessionmaker

            self._session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        self._register_lock = threading.Lock()

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
        """吊销 license 及其名下全部节点（未找到返回 None）。

        名下节点按 root 面动作吊销（source='root'）——license 重发本就须 root
        签新 key，sticky 语义与单节点 root 吊销一致。"""
        now = _utcnow().isoformat()
        if self._session_factory is None:
            row = self._licenses.get(license_id)
            if row is None:
                return None
            row["status"] = "revoked"
            revoked_nodes = 0
            for n in self._rows.values():
                if n.get("license_id") == license_id and not n["revoked"]:
                    n["revoked"] = True
                    n["revoked_source"] = "root"
                    n["revoked_at"] = now
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
                .values(status="revoked", revoked_source="root", revoked_at=now)
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

    def register_licensed(self, *, license_key: str, fingerprint: str, name: str = "",
                          platform: str = "", org_id: str = "", version: str = ""
                          ) -> tuple[dict, str, str]:
        """加固模式注册（2026-09-16 深测 P1）：license 三查 + 建行同锁收口配额
        TOCTOU——旧版 validate 与 register 分属两事务，max_nodes=1 并发 12 实测
        10 个全过闸落库。进程内由 _register_lock 串行；多实例部署由 deps 幂等
        段的 (license_id, fingerprint) 部分唯一索引兜底（撞索 → 403）。
        返回 (license 行, node_id, 明文 token)；失败抛 LicenseError。"""
        with self._register_lock:
            lic = self.find_license(license_key)
            if lic is None:
                raise LicenseError(401, "unknown or missing license key")
            if lic["status"] != "active":
                raise LicenseError(401, "license revoked")
            reuse_id = self.find_node_by_fingerprint(lic["license_id"], fingerprint)
            if reuse_id is None and lic["nodes_used"] >= lic["max_nodes"]:
                raise LicenseError(
                    403, f"license quota exhausted ({lic['nodes_used']}/{lic['max_nodes']})")
            try:
                node_id, token = self.register(
                    name=name, platform=platform, org_id=org_id, version=version,
                    license_id=lic["license_id"], fingerprint=fingerprint)
            except Exception as exc:
                # 多实例并发撞 (license_id, fingerprint) 唯一索引 → 按配额语义 403。
                if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                    raise LicenseError(403, "duplicate node registration") from exc
                raise
            return lic, node_id, token

    # ---- 注册/心跳 ----

    def register(self, name: str, platform: str, org_id: str = "", version: str = "",
                 license_id: str = "", fingerprint: str = "") -> tuple[str, str]:
        """签发 node_token；带 license_id 时同指纹幂等复用 node_id。

        sticky（site-delivery M1）：复用候选是 **root 吊销**（revoked_source='root'）
        的行时抛 LicenseError(401, "node revoked")——不复活、不换发 token、不新建
        行（配额零消耗）；auto_clone/未标记行照旧复活（克隆检出恢复路径）。"""
        reuse_id = (self.find_node_by_fingerprint(license_id, fingerprint)
                    if license_id and fingerprint else None)
        if reuse_id:
            existing = self.node_by_id(reuse_id)
            if existing is not None and existing.get("revoked") and (
                    existing.get("revoked_source") or "") == "root":
                raise LicenseError(401, "node revoked")
        node_id = reuse_id or f"node-{secrets.token_hex(6)}"
        token = secrets.token_urlsafe(32)
        row = {
            "node_id": node_id, "org_id": org_id, "name": name, "platform": platform,
            "version": version, "token_hash": _hash(token), "metrics_json": "{}",
            "last_seen_at": _utcnow(), "revoked": False,
            "revoked_source": "", "revoked_at": "",
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
                        # 复活清来源标记（revoked_at 保留作历史）——auto_clone 行
                        # 复活后不得残留误导性的吊销来源。
                        existing.revoked_source = ""
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
                    "revoked_source": n.revoked_source or "",
                    "revoked_at": n.revoked_at or "",
                    "license_id": n.license_id or "", "fingerprint": n.fingerprint or ""}

    def resolve_node_token(self, token: str) -> dict | None:
        """按明文 node_token 解节点行（sha256 查找；窒息点/心跳 401 detail 用）。

        空 token / 非 node_token 凭据（用户 JWT、CP token）→ None——调用方必须
        容忍缺位直通，不得把 None 当拒绝依据。"""
        if not token:
            return None
        return self._get_node_for_token(_hash(token))

    def node_by_id(self, node_id: str) -> dict | None:
        """按 node_id 取节点行（建单绑定校验 / register 复用路径 sticky 判定用）。

        双模同形出参：revoked（bool）/ status（str）/ revoked_source / revoked_at。"""
        if not node_id:
            return None
        if self._session_factory is None:
            row = self._rows.get(node_id)
            if row is None:
                return None
            return {"node_id": node_id, "revoked": bool(row["revoked"]),
                    "status": "revoked" if row["revoked"] else "online",
                    "revoked_source": row.get("revoked_source", ""),
                    "revoked_at": row.get("revoked_at", ""),
                    "license_id": row.get("license_id", ""),
                    "fingerprint": row.get("fingerprint", "")}
        from bok_voice_business_db import models

        with self._session_factory() as session:
            n = session.get(models.Node, node_id)
            if n is None:
                return None
            return {"node_id": n.id, "revoked": n.status == "revoked",
                    "status": n.status, "revoked_source": n.revoked_source or "",
                    "revoked_at": n.revoked_at or "",
                    "license_id": n.license_id or "", "fingerprint": n.fingerprint or ""}

    def revoke_node(self, node_id: str, *, source: str = "root") -> bool:
        """吊销单节点并打来源标记；行存在且未吊销返回 True（root 面
        /api/nodes/{id}/revoke 用 source="root"，心跳克隆检出用 "auto_clone"）。

        source 决定 sticky 语义：'root'=注册端点不得复活（须 /unrevoke），
        'auto_clone'/''=原机指纹重注册仍可复活（克隆检出恢复路径）。"""
        now = _utcnow().isoformat()
        if self._session_factory is None:
            row = self._rows.get(node_id)
            if row is None or row["revoked"]:
                return False
            row["revoked"] = True
            row["revoked_source"] = source
            row["revoked_at"] = now
            return True
        from sqlalchemy import update

        from bok_voice_business_db import models

        with self._session_factory() as session:
            result = session.execute(
                update(models.Node).where(models.Node.id == node_id,
                                           models.Node.status != "revoked")
                .values(status="revoked", revoked_source=source, revoked_at=now))
            session.commit()
            return result.rowcount > 0

    def unrevoke_node(self, node_id: str) -> str | None:
        """解除节点吊销（root 面 /api/nodes/{id}/unrevoke 用；sticky 的唯一恢复路径）。

        返回 "unrevoked"（已解除：status→offline、revoked_source→''，revoked_at
        保留作历史）/ "live"（节点本就未吊销——调用方语义 409）/ None（节点不存在）。
        解除后节点须重注册（换发 token）或心跳成功才回到 online。"""
        if self._session_factory is None:
            row = self._rows.get(node_id)
            if row is None:
                return None
            if not row["revoked"]:
                return "live"
            row["revoked"] = False
            row["revoked_source"] = ""
            return "unrevoked"
        from bok_voice_business_db import models

        with self._session_factory() as session:
            n = session.get(models.Node, node_id)
            if n is None:
                return None
            if n.status != "revoked":
                return "live"
            n.status = "offline"
            n.revoked_source = ""
            session.commit()
            return "unrevoked"

    def heartbeat(self, token: str, metrics: dict | None = None,
                  fingerprint: str = "", require_license: bool = False,
                  version: str = "") -> tuple[bool, str]:
        """心跳四验：token / license active /（加固模式）license 绑定 / 指纹。

        require_license=True（CP 加固模式）：开放期注册的 license_id="" 存量 token
        是不可吊销的长命凭证（吊销端点够不着它）——拒绝但不自动吊销，留现场供
        root 处置/迁移（2026-09-16 深测 P2）。注册绑定了指纹的节点心跳缺指纹=
        不合作客户端绕过克隆检测——按指纹不符自动吊销（协议强制）。

        version（P3 commands，2026-09-17）：节点上报当前包版本——写回 Node.version
        （舰队版本面板），并自动关闭该节点 target_version==version 的 update 指令
        （收敛即完成，无需 ack 协议；重启/换版本间隙的心跳不误关——版本不匹配
        的指令保持 delivered）。

        返回 (ok, reason)；reason ∈ {"", "unknown_token", "revoked",
        "license_revoked", "license_required", "fingerprint_mismatch"}——后两者
        （license_revoked/fingerprint_mismatch）节点被自动吊销，供 CP 侧审计
        克隆/挪机与吊销面。auto_clone 吊销来源行（revoked_source）随行持久化：
        root 吊销=sticky（注册不得复活、心跳 401 附 shutdown 指令，见 main），
        auto_clone=原机重注册复活保留。
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
                self.revoke_node(node["node_id"])
                return False, "license_revoked"
        if node["revoked"] or node.get("status") == "revoked":
            return False, "revoked"
        if require_license and not node.get("license_id"):
            return False, "license_required"
        if node.get("fingerprint") and not fingerprint:
            # 协议强制（深测 P2）：指纹检测是「客户端自愿」时对不合作实现无效。
            self.revoke_node(node["node_id"], source="auto_clone")
            return False, "fingerprint_mismatch"
        if (node.get("fingerprint") and fingerprint
                and fingerprint != node["fingerprint"]):
            # 克隆/挪机：token 被另一台机器持有——自动吊销该 token（原机指纹重注册
            # 幂等复用 node_id 换新 token，恢复路径存在；auto_clone 非 sticky）。
            self.revoke_node(node["node_id"], source="auto_clone")
            return False, "fingerprint_mismatch"
        if self._session_factory is None:
            row = self._rows.get(node["node_id"])
            if row is not None:
                row["last_seen_at"] = _utcnow()
                row["metrics_json"] = json.dumps(metrics or {})
                if version:
                    row["version"] = version
        else:
            from bok_voice_business_db import models

            # ORM 单元工作（属性赋值+commit，无 update() 构建——心跳每分钟
            # 一发，行级读取更新成本可忽略）。
            with self._session_factory() as session:
                n = session.query(models.Node).filter(
                    models.Node.token_hash == token_hash,
                    models.Node.status != "revoked").first()
                if n is not None:
                    n.last_seen_at = _utcnow()
                    n.metrics_json = json.dumps(metrics or {})
                    if version:
                        n.version = version
                session.commit()
        if version:
            # update 收敛关单：只关 target_version 精确匹配的（重启/换版本间隙
            # 的其他 update 指令保持 delivered，等各自目标版本到达再关）。
            self._close_update_commands_by_version(node["node_id"], version)
        return True, ""

    def _close_update_commands_by_version(self, node_id: str, version: str) -> int:
        """节点心跳 version 与目标版本收敛 → 对应 update 指令关单（done）。

        版本匹配在 Python 侧做（ORM 取行后逐行比 target_version）——精确、
        且不把运行期值带进任何语句构建位。"""
        now = _utcnow()
        if self._session_factory is None:
            n = 0
            for c in self._commands.values():
                if (c["node_id"] == node_id and c["action"] == "update"
                        and c["status"] in ("pending", "delivered")
                        and c["target_version"] == version):
                    c["status"] = "done"
                    c["result"] = "node version converged"
                    c["closed_at"] = now
                    n += 1
            return n
        from bok_voice_business_db import models

        with self._session_factory() as session:
            rows = session.query(models.NodeCommand).filter(
                models.NodeCommand.node_id == node_id,
                models.NodeCommand.action == "update",
                models.NodeCommand.status.in_(("pending", "delivered"))).all()
            changed = 0
            for r in rows:
                if r.target_version == version:
                    r.status = "done"
                    r.result = "node version converged"
                    r.closed_at = now
                    changed += 1
            if changed:
                session.commit()
            return changed

    # ---- commands 通道（P3，2026-09-17）----

    def enqueue_command(self, node_id: str, action: str, *,
                        args: dict | None = None, created_by: str = "") -> dict | None:
        """入队一条指令（root 面 CP 端点用）。动作白名单外抛 ValueError——
        端点层 400，绝不把未审计动作放进通道。update 必带 target_version。"""
        if action not in NODE_COMMAND_ACTIONS:
            raise ValueError(f"unsupported action: {action}")
        args = dict(args or {})
        target_version = str(args.get("version", ""))
        if action == "update" and not target_version:
            raise ValueError("update command requires version")
        cmd_id = f"cmd-{secrets.token_hex(6)}"
        now = _utcnow()
        row = {
            "id": cmd_id, "node_id": node_id, "action": action,
            "target_version": target_version, "args_json": json.dumps(args),
            "status": "pending", "result": "", "created_by": created_by,
            "created_at": now, "delivered_at": None, "closed_at": None,
        }
        if self._session_factory is None:
            self._commands[cmd_id] = row
        else:
            from bok_voice_business_db import models

            with self._session_factory() as session:
                session.add(models.NodeCommand(
                    id=cmd_id, node_id=node_id, action=action,
                    target_version=target_version, args_json=row["args_json"],
                    status="pending", created_by=created_by, created_at=now,
                ))
                session.commit()
        return self._command_public(row)

    def _command_public(self, row: dict) -> dict:
        created = _as_utc(row.get("created_at"))
        delivered = _as_utc(row.get("delivered_at"))
        closed = _as_utc(row.get("closed_at"))
        try:
            args = json.loads(row.get("args_json") or "{}")
        except (TypeError, ValueError):
            args = {}
        return {
            "id": row["id"], "node_id": row["node_id"], "action": row["action"],
            "target_version": row.get("target_version", ""), "args": args,
            "status": row["status"], "result": row.get("result", ""),
            "created_by": row.get("created_by", ""),
            "created_at": created.isoformat() if created else None,
            "delivered_at": delivered.isoformat() if delivered else None,
            "closed_at": closed.isoformat() if closed else None,
        }

    def pop_commands(self, node_id: str, limit: int = 8) -> list[dict]:
        """心跳领指令：pending → delivered（delivered_at 落时刻），wire 形返回。

        双发竞态窗（同一节点并发心跳）结构性不存在——node_agent 单循环单飞；
        即便撞上，动作幂等（restart/update 收敛、shutdown 终态）。"""
        now = _utcnow()
        if self._session_factory is None:
            pending = sorted(
                (c for c in self._commands.values()
                 if c["node_id"] == node_id and c["status"] == "pending"),
                key=lambda c: c["created_at"])[:limit]
            for c in pending:
                c["status"] = "delivered"
                c["delivered_at"] = now
            return [{"id": c["id"], "action": c["action"],
                     "args": json.loads(c["args_json"])} for c in pending]
        from bok_voice_business_db import models

        with self._session_factory() as session:
            rows = (session.query(models.NodeCommand)
                    .filter(models.NodeCommand.node_id == node_id,
                            models.NodeCommand.status == "pending")
                    .order_by(models.NodeCommand.created_at)
                    .limit(limit).all())
            out = []
            for r in rows:
                r.status = "delivered"
                r.delivered_at = now
                try:
                    args = json.loads(r.args_json or "{}")
                except (TypeError, ValueError):
                    args = {}
                out.append({"id": r.id, "action": r.action, "args": args})
            session.commit()
            return out

    def ack_command(self, node_id: str, cmd_id: str, ok: bool, result: str = "") -> bool:
        """节点 ack 单条指令（update 失败回执用；成功路径走 version 收敛关单）。"""
        now = _utcnow()
        target = "done" if ok else "failed"
        if self._session_factory is None:
            c = self._commands.get(cmd_id)
            if c is None or c["node_id"] != node_id or c["status"] not in ("pending", "delivered"):
                return False
            c["status"] = target
            c["result"] = result[:255]
            c["closed_at"] = now
            return True
        from bok_voice_business_db import models

        with self._session_factory() as session:
            c = session.get(models.NodeCommand, cmd_id)
            if c is None or c.node_id != node_id or c.status not in ("pending", "delivered"):
                return False
            c.status = target
            c.result = result[:255]
            c.closed_at = now
            session.commit()
            return True

    def pending_command_count(self, node_id: str) -> int:
        if self._session_factory is None:
            return sum(1 for c in self._commands.values()
                       if c["node_id"] == node_id and c["status"] == "pending")
        from bok_voice_business_db import models

        with self._session_factory() as session:
            return session.query(models.NodeCommand).filter(
                models.NodeCommand.node_id == node_id,
                models.NodeCommand.status == "pending").count()

    def list_commands(self, node_id: str | None = None, limit: int = 50) -> list[dict]:
        """指令台账（root 面排障用；node_id 空=全舰队）。"""
        if self._session_factory is None:
            rows = [c for c in self._commands.values()
                    if node_id is None or c["node_id"] == node_id]
        else:
            from bok_voice_business_db import models

            with self._session_factory() as session:
                q = session.query(models.NodeCommand)
                if node_id:
                    q = q.filter(models.NodeCommand.node_id == node_id)
                rows = [
                    {"id": c.id, "node_id": c.node_id, "action": c.action,
                     "target_version": c.target_version, "args_json": c.args_json,
                     "status": c.status, "result": c.result, "created_by": c.created_by,
                     "created_at": c.created_at, "delivered_at": c.delivered_at,
                     "closed_at": c.closed_at}
                    for c in q.order_by(models.NodeCommand.created_at.desc())
                    .limit(limit).all()
                ]
        rows = sorted(rows, key=lambda c: c["created_at"], reverse=True)[:limit]
        return [self._command_public(c) for c in rows]

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
                     "revoked_source": n.revoked_source or "",
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
                "revoked_source": r.get("revoked_source", ""),
                "last_seen_at": last_seen.isoformat() if last_seen else None,
                "license_id": r.get("license_id", ""),
                # 指纹只出前 12 位 hex（可辨识、不可还原完整机器标识）。
                "fingerprint_prefix": (r.get("fingerprint") or "")[:12],
            })
        return out
