"use client";

// 意图（P2.5「意图表单化＋画布退役」，2026-09-21 收编进「主流程」表单）：
// 第一层识别——客户原话命中关键词（或判据）→ 执行动作。折叠卡（/calls 意图规则卡同款
// 交互：表格摘要 + 弹窗编辑）表内直编 graph_json，**画布不再承担意图编辑**。
// 写路径统一走 lib/intent-table（saveIntentDoc）：未改动＝原字节返回，改过的项才重建。
// 引擎契约（packages/core/bok_voice_core/flow_graph.py）：关键词确定性命中；judge=关键词
// 未中时背景大模型批量判定（prompt ≤400 字）；动作 play_qa（可带 then_jump）/jump_step/
// notify_human。**每个常规意图至少要有一条启用动作**（P2.2 孤儿门，无动作 CP 保存 400）；
// 兜底意图保留 id `"*"`（keywords 必空、无判据、其绑定 once 必须 false）＝常规意图与快答
// 都没接住时的最后出口，不配＝落 AI 自由应答。

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { ErrorState } from "@/components/app-shell";
import { parseGraphDoc, parseTemplateSteps } from "@/lib/qa-canvas";
import type { TemplateRow } from "@/components/template-editor";
import {
  CATCHALL_INTENT_ID, JUDGE_PROMPT_MAX_CHARS,
  genGraphId, isCatchallId, isIntentIdValid, isJudgeTooLong,
  parseKeywords, parseStepsText, rowsFromDoc, saveIntentDoc,
  type BindingRow, type GraphAction, type IntentRow,
} from "@/lib/intent-table";

/** 关键词展示/落库上限（引擎无硬限；PRD 口径 200——超限保存拦截而非静默截断）。 */
const KEYWORD_LIMIT = 200;

const ACTIONS: [GraphAction, string][] = [
  ["jump_step", "跳到指定步骤"],
  ["play_qa", "播快答（QA 词条）"],
  ["notify_human", "通知人工（打铃不暂停）"],
];

/** 动作摘要（表格行）：跳第N步 / 播快答[→第N步] / 通知人工。 */
function actionText(b: BindingRow): string {
  if (b.action === "jump_step") return `跳第${b.step}步`;
  if (b.action === "play_qa") return `播快答${b.then_jump > 0 ? `→第${b.then_jump}步` : ""}`;
  return "通知人工";
}

function bindingSummary(bindings: BindingRow[]): string {
  if (bindings.length === 0) return "无动作";
  return bindings.map(actionText).join("，");
}

/** 意图编辑弹窗草稿（加载自已存意图或新建空壳）。 */
type IntentDraft = {
  /** 编辑时的原始 id（新建=null）；重命名靠它定位基线行。 */
  originalId: string | null;
  id: string;
  label: string;
  keywordsText: string;
  stepsText: string;
  judge: string;
  enabled: boolean;
  bindings: BindingRow[];
  /** 兜底（"*"）档：只留动作面，关键词/作用步/判据/once 全收起。 */
  catchAll: boolean;
};

function emptyDraft(): IntentDraft {
  return {
    originalId: null,
    id: genGraphId("int_"),
    label: "",
    keywordsText: "",
    stepsText: "",
    judge: "",
    enabled: true,
    bindings: [],
    catchAll: false,
  };
}

/** 空兜底草稿（新建兜底动作）。 */
function emptyCatchall(): IntentDraft {
  return {
    originalId: null,
    id: CATCHALL_INTENT_ID,
    label: "兜底",
    keywordsText: "",
    stepsText: "",
    judge: "",
    enabled: true,
    bindings: [],
    catchAll: true,
  };
}

/** 已存意图行 → 草稿（含兜底档）。 */
function toDraft(row: IntentRow): IntentDraft {
  return {
    originalId: row.id,
    id: row.id,
    label: row.label,
    keywordsText: row.keywords.join("，"),
    stepsText: row.steps.join(","),
    judge: row.judge,
    enabled: row.enabled,
    bindings: row.bindings.map((b) => ({ ...b })),
    catchAll: isCatchallId(row.id),
  };
}

function newBinding(action: GraphAction, once: boolean): BindingRow {
  return { id: genGraphId("bnd_"), action, qa_id: "", step: 1, then_jump: 0, priority: 10, once, enabled: true };
}

/** 草稿 → 意图行（兜底档强制 keywords 空/无判据/无作用步/once=false）。 */
function draftToRow(draft: IntentDraft): IntentRow {
  const catchAll = draft.catchAll;
  const keywords = catchAll ? [] : parseKeywords(draft.keywordsText).slice(0, KEYWORD_LIMIT);
  const steps = catchAll ? [] : parseStepsText(draft.stepsText);
  return {
    id: catchAll ? CATCHALL_INTENT_ID : draft.id.trim(),
    label: (catchAll ? draft.label || "兜底" : draft.label).trim(),
    keywords,
    keywordsText: keywords.join("，"),
    steps,
    stepsText: steps.join(","),
    judge: catchAll ? "" : draft.judge.trim(),
    enabled: draft.enabled,
    bindings: draft.bindings.map((b) => (catchAll ? { ...b, once: false } : { ...b })),
  };
}

/** 草稿合入行集（按 originalId 原地替换，新建追加）——供 saveIntentDoc 序列化。 */
function withDraftRow(baseRows: IntentRow[], draft: IntentDraft): IntentRow[] {
  const row = draftToRow(draft);
  const next = baseRows.map((r) => ({ ...r }));
  const i = draft.originalId ? next.findIndex((r) => r.id === draft.originalId) : -1;
  if (i >= 0) next[i] = row;
  else next.push(row);
  return next;
}

/** 客户端前置校验（CP validate_flow_graph 同口径；错误可见不静默改写）。返回错误文案或 ""。 */
function validateDraft(draft: IntentDraft, allRows: IntentRow[], stepCount: number): string {
  const row = draftToRow(draft);
  if (draft.catchAll) {
    // 兜底：只校验动作参数（无关键词/判据/once）。不需要兜底就删掉这一行。
    if (row.bindings.length === 0) return "兜底至少要配一个动作（不需要兜底就删除这一行）。";
    for (const b of row.bindings) {
      if (b.action === "play_qa" && !b.qa_id) return "兜底动作「播快答」必须选择 QA 词条。";
      if (b.action === "jump_step" && (b.step < 1 || b.step > stepCount)) return `兜底动作跳转步号必须在 1..${stepCount}。`;
      if (b.action === "play_qa" && (b.then_jump < 0 || b.then_jump > stepCount)) return `兜底播完跳转步号必须在 0..${stepCount}。`;
    }
    return "";
  }
  if (!row.label) return "意图名称不能为空。";
  if (!isIntentIdValid(row.id)) return "标识 id 只能小写字母开头、只含小写字母/数字/下划线（如 whatsapp_contact）。";
  if (isCatchallId(row.id)) return `标识「${CATCHALL_INTENT_ID}」是兜底保留字，普通意图请换个 id。`;
  const others = allRows.filter((r) => !isCatchallId(r.id) && r.id !== draft.originalId);
  if (others.some((r) => r.id === row.id)) return `标识 id「${row.id}」已被其它意图使用。`;
  if (row.keywords.length === 0) return "至少填一个关键词（判据只补关键词未中的模糊轮）。";
  if (row.keywords.length > KEYWORD_LIMIT) return `关键词最多 ${KEYWORD_LIMIT} 个。`;
  if (isJudgeTooLong(row.judge)) return `判据超长（>${JUDGE_PROMPT_MAX_CHARS} 字）。`;
  if (draft.stepsText.trim() !== "" && row.steps.length === 0) return "生效范围格式应为步号（如 1,3）或留空。";
  if (row.bindings.filter((b) => b.enabled !== false).length === 0)
    return "每个意图至少要有一个启用动作（无动作的意图无法触发，保存会被拒绝）。";
  for (const b of row.bindings) {
    if (b.action === "play_qa" && !b.qa_id) return "动作「播快答」必须选择 QA 词条。";
    if (b.action === "jump_step" && (b.step < 1 || b.step > stepCount)) return `跳转步号必须在 1..${stepCount}。`;
    if (b.action === "play_qa" && (b.then_jump < 0 || b.then_jump > stepCount)) return `播完跳转步号必须在 0..${stepCount}。`;
  }
  return "";
}

const inputCls =
  "w-full rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--live)";

/** 动作行编辑器（弹窗内；兜底档隐藏 优先级/仅一次/启用 三开关）。 */
function BindingEditor(props: {
  row: BindingRow;
  stepCount: number;
  qaRows: { id?: string; question_text?: string }[];
  readOnly: boolean;
  catchAll: boolean;
  onChange: (patch: Partial<BindingRow>) => void;
  onRemove: () => void;
}) {
  const { row, readOnly, catchAll } = props;
  const qaOptions = props.qaRows.slice(0, 300);
  return (
    <div className="flex flex-wrap items-center gap-1.5 rounded-lg bg-muted/60 p-2">
      <select
        className="select text-xs"
        value={row.action}
        disabled={readOnly}
        onChange={(e) => props.onChange({ action: e.target.value as GraphAction })}
      >
        {ACTIONS.map(([v, l]) => (
          <option key={v} value={v}>{l}</option>
        ))}
      </select>
      {row.action === "jump_step" && (
        <label className="flex items-center gap-1 text-[11px] muted">
          跳到第
          <input
            type="number"
            min={1}
            max={Math.max(props.stepCount, 1)}
            className="input w-16 text-xs"
            value={row.step}
            disabled={readOnly}
            onChange={(e) => props.onChange({ step: Math.max(1, Math.round(Number(e.target.value)) || 1) })}
          />
          步
        </label>
      )}
      {row.action === "play_qa" && (
        <>
          <select
            className="select max-w-52 truncate text-xs"
            value={row.qa_id}
            disabled={readOnly}
            onChange={(e) => props.onChange({ qa_id: e.target.value })}
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
              value={row.then_jump}
              disabled={readOnly}
              onChange={(e) => props.onChange({ then_jump: Math.max(0, Math.round(Number(e.target.value)) || 0) })}
            />
            步（0=不跳）
          </label>
        </>
      )}
      {!catchAll && (
        <>
          <label className="flex items-center gap-1 text-[11px] muted" title="同轮多意图命中时小者先">
            P
            <input
              type="number"
              min={0}
              max={1000}
              className="input w-16 text-xs"
              value={row.priority}
              disabled={readOnly}
              onChange={(e) => props.onChange({ priority: Math.round(Number(e.target.value)) || 0 })}
            />
          </label>
          <label className="flex items-center gap-1 text-[11px] muted">
            <input
              type="checkbox"
              className="size-3 accent-(--live)"
              checked={row.once}
              disabled={readOnly}
              onChange={(e) => props.onChange({ once: e.target.checked })}
            />
            仅一次
          </label>
          <label className="flex items-center gap-1 text-[11px] muted">
            <input
              type="checkbox"
              className="size-3 accent-(--live)"
              checked={row.enabled}
              disabled={readOnly}
              onChange={(e) => props.onChange({ enabled: e.target.checked })}
            />
            启用
          </label>
        </>
      )}
      {!readOnly && (
        <button className="ml-auto text-xs text-red-600" onClick={props.onRemove}>删除</button>
      )}
    </div>
  );
}

/** 意图编辑弹窗（受控草稿；保存时逐项校验并重建绑定）。 */
function IntentModal(props: {
  draft: IntentDraft;
  stepCount: number;
  qaRows: { id?: string; question_text?: string }[];
  readOnly: boolean;
  onChange: (next: IntentDraft) => void;
  onClose: () => void;
  onSubmit: () => void;
  busy: boolean;
  err: string;
}) {
  const { draft, readOnly } = props;
  const catchAll = draft.catchAll;
  const patch = (p: Partial<IntentDraft>) => props.onChange({ ...draft, ...p });
  const patchBinding = (i: number, p: Partial<BindingRow>) =>
    patch({ bindings: draft.bindings.map((b, j) => (j === i ? { ...b, ...p } : b)) });

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-foreground/30 p-4">
      <div className="w-full max-w-2xl space-y-3 rounded-xl border border-(--card-border) bg-background p-4 shadow-lg">
        <div className="flex items-center justify-between">
          <span className="label">{catchAll ? "编辑兜底（*）" : draft.originalId ? "编辑意图" : "新建意图"}</span>
          <button className="btn-ghost px-2 py-0.5 text-xs" onClick={props.onClose}>关闭 ✕</button>
        </div>

        {catchAll ? (
          <p className="rounded-lg bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
            兜底：常规意图与快答都没接住时执行的动作。不配＝落 AI 自由应答。兜底不设关键词、不看判据、每通不限次数。
          </p>
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block">
                <span className="text-xs muted">标识 id（小写字母开头，仅小写字母/数字/下划线，如 whatsapp_contact）</span>
                <input
                  className={`mt-1 ${inputCls} font-mono`}
                  maxLength={64}
                  value={draft.id}
                  disabled={readOnly}
                  placeholder="如：session_refuse"
                  onChange={(e) => patch({ id: e.target.value })}
                />
              </label>
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
              <span className="text-xs muted">关键词 *（客户说到任一关键词即命中；逗号/顿号/换行分隔，批量粘贴自动拆分）</span>
              <textarea
                className={`mt-1 h-16 ${inputCls}`}
                disabled={readOnly}
                value={draft.keywordsText}
                placeholder="如：不用了, 唔使, 别打了"
                onChange={(e) => patch({ keywordsText: e.target.value })}
              />
              {draft.keywordsText.trim() !== "" && (
                <div className="mt-1.5 flex flex-wrap items-center gap-1">
                  {parseKeywords(draft.keywordsText).slice(0, 30).map((k) => (
                    <span key={k} className="rounded-full border border-(--card-border) px-1.5 py-0.5 text-[10px]">{k}</span>
                  ))}
                  <span className="ml-auto text-[10px] muted">
                    {parseKeywords(draft.keywordsText).length}/{KEYWORD_LIMIT}
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
          </>
        )}

        <div>
          <div className="flex items-center justify-between">
            <span className="text-xs muted">
              命中后的动作（{catchAll ? "可多个，兜底按优先级最小者执行" : "每个意图至少要有一个启用动作；同轮多命中只执行优先级最小的一个"}）
            </span>
            {!readOnly && (
              <button
                className="btn-ghost px-2 py-0.5 text-xs"
                onClick={() => patch({ bindings: [...draft.bindings, newBinding("jump_step", !catchAll)] })}
              >
                ＋ 加动作
              </button>
            )}
          </div>
          <div className="mt-1 space-y-2">
            {draft.bindings.length === 0 && (
              <p className="text-[11px] muted">
                {catchAll
                  ? "还没配兜底动作——点「＋ 加动作」至少配一个（不需要兜底就删掉这一行）。"
                  : "还没有动作——意图必须至少挂一个启用动作，否则保存会被拒绝。"}
              </p>
            )}
            {draft.bindings.map((b, i) => (
              <BindingEditor
                key={b.id}
                row={b}
                stepCount={props.stepCount}
                qaRows={props.qaRows}
                readOnly={readOnly}
                catchAll={catchAll}
                onChange={(p) => patchBinding(i, p)}
                onRemove={() => patch({ bindings: draft.bindings.filter((_, j) => j !== i) })}
              />
            ))}
          </div>
        </div>

        {!catchAll && (
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
        )}

        {props.err && <p className="text-sm text-red-600">{props.err}</p>}
        <div className="flex justify-end gap-2">
          <button className="btn-ghost text-xs" onClick={props.onClose}>取消</button>
          <button className="btn-primary text-xs" disabled={props.busy || readOnly} onClick={props.onSubmit}>
            {props.busy ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** 意图表格（折叠卡）：正常意图行 + 表底固定「兜底（*）」行；弹窗编辑。 */
export default function IntentManager(props: {
  tpl: TemplateRow;
  accountId: string;
  readOnly: boolean;
  /** 保存 graph_json 成功后回调（外层重拉模板行）。 */
  onSaved: () => void;
}) {
  const { tpl, readOnly } = props;
  const [open, setOpen] = useState(true);
  const [draft, setDraft] = useState<IntentDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [qaRows, setQaRows] = useState<{ id?: string; question_text?: string }[]>([]);

  const graphRaw = tpl.graph_json ?? "";
  const rows = useMemo(() => rowsFromDoc(parseGraphDoc(graphRaw)), [graphRaw]);
  const normalRows = useMemo(() => rows.filter((r) => !isCatchallId(r.id)), [rows]);
  const catchall = useMemo(() => rows.find((r) => isCatchallId(r.id)), [rows]);
  const stepCount = useMemo(
    () => Math.max(parseTemplateSteps(tpl.steps_json ?? "").length, 1),
    [tpl.steps_json],
  );

  useEffect(() => {
    let alive = true;
    api.listQaAll(props.accountId)
      .then((raw) => {
        if (alive) setQaRows(Array.isArray(raw) ? raw : []);
      })
      .catch(() => {
        if (alive) setQaRows([]);
      });
    return () => {
      alive = false;
    };
  }, [props.accountId]);

  const submit = useCallback(async () => {
    if (!draft) return;
    const problem = validateDraft(draft, rows, stepCount);
    if (problem) return setErr(problem);
    const next = saveIntentDoc(graphRaw, withDraftRow(rows, draft));
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
  }, [draft, rows, stepCount, graphRaw, tpl.id, props.onSaved]);

  const removeIntent = useCallback(
    async (row: IntentRow) => {
      if (!window.confirm(`确认删除意图「${row.label || row.id}」及其全部动作？`)) return;
      const next = saveIntentDoc(graphRaw, rows.filter((r) => r.id !== row.id));
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
    [rows, graphRaw, tpl.id, props.onSaved],
  );

  return (
    <details
      className="card"
      open={open}
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="cursor-pointer text-sm font-medium">
        意图
        <span className="ml-1 text-xs muted">（客户命中关键词/判据后就做什么——第一层识别）</span>
      </summary>
      <div className="mt-3 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs muted">
            客户原话命中关键词（或判据）→ 执行动作。同轮多命中只执行优先级最小者；每个意图至少要有一个启用动作。
          </p>
          {!readOnly && (
            <button className="btn-ghost text-xs" disabled={busy} onClick={() => { setErr(""); setDraft(emptyDraft()); }}>
              ＋新建意图
            </button>
          )}
        </div>
        {err && <ErrorState message={err} />}
        {normalRows.length === 0 ? (
          <p className="text-sm muted">
            该模板还没有意图——从学习报告（场景学习 tab）挖掘的意图可在此登记，登记后记得挂动作。
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-(--card-border) muted">
                  <th className="py-1.5 pr-2 font-medium">标识</th>
                  <th className="py-1.5 pr-2 font-medium">名称</th>
                  <th className="py-1.5 pr-2 font-medium">关键词</th>
                  <th className="py-1.5 pr-2 font-medium">判据</th>
                  <th className="py-1.5 pr-2 font-medium">命中动作</th>
                  <th className="py-1.5 pr-2 font-medium">作用步</th>
                  <th className="py-1.5 pr-2 font-medium">状态</th>
                  <th className="py-1.5 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {normalRows.map((it) => (
                  <tr key={it.id} className="border-b border-(--card-border)/60 align-top">
                    <td className="py-2 pr-2 font-mono text-[11px] muted">{it.id}</td>
                    <td className="py-2 pr-2 font-medium">{it.label || "(未命名)"}</td>
                    <td className="max-w-56 py-2 pr-2">
                      <span className="line-clamp-2 muted" title={it.keywords.join("、")}>
                        {it.keywords.slice(0, 6).join("、")}
                        {it.keywords.length > 6 ? ` …（${it.keywords.length}）` : ""}
                      </span>
                    </td>
                    <td className="py-2 pr-2">
                      {it.judge ? (
                        <span className="rounded-sm bg-amber-100 px-1 text-[10px] text-amber-700" title={it.judge}>有</span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td className="max-w-52 py-2 pr-2 muted">{bindingSummary(it.bindings)}</td>
                    <td className="py-2 pr-2 muted">{it.steps.length === 0 ? "全程" : `第 ${it.steps.join(",")} 步`}</td>
                    <td className="py-2 pr-2">
                      {it.enabled === false ? (
                        <span className="rounded-sm bg-muted px-1 text-[10px] muted">已停用</span>
                      ) : (
                        <span className="rounded-sm bg-emerald-100 px-1 text-[10px] text-emerald-700">启用</span>
                      )}
                    </td>
                    <td className="py-2">
                      <div className="flex gap-2">
                        <button className="text-(--live)" disabled={busy} onClick={() => { setErr(""); setDraft(toDraft(it)); }}>编辑</button>
                        {!readOnly && (
                          <button className="text-red-600" disabled={busy} onClick={() => void removeIntent(it)}>删除</button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
                {/* 表底固定兜底行：只配动作+参数（无关键词/once/判据） */}
                <tr className="bg-muted/40 align-top">
                  <td className="py-2 pr-2 font-mono text-[11px] text-amber-700">{CATCHALL_INTENT_ID}</td>
                  <td className="py-2 pr-2 font-medium">{catchall ? catchall.label || "兜底" : "兜底"}</td>
                  <td className="py-2 pr-2 muted" colSpan={2}>—</td>
                  <td className="max-w-52 py-2 pr-2 muted">{catchall ? bindingSummary(catchall.bindings) : "未配置"}</td>
                  <td className="py-2 pr-2 muted">全程</td>
                  <td className="py-2 pr-2">
                    {catchall ? (
                      <span className="rounded-sm bg-amber-100 px-1 text-[10px] text-amber-700">兜底</span>
                    ) : (
                      <span className="rounded-sm bg-muted px-1 text-[10px] muted">未配置</span>
                    )}
                  </td>
                  <td className="py-2">
                    <div className="flex gap-2">
                      <button
                        className="text-(--live)"
                        disabled={busy}
                        onClick={() => { setErr(""); setDraft(catchall ? toDraft(catchall) : emptyCatchall()); }}
                      >
                        {catchall ? "编辑" : "配置动作"}
                      </button>
                      {catchall && !readOnly && (
                        <button className="text-red-600" disabled={busy} onClick={() => void removeIntent(catchall)}>删除</button>
                      )}
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        )}
        <p className="text-[11px] leading-relaxed muted">
          兜底（*）：常规意图与快答都没接住时的兜底动作；不配＝落 AI 自由应答。
        </p>
      </div>

      {draft && (
        <IntentModal
          draft={draft}
          stepCount={stepCount}
          qaRows={qaRows}
          readOnly={readOnly}
          onChange={setDraft}
          onClose={() => setDraft(null)}
          onSubmit={() => void submit()}
          busy={busy}
          err={err}
        />
      )}
    </details>
  );
}
