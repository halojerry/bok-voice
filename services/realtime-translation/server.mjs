// B-line realtime translation worker: WebSocket server that owns per-channel
// TranslationChannel state and streams subtitle / audio / metrics to the UI.
//
// Protocol (client -> server):
//   {type:"open_channel", channelId, sourceLang, targetLang, translatorProvider?}
//   {type:"audio", channelId, pcm: base64, sampleRate}
//   {type:"flush", channelId}
//   {type:"tick", channelId, advanceMs}
//   {type:"discard", channelId, uptoSourceSeqId}
//   {type:"clear", channelId}
//   {type:"close_channel", channelId}
// Server -> client:
//   {type:"channel_open", channelId} | {type:"error", channelId?, message}
//   {type:"subtitle", channelId, source, translated, sourceSeqId, at}
//   {type:"audio", channelId, ...PlaybackChunkTrace, pcm, sampleRate}
//   {type:"metrics", channelId, ...schedulerMetrics}

import { EventEmitter } from "node:events";
import { pathToFileURL } from "node:url";
import { WebSocketServer } from "ws";

import { TranslationChannel } from "./src/channel.js";
import { loadConfig } from "./src/config.js";
import { appendMetrics } from "./src/metrics.js";
import { EnergyVAD } from "./src/providers/energy-vad.js";
import { MockASR, MockTranslator, MockTTS } from "./src/providers/mock.js";
import { Qwen3ASRProvider } from "./src/providers/qwen3-asr.js";
import { Qwen3TTSProvider } from "./src/providers/qwen3-tts.js";
import { LocalOpenAITranslator } from "./src/providers/local-openai.js";
import { DashScopeTranslator } from "./src/providers/dashscope.js";

function send(ws, obj) {
  if (ws.readyState === ws.OPEN) ws.send(JSON.stringify(obj));
}

function buildTranslator(provider, config) {
  const t = config.translator;
  if (provider === "dashscope") {
    try {
      return new DashScopeTranslator();
    } catch (err) {
      console.warn("[worker] DashScope unavailable, falling back to local LLM:", err.message);
    }
  }
  return new LocalOpenAITranslator({
    baseUrl: t.base_url,
    model: t.model,
  });
}

export function createWorker(config, { metricsSink = appendMetrics, ttsVoices = {} } = {}) {
  const channels = new Map(); // channelId -> { channel, ws, timer }

  function openChannel(ws, msg) {
    if (!msg.channelId || !msg.sourceLang || !msg.targetLang) {
      return send(ws, { type: "error", message: "channelId/sourceLang/targetLang required" });
    }
    if (channels.has(msg.channelId)) {
      return send(ws, { type: "error", channelId: msg.channelId, message: "channel already open" });
    }

    const asrCfg = config.asr;
    const ttsCfg = config.tts;
    const emitter = new EventEmitter();
    const useMock = msg.mock === true;
    const channel = new TranslationChannel({
      id: msg.channelId,
      sourceLang: msg.sourceLang,
      targetLang: msg.targetLang,
      asr: useMock
        ? new MockASR()
        : new Qwen3ASRProvider({
            baseUrl: asrCfg.base_url,
            sampleRate: asrCfg.sample_rate || 16000,
            vad: new EnergyVAD({ sampleRate: asrCfg.sample_rate || 16000 }),
          }),
      translator: useMock ? new MockTranslator() : buildTranslator(msg.translatorProvider, config),
      tts: useMock ? new MockTTS() : new Qwen3TTSProvider({ baseUrl: ttsCfg.base_url, sampleRate: ttsCfg.sample_rate || 24000, voices: ttsVoices }),
      events: emitter,
    });

    emitter.on("subtitle", (ev) => send(ws, { type: "subtitle", ...ev }));
    emitter.on("audio", (ev) => send(ws, { type: "audio", ...ev }));
    emitter.on("metrics", (ev) => {
      send(ws, { type: "metrics", ...ev });
      metricsSink(config, ev);
    });

    const timer = setInterval(() => {
      try {
        channel.tick(Date.now(), 0);
      } catch {
        /* noop */
      }
    }, 500);

    channels.set(msg.channelId, { channel, ws, timer });
    send(ws, { type: "channel_open", channelId: msg.channelId, ok: true });
    console.log(`[worker] channel open: ${msg.channelId} (${msg.sourceLang}->${msg.targetLang})`);
  }

  function closeChannel(channelId, reason = "closed") {
    const entry = channels.get(channelId);
    if (!entry) return;
    clearInterval(entry.timer);
    entry.channel.clearAudioPlaybackChannel();
    channels.delete(channelId);
    console.log(`[worker] channel closed: ${channelId} (${reason})`);
  }

  const wss = new WebSocketServer({ host: config.server.host, port: config.server.port });

  wss.on("connection", (ws) => {
    ws.on("message", async (raw) => {
      let msg;
      try {
        msg = JSON.parse(String(raw));
      } catch {
        return send(ws, { type: "error", message: "invalid JSON" });
      }
      try {
        switch (msg.type) {
          case "open_channel":
            openChannel(ws, msg);
            break;
          case "audio": {
            const entry = channels.get(msg.channelId);
            if (!entry) return send(ws, { type: "error", channelId: msg.channelId, message: "channel not open" });
            const pcm = Buffer.from(msg.pcm || "", "base64");
            if (pcm.length) await entry.channel.pushAudio({ pcm, sampleRate: msg.sampleRate || 16000 });
            break;
          }
          case "flush": {
            const entry = channels.get(msg.channelId);
            if (entry) await entry.channel.flush();
            break;
          }
          case "tick": {
            const entry = channels.get(msg.channelId);
            if (entry) entry.channel.tick(Date.now(), Number(msg.advanceMs) || 0);
            break;
          }
          case "discard": {
            const entry = channels.get(msg.channelId);
            if (entry) entry.channel.discardQueuedAudio(Number(msg.uptoSourceSeqId) || 0);
            break;
          }
          case "clear": {
            const entry = channels.get(msg.channelId);
            if (entry) entry.channel.clearAudioPlaybackChannel();
            break;
          }
          case "close_channel":
            closeChannel(msg.channelId);
            break;
          default:
            send(ws, { type: "error", message: `unknown message type: ${msg.type}` });
        }
      } catch (err) {
        send(ws, { type: "error", channelId: msg.channelId, message: String(err?.message || err) });
        console.error(`[worker] ${msg.type} error:`, err);
      }
    });

    ws.on("close", () => {
      for (const [channelId, entry] of channels) {
        if (entry.ws === ws) closeChannel(channelId, "client disconnected");
      }
    });
  });

  return wss;
}

// 从 control-plane 拉取 TTS 三语言音色（speaker_zh/speaker_cantonese/en），让 B 线合成时按目标语言
// 选粤语克隆/预设音色（否则默认普通话 Vivian 念粤语会「夹生」）。失败降级空 map（不崩）。
// control-plane 固定本机 127.0.0.1:8000（A/B 线同栈）。
async function loadTtsVoices(config) {
  const cp = config.controlPlaneUrl || "http://127.0.0.1:8000";
  try {
    const r = await fetch(`${cp}/api/settings?internal=1`, { signal: AbortSignal.timeout(5000) });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const s = await r.json();
    const t = s.tts || {};
    // speaker_cantonese 为新键；speaker_yue 兼容旧数据（DB 迁移后只剩新键）。
    const voices = { zh: t.speaker_zh || "", yue: t.speaker_cantonese || t.speaker_yue || "", en: t.speaker_en || "" };
    console.log("[worker] tts voices (from control-plane):", JSON.stringify(voices));
    return voices;
  } catch (err) {
    console.warn("[worker] loadTtsVoices failed (voice fallback to default):", err?.message || err);
    return {};
  }
}

// 直接运行时监听；被测试 import 时不自动起服务。
if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  const config = loadConfig();
  const voices = await loadTtsVoices(config);
  createWorker(config, { ttsVoices: voices });
  console.log(`[worker] realtime translation listening on ws://${config.server.host}:${config.server.port}`);
}
