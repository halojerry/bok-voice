// 意图表格纯函数（P2.5「意图表单化＋画布退役」，2026-09-21）：graph_json ↔ 表格行 的
// 双向转换与序列化＝意图编辑的**唯一写路径**（画布退役后不再直接拼 graph_json）。
//
// 引擎契约逐字节镜像 packages/core/bok_voice_core/flow_graph.py（CP 不 import agent_runtime，
// web 亦不 import python——两侧手工同步，改一处须同步另一处）：
//  · 常规意图 id `^[a-z][a-z0-9_]*$`（P2.2 放宽，挖掘产物的 snake_case 名才存得进图）；
//    **兜底保留 id `"*"`**（CATCHALL_INTENT_ID）：keywords 必空、无判据、其绑定 once 必须
//    false、全图唯一；语义＝常规意图与快答都没接住时的最后出口，不配＝落 AI 自由应答。
//  · 绑定 id `bnd_[0-9a-f]{8}`；action ∈ {play_qa, jump_step, notify_human}。
//
// **未改动 → 原字节返回**：saveIntentDoc 先算「基线行＝rowsFromDoc(parse(raw))」，当前行与
// 基线相等（营业员打开不改也点保存）即直接吐**原始 graph_json 串**（格式/键序/空白全保真），
// 绝不因一次无改动保存搅动存量数据；有改动才走规范化重建。

import {
  parseGraphDoc, JUDGE_PROMPT_MAX_CHARS,
  type GraphDoc, type GraphIntent, type GraphBinding,
} from "./qa-canvas";

export { JUDGE_PROMPT_MAX_CHARS };

/** 兜底意图保留 id（与 flow_graph.CATCHALL_INTENT_ID 同值）。 */
export const CATCHALL_INTENT_ID = "*";
/** 常规意图 id 硬规则（与 flow_graph._INTENT_ID_RE 同款）。 */
export const INTENT_ID_RE = /^[a-z][a-z0-9_]*$/;
/** 绑定 id 硬规则（与 flow_graph._ID_RE 同款）。 */
export const BINDING_ID_RE = /^bnd_[0-9a-f]{8}$/;

export type GraphAction = GraphBinding["action"];

/** 绑定行（表格/弹窗草稿面）：字段全部显式，序列化逐笔重建。 */
export type BindingRow = {
  id: string;
  action: GraphAction;
  qa_id: string;
  /** jump_step 目标步（1-based）。 */
  step: number;
  /** play_qa 播完跳转步（0=不跳）。 */
  then_jump: number;
  priority: number;
  once: boolean;
  enabled: boolean;
};

/** 意图行：一个意图 + 其全部绑定（表格一行＝一个意图）。 */
export type IntentRow = {
  id: string;
  label: string;
  /** 关键词数组（兜底恒空）。 */
  keywords: string[];
  /** 关键词原文（逗号分隔，表格列编辑面）。 */
  keywordsText: string;
  /** 生效步（1-based；空=全程）。 */
  steps: number[];
  /** 生效步原文（"1,3"；空=全程）。 */
  stepsText: string;
  /** 判据 prompt 原文（空=无判据）。 */
  judge: string;
  enabled: boolean;
  bindings: BindingRow[];
};

/** 是否兜底意图 id（保留字 "*"）。 */
export function isCatchallId(id: unknown): boolean {
  return String(id ?? "") === CATCHALL_INTENT_ID;
}

/**
 * 新建 id（crypto 随机）：CP `_ID_RE` 只收 `int_`/`bnd_` + 8 位小写 hex。
 * 2026-09-23（Mimosa insecure-randomness 修复）：删 Math.random 回退——目标
 * 环境（现代浏览器/node）恒有 crypto.getRandomValues；缺失=运行环境不支持，
 * 明确抛错好过静默降级成可预测 id。id 进 CP 且有 _ID_RE 形状校验兜底。
 */
export function genGraphId(prefix: "int_" | "bnd_"): string {
  const c = (globalThis as { crypto?: Crypto }).crypto;
  if (!c || typeof c.getRandomValues !== "function") {
    throw new Error("crypto.getRandomValues unavailable — cannot generate graph id");
  }
  const bytes = new Uint8Array(4);
  c.getRandomValues(bytes);
  return prefix + Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** 文本 → 关键词数组：逗号/顿号/分号/空白/换行分隔，去重保序。 */
export function parseKeywords(text: unknown): string[] {
  const out: string[] = [];
  for (const raw of String(text ?? "").split(/[,，、;；\n\r\t]+/)) {
    const t = raw.trim();
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}

/** 生效范围文本（"1,3"）→ 1-based 步号数组；空串=全程。 */
export function parseStepsText(text: unknown): number[] {
  const out: number[] = [];
  for (const t of String(text ?? "").split(/[,，、\s]+/)) {
    const n = Math.round(Number(t));
    if (t.trim() !== "" && Number.isFinite(n) && n >= 1 && n <= 999 && !out.includes(n)) out.push(n);
  }
  return out;
}

/** 常规意图 id 是否合法（兜底 "*" 另判）。 */
export function isIntentIdValid(id: unknown): boolean {
  return INTENT_ID_RE.test(String(id ?? "").trim());
}

/** 判据是否超长（CP 严格档 400 字）。 */
export function isJudgeTooLong(text: unknown): boolean {
  return String(text ?? "").trim().length > JUDGE_PROMPT_MAX_CHARS;
}

function num(v: unknown, dflt: number): number {
  const n = Math.round(Number(v));
  return Number.isFinite(n) ? n : dflt;
}

/** 绑定对象 → 行（宽容收窄；缺省值兜底）。 */
function bindingToRow(b: Partial<GraphBinding> | null | undefined): BindingRow {
  const action = (b?.action === "play_qa" || b?.action === "notify_human" || b?.action === "jump_step"
    ? b.action
    : "jump_step") as GraphAction;
  return {
    id: String(b?.id ?? ""),
    action,
    qa_id: String(b?.qa_id ?? ""),
    step: Math.max(1, num(b?.step, 1)),
    then_jump: Math.max(0, num(b?.then_jump, 0)),
    priority: num(b?.priority, 10),
    once: b?.once === true,
    enabled: b?.enabled !== false,
  };
}

/** graph_json → 表格行（顺序＝文档顺序；兜底行亦在内，由组件按 id 归位到表底）。 */
export function rowsFromDoc(doc: GraphDoc): IntentRow[] {
  const intents = Array.isArray(doc?.intents) ? doc.intents : [];
  const bindings = Array.isArray(doc?.bindings) ? doc.bindings : [];
  return intents.map((it) => {
    const id = String(it?.id ?? "");
    const kws = (Array.isArray(it?.keywords) ? it.keywords : []).map((k) => String(k));
    const steps = (Array.isArray(it?.steps) ? it.steps : [])
      .map((s) => Math.round(Number(s)))
      .filter((n) => Number.isFinite(n) && n >= 1);
    return {
      id,
      label: String(it?.label ?? ""),
      keywords: kws,
      keywordsText: kws.join("，"),
      steps,
      stepsText: steps.join(","),
      judge: String(it?.judge?.prompt ?? ""),
      enabled: it?.enabled !== false,
      bindings: bindings
        .filter((b) => String(b?.intent ?? "") === id)
        .map((b) => bindingToRow(b)),
    } satisfies IntentRow;
  });
}

/** 行 → 意图对象（规范化键序：id/label/keywords/steps/enabled[/judge]）。 */
function intentFromRow(row: IntentRow): GraphIntent | null {
  const id = String(row.id ?? "").trim();
  if (!id) return null;
  const catchAll = isCatchallId(id);
  if (!catchAll && !INTENT_ID_RE.test(id)) return null;
  const intent: GraphIntent = {
    id,
    label: String(row.label ?? ""),
    // 兜底 keywords 恒空（引擎严格档：有词＝不是兜底）。
    keywords: catchAll ? [] : [...row.keywords],
    steps: [...row.steps],
    enabled: row.enabled !== false,
  };
  if (!catchAll) {
    const prompt = String(row.judge ?? "").trim();
    if (prompt) intent.judge = { prompt };
  }
  return intent;
}

/** 行 → 绑定对象（键序＝bindingFromDraft 同款：common → action → 动作专属负载）。 */
function bindingFromRow(row: BindingRow, intentId: string): GraphBinding {
  const catchAll = isCatchallId(intentId);
  const common = {
    id: String(row.id),
    intent: intentId,
    priority: Math.min(Math.max(num(row.priority, 10), 0), 1000),
    // 兜底绑定 once 必须 false（引擎严格档 400）。
    once: catchAll ? false : row.once === true,
    enabled: row.enabled !== false,
  };
  if (row.action === "jump_step") {
    return { ...common, action: "jump_step", step: Math.max(1, num(row.step, 1)) };
  }
  if (row.action === "notify_human") {
    return { ...common, action: "notify_human" };
  }
  const out: GraphBinding = { ...common, action: "play_qa", qa_id: String(row.qa_id ?? "") };
  const tj = num(row.then_jump, 0);
  if (tj > 0) out.then_jump = tj;
  return out;
}

/** 深比较（键序敏感）：相等即在序列化时原样复用旧对象，保字节。 */
function sameJson(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/**
 * 行集 → 图文档：**顺序保持**（意图/绑定皆按原文档位次原地替换，新增项追加），
 * 未变项复用原始对象（键序/字节不动）。绑定归属跟随其行的 id。
 */
export function buildDoc(doc: GraphDoc, rows: IntentRow[]): GraphDoc {
  const srcIntents = Array.isArray(doc?.intents) ? doc.intents : [];
  const srcBindings = Array.isArray(doc?.bindings) ? doc.bindings : [];
  const byId = new Map<string, IntentRow>();
  for (const r of rows) byId.set(String(r.id), r);

  const intents: GraphIntent[] = [];
  const seen = new Set<string>();
  for (const orig of srcIntents) {
    const id = String(orig?.id ?? "");
    const row = byId.get(id);
    if (!row) continue; // 该意图被删除
    const built = intentFromRow(row);
    if (!built) continue;
    intents.push(sameJson(built, orig) ? orig : built);
    seen.add(id);
  }
  for (const row of rows) {
    const id = String(row.id);
    if (seen.has(id)) continue;
    const built = intentFromRow(row);
    if (built) {
      intents.push(built);
      seen.add(id);
    }
  }

  // 绑定：先按原文档顺序原地替换（未变复用旧对象），再把新增绑定按行序追加。
  const bindings: GraphBinding[] = [];
  const seenBindings = new Set<string>();
  for (const orig of srcBindings) {
    const intentId = String(orig?.intent ?? "");
    const row = byId.get(intentId);
    if (!row) continue; // 意图已删 → 悬空绑定一并丢
    const rb = row.bindings.find((b) => String(b.id) === String(orig?.id));
    if (!rb) continue; // 该绑定被删
    const built = bindingFromRow(rb, intentId);
    bindings.push(sameJson(built, orig) ? orig : built);
    seenBindings.add(String(rb.id));
  }
  for (const row of rows) {
    for (const rb of row.bindings) {
      if (seenBindings.has(String(rb.id))) continue;
      bindings.push(bindingFromRow(rb, String(row.id)));
      seenBindings.add(String(rb.id));
    }
  }

  return { version: 1, intents, bindings };
}

/** 图文档 → graph_json 串（顶层键序 version/intents/bindings 恒定）。 */
export function stringifyDoc(doc: GraphDoc): string {
  return JSON.stringify({ version: 1, intents: doc.intents, bindings: doc.bindings });
}

/** 两份行集是否逐字节等价（用于「未改动」判定）。 */
export function rowsEqual(a: IntentRow[], b: IntentRow[]): boolean {
  return sameJson(a, b);
}

/**
 * 保存路径唯一入口：当前行 → graph_json。
 * 行集与原始行的基线相等（未改动）→ **原字节返回**（保格式/键序/空白）；
 * 有改动 → 规范化重建（复用未变项对象，只有真改过的项才重排/重写）。
 */
export function saveIntentDoc(originalRaw: string, rows: IntentRow[]): string {
  const raw = String(originalRaw ?? "");
  const doc = parseGraphDoc(raw);
  const baseline = rowsFromDoc(doc);
  if (rowsEqual(rows, baseline)) return raw;
  return stringifyDoc(buildDoc(doc, rows));
}
