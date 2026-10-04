/**
 * MiniMax 云端音色目录（人设页与设置页共享的唯一数据源，避免两处硬编码漂移）。
 *
 * 2026-10-02 换血（用户拍板新清单）：预置音色按语言分组整体重排——粤语 5 只、
 * 普通话 5 只（含 moss 男声 A/B）、英语 6 只；组内女性在前、男性在后。
 * 旧预置条目（Cantonese_crisp_reporter_vv2 / male-qn-* / English_magnetic_voiced_man 等）
 * 整体撤下；**克隆资产保留**：克隆系 / moss_audio 系 / 曾标注为克隆的条目即使不在
 * 新清单也留在 CLONE_ASSETS 组（物化过的资产删条会丢账），按原语言（zh）归组。
 *
 * MiniMax 大部分音色是多语模型：粤语播报音色念普通话/英语也自然（按新闻主播腔），
 * 普通话音色念粤语文字会带普通话腔。因此「全场始终同一个音色」模式下建议优先选
 * 粤语播报音色（香港客户场景粤/普/英都够地道）；下方 label 已按语言分组便于辨认。
 *
 * 维护提示：
 * - 新增音色 ID 先跑 scripts/cache_minimax_auditions.py 批量试听（对官方 t2a_v2
 *   真合成落 assets/minimax-auditions/，2054 voice-not-exist 当场现形）；该脚本
 *   正则解析本文件的数组组（`const X: Array<[string, string]> = [` + `["id", "label"],`）
 *   与导出块的 group→lang 映射——改这里的语法两处必须同步。
 * - 全角括号等特殊字符的 voice_id（如 Cantonese_ProfessionalHost（F)）会 2054
 *   voice-not-exist，勿回填。
 */

export type MinimaxVoiceLang = "cantonese" | "zh" | "en";

export interface MinimaxVoiceEntry {
  id: string;
  label: string;
  lang: MinimaxVoiceLang;
}

/** [voice_id, label]——ID 原样保留（含空格），label 只作下拉短标。 */
const CANTONESE: Array<[string, string]> = [
  // 粤语女
  ["Cantonese_news_anchor_vv2", "新闻主播"],
  ["Cantonese_crisp_news_anchor_vv2", "清脆新闻主播"],
  ["Cantonese_GentleLady", "温柔女士"],
  // 粤语男
  ["Cantonese_objective_narrator_vv2", "客观旁述"],
  ["Cantonese_Male_news_anchor_vv2", "男新闻主播"],
];

const ZH: Array<[string, string]> = [
  // 普通话女
  ["Chinese (Mandarin)_Soft_Girl", "软妹"],
  ["Chinese (Mandarin)_Warm_Bestie", "暖闺蜜"],
  ["Chinese_wenrounvxing", "温柔女星"],
  // 普通话男（moss 克隆系）
  ["moss_audio_ce44fc67-7ce3-11f0-8de5-96e35d26fb85", "moss 男声 A"],
  ["moss_audio_9c223de9-7ce1-11f0-9b9f-463feaa3106a", "moss 男声 B"],
];

/** 克隆资产（不在新清单仍保留，按换血前语言归属=zh）：A 线普通话默认克隆。 */
const CLONE_ASSETS: Array<[string, string]> = [
  ["moss_audio_aaa1346a-7ce7-11f0-8e61-2e6e3c7ee85d", "克隆音色 moss（默认）"],
];

const EN: Array<[string, string]> = [
  // 英语男
  ["English_Trustworth_Man", "可信男声"],
  ["English_Lively_Male_11", "活泼男声"],
  ["English_Explanatory_Man", "讲解男声"],
  // 英语女
  ["English_MatureBoss", "成熟女强人"],
  ["English_AttractiveGirl", "魅力女声"],
  ["socialmedia_female_2_v1", "社媒女声"],
];

export const MINIMAX_VOICE_ENTRIES: MinimaxVoiceEntry[] = [
  ...CANTONESE.map(([id, label]) => ({ id, label, lang: "cantonese" as const })),
  ...ZH.map(([id, label]) => ({ id, label, lang: "zh" as const })),
  ...CLONE_ASSETS.map(([id, label]) => ({ id, label, lang: "zh" as const })),
  ...EN.map(([id, label]) => ({ id, label, lang: "en" as const })),
];

export const MINIMAX_VOICE_LANG_LABEL: Record<MinimaxVoiceLang, string> = {
  cantonese: "粤语",
  zh: "普通话",
  en: "英语",
};

/** 按语言筛可选音色（返回 {value,label}，符合设置页 FieldMeta options）。
 *  下拉装配/试听语言已统一收编到 lib/voice-options.ts（W5-T2）：多来源合并用
 *  buildVoiceSelectOptions、试听语言用 resolvePreviewLang——本文件只留目录本体。 */
export function minimaxVoiceOptionsFor(lang: MinimaxVoiceLang | string) {
  return MINIMAX_VOICE_ENTRIES.filter((v) => v.lang === lang).map((v) => ({ value: v.id, label: v.label }));
}
