"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { useAccount } from "@/components/account-context";
import { startRecording, type RecorderHandle } from "@/lib/recorder";
import { MINIMAX_VOICE_ENTRIES, previewLangForVoice } from "@/lib/minimax-voices";

const EMPTY = { name: "", company: "", tone: "", language: "zh", reference_audio: "", tts_provider: "" };
const LANGS = [
  ["zh", "普通话"],
  ["cantonese", "粤语"],
  ["en", "English"],
] as const;

/** 试听合成文案：随当前语言给出含人设称呼的一句话。 */
function previewTextFor(lang: (typeof LANGS)[number][0], name: string): string {
  const n = name.trim() || "Bok 客服";
  switch (lang) {
    case "cantonese":
      return `你好，我係${n}，唔該想問下件貨而家到咗未？可以幫我 check 下 status 嘛？`;
    case "en":
      return `Hello, this is ${n}. How can I help you today?`;
    default:
      return `你好，我是${n}，请问有什么可以帮您？`;
  }
}

/** 本地 Qwen3 预置音色的中文名（音译，便于识别；预置为多语模型，非克隆）。 */
const PRESET_VOICE_CN: Record<string, string> = {
  serena: "塞蕾娜",
  vivian: "薇薇安",
  uncle_fu: "傅叔叔",
  ryan: "瑞安",
  aiden: "艾登",
  ono_anna: "小野安娜",
  sohee: "素希",
  eric: "埃里克",
  dylan: "迪伦",
};

const LANG_LABEL: Record<string, string> = { zh: "普通话", cantonese: "粤语", en: "英语" };

/** 音色下拉选项：预置音色 + 已克隆 voice（后者标 [克隆]，带其注册语言）。 */
interface VoiceOption {
  id: string;
  cloned: boolean;
  lang?: string;
}

function voiceLabel(vo: VoiceOption): string {
  if (vo.cloned) {
    const langTag = vo.lang ? LANG_LABEL[vo.lang] ?? vo.lang : "";
    return `${vo.id}（克隆${langTag ? " · " + langTag : ""}）`;
  }
  return `${PRESET_VOICE_CN[vo.id] ?? vo.id}（预置音色）`;
}

/** 给某语言推荐一个音色：已绑定的 > 语言匹配的克隆 > 第一个预置。 */
function suggestVoiceFor(
  lang: string,
  voiceMap: Record<string, string>,
  clonedVoices: VoiceOption[],
  speakers: string[],
): string {
  if (voiceMap?.[lang]) return voiceMap[lang];
  const match = clonedVoices.find((c) => c.lang === lang);
  if (match) return match.id;
  return speakers?.[0] ?? "";
}

function parseVoiceMap(raw: unknown): Record<string, string> {
  if (typeof raw !== "string" || !raw.trim().startsWith("{")) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed as Record<string, string> : {};
  } catch {
    return {};
  }
}

/** 从语音映射选「整场主音色」：优先人设主语言，缺则 zh→cantonese→首个非空（与 agent 收敛同规则）。 */
function primaryVoiceFor(lang: string, voiceMap: Record<string, string>): string {
  const keys = [lang, "zh", "cantonese", "en"];
  for (const k of keys) {
    if (voiceMap[k]) return voiceMap[k];
  }
  return Object.values(voiceMap)[0] ?? "";
}

/** 云端单音色保存时写回 reference_audio（沿用 {zh,cantonese,en} 兼容结构，全场同声）。 */
function voiceMapForOneVoice(v: string): Record<string, string> {
  return v ? { zh: v, cantonese: v, en: v } : {};
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
  // 云端单音色（全场同声）选择：与人设主语言解耦，一个人设一把声。
  const [cloudVoice, setCloudVoice] = useState("");
  const [previewUrl, setPreviewUrl] = useState("");
  // 云端试听语言：默认跟随音色（Cantonese_* 默认粤语），可切到普/英听同一声。
  const [cloudPreviewLang, setCloudPreviewLang] = useState<"zh" | "cantonese" | "en" | "">("");
  const [speakers, setSpeakers] = useState<string[]>([]);
  // 已克隆 voice（来自 /api/tts/voices，注册在 TTS sidecar 的 voice_registry）
  const [clonedVoices, setClonedVoices] = useState<VoiceOption[]>([]);
  // 录音克隆：直接对麦克风说话生成参考音频，无需本地上传文件。
  const [recording, setRecording] = useState(false);
  const [recSec, setRecSec] = useState(0);
  const [recBlobUrl, setRecBlobUrl] = useState("");
  const recRef = useRef<RecorderHandle | null>(null);
  const recTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 人设选的是云端引擎（MiniMax/火山）：音色区切换到云端音色选择，隐藏本地克隆。
  const engineIsCloud = ["minimax", "minimax_streaming", "volcano_streaming"].includes(
    String(form.tts_provider ?? "").trim().toLowerCase(),
  );

  // 云端音色下拉只列与人设语言匹配的音色（英文人设只见英文音色，唔会乱）；
  // 未知/旧語言（如 vi）唔清空照列全部；跨語言已選值保留「自定义」項、唔静默改。
  const cloudVoiceOptions = useMemo(() => {
    const lang = String(form.language ?? "").toLowerCase();
    if (!["zh", "cantonese", "en"].includes(lang)) {
      return MINIMAX_VOICE_ENTRIES.map((v) => ({ value: v.id, label: v.label }));
    }
    return MINIMAX_VOICE_ENTRIES.filter((v) => v.lang === lang).map((v) => ({
      value: v.id,
      label: v.label,
    }));
  }, [form.language]);

  // 引擎或人设主语言切换时，把云端当前音色初始化为语音映射里的主音色（旧分语言数据收敛成单音色）。
  useEffect(() => {
    if (!engineIsCloud) return;
    const prim = primaryVoiceFor(form.language, voiceMap);
    setCloudVoice((prev) => (prev && !prim ? prev : prim));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engineIsCloud, form.language]);

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
    api
      .listTtsVoices()
      .then((voices) =>
        setClonedVoices(
          Array.isArray(voices)
            ? voices
                .map((v) => ({ id: String(v.voice_id ?? ""), cloned: true, lang: String(v.language ?? "").toLowerCase() || undefined }))
                .filter((v) => v.id)
            : [],
        ),
      )
      .catch(() => {});
  }, []);

  /** 当前语言音色下拉选项（预置 + 已克隆，去重）。 */
  const voiceOptions = useMemo<VoiceOption[]>(() => {
    const seen = new Set<string>();
    const out: VoiceOption[] = [];
    for (const s of speakers) {
      if (s && !seen.has(s)) {
        seen.add(s);
        out.push({ id: s, cloned: false });
      }
    }
    for (const c of clonedVoices) {
      if (c.id && !seen.has(c.id)) {
        seen.add(c.id);
        out.push(c);
      }
    }
    return out;
  }, [speakers, clonedVoices]);

  // 下拉排序：与当前语言匹配的克隆音色排最前（切到「粤语」时粤语克隆在最上面），其余克隆、预置在后。
  const orderedVoiceOptions = useMemo(() => {
    const matching = voiceOptions.filter((v) => v.cloned && v.lang === activeLang);
    const rest = voiceOptions.filter((v) => !(v.cloned && v.lang === activeLang));
    return [...matching, ...rest];
  }, [voiceOptions, activeLang]);

  // 切语言时若该语言还没绑音色，自动给一个推荐（语言匹配的克隆优先，否则第一个预置），
  // 让「试听已选音色」立刻能放出声；可再手动改下拉。
  useEffect(() => {
    setVoiceMap((prev) => {
      if (prev[activeLang]) return prev;
      const rec = suggestVoiceFor(activeLang, prev, clonedVoices, speakers);
      if (!rec) return prev;
      return { ...prev, [activeLang]: rec };
    });
  }, [activeLang, clonedVoices, speakers]);

  async function save() {
    if (!form.name.trim()) {
      // 必须给出明确反馈：此前静默 return 会让用户以为"新建没反应"。
      setErr("请先填写称呼（名称）再保存。");
      return;
    }
    setErr(null);
    setOk(false);
    try {
      // 云端引擎：整场固定一个音色（不随客户语言换声）。reference_audio 存三键同值，
      // 兼容 agent 按 {zh,cantonese,en} 读 map 的旧路径；本地 Qwen3 仍存分语言 voiceMap。
      const finalMap = engineIsCloud ? voiceMapForOneVoice(cloudVoice) : voiceMap;
      const payload = { ...form, reference_audio: JSON.stringify(finalMap) };
      if (editingId) await api.updatePersona(editingId, { ...payload, account_id: accountId });
      else await api.createPersona({ ...payload, account_id: accountId });
      setForm(EMPTY);
      setEditingId(null);
      setVoiceMap({});
      setCloudVoice("");
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
    const lang = String(row.language ?? "zh");
    setForm({
      name: String(row.name ?? ""),
      company: String(row.company ?? ""),
      tone: String(row.tone ?? ""),
      language: lang,
      reference_audio: String(row.reference_audio ?? ""),
      tts_provider: String(row.tts_provider ?? ""),
    });
    const map = parseVoiceMap(row.reference_audio);
    setVoiceMap(map);
    setCloudVoice(primaryVoiceFor(lang, map));
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
      // 新克隆立即可见（不用等下次刷新 voices），并带上其语言，方便按语言推荐/排序。
      setClonedVoices((prev) =>
        prev.some((v) => v.id === voiceId) ? prev : [...prev, { id: voiceId, cloned: true, lang: activeLang }],
      );
      clearRecording();
      setOk(true);
    } catch (e) {
      setErr(String(e));
    }
  }

  async function previewVoice() {
    if (engineIsCloud) {
      if (!cloudVoice) {
        setErr("请先选择一个人设音色（整场同声）。");
        return;
      }
      setErr(null);
      try {
        // 试听语言默认跟随音色（Cantonese_* → 粤语文本），用户可手动切普/英听同一声，
        // 避免「粤语音色念普通话文字 → 广式普通话」。
        const lang = (cloudPreviewLang || previewLangForVoice(cloudVoice)) as "zh" | "cantonese" | "en";
        const blob = await api.previewTts({
          text: previewTextFor(lang, form.name),
          voice: cloudVoice,
          language: lang,
          provider: String(form.tts_provider ?? ""),
        });
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        setPreviewUrl(URL.createObjectURL(blob));
      } catch (e) {
        setErr(String(e));
      }
      return;
    }
    const lang = activeLang;
    const voice = voiceMap[lang] || suggestVoiceFor(lang, voiceMap, clonedVoices, speakers);
    if (!voice) {
      setErr("暂无可用音色，请先克隆一个音色（录音/上传参考音频）。");
      return;
    }
    setErr(null);
    try {
      const blob = await api.previewTts({
        text: previewTextFor(lang, form.name),
        voice,
        language: lang,
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
                        {String(row.company ?? "")} · AI语言：{LANG_LABEL[String(row.language ?? "zh")] ?? String(row.language ?? "zh")}
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
          <div className="rounded-lg border border-[var(--card-border)] p-3">
            <span className="label mb-1 block">AI 使用语言</span>
            <p className="mb-2 text-[11px] text-[var(--muted)]">
              决定 AI 用什么语言开口（开场白）与默认表达；客户说其它语言时仍会跟随客户切换。
            </p>
            <div className="flex flex-wrap gap-2">
              {LANGS.map(([lang, label]) => (
                <button
                  key={lang}
                  className={`btn-ghost text-xs ${form.language === lang ? "!border-[var(--accent)]" : ""}`}
                  onClick={() => setForm({ ...form, language: lang })}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          <div className="rounded-lg border border-[var(--card-border)] p-3">
            <span className="label mb-1 block">语音引擎</span>
            <p className="mb-2 text-[11px] text-[var(--muted)]">
              决定该人设通话用哪套 TTS：本地 Qwen3（可用下方克隆音色）或云端 MiniMax
              （可在下方为这个人设选一个固定音色，整场同声）。留空 = 跟随全局设置。
            </p>
            <select
              className="w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              value={form.tts_provider ?? ""}
              onChange={(e) => setForm({ ...form, tts_provider: e.target.value })}
            >
              <option value="">跟随全局设置</option>
              <option value="qwen3_tts">本地 Qwen3-TTS（可用克隆音色）</option>
              <option value="minimax">MiniMax（云端 · 粤语地道/情感自然）</option>
              <option value="volcano_streaming">火山引擎（云端）</option>
            </select>
            {(form.tts_provider === "minimax" || form.tts_provider === "volcano_streaming") && (
              <p className="mt-2 text-[11px] text-[var(--accent)]">
                云端引擎：人设绑定一个音色后，整场通话（粤/普/英）都用它发声。火山引擎目前仍用全局配置音色。
              </p>
            )}
          </div>
          <div className="space-y-2">
            {engineIsCloud ? (
              <>
                {/* 云端引擎：整场固定一个音色（不随语言换声）。 */}
                <span className="text-xs text-[var(--stage-muted)]">AI 音色（整场同声 · 不随语言切换）</span>
                <p className="text-[11px] text-[var(--muted)]">
                  只列与当前人设语言匹配的音色（英文人设显示英文音色，对应语言显示对应音色），不会混在一起挑错。
                </p>
                <select
                  className="w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
                  value={cloudVoice}
                  onChange={(e) => { setCloudVoice(e.target.value); setCloudPreviewLang(""); setPreviewUrl(""); }}
                >
                  <option value="">选择 MiniMax 音色…</option>
                  {cloudVoiceOptions.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                  {cloudVoice && !cloudVoiceOptions.some((o) => o.value === cloudVoice) && (
                    <option value={cloudVoice}>已选（其他语言/自定义）：{cloudVoice}</option>
                  )}
                </select>
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-[var(--muted)]">试听语言：</span>
                  {([["cantonese", "粤"], ["zh", "普"], ["en", "英"]] as const).map(([lv, lb]) => {
                    const cur = cloudPreviewLang || previewLangForVoice(cloudVoice);
                    return (
                      <button
                        key={lv}
                        className={`btn-ghost px-1.5 py-0 text-[11px] ${cur === lv ? "!border-[var(--accent)]" : ""}`}
                        onClick={() => { setCloudPreviewLang(lv); setPreviewUrl(""); }}
                      >
                        {lb}
                      </button>
                    );
                  })}
                  <span className="ml-auto" />
                  <button className="btn-ghost w-auto px-2 text-xs" onClick={previewVoice}>试听</button>
                </div>
                {previewUrl && <audio controls autoPlay src={previewUrl} className="mt-2 w-full" />}
              </>
            ) : (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs text-[var(--stage-muted)]">为该语言选音色：</span>
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
                <p className="text-[11px] text-[var(--muted)]">
                  {`当前「${LANG_LABEL[activeLang] ?? activeLang}」音色：没绑时自动推荐${activeLang === "cantonese" ? "粤语克隆" : "该语言克隆"}${activeLang !== "cantonese" ? "" : "；要讲粤语请用「粤语参考音频克隆」的音色，普通话音色读粤语会带普通话音"}`}
                </p>
            <select
              className="w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              value={voiceMap[activeLang] ?? ""}
              onChange={(e) => setVoiceMap((prev) => ({ ...prev, [activeLang]: e.target.value }))}
            >
              <option value="">选择音色（预置 / 已克隆）</option>
              {orderedVoiceOptions.map((vo) => (
                <option key={vo.id} value={vo.id}>
                  {voiceLabel(vo)}
                </option>
              ))}
              {voiceMap[activeLang] && !orderedVoiceOptions.some((vo) => vo.id === voiceMap[activeLang]) && (
                <option value={voiceMap[activeLang]}>{voiceMap[activeLang]}（克隆）</option>
              )}
            </select>
            <input
              className="w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              value={refText}
              onChange={(e) => setRefText(e.target.value)}
              placeholder={activeLang === "cantonese" ? "参考音频对应的文字（录音/上传都要填），例如：你好，我係小博，有咩可以幫到你？" : "参考音频对应的文字（录音/上传都要填）"}
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
            <p className="rounded-lg bg-white/5 p-2 text-[11px] leading-relaxed text-[var(--muted)]">
              克隆出来的音色会讲什么语言/口音，由你录的参考音频决定：想让 AI 讲<b className="text-[var(--foreground)]">粤语</b>，就对着麦用粤语说一段参考语料（如上方的粤语示例）；用普通话参考音频克隆出的音色，读粤语文字也会带普通话音。克隆会存为独立音色，可随时回来试听。
            </p>
            <div className="flex gap-2">
              <button className="btn-ghost w-full" onClick={registerVoice} disabled={recording}>克隆并保存音色</button>
              <button className="btn-ghost w-full" onClick={previewVoice}>试听已选音色</button>
            </div>
            {previewUrl && <audio controls autoPlay src={previewUrl} className="mt-2 w-full" />}
            {clonedVoices.length > 0 && (
              <div className="rounded-lg border border-[var(--card-border)] p-2">
                <span className="text-xs text-[var(--stage-muted)]">已克隆音色</span>
                <ul className="mt-1.5 space-y-1">
                  {clonedVoices.map((cv) => (
                    <li key={cv.id} className="flex items-center justify-between gap-2 text-xs">
                      <span className="truncate text-[var(--foreground)]">
                        {cv.id}
                        {cv.lang ? `（${LANG_LABEL[cv.lang] ?? cv.lang}）` : ""}
                      </span>
                      <div className="flex shrink-0 gap-1">
                        <button
                          className="text-[var(--muted)] hover:text-[var(--accent)]"
                          onClick={async () => {
                            // 试听该克隆：临时切到对应语言标签并绑定，再试听。
                            if (cv.lang && ["zh", "cantonese", "en"].includes(cv.lang)) {
                              setActiveLang(cv.lang as "zh" | "cantonese" | "en");
                            }
                            setVoiceMap((prev) => ({ ...prev, [cv.lang && ["zh", "cantonese", "en"].includes(cv.lang) ? cv.lang : "zh"]: cv.id }));
                            setErr(null);
                            try {
                              const lang = cv.lang && ["zh", "cantonese", "en"].includes(cv.lang) ? cv.lang : "zh";
                              const blob = await api.previewTts({
                                text: previewTextFor(lang as "zh" | "cantonese" | "en", form.name),
                                voice: cv.id,
                                language: lang,
                              });
                              if (previewUrl) URL.revokeObjectURL(previewUrl);
                              setPreviewUrl(URL.createObjectURL(blob));
                            } catch (e) {
                              setErr(String(e));
                            }
                          }}
                        >
                          试听
                        </button>
                        <button
                          className="text-red-300 hover:text-red-200"
                          onClick={async () => {
                            if (!window.confirm(`确认删除克隆音色「${cv.id}」？\n已绑定该音色的人设会自动改为不绑定。`)) return;
                            setErr(null);
                            try {
                              await api.deleteTtsVoice(cv.id);
                              setClonedVoices((prev) => prev.filter((v) => v.id !== cv.id));
                              setVoiceMap((prev) => {
                                const next = { ...prev };
                                for (const k of Object.keys(next)) if (next[k] === cv.id) delete next[k];
                                return next;
                              });
                            } catch (e) {
                              setErr(`删除失败：${String(e)}`);
                            }
                          }}
                        >
                          删除
                        </button>
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            )}
              </>
            )}
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
