"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { useAccount } from "@/components/account-context";

interface ObjectRow {
  id: string;
  display_name: string;
  role_template: string;
  language: string;
  background: string;
  phone: string;
  template_id: string;
}

const EMPTY_FORM = {
  display_name: "",
  role_template: "采购商",
  language: "vi",
  background: "",
  phone: "",
  template_id: "",
};

export default function ObjectsPage() {
  const { accountId } = useAccount();
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [q, setQ] = useState("");
  const [form, setForm] = useState(EMPTY_FORM);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [templates, setTemplates] = useState<Record<string, unknown>[]>([]);

  async function refresh() {
    setLoading(true);
    try {
      const data = await api.listObjects(accountId);
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
    api.listTemplates(accountId).then(setTemplates).catch(() => {});
  }, [accountId]);

  const filtered = useMemo(() => {
    const query = q.trim().toLowerCase();
    if (!query) return rows;
    return rows.filter((r) =>
      [r.display_name, r.phone, r.role_template].some((v) => String(v ?? "").toLowerCase().includes(query)),
    );
  }, [q, rows]);

  async function save() {
    if (!form.display_name.trim()) return;
    setErr(null);
    try {
      if (editingId) await api.updateObject(editingId, form);
      else await api.createObject(form, accountId);
      setForm(EMPTY_FORM);
      setEditingId(null);
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  }

  async function remove(id: string) {
    if (!window.confirm("确认删除该对象？")) return;
    try {
      await api.deleteObject(id);
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  }

  function edit(row: ObjectRow) {
    setEditingId(row.id);
    setForm({
      display_name: row.display_name,
      role_template: row.role_template,
      language: row.language,
      background: row.background,
      phone: row.phone,
      template_id: row.template_id ?? "",
    });
  }

  return (
    <div>
      <div className="mb-8 flex items-start justify-between">
        <div>
          <h1 className="page-title">对象管理</h1>
          <p className="page-sub">AI 对话对方档案 · 建档 / 编辑 / 历史主题</p>
        </div>
        <button className="btn-ghost" onClick={() => { setEditingId(null); setForm(EMPTY_FORM); }}>
          清空表单
        </button>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_380px]">
        <section className="card">
          <input
            className="mb-4 w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
            placeholder="搜索对象…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          {err && <ErrorState message={err} />}
          {loading ? (
            <LoadingState />
          ) : filtered.length === 0 ? (
            <EmptyState label="暂无对象，请在右侧建档。" />
          ) : (
            <div className="space-y-2">
              {filtered.map((r) => {
                const id = String(r.id ?? r.object_id ?? "");
                return (
                  <div key={id} className="flex items-center justify-between gap-3 rounded-lg bg-white/5 px-4 py-3">
                    <div className="min-w-0">
                      <p className="truncate font-medium">{String(r.display_name ?? "-")}</p>
                      <p className="text-xs text-[var(--muted)]">
                        {String(r.role_template ?? "-")} · {String(r.language ?? "-")}
                        {String(r.phone ?? "") ? ` · ${String(r.phone)}` : ""}
                      </p>
                      {String(r.background ?? "") && (
                        <p className="mt-1 line-clamp-2 text-xs text-[var(--muted)]">{String(r.background)}</p>
                      )}
                    </div>
                    <div className="flex shrink-0 gap-2">
                      <button className="btn-ghost text-xs" onClick={() => edit(r as unknown as ObjectRow)}>编辑</button>
                      <button className="btn-ghost text-xs text-red-300" onClick={() => remove(id)}>删除</button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section>

        <section className="card">
          <span className="label">{editingId ? "编辑对象" : "新建对象"}</span>
          <div className="mt-3 space-y-3">
            <input
              className="w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              placeholder="姓名"
              value={form.display_name}
              onChange={(e) => setForm({ ...form, display_name: e.target.value })}
            />
            <div className="grid grid-cols-2 gap-3">
              <select
                className="rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none"
                value={form.role_template}
                onChange={(e) => setForm({ ...form, role_template: e.target.value })}
              >
                <option>采购商</option>
                <option>客户经理</option>
                <option>经销商</option>
              </select>
              <select
                className="rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none"
                value={form.language}
                onChange={(e) => setForm({ ...form, language: e.target.value })}
              >
                <option value="vi">越南语</option>
                <option value="zh">中文</option>
                <option value="en">英语</option>
                <option value="yue">粤语</option>
              </select>
            </div>
            <input
              className="w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              placeholder="电话（可选）"
              value={form.phone}
              onChange={(e) => setForm({ ...form, phone: e.target.value })}
            />
            <textarea
              className="h-28 w-full resize-none rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              placeholder="背景 / 角色说明…"
              value={form.background}
              onChange={(e) => setForm({ ...form, background: e.target.value })}
            />
            <label className="block">
              <span className="text-xs text-[var(--stage-muted)]">绑定话术模板</span>
              <select
                className="mt-1 w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none"
                value={form.template_id}
                onChange={(e) => setForm({ ...form, template_id: e.target.value })}
              >
                <option value="">不绑定</option>
                {templates.map((t) => (
                  <option key={String(t.id)} value={String(t.id)}>{String(t.name ?? t.id)}</option>
                ))}
              </select>
            </label>
            <button className="btn-primary w-full" onClick={save}>
              {editingId ? "保存修改" : "建档"}
            </button>
            <p className="text-xs text-[var(--muted)]">对象档案会注入后续通话上下文。</p>
          </div>
        </section>
      </div>
    </div>
  );
}
