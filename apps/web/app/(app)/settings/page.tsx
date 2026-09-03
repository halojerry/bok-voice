"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { ErrorState, LoadingState } from "@/components/app-shell";
import DesktopStatus from "@/components/desktop-status";
import { SETTING_CARDS, POLICY_META, DEFAULT_PROVIDER, type ProviderKind, type FieldMeta } from "@/lib/settings-meta";
import {
  applyOutputDevice,
  isTauriShell,
  listAudioDevicesOf,
  requestMicPermission,
  savedMicDevice,
  savedOutputDevice,
  saveMicDevice,
  saveOutputDevice,
  webCanSwitchOutput,
  type AudioDeviceInfo,
} from "@/lib/audio";
import { friendlyErrorText } from "@/lib/api-ready";

type ProviderForm = Record<string, unknown> & { provider?: string };

const EMPTY_FORM: Record<ProviderKind, ProviderForm> & { policy: string } = {
  asr: { provider: DEFAULT_PROVIDER.asr },
  llm: { provider: DEFAULT_PROVIDER.llm },
  tts: { provider: DEFAULT_PROVIDER.tts, speaker: "", sample_rate: 24000 },
  vad: { provider: DEFAULT_PROVIDER.vad, max_buffered_speech: 15, min_speech_duration: 0.2, min_silence_duration: 0.35, sensitivity: 0.6, interruption: true },
  policy: "offline_first",
};

function FieldInput({
  field,
  value,
  onChange,
}: {
  field: FieldMeta;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const base =
    "w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]";
  if (field.type === "select") {
    const options = field.options ?? [];
    const isBool = options.some((o) => o.value === "true" || o.value === "false");
    const raw = value === undefined || value === null ? (isBool ? "true" : "") : String(value);
    return (
      <select
        className={`mt-1 ${base}`}
        value={raw}
        onChange={(e) => {
          const v = e.target.value;
          onChange(isBool ? v === "true" : v);
        }}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
        {raw && !options.some((o) => o.value === raw) && (
          <option key={`custom-${raw}`} value={raw}>
            自定义：{raw}
          </option>
        )}
      </select>
    );
  }
  return (
    <input
      type={field.type === "number" ? "number" : field.type === "secret" ? "password" : "text"}
      className={`mt-1 ${base}`}
      placeholder={field.placeholder ?? field.key}
      value={value === undefined || value === null ? "" : String(value)}
      min={field.min}
      max={field.max}
      step={field.step}
      onChange={(e) => onChange(field.type === "number" ? Number(e.target.value) : e.target.value)}
    />
  );
}

function ProviderCard({
  kind,
  value,
  onChange,
}: {
  kind: ProviderKind;
  value: ProviderForm;
  onChange: (next: ProviderForm) => void;
}) {
  const meta = SETTING_CARDS.find((c) => c.kind === kind)!;
  const provider = String(value.provider ?? DEFAULT_PROVIDER[kind]);
  const providerMeta = meta.providers.find((p) => p.value === provider);
  return (
    <section className="card">
      <span className="label">{meta.title}</span>
      <p className="mt-1 text-xs text-[var(--muted)]">{meta.desc}</p>
      <div className="mt-3 space-y-2">
        <div>
          <select
            className="w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
            value={provider}
            onChange={(e) => onChange({ ...value, provider: e.target.value })}
          >
            {meta.providers.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
          {providerMeta?.hint && <p className="mt-1 text-xs text-[var(--muted)]">{providerMeta.hint}</p>}
        </div>
        {meta.fields.filter((f) => !f.advanced).map((field) => (
          <label key={field.key} className="block">
            <span className="text-xs text-[var(--stage-muted)]">{field.label}</span>
            <FieldInput field={field} value={value[field.key]} onChange={(v) => onChange({ ...value, [field.key]: v })} />
            {field.hint && <p className="mt-1 text-xs text-[var(--muted)]">{field.hint}</p>}
            {field.preview && kind === "tts" && (
              <VoicePreview provider={provider} fieldKey={field.key} voice={String(value[field.key] ?? "")} />
            )}
          </label>
        ))}
        {meta.fields.some((f) => f.advanced) && (
          <details className="rounded-lg border border-[var(--card-border)] p-2 text-sm">
            <summary className="cursor-pointer text-xs text-[var(--muted)] hover:text-[var(--accent)]">
              高级（旧按语言分音色，仅兼容旧数据）
            </summary>
            <div className="mt-2 space-y-2">
              {meta.fields.filter((f) => f.advanced).map((field) => (
                <label key={field.key} className="block">
                  <span className="text-xs text-[var(--stage-muted)]">{field.label}</span>
                  <FieldInput field={field} value={value[field.key]} onChange={(v) => onChange({ ...value, [field.key]: v })} />
                  {field.hint && <p className="mt-1 text-xs text-[var(--muted)]">{field.hint}</p>}
                  {field.preview && kind === "tts" && (
                    <VoicePreview provider={provider} fieldKey={field.key} voice={String(value[field.key] ?? "")} />
                  )}
                </label>
              ))}
            </div>
          </details>
        )}
      </div>
    </section>
  );
}

/** 音色字段的「试听」按钮：调 /api/tts/preview 播放当前选中音色。 */
function VoicePreview({ provider, fieldKey, voice }: { provider: string; fieldKey: string; voice: string }) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  if (!voice) return null;
  async function play() {
    if (!voice) return;
    setBusy(true);
    setErr("");
    try {
      const lang = fieldKey === "speaker_yue" ? "yue" : fieldKey === "speaker_en" ? "en" : "zh";
      const text =
        lang === "yue"
          ? "你好，我係想問下件貨而家到咗邊度？"
          : lang === "en"
            ? "Hello, I'd like to ask about your delivery."
            : "你好，我想了解一下你们的产品和服务。";
      const blob = await api.previewTts({ provider, text, voice, language: lang, sample_rate: 24000 });
      if (url) URL.revokeObjectURL(url);
      const u = URL.createObjectURL(blob);
      setUrl(u);
      const el = new Audio(u);
      el.play().catch(() => {});
    } catch (e) {
      setErr(friendlyErrorText(String(e)));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="mt-1 flex items-center gap-2">
      <button className="btn-ghost px-2 py-0.5 text-[11px]" onClick={play} disabled={busy}>
        {busy ? "合成中…" : url ? "试听已选音色" : "试听"}
      </button>
      {url && <audio controls src={url} className="h-6 w-44" />}
      {err && <span className="text-[11px] text-red-300">{err}</span>}
    </div>
  );
}

function AudioDevicesCard() {
  const [mic, setMic] = useState<AudioDeviceInfo[]>([]);
  const [outs, setOuts] = useState<AudioDeviceInfo[]>([]);
  const [micId, setMicId] = useState("");
  const [outId, setOutId] = useState("");
  const [note, setNote] = useState("");

  const refresh = useCallback(async () => {
    const mics = await listAudioDevicesOf("input").catch(() => []);
    setMic(mics);
    const savedMic = savedMicDevice();
    if (savedMic && mics.some((m) => m.id === savedMic)) setMicId(savedMic);
    else if (mics.some((m) => m.is_default)) setMicId(mics.find((m) => m.is_default)!.id);
    else if (mics.length > 0) setMicId(mics[0].id);

    const outsArr = await listAudioDevicesOf("output").catch(() => []);
    setOuts(outsArr);
    const savedOut = savedOutputDevice();
    if (savedOut && outsArr.some((o) => o.id === savedOut)) setOutId(savedOut);
    else if (outsArr.some((o) => o.is_default)) setOutId(outsArr.find((o) => o.is_default)!.id);
    else if (outsArr.length > 0) setOutId(outsArr[0].id);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const canSetOutput = isTauriShell() || webCanSwitchOutput();

  return (
    <section className="card">
      <span className="label">音频设备</span>
      <p className="mt-1 text-xs text-[var(--muted)]">
        {isTauriShell()
          ? "桌面版扬声器切换的是系统默认输出设备（A 线通话与 B 线同传都会跟随）。"
          : "浏览器模式下仅 Chromium 内核支持切换扬声器输出。"}
      </p>
      <div className="mt-3 space-y-3">
        <div>
          <span className="text-xs text-[var(--stage-muted)]">麦克风（输入）</span>
          <div className="mt-1 flex gap-2">
            <select
              className="flex-1 rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              value={micId}
              onChange={(e) => { setMicId(e.target.value); saveMicDevice(e.target.value); }}
            >
              {mic.length === 0 && <option value="">未检测到麦克风</option>}
              {mic.map((m) => (
                <option key={m.id} value={m.id}>{m.name}{m.is_default ? "（系统默认）" : ""}</option>
              ))}
            </select>
            <button
              className="btn-ghost text-xs"
              onClick={async () => {
                const ok = await requestMicPermission();
                setNote(ok ? "麦克风权限已开启，正在刷新设备…" : "麦克风权限被拒绝。请在 系统设置 › 隐私与安全性 › 麦克风 中允许本应用。");
                await refresh();
              }}
            >
              刷新
            </button>
          </div>
          {mic.length === 0 && (
            <p className="mt-1 text-xs text-red-300">
              未检测到麦克风或未授权。请先点击「刷新」授权；若仍为空，到系统设置开启麦克风权限后重启应用。
            </p>
          )}
        </div>

        <div>
          <span className="text-xs text-[var(--stage-muted)]">扬声器 / 输出</span>
          {canSetOutput ? (
            <select
              className="mt-1 w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              value={outId}
              onChange={(e) => {
                const id = e.target.value;
                setOutId(id);
                saveOutputDevice(id);
                void applyOutputDevice(id);
              }}
            >
              {outs.length === 0 && <option value="">未检测到输出设备</option>}
              {outs.map((o) => (
                <option key={o.id} value={o.id}>{o.name}{o.is_default ? "（系统默认）" : ""}</option>
              ))}
            </select>
          ) : (
            <p className="mt-1 text-xs text-[var(--muted)]">
              当前浏览器（WebKit）不支持网页切换扬声器。输出跟随系统默认设备，请在系统声音设置中选择。
            </p>
          )}
        </div>
        {note && <p className="text-xs text-[var(--muted)]">{note}</p>}
      </div>
    </section>
  );
}

export default function SettingsPage() {
  const [form, setForm] = useState<Record<string, any>>(EMPTY_FORM);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);
  const [health, setHealth] = useState("");

  useEffect(() => {
    api.getSettings()
      .then((settings) => {
        const s = settings as Record<string, any>;
        setForm({
          asr: { ...EMPTY_FORM.asr, ...(s.asr ?? {}) },
          llm: { ...EMPTY_FORM.llm, ...(s.llm ?? {}) },
          tts: { ...EMPTY_FORM.tts, ...(s.tts ?? {}) },
          vad: { ...EMPTY_FORM.vad, ...(s.vad ?? {}) },
          policy: s.policy ?? "offline_first",
        });
      })
      .catch((e) => setErr(friendlyErrorText(String(e))))
      .finally(() => setLoading(false));
  }, []);

  async function save() {
    setErr(null);
    setOk(false);
    try {
      const payload = {
        asr: { ...EMPTY_FORM.asr, ...form.asr },
        llm: { ...EMPTY_FORM.llm, ...form.llm },
        tts: { ...EMPTY_FORM.tts, ...form.tts },
        vad: { ...EMPTY_FORM.vad, ...form.vad },
        policy: form.policy ?? "offline_first",
      };
      await api.saveSettings(payload);
      setOk(true);
    } catch (e) {
      setErr(friendlyErrorText(String(e)));
    }
  }

  async function testHealth(kind: "asr" | "tts") {
    setHealth("");
    try {
      const result = kind === "asr" ? await api.asrHealth() : await api.ttsHealth();
      setHealth(`${kind.toUpperCase()} OK: ${JSON.stringify(result)}`);
    } catch (e) {
      setHealth(`${kind.toUpperCase()} failed: ${friendlyErrorText(String(e))}`);
    }
  }

  const policyValue = form.policy ?? "offline_first";
  const policyOption = POLICY_META.options.find((o) => o.value === policyValue);

  const kindCards = useMemo(() => SETTING_CARDS.map((c) => c.kind), []);

  return (
    <div>
      <div className="mb-8">
        <h1 className="page-title">设置</h1>
        <p className="page-sub">引擎 Provider · VAD 与打断 · 音频设备 · 运行策略</p>
      </div>

      {loading ? (
        <LoadingState />
      ) : (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          {kindCards.map((kind) => (
            <ProviderCard key={kind} kind={kind} value={form[kind] ?? {}} onChange={(next) => setForm({ ...form, [kind]: next })} />
          ))}
          <AudioDevicesCard />
          <section className="card">
            <span className="label">{POLICY_META.title}</span>
            <select
              className="mt-3 w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              value={policyValue}
              onChange={(e) => setForm({ ...form, policy: e.target.value })}
            >
              {POLICY_META.options.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
            {policyOption?.hint && <p className="mt-2 text-xs text-[var(--muted)]">{policyOption.hint}</p>}
            <p className="mt-2 text-xs text-[var(--muted)]">策略与 Provider 会在下一次建立通话时应用到 Agent 会话。</p>
          </section>
          <div className="flex flex-wrap items-end gap-3 lg:col-span-2">
            <button className="btn-primary" onClick={save}>保存设置</button>
            <button className="btn-ghost" onClick={() => testHealth("asr")}>测试 ASR</button>
            <button className="btn-ghost" onClick={() => testHealth("tts")}>测试 TTS</button>
            {ok && <span className="text-sm text-emerald-400">已保存。</span>}
            {health && <span className="text-sm text-[var(--muted)]">{health}</span>}
          </div>
          {err && <div className="lg:col-span-2"><ErrorState message={err} /></div>}
          <div className="lg:col-span-2">
            <DesktopStatus />
          </div>
        </div>
      )}
    </div>
  );
}
