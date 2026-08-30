"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";

export default function KnowledgePage() {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<Record<string, unknown>[]>([]);
  const [docs, setDocs] = useState<Record<string, unknown>[]>([]);
  const [content, setContent] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);
  const [loading, setLoading] = useState(true);

  async function refreshDocs() {
    setLoading(true);
    try {
      const data = await api.listKnowledge();
      setDocs(Array.isArray(data) ? data : []);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refreshDocs();
  }, []);

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
    if (!content.trim()) return;
    setErr(null);
    setOk(false);
    try {
      await api.importKnowledge({ account_id: "acc-001", path: `manual-${Date.now()}.md`, content });
      setContent("");
      setOk(true);
      await refreshDocs();
    } catch (e) {
      setErr(String(e));
    }
  }

  async function remove(id: string) {
    if (!window.confirm("确认删除该知识片段？")) return;
    try {
      await api.deleteKnowledge(id);
      await refreshDocs();
    } catch (e) {
      setErr(String(e));
    }
  }

  return (
    <div>
      <div className="mb-8">
        <h1 className="page-title">知识库</h1>
        <p className="page-sub">账号知识库 · Markdown 事实源 · 导入 / 检索 / 删除</p>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_360px]">
        <section className="card">
          <span className="label">已导入知识</span>
          {err && <ErrorState message={err} />}
          {loading ? (
            <LoadingState />
          ) : docs.length === 0 ? (
            <EmptyState label="暂无知识源，请在右侧导入 Markdown。" />
          ) : (
            <div className="mt-3 space-y-2">
              {docs.map((doc) => {
                const id = String(doc.id ?? "");
                return (
                  <div key={id} className="flex items-start justify-between gap-3 rounded-lg bg-white/5 px-4 py-3">
                    <div className="min-w-0">
                      <p className="text-xs text-[var(--muted)]">{String(doc.path ?? doc.source ?? "片段")}</p>
                      <p className="mt-1 line-clamp-3 text-sm">{String(doc.text ?? doc.content ?? "")}</p>
                    </div>
                    <button className="shrink-0 text-xs text-red-300" onClick={() => remove(id)}>删除</button>
                  </div>
                );
              })}
            </div>
          )}
        </section>

        <section className="space-y-6">
          <div className="card">
            <span className="label">语义检索</span>
            <div className="mt-3 flex gap-2">
              <input
                className="flex-1 rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
                placeholder="检索产品知识…"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && search()}
              />
              <button className="btn-primary" onClick={search}>检索</button>
            </div>
            <div className="mt-4 space-y-2">
              {results.length === 0 && <p className="text-sm text-[var(--muted)]">输入关键词检索当前账号知识库。</p>}
              {results.map((r, i) => (
                <div key={i} className="rounded-lg bg-white/5 p-3 text-sm">
                  <p className="text-[var(--muted)]">{String(r.source ?? r.title ?? "片段")}</p>
                  <p className="mt-1 line-clamp-3">{String(r.text ?? r.content ?? r.snippet ?? "")}</p>
                </div>
              ))}
            </div>
          </div>

          <div className="card">
            <span className="label">导入 Markdown</span>
            <textarea
              className="mt-3 h-44 w-full resize-none rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              placeholder="粘贴文档 / 产品资料…"
              value={content}
              onChange={(e) => setContent(e.target.value)}
            />
            <button className="btn-primary mt-3 w-full" onClick={importMd}>导入知识库</button>
            {ok && <p className="mt-2 text-sm text-emerald-400">导入成功。</p>}
          </div>
        </section>
      </div>
    </div>
  );
}
