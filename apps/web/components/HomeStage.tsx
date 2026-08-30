"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import { LocalAudioTrack } from "livekit-client";
import { StageHeader } from "@/components/StageHeader";
import { VoiceAgentInterface } from "@/components/VoiceAgentInterface";
import { api } from "@/lib/api";

/* —— 舞台内容：左遥测 / 中可视化 / 右转写 —— */

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="stage-sep">
      <h2 className="stage-title">{title}</h2>
      <div>{children}</div>
    </div>
  );
}

function Row({ k, v, indent }: { k: string; v: string; indent?: boolean }) {
  return (
    <div className={`stage-row ${indent ? "border-l border-[var(--card-border)] pl-3" : ""}`}>
      <span className="stage-key">{k}</span>
      <span className="stage-value stage-glow">{v}</span>
    </div>
  );
}

const BENCHMARKS = [
  { k: "Task completion", v: "88/100" },
  { k: "Latency", v: "381ms" },
];

const AGENT_CONFIG = [
  { k: "VAD", v: "Silero" },
  { k: "Speech-to-text", v: "Qwen3-ASR" },
  { k: "model", v: "Qwen3-ASR-0.6B", indent: true },
  { k: "LLM", v: "Ollama" },
  { k: "model", v: "huihui_ai/qwen3.5-abliterated:9b", indent: true },
  { k: "Text-to-speech", v: "Qwen3-TTS" },
  { k: "model", v: "Qwen3-TTS-1.7B", indent: true },
  { k: "Voice", v: "zh/yue/en voice map" },
];

const ENHANCEMENTS = [
  { k: "Turn detection", v: "True" },
  { k: "Noise cancellation", v: "True" },
  { k: "Expressiveness", v: "True" },
];

const LATENCY = [
  { k: "Speech-to-text", v: "—" },
  { k: "End of turn", v: "534ms" },
  { k: "LLM", v: "1281ms" },
  { k: "Text-to-speech", v: "317ms" },
  { k: "Overall", v: "1682ms" },
];

const DEMO_TRANSCRIPT: { who: "AGENT" | "USER"; text: string }[] = [
  { who: "AGENT", text: "您好，我是 Bok 语音助手，协助您了解我们的多账号客服产品。我们该怎么开始呢？" },
  { who: "USER", text: "我可以选择语言，但 Python 通常是搭建原型最快的方式。我们是用 Python 还是 TypeScript？" },
  { who: "AGENT", text: "你把脚本完全反过来了：我已经开始写了，现在轮到你来选语言。Python 还是 TypeScript？" },
  { who: "AGENT", text: "好的，好，你赢了这轮。我先暂停复读。" },
];

export function HomeStage() {
  const [micOn, setMicOn] = useState(false);
  const [micTrack, setMicTrack] = useState<LocalAudioTrack | null>(null);
  const [live, setLive] = useState<{ active?: number }>({});
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.activeCalls(), api.health()])
      .then(([calls, health]) => {
        setLive({ active: Array.isArray(calls) ? calls.length : 0 });
        if (health && health.ok === false) setErr("控制面未就绪");
      })
      .catch(() => {
        /* 控制面离线：保持演示态 */
      });
  }, []);

  // 麦克风：真实电平喂给官方点阵（LocalAudioTrack），失败静默回退演示态。
  async function toggleMic() {
    if (micOn) {
      micTrack?.stop();
      setMicTrack(null);
      setMicOn(false);
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const track = new LocalAudioTrack(stream.getAudioTracks()[0]);
      setMicTrack(track);
      setMicOn(true);
    } catch {
      /* 无权限/失败：忽略 */
    }
  }

  useEffect(() => {
    return () => {
      micTrack?.stop();
    };
  }, [micTrack]);

  const active = live.active ?? 0;
  const listening = micOn;

  return (
    <div className="stage-shell min-h-screen w-full">
      {/* 全站统一顶栏 */}
      <StageHeader
        status={
          <>
            <span className={`h-2 w-2 rounded-full ${listening ? "bg-emerald-400 animate-pulse" : "bg-[var(--stage-value)]"}`} />
            <span className="font-mono">{listening ? "live" : "demo"} · v0.1.0</span>
          </>
        }
      />

      {/* 主舞台：左遥测 / 中可视化 / 右转写 */}
      <main className="mx-auto flex w-full max-w-7xl flex-col gap-6 px-6 pb-10 pt-2 lg:flex-row lg:items-stretch lg:gap-4 lg:px-10">
        {/* 左：遥测 */}
        <aside className="order-2 w-full shrink-0 lg:order-none lg:w-56">
          <div className="stage-col min-h-[280px]">
            <div className="stage-col-inner p-4">
              <Section title="Benchmarks">
                {BENCHMARKS.map((r) => (
                  <Row key={r.k} {...r} />
                ))}
              </Section>
              <Section title="Agent configuration">
                {AGENT_CONFIG.map((r) => (
                  <Row key={r.k} {...r} />
                ))}
              </Section>
              <Section title="Enhancements">
                {ENHANCEMENTS.map((r) => (
                  <Row key={r.k} {...r} />
                ))}
              </Section>
              <Section title="Latency">
                {LATENCY.map((r) => (
                  <Row key={r.k} {...r} />
                ))}
              </Section>
            </div>
          </div>
        </aside>

        {/* 中：可视化 + 控制条 */}
        <section className="order-1 flex min-w-0 flex-1 flex-col items-center justify-center gap-5 py-2 lg:order-none">
          <div className="relative flex h-[min(46vh,470px)] w-full max-w-[680px] items-center justify-center overflow-hidden rounded border border-[var(--card-border)] bg-black/30">
            <VoiceAgentInterface
              state={micTrack ? "speaking" : "connecting"}
              audioTrack={micTrack ?? undefined}
              size="lg"
            />
            <div className="pointer-events-none absolute left-4 top-4 flex items-center gap-2">
              <span className={`h-2 w-2 rounded-full ${listening ? "bg-emerald-400 animate-pulse" : "bg-[var(--stage-value)]"}`} />
              <span className="font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-[var(--stage-muted)]">
                {listening ? "Live · listening" : "Live · demo"}
              </span>
            </div>
            {active > 0 && (
              <div className="pointer-events-none absolute right-4 top-4 rounded-full border border-[var(--card-border)] bg-black/30 px-3 py-1 font-mono text-xs text-[var(--stage-value)]">
                {active} 路活跃
              </div>
            )}
          </div>

          {/* 控制条：mic(+chevron) + 挂断（对齐官方 VoiceAssistantControlBar） */}
          <div className="flex items-center gap-3">
            <div
              className={`flex h-9 items-center overflow-hidden rounded border ${
                micOn ? "border-[var(--stage-value)] bg-[var(--accent-soft)]" : "border-[var(--card-border)] bg-[var(--card)]"
              }`}
            >
              <button
                type="button"
                onClick={toggleMic}
                className={`flex h-9 items-center gap-1.5 px-3 transition ${micOn ? "text-[var(--stage-value)]" : "text-[var(--foreground)] hover:bg-[#141515]"}`}
                aria-pressed={micOn}
                title={micOn ? "关闭麦克风" : "开启麦克风（驱动可视化）"}
              >
                <MicIcon />
              </button>
              <button
                type="button"
                className="flex h-9 items-center border-l border-[var(--card-border)] px-1.5 text-[var(--stage-muted)] hover:text-[var(--foreground)]"
                aria-label="更多选项"
              >
                <ChevronIcon />
              </button>
            </div>

            <span className="stage-key w-16 text-center">{listening ? "聆听中" : "演示中"}</span>

            <button
              type="button"
              onClick={() => setMicOn(false)}
              className="flex h-9 w-9 items-center justify-center justify-items-center rounded border border-[var(--card-border)] bg-[var(--card)] text-red-400 transition hover:bg-red-500/10"
              title="挂断"
            >
              <HangupIcon />
            </button>
          </div>

          {err && <p className="text-xs text-[var(--stage-muted)]">{err}</p>}
        </section>

        {/* 右：转写 */}
        <aside className="order-3 w-full shrink-0 lg:order-none lg:w-56">
          <div className="stage-col flex min-h-[280px] flex-col">
            <div className="stage-col-inner flex min-h-0 flex-1 flex-col p-4">
              <h2 className="stage-title flex items-center justify-between">
                Transcription
                <span className="inline-flex items-center gap-1.5 text-[9px] tracking-[0.16em] text-[var(--stage-value-dim)]">
                  <span className="h-1.5 w-1.5 rounded-full bg-[var(--stage-value)] animate-pulse" />
                  streaming
                </span>
              </h2>
              <div className="mt-1 flex-1 space-y-2.5 overflow-y-auto font-mono text-[10px] font-bold leading-relaxed tracking-wider">
                {DEMO_TRANSCRIPT.map((t, i) => (
                  <p
                    key={i}
                    className={`stage-glow ${t.who === "AGENT" ? "text-[var(--stage-value)]" : "text-[var(--foreground)]"}`}
                  >
                    {t.who}: {t.text}
                  </p>
                ))}
              </div>
            </div>
            <div className="shrink-0 p-4 pt-0">
              <Link href="/supervisor" className="stage-btn-ghost w-full">
                查看全部 agent
              </Link>
            </div>
          </div>
        </aside>
      </main>
    </div>
  );
}

function MicIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className="h-4 w-4">
      <path d="M12 19v3" />
      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
      <rect x="9" y="2" width="6" height="13" rx="3" />
    </svg>
  );
}

function ChevronIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className="h-3.5 w-3.5">
      <path d="m6 9 6 6 6-6" />
    </svg>
  );
}

function HangupIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" className="h-4 w-4">
      <path d="M4.75 4.75 19.25 19.25M19.25 4.75 4.75 19.25" />
    </svg>
  );
}
