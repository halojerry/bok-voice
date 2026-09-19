"use client";

// 模板编辑表单（W1 AI 工作站）：自 apps/web/app/(app)/templates/page.tsx 原样提取的共用组件。
// /templates 页（右侧编辑面板）与 /studio 工作台「话术流程」tab 共用；
// 字段、默认值、占位文案与提取前逐字一致。
// 列表 / 归属过滤 tab / owner 徽标 / 行级 canEdit 闸仍留在页面侧；组件内只做
// owner 只读兜底（共享话术且非主管 → 全字段禁用、不出保存按钮）。
// 纯函数助手（stepsToJson/jsonToSteps/parseStepsFromTable/fourSectionsToSteps/
// STEPS_EXAMPLES/FlowStep/TemplateRow/toTemplateRow）随迁并导出，studio 页复用。

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ErrorState } from "@/components/app-shell";
import { useAccount } from "@/components/account-context";
import { useSession } from "@/components/session-context";
import { VarTextarea } from "@/components/var-insert";

export const LANGS = [
  ["zh", "普通话"],
  ["cantonese", "粤语"],
  ["en", "English"],
] as const;

/** 模板行（编辑器消费面）：字段全部可选，读取一律 String() 防御（行数据来自 CP 裸 dict）。 */
export type TemplateRow = {
  id: string;
  name?: string;
  steps_json?: string;
  graph_json?: string;
  language?: string;
  tone_override?: string;
  hotwords?: string;
  owner_user_id?: string;
  opening?: string;
  core?: string;
  objection?: string;
  closing?: string;
  /** 发布两态派生布尔（W2，CP 列表/详情附)；旧 CP 行无此字段=undefined → 按「未发布」呈现。 */
  published?: boolean;
  has_changes?: boolean;
};

/** CP 裸 dict 行 → TemplateRow（防御式收窄；templates/studio 两页共用）。 */
export function toTemplateRow(row: Record<string, unknown>): TemplateRow {
  const str = (v: unknown) => (typeof v === "string" ? v : undefined);
  return {
    id: String(row.id ?? ""),
    name: str(row.name),
    steps_json: str(row.steps_json),
    graph_json: str(row.graph_json),
    language: str(row.language),
    tone_override: str(row.tone_override),
    hotwords: str(row.hotwords),
    owner_user_id: typeof row.owner_user_id === "string" ? row.owner_user_id : "",
    opening: str(row.opening),
    core: str(row.core),
    objection: str(row.objection),
    closing: str(row.closing),
    published: row.published === true,
    has_changes: row.has_changes === true,
  };
}

/** 模板四段字段的中文标签（数据库里以 opening/core/objection/closing 存储）。 */
export const FIELD_LABELS = {
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

export const TEMPLATE_FIELDS = ["opening", "core", "objection", "closing"] as const;

/** 分步话术:每一步 = 要达成的目标(goal) + 参考说法(ref,可含 {变量})。
 * say=直念步:进入该步的当轮 AI 逐字念参考说法首行(通知/道歉等要逐字一致的
 * 合规内容),不走 LLM 自由发挥;第 1 步的首行始终是开场白直念,不必勾选。 */
export interface FlowStep {
  goal: string;
  ref: string;
  say?: boolean;
  emotion?: string;
  /** 场景（W2 流程画布泳道,纯分组不改推进语义）；缺失/空=""。表单 UI 不渲染,画布专属。 */
  scene?: string;
}

type TplForm = {
  name: string;
  opening: string;
  core: string;
  objection: string;
  closing: string;
  tone_override: string;
  language: string;
  hotwords: string;
};

const EMPTY_FORM: TplForm = {
  name: "",
  opening: "",
  core: "",
  objection: "",
  closing: "",
  tone_override: "",
  language: "zh",
  hotwords: "",
};

const STEPS_HINT = "可用变量:{姓名}/{名字}/{name} {快递单号} {快递尾号}/{tracking_tail} {物流公司}/{快递公司}/{courier} {收货地址}/{地址} {电话} {聯絡方式}/{联系方式}/{contact}。完整目录与预览见工作站·变量 tab。\n参考说法是给 AI 的要点参考,不是逐字稿——AI 会结合客户原话用自己的话讲。\n勾选「直念」的步骤:进入该步的当轮 AI 逐字念参考说法首行,适合通知/道歉等要逐字一致的内容。\n直念步可选「情绪」:只在罐头物化时烧进音频(如致歉步选低沉柔和),实时生成的回复保持语气稳定不受影响;改情绪/参考说法后需重跑 tts-pregen。";

/** 把 steps 序列化/反序列化为 steps_json(存库)。say 只在 true 时写出(省体积);
 * emotion 只在直念步且非空时写出(2026-09-16 罐头带情绪,pregen 物化烧进音频);
 * scene 只在非空时写出(W2 画布泳道,旧数据无键零变化——往返不丢,画布保存键死)。 */
export function stepsToJson(steps: FlowStep[]): string {
  return JSON.stringify(
    steps
      .filter((s) => s.goal.trim() || s.ref.trim())
      .map((s) => {
        const base = { goal: s.goal, ref: s.ref };
        if (!s.say) return s.scene ? { ...base, scene: s.scene } : base;
        return {
          ...base,
          say: 1,
          ...(s.emotion ? { emotion: s.emotion } : {}),
          ...(s.scene ? { scene: s.scene } : {}),
        };
      }),
  );
}
export function jsonToSteps(raw: unknown): FlowStep[] {
  try {
    const arr = JSON.parse(String(raw ?? "") || "[]");
    if (!Array.isArray(arr)) return [];
    return arr
      .filter((s) => s && typeof s === "object")
      .map((s) => ({
        goal: String((s as { goal?: unknown }).goal ?? ""),
        ref: String((s as { ref?: unknown }).ref ?? ""),
        say: Boolean((s as { say?: unknown }).say),
        emotion:
          typeof (s as { emotion?: unknown }).emotion === "string"
            ? String((s as { emotion?: unknown }).emotion).toLowerCase()
            : "",
        scene: typeof (s as { scene?: unknown }).scene === "string" ? String((s as { scene?: unknown }).scene) : "",
      }));
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

export function fourSectionsToSteps(form: { opening: string; core: string; objection: string; closing: string }): FlowStep[] {
  const out: FlowStep[] = [];
  (["opening", "core", "objection", "closing"] as const).forEach((k) => {
    const txt = String(form[k] ?? "").trim();
    if (txt) out.push({ goal: LEGACY_STEP_GOALS[k], ref: txt });
  });
  return out;
}

/** 分步格式的成品示例(理赔场景,示范 goal/ref/分支/{变量} 的写法)。 */
export const STEPS_EXAMPLES: Record<string, FlowStep[]> = {
  cantonese: [
    { goal: "确认包裹是不是{姓名}本人的", ref: "你好，请问係咪{姓名}？我哋係{物流公司}，有个包裹单号尾号{快递尾号}运输途中唔见咗，想同你核对下。\n如果客户唔记得 → 提佢下单时填嘅地址/电话帮佢回忆" },
    { goal: "说明一赔二方案,稳住客户", ref: "係我哋责任,我哋有买运费保险,会以一赔二赔俾你,唔使自己蚀钱。\n如果客户问点解要赔 → 讲係运输途中遗失,顺丰全责\n如果客户担心唔到账 → 讲赔付会直接落微信零钱/钱包\n如果客户话要重新买 → 讲可以用赔付抵扣,唔使自己再贴钱" },
    { goal: "向客户索取微信号,微信专员加客户办理", ref: "理赔係通过微信消费者保护线上专员办理,麻烦你将你嘅微信号报俾我,微信专员会加你,按佢步骤操作办理就得。\n如果客户话唔方便/而家冇空 → 问佢几时方便或者用咩方式,约好再跟进" },
    { goal: "收尾确认,礼貌结束", ref: "好,唔该晒你今日嘅时间。我哋会跟进,有咩问题随时搵我。祝你一切顺利,拜拜!" },
  ],
  zh: [
    { goal: "确认包裹是不是{姓名}本人的", ref: "您好，请问是{姓名}吗？我们是{物流公司}，有个包裹单号尾号{快递尾号}运输途中丢失了，想跟您核对一下。\n如果客户不记得 → 提他下单时填的地址/电话帮他回忆" },
    { goal: "说明一赔二方案,稳住客户", ref: "这是我们的责任，我们有购买运费保险，会以一赔二赔付给您，不用自己贴钱。\n如果客户问为什么赔 → 说明是运输途中遗失，我方全责\n如果客户担心不到账 → 说明赔付会直接到微信零钱/钱包\n如果客户说要重新买 → 说明可以用赔付抵扣，不用自己再贴钱" },
    { goal: "向客户索取微信号，微信专员加客户办理", ref: "理赔是通过微信消费者保护线上专员办理，麻烦您把您的微信号报给我，微信专员会加您，按步骤操作办理就行。\n如果客户说不方便/现在没空 → 问他什么时候方便或用什么方式，约好再跟进" },
    { goal: "收尾确认,礼貌结束", ref: "好的，感谢您今天的时间。我们会跟进，有问题随时找我。祝您一切顺利，再见！" },
  ],
  en: [
    { goal: "Confirm the parcel belongs to {name}", ref: "Hello, is this {name}? We're {courier}. A parcel (tracking ending {tracking_tail}) was lost in transit and I'd like to verify with you." },
    { goal: "Explain 1-for-2 compensation and reassure", ref: "It's our responsibility. We have shipping insurance, so we'll compensate 2x. You won't lose money.\nIf they ask why → it was lost in transit, we take full responsibility\nIf they worry about payment → it goes straight to their WeChat wallet" },
    { goal: "Ask for their WeChat ID; specialist will add them", ref: "The claim is handled by our WeChat consumer-protection specialist. Could you give me your WeChat ID? The specialist will add you and walk you through the steps.\nIf they're busy right now → ask when or how works best and arrange a follow-up." },
    { goal: "Confirm and close politely", ref: "Thank you for your time. We'll follow up — reach out anytime. Goodbye!" },
  ],
};

/** 解析单元格：去掉首尾空白。 */
function cell(raw: string): string {
  return raw.replace(/\s+/g, " ").trim();
}

/**
 * 从表格文本解析步骤。支持：
 *  - TSV：每行一步，第一列=目标，第二列=参考说法（默认按 \t 分列；若没有 \t 则退化为
 *    「目标,参考说法」整行判断）。
 *  - CSV（带表头）：识别 目标/goal/参考/ref/参考说法 列。
 *  - 纯 TSV 无表头：第一列当目标、第二列当参考。
 * 返回 {steps, error}；error 非空表示解析失败。
 */
export function parseStepsFromTable(text: string): { steps: FlowStep[]; error: string } {
  const lines = text.split(/\r?\n/).map((l) => l.trimEnd());
  const nonEmpty = lines.filter((l) => l.trim() !== "");
  if (nonEmpty.length === 0) return { steps: [], error: "没有可导入的内容。" };
  const containsTab = nonEmpty.some((l) => l.includes("\t"));
  // CSV 解析：支持引号包裹与逗号分隔（含中文逗号，因粘贴可能来自表格软件）。
  const rows: string[][] = nonEmpty.map((line) => {
    if (containsTab) return line.split("\t");
    // 尝试 CSV：处理带引号字段
    const out: string[] = [];
    let cur = "";
    let inQ = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (inQ) {
        if (ch === '"') {
          if (line[i + 1] === '"') { cur += '"'; i++; } else inQ = false;
        } else cur += ch;
      } else if (ch === '"') inQ = true;
      else if (ch === "," || ch === "，") { out.push(cur); cur = ""; }
      else cur += ch;
    }
    out.push(cur);
    return out;
  });
  // 表头检测：第一行含 目标/goal/参考/ref 字样则视为表头。
  const head = rows[0].map((h) => cell(h.toLowerCase()));
  const isHeader = head.some((h) => ["目标", "goal", "目的", "参考", "参考说法", "ref", "话术", "说法"].includes(h));
  const headerMap: Record<string, number> = {};
  if (isHeader) {
    head.forEach((h, idx) => {
      if (["目标", "goal", "目的"].includes(h)) headerMap.goal = idx;
      else if (["参考", "参考说法", "ref", "话术", "说法"].includes(h)) headerMap.ref = idx;
    });
    if (headerMap.goal === undefined) return { steps: [], error: "表头缺少「目标」列（可用：目标/goal）。" };
    if (headerMap.ref === undefined) return { steps: [], error: "表头缺少「参考说法」列（可用：参考/ref/话术）。" };
  }
  const dataRows = isHeader ? rows.slice(1) : rows;
  const steps: FlowStep[] = [];
  for (const r of dataRows) {
    if (!r.some((c) => c.trim() !== "")) continue;
    let goal = "";
    let ref = "";
    if (isHeader) {
      goal = cell(r[headerMap.goal] ?? "");
      ref = cell(r[headerMap.ref] ?? "");
    } else {
      goal = cell(r[0] ?? "");
      ref = cell(r[1] ?? "");
    }
    if (!goal && !ref) continue;
    steps.push({ goal, ref });
  }
  if (steps.length === 0) return { steps: [], error: "解析后没有有效步骤（每行至少要有目标或参考说法）。" };
  return { steps, error: "" };
}

/** 发布态徽标三态（W2，编辑器头部与 studio 列表行共用）：
 * 行无 published 字段（旧 CP 兼容）或 false=灰「未发布」；published 且无改动=绿「已发布」；
 * 有未发布改动=琥珀「有未发布改动」。行数据是 CP 裸 dict，字段按 unknown 防御收窄。 */
export function PublishBadge(props: { row: { published?: unknown; has_changes?: unknown } }) {
  const published = props.row.published === true;
  const hasChanges = props.row.has_changes === true;
  const [cls, label] = !published
    ? ["bg-muted muted", "未发布"]
    : hasChanges
      ? ["bg-amber-100 text-amber-700", "有未发布改动"]
      : ["bg-emerald-100 text-emerald-700", "已发布"];
  return <span className={`rounded-sm px-1.5 py-0.5 text-[10px] font-normal ${cls}`}>{label}</span>;
}

export default function TemplateEditor(props: {
  /** 编辑的模板行；null/无 id = 新建态。 */
  tpl: TemplateRow | null;
  /** 保存成功（创建或更新）后回调：页面侧刷新列表/重拉模板行。 */
  onSaved?: () => void;
  /** 「取消」回调：页面侧清掉选中回到新建态；不给时组件本地退回新建态。 */
  onCancel?: () => void;
}) {
  const { accountId } = useAccount();
  const session = useSession();
  const [form, setForm] = useState<TplForm>(EMPTY_FORM);
  const [steps, setSteps] = useState<FlowStep[]>([]);
  // 编辑态组件内自持（save 用它决定 create/update，与原 editingId 同语义）；
  // tpl.id 变化即重锚表单（保存后嵌入方重拉同一模板=同 id，不会冲掉已切到的新建态）。
  const [editing, setEditing] = useState(false);
  const [showLegacy, setShowLegacy] = useState(false);
  const [showTableImport, setShowTableImport] = useState(false);
  const [tableText, setTableText] = useState("");
  const [tableMsg, setTableMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);
  const [pubErr, setPubErr] = useState<string | null>(null);
  const [publishing, setPublishing] = useState(false);

  const tpl = props.tpl;
  const tplId = String(tpl?.id ?? "");
  useEffect(() => {
    setEditing(Boolean(tpl?.id));
    const f: TplForm = {
      name: String(tpl?.name ?? ""),
      opening: String(tpl?.opening ?? ""),
      core: String(tpl?.core ?? ""),
      objection: String(tpl?.objection ?? ""),
      closing: String(tpl?.closing ?? ""),
      tone_override: String(tpl?.tone_override ?? ""),
      language: String(tpl?.language ?? "zh"),
      hotwords: String(tpl?.hotwords ?? ""),
    };
    setForm(f);
    // 分步为主:旧模板(只有四段无 steps)载入时自动转成步骤,让用户按步骤编辑。
    const saved = jsonToSteps(tpl?.steps_json);
    setSteps(saved.length > 0 ? saved : fourSectionsToSteps(f));
    setErr(null);
    setPubErr(null);
    // 只跟 tpl.id 走：对象引用换新但同 id（保存后重拉）不重锚。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tplId]);

  // B4 owner 只读兜底（行级 canEdit 闸在页面侧；studio 直达详情时靠这里兜底）：
  // 主管（admin/root/本地匿名）可改；话务员只改自己的，共享/他人模板只读（服务端 403 兜底）。
  const isManager = Boolean(
    session && (session.anonymous || session.role === "admin" || session.role === "root"),
  );
  const uid = session?.user_id ?? "";
  const readOnly = Boolean(tplId) && !isManager && !(uid !== "" && String(tpl?.owner_user_id ?? "") === uid);

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
      // 空白步(goal+ref 全空)会被 stepsToJson 静默过滤——计数提示,防「明明填了 N 步存出来少几步」困惑(2026-09-09 QA B2)。
      const droppedBlanks = finalSteps.filter((s) => !s.goal.trim() && !s.ref.trim()).length;
      if (droppedBlanks > 0) setErr(`已忽略 ${droppedBlanks} 个空白步（目标与参考说法都为空）。`);
      // 四段已并入步骤,保存时不落四段字段(避免双写/旧路径读到空整段)。
      const payload = { ...form, opening: "", core: "", objection: "", closing: "", steps_json: stepsToJson(finalSteps), account_id: accountId };
      if (editing) await api.updateTemplate(String(tpl?.id ?? ""), payload);
      else await api.createTemplate(payload);
      setEditing(false);
      setForm(EMPTY_FORM);
      setSteps([]);
      setOk(true);
      props.onSaved?.();
    } catch (e) {
      setErr(String(e));
    }
  }

  /** 发布当前版本（W2）：confirm 后冻结当时 live 九键 → 外层 onSaved 重拉行刷新徽标。
   * 只读（共享话术非主管）与新建态不出按钮——发布闸链与 PUT 同（CP 侧兜底）。 */
  async function publish() {
    if (!tplId || !window.confirm("发布后新通话将使用此版本？")) return;
    setPublishing(true);
    setPubErr(null);
    try {
      await api.publishTemplate(tplId);
      props.onSaved?.();
    } catch (e) {
      setPubErr(String(e));
    } finally {
      setPublishing(false);
    }
  }

  function cancelEdit() {
    if (props.onCancel) {
      props.onCancel();
      return;
    }
    // 未接 onCancel 的嵌入方（防御）：本地退回新建态。
    setEditing(false);
    setForm(EMPTY_FORM);
    setSteps([]);
  }

  /** 填入三语理赔示例（分步为主:示例直接填成分步,四段清空由步骤统一承载）。 */
  function fillExample(lang: string) {
    const example = STEPS_EXAMPLES[lang];
    if (!example) return;
    const nameByLang = { cantonese: "理赔·分步（粤语示例）", zh: "理赔·分步（普通话示例）", en: "Claims · Step-by-step (English)" };
    setForm({ ...form, name: nameByLang[lang as keyof typeof nameByLang] ?? "", opening: "", core: "", objection: "", closing: "", language: lang });
    setSteps(example);
  }

  /** 从表格粘贴导入：解析成步骤并填充当前编辑区（可继续增删改）。 */
  function importTable() {
    const { steps: parsed, error } = parseStepsFromTable(tableText);
    if (error) {
      setTableMsg(error);
      return;
    }
    setSteps((prev) => [...prev, ...parsed]);
    setTableMsg(`已从表格导入 ${parsed.length} 步。`);
    setShowTableImport(false);
    setTableText("");
  }

  const textarea = "w-full resize-none rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--live)";

  return (
    <section className="card space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="label">{editing ? "编辑模板" : "新建模板"}</span>
        {editing && (
          <div className="flex items-center gap-2">
            <PublishBadge row={tpl ?? {}} />
            {!readOnly && (
              <button className="btn-ghost px-2 py-0.5 text-xs" disabled={publishing} onClick={publish}>
                {publishing ? "发布中…" : "发布当前版本"}
              </button>
            )}
          </div>
        )}
      </div>
      {pubErr && <ErrorState message={pubErr} />}
      {readOnly && (
        <p className="rounded-lg bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
          共享话术由主管维护；你可以查看但不能修改。
        </p>
      )}
      <input
        className="w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--live)"
        value={form.name}
        disabled={readOnly}
        onChange={(e) => setForm({ ...form, name: e.target.value })}
        placeholder="模板名，例如：顺丰理赔·分步（粤语）"
      />

      {/* 分步话术(主编辑方式):1.2.3.4 逐步推进,每步填 目标 + 参考说法 */}
      <div className="rounded-lg border border-(--live)/40 p-3">
        <div className="flex items-center justify-between">
          <span className="label">分步话术（推荐 · 通话按步骤逐步推进，不会一口气讲完）</span>
          <button
            className="btn-ghost px-2 py-0.5 text-xs"
            disabled={readOnly}
            onClick={() => setSteps((s) => [...s, { goal: "", ref: "" }])}
          >
            + 加一步
          </button>
        </div>
        <p className="mt-1 whitespace-pre-line text-[11px] leading-relaxed muted">{STEPS_HINT}</p>
        <p className="mt-1 text-[11px] leading-relaxed muted">
          参考说法可分行写分支：<span className="text-(--live-ink)">如果客户… → 就…</span>，AI 会看客户实际反应挑对应分支回答。
        </p>
        {steps.length === 0 && (
          <p className="mt-1 text-[11px] muted">
            还没配置步骤？点上方「填入示例」一键带出完整分步（含分支写法），或「从旧四段导入」把下方开场/核心/异议/收尾转成步骤。
          </p>
        )}
        <div className="mt-2 space-y-3">
          {steps.map((st, i) => (
            <div key={i} className="rounded-lg border border-(--card-border) bg-muted/60 p-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-bold text-(--live-ink)">第 {i + 1} 步</span>
                <div className="flex gap-1">
                  <button className="btn-ghost px-1.5 py-0 text-xs" disabled={readOnly || i === 0} onClick={() => setSteps((s) => { const n = [...s]; [n[i - 1], n[i]] = [n[i], n[i - 1]]; return n; })}>↑</button>
                  <button className="btn-ghost px-1.5 py-0 text-xs" disabled={readOnly || i === steps.length - 1} onClick={() => setSteps((s) => { const n = [...s]; [n[i + 1], n[i]] = [n[i], n[i + 1]]; return n; })}>↓</button>
                  <button className="btn-ghost px-1.5 py-0 text-xs text-red-600" disabled={readOnly} onClick={() => setSteps((s) => s.filter((_, j) => j !== i))}>删</button>
                </div>
              </div>
              <input
                className="mt-1.5 w-full rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--live)"
                placeholder="这一步要达成的目标(如:确认包裹是不是{姓名}本人的)"
                value={st.goal}
                disabled={readOnly}
                onChange={(e) => setSteps((s) => s.map((x, j) => (j === i ? { ...x, goal: e.target.value } : x)))}
              />
              <div className="mt-1.5">
                <VarTextarea
                  className={`h-24 ${textarea} text-xs`}
                  placeholder={"参考说法(要点+分支;AI 结合客户原话用自己的话讲)\n例:你好,请问係咪{姓名}?我哋係{物流公司}…\n如果客户唔记得 → 提佢下单填嘅地址帮佢回忆"}
                  value={st.ref}
                  disabled={readOnly}
                  onChange={(v) => setSteps((s) => s.map((x, j) => (j === i ? { ...x, ref: v } : x)))}
                />
              </div>
              <label className="mt-1 flex items-center gap-1.5 text-[11px] muted">
                <input
                  type="checkbox"
                  className="size-3 accent-(--live)"
                  checked={Boolean(st.say)}
                  disabled={readOnly}
                  onChange={(e) => setSteps((s) => s.map((x, j) => (j === i ? { ...x, say: e.target.checked } : x)))}
                />
                直念(进入该步的当轮逐字念首行,适合通知/道歉等合规内容)
              </label>
              {Boolean(st.say) && (
                <label className="mt-1 flex items-center gap-1.5 text-[11px] muted">
                  情绪
                  <select
                    className="rounded-lg border border-(--card-border) bg-transparent px-1.5 py-0.5 text-xs outline-hidden focus:border-(--live)"
                    value={st.emotion ?? ""}
                    disabled={readOnly}
                    onChange={(e) => setSteps((s) => s.map((x, j) => (j === i ? { ...x, emotion: e.target.value } : x)))}
                  >
                    <option value="">自动(不下发,按文本匹配)</option>
                    <option value="calm">平稳自然</option>
                    <option value="sad">低沉柔和(致歉/安抚)</option>
                    <option value="happy">轻快亲切</option>
                    <option value="surprised">惊讶上扬</option>
                  </select>
                  <span className="text-[10px]">罐头物化时烧进音频;实时回复不受影响</span>
                </label>
              )}
            </div>
          ))}
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <button className="btn-ghost text-xs" disabled={readOnly} onClick={() => setShowTableImport((v) => !v)}>
            {showTableImport ? "收起表格导入 ▲" : "从表格粘贴导入 ▼"}
          </button>
          {LANGS.map(([v, l]) => (
            <button key={v} className="btn-ghost text-xs" disabled={readOnly} onClick={() => fillExample(v)}>
              填入{l}理赔示例
            </button>
          ))}
          {!readOnly && (form.opening || form.core || form.objection || form.closing) && (
            <button className="btn-ghost text-xs" onClick={() => { setSteps(fourSectionsToSteps(form)); setForm({ ...form, opening: "", core: "", objection: "", closing: "" }); }}>
              从旧四段导入步骤
            </button>
          )}
          {steps.length > 0 && (
            <button className="btn-ghost px-2 py-0.5 text-xs muted" disabled={readOnly} onClick={() => setSteps([])}>清空步骤</button>
          )}
        </div>
        {showTableImport && (
          <div className="mt-2 rounded-lg border border-dashed border-(--card-border) p-2">
            <p className="text-[11px] leading-relaxed muted">
              从表格（Excel/Google Sheets/CSV）粘贴：<b>每行一步</b>，第一列=目标，第二列=参考说法。
              支持带表头（列名：目标/参考说法）或不带表头（直接两列）。参考说法里可写
              <span className="text-(--live-ink)">如果客户… → 就…</span>分支与{"{变量}"}。
            </p>
            <textarea
              className={`mt-1.5 h-24 ${textarea} text-xs`}
              placeholder={"目标\t参考说法\n开场确认\t你好,请问係咪{姓名}?我哋係{物流公司}…\n异议应对\t如果客户唔记得 → 提佢下单填嘅地址帮佢回忆"}
              value={tableText}
              disabled={readOnly}
              onChange={(e) => { setTableText(e.target.value); setTableMsg(null); }}
            />
            <div className="mt-1.5 flex items-center gap-2">
              <button className="btn-primary px-3 py-1 text-xs" disabled={readOnly} onClick={importTable}>导入为步骤</button>
              {tableMsg && <span className="text-xs muted">{tableMsg}</span>}
            </div>
          </div>
        )}
      </div>

      {/* 旧式四段(兼容折叠):历史模板仍可编辑;新模板建议直接用分步 */}
      <div className="rounded-lg border border-(--card-border) p-3">
        <button className="flex w-full items-center justify-between text-left" onClick={() => setShowLegacy((v) => !v)}>
          <span className="text-xs muted">旧式四段话术（开场/核心/异议/收尾 — 兼容历史模板，保存时自动转步骤）</span>
          <span className="text-xs muted">{showLegacy ? "收起 ▲" : "展开 ▼"}</span>
        </button>
        {showLegacy && (
          <div className="mt-2 space-y-2">
            {TEMPLATE_FIELDS.map((k) => (
              <label key={k} className="block">
                <span className="text-xs muted">{FIELD_LABELS[k]}</span>
                <textarea
                  className={`mt-1 h-16 ${textarea} text-xs`}
                  value={form[k]}
                  disabled={readOnly}
                  onChange={(e) => setForm({ ...form, [k]: e.target.value })}
                  placeholder={FIELD_PLACEHOLDERS[k]}
                />
              </label>
            ))}
          </div>
        )}
      </div>
      <label className="block">
        <span className="text-xs muted">语气覆盖（可选，优先于人设）</span>
        <input
          className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--live)"
          value={form.tone_override}
          disabled={readOnly}
          onChange={(e) => setForm({ ...form, tone_override: e.target.value })}
          placeholder="如：专业、温和、简洁"
        />
      </label>
      <label className="block">
        <span className="text-xs muted">识别热词（可选，本套话术专属）</span>
        <input
          className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--live)"
          value={form.hotwords}
          disabled={readOnly}
          onChange={(e) => setForm({ ...form, hotwords: e.target.value })}
          placeholder="如：順豐速運, 集運, 理賠（逗号/顿号分隔）"
        />
        <span className="mt-1 block text-[11px] leading-relaxed muted">
          客户或话术里的专名词容易听错，填进这里 AI 语音识别会更准。纯数字串会被忽略（防止识别出错误号码）。
        </span>
      </label>
      <label className="block">
        <span className="text-xs muted">语言</span>
        <select
          className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm"
          value={form.language}
          disabled={readOnly}
          onChange={(e) => setForm({ ...form, language: e.target.value })}
        >
          {LANGS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </label>
      {err && <ErrorState message={err} />}
      <div className="flex items-center gap-3">
        {!readOnly && <button className="btn-primary" onClick={save}>{editing ? "保存修改" : "创建模板"}</button>}
        {editing && !readOnly && <button className="btn-ghost" onClick={cancelEdit}>取消</button>}
        {ok && <span className="text-sm text-emerald-600">已保存。</span>}
      </div>
    </section>
  );
}
