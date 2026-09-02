"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { useAccount } from "@/components/account-context";
import { startRecording, type RecorderHandle } from "@/lib/recorder";

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
  // 录音克隆：直接对麦克风说话生成参考音频，无需本地上传文件。
  const [recording, setRecording] = useState(false);
  const [recSec, setRecSec] = useState(0);
  const [recBlobUrl, setRecBlobUrl] = useState("");
  const recRef = useRef<RecorderHandle | null>(null);
  const recTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

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
    if (!form.name.trim()) {
      // 必须给出明确反馈：此前静默 return 会让用户以为"新建没反应"。
      setErr("请先填写称呼（名称）再保存。");
      return;
    }
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
      clearRecording();
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
    clearRecording();
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

  async function toggleRecording() {
    setErr(null);
    if (recording) {
      // 停止：把录音转成 WAV 作为克隆参考音频（自动填入参考文字提示）。
      const handle = recRef.current;
      recRef.current = null;
      if (recTimerRef.current) { clearInterval(recTimerRef.current); recTimerRef.current = null; }
      setRecording(false);
      setRecSec(0);
      if (!handle) return;
      try {
        const wav = await handle.stop();
        if (wav.size < 4096) {
          setErr("录音太短，请至少说 1-2 秒后再试。");
          return;
        }
        const file = new File([wav], `ref-${activeLang}-${Date.now()}.wav`, { type: "audio/wav" });
        setRefFile(file);
        if (recBlobUrl) URL.revokeObjectURL(recBlobUrl);
        setRecBlobUrl(URL.createObjectURL(wav));
        setOk(false);
      } catch (e) {
        setErr(`录音失败：${String(e)}`);
      }
      return;
    }
    // 开始录音
    try {
      const handle = await startRecording(30000);
      recRef.current = handle;
      setRecording(true);
      setRecSec(0);
      recTimerRef.current = setInterval(() => setRecSec((s) => s + 1), 1000);
    } catch (e) {
      setErr(`无法开始录音：${String(e)}。请在系统设置允许麦克风权限。`);
    }
  }

  function clearRecording() {
    if (recRef.current) { recRef.current.cancel(); recRef.current = null; }
    if (recTimerRef.current) { clearInterval(recTimerRef.current); recTimerRef.current = null; }
    setRecording(false);
    setRecSec(0);
    setRefFile(null);
    if (recBlobUrl) URL.revokeObjectURL(recBlobUrl);
    setRecBlobUrl("");
  }

  async function registerVoice() {
    if (!refFile) {
      setErr("请先上传参考音频，或点「录音」说一段话作为克隆素材。");
      return;
    }
    if (!refText.trim()) {
      setErr("请填写参考音频对应的文字（方便克隆对齐）。");
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
      clearRecording();
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
              placeholder="参考音频对应的文字（录音/上传都要填），例如：你好，我係小博，有咩可以幫到你？"
            />
            {/* 录音克隆：直接对麦克风说一段话作为该语言的参考音色 */}
            <div className="rounded-lg border border-[var(--card-border)] p-2">
              <div className="flex items-center gap-2">
                <button
                  className={`btn-ghost flex-1 text-xs ${recording ? "!text-red-300" : ""}`}
                  onClick={toggleRecording}
                >
                  {recording ? `● 停止录音（${recSec}s）` : "🎙 录音（用麦克风录一段）"}
                </button>
                {(refFile || recBlobUrl) && (
                  <button className="btn-ghost text-xs" onClick={clearRecording}>清除</button>
                )}
              </div>
              {recording && (
                <p className="mt-1 text-xs text-red-300">正在录音…请对着麦克风说参考语料（建议 5-10 秒，可含目标语言特征）。</p>
              )}
              {recBlobUrl && (
                <div className="mt-2">
                  <p className="text-xs text-[var(--muted)]">录音预览（将作为克隆参考音频）：</p>
                  <audio controls src={recBlobUrl} className="mt-1 w-full" />
                </div>
              )}
            </div>
            <input
              type="file"
              accept="audio/*,.wav,.mp3,.m4a"
              onChange={(e) => {
                setRefFile(e.target.files?.[0] ?? null);
                if (recBlobUrl) URL.revokeObjectURL(recBlobUrl);
                setRecBlobUrl("");
              }}
            />
            <div className="flex gap-2">
              <button className="btn-ghost w-full" onClick={registerVoice} disabled={recording}>克隆并保存音色</button>
              <button className="btn-ghost w-full" onClick={previewVoice}>试听已选音色</button>
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
