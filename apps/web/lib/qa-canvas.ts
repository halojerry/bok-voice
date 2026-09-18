// QA 画布纯函数（qa-canvas Phase 1 Task 4，spec §4.3/§4.4）：解析 steps、派生节点/边、
// 簇校验、布局契约。Task 5/6/7 的画布组件唯一数据面——函数签名与返回形态勿动。
// 布局契约（archify 原则）：步骤脊柱=唯一主路径；QA 簇=最近步骤列的星形侧分支
// （head 行首、变体缩进挂其右下）；确定性网格（同输入同输出，无随机/时间源）；
// 语义边标签（cluster=变体归属，step=跳步入口）；位置覆盖走 localStorage（LOCAL_POS_KEY）。

export type QaRow = {
  id: string;
  owner_user_id?: string;
  question_text?: string;
  answer_text?: string;
  lang?: string;
  scope?: string;
  step_index?: number;
  voice_id?: string;
  enabled?: boolean;
  hit_count?: number;
  source?: string;
  cluster_head_id?: string;
  /** 匹配优先级（Phase 3.1）：小者先，默认 10；≠10 时画布出徽标。 */
  priority?: number;
  created_at?: string;
};

export type FlowStep = { goal: string; ref: string };
export type Pt = { x: number; y: number };

// 三类节点 data 均带 `kind` 判别面（step/qaEntry/intent），CanvasNode 联合因此可判别。
export type CanvasStepNode = {
  id: string; type: "qaStep";
  position: Pt;
  data: { kind: "step"; index: number; goal: string; refFirstLine: string; virtual: boolean };
};
export type CanvasQaNode = {
  id: string; type: "qaEntry";
  position: Pt;
  data: QaRow & { kind: "qaEntry"; isHead: boolean; canned?: "ok" | "missing" };
};
export type CanvasEdge = {
  id: string;
  source: string; target: string;
  sourceHandle: string | null; targetHandle: string | null;
  data: { kind: "cluster" | "step" | "spine" | "binding" };
  animated?: boolean;
  label?: string;
};

// —— 话术图 Phase 2（spec §7，2026-09-18）：模板可选携带 graph_json = 意图节点 + 绑定边 ——
// 运行时消费方=agent FlowController（Task 4）；此处只做画布展示派生（Task 7/8 接线交互）。
export interface GraphIntent {
  id: string;
  label: string;
  keywords: string[];
  steps: number[]; // 1-based；空=全程
  enabled: boolean;
}
export interface GraphBinding {
  id: string;
  intent: string;
  action: "play_qa" | "jump_step";
  qa_id?: string;
  step?: number;
  priority: number;
  once: boolean;
  enabled: boolean;
}
export interface GraphDoc {
  version: number;
  intents: GraphIntent[];
  bindings: GraphBinding[];
}

/** 宽容解析（spec §7）：坏 JSON/版本不符/缺数组 → 空 doc，永不抛错。 */
export function parseGraphDoc(raw: string | null | undefined): GraphDoc {
  const empty: GraphDoc = { version: 1, intents: [], bindings: [] };
  const text = String(raw || "").trim();
  if (!text) return empty;
  try {
    const data = JSON.parse(text) as Partial<GraphDoc>;
    if (!data || data.version !== 1 || !Array.isArray(data.intents) || !Array.isArray(data.bindings)) return empty;
    return { version: 1, intents: data.intents, bindings: data.bindings };
  } catch {
    return empty;
  }
}

export type CanvasIntentNode = {
  id: string; type: "intent";
  position: Pt;
  data: { kind: "intent"; intent: GraphIntent };
};
/** 三类画布的判别联合（`deriveGraph().nodes` 的组合形态；qaNodes/stepNodes 子集保留兼容）。 */
export type CanvasNode = CanvasStepNode | CanvasQaNode | CanvasIntentNode;

// 布局几何 v1.1（用户实测反馈「画面太挤、没读出关联」后重排）：
// 脊柱=左侧流程主线（步骤间顺序连线读出走向）；卫星道外推拉开大走廊；
// global 泳道甩到脊柱左侧，与步骤卫星彻底分居；簇间留白防止扎堆墙。
export const SPINE_X = 0;          // 步骤脊柱列 x
export const STEP_GAP_Y = 280;     // 相邻步骤节点的垂直间距
export const SAT_X = 620;          // 卫星道距脊柱的水平走廊（给步骤边标签留位）
export const ENTRY_PITCH_Y = 180;  // 条目纵向节距
export const CLUSTER_GAP_Y = 80;   // 簇边界（非变体条目入列前）的额外留白
export const WRAP_AFTER = 10;      // 每条卫星子道的条目容量，超出换下一子道
export const LANE_X = 320;         // 子道水平间距
export const GLOBAL_X = -560;      // global（全程通用）泳道 x：脊柱左侧，分居减密度
export const INTENT_X = -280;       // 意图节点道：脊柱左侧、全程通用泳道（-560）之右
export const INTENT_STACK_Y = 140;  // 同锚点多个意图的纵向堆叠节距

export function parseTemplateSteps(stepsJson: string): FlowStep[] {
  try {
    const raw = JSON.parse(stepsJson || "[]");
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
}

export function LOCAL_POS_KEY(accountId: string, templateId: string): string {
  return `qa-canvas-pos:${accountId}:${templateId}`;
}

/** 列 x 坐标：0=通用/未挂步，1..N=步骤。 */
function colOf(row: QaRow, stepCount: number): number {
  if (String(row.scope ?? "") === "step") {
    const idx = Number(row.step_index ?? -1);
    if (idx >= 0 && idx < stepCount) return idx + 1;
  }
  return 0;
}

export function deriveGraph(
  rows: QaRow[],
  steps: FlowStep[],
  // langFilter/positions 都是可选覆盖：缺省=按语言全量 + 纯确定性布局（纯函数层不假设
  // 调用方已备好覆盖表——既有调用传 positions，Phase 2 用例只传 `{ graph }`）。
  opts: { langFilter: string; positions?: Record<string, Pt>; graph?: GraphDoc },
): { nodes: CanvasNode[]; qaNodes: CanvasQaNode[]; stepNodes: CanvasStepNode[]; edges: CanvasEdge[] } {
  const filtered = (rows ?? []).filter(
    (r) => opts.langFilter === "all" || String(r.lang ?? "zh") === opts.langFilter,
  );
  const stepNodes: CanvasStepNode[] = steps.map((s, i) => ({
    id: `step:${i}`,
    type: "qaStep" as const,
    position: { x: SPINE_X, y: 80 + i * STEP_GAP_Y },
    data: {
      kind: "step",
      index: i,
      goal: String(s.goal || ""),
      refFirstLine: String(s.ref || "").split("\n")[0].slice(0, 40),
      virtual: false,
    },
  }));
  // 虚拟「全程通用」节点在脊柱顶端；其卫星在左侧独立泳道（GLOBAL_X）。
  stepNodes.unshift({
    id: "step:global",
    type: "qaStep" as const,
    position: { x: SPINE_X, y: 0 },
    data: { kind: "step", index: -1, goal: "全程通用", refFirstLine: "不挂步骤的条目归此列", virtual: true },
  });

  const heads = new Set(filtered.map((r) => String(r.cluster_head_id || "")).filter(Boolean));
  const qaNodes: CanvasQaNode[] = [];
  // 每列游标：nextY=下一个条目的 y；lane=子道序号；count=已排条目数。
  const colState = new Map<number, { nextY: number; lane: number; count: number }>();
  for (const r of filtered) {
    const col = colOf(r, steps.length);
    const st = colState.get(col) ?? { nextY: col === 0 ? 0 : 80, lane: 0, count: 0 };
    const isHead = heads.has(String(r.id));
    const headId = String(r.cluster_head_id || "");
    // 星形侧分支：head 行首，变体缩进挂其右下；簇边界（非变体条目入列）加留白。
    const indent = headId ? 140 : 0;
    let y = st.nextY;
    if (st.count > 0 && !headId) y += CLUSTER_GAP_Y;
    // 容量换道：每 WRAP_AFTER 条换一条子道（确定性，纯几何）。
    const lane = Math.floor(st.count / WRAP_AFTER);
    const anchorX = col === 0 ? GLOBAL_X : SPINE_X + SAT_X;
    const pos = opts.positions?.[String(r.id)] ?? {
      x: anchorX + lane * LANE_X + indent,
      y,
    };
    colState.set(col, { nextY: y + ENTRY_PITCH_Y, lane, count: st.count + 1 });
    qaNodes.push({
      id: String(r.id),
      type: "qaEntry",
      position: pos,
      data: { ...r, kind: "qaEntry", isHead },
    });
  }
  // 意图节点（话术图 Phase 2）：锚定首个生效步（全程锚第 1 步），同锚点纵向堆叠；
  // 禁用意图照渲染（视图置灰）。位置优先取用户拖过的 localStorage 坐标。
  const intentNodes: CanvasIntentNode[] = [];
  const stackY = new Map<number, number>();
  for (const intent of opts.graph?.intents ?? []) {
    const stepIdx = Math.min(Math.max((intent.steps[0] ?? 1) - 1, 0), Math.max(steps.length - 1, 0));
    const baseY = stepNodes.find((n) => n.id === `step:${stepIdx}`)?.position.y ?? 0;
    const stack = stackY.get(stepIdx) ?? 0;
    stackY.set(stepIdx, stack + INTENT_STACK_Y);
    intentNodes.push({
      id: `intent:${intent.id}`,
      type: "intent" as const,
      position: opts.positions?.[`intent:${intent.id}`] ?? { x: INTENT_X, y: baseY + stack },
      data: { kind: "intent", intent },
    });
  }
  const edges: CanvasEdge[] = [];
  // 脊柱顺序连线（展示性：读出「第1步→第2步→…」流程走向；不可删、右键不响应）。
  for (let i = 0; i + 1 < steps.length; i++) {
    edges.push({
      id: `spine:${i}`,
      source: `step:${i}`, target: `step:${i + 1}`,
      sourceHandle: null, targetHandle: null,
      data: { kind: "spine" },
    });
  }
  for (const r of filtered) {
    const headId = String(r.cluster_head_id || "");
    if (headId && filtered.some((x) => String(x.id) === headId)) {
      edges.push({
        id: `c:${r.id}:${headId}`,
        source: String(r.id), target: headId,
        sourceHandle: null, targetHandle: null,
        data: { kind: "cluster" },
      });
    }
    if (String(r.scope ?? "") === "step") {
      const idx = Number(r.step_index ?? -1);
      if (idx >= 0 && idx < steps.length) {
        edges.push({
          id: `s:${r.id}:step:${idx}`,
          source: String(r.id), target: `step:${idx}`,
          sourceHandle: null, targetHandle: null,
          data: { kind: "step" },
          animated: false,
          label: `进入第 ${idx + 1} 步`,
        });
      }
    }
  }
  // 绑定边：意图 → 目标（play_qa→QA 条目节点 / jump_step→步骤节点）。
  // 悬空引用不画（条目已删/步号越界），kind=binding，label=意图 label 退首关键词。
  const nodeIds = new Set([...stepNodes, ...qaNodes, ...intentNodes].map((n) => n.id));
  for (const b of opts.graph?.bindings ?? []) {
    if (!b.enabled) continue;
    const src = `intent:${b.intent}`;
    const tgt = b.action === "jump_step"
      ? `step:${Math.min(Math.max((b.step ?? 1) - 1, 0), Math.max(steps.length - 1, 0))}`
      : String(b.qa_id || "");
    if (!nodeIds.has(src) || !tgt || !nodeIds.has(tgt)) continue;
    const intent = (opts.graph?.intents ?? []).find((i) => i.id === b.intent);
    edges.push({
      id: `bind:${b.id}`,
      source: src,
      target: tgt,
      sourceHandle: null,
      targetHandle: null,
      label: intent?.label || intent?.keywords?.[0] || "",
      data: { kind: "binding" },
    });
  }
  return { nodes: [...stepNodes, ...qaNodes, ...intentNodes], qaNodes, stepNodes, edges };
}

/**
 * 连簇校验（spec §4.4）：一层星形；目标是变体→重定向其 head；自连拒绝。
 * source 侧：已带变体的主条目整类拒绝（H1→H2 / H1→V2 / H1→S 都会令其变体链
 * V1→H1→H2 变两层）——一层星形是数据不变量，CP 侧零校验后盾，纯函数层把死。
 * target 侧：主条目挂到自己簇内变体被 targetHead===fromId 拦截。
 * 变体重挂自己已挂的 head=幂等成功（headId 仍返回该 head，调用方落库为 no-op）。
 * （brief 实现草稿的「两条目已在同一簇」分支会误杀幂等重挂，已按测试语义收窄。）
 */
export function resolveClusterTarget(
  rows: QaRow[], fromId: string, toId: string,
): { ok: boolean; headId: string; reason: string } {
  if (fromId === toId) return { ok: false, headId: "", reason: "不能连接到自己" };
  if (rows.some((x) => String(x.cluster_head_id || "") === fromId)) {
    return { ok: false, headId: "", reason: "主条目已带变体，先解除其变体再挂" };
  }
  const headOf = (id: string): string => {
    const r = rows.find((x) => String(x.id) === id);
    return String(r?.cluster_head_id || "") || id;
  };
  const targetHead = headOf(toId);
  if (targetHead === fromId) return { ok: false, headId: "", reason: "主条目不能挂到自己的变体" };
  return { ok: true, headId: targetHead, reason: "" };
}

/** PATCH 失败回滚:返回剔除乐观变更后的行集(spec §8:不保留脏边)。 */
export function revertCluster(rows: QaRow[], childId: string, prevHeadId: string): QaRow[] {
  return rows.map((r) => (String(r.id) === childId ? { ...r, cluster_head_id: prevHeadId } : r));
}
