"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { useAccount } from "@/components/account-context";

const LANGS = [
  ["zh", "普通话"],
  ["yue", "粤语"],
  ["en", "English"],
] as const;

/** 模板四段字段的中文标签（数据库里以 opening/core/objection/closing 存储）。 */
const FIELD_LABELS = {
  opening: "开场白",
  core: "核心话术",
  objection: "异议应对",
  closing: "收尾话术",
} as const;

const FIELD_PLACEHOLDERS = {
  opening: "如：您好，我是…，今天联系您是想…",
  core: "产品卖点 / 需要传达的核心信息…",
  objection: "客户可能的顾虑与应对话术…",
  closing: "如：好的，那就不打扰您了，再见。",
} as const;

const TEMPLATE_FIELDS = ["opening", "core", "objection", "closing"] as const;

const EMPTY = {
  name: "",
  opening: "",
  core: "",
  objection: "",
  closing: "",
  tone_override: "",
  language: "zh",
};

export default function TemplatesPage() {
  const { accountId } = useAccount();
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState(EMPTY);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);
  const [loading, setLoading] = useState(true);

  async function refresh() {
    setLoading(true);
    try {
      const data = await api.listTemplates(accountId);
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
  }, [accountId]);

  function edit(row: Record<string, unknown>) {
    setEditingId(String(row.id ?? ""));
    setForm({
      name: String(row.name ?? ""),
      opening: String(row.opening ?? ""),
      core: String(row.core ?? ""),
      objection: String(row.objection ?? ""),
      closing: String(row.closing ?? ""),
      tone_override: String(row.tone_override ?? ""),
      language: String(row.language ?? "zh"),
    });
    setOk(false);
  }

  async function save() {
    if (!form.name.trim()) {
      setErr("请填写模板名称");
      return;
    }
    setErr(null);
    setOk(false);
    try {
      const payload = { ...form, account_id: accountId };
      if (editingId) await api.updateTemplate(editingId, payload);
      else await api.createTemplate(payload);
      setForm(EMPTY);
      setEditingId(null);
      setOk(true);
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  }

  async function remove(id: string) {
    if (!window.confirm("确认删除该话术模板？")) return;
    try {
      await api.deleteTemplate(id);
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  }

  const textarea = "w-full resize-none rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]";

  return (
    <div>
      <div className="mb-8">
        <h1 className="page-title">话术库</h1>
        <p className="page-sub">可复用的对话模板 · 开场白 / 核心话术 / 异议应对 / 收尾，对象卡可绑定</p>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_440px]">
        <section className="card">
          {err && <ErrorState message={err} />}
          {loading ? (
            <LoadingState />
          ) : rows.length === 0 ? (
            <EmptyState label="暂无话术模板，请在右侧新建。" />
          ) : (
            <div className="space-y-3">
              {rows.map((row) => {
                const id = String(row.id ?? "");
                return (
                  <div key={id} className="rounded-lg bg-white/5 p-4">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="font-medium">{String(row.name ?? "-")}</p>
                        <p className="mt-1 text-xs text-[var(--muted)]">
                          {LANGS.find((l) => l[0] === String(row.language ?? "zh"))?.[1] ?? String(row.language ?? "zh")}
                          {String(row.tone_override ?? "") && ` · 语气 ${String(row.tone_override)}`}
                        </p>
                      </div>
                      <div className="flex shrink-0 gap-2">
                        <button className="btn-ghost text-xs" onClick={() => edit(row)}>编辑</button>
                        <button className="btn-ghost text-xs text-red-300" onClick={() => remove(id)}>删除</button>
                      </div>
                    </div>
                    <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
                      {TEMPLATE_FIELDS.map((k) => (
                        <div key={k}>
                          <span className="label">{FIELD_LABELS[k]}</span>
                          <p className="mt-0.5 line-clamp-3 text-[var(--muted)]">{String(row[k] ?? "")}</p>
                        </div>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section>

        <section className="card space-y-3">
          <span className="label">{editingId ? "编辑模板" : "新建模板"}</span>
          <input
            className="w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="模板名，例如：越南采购商常见异议"
          />
          {TEMPLATE_FIELDS.map((k) => (
            <label key={k} className="block">
              <span className="text-xs text-[var(--stage-muted)]">{FIELD_LABELS[k]}</span>
              <textarea
                className={`mt-1 h-20 ${textarea}`}
                value={form[k]}
                onChange={(e) => setForm({ ...form, [k]: e.target.value })}
                placeholder={FIELD_PLACEHOLDERS[k]}
              />
            </label>
          ))}
          <label className="block">
            <span className="text-xs text-[var(--stage-muted)]">语气覆盖（可选，优先于人设）</span>
            <input
              className="mt-1 w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              value={form.tone_override}
              onChange={(e) => setForm({ ...form, tone_override: e.target.value })}
              placeholder="如：专业、温和、简洁"
            />
          </label>
          <label className="block">
            <span className="text-xs text-[var(--stage-muted)]">语言</span>
            <select
              className="mt-1 w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm"
              value={form.language}
              onChange={(e) => setForm({ ...form, language: e.target.value })}
            >
              {LANGS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
          <div className="flex items-center gap-3">
            <button className="btn-primary" onClick={save}>{editingId ? "保存修改" : "创建模板"}</button>
            {editingId && <button className="btn-ghost" onClick={() => { setEditingId(null); setForm(EMPTY); }}>取消</button>}
            {ok && <span className="text-sm text-emerald-400">已保存。</span>}
          </div>
        </section>
      </div>
    </div>
  );
}
