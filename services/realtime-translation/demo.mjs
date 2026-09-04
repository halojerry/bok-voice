// B-line local verification demo: two concurrent TranslationChannels with
// backlog -> chase -> discard -> clear, printing 金喜同传-style metrics.
import { TranslationChannel } from "./src/channel.js";
import { loadConfig } from "./src/config.js";
import { EnergyVAD } from "./src/providers/energy-vad.js";
import { MockASR, MockTranslator, MockTTS } from "./src/providers/mock.js";
import { LocalOpenAITranslator } from "./src/providers/local-openai.js";
import { Qwen3ASRProvider } from "./src/providers/qwen3-asr.js";
import { Qwen3TTSProvider } from "./src/providers/qwen3-tts.js";

function fmt(m) {
  return [
    `queueDepth=${m.queueDepth}`,
    `queuedAudioMs=${m.queuedAudioMs}`,
    `backlog=${m.queuedAudioMs - m.targetBufferMs}ms`,
    `chaseState=${m.chaseState}`,
    `chaseSpeed=${m.chaseSpeed}`,
    `droppedBlocks=${m.droppedBlocks}`,
    `droppedMs=${m.droppedMs}`,
  ].join("  ");
}

async function main() {
  const useReal = process.argv.includes("--real");
  const config = loadConfig();
  const channels = [
    new TranslationChannel({
      id: "ch-zh-en",
      sourceLang: "zh",
      targetLang: "en",
      asr: useReal
        ? new Qwen3ASRProvider({ baseUrl: config.asr.base_url, sampleRate: config.asr.sample_rate, vad: new EnergyVAD({ sampleRate: config.asr.sample_rate }) })
        : new MockASR(),
      translator: useReal
        ? new LocalOpenAITranslator({ baseUrl: config.translator.base_url, model: config.translator.model })
        : new MockTranslator(),
      tts: useReal ? new Qwen3TTSProvider({ baseUrl: config.tts.base_url, sampleRate: config.tts.sample_rate }) : new MockTTS(),
      maxQueueMs: 4000,
    }),
    new TranslationChannel({
      id: "ch-cantonese-zh",
      sourceLang: "cantonese",
      targetLang: "zh",
      asr: useReal
        ? new Qwen3ASRProvider({ baseUrl: config.asr.base_url, sampleRate: config.asr.sample_rate, vad: new EnergyVAD({ sampleRate: config.asr.sample_rate }) })
        : new MockASR(),
      translator: useReal
        ? new LocalOpenAITranslator({ baseUrl: config.translator.base_url, model: config.translator.model })
        : new MockTranslator(),
      tts: useReal ? new Qwen3TTSProvider({ baseUrl: config.tts.base_url, sampleRate: config.tts.sample_rate }) : new MockTTS(),
      maxQueueMs: 4000,
    }),
  ];

  console.log(`== B-line multi-channel demo (${useReal ? "REAL providers" : "mock providers"}) ==`);
  for (const channel of channels) {
    await channel.pushAudio("你好，我们支持离线粤语通话。");
    await channel.pushAudio("请问今天可以下单吗？");
    await channel.flush();
    console.log(`\n[${channel.id}] ${channel.sourceLang}->${channel.targetLang}`);
    console.log("  sentences:", channel.translateQueue.length, "ttsStream:", channel.ttsStream.length);
  }

  // Simulate sustained backlog so the scheduler enters chase then drops.
  console.log("\n== backlog / chase / drop ==");
  const scheduler = channels[0].playbackQueue;
  for (let i = 1; i <= 12; i++) {
    scheduler.enqueue({ sourceSeqId: i, durationMs: 1000, audio: `block-${i}` });
  }
  let m = scheduler.tick(1000, 50);
  console.log("tick1:", fmt(m));
  scheduler.tick(2000, 250);
  scheduler.tick(3000, 250);
  m = scheduler.tick(4000, 250);
  console.log("tick4:", fmt(m));
  console.log("chunk trace fields:", Object.keys(scheduler.queue[0] ?? {}).slice(0, 12).join(","));

  console.log("\n== discardQueuedAudio + clearAudioPlaybackChannel ==");
  scheduler.discardQueuedAudio(Math.floor(scheduler.queue.length / 2));
  m = scheduler.metrics();
  console.log("after discard:", fmt(m));
  channels[0].clearAudioPlaybackChannel();
  m = scheduler.metrics();
  console.log("after clear:", fmt(m));

  const ok =
    m.queueDepth === 0 &&
    scheduler.droppedBlocks > 0 &&
    scheduler.droppedMs > 0 &&
    channels[0].ttsStream.length > 0 &&
    channels[1].ttsStream.length > 0;
  console.log(ok ? "\nB_LINE_DEMO_PASSED" : "\nB_LINE_DEMO_FAILED");
  if (!ok) process.exitCode = 1;
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
