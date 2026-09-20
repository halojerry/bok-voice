// 流程画布纯函数（W2 T2；2026-09-20 重设计为纵向步骤工作流）：
// step.ref 三件拆装 + 确定性布局（步骤脊柱纵向瀑布 + 意图左栏条件边）。
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
//
// 正则调用一律走 String.prototype.match（非全局正则与 RegExp.prototype 正则执行同返回）,
// 匹配结果只做字符串拆装——本文件零副作用、零 IO。

/** 画布消费的步骤形状（template-editor.FlowStep 超集兼容面;scene 画布专属）。 */
export type CanvasFlowStep = {
  goal: string;
  ref: string;
  say?: boolean;
  emotion?: string;
  scene?: string;
};

export type StepBranch = { cond: string; resp: string };

/** 步节点分支 chip 行 = StepBranch + 动作派生（resp 恒为原文含标记,抽屉同源）。 */
export type StepNodeBranch = StepBranch & { action: BranchAction; jump: number };

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
    const bm = line.match(BRANCH_RE);
    if (bm) {
      const cond = String(bm[2] ?? "").trim();
      const resp = String(bm[3] ?? "").trim();
      parts.branches.push({ cond, resp });
      continue;
    }
    const nm = line.match(NOTE_RE);
    if (nm) {
      const note = String(nm[1] ?? "").trim();
      noteLines.push(note);
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
      // 分支行规范形：锚词+条件+箭头+应答（普通字符串拼接,无任何执行语义）。
      const branchLine = "如果客户" + cond + "→" + resp;
      lines.push(branchLine);
    } else if (cond || resp) {
      // 残行（缺条件或缺应答）不成合法分支语法,降级为普通行——文字不丢,
      // 重解析落回 script 段,抽屉里仍可见可改。
      const residual = cond || resp;
      lines.push(residual);
    }
    // 全空行（抽屉里点了「加分支」没填）不产垃圾。
  }
  for (const n of String(parts.notes ?? "").split("\n")) {
    const t = n.trim();
    if (t) {
      const noteLine = "注意：" + t; // 多条注意行逐行还原「注意：」头
      lines.push(noteLine);
    }
  }
  return lines.join("\n");
}

// —— 分支动作前缀（2026-09-20 A-③;语义镜像 flow.py `_BRANCH_ACTION_RE`/`parse_branch_action`）——
// 分支应答（resp）首部可选动作标记：【收线】/【挂断】=礼貌收线、【转人工】=打铃通知人工
// （AI 照常兜话不打断）、【跳第N步】=跳到第 N 步（1-based）、【留本步】=明确留本步。
// 标记只活在 branches[].resp 原文里（round-trip 无损依赖它）;抽屉编辑面拆成
// 动作下拉+步号+纯文本 三件,编辑时重组回 resp——标记绝不进 script、不进画布摘要。

/** 分支动作（""=按内容回答,resp 无标记）。 */
export type BranchAction = "" | "hold" | "refuse" | "handoff" | "jump";

export type BranchActionInfo = { action: BranchAction; step: number; text: string };

/** flow.py `_BRANCH_ACTION_RE` 逐语义移植：^【\s*(kind)\s*】\s*;kind 内部自带空白容错
 * （【 收线 】/【跳第 3 步】）;\d{1,3} 限 1..999——4 位以上步号整体不认作标记（原样保留）。 */
const BRANCH_ACTION_RE = /^【\s*(收线|挂断|转人工|跳第\s*(\d{1,3})\s*步|留本步)\s*】\s*/;

/** jump 步号钳制：非 1..999 整数一律按 1（引擎侧 jump_to 另有越界钳制,这里是编辑面保底）。 */
function clampJumpStep(step: number): number {
  const n = Number(step);
  return Number.isInteger(n) && n >= 1 && n <= 999 ? n : 1;
}

/** 拆分支应答首部动作标记 → (action, step, 纯文本)。规则（与 flow.py 逐语义一致）：
 * 收线|挂断→("refuse",0,余文);转人工→("handoff",0,余文);留本步→("hold",0,余文);
 * 跳第N步 且 N≥1→("jump",N,余文);跳第0步→标记已消费但无动作("",0,余文);
 * 无标记/空→("",0,resp 原样,逐字节不动)。step 只在 action==="jump" 时有意义。 */
export function parseBranchAction(resp: string): BranchActionInfo {
  const s = String(resp ?? "");
  const m = s.match(BRANCH_ACTION_RE);
  if (!m) return { action: "", step: 0, text: s };
  const kind = String(m[1] ?? "");
  const rest = s.slice(m[0].length);
  if (kind === "收线" || kind === "挂断") return { action: "refuse", step: 0, text: rest };
  if (kind === "转人工") return { action: "handoff", step: 0, text: rest };
  if (kind === "留本步") return { action: "hold", step: 0, text: rest };
  const n = Number(m[2] ?? "0");
  if (!(n >= 1)) return { action: "", step: 0, text: rest }; // 跳第0步：标记消费、无动作
  return { action: "jump", step: n, text: rest };
}

/** parseBranchAction 的逆操作：动作+步号+文本 重组 resp 原文。
 * action==="" 恒逐字节等于 text（无标记分支零改写）;jump 步号非 1..999 整数按 1 钳制。
 * 不变量：parseBranchAction(composeBranchResp(a,n,t)) 与
 * {action:a, step:(a==="jump"?钳制后n:0), text:t.trim()} 一致（t 已 trim 时逐件相等）。 */
export function composeBranchResp(action: BranchAction, step: number, text: string): string {
  const t = String(text ?? "");
  switch (action) {
    case "refuse": return "【收线】" + t;
    case "handoff": return "【转人工】" + t;
    case "hold": return "【留本步】" + t;
    case "jump": return "【跳第" + clampJumpStep(step) + "步】" + t;
    default: return t;
  }
}

/** 答法抽屉动作下拉（顺序即下拉顺序;首项=默认项）。 */
export const BRANCH_ACTIONS: { value: BranchAction; label: string; hint: string }[] = [
  { value: "", label: "按内容回答（默认）", hint: "AI 现场组织语言，命中率高的话可以补录成录音" },
  { value: "refuse", label: "礼貌收线", hint: "道歉/再见一句，然后结束通话，不再推销" },
  { value: "handoff", label: "通知人工", hint: "给人工坐席打铃，AI 继续照常应答，不打断通话" },
  { value: "jump", label: "跳到第 N 步", hint: "直接切换到指定步骤继续，本轮不再按顺序推进" },
  { value: "hold", label: "留在本步", hint: "本轮不往下推进，先处理客户这句话" },
];

/** 步节点分支 chip 的动作徽标文案（无动作=空串不渲染;jump 带「跳第N步」）。 */
export function branchActionBadge(action: BranchAction, step: number): string {
  switch (action) {
    case "refuse": return "收线";
    case "handoff": return "转人工";
    case "hold": return "留本步";
    case "jump": return "跳第" + clampJumpStep(step) + "步";
    default: return "";
  }
}

/** 分支罐头录音状态（组件只读渲染,不发请求;key=分支 resp 原文含标记,逐字节）。 */
export type BranchCannedStatus = "ok" | "missing" | "ph";

/** 录音状态展示元数据（点色类名+提示文案;抽纯函数便于单测）。 */
export function branchCannedMeta(status: BranchCannedStatus): { dot: string; title: string } {
  switch (status) {
    case "ok": return { dot: "bg-emerald-500", title: "已有录音" };
    case "missing": return { dot: "bg-zinc-400", title: "未录" };
    default: return { dot: "bg-amber-400", title: "含变量，无法预录" };
  }
}

/** 场景缺省名（抽屉场景下拉的空档;scene 仍是 steps_json 纯数据位,不参与布局）。 */
export const UNGROUPED_LANE = "未分组";

// —— 布局（确定性,同输入同输出;2026-09-20 重设计：纵向步骤工作流）——
// 步骤=中列瀑布脊柱（第 1 步在上,逐级向下）;意图=左栏卡片,按锚定目标步纵向堆叠;
// 边=脊柱顺序线（默认推进）+ 意图→目标步条件边（关键词入边）+ 播完跳转边（then_jump）。
// 场景不再是泳道（旧版横向泳道被用户判「没法用、不直观」）,退为步节点上的徽标。
export const STEP_COL_X = 380; // 步骤主列 x（工作流脊柱）
export const INTENT_COL_X = 40; // 意图左栏 x
export const TOP_Y = 40; // 首步 y
export const STEP_GAP_Y = 210; // 步节点纵向节距（卡更高：分支 chip 行占位）
export const INTENT_STACK_Y = 130; // 同锚点多个意图的纵向堆叠节距

export const STEP_NODE_W = 300;
export const INTENT_NODE_W = 240;
/** 步节点上分支 chip 的展示上限（超出折叠为「+N」）。 */
export const BRANCH_CHIP_LIMIT = 3;

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
    then_jump?: number;
    enabled?: boolean;
  }[];
};

/** 画布节点（kind 判别：step/intent）。位置为纯几何派生,拖拽覆盖在组件层。 */
export type FlowNode =
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
      scene: string;
      /** 答法分支（答法抽屉同源数据;resp 原文含动作标记,节点上渲染为条件 chip,上限外折叠计数）。 */
      branches: StepNodeBranch[];
      branchMore: number;
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
  /** spine=步间顺序线（默认推进）;jump=意图→目标步（jump_step）;thenjump=播快答播完跳转。 */
  kind: "spine" | "jump" | "thenjump";
  /** 边标签（spine=固定文案;jump=意图名+关键词;thenjump=播完跳转）。 */
  label: string;
};

/** 意图边标签：意图名 + 前两个关键词（画布上「哪些条件触发哪个步骤」的直接答案）。 */
function intentEdgeLabel(label: string, keywords: string[]): string {
  const kw = (keywords ?? []).slice(0, 2).join("、");
  return kw ? `${label}（${kw}）` : label;
}

/** 纵向工作流布局：步骤脊柱（i → i+1 顺序线,标「默认推进」）;意图锚定首个
 * jump_step 目标步（无绑定锚首步/生效首步）纵向堆叠在左栏。jump_step 悬空
 * （步号越界/意图缺失/停用绑定）不画边不计数;play_qa 的 then_jump 单独画
 * 「播完跳转」边（同为意图→步,引擎里是罐头播完当场跳）。 */
export function layoutFlow(
  steps: CanvasFlowStep[],
  graph?: GraphLite,
): { nodes: FlowNode[]; edges: FlowEdge[] } {
  const nodes: FlowNode[] = [];
  const edges: FlowEdge[] = [];
  // 步节点 id 用全局下标（fstep:<i>）——抽屉/意图目标都按全局下标寻址。
  const stepId = (i: number) => `fstep:${i}`;
  const intentNodeId = (id: string) => `fintent:${id}`;

  const stepY = (i: number) => TOP_Y + i * STEP_GAP_Y;

  steps.forEach((s, i) => {
    const parts = parseStepRefParts(s.ref);
    // 分支 chip 行：动作标记拆成 action/jump 派生位（resp 原文不动,抽屉 round-trip 依赖）。
    const branchNodes: StepNodeBranch[] = parts.branches.slice(0, BRANCH_CHIP_LIMIT).map((b) => {
      const info = parseBranchAction(b.resp);
      return { ...b, action: info.action, jump: info.step };
    });
    // 节点预览：正稿首行;无正稿（纯分支 ref）退首分支行,画布上仍有可读摘要——
    // 动作标记剥掉不进摘要（画布上不出现「【收线】」原始标记串）,标记应答为空时退动作徽标文案。
    const firstBranch = parts.branches[0];
    let scriptFirst = parts.script.split("\n")[0] ?? "";
    if (!scriptFirst && firstBranch) {
      const info = parseBranchAction(firstBranch.resp);
      scriptFirst = "如果客户" + firstBranch.cond + "→" + (info.text || branchActionBadge(info.action, info.step));
    }
    nodes.push({
      id: stepId(i),
      kind: "step",
      x: STEP_COL_X,
      y: stepY(i),
      index: i,
      goal: s.goal || "",
      scriptFirst: scriptFirst.slice(0, 40),
      say: s.say === true,
      emotion: s.emotion ?? "",
      scene: (s.scene ?? "").trim(),
      branches: branchNodes,
      branchMore: Math.max(0, parts.branches.length - BRANCH_CHIP_LIMIT),
      jumpIn: 0,
    });
  });

  // 脊柱顺序线：每对相邻步一条（工作流主推进路径——引擎按步序自动推进,跨场景不断线）。
  for (let i = 0; i + 1 < steps.length; i++) {
    const spine = {
      id: `spine:${i}:${i + 1}`,
      source: stepId(i),
      target: stepId(i + 1),
      kind: "spine" as const,
      label: "默认推进",
    };
    edges.push(spine);
  }

  // 意图节点 + 条件边（只读 overlay,编辑深链问答画布）。
  const intents = graph?.intents ?? [];
  const byId = new Map(intents.map((it) => [it.id, it]));
  const enabledBindings = (graph?.bindings ?? []).filter(
    (b) => b.enabled !== false && byId.has(b.intent),
  );
  const clampStep = (n: unknown, len: number) =>
    Math.min(Math.max((Number(n) || 1) - 1, 0), Math.max(len - 1, 0));

  // jump_step 锚点与 jumpIn 计数（悬空步号被钳制,但停用/悬空意图不画）。
  const intentAnchor = new Map<string, number>();
  for (const b of enabledBindings.filter((x) => x.action === "jump_step")) {
    const idx = clampStep(b.step, steps.length);
    intentAnchor.set(b.intent, idx);
    const node = nodes.find((n) => n.id === stepId(idx));
    if (node && node.kind === "step") node.jumpIn += 1;
  }
  // play_qa+then_jump 也锚到跳转目标（无则落播放锚点=生效首步/首步）。
  for (const b of enabledBindings.filter((x) => x.action === "play_qa" && Number(x.then_jump) > 0)) {
    if (!intentAnchor.has(b.intent)) {
      intentAnchor.set(b.intent, clampStep(b.then_jump, steps.length));
    }
  }

  const stackAt = new Map<number, number>();
  for (const it of intents) {
    const anchorIdx =
      intentAnchor.get(it.id) ??
      (steps.length > 0 ? clampStep(it.steps?.[0], steps.length) : 0);
    const stack = stackAt.get(anchorIdx) ?? 0;
    stackAt.set(anchorIdx, stack + INTENT_STACK_Y);
    nodes.push({
      id: intentNodeId(it.id),
      kind: "intent",
      x: INTENT_COL_X,
      y: stepY(anchorIdx) + stack,
      intentId: it.id,
      label: it.label || "(未命名意图)",
      keywords: it.keywords ?? [],
      enabled: it.enabled !== false,
      judge: Boolean(it.judge?.prompt),
      playQa: enabledBindings.some((b) => b.intent === it.id && b.action === "play_qa"),
    });
  }
  for (const b of enabledBindings.filter((x) => x.action === "jump_step")) {
    const it = byId.get(b.intent);
    const jumpEdge = {
      id: `jump:${b.id ?? `${b.intent}:${b.step}`}`,
      source: intentNodeId(b.intent),
      target: stepId(clampStep(b.step, steps.length)),
      kind: "jump" as const,
      label: intentEdgeLabel(it?.label || b.intent, it?.keywords ?? []),
    };
    edges.push(jumpEdge);
  }
  for (const b of enabledBindings.filter((x) => x.action === "play_qa" && Number(x.then_jump) > 0)) {
    const thenEdge = {
      id: `thenjump:${b.id ?? `${b.intent}:${b.then_jump}`}`,
      source: intentNodeId(b.intent),
      target: stepId(clampStep(b.then_jump, steps.length)),
      kind: "thenjump" as const,
      label: "播完跳转",
    };
    edges.push(thenEdge);
  }
  return { nodes, edges };
}
