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
  tracking_no: string;
  courier: string;
  template_id: string;
}

const EMPTY_FORM = {
  display_name: "",
  role_template: "采购商",
  language: "vi",
  background: "",
  phone: "",
  tracking_no: "",
  courier: "",
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
  const [importText, setImportText] = useState("");
  const [importMsg, setImportMsg] = useState<string | null>(null);
  const [showImport, setShowImport] = useState(false);

  /** 解析 CSV/制表符分隔文本 → 对象行(支持表头:姓名/快递单号/物流公司/电话)。 */
  function parseImport(text: string): Record<string, string>[] {
    const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    if (lines.length === 0) return [];
    const head = lines[0].split(/[,\t]/).map((h) => h.trim());
    const colOf = (names: string[]) => {
      const i = head.findIndex((h) => names.includes(h.toLowerCase().replace(/\s/g, "")));
      return i;
    };
    const cName = colOf(["姓名", "名字", "name", "display_name"]);
    const cNo = colOf(["快递单号", "单号", "tracking_no", "tracking"]);
    const cCourier = colOf(["物流公司", "快递公司", "物流", "courier"]);
    const cPhone = colOf(["电话", "手机", "phone"]);
    const rows: Record<string, string>[] = [];
    for (const line of lines.slice(1)) {
      const cells = line.split(/[,\t]/).map((c) => c.trim());
      const name = cName >= 0 ? cells[cName] ?? "" : "";
      if (!name) continue;
      rows.push({
        display_name: name,
        role_template: "采购商",
        language: "vi",
        tracking_no: cNo >= 0 ? cells[cNo] ?? "" : "",
        courier: cCourier >= 0 ? cells[cCourier] ?? "" : "",
        phone: cPhone >= 0 ? cells[cPhone] ?? "" : "",
      });
    }
    return rows;
  }

  async function doImport() {
    if (!importText.trim()) { setImportMsg("请先粘贴表格内容。"); return; }
    setImportMsg(null);
    try {
      const rows = parseImport(importText);
      if (rows.length === 0) { setImportMsg("没有解析到有效行：首行需含「姓名」表头，每行一个人。"); return; }
      const res = await api.importObjects(rows, accountId);
      const imported = Number((res as { imported?: number }).imported ?? rows.length);
      setImportMsg(`已导入 ${imported} 条。`);
      setImportText("");
      setShowImport(false);
      await refresh();
    } catch (e) {
      setImportMsg(`导入失败：${String(e)}`);
    }
  }

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
      tracking_no: row.tracking_no ?? "",
      courier: row.courier ?? "",
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
          <div className="mb-4 flex items-center gap-2">
            <input
              className="min-w-0 flex-1 rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              placeholder="搜索对象…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            <button className="btn-ghost shrink-0 text-xs" onClick={() => setShowImport((v) => !v)}>
              表格导入
            </button>
          </div>
          {showImport && (
            <div className="mb-4 rounded-lg border border-[var(--card-border)] p-3">
              <p className="text-xs text-[var(--muted)]">
                粘贴表格(支持 CSV/制表符,首行为表头):<b>姓名</b>、<b>快递单号</b>、<b>物流公司</b>、电话(可选)
              </p>
              <textarea
                className="mt-2 h-28 w-full resize-none rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 font-mono text-xs outline-none focus:border-[var(--accent)]"
                placeholder={"姓名,快递单号,物流公司,电话\n张三,SF1234567890,顺丰,13800000000\n李四,YT9988776655,圆通"}
                value={importText}
                onChange={(e) => setImportText(e.target.value)}
              />
              <div className="mt-2 flex items-center gap-2">
                <button className="btn-primary px-3 py-1 text-xs" onClick={doImport}>导入</button>
                {importMsg && <span className="text-xs text-[var(--muted)]">{importMsg}</span>}
              </div>
            </div>
          )}
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
                        {String(r.tracking_no ?? "") ? ` · 单号 ${String(r.tracking_no)}` : ""}
                        {String(r.courier ?? "") ? ` · ${String(r.courier)}` : ""}
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
            <div className="grid grid-cols-2 gap-3">
              <input
                className="rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
                placeholder="快递单号"
                value={form.tracking_no}
                onChange={(e) => setForm({ ...form, tracking_no: e.target.value })}
              />
              <input
                className="rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
                placeholder="物流公司"
                value={form.courier}
                onChange={(e) => setForm({ ...form, courier: e.target.value })}
              />
            </div>
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
