// 话术分支/意图词提案纯函数（L-② 2026-09-20，语义镜像 CP control_plane/gap_proposals.py）：
// 提案按漏网轮分组；每条漏网轮的三条教法路由（做成快答=既有 / 做成话术分支 /
// 做成意图词）的可用性判断、人话原因兜底、采纳请求体组装、分支行预览。
//
// 本文件必须保持零 import（test/gap-proposals.test.mjs 按 flow-canvas.test.mjs
// 同款单文件转译直载），类型自持——lib/api.ts 以 import type 复用这里的形状，
// 组件层不重复声明。字段名与 GET /api/stats/template-proposals 出仓契约逐字对齐。

/** 提案种类：branch=写进某步的分支行；intent_keyword=给流程图意图加关键词。 */
export type ProposalKind = "branch" | "intent_keyword";

/** GET /api/stats/template-proposals 的 proposals[] 元素（CP 契约形状）。 */
export type TemplateProposal = {
  key: string;
  kind: ProposalKind;
  template_id: string;
  template_name: string;
  customer_text: string;
  norm: string;
  count: number;
  calls: number;
  lang: string;
  sample_answer: string;
  sample_call_id: string;
  step: number;
  step_goal: string;
  branch_cond: string;
  branch_resp: string;
  branch_line: string;
  intent_id: string;
  intent_label: string;
  keyword: string;
  available: boolean;
  blocked_reason: string;
  blocked_label: string;
};

export type ProposalCoverage = {
  turns: number;
  fastpath: number;
  llm: number;
  fastpath_ratio: number;
  by_gen: Record<string, number>;
  by_provider: Record<string, number>;
};

export type ProposalGapRow = {
  customer_text: string;
  count: number;
  calls: number;
  template_id: string;
  step: number;
  lang: string;
  sample_answer: string;
  sample_call_id: string;
};

/** GET /api/stats/template-proposals 整包（coverage/gaps 与 L-① 同源同形）。 */
export type TemplateProposalsReport = {
  coverage: ProposalCoverage;
  gaps: ProposalGapRow[];
  proposals: TemplateProposal[];
  generated_at: number;
};

/** POST /api/stats/template-proposals/adopt 的 items[] 元素。 */
export type ProposalAdoptItem = {
  key: string;
  kind: ProposalKind;
  template_id: string;
  norm: string;
  step: number;
  cond: string;
  text: string;
  intent_id: string;
};

export type ProposalAdoptResult = {
  key: string;
  kind: ProposalKind;
  template_id: string;
  id: string;
  created: boolean;
  detail: string;
};

export type ProposalAdoptResponse = {
  results: ProposalAdoptResult[];
  adopted: number;
};

/** 漏网轮的分组键（与 gap-mining.tsx rowKey 同式：模板|原话）。 */
export function proposalGapKey(templateId: string, customerText: string): string {
  return `${templateId || ""}|${customerText || ""}`;
}

/** 按漏网轮取它的两条提案（branch / intent_keyword 各取第一条）。 */
export function proposalsForGap(
  proposals: TemplateProposal[],
  templateId: string,
  customerText: string,
): { branch: TemplateProposal | null; intent: TemplateProposal | null } {
  const key = proposalGapKey(templateId, customerText);
  let branch: TemplateProposal | null = null;
  let intent: TemplateProposal | null = null;
  for (const p of proposals ?? []) {
    if (!p || proposalGapKey(p.template_id, p.customer_text) !== key) continue;
    if (p.kind === "branch" && !branch) branch = p;
    if (p.kind === "intent_keyword" && !intent) intent = p;
  }
  return { branch, intent };
}

/** 分支路由不可用的人话原因（服务端 blocked_label 优先，缺提案时本地兜底）。 */
export function branchRouteHint(
  gapTemplateId: string,
  branch: TemplateProposal | null,
): string {
  if (branch) {
    if (branch.blocked_label) return branch.blocked_label;
    if (branch.available) return "";
    return "这句话暂时做不了话术分支。";
  }
  return gapTemplateId
    ? "这句话暂时做不了话术分支。"
    : "这通通话没有绑定话术模板，先去「话术」页给这类通话绑定模板。";
}

/** 意图词路由不可用的人话原因（同上）。 */
export function intentRouteHint(
  gapTemplateId: string,
  intent: TemplateProposal | null,
): string {
  if (intent) {
    if (intent.blocked_label) return intent.blocked_label;
    if (intent.available) return "";
    return "这句话暂时做不了意图词。";
  }
  return gapTemplateId
    ? "这句话暂时做不了意图词。"
    : "这通通话没有绑定话术模板，先去「话术」页给这类通话绑定模板。";
}

/** 分支行预览（与 CP branch_line 同形：规范中文锚+→；运行时 parse_step_ref 认账）。 */
export function branchLinePreview(cond: string, resp: string): string {
  return `如果客户${String(cond || "").trim()}→${String(resp || "").trim()}`;
}

/** 组装分支采纳项（resp=人工改后的应答；其余字段原样回传供服务端键校验）。 */
export function buildBranchAdoptItem(p: TemplateProposal, resp: string): ProposalAdoptItem {
  return {
    key: p.key,
    kind: "branch",
    template_id: p.template_id,
    norm: p.norm,
    step: Number(p.step) || 0,
    cond: p.branch_cond,
    text: String(resp || "").trim(),
    intent_id: "",
  };
}

/** 组装意图词采纳项（keyword=人工改后的关键词）。 */
export function buildIntentAdoptItem(p: TemplateProposal, keyword: string): ProposalAdoptItem {
  return {
    key: p.key,
    kind: "intent_keyword",
    template_id: p.template_id,
    norm: p.norm,
    step: 0,
    cond: "",
    text: String(keyword || "").trim(),
    intent_id: p.intent_id,
  };
}
