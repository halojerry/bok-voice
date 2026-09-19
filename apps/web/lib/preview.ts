/**
 * 试听取声/放声单点（W5-T2 卫生收编）：objectURL 创建与回收、鉴权头、
 * 罐头缓存优先级全部收敛在这——修 canned-audition 垫话裸 fetch（auth-on 缺
 * Bearer）、MinimaxClonePanel 与 interpret-console sink 试听的 objectURL 泄漏，
 * 以及各页 previewTts 拷贝味。
 */
import { api, authHeaders } from "@/lib/api";

/**
 * 播一段音频 blob（qa 页 playBlob 手法收编）：开始播放即返回，ended/error 后
 * 回收 objectURL；setSinkId 可选（指定输出设备试听）；play 失败立即回收并 rethrow。
 */
export async function playAudioBlob(blob: Blob, opts: { sinkId?: string } = {}): Promise<void> {
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  try {
    if (opts.sinkId) {
      const sink = audio as HTMLAudioElement & { setSinkId?: (id: string) => Promise<void> };
      if (sink.setSinkId) await sink.setSinkId(opts.sinkId);
    }
    await audio.play();
    audio.onended = () => URL.revokeObjectURL(url);
    audio.onerror = () => URL.revokeObjectURL(url);
  } catch (e) {
    URL.revokeObjectURL(url);
    throw e;
  }
}

export interface PreviewVoiceSpec {
  provider?: string;
  voice?: string;
  language?: string;
  text: string;
  sample_rate?: number;
  /** QA 词条 id：给了先取 /api/qa/{id}/canned-audio 录音缓存（零云费），404/失败降级。 */
  cannedEntryId?: string;
  /** 现场合成烧云配额仅主管；false 且缓存不可用时抛错（不烧云）。默认 true。 */
  allowLive?: boolean;
}

/**
 * 取试听音频 Blob：缓存优先（cannedEntryId → 罐头 wav）→ 现场合成
 * （POST /api/tts/preview）。全部 fetch 带 authHeaders（auth-on 附 Bearer）；
 * 返回 Blob，播放与 objectURL 回收交给调用方（通常配 playAudioBlob）。
 */
export async function previewVoice(spec: PreviewVoiceSpec): Promise<Blob> {
  const allowLive = spec.allowLive !== false;
  if (spec.cannedEntryId) {
    try {
      const res = await fetch(api.cannedAudioUrl(spec.cannedEntryId), { headers: authHeaders() });
      if (res.ok) return await res.blob();
    } catch {
      /* 网络失败等同缺料，照降级 */
    }
  }
  if (!allowLive) throw new Error("需要主管权限现场合成");
  return api.previewTts({
    provider: spec.provider,
    voice: spec.voice,
    language: spec.language,
    text: spec.text,
    sample_rate: spec.sample_rate ?? 24000,
  });
}
