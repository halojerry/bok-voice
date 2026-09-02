"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { applyOutputDevice, savedMicDevice, savedOutputDevice } from "@/lib/audio";

const WS_URL = process.env.NEXT_PUBLIC_TRANSLATION_WS_URL || "ws://127.0.0.1:8790";
const LANGS = [
  { code: "zh", label: "普通话" },
  { code: "yue", label: "粤语" },
  { code: "en", label: "英语" },
];

type Subtitle = { source: string; translated: string; sourceSeqId: number; at: number };
type Metrics = {
  queueDepth: number;
  queuedAudioMs: number;
  playableBacklogMs: number;
  chaseState: string;
  chaseSpeed: number;
  droppedBlocks: number;
  droppedMs: number;
};

type Channel = {
  id: string;
  sourceLang: string;
  targetLang: string;
  provider: "local_openai" | "dashscope";
  running: boolean;
  subtitles: Subtitle[];
  metrics: Metrics | null;
  status: string;
};

let channelSeq = 0;

export default function TranslatePage() {
  const [wsState, setWsState] = useState<"connecting" | "open" | "closed">("connecting");
  const [channels, setChannels] = useState<Channel[]>([]);
  const [form, setForm] = useState({ sourceLang: "zh", targetLang: "en", provider: "local_openai" as Channel["provider"] });
  const wsRef = useRef<WebSocket | null>(null);
  const micRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const playbackQueuesRef = useRef<Record<string, { buffer: AudioBuffer; durationMs: number }[]>>({});
  const playingRef = useRef<Record<string, boolean>>({});
  const wsOpenRef = useRef(false);

  useEffect(() => {
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;
    ws.onopen = () => {
      wsOpenRef.current = true;
      setWsState("open");
    };
    ws.onclose = () => {
      wsOpenRef.current = false;
      setWsState("closed");
    };
    ws.onerror = () => setWsState("closed");
    ws.onmessage = (ev) => {
      const msg = JSON.parse(String(ev.data));
      if (msg.type === "subtitle") {
        setChannels((prev) =>
          prev.map((c) =>
            c.id === msg.channelId
              ? { ...c, subtitles: [...c.subtitles.slice(-19), { source: msg.source, translated: msg.translated, sourceSeqId: msg.sourceSeqId, at: msg.at }] }
              : c,
          ),
        );
      } else if (msg.type === "metrics") {
        setChannels((prev) =>
          prev.map((c) =>
            c.id === msg.channelId
              ? {
                  ...c,
                  metrics: {
                    queueDepth: msg.queueDepth,
                    queuedAudioMs: msg.queuedAudioMs,
                    playableBacklogMs: msg.playableBacklogMs,
                    chaseState: msg.chaseState,
                    chaseSpeed: msg.chaseSpeed,
                    droppedBlocks: msg.droppedBlocks,
                    droppedMs: msg.droppedMs,
                  },
                }
              : c,
          ),
        );
      } else if (msg.type === "audio") {
        schedulePlayback(msg);
      } else if (msg.type === "channel_open") {
        setChannels((prev) =>
          prev.map((c) => (c.id === msg.channelId ? { ...c, running: true, status: "通道已连接，麦克风就绪" } : c)),
        );
      } else if (msg.type === "error") {
        setChannels((prev) =>
          prev.map((c) => (c.id === msg.channelId ? { ...c, status: `错误: ${msg.message}` } : c)),
        );
      }
    };
    return () => {
      ws.close();
      micRef.current?.disconnect();
    };
  }, []);

  const send = useCallback((obj: unknown) => {
    if (wsRef.current && wsOpenRef.current) wsRef.current.send(JSON.stringify(obj));
  }, []);

  function schedulePlayback(msg: any) {
    const ctx = (audioCtxRef.current ||= new AudioContext());
    if (ctx.state === "suspended") void ctx.resume();
    const pcm = Uint8Array.from(atob(msg.pcm), (c) => c.charCodeAt(0));
    const int16 = new Int16Array(pcm.buffer, pcm.byteOffset, pcm.length / 2);
    const float = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) float[i] = int16[i] / 32768;
    const buffer = ctx.createBuffer(1, float.length, msg.sampleRate || 24000);
    buffer.copyToChannel(float, 0);
    (playbackQueuesRef.current[msg.channelId] ||= []).push({ buffer, durationMs: msg.durationMs || 0 });
    playNext(msg.channelId);
  }

  function playNext(channelId: string) {
    const ctx = audioCtxRef.current;
    if (!ctx || playingRef.current[channelId]) return;
    const queue = playbackQueuesRef.current[channelId] || [];
    const item = queue.shift();
    if (!item) return;
    playingRef.current[channelId] = true;
    const src = ctx.createBufferSource();
    src.buffer = item.buffer;
    src.connect(ctx.destination);
    src.onended = () => {
      playingRef.current[channelId] = false;
      if (item.durationMs > 0) send({ type: "tick", channelId, advanceMs: item.durationMs });
      playNext(channelId);
    };
    src.start();
  }

  async function startCapture(channelId: string) {
    // 输出跟随用户在设置里选的设备（桌面壳切系统默认输出 / Chromium setSinkId）。
    const outId = savedOutputDevice();
    if (outId) applyOutputDevice(outId).catch(() => {});
    const micId = savedMicDevice();
    const audioConstraints: MediaTrackConstraints = { channelCount: 1 };
    // 非 exact：保存的麦克风失效/已插拔时回退系统默认，避免 getUserMedia reject。
    if (micId) audioConstraints.deviceId = micId;
    const stream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints });
    const ctx = (audioCtxRef.current ||= new AudioContext({ sampleRate: 16000 }));
    const source = ctx.createMediaStreamSource(stream);
    const node = ctx.createScriptProcessor(4096, 1, 1);
    node.onaudioprocess = (e) => {
      const input = e.inputBuffer.getChannelData(0);
      const int16 = new Int16Array(input.length);
      for (let i = 0; i < input.length; i++) {
        const s = Math.max(-1, Math.min(1, input[i]));
        int16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      const bytes = new Uint8Array(int16.buffer);
      let bin = "";
      for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
      send({ type: "audio", channelId, pcm: btoa(bin), sampleRate: ctx.sampleRate });
    };
    source.connect(node);
    // WebAudio only pulls (processes) a ScriptProcessor if its output is fed
    // back to the destination. Route it through a zero-gain node so capture
    // keeps running without piping the microphone back into the speakers —
    // the previous direct `node.connect(ctx.destination)` caused echo/feedback.
    const mute = ctx.createGain();
    mute.gain.value = 0;
    node.connect(mute);
    mute.connect(ctx.destination);
    micRef.current = source;
  }

  async function startChannel(channel: Channel) {
    send({ type: "open_channel", channelId: channel.id, sourceLang: channel.sourceLang, targetLang: channel.targetLang, translatorProvider: channel.provider });
    try {
      await startCapture(channel.id);
      setChannels((prev) => prev.map((c) => (c.id === channel.id ? { ...c, status: "采集中…" } : c)));
    } catch (err) {
      setChannels((prev) => prev.map((c) => (c.id === channel.id ? { ...c, status: `麦克风失败: ${String(err)}` } : c)));
    }
  }

  function stopChannel(channel: Channel) {
    send({ type: "flush", channelId: channel.id });
    send({ type: "close_channel", channelId: channel.id });
    setChannels((prev) => prev.map((c) => (c.id === channel.id ? { ...c, running: false, status: "已停止" } : c)));
  }

  function addChannel() {
    const id = `ch-${++channelSeq}`;
    setChannels((prev) => [
      ...prev,
      { id, sourceLang: form.sourceLang, targetLang: form.targetLang, provider: form.provider, running: false, subtitles: [], metrics: null, status: "待启动" },
    ]);
  }

  const runningCount = useMemo(() => channels.filter((c) => c.running).length, [channels]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <p className="stage-eyebrow">REALTIME TRANSLATION</p>
          <h1 className="text-2xl font-semibold tracking-tight">同声传译</h1>
          <p className="mt-1 text-sm text-[var(--stage-muted)]">
            独立于客服 Agent 的 B 线：ASR → 原文字幕 → 翻译 → Qwen3-TTS → 播放/字幕
          </p>
        </div>
        <span className={`font-mono text-xs ${wsState === "open" ? "text-[var(--stage-value)]" : "text-red-300"}`}>
          WS {wsState.toUpperCase()} · {WS_URL}
        </span>
      </div>

      <div className="flex flex-wrap items-end gap-3 rounded-xl border border-[var(--card-border)] bg-[var(--card)]/60 p-4">
        <label className="text-sm">
          <span className="block text-xs text-[var(--stage-muted)]">源语言</span>
          <select className="mt-1 rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm" value={form.sourceLang} onChange={(e) => setForm({ ...form, sourceLang: e.target.value })}>
            {LANGS.map((l) => <option key={l.code} value={l.code}>{l.label}</option>)}
          </select>
        </label>
        <label className="text-sm">
          <span className="block text-xs text-[var(--stage-muted)]">目标语言</span>
          <select className="mt-1 rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm" value={form.targetLang} onChange={(e) => setForm({ ...form, targetLang: e.target.value })}>
            {LANGS.map((l) => <option key={l.code} value={l.code}>{l.label}</option>)}
          </select>
        </label>
        <label className="text-sm">
          <span className="block text-xs text-[var(--stage-muted)]">翻译引擎</span>
          <select className="mt-1 rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm" value={form.provider} onChange={(e) => setForm({ ...form, provider: e.target.value as Channel["provider"] })}>
            <option value="local_openai">本地 LLM</option>
            <option value="dashscope">DashScope Qwen-MT</option>
          </select>
        </label>
        <button className="btn-ghost" onClick={addChannel}>+ 添加通道</button>
        <span className="ml-auto font-mono text-xs text-[var(--stage-muted)]">{runningCount} 通道运行中</span>
      </div>

      {channels.length === 0 && <p className="text-sm text-[var(--stage-muted)]">还没有通道。选择语言对后点「添加通道」，再点「开始」。</p>}

      <div className="grid gap-4 lg:grid-cols-2">
        {channels.map((ch) => (
          <div key={ch.id} className="rounded-xl border border-[var(--card-border)] bg-[var(--card)]/60 p-4">
            <div className="mb-3 flex items-center justify-between">
              <div className="font-mono text-sm">
                <span className="text-[var(--stage-value)]">{LANGS.find((l) => l.code === ch.sourceLang)?.label}</span>
                <span className="mx-2 text-[var(--stage-muted)]">→</span>
                <span>{LANGS.find((l) => l.code === ch.targetLang)?.label}</span>
                <span className="ml-3 rounded bg-[var(--stage-muted)]/10 px-2 py-0.5 text-xs">{ch.provider}</span>
              </div>
              {ch.running ? (
                <button className="btn-ghost" onClick={() => stopChannel(ch)}>停止</button>
              ) : (
                <button className="stage-btn-primary" onClick={() => startChannel(ch)}>开始</button>
              )}
            </div>

            <p className="mb-3 text-xs text-[var(--stage-muted)]">{ch.status}</p>

            <div className="max-h-56 space-y-2 overflow-y-auto">
              {ch.subtitles.length === 0 && <p className="text-xs text-[var(--stage-muted)]">等待字幕…</p>}
              {ch.subtitles.map((s, i) => (
                <div key={`${s.sourceSeqId}-${i}`} className="rounded-lg border border-[var(--card-border)] bg-black/20 p-3">
                  <p className="text-sm text-[var(--stage-muted)]">{s.source}</p>
                  <p className="mt-1 text-sm text-[var(--stage-value)]">{s.translated}</p>
                </div>
              ))}
            </div>

            {ch.metrics && (
              <div className="mt-3 grid grid-cols-3 gap-2 border-t border-[var(--card-border)] pt-3 font-mono text-xs">
                <span>queueDepth <b className="text-[var(--stage-value)]">{ch.metrics.queueDepth}</b></span>
                <span>backlog <b className="text-[var(--stage-value)]">{ch.metrics.playableBacklogMs}ms</b></span>
                <span>chase <b className="text-[var(--stage-value)]">{ch.metrics.chaseState} ×{ch.metrics.chaseSpeed.toFixed(2)}</b></span>
                <span>queued <b className="text-[var(--stage-value)]">{ch.metrics.queuedAudioMs}ms</b></span>
                <span>dropped <b className="text-red-300">{ch.metrics.droppedBlocks}块/{ch.metrics.droppedMs}ms</b></span>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
