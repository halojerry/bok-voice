export class PlaybackChunkTrace {
  constructor({
    seqId,
    sourceSeqId,
    playbackBatchId,
    isFinal,
    durationMs,
    playableBacklogMs,
    queueDepth,
    chaseState,
    chaseSpeed,
    gateRule,
    gateAction,
    gateReason,
    firstPlayable,
    audioHash,
  }) {
    this.seqId = seqId;
    this.sourceSeqId = sourceSeqId;
    this.playbackBatchId = playbackBatchId;
    this.isFinal = isFinal;
    this.durationMs = durationMs;
    this.playableBacklogMs = playableBacklogMs;
    this.queueDepth = queueDepth;
    this.chaseState = chaseState;
    this.chaseSpeed = chaseSpeed;
    this.gateRule = gateRule;
    this.gateAction = gateAction;
    this.gateReason = gateReason;
    this.firstPlayable = firstPlayable;
    this.audioHash = audioHash;
  }
}

export class PlaybackScheduler {
  constructor({
    channelId,
    targetBufferMs = 1200,
    maxBacklogMs = 4000,
    maxChaseSpeed = 1.25,
  } = {}) {
    this.channelId = channelId;
    this.targetBufferMs = targetBufferMs;
    this.maxBacklogMs = maxBacklogMs;
    this.maxChaseSpeed = maxChaseSpeed;
    this.queue = [];
    this.seq = 0;
    this.playedMs = 0;
    this.lastNowMs = null;
    this.droppedBlocks = 0;
    this.droppedMs = 0;
    this.inputOverflow = 0;
    this.inputUnderflow = 0;
    this.outputOverflow = 0;
    this.outputUnderflow = 0;
  }

  enqueue({ sourceSeqId, durationMs, audio, isFinal = false }) {
    const playableBacklogMs = this.queue.reduce((sum, item) => sum + item.durationMs, 0);
    const trace = new PlaybackChunkTrace({
      seqId: ++this.seq,
      sourceSeqId,
      playbackBatchId: sourceSeqId,
      isFinal,
      durationMs,
      playableBacklogMs,
      queueDepth: this.queue.length,
      chaseState: "idle",
      chaseSpeed: 1.0,
      gateRule: "maxBacklogMs",
      gateAction: "enqueue",
      gateReason: "",
      firstPlayable: this.queue.length === 0,
      audioHash: audio ? `${audio.length}` : "",
    });
    this.queue.push({ ...trace, audio });

    while (this.queue.length > 1) {
      const total = this.queue.reduce((sum, item) => sum + item.durationMs, 0);
      if (total <= this.maxBacklogMs) break;
      const dropped = this.queue.shift();
      this.droppedBlocks += 1;
      this.droppedMs += dropped.durationMs;
    }
    return trace;
  }

  tick(nowMs, advanceMs = 0) {
    if (this.lastNowMs === null) {
      this.lastNowMs = nowMs;
    }
    if (advanceMs > 0 && this.queue.length) {
      this.queue[0].durationMs = Math.max(0, this.queue[0].durationMs - advanceMs);
      if (this.queue[0].durationMs === 0) {
        this.queue.shift();
      }
    }
    const queuedAudioMs = this.queue.reduce((sum, item) => sum + item.durationMs, 0);
    const backlog = queuedAudioMs - this.targetBufferMs;
    let chaseState = "idle";
    let chaseSpeed = 1.0;
    if (backlog > 800) {
      chaseState = "chasing";
      chaseSpeed = Math.min(this.maxChaseSpeed, 1 + backlog / 4000);
    } else if (backlog < -200) {
      chaseState = "draining";
      chaseSpeed = 1.0;
    }
    this.queue.forEach((item) => {
      item.playableBacklogMs = Math.max(0, backlog);
      item.queueDepth = this.queue.length;
      item.chaseState = chaseState;
      item.chaseSpeed = chaseSpeed;
    });
    return this.metrics();
  }

  discardQueuedAudio(uptoSourceSeqId) {
    const removedItems = this.queue.filter((item) => item.sourceSeqId <= uptoSourceSeqId);
    const droppedMs = removedItems.reduce((sum, item) => sum + item.durationMs, 0);
    this.queue = this.queue.filter((item) => item.sourceSeqId > uptoSourceSeqId);
    if (removedItems.length) {
      this.droppedBlocks += removedItems.length;
      this.droppedMs += droppedMs;
    }
  }

  clear() {
    this.queue = [];
  }

  metrics() {
    const last = this.queue[this.queue.length - 1];
    const queuedAudioMs = this.queue.reduce((sum, item) => sum + item.durationMs, 0);
    return {
      channelId: this.channelId,
      queuedAudioMs,
      currentRemainingMs: this.queue[0]?.durationMs ?? 0,
      nextRemainingMs: this.queue[1]?.durationMs ?? 0,
      targetBufferMs: this.targetBufferMs,
      queueDepth: this.queue.length,
      playableBacklogMs: Math.max(0, queuedAudioMs - this.targetBufferMs),
      chaseState: last?.chaseState ?? "idle",
      chaseSpeed: last?.chaseSpeed ?? 1.0,
      droppedBlocks: this.droppedBlocks,
      droppedMs: this.droppedMs,
      inputOverflow: this.inputOverflow,
      inputUnderflow: this.inputUnderflow,
      outputOverflow: this.outputOverflow,
      outputUnderflow: this.outputUnderflow,
    };
  }
}
