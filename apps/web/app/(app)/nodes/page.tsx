"use client";

// 节点管理（site-delivery M1 kill-switch UI）：root 专属平台面——节点注册表清单 +
// 吊销 / 解除吊销。吊销=粘性停栈指令（token 即刻失效、节点 agent 收到 shutdown 停栈），
// 唯一恢复路径是「解除吊销」后重新注册换发 token。结构对齐员工管理页（users/page.tsx）。

import { useCallback, useEffect, useState } from "react";
import { api, type NodeRow } from "@/lib/api";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { useSession } from "@/components/session-context";
import { friendlyErrorText } from "@/lib/api-ready";

const STATUS_LABEL: Record<string, string> = { online: "在线", offline: "离线", revoked: "已吊销" };
const STATUS_BADGE: Record<string, string> = {
  online: "bg-emerald-400/15 text-emerald-300",
  offline: "bg-white/10 muted",
  revoked: "bg-red-500/15 text-red-300",
};

/** 吊销来源：root=人工停栈（粘性）；auto_clone=克隆/挪机自动吊销（重注册可复活）。 */
function revokedSourceLabel(row: NodeRow): string {
  if (row.revoked_source === "root") return "root 吊销";
  if (row.revoked_source === "auto_clone") return "克隆检出";
  return "";
}

/** last_seen_at（ISO UTC）→ 相对时间；无记录显示 —。 */
function relativeTime(iso?: string | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const s = Math.floor((Date.now() - t) / 1000);
  if (s < 0) return "刚刚";
  if (s < 60) return `${s} 秒前`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分钟前`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时前`;
  return `${Math.floor(h / 24)} 天前`;
}

/** 操作错误的中文映射：409/404 是本页动作的预期冲突，其余交给通用友好文案。 */
function nodeActionError(raw: string): string {
  const text = String(raw ?? "");
  if (/^409\b/.test(text.trim())) return "该节点当前未处于吊销状态，无需解除。";
  if (/^404\b/.test(text.trim())) return "节点不存在（可能已被清理），请刷新列表。";
  return friendlyErrorText(text);
}

export default function NodesPage() {
  const session = useSession();
  const [rows, setRows] = useState<NodeRow[] | null>(null);
  const [err, setErr] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");

  /** 平台面：仅登录 root 可用（匿名本地会话不算 root）。 */
  const isRoot = Boolean(session && !session.anonymous && session.role === "root");

  const refresh = useCallback(async () => {
    try {
      const raw = (await api.listNodes()) as unknown;
      // 兼容裸数组与 {nodes:[...]} 包裹两种响应形态（后端当前返回裸数组）。
      const list = Array.isArray(raw)
        ? (raw as NodeRow[])
        : Array.isArray((raw as { nodes?: NodeRow[] })?.nodes)
          ? (raw as { nodes: NodeRow[] }).nodes
          : [];
      setRows(list);
      setErr("");
    } catch (e) {
      setErr(nodeActionError(String(e)));
      setRows((prev) => prev ?? []);
    }
  }, []);

  useEffect(() => {
    if (!isRoot) return;
    void refresh();
  }, [isRoot, refresh]);

  async function revoke(row: NodeRow) {
    const label = row.name || row.node_id;
    if (
      !window.confirm(
        `确认吊销节点「${label}」？该节点的令牌即刻失效，节点上的服务将停栈退出；吊销为粘性状态（重新注册不会自动复活），需「解除吊销」并重新注册才能恢复。`,
      )
    ) {
      return;
    }
    setBusy(row.node_id);
    setErr("");
    setNotice("");
    try {
      await api.revokeNode(row.node_id);
      setNotice(`已吊销「${label}」，节点令牌即刻失效。`);
      await refresh();
    } catch (e) {
      setErr(nodeActionError(String(e)));
    } finally {
      setBusy("");
    }
  }

  async function unrevoke(row: NodeRow) {
    const label = row.name || row.node_id;
    setBusy(row.node_id);
    setErr("");
    setNotice("");
    try {
      await api.unrevokeNode(row.node_id);
      setNotice(`已解除「${label}」的吊销；节点重新注册换发令牌后将回到在线。`);
      await refresh();
    } catch (e) {
      setErr(nodeActionError(String(e)));
    } finally {
      setBusy("");
    }
  }

  if (!session) return <LoadingState label="正在读取会话…" />;
  if (!isRoot) {
    return (
      <div className="card space-y-2">
        <span className="label">无权限</span>
        <p className="text-sm">节点管理仅超级管理员（root）可用，请使用 root 账号登录。</p>
        <p className="text-xs muted">节点吊销是停栈指令（kill-switch），管理员与话务员不可见。</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h1 className="page-title">节点管理</h1>
          <p className="page-sub">节点注册表 · 吊销 / 解除吊销（root 专属停栈开关）</p>
        </div>
        <button className="btn-ghost text-xs" disabled={rows === null} onClick={() => void refresh()}>
          刷新
        </button>
      </div>

      {err && <ErrorState message={err} />}
      {notice && <p className="text-sm text-emerald-400">{notice}</p>}

      <section className="card">
        {rows === null ? (
          <LoadingState />
        ) : rows.length === 0 ? (
          <EmptyState label="暂无注册节点；节点 agent 首次注册后出现在这里。" />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left muted">
                <th className="py-1">节点</th>
                <th>平台</th>
                <th>状态</th>
                <th>许可证</th>
                <th>最近心跳</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((n) => {
                const rowBusy = busy === n.node_id;
                const revoked = n.status === "revoked";
                const source = revokedSourceLabel(n);
                const lid = n.license_id || "";
                return (
                  <tr key={n.node_id} className="border-t border-(--card-border) align-top">
                    <td className="py-1.5">
                      <div>{n.name || "—"}</div>
                      <div className="font-mono text-[11px] muted">{n.node_id}</div>
                    </td>
                    <td className="text-xs">
                      {n.platform || "—"}
                      {n.version ? <span className="muted"> · {n.version}</span> : null}
                    </td>
                    <td>
                      <span
                        className={`rounded-sm px-1.5 py-0.5 text-[10px] ${STATUS_BADGE[n.status] ?? "bg-white/10 muted"}`}
                      >
                        {STATUS_LABEL[n.status] ?? n.status}
                      </span>
                      {source && <div className="mt-0.5 text-[11px] muted">{source}</div>}
                    </td>
                    <td className="font-mono text-xs" title={lid}>
                      {lid ? (lid.length > 10 ? `${lid.slice(0, 10)}…` : lid) : "—"}
                    </td>
                    <td className="whitespace-nowrap text-xs muted" title={n.last_seen_at ?? ""}>
                      {relativeTime(n.last_seen_at)}
                    </td>
                    <td className="whitespace-nowrap">
                      {revoked ? (
                        <button className="btn-ghost text-xs" disabled={rowBusy} onClick={() => void unrevoke(n)}>
                          解除吊销
                        </button>
                      ) : (
                        <button className="btn-ghost text-xs text-red-300" disabled={rowBusy} onClick={() => void revoke(n)}>
                          吊销
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      <p className="text-xs muted">
        说明：吊销为粘性停栈指令——节点令牌即刻失效，节点 agent 心跳收到 shutdown 后停栈退出，重新注册不会自动复活；
        「解除吊销」后节点须重新注册换发令牌（或心跳成功）才回到在线。克隆检出的自动吊销（auto_clone）原机重注册即可复活，不经本页。
      </p>
    </div>
  );
}
