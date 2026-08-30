import test from "node:test";
import assert from "node:assert/strict";
import WebSocket from "ws";

import { createWorker } from "../server.mjs";

test("worker streams subtitle + audio events over WebSocket (mock channel)", async () => {
  const wss = createWorker(
    {
      asr: { base_url: "http://127.0.0.1:1", sample_rate: 16000 },
      translator: { base_url: "http://127.0.0.1:1", model: "m", think: false },
      tts: { base_url: "http://127.0.0.1:1", sample_rate: 24000 },
      server: { port: 0, metrics_file: null },
    },
    { metricsSink: () => {} },
  );
  await new Promise((resolve) => wss.on("listening", resolve));
  const port = wss.address().port;

  const ws = new WebSocket(`ws://127.0.0.1:${port}`);
  const events = [];
  ws.on("message", (raw) => events.push(JSON.parse(String(raw))));
  await new Promise((resolve, reject) => {
    ws.on("open", resolve);
    ws.on("error", reject);
  });

  ws.send(
    JSON.stringify({
      type: "open_channel",
      channelId: "ch1",
      sourceLang: "zh",
      targetLang: "en",
      mock: true,
    }),
  );
  await new Promise((resolve) => setTimeout(resolve, 200));

  ws.send(
    JSON.stringify({
      type: "audio",
      channelId: "ch1",
      pcm: Buffer.from("你好。", "utf8").toString("base64"),
      sampleRate: 16000,
    }),
  );
  ws.send(JSON.stringify({ type: "flush", channelId: "ch1" }));

  await new Promise((resolve) => setTimeout(resolve, 500));

  const open = events.find((e) => e.type === "channel_open");
  const subtitle = events.find((e) => e.type === "subtitle");
  const audio = events.find((e) => e.type === "audio");

  assert.ok(open?.ok, "channel_open received");
  assert.ok(subtitle, "subtitle event received");
  assert.equal(subtitle.source, "你好。");
  assert.match(subtitle.translated, /\[zh->en\]/);
  assert.ok(audio, "audio event received");
  assert.equal(typeof audio.playableBacklogMs, "number");
  assert.equal(typeof audio.queueDepth, "number");
  assert.equal(typeof audio.chaseState, "string");

  ws.send(JSON.stringify({ type: "close_channel", channelId: "ch1" }));
  ws.close();
  wss.close();
});
