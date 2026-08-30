export class MockASR {
  constructor() {
    this.buffer = "";
  }

  async push(chunk, sourceLang) {
    // channel 传 {pcm, sampleRate}；单测直接传字符串。
    const text =
      typeof chunk === "string"
        ? chunk
        : Buffer.isBuffer(chunk.pcm)
          ? chunk.pcm.toString("utf8")
          : String(chunk.pcm || "");
    this.buffer += text;
    if (/[。！？.!?]/.test(this.buffer)) {
      const sentence = this.buffer.trim();
      this.buffer = "";
      return sentence || null;
    }
    return null;
  }

  async flush() {
    const sentence = this.buffer.trim();
    this.buffer = "";
    return sentence || null;
  }
}

export class MockTranslator {
  async translate(text, sourceLang, targetLang) {
    return `[${sourceLang}->${targetLang}] ${text}`;
  }
}

export class MockTTS {
  async synthesize(text, targetLang) {
    const durationMs = 200;
    return [
      { durationMs, audio: Buffer.from(text).toString("base64"), final: true },
    ];
  }
}
