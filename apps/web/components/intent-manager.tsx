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
import {
  bindingFromDraft, intentJudgeField, parseGraphDoc, parseTemplateSteps,
  JUDGE_PROMPT_MAX_CHARS, type GraphBinding, type GraphIntent,
} from "@/lib/qa-canvas";
import type { SeedIntent } from "@/lib/seed-pack";
import type { TemplateRow } from "@/components/template-editor";

/** 关键词展示/落库上限（引擎无硬限；PRD 口径 200——超限保存拦截而非静默截断）。 */
const KEYWORD_LIMIT = 200;

const ACTIONS: [GraphBinding["action"], string][] = [
  ["jump_step", "跳到指定步骤"],
  ["play_qa", "播快答（QA 词条）"],
  ["notify_human", "通知人工（打铃不暂停）"],
];

function rid(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 8)}`;
}

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
    id: rid("int"),
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
        id: b.id || rid("bnd"),
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
  qaRows: { id?: string; question_text?: string }[];
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
                      { id: rid("bnd"), action: "jump_step", qa_id: "", step: 1, then_jump: 0, priority: 10, once: true, enabled: true },
                    ],
                  })
                }
              >
                ＋ 加动作
              </button>
            )}
          </div>
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
  const [qaRows, setQaRows] = useState<{ id?: string; question_text?: string }[]>([]);

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
      id: rid("int"),
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

  return (
    <section className="card space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs muted">
          第一层识别：客户原话命中关键词（或判据）→ 执行动作。同轮多命中只执行优先级最小者。
        </p>
        <div className="flex items-center gap-2">
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
          qaRows={qaRows}
          readOnly={readOnly}
          onChange={setDraft}
          onClose={() => setDraft(null)}
          onSubmit={() => void submit()}
          busy={busy}
          err={err}
        />
      )}
    </section>
  );
}
