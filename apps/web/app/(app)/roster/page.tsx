"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { LoadingState, EmptyState } from "@/components/app-shell";

type RosterEntry = {
  id: string; call_id: string; object_id: string; channel: string; number: string;
  display_name: string; summary: string; status: string; claimed_by: string;
  claimed_at: string; created_at: string;
};

const STATUS_LABEL: Record<string, string> = {
  unclaimed: "待认领", claimed: "已认领", handled: "已对接",
};
const CHANNEL_LABEL: Record<string, string> = { whatsapp: "WhatsApp", wechat: "微信" };

export default function RosterPage() {
  const [rows, setRows] = useState<RosterEntry[] | null>(null);
  const [status, setStatus] = useState("");
  const [channel, setChannel] = useState("");
  const [copied, setCopied] = useState("");

  const reload = useCallback(async () => {
    setRows(null);
    try {
      setRows(await api.listRoster(status, channel) as RosterEntry[]);
    } catch {
      setRows([]);
    }
  }, [status, channel]);

  useEffect(() => { void reload(); }, [reload]);

  const act = async (fn: () => Promise<unknown>) => { await fn(); await reload(); };
  const copy = async (number: string) => {
    await navigator.clipboard.writeText(number);
    setCopied(number);
    setTimeout(() => setCopied(""), 1500);
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-medium">名册 · 认领池</h1>
        <select value={status} onChange={(e) => setStatus(e.target.value)} className="rounded border px-2 py-1 text-sm">
          <option value="">全部状态</option>
          <option value="unclaimed">待认领</option>
          <option value="claimed">已认领</option>
          <option value="handled">已对接</option>
        </select>
        <select value={channel} onChange={(e) => setChannel(e.target.value)} className="rounded border px-2 py-1 text-sm">
          <option value="">全部渠道</option>
          <option value="whatsapp">WhatsApp</option>
          <option value="wechat">微信</option>
        </select>
        <button onClick={() => void reload()} className="rounded border px-2 py-1 text-sm">刷新</button>
      </div>
      {rows === null ? <LoadingState /> : rows.length === 0 ? <EmptyState label="名册暂无条目" /> : (
        <table className="w-full text-sm">
          <thead><tr className="text-left muted">
            <th className="py-1">客户</th><th>渠道</th><th>号码</th><th>摘要</th>
            <th>状态</th><th>认领人</th><th>来源</th><th />
          </tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="border-t">
                <td className="py-1.5">{r.display_name || r.object_id || "—"}</td>
                <td>{CHANNEL_LABEL[r.channel] || r.channel}</td>
                <td className="font-mono">{r.number}</td>
                <td className="max-w-64 truncate" title={r.summary}>{r.summary || "—"}</td>
                <td>{STATUS_LABEL[r.status] || r.status}</td>
                <td>{r.status === "claimed" ? r.claimed_by : "—"}</td>
                <td>{r.call_id ? <Link className="underline" href={`/calls`}>{r.call_id}</Link> : "—"}</td>
                <td className="whitespace-nowrap">
                  <button onClick={() => void copy(r.number)} className="mr-2 rounded border px-2 py-0.5">
                    {copied === r.number ? "已复制" : "复制"}
                  </button>
                  {r.status === "unclaimed" && (
                    <button onClick={() => void act(() => api.rosterClaim(r.id))} className="mr-2 rounded border px-2 py-0.5">认领</button>
                  )}
                  {r.status === "claimed" && (
                    <>
                      <button onClick={() => void act(() => api.rosterHandled(r.id, true))} className="mr-2 rounded border px-2 py-0.5">标已对接</button>
                      <button onClick={() => void act(() => api.rosterUnclaim(r.id))} className="rounded border px-2 py-0.5">释放</button>
                    </>
                  )}
                  {r.status === "handled" && (
                    <button onClick={() => void act(() => api.rosterHandled(r.id, false))} className="rounded border px-2 py-0.5">撤销</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
