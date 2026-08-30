// Lightweight energy-based VAD for the B-line POC.
// Plan explicitly defers ONNX/sherpa-onnx/llama.cpp until metrics prove the
// bottleneck, so this frame-RMS detector is enough for sentence boundarying.

const DEFAULT_OPTS = {
  sampleRate: 16000,
  frameMs: 30,
  threshold: 0.012, // RMS above which a frame counts as speech
  minSpeechMs: 250, // discard clicks shorter than this
  minSilenceMs: 500, // end a segment after this much trailing silence
  maxSpeechMs: 20000, // safety cap: force a boundary
};

function rms(int16) {
  if (int16.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < int16.length; i++) {
    const v = int16[i] / 32768;
    sum += v * v;
  }
  return Math.sqrt(sum / int16.length);
}

export class EnergyVAD {
  constructor(opts = {}) {
    this.opts = { ...DEFAULT_OPTS, ...opts };
    this.sampleRate = this.opts.sampleRate;
    this.frameSamples = Math.round((this.opts.sampleRate * this.opts.frameMs) / 1000);
    this.pending = Buffer.alloc(0);
    this.speech = Buffer.alloc(0);
    this.speaking = false;
    this.silenceMs = 0;
  }

  push(pcm16, sampleRate = this.sampleRate) {
    this.pending = Buffer.concat([this.pending, pcm16]);
    const ratio = sampleRate / this.sampleRate;
    const out = [];
    while (this.pending.length >= this.frameSamples * 2 * ratio) {
      const take = Math.floor(this.frameSamples * ratio) * 2;
      const frame = this.pending.subarray(0, take);
      this.pending = this.pending.subarray(take);
      const samples = new Int16Array(frame.buffer, frame.byteOffset, frame.length / 2);
      const level = rms(samples);
      if (level >= this.opts.threshold) {
        if (!this.speaking) this.speaking = true;
        this.silenceMs = 0;
        this.speech = Buffer.concat([this.speech, frame]);
        if (this.speech.length / 2 / this.sampleRate * 1000 >= this.opts.maxSpeechMs) {
          out.push(this._takeSegment());
        }
      } else if (this.speaking) {
        this.silenceMs += this.opts.frameMs;
        this.speech = Buffer.concat([this.speech, frame]);
        if (this.silenceMs >= this.opts.minSilenceMs) {
          out.push(this._takeSegment());
        }
      }
    }
    return out.length ? out : null;
  }

  flush() {
    if (!this.speaking) return null;
    const seg = this._takeSegment();
    return seg && seg.length ? [seg] : null;
  }

  _takeSegment() {
    const seg = this.speech;
    const durMs = (seg.length / 2 / this.sampleRate) * 1000;
    this.speech = Buffer.alloc(0);
    this.speaking = false;
    this.silenceMs = 0;
    return durMs >= this.opts.minSpeechMs ? seg : null;
  }
}
