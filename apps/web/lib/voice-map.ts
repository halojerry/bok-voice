/**
 * 人设 reference_audio 语音映射解析/收敛(单源,2026-10-08 音色选择器统一波)。
 *
 * 语义权威 = packages/core/bok_voice_core/voice_map.py(parse_voice_map +
 * collapse_voice_map):personas 页与同传页共用同一份解析——「整场主音色」=
 * 优先人设主语言槽,缺则 zh→cantonese→首个非空(与 agent 收敛同规则)。
 * 在此之前两页各持一份拷贝(interpret 页新增第三份前先收编)。
 */

/** reference_audio 原始值 → 分语言音色 map;坏形状=空 map(配置错误不清空通话)。 */
export function parseVoiceMap(raw: unknown): Record<string, string> {
  if (typeof raw !== "string" || !raw.trim().startsWith("{")) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? (parsed as Record<string, string>) : {};
  } catch {
    return {};
  }
}

/** 从语音映射选「整场主音色」：优先人设主语言，缺则 zh→cantonese→首个非空。 */
export function primaryVoiceFor(lang: string, voiceMap: Record<string, string>): string {
  const keys = [lang, "zh", "cantonese", "en"];
  for (const k of keys) {
    if (voiceMap[k]) return voiceMap[k];
  }
  return Object.values(voiceMap)[0] ?? "";
}
