"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { useAccount } from "@/components/account-context";

const LANGS = [
  ["zh", "普通话"],
  ["yue", "粤语"],
  ["en", "English"],
] as const;

/** 模板四段字段的中文标签（数据库里以 opening/core/objection/closing 存储）。 */
const FIELD_LABELS = {
  opening: "开场白",
  core: "核心话术",
  objection: "异议应对",
  closing: "收尾话术",
} as const;

const FIELD_PLACEHOLDERS = {
  opening: "如：您好，我是…，今天联系您是想…",
  core: "产品卖点 / 需要传达的核心信息…",
  objection: "客户可能的顾虑与应对话术…",
  closing: "如：好的，那就不打扰您了，再见。",
} as const;

const TEMPLATE_FIELDS = ["opening", "core", "objection", "closing"] as const;

/** 分步话术:每一步 = 要达成的目标(goal) + 参考说法(ref,可含 {变量})。 */
interface FlowStep {
  goal: string;
  ref: string;
}

const EMPTY = {
  name: "",
  opening: "",
  core: "",
  objection: "",
  closing: "",
  tone_override: "",
  language: "zh",
};

const STEPS_HINT = "可用变量:{姓名} {快递单号} {快递尾号} {物流公司} {收货地址}。\n参考说法是给 AI 的要点参考,不是逐字稿——AI 会结合客户原话用自己的话讲。";

/** 把 steps 序列化/反序列化为 steps_json(存库)。 */
function stepsToJson(steps: FlowStep[]): string {
  return JSON.stringify(steps.filter((s) => s.goal.trim() || s.ref.trim()));
}
function jsonToSteps(raw: unknown): FlowStep[] {
  try {
    const arr = JSON.parse(String(raw ?? "") || "[]");
    if (!Array.isArray(arr)) return [];
    return arr
      .filter((s) => s && typeof s === "object")
      .map((s) => ({ goal: String((s as { goal?: unknown }).goal ?? ""), ref: String((s as { ref?: unknown }).ref ?? "") }));
  } catch {
    return [];
  }
}

/** 旧式四段 → 步骤(与 agent flow.template_to_steps 同款 goal 标签)。 */
const LEGACY_STEP_GOALS: Record<string, string> = {
  opening: "开场:自报家门,说明来意,向客户确认身份/包裹",
  core: "核心:向客户说明处理方案/关键信息,争取客户认可",
  objection: "异议:针对客户疑虑/拒绝,解释并稳住客户",
  closing: "收尾:确认客户意愿,礼貌收尾,不强推",
};

function fourSectionsToSteps(form: { opening: string; core: string; objection: string; closing: string }): FlowStep[] {
  const out: FlowStep[] = [];
  (["opening", "core", "objection", "closing"] as const).forEach((k) => {
    const txt = String(form[k] ?? "").trim();
    if (txt) out.push({ goal: LEGACY_STEP_GOALS[k], ref: txt });
  });
  return out;
}

/** 分步格式的成品示例(理赔场景,示范 goal/ref/分支/{变量} 的写法)。 */
const STEPS_EXAMPLES: Record<string, FlowStep[]> = {
  yue: [
    { goal: "确认包裹是不是{姓名}本人的", ref: "你好，请问係咪{姓名}？我哋係{物流公司}，有个包裹单号尾号{快递尾号}运输途中唔见咗，想同你核对下。\n如果客户唔记得 → 提佢下单时填嘅地址/电话帮佢回忆" },
    { goal: "说明一赔二方案,稳住客户", ref: "係我哋责任,我哋有买运费保险,会以一赔二赔俾你,唔使自己蚀钱。\n如果客户问点解要赔 → 讲係运输途中遗失,顺丰全责\n如果客户担心唔到账 → 讲赔付会直接落微信零钱/钱包\n如果客户话要重新买 → 讲可以用赔付抵扣,唔使自己再贴钱" },
    { goal: "引导客户加微信线上专员办理", ref: "理赔係通过微信消费者保护线上专员办理,我发微信号俾你,你加咗之后按佢步骤操作就得。\n如果客户话唔方便加微信 → 问佢方便嘅时间/方式,约好再跟进" },
    { goal: "收尾确认,礼貌结束", ref: "好,唔该晒你今日嘅时间。我哋会跟进,有咩问题随时搵我。祝你一切顺利,拜拜!" },
  ],
  zh: [
    { goal: "确认包裹是不是{姓名}本人的", ref: "您好，请问是{姓名}吗？我们是{物流公司}，有个包裹单号尾号{快递尾号}运输途中丢失了，想跟您核对一下。\n如果客户不记得 → 提他下单时填的地址/电话帮他回忆" },
    { goal: "说明一赔二方案,稳住客户", ref: "这是我们的责任，我们有购买运费保险，会以一赔二赔付给您，不用自己贴钱。\n如果客户问为什么赔 → 说明是运输途中遗失，我方全责\n如果客户担心不到账 → 说明赔付会直接到微信零钱/钱包\n如果客户说要重新买 → 说明可以用赔付抵扣，不用自己再贴钱" },
    { goal: "引导客户加微信线上专员办理", ref: "理赔是通过微信消费者保护线上专员办理，我把微信号发给您，您添加后按步骤操作就行。\n如果客户说不方便加微信 → 问他方便的时间/方式，约好再跟进" },
    { goal: "收尾确认,礼貌结束", ref: "好的，感谢您今天的时间。我们会跟进，有问题随时找我。祝您一切顺利，再见！" },
  ],
  en: [
    { goal: "Confirm the parcel belongs to {name}", ref: "Hello, is this {name}? We're {courier}. A parcel (tracking ending {tracking_tail}) was lost in transit and I'd like to verify with you." },
    { goal: "Explain 1-for-2 compensation and reassure", ref: "It's our responsibility. We have shipping insurance, so we'll compensate 2x. You won't lose money.\nIf they ask why → it was lost in transit, we take full responsibility\nIf they worry about payment → it goes straight to their WeChat wallet" },
    { goal: "Guide them to add the WeChat specialist", ref: "The claim is handled by our WeChat consumer-protection specialist. I'll send the ID — add it and follow the steps." },
    { goal: "Confirm and close politely", ref: "Thank you for your time. We'll follow up — reach out anytime. Goodbye!" },
  ],
};

function fillExample(form: typeof EMPTY, setForm: (f: typeof EMPTY) => void, lang: string, setSteps: (s: FlowStep[]) => void) {
  // 分步为主:示例直接填成分步(含分支写法示范),四段清空(由步骤统一承载)。
  const steps = STEPS_EXAMPLES[lang];
  if (!steps) return;
  const nameByLang = { yue: "理赔·分步（粤语示例）", zh: "理赔·分步（普通话示例）", en: "Claims · Step-by-step (English)" };
  setForm({ ...form, name: nameByLang[lang as keyof typeof nameByLang] ?? "", opening: "", core: "", objection: "", closing: "", language: lang });
  setSteps(steps);
}

export default function TemplatesPage() {
  const { accountId } = useAccount();
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState(EMPTY);
  const [steps, setSteps] = useState<FlowStep[]>([]);
  const [showLegacy, setShowLegacy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);
  const [loading, setLoading] = useState(true);

  async function refresh() {
    setLoading(true);
    try {
      const data = await api.listTemplates(accountId);
      setRows(Array.isArray(data) ? data : []);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
  }, [accountId]);

  function edit(row: Record<string, unknown>) {
    setEditingId(String(row.id ?? ""));
    const f = {
      name: String(row.name ?? ""),
      opening: String(row.opening ?? ""),
      core: String(row.core ?? ""),
      objection: String(row.objection ?? ""),
      closing: String(row.closing ?? ""),
      tone_override: String(row.tone_override ?? ""),
      language: String(row.language ?? "zh"),
    };
    setForm(f);
    // 分步为主:旧模板(只有四段无 steps)载入时自动转成步骤,让用户按步骤编辑。
    const saved = jsonToSteps(row.steps_json);
    setSteps(saved.length > 0 ? saved : fourSectionsToSteps(f));
    setOk(false);
  }

  async function save() {
    if (!form.name.trim()) {
      setErr("请填写模板名称");
      return;
    }
    setErr(null);
    setOk(false);
    try {
      // 分步为主:没填步骤但有四段 → 自动转成步骤(统一存 steps_json,不再存四段)。
      const finalSteps = steps.length > 0 ? steps : fourSectionsToSteps(form);
      // 四段已并入步骤,保存时不落四段字段(避免双写/旧路径读到空整段)。
      const payload = { ...form, opening: "", core: "", objection: "", closing: "", steps_json: stepsToJson(finalSteps), account_id: accountId };
      if (editingId) await api.updateTemplate(editingId, payload);
      else await api.createTemplate(payload);
      setForm(EMPTY);
      setSteps([]);
      setEditingId(null);
      setOk(true);
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  }

  async function remove(id: string) {
    if (!window.confirm("确认删除该话术模板？")) return;
    try {
      await api.deleteTemplate(id);
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  }

  const textarea = "w-full resize-none rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]";

  return (
    <div>
      <div className="mb-8">
        <h1 className="page-title">话术库</h1>
        <p className="page-sub">可复用的对话模板 · 开场白 / 核心话术 / 异议应对 / 收尾，对象卡可绑定</p>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_440px]">
        <section className="card">
          {err && <ErrorState message={err} />}
          {loading ? (
            <LoadingState />
          ) : rows.length === 0 ? (
            <EmptyState label="暂无话术模板，请在右侧新建。" />
          ) : (
            <div className="space-y-3">
              {rows.map((row) => {
                const id = String(row.id ?? "");
                return (
                  <div key={id} className="rounded-lg bg-white/5 p-4">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="font-medium">{String(row.name ?? "-")}</p>
                        <p className="mt-1 text-xs text-[var(--muted)]">
                          {LANGS.find((l) => l[0] === String(row.language ?? "zh"))?.[1] ?? String(row.language ?? "zh")}
                          {String(row.tone_override ?? "") && ` · 语气 ${String(row.tone_override)}`}
                        </p>
                      </div>
                      <div className="flex shrink-0 gap-2">
                        <button className="btn-ghost text-xs" onClick={() => edit(row)}>编辑</button>
                        <button className="btn-ghost text-xs text-red-300" onClick={() => remove(id)}>删除</button>
                      </div>
                    </div>
                    <div className="mt-2 text-xs">
                      {(() => {
                        const s = jsonToSteps(row.steps_json);
                        if (s.length > 0) {
                          return (
                            <div className="space-y-1">
                              {s.map((st, i) => (
                                <p key={i} className="text-[var(--muted)]">
                                  <span className="font-bold text-[var(--accent)]">{i + 1}.</span>{" "}
                                  {st.goal || "(无目标)"}
                                </p>
                              ))}
                            </div>
                          );
                        }
                        return (
                          <div className="grid grid-cols-2 gap-2">
                            {TEMPLATE_FIELDS.map((k) => (
                              <div key={k}>
                                <span className="label">{FIELD_LABELS[k]}</span>
                                <p className="mt-0.5 line-clamp-3 text-[var(--muted)]">{String(row[k] ?? "")}</p>
                              </div>
                            ))}
                          </div>
                        );
                      })()}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section>

        <section className="card space-y-3">
          <span className="label">{editingId ? "编辑模板" : "新建模板"}</span>
          <input
            className="w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="模板名，例如：顺丰理赔·分步（粤语）"
          />

          {/* 分步话术(主编辑方式):1.2.3.4 逐步推进,每步填 目标 + 参考说法 */}
          <div className="rounded-lg border border-[var(--accent)]/40 p-3">
            <div className="flex items-center justify-between">
              <span className="label">分步话术（推荐 · 通话按步骤逐步推进，不会一口气讲完）</span>
              <button
                className="btn-ghost px-2 py-0.5 text-xs"
                onClick={() => setSteps((s) => [...s, { goal: "", ref: "" }])}
              >
                + 加一步
              </button>
            </div>
            <p className="mt-1 whitespace-pre-line text-[11px] leading-relaxed text-[var(--muted)]">{STEPS_HINT}</p>
            <p className="mt-1 text-[11px] leading-relaxed text-[var(--muted)]">
              参考说法可分行写分支：<span className="text-[var(--accent)]">如果客户… → 就…</span>，AI 会看客户实际反应挑对应分支回答。
            </p>
            {steps.length === 0 && (
              <p className="mt-1 text-[11px] text-[var(--muted)]">
                还没配置步骤？点上方「填入示例」一键带出完整分步（含分支写法），或「从旧四段导入」把下方开场/核心/异议/收尾转成步骤。
              </p>
            )}
            <div className="mt-2 space-y-3">
              {steps.map((st, i) => (
                <div key={i} className="rounded-lg border border-[var(--card-border)] bg-white/5 p-2">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-bold text-[var(--accent)]">第 {i + 1} 步</span>
                    <div className="flex gap-1">
                      <button className="btn-ghost px-1.5 py-0 text-xs" disabled={i === 0} onClick={() => setSteps((s) => { const n = [...s]; [n[i - 1], n[i]] = [n[i], n[i - 1]]; return n; })}>↑</button>
                      <button className="btn-ghost px-1.5 py-0 text-xs" disabled={i === steps.length - 1} onClick={() => setSteps((s) => { const n = [...s]; [n[i + 1], n[i]] = [n[i], n[i + 1]]; return n; })}>↓</button>
                      <button className="btn-ghost px-1.5 py-0 text-xs text-red-300" onClick={() => setSteps((s) => s.filter((_, j) => j !== i))}>删</button>
                    </div>
                  </div>
                  <input
                    className="mt-1.5 w-full rounded-lg border border-[var(--card-border)] bg-transparent px-2 py-1 text-xs outline-none focus:border-[var(--accent)]"
                    placeholder="这一步要达成的目标(如:确认包裹是不是{姓名}本人的)"
                    value={st.goal}
                    onChange={(e) => setSteps((s) => s.map((x, j) => (j === i ? { ...x, goal: e.target.value } : x)))}
                  />
                  <textarea
                    className={`mt-1.5 h-20 ${textarea} text-xs`}
                    placeholder={"参考说法(要点+分支;AI 结合客户原话用自己的话讲)\n例:你好,请问係咪{姓名}?我哋係{物流公司}…\n如果客户唔记得 → 提佢下单填嘅地址帮佢回忆"}
                    value={st.ref}
                    onChange={(e) => setSteps((s) => s.map((x, j) => (j === i ? { ...x, ref: e.target.value } : x)))}
                  />
                </div>
              ))}
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              {LANGS.map(([v, l]) => (
                <button key={v} className="btn-ghost text-xs" onClick={() => fillExample(form, setForm, v, setSteps)}>
                  填入{l}理赔示例
                </button>
              ))}
              {(form.opening || form.core || form.objection || form.closing) && (
                <button className="btn-ghost text-xs" onClick={() => { setSteps(fourSectionsToSteps(form)); setForm({ ...form, opening: "", core: "", objection: "", closing: "" }); }}>
                  从旧四段导入步骤
                </button>
              )}
              {steps.length > 0 && (
                <button className="btn-ghost px-2 py-0.5 text-xs text-[var(--muted)]" onClick={() => setSteps([])}>清空步骤</button>
              )}
            </div>
          </div>

          {/* 旧式四段(兼容折叠):历史模板仍可编辑;新模板建议直接用分步 */}
          <div className="rounded-lg border border-[var(--card-border)] p-3">
            <button className="flex w-full items-center justify-between text-left" onClick={() => setShowLegacy((v) => !v)}>
              <span className="text-xs text-[var(--stage-muted)]">旧式四段话术（开场/核心/异议/收尾 — 兼容历史模板，保存时自动转步骤）</span>
              <span className="text-xs text-[var(--muted)]">{showLegacy ? "收起 ▲" : "展开 ▼"}</span>
            </button>
            {showLegacy && (
              <div className="mt-2 space-y-2">
                {TEMPLATE_FIELDS.map((k) => (
                  <label key={k} className="block">
                    <span className="text-xs text-[var(--stage-muted)]">{FIELD_LABELS[k]}</span>
                    <textarea
                      className={`mt-1 h-16 ${textarea} text-xs`}
                      value={form[k]}
                      onChange={(e) => setForm({ ...form, [k]: e.target.value })}
                      placeholder={FIELD_PLACEHOLDERS[k]}
                    />
                  </label>
                ))}
              </div>
            )}
          </div>
          <label className="block">
            <span className="text-xs text-[var(--stage-muted)]">语气覆盖（可选，优先于人设）</span>
            <input
              className="mt-1 w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              value={form.tone_override}
              onChange={(e) => setForm({ ...form, tone_override: e.target.value })}
              placeholder="如：专业、温和、简洁"
            />
          </label>
          <label className="block">
            <span className="text-xs text-[var(--stage-muted)]">语言</span>
            <select
              className="mt-1 w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm"
              value={form.language}
              onChange={(e) => setForm({ ...form, language: e.target.value })}
            >
              {LANGS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
          <div className="flex items-center gap-3">
            <button className="btn-primary" onClick={save}>{editingId ? "保存修改" : "创建模板"}</button>
            {editingId && <button className="btn-ghost" onClick={() => { setEditingId(null); setForm(EMPTY); }}>取消</button>}
            {ok && <span className="text-sm text-emerald-400">已保存。</span>}
          </div>
        </section>
      </div>
    </div>
  );
}
