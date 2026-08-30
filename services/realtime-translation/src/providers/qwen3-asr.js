// Real ASR provider: wraps the local Qwen3-ASR sidecar (batch transcribe per
// VAD segment). Sentence boundarying comes from the EnergyVAD; the sidecar's
// /api/start|chunk|finish protocol is shared with the A-line agent.

import { EnergyVAD } from "./energy-vad.js";

function resample16k(pcm, sampleRate, targetRate) {
  if (sampleRate === targetRate) return pcm;
  const src = new Int16Array(pcm.buffer, pcm.byteOffset, pcm.length / 2);
  const outLen = Math.round((src.length * targetRate) / sampleRate);
  const out = new Int16Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const pos = (i * src.length) / outLen;
    const i0 = Math.floor(pos);
    const i1 = Math.min(i0 + 1, src.length - 1);
    const frac = pos - i0;
    out[i] = Math.round(src[i0] * (1 - frac) + src[i1] * frac);
  }
  return Buffer.from(out.buffer);
}

export class Qwen3ASRProvider {
  constructor({ baseUrl = "http://127.0.0.1:8787", sampleRate = 16000, timeoutMs = 30000, vad } = {}) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.sampleRate = sampleRate;
    this.timeoutMs = timeoutMs;
    this.vad = vad || new EnergyVAD({ sampleRate });
    this.fetch = globalThis.fetch;
  }

  _signal() {
    return AbortSignal.timeout(this.timeoutMs);
  }

  async _json(path, init = {}) {
    const res = await this.fetch(`${this.baseUrl}${path}`, { signal: this._signal(), ...init });
    if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
    return res.json();
  }

  async push(chunk, sourceLang) {
    const segments = this.vad.push(chunk.pcm, chunk.sampleRate || this.sampleRate);
    if (!segments) return null;
    const texts = [];
    for (const seg of segments) {
      if (!seg || !seg.length) continue;
      const r = await this._transcribe(seg);
      if (r.text) texts.push(r.text);
    }
    return texts.length ? texts.join(" ") : null;
  }

  async flush(sourceLang) {
    const segments = this.vad.flush();
    if (!segments) return null;
    const texts = [];
    for (const seg of segments) {
      if (!seg || !seg.length) continue;
      const r = await this._transcribe(seg);
      if (r.text) texts.push(r.text);
    }
    return texts.length ? texts.join(" ") : null;
  }

  async _transcribe(pcm) {
    const pcm16k = resample16k(pcm, this.sampleRate, 16000);
    const start = await this._json("/api/start", { method: "POST" });
    const sessionId = start.session_id;
    for (let i = 0; i < pcm16k.length; i += 3200) {
      const res = await this.fetch(
        `${this.baseUrl}/api/chunk?session_id=${sessionId}`,
        {
          method: "POST",
          body: pcm16k.subarray(i, i + 3200),
          headers: { "Content-Type": "application/octet-stream" },
          signal: this._signal(),
        },
      );
      if (!res.ok) throw new Error(`chunk -> HTTP ${res.status}`);
    }
    const data = await this._json(`/api/finish?session_id=${sessionId}`, { method: "POST" });
    return { text: String(data.text || ""), language: String(data.language || "") };
  }
}
