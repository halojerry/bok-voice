/**
 * 云端音色目录（人设页与平台设置页共享的唯一数据源，避免两处硬编码漂移）。
 *
 * 命名纪律（W④ 2026-10-09）：本文件进客户 bundle——标识符/注释零厂商名，
 * 音色 id 是不透明标识符（值域由平台维护，语义勿从字面推断）。
 *
 * 数据说明（平台侧维护档案，勿改语法）：
 * - 预置音色按语言分组整体重排（2026-10-02）——粤语 5、普通话 5（含 moss 男声
 *   A/B）、英语 6；组内女性在前男性在后。旧预置条目整体撤下；**克隆资产保留**：
 *   克隆系 / moss_audio 系 / 曾标注为克隆的条目即使不在新清单也留在 CLONE_ASSETS
 *   组（物化过的资产删条会丢账），按原语言（zh）归组。
 * - 2026-10-06 W2 四语扩容：新增德/法/日/葡四组；`VoiceCatalogEntry` 带
 *   `gender: "f"|"m"`（VOICE_GENDER 数据表，缺标=模块加载即抛错）；日语组收编
 *   用户克隆两枚（按 CLONE_ASSETS 语义不过期）；audition 真合成验证（scripts/
 *   seed/cache_cloud_auditions.py 全量物化到 apps/web/public/voice-auditions/）。
 *   官方 ID 含空格（如 French_Female_News Anchor）系原样，勿"修"。
 * - 多语音色特性：粤语播报音色念普通话/英语也自然（按新闻主播腔）；「全场始终
 *   同一个音色」模式下建议优先选粤语播报音色（香港客户场景粤/普/英都够地道）。
 *
 * 维护提示：
 * - 新增音色 ID 先跑 scripts/seed/cache_cloud_auditions.py 批量试听（真合成落
 *   apps/web/public/voice-auditions/，无效 id 当场现形）；该脚本正则解析本文件
 *   的数组组（`const X: Array<[string, string]> = [` + `["id", "label"],`）与
 *   导出块的 group→lang 映射（`...组.map(([id, label]) => ({ id, label, lang: "xx" as const })),`）
 *   ——改这里的语法两处必须同步；gender 标注补进 VOICE_GENDER 表。
 * - 全角括号等特殊字符的 voice_id 历史上会 voice-not-exist，勿回填。
 */

export type VoiceLang = "cantonese" | "zh" | "en" | "de" | "fr" | "ja" | "pt";

export type VoiceGender = "f" | "m";

export interface VoiceCatalogEntry {
  id: string;
  label: string;
  lang: VoiceLang;
  gender: VoiceGender;
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
  // 普通话男（克隆系）
  ["moss_audio_ce44fc67-7ce3-11f0-8de5-96e35d26fb85", "男声 A"],
  ["moss_audio_9c223de9-7ce1-11f0-9b9f-463feaa3106a", "男声 B"],
];

/** 克隆资产（不在新清单仍保留，按换血前语言归属=zh）：A 线普通话默认克隆。 */
const CLONE_ASSETS: Array<[string, string]> = [
  ["moss_audio_aaa1346a-7ce7-11f0-8e61-2e6e3c7ee85d", "克隆音色（默认）"],
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

// ---- 2026-10-06 W2 四语组（德/法/日/葡；audition 真合成验证存活才入目录）----
const DE: Array<[string, string]> = [
  // 德语女
  ["German_SweetLady", "德语·甜美女声"],
  // 德语男
  ["German_FriendlyMan", "德语·友善男士"],
];

const FR: Array<[string, string]> = [
  // 法语女（官方 ID 含空格，原样保留）
  ["French_Female_News Anchor", "法语·新闻女主播"],
  ["French_FemaleAnchor", "法语·女主持"],
  // 法语男
  ["French_MaleNarrator", "法语·男叙述员"],
  ["French_CasualMan", "法语·随和男声"],
];

const JA: Array<[string, string]> = [
  // 日语女（克隆=用户自有资产，不过期）
  ["moss_audio_c373f8c3-7c24-11f0-8417-7e9cf3e02f36", "日语·克隆女声"],
  ["Japanese_efficient_reporter_vv1", "日语·干练记者"],
  // 日语男
  ["Japanese_GenerousIzakayaOwner", "日语·豪爽居酒屋老板"],
  ["Japanese_rebellious_youth_vv2", "日语·叛逆青年"],
  ["moss_audio_10297aea-7c27-11f0-8de5-96e35d26fb85", "日语·克隆男声"],
];

const PT: Array<[string, string]> = [
  // 葡语女
  ["Portuguese_UpsetGirl", "葡语·小脾气女孩"],
  ["Portuguese_AnimeCharacter", "葡语·动漫角色"],
  ["Portuguese_LovelyLady", "葡语·可爱女士"],
  ["Portuguese_CaringGirlfriend", "葡语·贴心女友"],
  // 葡语男
  ["Portuguese_Strong-WilledBoy", "葡语·倔强男孩"],
  ["Portuguese_Optimisticyouth", "葡语·乐观青年"],
  ["Portuguese_FunnyGuy", "葡语·幽默男声"],
];

/** gender 标注表（分组注释转正为数据；voice_id → f/m。新条目漏标=下方组装时
 *  模块加载即抛错，目录错误在 dev/build 当场现形而非上线后哑选）。 */
const VOICE_GENDER: Record<string, VoiceGender> = {
  // 粤语：女三男二
  Cantonese_news_anchor_vv2: "f",
  Cantonese_crisp_news_anchor_vv2: "f",
  Cantonese_GentleLady: "f",
  Cantonese_objective_narrator_vv2: "m",
  Cantonese_Male_news_anchor_vv2: "m",
  // 普通话：女三男二（克隆两枚=男）
  "Chinese (Mandarin)_Soft_Girl": "f",
  "Chinese (Mandarin)_Warm_Bestie": "f",
  Chinese_wenrounvxing: "f",
  "moss_audio_ce44fc67-7ce3-11f0-8de5-96e35d26fb85": "m",
  "moss_audio_9c223de9-7ce1-11f0-9b9f-463feaa3106a": "m",
  // 克隆资产：男
  "moss_audio_aaa1346a-7ce7-11f0-8e61-2e6e3c7ee85d": "m",
  // 英语：男三女三
  English_Trustworth_Man: "m",
  English_Lively_Male_11: "m",
  English_Explanatory_Man: "m",
  English_MatureBoss: "f",
  English_AttractiveGirl: "f",
  socialmedia_female_2_v1: "f",
  // 德语：女一男一
  German_SweetLady: "f",
  German_FriendlyMan: "m",
  // 法语：女二男二
  "French_Female_News Anchor": "f",
  French_FemaleAnchor: "f",
  French_MaleNarrator: "m",
  French_CasualMan: "m",
  // 日语：女二男三（含克隆两枚）
  "moss_audio_c373f8c3-7c24-11f0-8417-7e9cf3e02f36": "f",
  Japanese_efficient_reporter_vv1: "f",
  Japanese_GenerousIzakayaOwner: "m",
  Japanese_rebellious_youth_vv2: "m",
  "moss_audio_10297aea-7c27-11f0-8de5-96e35d26fb85": "m",
  // 葡语：女四男三
  Portuguese_UpsetGirl: "f",
  Portuguese_AnimeCharacter: "f",
  Portuguese_LovelyLady: "f",
  Portuguese_CaringGirlfriend: "f",
  "Portuguese_Strong-WilledBoy": "m",
  Portuguese_Optimisticyouth: "m",
  Portuguese_FunnyGuy: "m",
};

/** 组装基底。语法契约：`...组.map(([id, label]) => ({ id, label, lang: "xx" as const })),`
 *  逐行形态勿改——scripts/seed/cache_cloud_auditions.py _EXPORT_RE 逐行扫本块收
 *  group→lang 映射；gender 统一在下方导出组装时按 VOICE_GENDER 表补。 */
const CATALOG_BASE: Array<{ id: string; label: string; lang: VoiceLang }> = [
  ...CANTONESE.map(([id, label]) => ({ id, label, lang: "cantonese" as const })),
  ...ZH.map(([id, label]) => ({ id, label, lang: "zh" as const })),
  ...CLONE_ASSETS.map(([id, label]) => ({ id, label, lang: "zh" as const })),
  ...EN.map(([id, label]) => ({ id, label, lang: "en" as const })),
  ...DE.map(([id, label]) => ({ id, label, lang: "de" as const })),
  ...FR.map(([id, label]) => ({ id, label, lang: "fr" as const })),
  ...JA.map(([id, label]) => ({ id, label, lang: "ja" as const })),
  ...PT.map(([id, label]) => ({ id, label, lang: "pt" as const })),
];

export const VOICE_CATALOG_ENTRIES: VoiceCatalogEntry[] = CATALOG_BASE.map((e) => {
  const gender = VOICE_GENDER[e.id];
  if (!gender) throw new Error(`voice-catalog: 音色缺 gender 标注: ${e.id}`);
  return { ...e, gender };
});

export const VOICE_LANG_LABEL: Record<VoiceLang, string> = {
  cantonese: "粤语",
  zh: "普通话",
  en: "英语",
  de: "德语",
  fr: "法语",
  ja: "日语",
  pt: "葡萄牙语",
};

/** 按语言筛可选音色（返回 {value,label}，符合设置页 FieldMeta options）。
 *  下拉装配/试听语言已统一收编到 lib/voice-options.ts（W5-T2）：多来源合并用
 *  buildVoiceSelectOptions、试听语言用 resolvePreviewLang——本文件只留目录本体。 */
export function voiceOptionsFor(lang: VoiceLang | string) {
  return VOICE_CATALOG_ENTRIES.filter((v) => v.lang === lang).map((v) => ({ value: v.id, label: v.label }));
}
