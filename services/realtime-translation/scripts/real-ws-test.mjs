// B 线真实链路集成验证（需 sidecar 8787/8788 + worker 8790 已启动）：
//   测试音频 → WS audio 分块 → EnergyVAD 切句 → Qwen3-ASR →
//   本地 LLM 翻译（OpenAI 兼容 :1235）→ Qwen3-TTS → subtitle/audio 事件 + metrics JSONL
// 运行：node scripts/real-ws-test.mjs

import { readFileSync, existsSync, statSync } from "node:fs";
import os from "node:os";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import WebSocket from "ws";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const WS_URL = process.env.WS_URL || "ws://127.0.0.1:8790";
const AUDIO_DIR = resolve(ROOT, "tests/fixtures/audio");
// 打包/统一编排后指标写入 app-data（bundle 只读），测试跟随同一路径。
const APP_DATA = process.env.LOCALAPPDATA
  ? resolve(process.env.LOCALAPPDATA, "BokVoice")
  : resolve(os.homedir(), "Library", "Application Support", "BokVoice");
const METRICS_FILE = resolve(APP_DATA, "translation-metrics.jsonl");
const CHANNELS = [
  { id: "real-zh-en", sourceLang: "zh", targetLang: "en", file: "zh.wav" },
  { id: "real-cantonese-zh", sourceLang: "cantonese", targetLang: "zh", file: "cantonese.wav" },
];

function readWavPcm16(path, maxSeconds = 5) {
  const buf = readFileSync(path);
  if (buf.toString("ascii", 0, 4) !== "RIFF") throw new Error(`${path} not RIFF`);
  const dataOffset = buf.indexOf("data");
  const fmt = buf.indexOf("fmt ");
  const channels = buf.readUInt16LE(fmt + 10);
  const sampleRate = buf.readUInt32LE(fmt + 12);
  const bits = buf.readUInt16LE(fmt + 22);
  if (channels !== 1 || bits !== 16) throw new Error(`${path}: need mono 16-bit (got ${channels}ch/${bits}bit)`);
  const pcm = buf.subarray(dataOffset + 8);
  const maxBytes = maxSeconds * sampleRate * 2;
  return { pcm: pcm.subarray(0, Math.min(pcm.length, maxBytes)), sampleRate };
}

function assert(cond, label, extra = "") {
  console.log(`[${cond ? "PASS" : "FAIL"}] ${label}${extra ? " " + extra : ""}`);
  if (!cond) process.exitCode = 1;
}

function openChannel(ws, ch) {
  return new Promise((resolvePromise, reject) => {
    const timer = setTimeout(() => reject(new Error(`channel_open timeout ${ch.id}`)), 15000);
    const onMsg = (raw) => {
      const m = JSON.parse(String(raw));
      if (m.type === "channel_open" && m.channelId === ch.id) {
        clearTimeout(timer);
        ws.off("message", onMsg);
        resolvePromise();
      } else if (m.type === "error" && m.channelId === ch.id) {
        clearTimeout(timer);
        ws.off("message", onMsg);
        reject(new Error(m.message));
      }
    };
    ws.on("message", onMsg);
    ws.send(JSON.stringify({ type: "open_channel", channelId: ch.id, sourceLang: ch.sourceLang, targetLang: ch.targetLang }));
  });
}

async function runChannel(ws, ch) {
  const { pcm, sampleRate } = readWavPcm16(resolve(AUDIO_DIR, ch.file));
  const events = [];
  const listener = (raw) => {
    const m = JSON.parse(String(raw));
    if (m.channelId === ch.id) events.push(m);
  };
  ws.on("message", listener);

  await openChannel(ws, ch);
  const CHUNK = 3200;
  for (let i = 0; i < pcm.length; i += CHUNK) {
    const slice = pcm.subarray(i, i + CHUNK);
    ws.send(JSON.stringify({ type: "audio", channelId: ch.id, pcm: slice.toString("base64"), sampleRate }));
  }
  ws.send(JSON.stringify({ type: "flush", channelId: ch.id }));

  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    const subtitle = events.find((e) => e.type === "subtitle");
    const audio = events.find((e) => e.type === "audio");
    if (subtitle && audio && events.some((e) => e.type === "metrics" && typeof e.droppedBlocks === "number")) break;
    await new Promise((r) => setTimeout(r, 1000));
  }
  ws.off("message", listener);

  const subtitle = events.find((e) => e.type === "subtitle");
  const audio = events.find((e) => e.type === "audio");
  const metrics = events.filter((e) => e.type === "metrics").at(-1);
  const err = events.find((e) => e.type === "error");

  console.log(`\n== ${ch.id} (${ch.sourceLang}->${ch.targetLang}) ==`);
  if (subtitle) console.log("  源:", subtitle.source.slice(0, 60));
  if (subtitle) console.log("  译:", subtitle.translated.slice(0, 60));
  assert(!!subtitle && subtitle.source.trim().length > 0, `${ch.id} subtitle source`);
  assert(!!subtitle && subtitle.translated.trim().length > 0, `${ch.id} subtitle translated`);
  assert(!!audio && typeof audio.pcm === "string" && audio.pcm.length > 100, `${ch.id} audio pcm bytes>0`, audio ? `${audio.pcm.length} b64 chars` : "");
  assert(!!audio && ["idle", "chasing", "draining"].includes(audio.chaseState), `${ch.id} audio trace chaseState`, audio?.chaseState);
  assert(!!metrics && typeof metrics.queueDepth === "number", `${ch.id} metrics queueDepth`, metrics ? `depth=${metrics.queueDepth} backlog=${metrics.playableBacklogMs}ms droppedMs=${metrics.droppedMs}` : "");
  assert(!err, `${ch.id} no error`, err?.message);
  ws.send(JSON.stringify({ type: "close_channel", channelId: ch.id }));
  return events.length;
}

const ws = new WebSocket(WS_URL);
await new Promise((resolvePromise, reject) => {
  ws.on("open", resolvePromise);
  ws.on("error", reject);
});
console.log(`connected ${WS_URL}`);

const metricsBefore = existsSync(METRICS_FILE) ? statSync(METRICS_FILE).size : 0;
let total = 0;
for (const ch of CHANNELS) total += await runChannel(ws, ch);

await new Promise((r) => setTimeout(r, 1500)); // 让最后一批 metrics 落盘
const metricsAfter = existsSync(METRICS_FILE) ? statSync(METRICS_FILE).size : 0;
assert(metricsAfter > metricsBefore, "metrics JSONL appended", `${metricsBefore}B -> ${metricsAfter}B`);

ws.close();
console.log(`\nB_LINE_REAL_WS ${total} events`);
if (process.exitCode) console.log("B_LINE_REAL_WS FAILED");
else console.log("B_LINE_REAL_WS PASSED");
