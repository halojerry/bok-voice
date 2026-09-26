"use client";

// 意图管理（PRD 3.3 / 需求6，2026-09-20 第二批）：第一层意图（graph_json intents）
// 表格+弹窗编辑——业务人员在 AI 工作站内直接维护名称/关键词/判据/生效范围/绑定动作，
// 不再跳问答画布。写路径=重建 GraphDoc → updateTemplate({graph_json})（与问答画布同一条）。
// 引擎契约（packages/core/bok_voice_core/flow_graph.py）：关键词确定性命中；judge=关键词
// 未中时 9B 背景判定（prompt ≤400 字）；绑定动作 play_qa（可带 then_jump）/jump_step/notify_human。
// 意图无绑定=零行为（inert）——种子意图可安全预置，绑定由业务按需挂。

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { ErrorState } from "@/components/app-shell";
import { downloadCsv, parseBoolCell, splitListCell } from "@/lib/csv";
import TableImport, {
  buildExampleCsvRows, rowOk, rowSkip, type ImportResult, type ParsedRow,
} from "@/components/table-import";
import {
  bindingFromDraft, intentJudgeField, parseGraphDoc, parseTemplateSteps,
  JUDGE_PROMPT_MAX_CHARS, type GraphBinding, type GraphIntent,
} from "@/lib/qa-canvas";
import type { SeedIntent } from "@/lib/seed-pack";
import type { TemplateRow } from "@/components/template-editor";

/** 关键词展示/落库上限（引擎无硬限；PRD 口径 200——超限保存拦截而非静默截断）。 */
const KEYWORD_LIMIT = 200;

const LANG_LABEL: Record<string, string> = { zh: "普通话", cantonese: "粤语", en: "English" };

// ---- 表格导入契约（列=意图ID,显示名,关键词,判据提示词,动作,动作目标,优先级,仅一次；
//      组装 graph_json 走既有保存，整表替换前弹确认）。----
const INTENT_IMPORT_COLUMNS = [
  { key: "id", label: "意图ID", hint: "可空自动生成；int_+8位小写hex 才原样使用" },
  { key: "label", label: "显示名", hint: "必填，≤64 字" },
  { key: "kw", label: "关键词", hint: "必填；分号/逗号分隔，≤32 个每个≤64 字" },
  { key: "judge", label: "判据提示词", hint: "可空；≤400 字" },
  { key: "action", label: "动作", hint: "play_qa/jump_step/notify_human/无，可空=无" },
  { key: "target", label: "动作目标", hint: "play_qa=词条ID；jump_step=步号；其余留空" },
  { key: "prio", label: "优先级", hint: "数字，可空=10（小者先）" },
  { key: "once", label: "仅一次", hint: "是/否，可空=否" },
];
const INTENT_IMPORT_FILENAME = "intents-example.csv";
const INTENT_IMPORT_EXAMPLE: string[][] = [
  ["", "客户投诉", "投诉;我要投诉;找你们领导", "客户表达不满或要求说法算命中；单纯询问细节不算。", "notify_human", "", "5", "是"],
  ["", "问理赔进度", "进度;几时赔;到哪一步", "", "play_qa", "粘贴快答词条ID", "10", "否"],
  ["", "不感兴趣", "不需要;别打了;唔使", "", "jump_step", "4", "20", "否"],
];

/** CP flow_graph `_ID_RE` 同款：意图/绑定 id 只收 int_/bnd_ + 8 位小写 hex。 */
const INTENT_ID_RE = /^int_[0-9a-f]{8}$/;

/** crypto 随机生成合法 id（与 /qa 页 genGraphId 同款，非 Math.random）。 */
function genHexId(prefix: "int_" | "bnd_"): string {
  const bytes = crypto.getRandomValues(new Uint8Array(4));
  return prefix + Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** 表格一行解析出的意图数据（预览/导入共用）。 */
type IntentImportRow = {
  intentId: string;
  /** 填了但不合法的原 ID（仅提示用；落库一律用 intentId）。 */
  providedId: string;
  label: string;
  keywords: string[];
  judge: string;
  /** ""=无动作（意图零行为，绑定由后续编辑补）。 */
  action: "play_qa" | "jump_step" | "notify_human" | "";
  qaId: string;
  /** jump_step 目标步号（1 基；其它动作=0）。 */
  step: number;
  priority: number;
  once: boolean;
};

const ACTIONS: [GraphBinding["action"], string][] = [
  ["jump_step", "跳到指定步骤"],
  ["play_qa", "播快答（QA 词条）"],
  ["notify_human", "通知人工（打铃不暂停）"],
];

// id 生成一律走 genHexId（int_/bnd_ + 8 位小写 hex，CP `_ID_RE` 唯一合法形状）。
// 2026-09-26 实弹：旧 rid()（Math.random base36、6 字符、含非 hex 字符）产出的
// int_88skq2 被 CP validate_flow_graph 400 拒——新建意图/加动作/基础意图包三条
// 路全灭且 UI 只剩裸 "400 Bad Request"。删 rid，四处调用点全换 genHexId。

/** 文本 → 关键词数组：逗号/顿号/分号/空白/换行分隔，去重保序。 */
function parseKeywords(text: string): string[] {
  const out: string[] = [];
  for (const raw of String(text ?? "").split(/[,，、;；\n\r\t]+/)) {
    const t = raw.trim();
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}

/** 生效范围文本（"1,3"）→ 1-based 步号数组；空串=全程。 */
function parseSteps(text: string): number[] {
  return String(text ?? "")
    .split(/[,，、\s]+/)
    .map((t) => Math.round(Number(t)))
    .filter((n) => Number.isFinite(n) && n >= 1 && n <= 999);
}

type BindingDraft = {
  id: string;
  action: GraphBinding["action"];
  qa_id: string;
  step: number;
  then_jump: number;
  priority: number;
  once: boolean;
  enabled: boolean;
};

/** 意图编辑弹窗草稿（加载自已存意图或新建空壳）。 */
type IntentDraft = {
  id: string;
  label: string;
  keywords: string[];
  stepsText: string;
  judge: string;
  enabled: boolean;
  bindings: BindingDraft[];
};

function emptyDraft(): IntentDraft {
  return {
    id: genHexId("int_"),
    label: "",
    keywords: [],
    stepsText: "",
    judge: "",
    enabled: true,
    bindings: [],
  };
}

function toDraft(it: GraphIntent, bindings: GraphBinding[]): IntentDraft {
  return {
    id: it.id,
    label: it.label || "",
    keywords: [...(it.keywords ?? [])],
    stepsText: (it.steps ?? []).join(","),
    judge: it.judge?.prompt ?? "",
    enabled: it.enabled !== false,
    bindings: bindings
      .filter((b) => b.intent === it.id)
      .map((b) => ({
        id: b.id || genHexId("bnd_"),
        action: b.action,
        qa_id: String(b.qa_id ?? ""),
        step: Number(b.step ?? 1) || 1,
        then_jump: Number(b.then_jump ?? 0) || 0,
        priority: Number(b.priority ?? 10),
        once: b.once === true,
        enabled: b.enabled !== false,
      })),
  };
}

const inputCls =
  "w-full rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--live)";

/** 意图编辑弹窗（受控草稿；保存时逐项校验并重建绑定）。 */
function IntentModal(props: {
  draft: IntentDraft;
  stepCount: number;
  /** 仅本模板语言的词条（语言过滤在上游做；弹窗只渲染）。 */
  qaRows: { id?: string; question_text?: string }[];
  langLabel: string;
  readOnly: boolean;
  onChange: (next: IntentDraft) => void;
  onClose: () => void;
  onSubmit: () => void;
  busy: boolean;
  err: string;
}) {
  const { draft, readOnly } = props;
  const patch = (p: Partial<IntentDraft>) => props.onChange({ ...draft, ...p });
  const patchBinding = (i: number, p: Partial<BindingDraft>) =>
    patch({ bindings: draft.bindings.map((b, j) => (j === i ? { ...b, ...p } : b)) });
  const qaOptions = props.qaRows.slice(0, 300);

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-foreground/30 p-4">
      <div className="w-full max-w-2xl space-y-3 rounded-xl border border-(--card-border) bg-background p-4 shadow-lg">
        <div className="flex items-center justify-between">
          <span className="label">{draft.label ? "编辑意图" : "新建意图"}</span>
          <button className="btn-ghost px-2 py-0.5 text-xs" onClick={props.onClose}>关闭 ✕</button>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block">
            <span className="text-xs muted">意图名称 *</span>
            <input
              className={`mt-1 ${inputCls}`}
              maxLength={64}
              value={draft.label}
              disabled={readOnly}
              placeholder="如：拒绝回答 / 肯定 / 否定"
              onChange={(e) => patch({ label: e.target.value })}
            />
          </label>
          <label className="block">
            <span className="text-xs muted">生效范围（步号，逗号分隔；留空=全程生效）</span>
            <input
              className={`mt-1 ${inputCls}`}
              value={draft.stepsText}
              disabled={readOnly}
              placeholder={`如：1,3（共 ${props.stepCount} 步）`}
              onChange={(e) => patch({ stepsText: e.target.value })}
            />
          </label>
        </div>

        <div>
          <span className="text-xs muted">关键词 *（客户说到任一关键词即命中；批量粘贴自动拆分）</span>
          <textarea
            className={`mt-1 h-16 ${inputCls}`}
            disabled={readOnly}
            placeholder="每行一个，或用逗号/顿号分隔：不用了, 唔使, 别打了"
            onChange={(e) => patch({ keywords: parseKeywords(e.target.value).slice(0, KEYWORD_LIMIT) })}
            defaultValue={draft.keywords.join("\n")}
            key={draft.id}
          />
          {draft.keywords.length > 0 && (
            <div className="mt-1.5 flex flex-wrap items-center gap-1">
              {draft.keywords.slice(0, 30).map((k) => (
                <span key={k} className="rounded-full border border-(--card-border) px-1.5 py-0.5 text-[10px]">
                  {k}
                  {!readOnly && (
                    <button
                      className="ml-1 text-red-500"
                      title="移除"
                      onClick={() => patch({ keywords: draft.keywords.filter((x) => x !== k) })}
                    >
                      ×
                    </button>
                  )}
                </span>
              ))}
              {draft.keywords.length > 30 && (
                <span className="text-[10px] muted">…共 {draft.keywords.length} 个</span>
              )}
              <span className="ml-auto text-[10px] muted">
                {draft.keywords.length}/{KEYWORD_LIMIT}
                <button
                  className="ml-2 underline decoration-dotted"
                  title="用 & 连接复制全部关键词"
                  onClick={() => void navigator.clipboard?.writeText(draft.keywords.join("&"))}
                >
                  复制(&)
                </button>
              </span>
            </div>
          )}
        </div>

        <label className="block">
          <span className="text-xs muted">
            判据（可选，≤{JUDGE_PROMPT_MAX_CHARS} 字）：关键词没命中时由后台大模型按这段描述判断
          </span>
          <textarea
            className={`mt-1 h-16 ${inputCls}`}
            maxLength={JUDGE_PROMPT_MAX_CHARS}
            disabled={readOnly}
            value={draft.judge}
            placeholder="写清什么算命中、什么不算（含正反例），如：客户明确表达不愿意继续通话才算命中；单纯询问细节不算。"
            onChange={(e) => patch({ judge: e.target.value })}
          />
        </label>

        <div>
          <div className="flex items-center justify-between">
            <span className="text-xs muted">命中后的动作（可多个；同轮多命中只执行优先级最小的一个）</span>
            {!readOnly && (
              <button
                className="btn-ghost px-2 py-0.5 text-xs"
                onClick={() =>
                  patch({
                    bindings: [
                      ...draft.bindings,
                      { id: genHexId("bnd_"), action: "jump_step", qa_id: "", step: 1, then_jump: 0, priority: 10, once: true, enabled: true },
                    ],
                  })
                }
              >
                ＋ 加动作
              </button>
            )}
          </div>
          <p className="mt-1 text-[11px] muted">
            「播快答」选词条只列出本模板语言（{props.langLabel}）的；其他语言词条到 /qa 页管理。
          </p>
          <div className="mt-1 space-y-2">
            {draft.bindings.length === 0 && (
              <p className="text-[11px] muted">暂无动作——意图命中后不会做任何事（可先建意图再挂动作）。</p>
            )}
            {draft.bindings.map((b, i) => (
              <div key={b.id} className="flex flex-wrap items-center gap-1.5 rounded-lg bg-muted/60 p-2">
                <select
                  className="select text-xs"
                  value={b.action}
                  disabled={readOnly}
                  onChange={(e) => patchBinding(i, { action: e.target.value as GraphBinding["action"] })}
                >
                  {ACTIONS.map(([v, l]) => (
                    <option key={v} value={v}>{l}</option>
                  ))}
                </select>
                {b.action === "jump_step" && (
                  <label className="flex items-center gap-1 text-[11px] muted">
                    跳到第
                    <input
                      type="number"
                      min={1}
                      max={Math.max(props.stepCount, 1)}
                      className="input w-16 text-xs"
                      value={b.step}
                      disabled={readOnly}
                      onChange={(e) => patchBinding(i, { step: Math.round(Number(e.target.value)) || 1 })}
                    />
                    步
                  </label>
                )}
                {b.action === "play_qa" && (
                  <>
                    <select
                      className="select max-w-52 truncate text-xs"
                      value={b.qa_id}
                      disabled={readOnly}
                      onChange={(e) => patchBinding(i, { qa_id: e.target.value })}
                    >
                      <option value="">选择 QA 词条…</option>
                      {qaOptions.map((q) => (
                        <option key={String(q.id ?? "")} value={String(q.id ?? "")}>
                          {String(q.question_text ?? "(无问法)").slice(0, 24)}
                        </option>
                      ))}
                    </select>
                    <label className="flex items-center gap-1 text-[11px] muted">
                      播完跳第
                      <input
                        type="number"
                        min={0}
                        max={Math.max(props.stepCount, 1)}
                        className="input w-14 text-xs"
                        value={b.then_jump}
                        disabled={readOnly}
                        onChange={(e) => patchBinding(i, { then_jump: Math.max(0, Math.round(Number(e.target.value)) || 0) })}
                      />
                      步（0=不跳）
                    </label>
                  </>
                )}
                <label className="flex items-center gap-1 text-[11px] muted" title="同轮多意图命中时小者先">
                  P
                  <input
                    type="number"
                    min={0}
                    max={1000}
                    className="input w-16 text-xs"
                    value={b.priority}
                    disabled={readOnly}
                    onChange={(e) => patchBinding(i, { priority: Math.round(Number(e.target.value)) || 0 })}
                  />
                </label>
                <label className="flex items-center gap-1 text-[11px] muted">
                  <input
                    type="checkbox"
                    className="size-3 accent-(--live)"
                    checked={b.once}
                    disabled={readOnly}
                    onChange={(e) => patchBinding(i, { once: e.target.checked })}
                  />
                  仅一次
                </label>
                <label className="flex items-center gap-1 text-[11px] muted">
                  <input
                    type="checkbox"
                    className="size-3 accent-(--live)"
                    checked={b.enabled}
                    disabled={readOnly}
                    onChange={(e) => patchBinding(i, { enabled: e.target.checked })}
                  />
                  启用
                </label>
                {!readOnly && (
                  <button
                    className="ml-auto text-xs text-red-600"
                    onClick={() => patch({ bindings: draft.bindings.filter((_, j) => j !== i) })}
                  >
                    删除
                  </button>
                )}
              </div>
            ))}
          </div>
        </div>

        <label className="flex items-center gap-1.5 text-[11px] muted">
          <input
            type="checkbox"
            className="size-3 accent-(--live)"
            checked={draft.enabled}
            disabled={readOnly}
            onChange={(e) => patch({ enabled: e.target.checked })}
          />
          意图启用
        </label>

        {props.err && <p className="text-sm text-red-600">{props.err}</p>}
        <div className="flex justify-end gap-2">
          <button className="btn-ghost text-xs" onClick={props.onClose}>取消</button>
          <button className="btn-primary text-xs" disabled={props.busy || readOnly} onClick={props.onSubmit}>
            {props.busy ? "保存中…" : "保存意图"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** 意图管理 tab：表格（名称/关键词/判据/动作/启用）+ 弹窗编辑。 */
export default function IntentManager(props: {
  tpl: TemplateRow;
  accountId: string;
  readOnly: boolean;
  /** 保存 graph_json 成功后回调（外层重拉模板行）。 */
  onSaved: () => void;
  /** 本语言基础意图种子（一键导入；不挂绑定=导入后零行为，业务再挂动作）。 */
  seedIntents?: SeedIntent[];
}) {
  const { tpl, readOnly } = props;
  const doc = useMemo(() => parseGraphDoc(tpl.graph_json ?? ""), [tpl.graph_json]);
  const stepCount = useMemo(
    () => Math.max(parseTemplateSteps(tpl.steps_json ?? "").length, 1),
    [tpl.steps_json],
  );
  const [draft, setDraft] = useState<IntentDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [qaRows, setQaRows] = useState<{ id?: string; question_text?: string; lang?: string }[]>([]);
  // 表格导入弹窗（整表替换 graph_json）。
  const [importOpen, setImportOpen] = useState(false);

  useEffect(() => {
    let alive = true;
    api.listQaAll(props.accountId)
      .then((rows) => {
        if (alive) setQaRows(Array.isArray(rows) ? rows : []);
      })
      .catch(() => {
        if (alive) setQaRows([]);
      });
    return () => {
      alive = false;
    };
  }, [props.accountId]);

  const tplLang = String(tpl.language ?? "zh");
  // 语言过滤（模板视角）：词条选择只列本模板语言的（其他语言去 /qa 全局页）。
  const langQaRows = useMemo(
    () => qaRows.filter((r) => String(r.lang ?? "zh") === tplLang),
    [qaRows, tplLang],
  );
  // play_qa 动作目标的合法性校验集（用全量词条——存在性与语言过滤是两件事）。
  const qaIdSet = useMemo(() => new Set(qaRows.map((r) => String(r.id ?? ""))), [qaRows]);

  const submit = useCallback(async () => {
    if (!draft) return;
    // 前置校验（CP validate_flow_graph 同口径；错误可见不静默改写）。
    const label = draft.label.trim();
    if (!label) return setErr("意图名称不能为空。");
    if (draft.keywords.length === 0) return setErr("至少一个关键词（判据只补关键词未中的模糊轮）。");
    if (draft.judge.length > JUDGE_PROMPT_MAX_CHARS) return setErr(`判据超长（>${JUDGE_PROMPT_MAX_CHARS} 字）。`);
    const steps = parseSteps(draft.stepsText);
    if (draft.stepsText.trim() !== "" && steps.length === 0) return setErr("生效范围格式应为步号（如 1,3）或留空。");
    for (const b of draft.bindings) {
      if (b.action === "play_qa" && !b.qa_id) return setErr("播快答动作必须选择 QA 词条。");
      if (b.action === "jump_step" && (b.step < 1 || b.step > stepCount))
        return setErr(`跳转步号必须在 1..${stepCount}。`);
      if (b.action === "play_qa" && (b.then_jump < 0 || b.then_jump > stepCount))
        return setErr(`播完跳转步号必须在 0..${stepCount}。`);
    }
    const intent: GraphIntent = {
      id: draft.id,
      label,
      keywords: draft.keywords,
      steps,
      enabled: draft.enabled,
      judge: intentJudgeField(draft.judge),
    };
    const others = doc.intents.filter((it) => it.id !== draft.id);
    const otherBindings = doc.bindings.filter((b) => b.intent !== draft.id);
    const rebuilt = draft.bindings.map((b) => bindingFromDraft(b, draft.id, stepCount));
    const next = JSON.stringify({ version: 1, intents: [...others, intent], bindings: [...otherBindings, ...rebuilt] });
    setBusy(true);
    setErr("");
    try {
      await api.updateTemplate(String(tpl.id ?? ""), { graph_json: next });
      setDraft(null);
      props.onSaved();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }, [draft, doc, stepCount, tpl.id, props.onSaved]);

  const removeIntent = useCallback(
    async (it: GraphIntent) => {
      if (!window.confirm(`确认删除意图「${it.label || it.id}」及其全部动作？`)) return;
      const next = JSON.stringify({
        version: 1,
        intents: doc.intents.filter((x) => x.id !== it.id),
        bindings: doc.bindings.filter((b) => b.intent !== it.id),
      });
      setBusy(true);
      setErr("");
      try {
        await api.updateTemplate(String(tpl.id ?? ""), { graph_json: next });
        props.onSaved();
      } catch (e) {
        setErr(String(e));
      } finally {
        setBusy(false);
      }
    },
    [doc, tpl.id, props.onSaved],
  );

  /** 一键导入种子意图：按名称去重（同名的跳过），导入即保存；不带绑定=零行为。 */
  const importSeeds = useCallback(async () => {
    const seeds = props.seedIntents ?? [];
    const existing = new Set(doc.intents.map((it) => it.label.trim()));
    const pending = seeds.filter((s) => !existing.has(s.label.trim()));
    if (pending.length === 0) {
      setErr("基础意图包已全部导入过（按名称去重）。");
      return;
    }
    if (!window.confirm(`导入 ${pending.length} 条基础意图？（只建意图不挂动作，命中后暂无行为，需在编辑里挂绑定）`)) return;
    const added: GraphIntent[] = pending.map((s) => ({
      id: genHexId("int_"),
      label: s.label,
      keywords: [...s.keywords],
      steps: [],
      enabled: true,
      judge: intentJudgeField(s.judge ?? ""),
    }));
    const next = JSON.stringify({ version: 1, intents: [...doc.intents, ...added], bindings: doc.bindings });
    setBusy(true);
    setErr("");
    try {
      await api.updateTemplate(String(tpl.id ?? ""), { graph_json: next });
      props.onSaved();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }, [props.seedIntents, doc, tpl.id, props.onSaved]);

  const actionText = (it: GraphIntent): string => {
    const binds = doc.bindings.filter((b) => b.intent === it.id);
    if (binds.length === 0) return "无动作";
    return binds
      .map((b) =>
        b.action === "jump_step"
          ? `跳第${Number(b.step ?? 1)}步`
          : b.action === "play_qa"
            ? `播快答${Number(b.then_jump ?? 0) > 0 ? `→第${Number(b.then_jump)}步` : ""}`
            : "通知人工",
      )
      .join("，");
  };

  // ---- 表格导入（2026-09-25）：整表替换 graph_json；预览期逐行校验 ----

  /** 单行解析：row-locally 校验 + 用 prev 做 文件内 ID/显示名 查重（预览期即标跳过）。 */
  const parseIntentsImportRow = useCallback(
    (row: string[], prev: ParsedRow<IntentImportRow>[]): ParsedRow<IntentImportRow> => {
      const rawId = String(row[0] ?? "").trim();
      const label = String(row[1] ?? "").trim();
      if (!label) return rowSkip("显示名为空");
      if (label.length > 64) return rowSkip(`显示名超 64 字（当前 ${label.length} 字）`);
      const seenLabels = new Set<string>();
      const seenIds = new Set<string>();
      for (const p of prev) {
        if (!p.ok || p.data === null) continue;
        seenLabels.add(p.data.label);
        if (p.data.providedId) seenIds.add(p.data.providedId);
        seenIds.add(p.data.intentId);
      }
      if (seenLabels.has(label)) return rowSkip(`显示名「${label}」与前面行重复`);
      const keywords = splitListCell(String(row[2] ?? ""));
      if (keywords.length === 0) return rowSkip("关键词为空");
      if (keywords.length > 32) return rowSkip(`关键词 ${keywords.length} 个，最多 32 个`);
      const overKw = keywords.find((k) => k.length > 64);
      if (overKw) return rowSkip(`关键词「${overKw.slice(0, 12)}…」超 64 字`);
      const judge = String(row[3] ?? "").trim();
      if (judge.length > JUDGE_PROMPT_MAX_CHARS) {
        return rowSkip(`判据超 ${JUDGE_PROMPT_MAX_CHARS} 字（当前 ${judge.length} 字）`);
      }
      const actionRaw = String(row[4] ?? "").trim().toLowerCase();
      let action: IntentImportRow["action"] = "";
      if (actionRaw === "play_qa" || actionRaw === "播快答") action = "play_qa";
      else if (actionRaw === "jump_step" || actionRaw === "跳步") action = "jump_step";
      else if (actionRaw === "notify_human" || actionRaw === "通知人工") action = "notify_human";
      else if (actionRaw !== "" && !["无", "none", "-"].includes(actionRaw)) {
        return rowSkip(`动作须为 play_qa/jump_step/notify_human/无（当前「${String(row[4]).trim()}」）`);
      }
      const target = String(row[5] ?? "").trim();
      let qaId = "";
      let step = 0;
      if (action === "play_qa") {
        if (!target) return rowSkip("动作=play_qa 但动作目标（词条 ID）为空");
        // 存在性只在校验集非空时把闸（词条表拉取失败时放行，运行时 play_miss 会优雅降级）。
        if (qaIdSet.size > 0 && !qaIdSet.has(target)) {
          return rowSkip(`词条 ID「${target.slice(0, 16)}」不在快答库（到问答库复制词条 ID）`);
        }
        qaId = target;
      } else if (action === "jump_step") {
        const n = Math.round(Number(target));
        if (!Number.isFinite(n) || n < 1 || n > stepCount) {
          return rowSkip(`动作目标须为 1..${stepCount} 的步号（当前「${target || "空"}」）`);
        }
        step = n;
      }
      let priority = 10;
      const prioRaw = String(row[6] ?? "").trim();
      if (prioRaw !== "") {
        const n = Math.round(Number(prioRaw));
        if (!Number.isFinite(n)) return rowSkip(`优先级「${prioRaw}」不是数字`);
        priority = Math.max(0, Math.min(n, 1000));
      }
      const once = parseBoolCell(String(row[7] ?? ""), false);
      if (once === null) return rowSkip(`仅一次「${String(row[7]).trim()}」须为 是/否`);
      // 意图 ID：合法 int_hex 原样用（支撑 导出→改→再导入 的幂等替换）；不合法/空=生成。
      let intentId = "";
      let providedId = "";
      if (rawId && INTENT_ID_RE.test(rawId)) {
        if (seenIds.has(rawId)) return rowSkip(`意图 ID「${rawId}」与前面行重复`);
        intentId = rawId;
        providedId = rawId;
      } else {
        providedId = rawId; // 填了但不合法：记录原值（提示面），落库用自动生成
        do {
          intentId = genHexId("int_");
        } while (seenIds.has(intentId));
      }
      return rowOk({
        intentId, providedId, label, keywords, judge, action, qaId, step, priority, once,
      });
    },
    [qaIdSet, stepCount],
  );

  /** 组装并保存：整表替换 intents+bindings（表格没写的意图会被删，确认文案明说）。 */
  async function importIntentRows(rows: IntentImportRow[]): Promise<ImportResult> {
    const intents: GraphIntent[] = rows.map((r) => ({
      id: r.intentId,
      label: r.label,
      keywords: r.keywords,
      steps: [],
      enabled: true,
      judge: r.judge ? { prompt: r.judge } : undefined,
    }));
    const bindings: GraphBinding[] = [];
    for (const r of rows) {
      if (!r.action) continue;
      bindings.push(
        bindingFromDraft(
          {
            id: genHexId("bnd_"),
            action: r.action,
            qa_id: r.qaId,
            step: r.step || 1,
            then_jump: 0,
            priority: r.priority,
            once: r.once,
            enabled: true,
          },
          r.intentId,
          stepCount,
        ),
      );
    }
    await api.updateTemplate(String(tpl.id ?? ""), {
      graph_json: JSON.stringify({ version: 1, intents, bindings }),
    });
    props.onSaved();
    return { done: rows.length, failed: 0, errors: [] };
  }

  return (
    <section className="card space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs muted">
          第一层识别：客户原话命中关键词（或判据）→ 执行动作。同轮多命中只执行优先级最小者。
        </p>
        <div className="flex items-center gap-2">
          {!readOnly && (
            <>
              <button className="btn-ghost text-xs" onClick={() => setImportOpen(true)}>
                导入意图
              </button>
              <button
                className="btn-ghost text-xs"
                title="下载示例 CSV（含列说明注释行，导入时自动忽略）"
                onClick={() =>
                  downloadCsv(INTENT_IMPORT_FILENAME, buildExampleCsvRows(INTENT_IMPORT_COLUMNS, INTENT_IMPORT_EXAMPLE))
                }
              >
                下载示例模板
              </button>
            </>
          )}
          {!readOnly && (props.seedIntents?.length ?? 0) > 0 && (
            <button className="btn-ghost text-xs" disabled={busy} onClick={() => void importSeeds()}>
              导入基础意图包
            </button>
          )}
          {!readOnly && (
            <button className="btn-ghost text-xs" disabled={busy} onClick={() => { setErr(""); setDraft(emptyDraft()); }}>
              ＋新建意图
            </button>
          )}
        </div>
      </div>
      {err && <ErrorState message={err} />}
      {doc.intents.length === 0 ? (
        <p className="text-sm muted">该模板还没有意图。新建意图后，画布左栏会出现对应条件边。</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-(--card-border) muted">
                <th className="py-1.5 pr-2 font-medium">名称</th>
                <th className="py-1.5 pr-2 font-medium">关键词</th>
                <th className="py-1.5 pr-2 font-medium">判据</th>
                <th className="py-1.5 pr-2 font-medium">命中动作</th>
                <th className="py-1.5 pr-2 font-medium">生效</th>
                <th className="py-1.5 pr-2 font-medium">状态</th>
                <th className="py-1.5 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {doc.intents.map((it) => {
                const kws = it.keywords ?? [];
                return (
                  <tr key={it.id} className="border-b border-(--card-border)/60 align-top">
                    <td className="py-2 pr-2 font-medium">{it.label || "(未命名)"}</td>
                    <td className="max-w-64 py-2 pr-2">
                      <span className="line-clamp-2 muted" title={kws.join("、")}>
                        {kws.slice(0, 6).join("、")}
                        {kws.length > 6 ? ` …（${kws.length}）` : ""}
                      </span>
                    </td>
                    <td className="py-2 pr-2">
                      {it.judge?.prompt ? (
                        <span className="rounded-sm bg-amber-100 px-1 text-[10px] text-amber-700" title={it.judge.prompt}>
                          有
                        </span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td className="max-w-52 py-2 pr-2 muted">{actionText(it)}</td>
                    <td className="py-2 pr-2 muted">{(it.steps ?? []).length === 0 ? "全程" : `第 ${(it.steps ?? []).join(",")} 步`}</td>
                    <td className="py-2 pr-2">
                      {it.enabled === false ? (
                        <span className="rounded-sm bg-muted px-1 text-[10px] muted">已停用</span>
                      ) : (
                        <span className="rounded-sm bg-emerald-100 px-1 text-[10px] text-emerald-700">启用</span>
                      )}
                    </td>
                    <td className="py-2">
                      <div className="flex gap-2">
                        <button
                          className="text-(--live)"
                          disabled={busy}
                          onClick={() => { setErr(""); setDraft(toDraft(it, doc.bindings)); }}
                        >
                          编辑
                        </button>
                        {!readOnly && (
                          <button className="text-red-600" disabled={busy} onClick={() => void removeIntent(it)}>
                            删除
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {draft && (
        <IntentModal
          draft={draft}
          stepCount={stepCount}
          qaRows={langQaRows}
          langLabel={LANG_LABEL[tplLang] ?? tplLang}
          readOnly={readOnly}
          onChange={setDraft}
          onClose={() => setDraft(null)}
          onSubmit={() => void submit()}
          busy={busy}
          err={err}
        />
      )}

      {/* 表格导入（2026-09-25）：CSV 上传→预览（N 行将导入/M 行跳过及原因）→确认（整表替换）→保存→摘要 */}
      {!readOnly && (
        <TableImport<IntentImportRow>
          open={importOpen}
          title="表格导入意图"
          description="整表替换：导入将替换该模板现有的全部意图与绑定，未包含在表格里的意图会被删除。"
          columns={INTENT_IMPORT_COLUMNS}
          exampleRows={INTENT_IMPORT_EXAMPLE}
          exampleFilename={INTENT_IMPORT_FILENAME}
          parseRow={parseIntentsImportRow}
          onImport={importIntentRows}
          confirmText={(n) =>
            `导入将【整表替换】该模板现有的意图与绑定：\n现有 ${doc.intents.length} 个意图 / ${doc.bindings.length} 条绑定 → 替换为表格的 ${n} 条意图。\n未包含在表格里的现有意图会被删除。确定继续？`
          }
          onClose={() => setImportOpen(false)}
        />
      )}
    </section>
  );
}
