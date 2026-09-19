// 变量 tab 纯函数（W3 T2）：占位符目录、扫描、渲染、直念行可念性判定。
//
// 语义逐条镜像 agent 运行时 apps/agent/agent_runtime/flow.py：
//   - object_vars（:252-289）：对象卡 → 变量映射（缺的留空）；
//   - render_template_text（:291-298）：{变量} 替换，**空串/缺失一律保留原占位**；
//   - _CANTONESE_DIGITS/digits_to_cantonese（:207-221）：数字逐位汉字（7890→七八九零），
//     EN 模板 tracking_tail 例外保留 ASCII（英文 TTS 直读，汉字会读错）；
//   - step_say_text/opening_text（:1020-1045）：直念行=首个非空行，渲染后仍含 {占位}
//     的行运行时跳过（开场白退通用语、直念步退 LLM）。
//
// 本文件必须保持零 import（test/var-panel.test.mjs 按 flow-canvas.test.mjs 同款
// 单文件转译直载），类型自持，结构与 components/template-editor 的 FlowStep 兼容。

/** 步骤形状（template-editor.FlowStep 兼容面）。 */
export type VarFlowStep = {
  goal: string;
  ref: string;
  say?: boolean;
};

/** 旧式四段（steps_json 为空时的兜底扫描源，flow.py:225 _LEGACY_STEP_GOALS 同域）。 */
export type LegacySections = {
  opening?: string;
  core?: string;
  objection?: string;
  closing?: string;
};

/** 数字变换口径（对齐 flow.py object_vars 各键的取值方式）。 */
export type VarTransform = "raw" | "digitsCn" | "digitsCnTail4" | "asciiTail4";

/** 占位符目录条目：keys=同值别名组（trim 后精确匹配，无繁简归一——「联系方式」与
 * 「聯絡方式」是两个 key 指向同一字段值）；column=来源对象字段；transform=数字变换；
 * channelDefault=联系渠道语言缺省语义（flow.py:268-271）。 */
export type PlaceholderCatalogEntry = {
  keys: string[];
  column: string;
  transform: VarTransform;
  channelDefault?: boolean;
};

/** flow.py:207 _CANTONESE_DIGITS 逐字照抄（0 读「零」，1-9 对应汉字）。 */
const CANTONESE_DIGITS: Record<string, string> = {
  "0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
  "5": "五", "6": "六", "7": "七", "8": "八", "9": "九",
};

/** flow.py:213 digits_to_cantonese：字符串里数字逐位转汉字（SF7890→SF七八九零），
 * 其余字符原样。 */
export function digitsToCn(text: string): string {
  const s = String(text ?? "");
  if (!s) return s;
  let out = "";
  for (const ch of s) out += CANTONESE_DIGITS[ch] ?? ch;
  return out;
}

/** 占位符目录（逐条对齐 flow.py:252-289 object_vars 返回表的每个键）。 */
export const PLACEHOLDER_CATALOG: PlaceholderCatalogEntry[] = [
  { keys: ["姓名", "名字", "name"], column: "display_name", transform: "raw" },
  { keys: ["快递单号"], column: "tracking_no", transform: "digitsCn" },
  { keys: ["快递尾号"], column: "tracking_no", transform: "digitsCnTail4" },
  // EN 模板占位用英文名：尾号保留阿拉伯数字（flow.py:282 注释同款口径）。
  { keys: ["tracking_tail"], column: "tracking_no", transform: "asciiTail4" },
  { keys: ["物流公司", "快递公司", "courier"], column: "courier", transform: "raw" },
  { keys: ["收货地址", "地址"], column: "address", transform: "raw" },
  { keys: ["电话"], column: "phone", transform: "digitsCn" },
  // 联系渠道：对象显式指定优先，缺省按对象语言（zh→微信 / 其余→WhatsApp）。
  { keys: ["聯絡方式", "联系方式", "contact"], column: "contact_channel", transform: "raw", channelDefault: true },
];

/** 目录键 → 条目索引（trim 后精确匹配，无繁简/大小写归一）。 */
function catalogIndex(): Map<string, PlaceholderCatalogEntry> {
  const map = new Map<string, PlaceholderCatalogEntry>();
  for (const e of PLACEHOLDER_CATALOG) {
    for (const k of e.keys) map.set(k, e);
  }
  return map;
}
const CATALOG = catalogIndex();

function strField(obj: Record<string, unknown> | null | undefined, key: string): string {
  const v = obj == null ? undefined : obj[key];
  return typeof v === "string" ? v.trim() : v == null ? "" : String(v).trim();
}

/** 联系渠道语言缺省（flow.py:268-271：zh→微信，其余含 cantonese/en/未知→WhatsApp）。 */
function channelForLang(lang: string): string {
  return lang === "zh" ? "微信" : "WhatsApp";
}

/** 对象行 → 变量映射（flow.py:252-289 object_vars 同构；空串=缺失，渲染层保留占位）。 */
export function varsMapFor(obj: Record<string, unknown> | null | undefined): Record<string, string> {
  const tracking = strField(obj, "tracking_no");
  const tail = tracking.length >= 4 ? tracking.slice(-4) : tracking;
  const lang = strField(obj, "language").toLowerCase() || "zh";
  const channel = strField(obj, "contact_channel") || channelForLang(lang);
  const map: Record<string, string> = {};
  for (const e of PLACEHOLDER_CATALOG) {
    let val: string;
    if (e.channelDefault) val = channel;
    else if (e.transform === "raw") val = strField(obj, e.column);
    else if (e.transform === "digitsCn") val = digitsToCn(strField(obj, e.column));
    else if (e.transform === "digitsCnTail4") val = digitsToCn(tail);
    else val = tail; // asciiTail4：EN 尾号保留 ASCII
    for (const k of e.keys) map[k] = val;
  }
  return map;
}

// flow.py:297 render_template_text 的占位正则同款：键=花括号间任意非花括号内容
// （不含换行的字符集在 JS 用 [^{}]+ 表达,语义一致——花括号内跨行不存在）。
const PLACEHOLDER_RE = /\{([^{}]+)\}/g;

/** 渲染 {变量}（flow.py:291-298 render_template_text 同构）：键 trim 后精确匹配；
 * **值空串/键未知一律保留原占位**（LLM 会向客户询问而非编造）。 */
export function renderVarsText(
  text: string,
  obj: Record<string, unknown> | null | undefined,
): string {
  const s = String(text ?? "");
  if (!s) return s;
  const vars = varsMapFor(obj);
  return s.replace(PLACEHOLDER_RE, (whole, key: string) => {
    const val = vars[String(key).trim()] ?? "";
    return val || whole;
  });
}

// —— 行分类（parseStepRefParts 同款三段拆分的简化内嵌版,零 import）——
// flow.py:96/:99 _BRANCH_LINE_RE/_NOTE_LINE_RE 的移植（与 lib/flow-canvas.ts 逐语义一致）。
const BRANCH_RE = /^(如果客户|(?:If|When)\s+the\s+customer)\s*(.{1,120}?)\s*→\s*(\S.*)$/i;
const NOTE_RE = /^(?:注意|Notes?)\s*[:：]\s*(.+)$/i;

export type RefSection = "script" | "branch" | "note";
export type TextSection = RefSection | "goal" | "opening" | "core" | "objection" | "closing";

/** 行 → 段落分类（正稿/分支/注意；空行返回 null 不参与扫描）。 */
function classifyLine(line: string): RefSection | null {
  const t = line.trim();
  if (!t) return null;
  if (BRANCH_RE.test(t)) return "branch";
  if (NOTE_RE.test(t)) return "note";
  return "script";
}

export type PlaceholderHit = {
  /** 占位键原样（未 trim）；匹配目录时 trim 后精确比对。 */
  key: string;
  /** 出现次数（全部扫描范围累计）。 */
  count: number;
  /** 出现的步下标（0-based，升序去重；legacy 四段时恒为 []）。 */
  stepIdxs: number[];
  /** 首次出现处的段落。 */
  section: TextSection;
  /** trim 后不在目录=无效占位（告警条）。 */
  unknown: boolean;
};

/** 扫描模板占位符：每步 goal + ref（ref 按分支/注意/正稿行分类扫描）；steps 为空时
 * 扫 legacy 四段文本。同 key 归并为一条（首现段落/步集/累计次数）。 */
export function scanPlaceholders(
  steps: VarFlowStep[],
  legacy?: LegacySections,
): PlaceholderHit[] {
  const byKey = new Map<string, PlaceholderHit>();
  const record = (rawKey: string, section: TextSection, stepIdx: number | null) => {
    const key = String(rawKey);
    const trimmed = key.trim();
    if (!trimmed) return; // 空键 {} 不成占位（正则要求 + 才命中,防御性跳过）
    let hit = byKey.get(trimmed);
    if (!hit) {
      hit = { key, count: 0, stepIdxs: [], section, unknown: !CATALOG.has(trimmed) };
      byKey.set(trimmed, hit);
    }
    hit.count += 1;
    if (stepIdx !== null && !hit.stepIdxs.includes(stepIdx)) hit.stepIdxs.push(stepIdx);
  };

  const scanText = (text: string, sectionOf: (line: string) => TextSection, stepIdx: number | null) => {
    for (const line of String(text ?? "").split(/\r?\n/)) {
      if (!line.trim()) continue;
      const section = sectionOf(line);
      for (const m of line.matchAll(PLACEHOLDER_RE)) record(m[1], section, stepIdx);
    }
  };

  for (let i = 0; i < (steps ?? []).length; i++) {
    const s = steps[i];
    scanText(s.goal, () => "goal", i);
    // ref 三段同款拆分：分支行/注意行/正稿行各自归类（变量三分支都可能出现在话术里）。
    scanText(s.ref, (line) => classifyLine(line) ?? "script", i);
  }
  if ((steps ?? []).length === 0 && legacy) {
    (["opening", "core", "objection", "closing"] as const).forEach((k) => {
      const text = String(legacy[k] ?? "");
      if (text.trim()) scanText(text, () => k, null);
    });
  }
  // 稳定序：目录内按目录顺序、未知键殿后（按键名序）。
  const catalogOrder = new Map<string, number>();
  PLACEHOLDER_CATALOG.forEach((e, i) => e.keys.forEach((k) => catalogOrder.set(k, i)));
  return [...byKey.values()].sort((a, b) => {
    const oa = catalogOrder.get(a.key.trim());
    const ob = catalogOrder.get(b.key.trim());
    if (oa !== undefined && ob !== undefined) return oa - ob;
    if (oa !== undefined) return -1;
    if (ob !== undefined) return 1;
    return a.key.localeCompare(b.key);
  });
}

export type RenderableLine = {
  /** 步下标（0-based）。 */
  stepIdx: number;
  /** opening=第 1 步首行（开场白恒直念,force 语义）；say=直念步首行。 */
  kind: "opening" | "say";
  /** 原文首行（未渲染）。 */
  raw: string;
  /** renderVarsText 后的行。 */
  rendered: string;
  /** 渲染后仍含 {占位}=变量缺失 → 运行时跳过该行（开场白退通用语/直念步退 LLM,
   * flow.py:1031-1045）。 */
  dropped: boolean;
};

/** 直念行清单：第 1 步首行（开场白,恒取）+ 各 say 步首行。行来源=ref 首个非空行
 * （ref 空退 goal,flow.py:1038 `s.ref or s.goal` 同款）；渲染后仍含 { 的行 dropped。 */
export function renderableLines(
  steps: VarFlowStep[],
  obj: Record<string, unknown> | null | undefined,
): RenderableLine[] {
  const out: RenderableLine[] = [];
  (steps ?? []).forEach((s, i) => {
    const isOpening = i === 0;
    if (!isOpening && s.say !== true) return;
    const source = String(s.ref ?? "").trim() ? s.ref : s.goal;
    const firstLine =
      String(source ?? "")
        .split(/\r?\n/)
        .map((l) => l.trim())
        .find((l) => l !== "") ?? "";
    if (!firstLine) return;
    const rendered = renderVarsText(firstLine, obj);
    out.push({
      stepIdx: i,
      kind: isOpening ? "opening" : "say",
      raw: firstLine,
      rendered,
      dropped: /\{[^{}]+\}/.test(rendered),
    });
  });
  return out;
}
