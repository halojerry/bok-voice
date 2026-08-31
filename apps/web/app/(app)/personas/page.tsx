"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { useAccount } from "@/components/account-context";

const EMPTY = { name: "", company: "", tone: "", language: "zh", reference_audio: "" };
const LANGS = [
  ["zh", "普通话"],
  ["yue", "粤语"],
  ["en", "English"],
] as const;

function parseVoiceMap(raw: unknown): Record<string, string> {
  if (typeof raw !== "string" || !raw.trim().startsWith("{")) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed as Record<string, string> : {};
  } catch {
    return {};
  }
}

export default function PersonasPage() {
  const { accountId } = useAccount();
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState(EMPTY);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);
  const [loading, setLoading] = useState(true);
  const [activeLang, setActiveLang] = useState<(typeof LANGS)[number][0]>("zh");
  const [refText, setRefText] = useState("");
  const [refFile, setRefFile] = useState<File | null>(null);
  const [voiceMap, setVoiceMap] = useState<Record<string, string>>({});
  const [previewUrl, setPreviewUrl] = useState("");
  const [speakers, setSpeakers] = useState<string[]>([]);

  async function refresh() {
    setLoading(true);
    try {
      const data = await api.listPersonas();
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
    api.listTtsSpeakers().then(setSpeakers).catch(() => {});
  }, []);

  async function save() {
    if (!form.name.trim()) return;
    setErr(null);
    setOk(false);
    try {
      const payload = { ...form, reference_audio: JSON.stringify(voiceMap) };
      if (editingId) await api.updatePersona(editingId, { ...payload, account_id: accountId });
      else await api.createPersona({ ...payload, account_id: accountId });
      setForm(EMPTY);
      setEditingId(null);
      setVoiceMap({});
      setRefText("");
      setRefFile(null);
      setOk(true);
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  }

  async function remove(id: string) {
    if (!window.confirm("确认删除该人设？")) return;
    try {
      await api.deletePersona(id);
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  }

  function edit(row: Record<string, unknown>) {
    setEditingId(String(row.id ?? ""));
    setForm({
      name: String(row.name ?? ""),
      company: String(row.company ?? ""),
      tone: String(row.tone ?? ""),
      language: String(row.language ?? "zh"),
      reference_audio: String(row.reference_audio ?? ""),
    });
    setVoiceMap(parseVoiceMap(row.reference_audio));
    setRefText("");
    setRefFile(null);
  }

  async function registerVoice() {
    if (!refFile) {
      setErr("请选择参考音频");
      return;
    }
    setErr(null);
    setOk(false);
    try {
      const body = new FormData();
      body.append("file", refFile);
      body.append("voice_id", `${form.name || "voice"}-${activeLang}-${Date.now()}`);
      body.append("ref_text", refText);
      body.append("language", activeLang);
      const result = await api.registerTtsVoice(body);
      const voiceId = String(result.voice_id ?? "");
      setVoiceMap((prev) => ({ ...prev, [activeLang]: voiceId }));
      setOk(true);
    } catch (e) {
      setErr(String(e));
    }
  }

  async function previewVoice() {
    const voice = voiceMap[activeLang];
    if (!voice) {
      setErr("当前语言还没有绑定音色");
      return;
    }
    setErr(null);
    try {
      const blob = await api.previewTts({
        text: "你好，我是 Bok 客服助手，请问有什么可以帮您？",
        voice,
        language: activeLang,
      });
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      setPreviewUrl(URL.createObjectURL(blob));
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

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_360px]">
        <section className="card">
          {err && <ErrorState message={err} />}
          {loading ? (
            <LoadingState />
          ) : rows.length === 0 ? (
            <EmptyState label="暂无 AI 人设，请在右侧新建。" />
          ) : (
            <div className="space-y-2">
              {rows.map((row) => {
                const id = String(row.id ?? "");
                return (
                  <div key={id} className="flex items-start justify-between gap-3 rounded-lg bg-white/5 px-4 py-3">
                    <div className="min-w-0">
                      <p className="font-medium">{String(row.name ?? "-")}</p>
                      <p className="mt-1 text-xs text-[var(--muted)]">
                        {String(row.company ?? "")} · {String(row.language ?? "zh")}
                      </p>
                      {String(row.tone ?? "") && <p className="mt-1 line-clamp-3 text-sm text-[var(--muted)]">{String(row.tone)}</p>}
                    </div>
                    <div className="flex shrink-0 gap-2">
                      <button className="btn-ghost text-xs" onClick={() => edit(row)}>编辑</button>
                      <button className="btn-ghost text-xs text-red-300" onClick={() => remove(id)}>删除</button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section>

        <section className="card space-y-4">
          <span className="label">{editingId ? "编辑人设" : "新建人设"}</span>
          <input
            className="w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="称呼，例如：小博"
          />
          <input
            className="w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
            value={form.company}
            onChange={(e) => setForm({ ...form, company: e.target.value })}
            placeholder="代表公司，例如：Bok 建材"
          />
          <textarea
            className="h-28 w-full resize-none rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
            value={form.tone}
            onChange={(e) => setForm({ ...form, tone: e.target.value })}
            placeholder="专业、温和、简洁；适当使用敬语…"
          />
          <div className="space-y-2">
            <div className="flex gap-2">
              {LANGS.map(([lang, label]) => (
                <button
                  key={lang}
                  className={`btn-ghost text-xs ${activeLang === lang ? "!border-[var(--accent)]" : ""}`}
                  onClick={() => setActiveLang(lang)}
                >
                  {label}
                </button>
              ))}
            </div>
            <select
              className="w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              value={voiceMap[activeLang] ?? ""}
              onChange={(e) => setVoiceMap((prev) => ({ ...prev, [activeLang]: e.target.value }))}
            >
              <option value="">选择预置音色 / 已克隆 voice_id</option>
              {speakers.map((speaker) => (
                <option key={speaker} value={speaker}>{speaker}</option>
              ))}
              {Object.keys(voiceMap).length > 0 && <option value={voiceMap[activeLang] ?? ""}>{voiceMap[activeLang] ?? ""}</option>}
            </select>
            <input
              className="w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              value={refText}
              onChange={(e) => setRefText(e.target.value)}
              placeholder="参考音频对应的文字，例如：你好，我是 Bok 客服助手"
            />
            <input
              type="file"
              accept="audio/*"
              onChange={(e) => setRefFile(e.target.files?.[0] ?? null)}
            />
            <div className="flex gap-2">
              <button className="btn-ghost w-full" onClick={registerVoice}>克隆并保存</button>
              <button className="btn-ghost w-full" onClick={previewVoice}>试听</button>
            </div>
            {previewUrl && <audio controls src={previewUrl} className="mt-2 w-full" />}
          </div>
          <button className="btn-primary w-full" onClick={save}>
            {editingId ? "保存修改" : "新建人设"}
          </button>
          {ok && <p className="text-sm text-emerald-400">已保存。</p>}
        </section>
      </div>
    </div>
  );
}
