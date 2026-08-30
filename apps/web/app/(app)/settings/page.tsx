"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { ErrorState, LoadingState } from "@/components/app-shell";

type ProviderKind = "asr" | "llm" | "tts" | "vad";

const PROVIDER_OPTIONS: Record<ProviderKind, string[]> = {
  asr: ["qwen3_asr", "sherpa_sensevoice", "fake"],
  llm: ["ollama", "deepseek", "fake"],
  tts: ["qwen3_tts", "fake"],
  vad: ["silero", "fake"],
};

const EMPTY_FORM = {
  asr: { provider: "qwen3_asr", model: "Qwen/Qwen3-ASR-0.6B", base_url: "http://127.0.0.1:8787", backend: "transformers", language: "zh" },
  llm: { provider: "ollama", model: "huihui_ai/qwen3.5-abliterated:9b", base_url: "http://host.docker.internal:11434/v1", api_key: "ollama" },
  tts: { provider: "qwen3_tts", model: "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", base_url: "http://127.0.0.1:8788", speaker: "", speaker_zh: "", speaker_yue: "", speaker_en: "", instruct: "", sample_rate: 24000 },
  vad: { provider: "silero", model: "", sensitivity: 0.5 },
  policy: "offline_first",
};

function ProviderFields({
  kind,
  value,
  onChange,
}: {
  kind: ProviderKind;
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
}) {
  const fields: Record<ProviderKind, string[]> = {
    asr: ["model", "base_url", "backend", "language"],
    llm: ["model", "base_url", "api_key"],
    tts: ["model", "base_url", "speaker", "speaker_zh", "speaker_yue", "speaker_en", "instruct", "sample_rate"],
    vad: ["model", "sensitivity"],
  };

  return (
    <div className="mt-3 space-y-2">
      <select
        className="w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
        value={String(value.provider ?? PROVIDER_OPTIONS[kind][0])}
        onChange={(e) => onChange({ ...value, provider: e.target.value })}
      >
        {PROVIDER_OPTIONS[kind].map((p) => <option key={p}>{p}</option>)}
      </select>
      {fields[kind].map((field) => (
        <input
          key={field}
          className="w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
          placeholder={field}
          value={String(value[field] ?? "")}
          onChange={(e) => onChange({ ...value, [field]: e.target.value })}
        />
      ))}
    </div>
  );
}

export default function SettingsPage() {
  const [form, setForm] = useState<Record<string, any>>(EMPTY_FORM);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);
  const [health, setHealth] = useState("");

  useEffect(() => {
    api.getSettings().then((settings) => {
      setForm({
        ...EMPTY_FORM,
        ...(settings as Record<string, unknown>),
      });
    }).catch((e) => setErr(String(e))).finally(() => setLoading(false));
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
      setErr(String(e));
    }
  }

  async function testHealth(kind: "asr" | "tts") {
    setHealth("");
    try {
      const result = kind === "asr" ? await api.asrHealth() : await api.ttsHealth();
      setHealth(`${kind.toUpperCase()} OK: ${JSON.stringify(result)}`);
    } catch (e) {
      setHealth(`${kind.toUpperCase()} failed: ${e}`);
    }
  }

  return (
    <div>
      <div className="mb-8">
        <h1 className="page-title">设置</h1>
        <p className="page-sub">全局 Provider · 音频策略 · 合规</p>
      </div>

      {loading ? (
        <LoadingState />
      ) : (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          {(["asr", "llm", "tts", "vad"] as ProviderKind[]).map((kind) => (
            <section key={kind} className="card">
              <span className="label">{kind.toUpperCase()}</span>
              <ProviderFields
                kind={kind}
                value={form[kind] ?? {}}
                onChange={(next) => setForm({ ...form, [kind]: next })}
              />
            </section>
          ))}
          <section className="card">
            <span className="label">策略</span>
            <select
              className="mt-3 w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              value={form.policy ?? "offline_first"}
              onChange={(e) => setForm({ ...form, policy: e.target.value })}
            >
              <option value="offline_first">本地优先</option>
              <option value="cloud_first">云端优先</option>
            </select>
            <p className="mt-2 text-xs text-[var(--muted)]">全局配置保存后由 Agent 在下一次通话构建 provider 时读取。</p>
          </section>
          <div className="flex items-end gap-3 lg:col-span-2">
            <button className="btn-primary" onClick={save}>保存设置</button>
            <button className="btn-ghost" onClick={() => testHealth("asr")}>测试 ASR</button>
            <button className="btn-ghost" onClick={() => testHealth("tts")}>测试 TTS</button>
            {ok && <span className="text-sm text-emerald-400">已保存。</span>}
            {err && <ErrorState message={err} />}
            {health && <span className="text-sm text-[var(--muted)]">{health}</span>}
          </div>
        </div>
      )}
    </div>
  );
}
