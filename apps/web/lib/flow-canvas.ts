// 流程画布纯函数（W2 T2）：step.ref 三件拆装、场景泳道分桶、确定性布局。
//
// parseStepRefParts/serializeStepRef 语义镜像 agent 侧运行时解析器
// apps/agent/agent_runtime/flow.py:95-149（_BRANCH_LINE_RE/_NOTE_LINE_RE/parse_step_ref）：
//   分支行  = 行头「如果客户」（或 EN「If/When the customer」,大小写不敏感）+ 条件 + 「→」 + 应答；
//             条件=锚词与箭头之间（1..120 字,非贪婪）,应答=箭头后首个非空白字符起;
//             箭头两侧空白容差（\s*→\s*）。
//   注意行  = 行头「注意」/「Note(s)」+ 冒号（全半角皆可）。
//   正稿    = 首个非分支非注意非空行;其余非分支非注意行并入正稿段（保守——flow.py 对
//             未知形态行只告警并从注入中丢弃,画布侧原样保留进 script,序列化时原样回写,
//             round-trip 无损：解析不了的行不丢,运营在抽屉里看得见、改得了）。
//
// 本文件必须保持零 import（test/flow-canvas.test.mjs 按 qa-canvas.test.mjs 同款装配,
// tsc 单文件转译直载——引 template-editor/react 会拖进 "@/" 别名与组件依赖炸掉转译）,
// 类型自持,结构上与 components/template-editor 的 FlowStep（+scene）兼容。

/** 画布消费的步骤形状（template-editor.FlowStep 超集兼容面;scene 画布专属）。 */
export type CanvasFlowStep = {
  goal: string;
  ref: string;
  say?: boolean;
  emotion?: string;
  scene?: string;
};

export type StepBranch = { cond: string; resp: string };

export type StepRefParts = {
  /** 正稿 + 并入的其余非分支非注意行（\n 连接,保序,未知行原样）。 */
  script: string;
  branches: StepBranch[];
  /** 注意行内容（多条 \n 连接,序列化时逐行还原「注意：」头）。 */
  notes: string;
};

// flow.py:96 `_BRANCH_LINE_RE` 的逐语义移植：锚词 | 条件(1..120 非贪婪) | \s*→\s* | 应答(\S.*)。
// 捕获组 1=锚词原文（EN 大小写变体原样保留,序列化按原文回写,不做 EN→中改写）。
const BRANCH_RE = /^(如果客户|(?:If|When)\s+the\s+customer)\s*(.{1,120}?)\s*→\s*(\S.*)$/i;
// flow.py:99 `_NOTE_LINE_RE` 的逐语义移植。
const NOTE_RE = /^(?:注意|Notes?)\s*[:：]\s*(.+)$/i;

/** 拆 step.ref 为 正稿/分支/注意 三件（纯函数,变量不渲染——与 flow.py:130 parse_step_ref 同姿态）。 */
export function parseStepRefParts(ref: string): StepRefParts {
  const parts: StepRefParts = { script: "", branches: [], notes: "" };
  const scriptLines: string[] = [];
  const noteLines: string[] = [];
  for (const raw of String(ref ?? "").split(/\r?\n/)) {
    const line = raw.trim(); // flow.py: line = raw.strip()
    if (!line) continue; // 空行跳过（flow.py:135-136）
    const bm = BRANCH_RE.exec(line);
    if (bm) {
      parts.branches.push({ cond: bm[2].trim(), resp: bm[3].trim() });
      continue;
    }
    const nm = NOTE_RE.exec(line);
    if (nm) {
      noteLines.push(nm[1].trim());
      continue;
    }
    // 首个非分支非注意非空行=正稿,其余（含带「→」的未知指令行）原样并入正稿段——
    // flow.py:147 对未知行只 _warn 后丢弃,画布侧保留=运营不丟字（任务书硬约束）。
    scriptLines.push(line);
  }
  parts.script = scriptLines.join("\n");
  parts.notes = noteLines.join("\n");
  return parts;
}

/** 与 parseStepRefParts 互逆：正稿行 + 每分支 `如果客户{cond}→{resp}` + 每注意行 `注意：{n}`。
 * 不变量：parse(serialize(parse(x))) deepEqual parse(x)（分支/注意/正稿逐件相等）。
 * 刻意偏差（对任务书 `→就{resp}` 字样）：不无条件补「就」——resp 是运行时逐字注入的
 * 应答内容（flow.py:96 从箭头后捕获、flow.py:1195-1197 原样注入 prompt）,现存语料分支
 * 普遍无「就」头（见 template-editor STEPS_EXAMPLES 三语样例）,补字=保存即改写运营内容,
 * 破坏无损往返;作者写了「就」的自然带出。EN 锚词分支序列化为规范中文锚（与任务书规范形一致,
 * 运行时两种锚等价）。解析不了的行（在 script 里）原样回写。 */
export function serializeStepRef(parts: StepRefParts): string {
  const lines: string[] = [];
  for (const l of String(parts.script ?? "").split("\n")) {
    const t = l.trim();
    if (t) lines.push(t); // 保留行原样回写（空行不产）
  }
  for (const b of parts.branches ?? []) {
    const cond = String(b.cond ?? "").trim();
    const resp = String(b.resp ?? "").trim();
    if (cond && resp) {
      lines.push(`如果客户${cond}→${resp}`);
    } else if (cond || resp) {
      // 残行（缺条件或缺应答）不成合法分支语法,降级为普通行——文字不丢,
      // 重解析落回 script 段,抽屉里仍可见可改。
      lines.push(cond || resp);
    }
    // 全空行（抽屉里点了「加分支」没填）不产垃圾。
  }
  for (const n of String(parts.notes ?? "").split("\n")) {
    const t = n.trim();
    if (t) lines.push(`注意：${t}`); // 多条注意行逐行还原「注意：」头
  }
  return lines.join("\n");
}

/** 运行时会忽略的「未知指令行」：含「→」但既非合法分支行、也非注意行的非空行
 * （例：「客户报出号码(数字串)→复述确认」——运行时 parse_step_ref 告警一次后把
 * 该行从注入中丢弃）。画布侧把这些行原样保留进正稿（不丟字），但「画布上看得见」
 * ≠「引擎会执行」——抽屉按本函数出警示（2026-09-19 审计 W3-17；0913 EN 模板
 * 分支整层静默失效同族）。 */
export function findUnparsedDirectives(ref: string): string[] {
  const hits: string[] = [];
  for (const raw of String(ref ?? "").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    if (line.includes("→") && !BRANCH_RE.test(line) && !NOTE_RE.test(line)) {
      hits.push(line);
    }
  }
  return hits;
}


/** 未分组泳道名（scene 空/缺失的步归此;保持出现位置,不殿后）。 */
export const UNGROUPED_LANE = "未分组";

export type SceneLane = { name: string; steps: CanvasFlowStep[] };

/** 场景泳道分桶：按出现顺序,同 scene 归并到首次出现的那条泳道（scene 空=「未分组」）。
 * 同名归并（而非严格相邻切段）=泳道名唯一,行内改名 onChangeScene(prev,new) 才有唯一落点;
 * 常规数据同场景步本就相邻,交错只是退化输入的确定序。 */
export function sceneLanes(steps: CanvasFlowStep[]): SceneLane[] {
  const lanes: SceneLane[] = [];
  const byName = new Map<string, SceneLane>();
  for (const s of steps ?? []) {
    const name = s.scene ? s.scene : UNGROUPED_LANE;
    let lane = byName.get(name);
    if (!lane) {
      lane = { name, steps: [] };
      byName.set(name, lane);
      lanes.push(lane);
    }
    lane.steps.push(s);
  }
  return lanes;
}

// —— 布局（确定性网格,同输入同输出）：泳道纵向排布、步节点脊柱、意图节点侧栏列 ——
export const LANE_HEADER_X = 0; // 泳道头列 x（场景名）
export const LANE_HEADER_W = 180; // 泳道头节点宽（给行内改名输入留位）
export const STEP_COL_X = 260; // 步节点脊柱列 x
export const INTENT_COL_X = 700; // 意图节点侧栏列 x
export const STEP_GAP_Y = 170; // 泳道内步节点纵向节距
export const LANE_GAP_Y = 100; // 泳道间留白
export const LANE_TOP_Y = 40; // 首条泳道 y
export const INTENT_STACK_Y = 130; // 同锚点多个意图的纵向堆叠节距

export const STEP_NODE_W = 260;
export const INTENT_NODE_W = 240;

/** 意图/绑定的只读消费面（结构兼容 lib/qa-canvas GraphDoc,组件直传 parseGraphDoc 结果）。 */
export type GraphLite = {
  intents?: {
    id: string;
    label?: string;
    keywords?: string[];
    steps?: number[];
    enabled?: boolean;
    judge?: { prompt?: string };
  }[];
  bindings?: {
    id?: string;
    intent: string;
    action?: string;
    qa_id?: string;
    step?: number;
    enabled?: boolean;
  }[];
};

/** 画布节点（kind 判别：lane/step/intent）。位置为纯几何派生,拖拽覆盖在组件层。 */
export type FlowNode =
  | { id: string; kind: "lane"; x: number; y: number; name: string }
  | {
      id: string;
      kind: "step";
      x: number;
      y: number;
      /** steps 数组内的全局下标（抽屉/保存按它回写）。 */
      index: number;
      goal: string;
      scriptFirst: string;
      say: boolean;
      emotion: string;
      /** jump_step 绑定入边数（徽标）。 */
      jumpIn: number;
    }
  | {
      id: string;
      kind: "intent";
      x: number;
      y: number;
      intentId: string;
      label: string;
      keywords: string[];
      enabled: boolean;
      judge: boolean;
      playQa: boolean;
    };

export type FlowEdge = {
  id: string;
  source: string;
  target: string;
  /** spine=泳道内步间顺序线;jump=意图→目标步（jump_step 绑定）。 */
  kind: "spine" | "jump";
};

/** 泳道纵向布局：每条泳道=泳道头（左）+ 步节点脊柱（中）;意图锚定首个 jump_step 目标步
 * （无绑定锚首步）纵向堆叠在右列。jump_step 悬空（步号越界/意图缺失/停用绑定）不画边不计数。 */
export function layoutFlow(
  steps: CanvasFlowStep[],
  graph?: GraphLite,
): { nodes: FlowNode[]; edges: FlowEdge[] } {
  const nodes: FlowNode[] = [];
  const edges: FlowEdge[] = [];
  // 步节点 id 用全局下标（fstep:<i>）,泳道只影响 y——抽屉/意图目标都按全局下标寻址。
  const stepId = (i: number) => `fstep:${i}`;
  const intentId = (id: string) => `fintent:${id}`;

  let y = LANE_TOP_Y;
  const stepY = new Map<number, number>();
  const idxOf = new Map<CanvasFlowStep, number>(steps.map((s, i) => [s, i]));
  // 泳道 id 用稳定序号（非名字派生）——行内改名逐键重映射 scene,名字派生 id 会令
  // 泳道头输入框每键重挂丢焦点。
  sceneLanes(steps).forEach((lane, li) => {
    nodes.push({ id: `lane:${li}`, kind: "lane", x: LANE_HEADER_X, y, name: lane.name });
    lane.steps.forEach((s, j) => {
      stepY.set(idxOf.get(s) ?? 0, y + j * STEP_GAP_Y);
    });
    y += Math.max(lane.steps.length, 1) * STEP_GAP_Y + LANE_GAP_Y;
  });
  steps.forEach((s, i) => {
    const parts = parseStepRefParts(s.ref);
    // 节点预览：正稿首行;无正稿（纯分支 ref）退首分支行,画布上仍有可读摘要。
    const scriptFirst = (parts.script.split("\n")[0] ?? "")
      || (parts.branches[0] ? `如果客户${parts.branches[0].cond}→${parts.branches[0].resp}` : "");
    nodes.push({
      id: stepId(i),
      kind: "step",
      x: STEP_COL_X,
      y: stepY.get(i) ?? LANE_TOP_Y,
      index: i,
      goal: s.goal || "",
      scriptFirst: scriptFirst.slice(0, 40),
      say: s.say === true,
      emotion: s.emotion ?? "",
      jumpIn: 0,
    });
  });
  // 泳道内步间顺序线（展示性,读流程走向）。
  for (const lane of sceneLanes(steps)) {
    for (let j = 0; j + 1 < lane.steps.length; j++) {
      const a = idxOf.get(lane.steps[j]) ?? 0;
      const b = idxOf.get(lane.steps[j + 1]) ?? 0;
      edges.push({ id: `spine:${a}:${b}`, source: stepId(a), target: stepId(b), kind: "spine" });
    }
  }
  // 意图节点 + jump_step 绑定边（只读 overlay,编辑深链问答画布）。
  const intents = graph?.intents ?? [];
  const byId = new Map(intents.map((it) => [it.id, it]));
  const stackAt = new Map<number, number>();
  const intentAnchor = new Map<string, number>();
  const jumpBindings = (graph?.bindings ?? []).filter(
    (b) => b.enabled !== false && b.action === "jump_step" && byId.has(b.intent),
  );
  for (const b of jumpBindings) {
    const idx = Math.min(Math.max((Number(b.step) || 1) - 1, 0), Math.max(steps.length - 1, 0));
    intentAnchor.set(b.intent, idx);
    const node = nodes.find((n) => n.id === stepId(idx));
    if (node && node.kind === "step") node.jumpIn += 1;
  }
  for (const it of intents) {
    const anchorIdx =
      intentAnchor.get(it.id) ??
      (steps.length > 0
        ? Math.min(Math.max((Number(it.steps?.[0]) || 1) - 1, 0), steps.length - 1)
        : 0);
    const stack = stackAt.get(anchorIdx) ?? 0;
    stackAt.set(anchorIdx, stack + INTENT_STACK_Y);
    nodes.push({
      id: intentId(it.id),
      kind: "intent",
      x: INTENT_COL_X,
      y: (stepY.get(anchorIdx) ?? LANE_TOP_Y) + stack,
      intentId: it.id,
      label: it.label || "(未命名意图)",
      keywords: it.keywords ?? [],
      enabled: it.enabled !== false,
      judge: Boolean(it.judge?.prompt),
      playQa: (graph?.bindings ?? []).some(
        (b) => b.intent === it.id && b.enabled !== false && b.action === "play_qa",
      ),
    });
  }
  for (const b of jumpBindings) {
    const idx = Math.min(Math.max((Number(b.step) || 1) - 1, 0), Math.max(steps.length - 1, 0));
    edges.push({ id: `jump:${b.id ?? `${b.intent}:${idx}`}`, source: intentId(b.intent), target: stepId(idx), kind: "jump" });
  }
  return { nodes, edges };
}
