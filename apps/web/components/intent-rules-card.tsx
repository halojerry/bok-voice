"use client";

// 客户意向规则（W4-T3 抽组件；2026-09-20 第二批升级为表格+弹窗，PRD 3.2）：
// 确定性条件 → 意向码/标签/处置，挂断时评估覆盖 disposition。
// 消费点：/calls 页顶部 + AI 工作站「客户意向」tab。
// 条件面=引擎 12 键通话事实（core intent_rules.INTENT_FACTS；时长/轮数/步数/捕获等）。

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ErrorState } from "@/components/app-shell";
import { useSession } from "@/components/session-context";

type IntentRuleRow = {
  id: string;
  account_id?: string;
  name?: string;
  intent_code?: string;
  label?: string;
  disposition?: string;
  priority?: number;
  enabled?: boolean;
  conditions?: IntentCond[];
};

type IntentCond = { fact?: string; op?: string; value?: unknown };

/** fact 目录（12 键，中文标签与 CP core `intent_rules.INTENT_FACTS` 手工同步）。 */
const INTENT_FACTS: [string, string][] = [
  ["duration_s", "通话时长秒"],
  ["nudge_fired", "心跳次数"],
  ["watchdog_fired", "看门狗次数"],
  ["storm_rounds", "风暴轮数"],
  ["repeat_count", "复述次数"],
  ["refuse_count", "拒绝次数"],
  ["objection_count", "异议次数"],
  ["confirm_count", "确认次数"],
  ["question_count", "提问次数"],
  ["step_max", "到达最大步"],
  ["wa_captured", "已捕获号码"],
  ["graph_notifies", "人工求助次数"],
];
const INTENT_FACT_LABEL: Record<string, string> = Object.fromEntries(INTENT_FACTS);
const INTENT_OPS: [string, string][] = [["gte", "≥"], ["lte", "≤"], ["eq", "="]];
const INTENT_OP_SYM: Record<string, string> = Object.fromEntries(INTENT_OPS);

/** 表单的条件行草稿：value 存输入原文（数字串或 "true"/"false"），提交时定型。 */
type IntentCondDraft = { fact: string; op: string; value: string };

const EMPTY_RULE_FORM = {
  name: "",
  intent_code: "",
  label: "",
  disposition: "",
  priority: "10",
  isGlobal: false,
  conditions: [] as IntentCondDraft[],
};

/** CP `validate_conditions` 拒字符串 value（只收数字/布尔）——提交侧唯一定型点。 */
function condDraftToPayload(c: IntentCondDraft): { fact: string; op: string; value: number | boolean } {
  if (c.fact === "wa_captured") return { fact: c.fact, op: c.op, value: c.value === "true" };
  const n = Number(c.value);
  return { fact: c.fact, op: c.op, value: Number.isFinite(n) ? n : 0 };
}

function condText(c: IntentCond): string {
  const fact = String(c.fact ?? "");
  const label = INTENT_FACT_LABEL[fact] ?? fact;
  const op = INTENT_OP_SYM[String(c.op ?? "")] ?? String(c.op ?? "?");
  if (fact === "wa_captured") return `${label}${op}${c.value === true || c.value === "true" ? "是" : "否"}`;
  return `${label}${op}${String(c.value ?? "")}`;
}

/** 行 → 表单草稿（编辑既有规则）。 */
function rowToForm(r: IntentRuleRow): typeof EMPTY_RULE_FORM {
  const conds = Array.isArray(r.conditions) ? r.conditions : [];
  return {
    name: String(r.name ?? ""),
    intent_code: String(r.intent_code ?? ""),
    label: String(r.label ?? ""),
    disposition: String(r.disposition ?? ""),
    priority: String(Number(r.priority ?? 10)),
    isGlobal: String(r.account_id ?? "") === "",
    conditions: conds.map((c) => ({
      fact: String(c.fact ?? INTENT_FACTS[0][0]),
      op: String(c.op ?? "gte"),
      value: String(c.value ?? ""),
    })),
  };
}

const inputCls =
  "w-full rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--live)";

/** 规则编辑弹窗（新建/编辑共用；条件行编辑器=事实下拉/比较符/值）。 */
function RuleModal(props: {
  form: typeof EMPTY_RULE_FORM;
  isManager: boolean;
  readOnly: boolean;
  onChange: (next: typeof EMPTY_RULE_FORM) => void;
  onClose: () => void;
  onSubmit: () => void;
  busy: boolean;
  err: string;
}) {
  const { form } = props;
  const patch = (p: Partial<typeof EMPTY_RULE_FORM>) => props.onChange({ ...form, ...p });
  const setCond = (i: number, p: Partial<IntentCondDraft>) =>
    patch({ conditions: form.conditions.map((c, k) => (k === i ? { ...c, ...p } : c)) });
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-foreground/30 p-4">
      <div className="w-full max-w-xl space-y-3 rounded-xl border border-(--card-border) bg-background p-4 shadow-lg">
        <div className="flex items-center justify-between">
          <span className="label">{form.name ? "编辑意向规则" : "新建意向规则"}</span>
          <button className="btn-ghost px-2 py-0.5 text-xs" onClick={props.onClose}>关闭 ✕</button>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block">
            <span className="text-xs muted">规则名称 *</span>
            <input
              className={`mt-1 ${inputCls}`}
              maxLength={64}
              value={form.name}
              placeholder="如：高意向客户"
              onChange={(e) => patch({ name: e.target.value })}
            />
          </label>
          <label className="block">
            <span className="text-xs muted">将客户意向设置为（意向码）*</span>
            <input
              className={`mt-1 ${inputCls}`}
              maxLength={32}
              value={form.intent_code}
              placeholder="如：HIGH_INTENT"
              onChange={(e) => patch({ intent_code: e.target.value })}
            />
          </label>
          <label className="block">
            <span className="text-xs muted">设置客户标签（可选）</span>
            <input
              className={`mt-1 ${inputCls}`}
              maxLength={64}
              value={form.label}
              placeholder="如：愿意办理"
              onChange={(e) => patch({ label: e.target.value })}
            />
          </label>
          <label className="block">
            <span className="text-xs muted">处置 disposition（可选，留空=只打意向码）</span>
            <input
              className={`mt-1 ${inputCls}`}
              maxLength={32}
              value={form.disposition}
              placeholder="如：intended"
              onChange={(e) => patch({ disposition: e.target.value })}
            />
          </label>
        </div>

        <div className="flex flex-wrap items-end gap-3">
          <label className="block">
            <span className="text-xs muted">优先级（数字越小越先命中）</span>
            <input
              type="number"
              min={0}
              max={1000}
              className={`mt-1 w-24 ${inputCls}`}
              value={form.priority}
              onChange={(e) => patch({ priority: e.target.value })}
            />
          </label>
          {props.isManager && (
            <label className="flex items-center gap-1.5 pb-2 text-xs muted">
              <input
                type="checkbox"
                className="size-3 accent-(--live)"
                checked={form.isGlobal}
                onChange={(e) => patch({ isGlobal: e.target.checked })}
              />
              全局（管理员，对所有账号生效）
            </label>
          )}
        </div>

        <div>
          <div className="flex items-center justify-between">
            <span className="text-xs muted">命中条件（全部成立才判中）</span>
            <button
              className="btn-ghost px-2 py-0.5 text-xs"
              onClick={() =>
                patch({ conditions: [...form.conditions, { fact: INTENT_FACTS[0][0], op: "gte", value: "1" }] })
              }
            >
              ＋新增条件
            </button>
          </div>
          <div className="mt-1 space-y-2">
            {form.conditions.length === 0 && (
              <p className="text-[11px] muted">还没有条件：空条件集不参与判中，请至少添加一条。</p>
            )}
            {form.conditions.map((c, i) => (
              <div key={i} className="flex flex-wrap items-center gap-2">
                <span className="text-[10px] muted">{i + 1}</span>
                <select className="select text-xs" value={c.fact} onChange={(e) => setCond(i, { fact: e.target.value })}>
                  {INTENT_FACTS.map(([k, zh]) => (
                    <option key={k} value={k}>{zh}</option>
                  ))}
                </select>
                <select className="select text-xs" value={c.op} onChange={(e) => setCond(i, { op: e.target.value })}>
                  {INTENT_OPS.map(([k, sym]) => (
                    <option key={k} value={k}>{sym}</option>
                  ))}
                </select>
                {c.fact === "wa_captured" ? (
                  <select className="select text-xs" value={c.value} onChange={(e) => setCond(i, { value: e.target.value })}>
                    <option value="true">是</option>
                    <option value="false">否</option>
                  </select>
                ) : (
                  <input
                    type="number"
                    className="input w-24 text-xs"
                    value={c.value}
                    onChange={(e) => setCond(i, { value: e.target.value })}
                  />
                )}
                <button className="btn-ghost text-xs text-red-600" onClick={() => patch({ conditions: form.conditions.filter((_, k) => k !== i) })}>
                  删除
                </button>
              </div>
            ))}
          </div>
        </div>

        {props.err && <p className="text-sm text-red-600">{props.err}</p>}
        <div className="flex justify-end gap-2">
          <button className="btn-ghost text-xs" onClick={props.onClose}>取消</button>
          <button className="btn-primary text-xs" disabled={props.busy} onClick={props.onSubmit}>
            {props.busy ? "保存中…" : "保存规则"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** 客户意向折叠卡（表格+弹窗）：列表（条件摘要/意向码/标签/处置/优先级/启用）+ 编辑。 */
export default function IntentRulesCard({ accountId }: { accountId: string }) {
  const session = useSession();
  // 「全局」作用域仅主管（admin/root）可见。
  const isManager = session?.role === "admin" || session?.role === "root";
  const [rules, setRules] = useState<IntentRuleRow[] | null>(null);
  const [rulesErr, setRulesErr] = useState("");
  const [busyId, setBusyId] = useState("");
  const [modal, setModal] = useState<{ form: typeof EMPTY_RULE_FORM; editingId: string | null } | null>(null);
  const [formErr, setFormErr] = useState("");
  const [saving, setSaving] = useState(false);

  const loadRules = useCallback(async () => {
    try {
      const data = await api.listIntentRules(accountId);
      setRules((Array.isArray(data) ? data : []) as IntentRuleRow[]);
      setRulesErr("");
    } catch (e) {
      setRulesErr(String(e));
    }
  }, [accountId]);

  useEffect(() => {
    void loadRules();
  }, [loadRules]);

  async function toggleEnabled(r: IntentRuleRow) {
    const id = String(r.id);
    setBusyId(id);
    try {
      await api.updateIntentRule(id, { enabled: r.enabled === false });
      await loadRules();
    } catch (e) {
      setRulesErr(String(e));
    } finally {
      setBusyId("");
    }
  }

  async function removeRule(r: IntentRuleRow) {
    const id = String(r.id);
    if (!window.confirm(`确认删除意向规则「${String(r.name ?? id)}」？`)) return;
    setBusyId(id);
    try {
      await api.deleteIntentRule(id);
      await loadRules();
    } catch (e) {
      setRulesErr(String(e));
    } finally {
      setBusyId("");
    }
  }

  async function submitRule() {
    if (!modal) return;
    const name = modal.form.name.trim();
    const code = modal.form.intent_code.trim();
    if (!name) return setFormErr("规则名称不能为空。");
    if (!code) return setFormErr("意向码不能为空。");
    const body: Record<string, unknown> = {
      name,
      intent_code: code,
      label: modal.form.label.trim(),
      disposition: modal.form.disposition.trim(),
      priority: Number(modal.form.priority) || 10,
      conditions: modal.form.conditions.map(condDraftToPayload),
    };
    if (modal.form.isGlobal) body.account_id = ""; // 全局行；缺省=CP 盖章本账号
    setSaving(true);
    try {
      if (modal.editingId) await api.updateIntentRule(modal.editingId, body);
      else await api.createIntentRule(body);
      setModal(null);
      setFormErr("");
      await loadRules();
    } catch (e) {
      setFormErr(String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <details className="card">
      <summary className="cursor-pointer text-sm font-medium">
        意向规则 <span className="ml-1 text-xs muted">（挂断时按通话事实判意向码/标签/处置）</span>
      </summary>
      <div className="mt-3 space-y-3">
        <div className="flex items-center justify-between">
          <p className="text-xs muted">
            命中条件全部成立才判中；按优先级从小到大取第一条启用中的规则。全局规则对所有账号生效。
          </p>
          <button
            className="btn-ghost text-xs"
            onClick={() => {
              setFormErr("");
              setModal({ form: { ...EMPTY_RULE_FORM }, editingId: null });
            }}
          >
            ＋新建规则
          </button>
        </div>

        {rulesErr && <ErrorState message={rulesErr} />}

        {/* 规则表格 */}
        {rules === null && !rulesErr && <p className="text-sm muted">加载中…</p>}
        {rules !== null && rules.length === 0 && !rulesErr && (
          <p className="text-sm muted">暂无规则。点「＋新建规则」按通话事实（时长/轮数/捕获等）配置意向判定。</p>
        )}
        {rules !== null && rules.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-(--card-border) muted">
                  <th className="py-1.5 pr-2 font-medium">名称</th>
                  <th className="py-1.5 pr-2 font-medium">命中条件</th>
                  <th className="py-1.5 pr-2 font-medium">意向码</th>
                  <th className="py-1.5 pr-2 font-medium">标签</th>
                  <th className="py-1.5 pr-2 font-medium">处置</th>
                  <th className="py-1.5 pr-2 font-medium">优先级</th>
                  <th className="py-1.5 pr-2 font-medium">启用</th>
                  <th className="py-1.5 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {[...rules]
                  .sort((a, b) => Number(a.priority ?? 10) - Number(b.priority ?? 10))
                  .map((r) => {
                    const id = String(r.id);
                    const global = String(r.account_id ?? "") === "";
                    const enabled = r.enabled !== false;
                    const conds = Array.isArray(r.conditions) ? r.conditions : [];
                    return (
                      <tr key={id} className="border-b border-(--card-border)/60 align-top">
                        <td className="py-2 pr-2 font-medium">
                          {String(r.name ?? "(未命名)")}
                          {global ? (
                            <span className="ml-1 rounded-sm bg-muted px-1 text-[10px] muted">全局</span>
                          ) : (
                            <span className="ml-1 rounded-sm bg-blue-100 px-1 text-[10px] text-blue-700">本账号</span>
                          )}
                        </td>
                        <td className="max-w-64 py-2 pr-2">
                          <span className="line-clamp-2 muted">
                            {conds.length > 0 ? conds.map(condText).join("，") : "—"}
                          </span>
                        </td>
                        <td className="py-2 pr-2">
                          <span className="rounded-sm bg-(--live-soft) px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-(--live-ink)">
                            {String(r.intent_code ?? "")}
                          </span>
                        </td>
                        <td className="py-2 pr-2 muted">{String(r.label ?? "") || "-"}</td>
                        <td className="py-2 pr-2 muted">{String(r.disposition ?? "") || "-"}</td>
                        <td className="py-2 pr-2 muted">P{Number(r.priority ?? 10)}</td>
                        <td className="py-2 pr-2">
                          <label className="flex items-center gap-1 text-[11px] muted" title="启用/停用该规则">
                            <input
                              type="checkbox"
                              className="accent-(--live)"
                              checked={enabled}
                              disabled={busyId === id}
                              onChange={() => void toggleEnabled(r)}
                            />
                          </label>
                        </td>
                        <td className="py-2">
                          <div className="flex gap-2">
                            <button
                              className="text-(--live)"
                              onClick={() => {
                                setFormErr("");
                                setModal({ form: rowToForm(r), editingId: id });
                              }}
                            >
                              编辑
                            </button>
                            <button
                              className="text-red-600"
                              disabled={busyId === id}
                              onClick={() => void removeRule(r)}
                            >
                              删除
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {modal && (
        <RuleModal
          form={modal.form}
          isManager={Boolean(isManager)}
          readOnly={false}
          onChange={(form) => setModal({ ...modal, form })}
          onClose={() => setModal(null)}
          onSubmit={() => void submitRule()}
          busy={saving}
          err={formErr}
        />
      )}
    </details>
  );
}
