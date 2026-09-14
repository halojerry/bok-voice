/**
 * 话术步骤 ref 的结构化解析 / 序列化 / 保存前校验（与后端 flow.py 同构）。
 *
 * ref 的存储形态不变（steps_json 里每步 {goal, ref, say?}，ref 是一个多行
 * 字符串）——本模块只负责「ref 文本 ↔ 结构化编辑模型」的双向转换，让模板
 * 编辑器把「分支 / 注意」从纯文本约定升级成结构化编辑，同时保证：
 *   1. 序列化输出与后端正则逐字兼容（AI 真能收到分支/注意）；
 *   2. 无损往返：parse(serialize(parse(ref))) === parse(ref)（结构相等），
 *      规范书写的 ref 甚至字节不变；
 *   3. 绝不丢数据：任何解析不出结构的行原样进 unknownLines（原文行）。
 *
 * 后端唯一权威实现在 apps/agent/agent_runtime/flow.py（历史事故：EN 模板
 * 18 条分支因行头写法不认整批静默失效数月，0913 审计才浮出——所以本模块
 * 的行头判定必须与后端正则逐条对齐，改动前先读后端）：
 *   - _BRANCH_LINE_RE   flow.py:81   分支行：^(如果客户 | If/When the customer)<cond 1-120 字> → <resp>
 *   - _NOTE_LINE_RE     flow.py:85   注意行：^(注意 | Notes?)[:：]<note>
 *   - parse_step_ref()  flow.py:116  逐行归类：分支 → 注意 → 正稿（首个非空
 *                                     非指令行，即使行内含「→」也算正稿，
 *                                     flow.py:131）→ 其余含「→」行=未识别指令
 *                                     （flow.py:133 告警后忽略）→ 再其余静默忽略
 *   - step_say_text()   flow.py:915  直念步取 ref 首个非空行（变量渲染后无残留占位）
 *   - object_vars()     flow.py:232  {变量} 的真实全集（下面 KNOWN_VARS 的依据）
 *   - _say_step_cap()   agent.py:468 直念长度护栏：>80 字（BOK_SAY_STEP_LIMIT）
 *                                     截首句，运营规约 ≤50 字/句
 */

/** 一条分支：条件（「如果客户」后面的部分）+ 应对（→ 后面的部分），均已 strip。 */
export interface StepRefBranch {
  cond: string;
  resp: string;
}

/** ref 的结构化编辑模型。serialize 按 正稿 → 分支 → 注意 → 原文行 的顺序输出。 */
export interface StepRefModel {
  /**
   * 主话术（非指令行，按原 ref 中的相对顺序）。
   * 后端正稿只注入第一行（flow.py:131 只认首个非空非指令行，其余行静默忽略）
   * ——编辑器全量保留并提示；第一行允许含「→」（后端同样当正稿）。
   */
  scriptLines: string[];
  /** 分支行（flow.py:81 正则认出的行）。 */
  branches: StepRefBranch[];
  /** 注意行（flow.py:85 正则认出的行，已剥「注意:/Note:」前缀）。 */
  notes: string[];
  /**
   * 未识别行：含「→」但行头后端不认（如「If they …→」「客户报号→复述」）。
   * 原样保真——绝不丢数据；后端不会把它当分支/注意生效（flow.py:133 告警+忽略）。
   */
  unknownLines: string[];
}

export function emptyStepRefModel(): StepRefModel {
  return { scriptLines: [], branches: [], notes: [], unknownLines: [] };
}

// ---- 与后端正则逐字对齐（u 标志=按码点计数/量词，对齐 Python str 语义）----

/** flow.py:81 _BRANCH_LINE_RE。IGNORECASE：if/when 任意大小写都认。 */
const BRANCH_LINE_RE = /^(?:如果客户|(?:If|When)\s+the\s+customer)\s*(.{1,120}?)\s*→\s*(\S.*)$/iu;
/** flow.py:85 _NOTE_LINE_RE。注意「Notes?」复数也认；全半角冒号都认。 */
const NOTE_LINE_RE = /^(?:注意|Notes?)\s*[:：]\s*(.+)$/iu;

/** 分支行头（序列化用）：按当前模板语言选前缀（zh/cantonese 共用「如果客户」，en 用「If the customer」）。 */
export function branchPrefix(lang: string): string {
  return lang === "en" ? "If the customer" : "如果客户";
}

/** 注意行头（序列化用）：zh/cantonese「注意:」（无空格，与 flow.py:1049 渲染同款），en「Note: 」。 */
export function notePrefix(lang: string): string {
  return lang === "en" ? "Note: " : "注意:";
}

/** 宽松行头探测（只用于校验提示，不参与解析归类）：
 *  以「如果 / If / When」开头的行多半是想写分支；以「注意 / Note」开头的行多半是想写注意。 */
const BRANCH_HEAD_LOOSE_RE = /^(?:如果|If|When)/i;
const NOTE_HEAD_LOOSE_RE = /^(?:注意|Notes?)\b/i;

/**
 * ref 文本 → 结构化模型。逐行归类顺序与 flow.py:116 parse_step_ref 完全一致：
 * 分支 → 注意 → 首个非空行当主话术（即使含 →）→ 含「→」的行进原文行 →
 * 其余行进主话术（后端会静默忽略，编辑器保留并提示）。
 */
export function parseStepRef(ref: string): StepRefModel {
  const model = emptyStepRefModel();
  // Python splitlines() 对齐：\r\n / \r / \n / U+2028 / U+2029 都算行界。
  for (const rawLine of String(ref ?? "").split(/\r\n|\r|\n|\u2028|\u2029/)) {
    const line = rawLine.trim();
    if (!line) continue;
    const bm = BRANCH_LINE_RE.exec(line);
    if (bm) {
      model.branches.push({ cond: bm[1].trim(), resp: bm[2].trim() });
      continue;
    }
    const nm = NOTE_LINE_RE.exec(line);
    if (nm) {
      model.notes.push(nm[1].trim());
      continue;
    }
    if (model.scriptLines.length === 0) {
      // flow.py:131 首个非空非指令行=正稿（行内含「→」也一样当正稿）。
      model.scriptLines.push(line);
      continue;
    }
    if (line.includes("→")) {
      // flow.py:133 未识别指令行（后端告警后忽略）；编辑器原样保留。
      model.unknownLines.push(line);
      continue;
    }
    // 后端静默丢弃的后续普通行；编辑器保真进主话术（校验会提示后端只注入首行）。
    model.scriptLines.push(line);
  }
  return model;
}

/**
 * 结构化模型 → ref 文本。顺序：主话术 → 分支 → 注意 → 原文行（与后端
 * parse_step_ref 的归类无关——它对行序不敏感，只认行头；这里选规范顺序）。
 * 条件与回复都为空的分支行、空注意行是编辑器瞬态，跳过不落 ref。
 * 条件或回复缺一边的分支行仍保真输出（该行后端正则不认会落进原文行，
 * 校验负责提示补全）——绝不因「不完整」而丢数据。
 */
export function serializeStepRef(model: StepRefModel, lang: string): string {
  const lines: string[] = [];
  const push = (s: string) => {
    const t = s.trim();
    if (t) lines.push(t);
  };
  model.scriptLines.forEach(push);
  const head = branchPrefix(lang);
  for (const b of model.branches) {
    const cond = b.cond.trim();
    const resp = b.resp.trim();
    if (!cond && !resp) continue;
    push(`${head}${lang === "en" ? " " : ""}${cond} → ${resp}`);
  }
  for (const n of model.notes) {
    const t = n.trim();
    if (!t) continue;
    push(`${notePrefix(lang)}${t}`);
  }
  model.unknownLines.forEach(push);
  return lines.join("\n");
}

/** 直念步实际会被念的「首行」：序列化后首个非空行（与 step_say_text 取首非空行对齐）。 */
export function firstSayLine(model: StepRefModel, lang: string): string {
  for (const line of serializeStepRef(model, lang).split("\n")) {
    const t = line.trim();
    if (t) return t;
  }
  return "";
}

// ---- {变量} 校验 ------------------------------------------------------------

/**
 * 已知变量全集 = flow.py:232 object_vars() 返回的键（比运营约定清单更全：
 * 「名字/快递公司/快递单号/收货地址/地址/电话」等都是后端真认的，不能误报）。
 */
export const KNOWN_VARS: ReadonlySet<string> = new Set([
  "姓名", "名字", "name",
  "快递单号", "快递尾号", "tracking_tail",
  "物流公司", "快递公司", "courier",
  "收货地址", "地址",
  "电话",
  "聯絡方式", "联系方式", "contact",
]);

/** 找出文本里不在 KNOWN_VARS 的 {占位符}（去重、保序）。 */
export function findUnknownVars(text: string): string[] {
  const out: string[] = [];
  for (const m of String(text ?? "").matchAll(/\{([^{}\n]+)\}/g)) {
    const name = (m[1] ?? "").trim();
    if (name && !KNOWN_VARS.has(name) && !out.includes(name)) out.push(name);
  }
  return out;
}

// ---- 保存前校验 ------------------------------------------------------------

export type StepRefIssueLevel = "warn" | "info";
export type StepRefBlock = "script" | "branch" | "note" | "unknown" | "step";

export interface StepRefIssue {
  level: StepRefIssueLevel; // warn=红（AI 收不到/会被截） info=黄（软提示，请确认）
  msg: string;
  /** 归属编辑块；branch/note/unknown 另带块内行下标。 */
  block: StepRefBlock;
  index?: number;
}

export interface ValidateStepRefOptions {
  /** 直念步（say=1）：校验首行长度。 */
  say?: boolean;
  /** 步骤目标也参与 {变量} 扫描。 */
  goal?: string;
  /** 序列化语言（算直念首行用），默认 zh。 */
  lang?: string;
}

/**
 * 单步校验（纯函数，不发请求）。规则与后端行为一一对应，见各行内注释。
 * warn = 这行 AI 现在收不到/会被截断；info = 软提示，请人工确认。
 */
export function validateStepRef(model: StepRefModel, opts: ValidateStepRefOptions = {}): StepRefIssue[] {
  const issues: StepRefIssue[] = [];
  const lang = opts.lang ?? "zh";

  // ---- 主话术块 ----
  if (model.scriptLines.length > 1) {
    issues.push({
      level: "info",
      block: "script",
      msg: "后端正稿只注入第一行（flow.py parse_step_ref），其余行 AI 不会逐字看到——建议并入第一行，或把内容改写成下方分支/注意。",
    });
  }
  model.scriptLines.forEach((line, i) => {
    if (i > 0 && line.includes("→")) {
      issues.push({
        level: "warn",
        block: "script",
        index: i,
        msg: `第 ${i + 1} 行带「→」但行头不是「如果客户/If the customer」，后端不会当分支（且正稿只注入首行）——请改写成下方分支行。`,
      });
    }
    if (BRANCH_HEAD_LOOSE_RE.test(line) && !line.includes("→") && !BRANCH_LINE_RE.test(line)) {
      issues.push({
        level: "warn",
        block: "script",
        index: i,
        msg: `第 ${i + 1} 行像分支但缺少「→」箭头，AI 收不到——写法：如果客户… → …（EN：If the customer … → …）。`,
      });
    }
    if (NOTE_HEAD_LOOSE_RE.test(line) && !NOTE_LINE_RE.test(line)) {
      issues.push({
        level: "warn",
        block: "script",
        index: i,
        msg: `第 ${i + 1} 行像注意但写法后端不认——规范写法「注意:内容」或「Note: 内容」（中英冒号均可）。`,
      });
    }
  });

  // ---- 分支行 ----
  model.branches.forEach((b, i) => {
    const cond = b.cond.trim();
    const resp = b.resp.trim();
    if (!cond && !resp) return; // 空行是编辑器瞬态，序列化时会跳过
    if (!cond || !resp) {
      issues.push({
        level: "warn",
        block: "branch",
        index: i,
        msg: !cond
          ? "分支缺少条件（「如果客户」后面要有内容），AI 收不到这条分支。"
          : "分支缺少「→」后的回复内容，AI 收不到这条分支。",
      });
    }
    if ([...cond].length > 120) {
      // flow.py:81 条件捕获组上限 120 字（码点），超限整行不被认成分支。
      issues.push({
        level: "warn",
        block: "branch",
        index: i,
        msg: "分支条件超过 120 字，后端正则不认（_BRANCH_LINE_RE 上限），这条分支 AI 收不到——请缩短条件。",
      });
    }
  });

  // ---- 原文行（未识别行）----
  model.unknownLines.forEach((line, i) => {
    if (line.includes("→") && !BRANCH_LINE_RE.test(line)) {
      issues.push({
        level: "warn",
        block: "unknown",
        index: i,
        msg: `第 ${i + 1} 行含「→」但行头后端不认——分支只认行头「如果客户…」「If the customer …」「When the customer …」，这条 AI 收不到；请在上方分支区重写一条。`,
      });
    } else if (BRANCH_HEAD_LOOSE_RE.test(line) && !line.includes("→")) {
      issues.push({
        level: "warn",
        block: "unknown",
        index: i,
        msg: `第 ${i + 1} 行像分支但缺少「→」箭头，AI 收不到——写法：如果客户… → …（EN：If the customer … → …）。`,
      });
    }
    if (NOTE_HEAD_LOOSE_RE.test(line) && !NOTE_LINE_RE.test(line)) {
      issues.push({
        level: "warn",
        block: "unknown",
        index: i,
        msg: `第 ${i + 1} 行像注意但写法后端不认——规范写法「注意:内容」或「Note: 内容」（中英冒号均可）。`,
      });
    }
  });

  // ---- {变量} 软提示（扫目标 + 全部行）----
  const varText = [opts.goal ?? "", ...model.scriptLines, ...model.notes, ...model.unknownLines,
    ...model.branches.map((b) => `${b.cond} ${b.resp}`)].join("\n");
  for (const v of findUnknownVars(varText)) {
    issues.push({
      level: "info",
      block: "step",
      msg: `变量 {${v}} 不是常用变量——请确认对象卡里有对应值，否则通话里会念出占位符原样或留空。`,
    });
  }

  // ---- 直念步首行长度（agent.py:468 _say_step_cap：>80 字截首句；规约 ≤50 字）----
  if (opts.say) {
    const sayLine = firstSayLine(model, lang);
    if ([...sayLine].length > 50) {
      issues.push({
        level: "warn",
        block: "step",
        msg: `直念首行 ${[...sayLine].length} 字过长：进入该步的当轮会逐字念这一行，超 80 字会被截首句（硬上限）——建议 ≤50 字或拆步。`,
      });
    }
  }

  return issues;
}
