"use client";

/**
 * 轻量麦克风录音 → WAV(16bit PCM mono)。
 *
 * 用于「人设 → 克隆音色」的参考音频：用户直接对着麦克风说一段话，
 * 无需本地上传文件。用 ScriptProcessor 采集 PCM 再编码成 WAV——
 * 相比 MediaRecorder 的 webm/opus，WAV/PCM 在 clone 侧解码最稳（webm 常不被支持）。
 * WKWebView 下 ScriptProcessor 已废弃但可用（同传页同款采集路径）。
 */

export interface RecorderHandle {
  stop: () => Promise<Blob>;
  cancel: () => void;
}

async function ensureMicStream(): Promise<MediaStream> {
  return navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, sampleRate: 16000 } });
}

/** 编码 16-bit mono PCM 到 WAV blob。 */
export function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeStr = (offset: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i));
  };
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true); // PCM chunk size
  view.setUint16(20, 1, true); // PCM format
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  writeStr(36, "data");
  view.setUint32(40, samples.length * 2, true);
  let offset = 44;
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    offset += 2;
  }
  return new Blob([buffer], { type: "audio/wav" });
}

/**
 * 开始录音。返回 handle：
 * - await stop()  → 结束并返回 WAV Blob（同时停掉麦克风轨道）
 * - cancel()      → 放弃本次录音
 * 录音最长 maxMs（默认 30s）自动停止。
 */
export async function startRecording(maxMs = 30000): Promise<RecorderHandle> {
  const stream = await ensureMicStream();
  const sampleRate = 16000;
  const ctx = new AudioContext({ sampleRate });
  const source = ctx.createMediaStreamSource(stream);
  const processor = ctx.createScriptProcessor(4096, 1, 1);
  const chunks: Float32Array[] = [];
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;

  processor.onaudioprocess = (e) => {
    if (stopped) return;
    chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  };

  // ScriptProcessor 只有在输出被消费时才会持续拉取：接零增益节点防回声（同同传页）。
  const mute = ctx.createGain();
  mute.gain.value = 0;
  source.connect(processor);
  processor.connect(mute);
  mute.connect(ctx.destination);

  const finish = async () => {
    if (stopped) return Promise.resolve(new Blob([], { type: "audio/wav" }));
    stopped = true;
    if (timer) clearTimeout(timer);
    let total = 0;
    for (const c of chunks) total += c.length;
    const all = new Float32Array(total);
    let off = 0;
    for (const c of chunks) {
      all.set(c, off);
      off += c.length;
    }
    try {
      processor.disconnect();
      source.disconnect();
      mute.disconnect();
      await ctx.close();
    } catch {
      /* ignore */
    }
    stream.getTracks().forEach((t) => t.stop());
    return encodeWav(all, sampleRate);
  };

  if (maxMs > 0) {
    timer = setTimeout(() => {
      void finish();
    }, maxMs);
  }

  return {
    stop: finish,
    cancel: () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      try {
        processor.disconnect();
        source.disconnect();
        mute.disconnect();
        void ctx.close();
      } catch {
        /* ignore */
      }
      stream.getTracks().forEach((t) => t.stop());
    },
  };
}
