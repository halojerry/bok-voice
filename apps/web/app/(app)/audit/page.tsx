"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";

type AuditRow = {
  id: string;
  ts: string;
  action: string;
  subject_type: string;
  subject_id: string;
  actor: string;
  outcome: string;
  request_id: string;
  call_id: string;
  account_id: string;
  object_id: string;
  detail: Record<string, unknown>;
};

const ACTIONS = [
  "",
  "voice.clone",
  "settle.create",
  "template.create",
  "template.update",
  "template.delete",
  "object.create",
  "object.update",
  "object.delete",
  "persona.create",
  "persona.update",
  "persona.delete",
  "knowledge.import",
  "settings.save",
];

export default function AuditPage() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [action, setAction] = useState("");
  const [accountId, setAccountId] = useState("");
  const [callId, setCallId] = useState("");

  const load = useMemo(
    () => async () => {
      setLoading(true);
      setError("");
      try {
        setRows((await api.listAudit(accountId, action, callId)) as AuditRow[]);
      } catch (e) {
        setError(String(e));
      } finally {
        setLoading(false);
      }
    },
    [action, accountId, callId],
  );

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="page-title">审计日志</h1>
        <p className="page-sub">可追溯、可审计的业务操作记录（语音克隆/结算/话术/对象/人设/设置/知识导入）。</p>
      </div>

      <div className="flex flex-wrap items-end gap-3 rounded-xl border border-[var(--card-border)] bg-[var(--card)] p-4">
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          动作
          <select className="rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm" value={action} onChange={(e) => setAction(e.target.value)}>
            {ACTIONS.map((a) => <option key={a} value={a}>{a || "全部"}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          账号
          <input className="rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm" placeholder="acc-001" value={accountId} onChange={(e) => setAccountId(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          通话
          <input className="rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm" placeholder="call-xxx" value={callId} onChange={(e) => setCallId(e.target.value)} />
        </label>
        <button className="rounded-lg border border-[var(--card-border)] px-4 py-2 text-sm hover:border-[var(--accent)]" onClick={() => load()}>
          查询
        </button>
      </div>

      {loading ? <LoadingState label="加载审计记录…" /> : error ? <ErrorState message={error} /> : rows.length === 0 ? <EmptyState label="暂无审计记录" /> : (
        <div className="overflow-x-auto rounded-xl border border-[var(--card-border)]">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-[var(--card-border)] text-xs text-muted-foreground">
              <tr>
                <th className="px-4 py-2">时间</th>
                <th className="px-4 py-2">动作</th>
                <th className="px-4 py-2">对象</th>
                <th className="px-4 py-2">账号 / 通话</th>
                <th className="px-4 py-2">操作者</th>
                <th className="px-4 py-2">结果</th>
                <th className="px-4 py-2">request_id</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r.id ?? i} className="border-b border-[var(--card-border)] last:border-0">
                  <td className="px-4 py-2 font-mono text-xs">{r.ts}</td>
                  <td className="px-4 py-2 font-mono text-xs text-[var(--accent)]">{r.action}</td>
                  <td className="px-4 py-2 text-xs">{r.subject_type}:<span className="font-mono">{r.subject_id}</span></td>
                  <td className="px-4 py-2 font-mono text-xs">{r.account_id || "—"} / {r.call_id || "—"}</td>
                  <td className="px-4 py-2 text-xs">{r.actor}</td>
                  <td className="px-4 py-2 text-xs">{r.outcome}</td>
                  <td className="px-4 py-2 font-mono text-xs text-muted-foreground">{r.request_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
