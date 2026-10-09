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
 * prepare 钩子（2026-10-02 刀3）：播放前就地准备（如一体台试听的 element.setSinkId
 * ——设备指认留在调用方），抛错则中止播放且 URL 立即回收，播放/回收仍由本函数单点管。
 */
export async function playAudioBlob(
  blob: Blob,
  opts: {
    sinkId?: string;
    prepare?: (audio: HTMLAudioElement) => void | Promise<void>;
  } = {},
): Promise<void> {
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  try {
    if (opts.prepare) await opts.prepare(audio);
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
  voice?: string;
  language?: string;
  text: string;
  sample_rate?: number;
  /** QA 词条 id：给了先取 /api/qa/{id}/canned-audio 录音缓存（零云费），404/失败降级。 */
  cannedEntryId?: string;
  /** 现场合成烧云配额仅主管；false 且缓存/本地物化都不可用时抛错（不烧云）。默认 true。 */
  allowLive?: boolean;
}

/** 本地物化试听（2026-10-06 W2e；2026-10-07 CI 修正=真文件直发）：
 *  scripts/seed/cache_cloud_auditions.py 对目录全量真合成落
 *  apps/web/public/voice-auditions/<voice_id 安全化>.mp3（与脚本 safe_name
 *  同规则：非 [A-Za-z0-9._-] 折叠 _、剥首尾 _），UI 同源静态托管——命中即零云费
 *  零延迟；未命中（云端克隆/本地 Qwen 音色/未物化部署）静默回落 /api/tts/preview
 *  现场合成，行为与旧版逐字节一致。（历史：曾用根 assets/ 目录+public 符号链接，
 *  CP Docker web-build stage 只 COPY apps/web → symlink 悬空 ENOENT，已改真文件。） */
const AUDITION_BASE = "/voice-auditions";

function auditionFileName(voice: string): string {
  const safe = String(voice).replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "");
  return `${safe || "voice"}.mp3`;
}

/**
 * 取试听音频 Blob：QA 罐头缓存（cannedEntryId）→ 本地物化试听（目录音色，
 * 零云费）→ 现场合成（POST /api/tts/preview）。fetch 带 authHeaders 的仅 CP 面
 * （罐头）；本地物化是 UI 同源静态文件不带鉴权头。返回 Blob，播放与 objectURL
 * 回收交给调用方（通常配 playAudioBlob）。
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
  if (spec.voice) {
    try {
      const res = await fetch(`${AUDITION_BASE}/${auditionFileName(spec.voice)}`);
      if (res.ok) return await res.blob();
    } catch {
      /* 本地物化缺席（未物化/非同源托管），照走现场合成 */
    }
  }
  if (!allowLive) throw new Error("需要主管权限现场合成");
  // W④（2026-10-09）：不传 provider——CP 服务端按 voice_id 前缀解析引擎
  // （请求体/抓包面零厂商名）。
  return api.previewTts({
    voice: spec.voice,
    language: spec.language,
    text: spec.text,
    sample_rate: spec.sample_rate ?? 24000,
  });
}
