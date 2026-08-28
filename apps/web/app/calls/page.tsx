"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";

const STATUS: Record<string, [string, string]> = {
  active: ["进行中", "bg-emerald-400"],
  ringing: ["振铃", "bg-amber-400"],
  paused: ["已暂停", "bg-sky-400"],
  ended: ["已结束", "bg-neutral-500"],
  failed: ["失败", "bg-red-400"],
};

function modeLabel(mode: string) {
  return mode === "live" ? "真实业务" : "训练";
}

export default function CallsPage() {
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [objects, setObjects] = useState<Record<string, unknown>[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    Promise.all([api.listCalls("acc-001", ""), api.listObjects("acc-001")])
      .then(([c, o]) => {
        setRows(Array.isArray(c) ? c : []);
        setObjects(Array.isArray(o) ? o : []);
        setErr(null);
      })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, []);

  function objectName(id: unknown) {
    const o = objects.find((x) => String(x.id) === String(id));
    return String(o?.display_name ?? id ?? "-");
  }

  return (
    <div>
      <div className="mb-6 flex items-start justify-between">
        <div>
          <h1 className="text-xl font-semibold">通话会话</h1>
          <p className="mt-1 text-sm text-[var(--muted)]">进入历史/活跃会话，或发起新会话</p>
        </div>
        <Link href="/calls/new" className="btn-primary">
          + 新建通话
        </Link>
      </div>

      {err && <p className="mb-4 text-sm text-red-300">{err}</p>}
      {loading && <p className="text-sm text-[var(--muted)]">加载中…</p>}

      <section className="card">
        <div className="space-y-2">
          {!loading && rows.length === 0 && (
            <p className="text-sm text-[var(--muted)]">暂无会话，点击右上角「新建通话」开始。</p>
          )}
          {rows.map((c) => {
            const id = String(c.id ?? c.call_id);
            const status = String(c.status ?? "idle");
            const [label, color] = STATUS[status] ?? [status, "bg-neutral-500"];
            return (
              <Link
                key={id}
                href={`/calls/${id}`}
                className="flex items-center justify-between rounded-xl bg-white/5 px-4 py-3 transition hover:bg-white/10"
              >
                <div className="min-w-0">
                  <p className="truncate font-medium">{objectName(c.object_id)}</p>
                  <p className="mt-1 text-xs text-[var(--muted)]">
                    {modeLabel(String(c.mode ?? "simulation"))} · {String(c.language ?? "-")} ·{" "}
                    {String(c.created_at ?? "").slice(0, 19).replace("T", " ")}
                  </p>
                  <p className="mt-1 truncate text-xs text-[var(--muted)]">{id}</p>
                </div>
                <div className="flex shrink-0 items-center gap-3">
                  <span className="inline-flex items-center gap-1.5 text-xs text-[var(--muted)]">
                    <span className={`h-2 w-2 rounded-full ${color}`} />
                    {label}
                  </span>
                  <span className="text-[var(--accent)]">进入 →</span>
                </div>
              </Link>
            );
          })}
        </div>
      </section>
    </div>
  );
}
