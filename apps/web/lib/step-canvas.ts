// 一步一画布派生层（Scene Canvas v2，2026-09-26，docs/SCENE_CANVAS.md）：
// 场景=步（1:1）。本文件=「第 N 步的宇宙」的纯函数派生与写回——
//   stepUniverse：steps+graph(+qa 标签)+当前步 → 锚点/意图/目标列/默认虚线；
//   layoutStepUniverse：宇宙 → 固定三列确定性布局的节点与边（不存坐标、不跑 dagre）；
//   buildRail：步列（导航+徽标）；
//   setStepBranchAction/clearStepBranchAction：锚点连线写回 steps 草稿
//     （标记写进分支行 resp，parse/serialize round-trip 无损依赖 flow-canvas 既有纪律）。
//
// 引擎零改动契约：本层只是既有 steps_json/graph_json 的投影视图——分支线=ref 行动作标记
// （flow.py parse_branch_action 消费），意图线=graph 绑定（jump_step/play_qa/notify_human），
// 默认推进虚线=should_auto_advance 的可视化（只读，不落任何数据）。
//
// 依赖纪律：只 import "./flow-canvas"（同为纯函数层;测试装配把两文件转译进同一 outDir，
// require 相对路径天然解析——见 test/step-canvas.test.mjs 头注）。类型自持，零副作用零 IO。

import {
  parseStepRefParts,
  parseBranchAction,
  composeBranchResp,
  serializeStepRef,
  type CanvasFlowStep,
  type GraphLite,
} from "./flow-canvas";

// 分支动作类型由 flow-canvas 权威定义（"" | hold | refuse | handoff | jump）。
export type { CanvasFlowStep, GraphLite, BranchAction, StepBranch, BranchActionInfo } from "./flow-canvas";
export { parseBranchAction, composeBranchResp, branchActionBadge, parseStepRefParts, serializeStepRef } from "./flow-canvas";

// —— 宇宙数据形状 ——

/** 步节点上的分支锚点（=答法抽屉同源数据 + 派生动作位）。resp 恒为原文含标记。 */
export type StepAnchor = {
  /** 分支在 parseStepRefParts().branches 里的下标（写回寻址）。 */
  branchIndex: number;
  cond: string;
  resp: string;
  action: import("./flow-canvas").BranchAction;
  /** action==="jump" 时的 1-based 目标步号（钳后;其余动作=0）。 */
  jump: number;
};

/** 意图绑定（enabled）的画布视图。qaLabel=qa_id 反查的词条问题摘要（缺省空串）。 */
export type BindingView = {
  bindingId: string;
  action: string; // "jump_step" | "play_qa" | "notify_human" | 其它（不画线）
  step?: number; // jump_step 1-based
  qaId?: string;
  qaLabel?: string;
  thenJump?: number;
};

/** 触发列的意图卡（scoped=作用域含本步;disabled 也出卡,组件置灰——与旧画布口径一致）。 */
export type IntentCard = {
  intentId: string;
  label: string;
  keywords: string[];
  enabled: boolean;
  judge: boolean;
  bindings: BindingView[];
};

/** 目标列 chip。kind=step（跳步目标）/ refuse（收线终点）/ bell（转人工+打铃共用终点——
 *  分支【转人工】与意图 notify_human 是同一个引擎动作 notify_human,不拆两个终点）。 */
export type TargetChip = {
  id: string; // "sc:tgt:step:N" / "sc:tgt:refuse" / "sc:tgt:bell"
  kind: "step" | "refuse" | "bell";
  stepNo?: number; // step chip 1-based
  label: string;
  /** 连到这枚 chip 的边 id 列表（branch/intent;ghost 不计——它是引擎默认不是配置）。 */
  usedBy: string[];
};

/** 默认推进虚线（只读可视化 should_auto_advance;末步=讲完自动收线）。 */
export type GhostFlow = {
  targetStepNo: number | null; // 非末步=下一步 1-based;末步=null
  closing: boolean;
  label: string;
};

/** 第 N 步的宇宙（layout 的唯一输入）。 */
export type StepUniverse = {
  stepNo: number; // 1-based
  index: number; // 0-based
  goal: string;
  scriptFirst: string; // 正稿首行(40 字截断);纯分支 ref 退首分支摘要（旧画布同款口径）
  say: boolean;
  emotion: string;
  scene: string;
  anchors: StepAnchor[];
  scopedIntents: IntentCard[];
  /** steps=[]（全程生效）的意图——不铺节点,收成顶部横条（防每张画布重复铺同一批）。 */
  globalIntents: IntentCard[];
  chips: TargetChip[]; // 其余各步 + 收线 + 打铃
  ghost: GhostFlow;
};

// —— 固定三列布局常量（确定性:同输入同输出;坐标不持久化,改样式只改这里） ——
// 列距按 1280 视口（侧栏 270+步列 240 后画布约 720px 宽）收紧：宇宙总宽 ≈ 980,
// fitView 0.6 下限下整图可入视野（2026-09-26 真浏览器验收:首版 1060 宽+0.75 下限
// 在抽屉打开/窄视口下三列全裁——右列 chip 被 flex 抽屉整列遮掉）。
export const SC_INTENT_COL_X = 30; // 左列：触发（意图卡+快答徽章）
export const SC_STEP_X = 370; // 中列：步节点
export const SC_TARGET_COL_X = 750; // 右列：目标 chip（+SC_CHIP_NODE_W=220 → 右缘 970）
export const SC_TOP_Y = 60;
export const SC_STEP_NODE_W = 320;
export const SC_INTENT_NODE_W = 260;
export const SC_CHIP_NODE_W = 220;
/** 左列纵向节奏：意图卡估高 + 每张 play_qa 徽章一行 + 卡间距。 */
export const SC_INTENT_CARD_H = 116;
export const SC_QA_BADGE_H = 44;
export const SC_INTENT_GAP = 28;
/** 右列纵向节奏。 */
export const SC_CHIP_H = 64;
export const SC_CHIP_GAP = 16;

// —— RF 节点/边（layout 产物;组件直转 <Node>/<Edge>） ——

export type ScNode =
  | {
      id: "sc:step";
      kind: "step";
      x: number;
      y: number;
      stepNo: number;
      goal: string;
      scriptFirst: string;
      say: boolean;
      emotion: string;
      scene: string;
      anchors: StepAnchor[];
    }
  | { id: string; kind: "intent"; x: number; y: number; card: IntentCard }
  | { id: string; kind: "chip"; x: number; y: number; chip: TargetChip }
  | { id: string; kind: "qabadge"; x: number; y: number; bindingId: string; qaId: string; label: string };

export type ScEdge = {
  id: string;
  source: string;
  sourceHandle: string;
  target: string;
  targetHandle: string;
  /** branch=分支锚点线;intent=意图绑定线;ghost=默认推进虚线（只读）。 */
  kind: "branch" | "intent" | "ghost";
  label: string;
  /** branch 边:锚点寻址（解绑/抽屉定位）。 */
  branchIndex?: number;
  /** intent 边:绑定寻址。 */
  intentId?: string;
  bindingId?: string;
  action?: string;
};

/** 正稿首行摘要（与旧画布 layoutFlow 同口径:纯分支 ref 退首分支行,标记不进摘要）。 */
function stepPreviewLine(ref: string): string {
  const parts = parseStepRefParts(ref);
  let first = parts.script.split("\n")[0] ?? "";
  if (!first && parts.branches[0]) {
    const info = parseBranchAction(parts.branches[0].resp);
    first = "如果客户" + parts.branches[0].cond + "→" + (info.text || info.action);
  }
  return first.slice(0, 40);
}

/**
 * 派生第 stepNo 步（1-based）的宇宙。越界/非整数 → null。
 * graph 缺省=空图（无意图;chips/ghost 照常）。qaLabels: qa_id → 词条问题摘要。
 */
export function stepUniverse(
  steps: CanvasFlowStep[],
  graph: GraphLite | null | undefined,
  stepNo: number,
  opts?: { qaLabels?: Record<string, string> },
): StepUniverse | null {
  const raw = Number(stepNo);
  // 严格整数契约：2.5/NaN/越界一律 null——步号是组件内部状态,不容静默舍入错步
  //（子代理实测指出旧 Math.round 版会把 2.5 落到第 3 步,2026-09-26 审查收紧）。
  if (!Array.isArray(steps) || !Number.isInteger(raw) || raw < 1 || raw > steps.length) return null;
  const n = raw;
  const i = n - 1;
  const st = steps[i] ?? { goal: "", ref: "" };
  const parts = parseStepRefParts(st.ref ?? "");

  const anchors: StepAnchor[] = parts.branches.map((b, bi) => {
    const info = parseBranchAction(b.resp);
    return { branchIndex: bi, cond: b.cond, resp: b.resp, action: info.action, jump: info.step };
  });

  // 意图分桶：steps 非空且含本步=scoped;空/缺=全程（横条）。disabled 意图也出卡（置灰）。
  const intents = graph?.intents ?? [];
  const bindings = graph?.bindings ?? [];
  const toCard = (it: NonNullable<GraphLite["intents"]>[number]): IntentCard => ({
    intentId: it.id,
    label: it.label || "(未命名意图)",
    keywords: it.keywords ?? [],
    enabled: it.enabled !== false,
    judge: Boolean(it.judge?.prompt),
    bindings: bindings
      .filter((b) => b.intent === it.id && b.enabled !== false)
      .map((b) => ({
        bindingId: String(b.id ?? `${b.intent}:${b.action}:${b.qa_id ?? b.step ?? 0}`),
        action: String(b.action ?? ""),
        step: b.step,
        qaId: b.qa_id,
        qaLabel: b.qa_id ? String(opts?.qaLabels?.[b.qa_id] ?? "") : "",
        thenJump: b.then_jump,
      })),
  });
  const scopedIntents: IntentCard[] = [];
  const globalIntents: IntentCard[] = [];
  for (const it of intents) {
    const scope = it.steps ?? [];
    // 分桶三分：steps 空=全程（横条桶,不铺节点）;非空含本步=铺卡;非空不含本步=本画布不出现
    //（换步再看——旧三元把全程装进 scoped、别步装进 global,两支全反,子代理实测抓出后修正）。
    if (scope.length === 0) globalIntents.push(toCard(it));
    else if (scope.includes(n)) scopedIntents.push(toCard(it));
  }

  // 目标列：其余各步 + 收线 + 打铃。usedBy 先记账再回填（边 id 与 layout 同规则）。
  const chips: TargetChip[] = [];
  steps.forEach((s, j) => {
    if (j === i) return;
    chips.push({
      id: `sc:tgt:step:${j + 1}`,
      kind: "step",
      stepNo: j + 1,
      label: `第${j + 1}步 · ${String(s.goal ?? "").trim() || "(未写目的)"}`,
      usedBy: [],
    });
  });
  chips.push(
    { id: "sc:tgt:refuse", kind: "refuse", label: "收线", usedBy: [] },
    { id: "sc:tgt:bell", kind: "bell", label: "转人工 / 打铃", usedBy: [] },
  );
  const chipById = new Map(chips.map((c) => [c.id, c]));
  const touch = (id: string, edgeId: string) => {
    const c = chipById.get(id);
    if (c) c.usedBy.push(edgeId);
  };
  anchors.forEach((a) => {
    const edgeId = `sc:br:${a.branchIndex}`;
    if (a.action === "refuse") touch("sc:tgt:refuse", edgeId);
    else if (a.action === "handoff") touch("sc:tgt:bell", edgeId);
    else if (a.action === "jump" && a.jump >= 1 && a.jump <= steps.length && a.jump !== n) {
      touch(`sc:tgt:step:${a.jump}`, edgeId);
    }
  });
  // usedBy 只记 scoped 意图的绑定——全程意图不铺卡不画线,记账会造成「高亮无线」假象。
  for (const card of scopedIntents) {
    for (const b of card.bindings) {
      const edgeId = `sc:ib:${b.bindingId}`;
      if (b.action === "jump_step" && Number(b.step) >= 1 && Number(b.step) <= steps.length) {
        touch(`sc:tgt:step:${Number(b.step)}`, edgeId);
      } else if (b.action === "notify_human") {
        touch("sc:tgt:bell", edgeId);
      }
    }
  }

  const closing = n === steps.length;
  const ghost: GhostFlow = closing
    ? { targetStepNo: null, closing: true, label: "末步讲完自动收线（引擎固有）" }
    : { targetStepNo: n + 1, closing: false, label: `默认推进 → 第${n + 1}步（引擎固有，不可编辑）` };

  return {
    stepNo: n,
    index: i,
    goal: String(st.goal ?? ""),
    scriptFirst: stepPreviewLine(st.ref ?? ""),
    say: (st as CanvasFlowStep).say === true,
    emotion: String((st as CanvasFlowStep).emotion ?? ""),
    scene: String((st as CanvasFlowStep).scene ?? "").trim(),
    anchors,
    scopedIntents,
    globalIntents,
    chips,
    ghost,
  };
}

/**
 * 宇宙 → 固定三列布局（左触发/中步/右目标）。确定性纯函数;坐标是渲染参数不是数据。
 * 边规则：分支线只画有动作的锚点（无标记/留本步=答完照常推进,不画线——「连线=偏离」）;
 * 意图线画 jump_step→步 chip / notify_human→打铃;play_qa→快答徽章节点（词条摘要）;
 * ghost 虚线只读（下一步 chip / 末步→收线 chip）。
 */
export function layoutStepUniverse(u: StepUniverse): { nodes: ScNode[]; edges: ScEdge[] } {
  const nodes: ScNode[] = [];
  const edges: ScEdge[] = [];
  // 目标在场表：jump 类边（分支标记/意图绑定）的步号可能越界（手写【跳第99步】/旧数据）,
  // chip 不在就不画线——防 RF 悬挂边指向不存在节点（子代理审查指出,2026-09-26 补闸）。
  const chipIds = new Set(u.chips.map((c) => c.id));

  nodes.push({
    id: "sc:step",
    kind: "step",
    x: SC_STEP_X,
    y: SC_TOP_Y + 40,
    stepNo: u.stepNo,
    goal: u.goal,
    scriptFirst: u.scriptFirst,
    say: u.say,
    emotion: u.emotion,
    scene: u.scene,
    anchors: u.anchors,
  });

  // 左列：意图卡纵向堆叠,每张 play_qa 绑定在其卡下挂一枚快答徽章节点。
  let ly = SC_TOP_Y;
  for (const card of u.scopedIntents) {
    nodes.push({ id: `sc:int:${card.intentId}`, kind: "intent", x: SC_INTENT_COL_X, y: ly, card });
    ly += SC_INTENT_CARD_H;
    for (const b of card.bindings.filter((x) => x.action === "play_qa")) {
      nodes.push({
        id: `sc:qa:${b.bindingId}`,
        kind: "qabadge",
        x: SC_INTENT_COL_X + 24,
        y: ly,
        bindingId: b.bindingId,
        qaId: String(b.qaId ?? ""),
        label: b.qaLabel ? `快答：${b.qaLabel}` : "快答：（词条缺失）",
      });
      edges.push({
        id: `sc:ib:${b.bindingId}`,
        source: `sc:int:${card.intentId}`,
        sourceHandle: "out",
        target: `sc:qa:${b.bindingId}`,
        targetHandle: "in",
        kind: "intent",
        label: "播快答",
        intentId: card.intentId,
        bindingId: b.bindingId,
        action: b.action,
      });
      ly += SC_QA_BADGE_H;
    }
    ly += SC_INTENT_GAP;
    // 意图绑定线：jump_step→步 chip;notify_human→打铃。
    for (const b of card.bindings) {
      if (b.action === "jump_step" && Number(b.step) >= 1 && Number(b.step) !== u.stepNo && chipIds.has(`sc:tgt:step:${Number(b.step)}`)) {
        edges.push({
          id: `sc:ib:${b.bindingId}`,
          source: `sc:int:${card.intentId}`,
          sourceHandle: "out",
          target: `sc:tgt:step:${Number(b.step)}`,
          targetHandle: "in",
          kind: "intent",
          label: card.label,
          intentId: card.intentId,
          bindingId: b.bindingId,
          action: b.action,
        });
      } else if (b.action === "notify_human") {
        edges.push({
          id: `sc:ib:${b.bindingId}`,
          source: `sc:int:${card.intentId}`,
          sourceHandle: "out",
          target: "sc:tgt:bell",
          targetHandle: "in",
          kind: "intent",
          label: `${card.label}（打铃）`,
          intentId: card.intentId,
          bindingId: b.bindingId,
          action: b.action,
        });
      }
    }
  }

  // 右列：固定终点在前,步 chip 随后。
  let ry = SC_TOP_Y;
  for (const chip of u.chips) {
    nodes.push({ id: chip.id, kind: "chip", x: SC_TARGET_COL_X, y: ry, chip });
    ry += SC_CHIP_H + SC_CHIP_GAP;
  }

  // 分支锚点线（有动作才画;hold/无标记不画——「留本步」是停留不是移动）。
  for (const a of u.anchors) {
    if (a.action === "refuse") {
      edges.push({
        id: `sc:br:${a.branchIndex}`,
        source: "sc:step",
        sourceHandle: `a${a.branchIndex}`,
        target: "sc:tgt:refuse",
        targetHandle: "in",
        kind: "branch",
        label: a.cond || "(空条件)",
        branchIndex: a.branchIndex,
      });
    } else if (a.action === "handoff") {
      edges.push({
        id: `sc:br:${a.branchIndex}`,
        source: "sc:step",
        sourceHandle: `a${a.branchIndex}`,
        target: "sc:tgt:bell",
        targetHandle: "in",
        kind: "branch",
        label: a.cond || "(空条件)",
        branchIndex: a.branchIndex,
      });
    } else if (a.action === "jump" && a.jump >= 1 && a.jump !== u.stepNo && chipIds.has(`sc:tgt:step:${a.jump}`)) {
      edges.push({
        id: `sc:br:${a.branchIndex}`,
        source: "sc:step",
        sourceHandle: `a${a.branchIndex}`,
        target: `sc:tgt:step:${a.jump}`,
        targetHandle: "in",
        kind: "branch",
        label: a.cond || "(空条件)",
        branchIndex: a.branchIndex,
      });
    }
  }

  // 默认推进虚线（只读;末步→收线 chip）。
  edges.push({
    id: "sc:ghost",
    source: "sc:step",
    sourceHandle: "ghost",
    target: u.ghost.closing ? "sc:tgt:refuse" : `sc:tgt:step:${u.ghost.targetStepNo}`,
    targetHandle: "in",
    kind: "ghost",
    label: u.ghost.label,
  });

  return { nodes, edges };
}

// —— 步列（导航 rail）——

export type RailItem = {
  stepNo: number; // 1-based
  goal: string;
  say: boolean;
  scene: string;
  /** 分支数（「客户这样说」应对条数）。 */
  branchCount: number;
  /** 作用域含该步的意图数（scoped 口径,全程意图不计——每步都一样）。 */
  intentCount: number;
};

/** 步列派生（rail 渲染唯一数据源;graph 缺省=空图）。 */
export function buildRail(steps: CanvasFlowStep[], graph: GraphLite | null | undefined): RailItem[] {
  const intents = graph?.intents ?? [];
  return steps.map((s, i) => ({
    stepNo: i + 1,
    goal: String(s.goal ?? "").trim(),
    say: s.say === true,
    scene: String(s.scene ?? "").trim(),
    branchCount: parseStepRefParts(s.ref ?? "").branches.length,
    intentCount: intents.filter((it) => {
      const scope = it.steps ?? [];
      return scope.length > 0 && scope.includes(i + 1);
    }).length,
  }));
}

// —— 锚点连线写回（分支动作标记进 ref 行;round-trip 无损依赖 flow-canvas） ——

/**
 * 设第 stepNo 步第 branchIndex 条分支的动作。无效寻址（步号/分支下标越界）原样返回
 * 输入数组（幂等防御,不炸）。jump 步号钳 1..步数（画布 chip 只出合法目标,这里兜底）。
 * 其余步/其余行（正稿/注意/未知行/箭头分隔）逐字节保留——serialize round-trip 纪律。
 */
export function setStepBranchAction(
  steps: CanvasFlowStep[],
  stepNo: number,
  branchIndex: number,
  action: import("./flow-canvas").BranchAction,
  jumpStep?: number,
): CanvasFlowStep[] {
  const si = Number(stepNo);
  const i = Number.isInteger(si) ? si - 1 : -1;
  if (!Array.isArray(steps) || i < 0 || i >= steps.length) return steps;
  const bi = Number(branchIndex);
  if (!Number.isInteger(bi) || bi < 0) return steps;
  const parts = parseStepRefParts(steps[i].ref ?? "");
  const b = parts.branches[bi];
  if (!b) return steps;
  const text = parseBranchAction(b.resp).text; // 剥旧标记,只换动作不丢应答文字
  const max = Math.max(1, steps.length);
  const n =
    action === "jump"
      ? Math.min(Math.max(Math.round(Number(jumpStep) || 1), 1), max)
      : 0;
  parts.branches[bi] = { ...b, resp: composeBranchResp(action, n, text) };
  return steps.map((s, j) => (j === i ? { ...s, ref: serializeStepRef(parts) } : s));
}

/** 解除某分支动作（=setStepBranchAction(…, "")——分支保留为普通答法,文字不丢）。 */
export function clearStepBranchAction(
  steps: CanvasFlowStep[],
  stepNo: number,
  branchIndex: number,
): CanvasFlowStep[] {
  return setStepBranchAction(steps, stepNo, branchIndex, "");
}
