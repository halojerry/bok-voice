import { PlaybackScheduler } from "./playback-scheduler.js";

export class TranslationChannel {
  constructor({
    id,
    sourceLang,
    targetLang,
    asr,
    translator,
    tts,
    maxQueueMs = 4000,
    events = null,
  }) {
    this.id = id;
    this.sourceLang = sourceLang;
    this.targetLang = targetLang;
    this.asrState = { text: "", final: "", pending: false };
    this.sentenceBoundary = null;
    this.translateQueue = [];
    this.ttsStream = [];
    this.playbackQueue = new PlaybackScheduler({ channelId: id });
    this.asr = asr;
    this.translator = translator;
    this.tts = tts;
    this.maxQueueMs = maxQueueMs;
    this.events = events;
    this._sourceSeq = 0;
  }

  async pushAudio(chunk) {
    this.asrState.pending = true;
    const sentence = await this.asr.push(chunk, this.sourceLang);
    if (sentence) {
      this.asrState.final = sentence;
      this.sentenceBoundary = Date.now();
      this.translateQueue.push({ source: sentence, at: Date.now() });
      await this.drainTranslationQueue();
    } else {
      this.asrState.pending = false;
    }
  }

  async flush() {
    const sentence = await this.asr.flush(this.sourceLang);
    if (sentence) {
      this.asrState.final = sentence;
      this.translateQueue.push({ source: sentence, at: Date.now() });
      await this.drainTranslationQueue();
    }
    this.asrState.pending = false;
  }

  async drainTranslationQueue() {
    const now = Date.now();
    while (this.translateQueue.length && now - this.translateQueue[0].at > this.maxQueueMs) {
      this.translateQueue.shift();
    }
    if (!this.translateQueue.length) return;
    const item = this.translateQueue.shift();
    const translated = await this.translator.translate(item.source, this.sourceLang, this.targetLang);
    const chunks = await this.tts.synthesize(translated, this.targetLang);
    const sourceSeqId = ++this._sourceSeq;
    this.events?.emit("subtitle", {
      channelId: this.id,
      source: item.source,
      translated,
      sourceSeqId,
      at: Date.now(),
    });
    chunks.forEach((chunk) => {
      this.ttsStream.push(chunk);
      const trace = this.playbackQueue.enqueue({
        sourceSeqId,
        durationMs: chunk.durationMs,
        audio: chunk.audio,
        isFinal: chunk.final === true,
      });
      this.events?.emit("audio", {
        channelId: this.id,
        ...trace,
        pcm: Buffer.isBuffer(chunk.audio) ? chunk.audio.toString("base64") : chunk.audio,
        sampleRate: chunk.sampleRate || 24000,
      });
    });
  }

  tick(nowMs, advanceMs = 0) {
    const metrics = this.playbackQueue.tick(nowMs, advanceMs);
    this.events?.emit("metrics", { channelId: this.id, ...metrics });
    return metrics;
  }

  discardQueuedAudio(uptoSourceSeqId) {
    this.playbackQueue.discardQueuedAudio(uptoSourceSeqId);
  }

  clearAudioPlaybackChannel() {
    this.playbackQueue.clear();
  }
}
