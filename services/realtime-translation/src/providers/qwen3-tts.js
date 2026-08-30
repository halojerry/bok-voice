// Real TTS provider: calls the local Qwen3-TTS sidecar /v1/audio/speech
// (shared protocol with the A-line agent) and slices the returned PCM into
// scheduler chunks. No CosyVoice in the B-line path, per plan.

const DEFAULT_OPTS = {
  baseUrl: "http://127.0.0.1:8788",
  sampleRate: 24000,
  chunkMs: 100,
  timeoutMs: 120000,
};

function durationMsFor(pcm, sampleRate) {
  return Math.round((pcm.length / 2 / sampleRate) * 1000);
}

export class Qwen3TTSProvider {
  constructor(opts = {}) {
    this.opts = { ...DEFAULT_OPTS, ...opts };
    this.baseUrl = this.opts.baseUrl.replace(/\/$/, "");
  }

  async synthesize(text, targetLang) {
    if (!text) return [];
    const res = await fetch(`${this.baseUrl}/v1/audio/speech`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        input: text,
        language: targetLang || "Auto",
        sample_rate: this.opts.sampleRate,
      }),
      signal: AbortSignal.timeout(this.opts.timeoutMs),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new Error(`TTS HTTP ${res.status}: ${detail.slice(0, 200)}`);
    }
    const pcm = Buffer.from(await res.arrayBuffer());
    if (!pcm.length) return [];

    const chunkSamples = Math.round((this.opts.sampleRate * this.opts.chunkMs) / 1000);
    const chunks = [];
    for (let i = 0; i < pcm.length; i += chunkSamples * 2) {
      const slice = pcm.subarray(i, i + chunkSamples * 2);
      const isLast = i + chunkSamples * 2 >= pcm.length;
      chunks.push({
        durationMs: durationMsFor(slice, this.opts.sampleRate),
        audio: Buffer.from(slice),
        final: isLast,
      });
    }
    return chunks;
  }
}
