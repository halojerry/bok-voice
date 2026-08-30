import test from "node:test";
import assert from "node:assert/strict";

import { TranslationChannel } from "../src/channel.js";
import { MockASR, MockTranslator, MockTTS } from "../src/providers/mock.js";
import { PlaybackScheduler } from "../src/playback-scheduler.js";

test("TranslationChannel emits one translated playback chunk per sentence", async () => {
  const channel = new TranslationChannel({
    id: "ch-1",
    sourceLang: "zh",
    targetLang: "en",
    asr: new MockASR(),
    translator: new MockTranslator(),
    tts: new MockTTS(),
  });
  await channel.pushAudio("你好。");
  await channel.flush();
  assert.equal(channel.ttsStream.length, 1);
  assert.equal(channel.playbackQueue.queue.length, 1);
});

test("PlaybackScheduler drops old queued blocks when backlog exceeds limit", () => {
  const scheduler = new PlaybackScheduler({
    channelId: "ch-1",
    maxBacklogMs: 100,
    targetBufferMs: 0,
  });
  scheduler.enqueue({ sourceSeqId: 1, durationMs: 80, audio: "a" });
  scheduler.enqueue({ sourceSeqId: 2, durationMs: 80, audio: "b" });
  assert.ok(scheduler.droppedBlocks >= 1);
});

test("discardQueuedAudio removes up to the requested source sequence id", () => {
  const scheduler = new PlaybackScheduler({ channelId: "ch-1" });
  scheduler.enqueue({ sourceSeqId: 1, durationMs: 100, audio: "a" });
  scheduler.enqueue({ sourceSeqId: 2, durationMs: 100, audio: "b" });
  scheduler.discardQueuedAudio(1);
  assert.equal(scheduler.queue.length, 1);
  assert.equal(scheduler.queue[0].sourceSeqId, 2);
});

test("metrics expose queue depth, backlog and chase state", () => {
  const scheduler = new PlaybackScheduler({
    channelId: "ch-1",
    targetBufferMs: 500,
    maxBacklogMs: 10000,
  });
  scheduler.enqueue({ sourceSeqId: 1, durationMs: 800, audio: "a" });
  scheduler.enqueue({ sourceSeqId: 2, durationMs: 800, audio: "b" });
  const m = scheduler.tick(1000, 100);
  assert.equal(m.queueDepth, 2);
  assert.equal(typeof m.playableBacklogMs, "number");
  assert.ok(m.playableBacklogMs > 0);
  assert.ok(["idle", "chasing", "draining"].includes(m.chaseState));
  assert.ok(m.chaseSpeed >= 1.0);
});
