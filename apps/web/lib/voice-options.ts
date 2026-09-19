/**
 * 音色下拉装配 / 试听语言 / 罐头音路由纯函数（W5-T2 卫生收编，零 import——
 * node --test 单文件转译直载，见 test/voice-options.test.mjs）。
 *
 * 语义权威 = docs/superpowers/plans/2026-09-19-ai-studio.md §8 T2 探针结论：
 * - 音色目录唯一数据源 = lib/minimax-voices.ts；设置页/personas/同传页各自的下拉
 *   在这里统一装配（克隆清单按页注入），消灭四份手写合并/过滤/标签拷贝；
 * - 装配序：firstOption → 匹配 slotLang 的克隆置顶 → 目录/预置 → 其余克隆
 *   （Set 去重贯穿全序；克隆全语言可选，不吃 slotLang 过滤）；
 * - slotLang 只过滤目录，未知语言回落全量（personas 旧语言如 vi 不清空成空下拉）；
 * - 试听语言：音色 ID 正则优先（Cantonese_→粤语、English_/socialmedia_→英语），
 *   否则按字段键（settings speaker_cantonese/speaker_en 本地链）→ fallback → 普通话；
 * - 罐头音试听路由：有录音=缓存（零云费）；缺录音现场合成烧云配额仅主管，
 *   非主管=拦截提示。
 */

export type PreviewLang = "zh" | "cantonese" | "en";

export interface VoiceCatalogEntry {
  id: string;
  label: string;
  lang: string;
}

export interface VoiceOptionItem {
  value: string;
  label: string;
}

const KNOWN_LANGS: readonly string[] = ["zh", "cantonese", "en"];

/** 克隆语言 → 中文标签（与 personas 页 LANG_LABEL 同值，此处为唯一装配点）。 */
const LANG_LABEL: Record<string, string> = { zh: "普通话", cantonese: "粤语", en: "英语" };

/** 前缀风格克隆标签默认前缀（settings/interpret 现行文案）。 */
const DEFAULT_CLONE_PREFIX = "克隆 · ";

export interface VoiceSelectSpec {
  /** 静态目录（MINIMAX_VOICE_ENTRIES）。唯一吃 slotLang 过滤的来源。 */
  catalog?: VoiceCatalogEntry[];
  /** 当前槽位语言：过滤目录（未知回落全量）+ 匹配克隆置顶。 */
  slotLang?: string;
  /** MiniMax 云端克隆（settings parseClones / 同传页 cloneVoices）。全语言可选。 */
  minimaxClones?: Array<{ voice_id?: unknown; label?: unknown; sample_lang?: unknown }>;
  /** 本地克隆音色（personas listTtsVoices / registerTtsVoice）。全语言可选。 */
  localClones?: Array<{ id?: unknown; lang?: unknown }>;
  /** 本地预置 speaker（personas listTtsSpeakers，多语模型，不过滤）。 */
  localSpeakers?: Array<unknown>;
  /** 首项默认项（如同传页「（默认，跟随设置）」）。 */
  firstOption?: VoiceOptionItem;
  /** 前缀风格克隆标签前缀，默认「克隆 · 」；传 "" 关闭前缀（裸 label）。 */
  cloneLabelPrefix?: string;
  /** personas 后缀风格：克隆=`id（克隆 · 语言）`、预置=名+presetSuffix。 */
  cloneSuffix?: boolean;
  /** 后缀风格下预置 speaker 显示名映射（personas PRESET_VOICE_CN）。 */
  presetLabels?: Record<string, string>;
  /** 后缀风格下预置条目后缀（personas 用「（预置音色）」）。 */
  presetSuffix?: string;
}

interface CloneItem {
  id: string;
  label: string;
  lang: string;
}

function cloneLabel(c: CloneItem, spec: VoiceSelectSpec): string {
  const langTag = c.lang ? LANG_LABEL[c.lang] ?? c.lang : "";
  if (spec.cloneSuffix) {
    // personas 手法原样：`${id}（克隆${langTag ? " · " + langTag : ""}）`。
    return `${c.id}（克隆${langTag ? " · " + langTag : ""}）`;
  }
  const prefix = spec.cloneLabelPrefix === undefined ? DEFAULT_CLONE_PREFIX : spec.cloneLabelPrefix;
  return prefix ? `${prefix}${c.label || c.id}` : c.label || c.id;
}

/**
 * 统一装配一个音色下拉的选项序列（Set 去重贯穿全序，先到先得）：
 * ① firstOption；② 匹配 slotLang 的克隆置顶（云端克隆先、本地克隆后）；
 * ③ 目录（仅已知语言槽位过滤，未知回落全量）；④ 本地预置 speaker；
 * ⑤ 其余克隆按原序垫底。
 */
export function buildVoiceSelectOptions(spec: VoiceSelectSpec = {}): VoiceOptionItem[] {
  const out: VoiceOptionItem[] = [];
  const seen = new Set<string>();
  const push = (value: string, label: string) => {
    if (!value || seen.has(value)) return;
    seen.add(value);
    out.push({ value, label });
  };
  // firstOption 是「不选/跟随设置」占位项，value 可为空串，不吃空值门。
  if (spec.firstOption && !seen.has(spec.firstOption.value)) {
    seen.add(spec.firstOption.value);
    out.push({ value: spec.firstOption.value, label: spec.firstOption.label });
  }

  const lang = String(spec.slotLang ?? "").toLowerCase();
  const knownLang = KNOWN_LANGS.includes(lang) ? lang : "";

  const clones: CloneItem[] = [
    ...(spec.minimaxClones ?? []).map((c) => ({
      id: String(c?.voice_id ?? ""),
      label: String(c?.label ?? ""),
      lang: String(c?.sample_lang ?? "").toLowerCase(),
    })),
    ...(spec.localClones ?? []).map((c) => ({
      id: String(c?.id ?? ""),
      label: "",
      lang: String(c?.lang ?? "").toLowerCase(),
    })),
  ].filter((c) => c.id);
  const topClones = knownLang ? clones.filter((c) => c.lang === knownLang) : [];
  const restClones = clones.filter((c) => !topClones.includes(c));

  for (const c of topClones) push(c.id, cloneLabel(c, spec));
  for (const e of spec.catalog ?? []) {
    if (knownLang && String(e?.lang ?? "").toLowerCase() !== knownLang) continue;
    push(String(e?.id ?? ""), String(e?.label ?? ""));
  }
  for (const s of spec.localSpeakers ?? []) {
    const id = String(s ?? "");
    push(id, spec.cloneSuffix ? `${spec.presetLabels?.[id] ?? id}${spec.presetSuffix ?? ""}` : id);
  }
  for (const c of restClones) push(c.id, cloneLabel(c, spec));
  return out;
}

export interface ResolvePreviewLangSpec {
  /** 设置页音色字段键（speaker_cantonese/speaker_en 本地音色链）。 */
  fieldKey?: string;
  /** 显式回落语言（如人设主语言）；不在三态内时忽略。 */
  fallback?: string;
}

/**
 * 试听用哪种语言/文本：按音色 ID 正则判定，而不是按字段标签——
 * 粤语音色（Cantonese_*）即使被设成「整场同声」，试听也该用粤语示例文本，
 * 否则 MiniMax 会用粤语音色念普通话文字 → 广式普通话。
 * 正则不中再按字段键回落（settings 本地 Qwen 音色链）→ fallback → 普通话。
 * 返回 "cantonese" | "en" | "zh"。
 */
export function resolvePreviewLang(voice: string, spec: ResolvePreviewLangSpec = {}): PreviewLang {
  const v = String(voice || "");
  if (/^Cantonese_/i.test(v)) return "cantonese";
  if (/^(English_|socialmedia_)/i.test(v)) return "en";
  if (spec.fieldKey === "speaker_cantonese") return "cantonese";
  if (spec.fieldKey === "speaker_en") return "en";
  if (spec.fallback && KNOWN_LANGS.includes(spec.fallback)) return spec.fallback as PreviewLang;
  return "zh";
}

/**
 * 试听示例文本（三语含粤语）：设置页与 personas 页文案合并收编——personas 版
 * 带人设称呼句式，name 缺省回落「Bok 客服」（设置页无称呼语境即用缺省）。
 */
export function previewSampleText(lang: string, name = ""): string {
  const n = name.trim() || "Bok 客服";
  switch (lang) {
    case "cantonese":
      return `你好，我係${n}，唔該想問下件貨而家到咗未？可以幫我 check 下 status 嘛？`;
    case "en":
      return `Hello, this is ${n}. How can I help you today?`;
    default:
      return `你好，我是${n}，请问有什么可以帮您？`;
  }
}

/** QA 词条罐头音状态面（与 CP /api/qa/canned-status 的 statuses 值同形）。 */
export interface CannedStateEntry {
  state?: "ok" | "missing";
}

/**
 * 试听路由：状态面标记有录音 → 缓存（零云费）；缺录音 → 现场合成烧云配额仅
 * 主管（live），非主管拦截（blocked，页面出提示）。状态面未知（未加载/TTL 滞后）
 * 回落 cache 先试取——页面在取播失败时仍按角色降级，与旧「先 fetch 再降级」等价。
 */
export function decideAuditionPath(
  cannedState: CannedStateEntry | undefined,
  isManager: boolean,
): "cache" | "live" | "blocked" {
  if (cannedState?.state === "missing") return isManager ? "live" : "blocked";
  return "cache";
}
