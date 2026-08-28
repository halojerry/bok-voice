"use client";

import { useState } from "react";
import { api } from "@/lib/api";

export default function KnowledgePage() {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<Record<string, unknown>[]>([]);
  const [markdown, setMarkdown] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);

  async function search() {
    if (!q.trim()) return;
    setErr(null);
    try {
      const data = await api.searchKnowledge(q);
      setResults(Array.isArray(data) ? data : []);
    } catch (e) {
      setErr(String(e));
    }
  }

  async function importMd() {
    if (!markdown.trim()) return;
    setErr(null);
    setOk(false);
    try {
      await api.importKnowledge({ markdown, account_id: "acc-001" });
      setMarkdown("");
      setOk(true);
    } catch (e) {
      setErr(String(e));
    }
  }

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-xl font-semibold">知识库</h1>
        <p className="mt-1 text-sm text-[var(--muted)]">账号知识库 · Markdown 事实源 · 全局洞察（管理员）</p>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_340px]">
        <section className="card">
          <span className="label">语义检索</span>
          <div className="mt-3 flex gap-2">
            <input
              className="flex-1 rounded-xl border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              placeholder="检索产品知识…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && search()}
            />
            <button className="btn-primary" onClick={search}>检索</button>
          </div>
          <div className="mt-4 space-y-2">
            {err && <p className="text-sm text-red-300">{err}</p>}
            {results.length === 0 && <p className="text-sm text-[var(--muted)]">输入关键词检索当前账号知识库。</p>}
            {results.map((r, i) => (
              <div key={i} className="rounded-xl bg-white/5 p-3 text-sm">
                <p className="text-[var(--muted)]">{String(r.source ?? r.title ?? "片段")}</p>
                <p className="mt-1 line-clamp-3">{String(r.text ?? r.content ?? r.snippet ?? "")}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="card">
          <span className="label">导入 Markdown</span>
          <textarea
            className="mt-3 h-44 w-full resize-none rounded-xl border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
            placeholder="粘贴文档 / 产品资料…"
            value={markdown}
            onChange={(e) => setMarkdown(e.target.value)}
          />
          <button className="btn-primary mt-3 w-full" onClick={importMd}>导入知识库</button>
          {ok && <p className="mt-2 text-sm text-emerald-400">导入成功。</p>}
          {err && <p className="mt-2 text-sm text-red-300">{err}</p>}
          <p className="mt-3 text-xs text-[var(--muted)]">Bok 作为 Markdown 事实源与知识治理，导入后按账号沉淀。</p>
        </section>
      </div>
    </div>
  );
}
