/**
 * MiniMax 云端音色目录（人设页与设置页共享的唯一数据源，避免两处硬编码漂移）。
 *
 * MiniMax 大部分音色是多语模型：粤语播报音色念普通话/英语也自然（按新闻主播腔），
 * 普通话音色念粤语文字会带普通话腔。因此「全场始终同一个音色」模式下建议优先选
 * 粤语播报音色（香港客户场景粤/普/英都够地道）；下方 label 已按语言分组便于辨认。
 *
 * 维护提示：新增音色 ID 前先对 /api/tts/preview 试听接口确认返回 200；
 * 全角括号等特殊字符的 voice_id（如 Cantonese_ProfessionalHost（F)）会 2054 voice-not-exist。
 */

export type MinimaxVoiceLang = "yue" | "zh" | "en";

export interface MinimaxVoiceEntry {
  id: string;
  label: string;
  lang: MinimaxVoiceLang;
}

const YUE: Array<[string, string]> = [
  // 用户补充的 5 个主播/记者系（已逐个试听 200）
  ["Cantonese_crisp_news_anchor_vv2", "清脆新闻主播"],
  ["Cantonese_crisp_reporter_vv2", "清脆记者"],
  ["Cantonese_Articulate_commentator_vv2", "清晰评论员"],
  ["Cantonese_news_anchor_vv2", "新闻主播"],
  ["Cantonese_Objective_commentator_vv2", "客观评论员"],
  // 原有粤语 7 个（含实测可用）
  ["Cantonese_Male_news_anchor_vv2", "男主播 news_anchor_vv2"],
  ["Cantonese_GentleLady", "温柔女声 GentleLady"],
  ["Cantonese_PlayfulMan", "活泼男声 PlayfulMan"],
  ["Cantonese_CuteGirl", "可爱女孩 CuteGirl"],
  ["Cantonese_KindWoman", "善良女声 KindWoman"],
  ["Cantonese_ProfessionalHost（F)", "专业女主持（F）"],
  ["Cantonese_ProfessionalHost（M)", "专业男主持（M）"],
];

const ZH: Array<[string, string]> = [
  ["male-qn-qingse", "青涩青年男声"],
  ["male-qn-jingying", "精英青年男声"],
  ["female-shaonv", "少女音"],
  ["female-yujie", "御姐音"],
  ["Chinese (Mandarin)_News_Anchor", "普通话新闻女声"],
];

const EN: Array<[string, string]> = [
  ["male_english_speaker", "英文男声"],
  ["female_english_speaker", "英文女声"],
];

export const MINIMAX_VOICE_ENTRIES: MinimaxVoiceEntry[] = [
  ...YUE.map(([id, label]) => ({ id, label, lang: "yue" as const })),
  ...ZH.map(([id, label]) => ({ id, label, lang: "zh" as const })),
  ...EN.map(([id, label]) => ({ id, label, lang: "en" as const })),
];

export const MINIMAX_VOICE_LANG_LABEL: Record<MinimaxVoiceLang, string> = {
  yue: "粤语",
  zh: "普通话",
  en: "英语",
};

/** 旧用法：按语言筛可选音色（返回 {value,label}，符合设置页 FieldMeta options）。 */
export function minimaxVoiceOptionsFor(lang: MinimaxVoiceLang | string) {
  return MINIMAX_VOICE_ENTRIES.filter((v) => v.lang === lang).map((v) => ({ value: v.id, label: v.label }));
}

/** 单音色（全场同声）下拉：列出全部音色并带语言标签。 */
export function allMinimaxVoiceOptions() {
  return MINIMAX_VOICE_ENTRIES.map((v) => ({
    value: v.id,
    label: `${MINIMAX_VOICE_LANG_LABEL[v.lang]} · ${v.label}`,
  }));
}
