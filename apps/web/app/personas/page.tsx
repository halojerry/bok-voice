"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export default function PersonasPage() {
  const [persona, setPersona] = useState<Record<string, unknown>>({});
  const [name, setName] = useState("");
  const [company, setCompany] = useState("");
  const [style, setStyle] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);

  useEffect(() => {
    api.listPersonas().then((rows) => {
      if (rows && rows.length) {
        const p = rows[0];
        setPersona(p);
        setName(String(p.name ?? ""));
        setCompany(String(p.company ?? ""));
        setStyle(String(p.tone ?? p.speech_style ?? ""));
        setErr(null);
      }
    }).catch((e) => setErr(String(e)));
  }, []);

  async function save() {
    setErr(null);
    setOk(false);
    try {
      await api.updatePersonas({ name, company, tone: style });
      setOk(true);
    } catch (e) {
      setErr(String(e));
    }
  }

  return (
    <div>
      <div className="mb-8">
        <h1 className="page-title">AI 人设</h1>
        <p className="page-sub">我方身份 · 代表公司 · 说话风格 · 参考音频</p>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_340px]">
        <section className="card space-y-4">
          <div>
            <span className="label">称呼</span>
            <input className="mt-2 w-full rounded-xl border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]" value={name} onChange={(e) => setName(e.target.value)} placeholder="例如：小博" />
          </div>
          <div>
            <span className="label">代表公司</span>
            <input className="mt-2 w-full rounded-xl border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]" value={company} onChange={(e) => setCompany(e.target.value)} placeholder="例如：Bok 建材" />
          </div>
          <div>
            <span className="label">说话风格</span>
            <textarea className="mt-2 h-28 w-full resize-none rounded-xl border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]" value={style} onChange={(e) => setStyle(e.target.value)} placeholder="专业、温和、简洁；适当使用敬语…" />
          </div>
          {err && <p className="text-sm text-red-300">{err}</p>}
          <div className="flex items-center gap-3">
            <button className="btn-primary" onClick={save}>保存人设</button>
            {ok && <span className="text-sm text-emerald-400">已保存。</span>}
          </div>
        </section>

        <section className="card">
          <span className="label">参考音频</span>
          <div className="mt-3 flex items-center justify-center rounded-xl border border-dashed border-[var(--card-border)] p-8 text-center text-sm text-[var(--muted)]">
            上传参考音频
            <br />(越 / 粤音色)
          </div>
          <p className="mt-3 text-xs text-[var(--muted)]">GPT-SoVITS 参考音频提前生成并缓存 voice embedding，禁止热路径首次克隆。</p>
        </section>
      </div>
    </div>
  );
}
