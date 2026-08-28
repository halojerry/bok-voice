"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export default function ObjectsPage() {
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [q, setQ] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState("采购商");
  const [lang, setLang] = useState("vi");
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function refresh() {
    setLoading(true);
    try {
      const data = await api.listObjects();
      setRows(Array.isArray(data) ? data : []);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function create() {
    if (!name.trim()) return;
    setErr(null);
    try {
      await api.createObject({ display_name: name, role_template: role, language: lang });
      setName("");
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  }

  const filtered = rows.filter((r) =>
    q ? String(r.display_name ?? r.name ?? "").toLowerCase().includes(q.toLowerCase()) : true,
  );

  return (
    <div>
      <div className="mb-6 flex items-start justify-between">
        <div>
          <h1 className="text-xl font-semibold">对象管理</h1>
          <p className="mt-1 text-sm text-[var(--muted)]">AI 对话对方档案 · 建档 / 表格导入 / 历史主题</p>
        </div>
        <button className="btn-primary" onClick={() => { setErr(null); setErr("表格导入待接入：可先支持 .xlsx/.csv 批量建档。"); }}>
          表格导入
        </button>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_340px]">
        <section className="card">
          <input
            className="mb-4 w-full rounded-xl border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
            placeholder="搜索对象…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          {err && <p className="mb-3 text-sm text-red-300">{err}</p>}
          {loading && <p className="text-sm text-[var(--muted)]">加载中…</p>}
          <div className="space-y-2">
            {filtered.length === 0 && !loading && <p className="text-sm text-[var(--muted)]">暂无对象。</p>}
            {filtered.map((r) => (
              <div key={String(r.id ?? r.object_id)} className="flex items-center justify-between rounded-xl bg-white/5 px-4 py-3">
                <div>
                  <p className="font-medium">{String(r.display_name ?? r.name ?? "-")}</p>
                  <p className="text-xs text-[var(--muted)]">{String(r.role_template ?? r.role ?? "-")} · {String(r.language ?? "-")}</p>
                </div>
                <span className="text-xs text-[var(--muted)]">{String(r.phone ?? r.contact_phone ?? "")}</span>
              </div>
            ))}
          </div>
        </section>

        <section className="card">
          <span className="label">新建对象</span>
          <div className="mt-3 space-y-3">
            <input className="w-full rounded-xl border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]" placeholder="姓名" value={name} onChange={(e) => setName(e.target.value)} />
            <div className="grid grid-cols-2 gap-3">
              <select className="rounded-xl border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none" value={role} onChange={(e) => setRole(e.target.value)}>
                <option>采购商</option>
                <option>客户经理</option>
                <option>经销商</option>
              </select>
              <select className="rounded-xl border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none" value={lang} onChange={(e) => setLang(e.target.value)}>
                <option value="vi">越南语</option>
                <option value="zh">中文</option>
                <option value="en">英语</option>
                <option value="yue">粤语</option>
              </select>
            </div>
            <button className="btn-primary w-full" onClick={create}>建档</button>
            <p className="text-xs text-[var(--muted)]">选择角色模板 + 语言 + 背景生成一个对象。</p>
          </div>
        </section>
      </div>
    </div>
  );
}
