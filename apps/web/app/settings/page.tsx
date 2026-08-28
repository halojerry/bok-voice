"use client";

import { useState } from "react";

const PROVIDERS = [
  { id: "asr", label: "ASR", value: "sherpa（本地）", hint: "SenseVoice zh/en/ja/ko/yue" },
  { id: "llm", label: "LLM", value: "DeepSeek（云）", hint: "Ollama 本地兜底" },
  { id: "tts", label: "TTS", value: "火山流式", hint: "参考音频缓存 voice embedding" },
  { id: "vad", label: "VAD", value: "Silero（本地）", hint: "livekit-local-inference" },
];

export default function SettingsPage() {
  const [offline, setOffline] = useState(true);
  const [consent, setConsent] = useState(true);
  const [recording, setRecording] = useState(false);

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-xl font-semibold">设置</h1>
        <p className="mt-1 text-sm text-[var(--muted)]">账号 · Provider · 成本策略 · 合规</p>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section className="card">
          <span className="label">Provider 策略</span>
          <div className="mt-3 space-y-3">
            {PROVIDERS.map((p) => (
              <div key={p.id} className="flex items-center justify-between rounded-xl bg-white/5 px-4 py-3">
                <div>
                  <p className="text-sm font-medium">{p.label} · {p.value}</p>
                  <p className="text-xs text-[var(--muted)]">{p.hint}</p>
                </div>
                <span className="text-xs text-[var(--muted)]">会话级锁定</span>
              </div>
            ))}
          </div>
        </section>

        <section className="card space-y-4">
          <span className="label">全局</span>
          <div className="rounded-xl bg-white/5 p-4">
            <div className="flex items-center justify-between">
              <span className="text-sm">本地优先（offline_first）</span>
              <button className={`relative h-6 w-11 rounded-full transition ${offline ? "bg-[var(--accent)]" : "bg-neutral-700"}`} onClick={() => setOffline(!offline)}>
                <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white transition ${offline ? "left-5" : "left-0.5"}`} />
              </button>
            </div>
            <p className="mt-2 text-xs text-[var(--muted)]">本地优先，云作质量/故障降级；通话中不突切。</p>
          </div>
          <div className="rounded-xl bg-white/5 p-4">
            <div className="flex items-center justify-between">
              <span className="text-sm">通话录音同意</span>
              <button className={`relative h-6 w-11 rounded-full transition ${consent ? "bg-[var(--accent)]" : "bg-neutral-700"}`} onClick={() => setConsent(!consent)}>
                <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white transition ${consent ? "left-5" : "left-0.5"}`} />
              </button>
            </div>
            <p className="mt-2 text-xs text-[var(--muted)]">录音 consent 提示与开启控制。</p>
          </div>
          <div className="rounded-xl bg-white/5 p-4">
            <div className="flex items-center justify-between">
              <span className="text-sm">会话录音</span>
              <button className={`relative h-6 w-11 rounded-full transition ${recording ? "bg-[var(--accent)]" : "bg-neutral-700"}`} onClick={() => setRecording(!recording)}>
                <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white transition ${recording ? "left-5" : "left-0.5"}`} />
              </button>
            </div>
            <p className="mt-2 text-xs text-[var(--muted)]">PII 脱敏、保留期、删除/被遗忘。</p>
          </div>
        </section>
      </div>
    </div>
  );
}
