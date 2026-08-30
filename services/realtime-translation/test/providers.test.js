import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";

import { EnergyVAD } from "../src/providers/energy-vad.js";
import { Qwen3ASRProvider } from "../src/providers/qwen3-asr.js";
import { Qwen3TTSProvider } from "../src/providers/qwen3-tts.js";
import { OllamaTranslator } from "../src/providers/ollama.js";

function sinePcm(seconds, sampleRate = 16000, freq = 440, amp = 0.3) {
  const n = Math.round(seconds * sampleRate);
  const out = new Int16Array(n);
  for (let i = 0; i < n; i++) {
    out[i] = Math.round(amp * 32767 * Math.sin((2 * Math.PI * freq * i) / sampleRate));
  }
  return Buffer.from(out.buffer);
}

function silencePcm(seconds, sampleRate = 16000) {
  return Buffer.alloc(Math.round(seconds * sampleRate) * 2);
}

test("EnergyVAD splits speech segments on trailing silence and flushes tail", () => {
  const vad = new EnergyVAD({ sampleRate: 16000, minSilenceMs: 200, minSpeechMs: 100 });
  const audio = Buffer.concat([
    sinePcm(0.4), silencePcm(0.35), sinePcm(0.5), silencePcm(0.1), sinePcm(0.3),
  ]);
  const segs = vad.push(audio);
  assert.ok(segs && segs.length >= 1, "first segment emitted after 200ms silence");
  const tail = vad.flush();
  assert.ok(tail && tail.length === 1, "flush returns the final speech segment");
  assert.ok(tail[0].length > 0);
});

test("Qwen3ASRProvider calls start/chunk/finish and returns text", async () => {
  const calls = [];
  const server = http.createServer((req, res) => {
    calls.push(req.url);
    if (req.url === "/api/start") {
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ session_id: "s1" }));
    } else if (req.url?.startsWith("/api/chunk")) {
      req.resume();
      req.on("end", () => res.end(JSON.stringify({})));
    } else if (req.url?.startsWith("/api/finish")) {
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ text: "你好", language: "Chinese" }));
    } else {
      res.statusCode = 404;
      res.end();
    }
  });
  await new Promise((resolve) => server.listen(0, resolve));
  const port = server.address().port;
  try {
    const asr = new Qwen3ASRProvider({
      baseUrl: `http://127.0.0.1:${port}`,
      vad: new EnergyVAD({ sampleRate: 16000, minSilenceMs: 200, minSpeechMs: 100 }),
    });
    const text = await asr.flush(); // no VAD segment yet -> null
    assert.equal(text, null);
    // speech followed by enough silence -> VAD emits the segment -> transcribe
    const out = await asr.push(
      { pcm: Buffer.concat([sinePcm(0.3), silencePcm(0.35)]), sampleRate: 16000 },
      "zh",
    );
    assert.ok(out, "speech segment transcribed");
    assert.match(calls.join(" "), /api\/start/);
    assert.match(calls.join(" "), /api\/finish/);
  } finally {
    server.close();
  }
});

test("Qwen3TTSProvider slices PCM into chunks with durationMs and final", async () => {
  const pcm = Buffer.concat([sinePcm(0.3, 24000), sinePcm(0.2, 24000)]);
  const server = http.createServer((req, res) => {
    res.setHeader("content-type", "audio/pcm");
    res.end(pcm);
  });
  await new Promise((resolve) => server.listen(0, resolve));
  const port = server.address().port;
  try {
    const tts = new Qwen3TTSProvider({ baseUrl: `http://127.0.0.1:${port}`, sampleRate: 24000, chunkMs: 100 });
    const chunks = await tts.synthesize("hello", "en");
    assert.ok(chunks.length >= 3);
    assert.ok(chunks.every((c) => c.durationMs > 0 && Buffer.isBuffer(c.audio)));
    assert.equal(chunks.at(-1).final, true);
  } finally {
    server.close();
  }
});

test("OllamaTranslator posts translation request and returns content", async () => {
  let body = null;
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (d) => (raw += d));
    req.on("end", () => {
      body = JSON.parse(raw);
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ message: { role: "assistant", content: "Hello." } }));
    });
  });
  await new Promise((resolve) => server.listen(0, resolve));
  const port = server.address().port;
  try {
    const tr = new OllamaTranslator({ baseUrl: `http://127.0.0.1:${port}` });
    const out = await tr.translate("你好。", "zh", "en");
    assert.equal(out, "Hello.");
    assert.equal(body.stream, false);
    assert.equal(body.think, false);
    assert.match(body.messages.at(-1).content, /Target language: en/);
  } finally {
    server.close();
  }
});
