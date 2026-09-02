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

const STEPS_HINT = "可用变量:{姓名} {快递单号} {快递尾号} {物流公司}。\n参考说法是给 AI 的要点参考,不是逐字稿——AI 会结合客户原话用自己的话讲。";

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

/** 一键填入的成品模板示例（按语言）。用户在示例基础上改，比空白更容易上手。 */
const TEMPLATE_EXAMPLES: Record<string, Omit<typeof EMPTY, "language"> & { language: string; name: string }> = {
  yue: {
    name: "粤语客服·产品咨询（示例）",
    language: "yue",
    opening: "你好，我係{公司}嘅客服{称呼}，請問而家方便講幾分鐘嗎？今日打嚟係想同你介紹我哋嘅{产品}。",
    core:
      "我哋嘅{产品}主打三個賣點：①{賣點一}；②{賣點二}；③{賣點三}。\n" +
      "價錢方面，{报价信息}。如果而家訂購，仲有{优惠}。\n" +
      "你可以隨時打斷我，有咩想了解多啲都可以直接問。",
    objection:
      "如果客戶話「唔使啦/我再睇睇」：\n" +
      "明白，唔緊要。不過想問多一句，你最主要係擔心{常見顧慮}定係{另一顧慮}？\n" +
      "我可以針對你嘅情況解釋下，等你考慮嗰陣有多啲資料。",
    closing:
      "好，唔該晒你今日嘅時間。我哋嘅資料我整理好發俾你參考，有問題隨時搵我。\n" +
      "祝你一切順利，拜拜！",
    tone_override: "地道粤语口语、热情有礼、唔用书面语",
  },
  zh: {
    name: "普通话客服·产品咨询（示例）",
    language: "zh",
    opening: "您好，我是{公司}的客服{称呼}，现在方便聊几分钟吗？今天联系您是想介绍我们的{产品}。",
    core:
      "我们的{产品}主要有三个卖点：①{卖点一}；②{卖点二}；③{卖点三}。\n" +
      "价格方面，{报价信息}。如果现在订购，还有{优惠}。\n" +
      "您可以随时打断我，想多了解哪方面都可以直接问。",
    objection:
      "如果客户说「不需要了/我再看看」：\n" +
      "理解，没关系。不过想多问一句，您主要是担心{常见顾虑}还是{另一顾虑}？\n" +
      "我可以针对您的情况说明一下，方便您考虑时参考。",
    closing: "好的，感谢您今天的时间。资料我整理好发给您参考，有问题随时找我。祝您一切顺利，再见！",
    tone_override: "标准普通话、热情有礼、简洁专业",
  },
  en: {
    name: "English CS · Product Inquiry (example)",
    language: "en",
    opening: "Hello, this is {name} from {company}. Do you have a couple of minutes to talk? I'm reaching out to introduce our {product}.",
    core:
      "Our {product} has three key highlights: ①{point1}; ②{point2}; ③{point3}.\n" +
      "On pricing, {pricing}. If you order now, you also get {offer}.\n" +
      "Feel free to interrupt me — ask about anything you'd like to know more about.",
    objection:
      'If the customer says "not interested / I\'ll think about it":\n' +
      "No problem at all. Just to understand better — is it more about {concern1} or {concern2}?\n" +
      "I can explain that point so you have more to go on.",
    closing: "Thank you for your time today. I'll send you the details for reference — feel free to reach out anytime. Have a great day!",
    tone_override: "Friendly, professional, clear spoken English",
  },
};

function fillExample(form: typeof EMPTY, setForm: (f: typeof EMPTY) => void, lang: string) {
  const ex = TEMPLATE_EXAMPLES[lang];
  if (!ex) return;
  setForm({
    ...form,
    name: ex.name,
    language: ex.language,
    opening: ex.opening,
    core: ex.core,
    objection: ex.objection,
    closing: ex.closing,
    tone_override: ex.tone_override,
  });
}

export default function TemplatesPage() {
  const { accountId } = useAccount();
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState(EMPTY);
  const [steps, setSteps] = useState<FlowStep[]>([]);
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
    setForm({
      name: String(row.name ?? ""),
      opening: String(row.opening ?? ""),
      core: String(row.core ?? ""),
      objection: String(row.objection ?? ""),
      closing: String(row.closing ?? ""),
      tone_override: String(row.tone_override ?? ""),
      language: String(row.language ?? "zh"),
    });
    setSteps(jsonToSteps(row.steps_json));
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
      const payload = { ...form, steps_json: stepsToJson(steps), account_id: accountId };
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
                    <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
                      {TEMPLATE_FIELDS.map((k) => (
                        <div key={k}>
                          <span className="label">{FIELD_LABELS[k]}</span>
                          <p className="mt-0.5 line-clamp-3 text-[var(--muted)]">{String(row[k] ?? "")}</p>
                        </div>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section>

        <section className="card space-y-3">
          <span className="label">{editingId ? "编辑模板" : "新建模板"}</span>
          <div className="rounded-lg border border-[var(--card-border)] bg-white/5 p-3">
            <p className="text-xs text-[var(--muted)]">
              不知道怎么写？点「填入示例」自动带出完整话术（含开场/核心/异议/收尾），再按你产品改成 {`{占位}`} 内容即可。
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              {LANGS.map(([v, l]) => (
                <button key={v} className="btn-ghost text-xs" onClick={() => fillExample(form, setForm, v)}>
                  填入{l}示例
                </button>
              ))}
              {(form.opening || form.core || form.objection || form.closing) && (
                <button
                  className="btn-ghost text-xs text-[var(--muted)]"
                  onClick={() => setForm({ ...form, opening: "", core: "", objection: "", closing: "", name: "", tone_override: "" })}
                >
                  清空
                </button>
              )}
            </div>
          </div>
          <input
            className="w-full rounded-lg border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="模板名，例如：越南采购商常见异议"
          />
          {TEMPLATE_FIELDS.map((k) => (
            <label key={k} className="block">
              <span className="text-xs text-[var(--stage-muted)]">{FIELD_LABELS[k]}</span>
              <textarea
                className={`mt-1 h-20 ${textarea}`}
                value={form[k]}
                onChange={(e) => setForm({ ...form, [k]: e.target.value })}
                placeholder={FIELD_PLACEHOLDERS[k]}
              />
            </label>
          ))}
          <div className="rounded-lg border border-[var(--card-border)] p-3">
            <div className="flex items-center justify-between">
              <span className="label">分步话术(推荐)</span>
              <button
                className="btn-ghost px-2 py-0.5 text-xs"
                onClick={() => setSteps((s) => [...s, { goal: "", ref: "" }])}
              >
                + 加一步
              </button>
            </div>
            <p className="mt-1 whitespace-pre-line text-[11px] leading-relaxed text-[var(--muted)]">{STEPS_HINT}</p>
            {steps.length === 0 && (
              <p className="mt-1 text-[11px] text-[var(--muted)]">未配置分步时,通话用上方开场/核心/异议/收尾整段作为参考。</p>
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
                    placeholder="这一步要达成的目标(如:核对包裹是不是本人的)"
                    value={st.goal}
                    onChange={(e) => setSteps((s) => s.map((x, j) => (j === i ? { ...x, goal: e.target.value } : x)))}
                  />
                  <textarea
                    className={`mt-1.5 h-16 ${textarea} text-xs`}
                    placeholder="参考说法(要点即可,AI 会结合客户原话用自己的话讲;可含 {姓名} {快递单号} 等)"
                    value={st.ref}
                    onChange={(e) => setSteps((s) => s.map((x, j) => (j === i ? { ...x, ref: e.target.value } : x)))}
                  />
                </div>
              ))}
            </div>
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
