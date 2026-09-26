"use client";

// 一步一画布（Scene Canvas v2，2026-09-26，docs/SCENE_CANVAS.md）：
// 话术流程从「一张大画布铺全模板」改为「步列 + 每步一张画布」——点左列第 N 步，
// 画布只画这一步的宇宙：中央步节点（正稿+分支锚点 pill）、左列触发它的意图
// （含快答徽章）、右列它能去的终点（跳步 chip / 收线 / 转人工·打铃）。
// 铁律：主流程顺序=数组顺序（引擎固有），**不画默认线**；画布上画的每根线=一次偏离。
// 唯一例外是「默认推进」ghost 虚线——它是 should_auto_advance 的只读可视化，
// 不是配置，所以 interactive:false 不可点不可选（防把它当配置去「解除」）。
//
// 与旧 FlowCanvas 纪律同源（2026-09-26 旧全模板画布已随本改版删除,本组件为其取代者）：
//   - 完全受控：draft/onDraftChange——草稿归工作站页面层，「应用」是唯一保存入口；
//   - 分支连线=动作标记写进该分支 resp 行（lib/step-canvas setStepBranchAction，
//     parse/serialize round-trip 无损）；
//   - 意图连线=立即落库 graph_json（onBindJump 由页面层走 graphDocWithJumpBinding）；
//   - owner 只读判定 / useSized 守门 / nodeTypes 模块级——姿势逐条照抄旧画布。
// 本组件零网络请求：qaLabels / branchCanned / 全部回调由上层喂。

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useSession } from "@/components/session-context";
import { useSized } from "@/lib/use-sized";
import { StepRefForm } from "@/components/step-form";
import StepRail from "@/components/step-rail";
import type { FlowStep, TemplateRow } from "@/components/template-editor";
import type { GraphDoc } from "@/lib/qa-canvas";
import {
  branchActionBadge,
  buildRail,
  clearStepBranchAction,
  layoutStepUniverse,
  setStepBranchAction,
  stepUniverse,
  SC_CHIP_NODE_W,
  SC_INTENT_NODE_W,
  SC_STEP_NODE_W,
  type ScNode,
} from "@/lib/step-canvas";
import { branchCannedMeta, canvasFitViewOptions, type BranchCannedStatus } from "@/lib/flow-canvas";

// 情绪下拉选项与 steps-list-editor / 答法抽屉同款（罐头物化烧进音频,实时回复不受影响）。
const EMOTIONS: [string, string][] = [
  ["", "自动"],
  ["calm", "平稳自然"],
  ["sad", "低沉柔和（致歉/安抚）"],
  ["happy", "轻快亲切"],
  ["surprised", "惊讶上扬"],
];
const EMOTION_LABEL: Record<string, string> = {
  calm: "平稳", sad: "柔和", happy: "亲切", surprised: "上扬",
};

// 拖拽柄外观（flow-canvas / qa-canvas 同款）。
const HANDLE_STYLE = {
  width: 8, height: 8, background: "var(--live)", border: "1px solid var(--card-border)",
};

// fitView 缩放夹在 [0.6,1]（真浏览器验收实弹校准,2026-09-26）：三列宇宙宽约 970px,
// 常规视口塞不下要缩到 ~0.6 才整图可见;0.75 下限（旧纵向画布口径）会把右列目标 chip
// 裁出视野。读细节靠点选后 RF 自动居中/缩放控件,首屏先给全貌。模块级常量,避免每渲染新对象。
const FIT_VIEW_OPTIONS = { ...canvasFitViewOptions(), minZoom: 0.6 };

const inputCls =
  "w-full rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--live)";

// 分支动作徽标配色（答法抽屉/旧画布同款:refuse=红(收线) handoff=蓝(人工) jump=琥珀(跳步) hold=紫(停留)）。
const ACTION_BADGE_CLS: Record<string, string> = {
  refuse: "bg-red-100 text-red-700",
  handoff: "bg-sky-100 text-sky-700",
  jump: "bg-amber-100 text-amber-700",
  hold: "bg-violet-100 text-violet-700",
};

// —— 纯渲染节点（四种,与 lib/step-canvas ScNode.kind 一一对应） ——

// 步节点（中央大卡）：步号+目的+正稿摘要+徽标+分支锚点 pill 列;点击开抽屉。
// 每枚 pill 右缘一枚 source Handle（id=a{branchIndex}）——拖到右列终点=给该分支设动作。
function ScStepNode({ data }: NodeProps) {
  const d = data as Extract<ScNode, { kind: "step" }> & {
    onOpen: () => void;
    canBind: boolean;
    branchCanned?: Record<string, BranchCannedStatus>;
    onPregenBranch?: (resp: string) => void;
  };
  return (
    <div
      className="cursor-pointer rounded-lg border border-(--live) bg-(--live-soft) p-3 text-xs hover:bg-accent"
      style={{ width: SC_STEP_NODE_W }}
      title="点这一步，编辑 AI 怎么说、怎么应对"
      onClick={() => d.onOpen()}
    >
      {/* 卡底 ghost 出柄：只作默认推进虚线的锚点,恒不可连（引擎默认不是可配动作）。 */}
      <Handle type="source" position={Position.Bottom} id="ghost" isConnectable={false} style={HANDLE_STYLE} />
      <p className="flex items-center gap-1.5 font-medium">
        <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-(--live) px-1 text-[10px] font-bold text-white">
          {d.stepNo}
        </span>
        <span className="min-w-0 flex-1 truncate">{d.goal || "(没写目的)"}</span>
      </p>
      <p className="mt-1 line-clamp-2 muted">{d.scriptFirst || "(还没写内容)"}</p>
      <div className="mt-2 flex flex-wrap items-center gap-1">
        {d.say && <span className="rounded-sm bg-sky-100 px-1 text-[10px] text-sky-700">逐字照念</span>}
        {d.say && d.emotion && (
          <span className="rounded-sm bg-muted px-1 text-[10px]">{EMOTION_LABEL[d.emotion] ?? d.emotion}</span>
        )}
      </div>
      {d.anchors.length > 0 && (
        <div className="mt-1.5 border-t border-(--card-border) pt-1.5">
          <p className="text-[9px] muted">客户这样说时 → 拖右侧圆点到终点=设动作</p>
          <div className="mt-1 space-y-1">
            {d.anchors.map((a) => {
              // 动作徽标（收线/转人工/跳第N步/留本步）;悬浮提示剥标记,不露「【收线】」原始串。
              const badge = branchActionBadge(a.action, a.jump);
              const canned = d.branchCanned?.[a.resp];
              const meta = canned ? branchCannedMeta(canned) : null;
              return (
                <div
                  key={a.branchIndex}
                  className="relative flex items-center gap-1 rounded-full border border-(--card-border) bg-background px-1.5 py-0.5 text-[10px]"
                  title={`客户${a.cond} → ${badge ? badge + "：" : ""}${a.resp.replace(/^【[^】]*】\s*/, "")}`}
                >
                  <span className="max-w-[10em] truncate">{a.cond || "(空)"}</span>
                  {badge && (
                    <span className={`shrink-0 rounded-sm px-1 leading-4 ${ACTION_BADGE_CLS[a.action] ?? "bg-muted"}`}>
                      {badge}
                    </span>
                  )}
                  {meta && (
                    <span
                      className={`inline-block size-1.5 shrink-0 rounded-full ${meta.dot}`}
                      aria-hidden
                      title={meta.title}
                    />
                  )}
                  {/* 补录入口（旧画布答法抽屉同款回归保住）：未录（missing）且回调在场才出。
                      ph=含变量录不出,不给按钮免得点了必败。 */}
                  {canned === "missing" && d.onPregenBranch && d.canBind && (
                    <button
                      type="button"
                      className="shrink-0 text-[9px] text-(--live) hover:underline"
                      title="生成这条应对的录音（AI 用模板音色合成，约几秒）"
                      onClick={(e) => {
                        e.stopPropagation(); // 别顺带开抽屉
                        d.onPregenBranch?.(a.resp);
                      }}
                    >
                      补录
                    </button>
                  )}
                  {/* 分支锚点出柄：canBind（非只读）才可拖——连线=改草稿,无需额外回调。 */}
                  <Handle
                    type="source"
                    position={Position.Right}
                    id={`a${a.branchIndex}`}
                    isConnectable={d.canBind}
                    style={HANDLE_STYLE}
                  />
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

// 意图节点（左列触发卡）：label+关键词 chips+停用置灰+判据徽标;右缘出柄拖到步 chip=设跳步绑定。
// 意图本身只读 overlay——新建/关键词/判据编辑走「意图管理」,本画布不承担。
function ScIntentNode({ data }: NodeProps) {
  const d = data as Extract<ScNode, { kind: "intent" }> & { canBind: boolean };
  const card = d.card;
  return (
    <div
      className={`rounded-lg border px-3 py-2 text-xs shadow-sm ${
        card.enabled ? "border-amber-300 bg-amber-50" : "border-(--card-border) bg-muted/60 opacity-50"
      }`}
      style={{ width: SC_INTENT_NODE_W }}
    >
      {/* 意图出柄：canBind 才可连（onBindJump 在场且非只读）——连线=立即落库 graph_json。 */}
      <Handle type="source" position={Position.Right} id="out" isConnectable={d.canBind} style={HANDLE_STYLE} />
      <p className="flex items-center gap-1.5 font-medium">
        <span aria-hidden>🎯</span>
        <span className="min-w-0 truncate">{card.label}</span>
      </p>
      <p className="mt-1 flex flex-wrap items-center gap-1">
        <span className="text-[10px] muted">听到：</span>
        {card.keywords.slice(0, 3).map((k) => (
          <span key={k} className="max-w-[8em] truncate rounded-sm bg-background px-1 text-[10px] muted">{k}</span>
        ))}
        {card.keywords.length > 3 && <span className="text-[10px] muted">+{card.keywords.length - 3}</span>}
        {card.keywords.length === 0 && <span className="text-[10px] muted">—</span>}
      </p>
      <div className="mt-1.5 flex flex-wrap gap-1">
        {card.judge && (
          <span
            className="rounded-sm bg-amber-100 px-1 text-[10px] text-amber-700"
            title="关键词没听清时，AI 会按这段描述再判断一次"
          >
            判据
          </span>
        )}
        {!card.enabled && <span className="rounded-sm bg-muted px-1 text-[10px]">已停用</span>}
      </div>
    </div>
  );
}

// 终点 chip（右列）：收线=红边、转人工/打铃=蓝边、跳步目标=普通（被连线时高亮）。
// 左缘 target Handle 是全部连线的落点（分支动作/意图跳步/默认虚线）。
function ScChipNode({ data }: NodeProps) {
  const d = data as Extract<ScNode, { kind: "chip" }> & { canBind?: boolean };
  const c = d.chip;
  const look =
    c.kind === "refuse"
      ? "border-red-300 bg-red-50"
      : c.kind === "bell"
        ? "border-sky-300 bg-sky-50"
        : c.usedBy.length > 0
          ? "border-(--live) bg-(--live-soft)"
          : "border-(--card-border) bg-background";
  // 步 chip 的 goal 摘要：label 形如「第N步 · goal」（stepUniverse 拼装),剥掉步号前缀。
  const goalPart = c.kind === "step" ? c.label.replace(/^第\d+步\s*·\s*/, "") : "";
  return (
    <div
      className={`rounded-lg border px-2.5 py-2 text-xs shadow-sm ${look}`}
      title={
        c.kind === "refuse"
          ? "拖线到这里＝这轮说完礼貌收线（结束通话）"
          : c.kind === "bell"
            ? "拖线到这里＝通知人工坐席打铃（AI 不打断通话）"
            : `拖线到这里＝跳到第 ${c.stepNo} 步`
      }
    >
      <Handle type="target" position={Position.Left} id="in" isConnectable={Boolean(d.canBind)} style={HANDLE_STYLE} />
      <p className="flex items-center gap-1 font-medium">
        {c.kind === "refuse" ? (
          <><span aria-hidden>✆</span>收线</>
        ) : c.kind === "bell" ? (
          <><span aria-hidden>🔔</span>转人工 / 打铃</>
        ) : (
          <><span aria-hidden>↩</span>第{c.stepNo}步</>
        )}
      </p>
      {c.kind === "refuse" && <p className="mt-0.5 text-[10px] muted">分支标记 / 明确拒绝 / 末步默认</p>}
      {c.kind === "step" && <p className="mt-0.5 truncate text-[10px] muted">{goalPart}</p>}
    </div>
  );
}

// 快答徽章（左列意图卡下的小条）：意图 play_qa 绑定的可视化;词条缺失时明说,不假装有词条。
// target 柄只是既有绑定边的锚点——没有任何手势能连到它,恒不可连。
function ScQaBadgeNode({ data }: NodeProps) {
  const d = data as Extract<ScNode, { kind: "qabadge" }>;
  return (
    <div
      className="rounded-full border border-emerald-300 bg-emerald-50 px-2 py-1 text-[10px] text-emerald-700 shadow-sm"
      title="听到该意图时直接播这条快答录音（更快更稳）"
    >
      <Handle type="target" position={Position.Left} id="in" isConnectable={false} style={HANDLE_STYLE} />
      {d.label}
    </div>
  );
}

// nodeTypes 必须是模块级常量——React Flow 硬规矩：组件体里每次渲染新对象会令全部节点
// 重挂（性能崩+控制台警告）,官方文档明令。四个 key 与节点 type 字符串一一对应。
const NODE_TYPES = {
  scStep: ScStepNode,
  scIntent: ScIntentNode,
  scChip: ScChipNode,
  scQaBadge: ScQaBadgeNode,
};

// —— 步编辑抽屉（右侧固定面板,非 modal）：全部受控,写路径归宿主草稿 ——

function StepDrawer(props: {
  stepNo: number; // 1-based（画布当前步）
  step: FlowStep;
  stepCount: number;
  readOnly: boolean;
  onGoal: (v: string) => void;
  onRef: (v: string) => void;
  onSay: (v: boolean) => void;
  onEmotion: (v: string) => void;
  onClose: () => void;
}) {
  return (
    // overlay 抽屉（absolute 悬浮,不占 flex 布局——真浏览器验收实弹:做成 flex 兄弟会
    // 把画布列挤窄 ~340px,三列宇宙右列被整列遮掉、左列意图卡切头。悬浮盖住右侧局部
    // 是可接受的代价:编辑文字时不需要看终点列,收起即回全貌;画布宽度恒定不重排。
    <aside className="absolute right-2 top-2 bottom-2 z-20 w-[340px] space-y-2 overflow-y-auto rounded-lg border border-(--card-border) bg-background/95 p-3 shadow-lg backdrop-blur-sm">
      <div className="flex items-center justify-between">
        <span className="label">第 {props.stepNo} 步：AI 怎么说</span>
        <button className="btn-ghost px-2 py-0.5 text-xs" onClick={props.onClose}>收起 ✕</button>
      </div>
      <p className="text-[11px] muted">改动记入右上角「未保存」，点「应用」才会生效。</p>
      <label className="block">
        <span className="text-xs muted">这一步的目的（给你自己看的备注）</span>
        <input
          className={`mt-0.5 ${inputCls}`}
          value={props.step.goal}
          disabled={props.readOnly}
          placeholder="如：确认对方身份"
          onChange={(e) => props.onGoal(e.target.value)}
        />
      </label>
      <label className="flex items-center gap-1.5 text-[11px] muted">
        <input
          type="checkbox"
          className="size-3 accent-(--live)"
          checked={props.step.say === true}
          disabled={props.readOnly}
          onChange={(e) => props.onSay(e.target.checked)}
        />
        逐字照念（AI 一字不差念正稿第一行，不自由发挥）
      </label>
      {props.step.say === true && (
        <label className="flex items-center gap-1.5 text-[11px] muted">
          念的语气
          <select
            className="rounded-lg border border-(--card-border) bg-transparent px-1.5 py-0.5 text-xs outline-hidden focus:border-(--live)"
            value={props.step.emotion ?? ""}
            disabled={props.readOnly}
            onChange={(e) => props.onEmotion(e.target.value)}
          >
            {EMOTIONS.map(([v, l]) => (
              <option key={v} value={v}>{l}</option>
            ))}
          </select>
          <span className="text-[10px]">录音时用这个语气</span>
        </label>
      )}
      {/* 三件表单原样复用（正稿/分支/注意）：refText 外部变化（换步/画布拖线改了分支动作）
          时 StepRefForm 自身按 mineRef 机制重拆,画布与抽屉永远同一份草稿。 */}
      <StepRefForm
        refText={props.step.ref}
        onChange={props.onRef}
        stepCount={props.stepCount}
        disabled={props.readOnly}
      />
    </aside>
  );
}

// —— 主组件 ——

export default function StepCanvasView(props: {
  /** 编辑的模板行（owner 判定源,同旧 FlowCanvas 口径）;null=无可编辑内容。 */
  tpl: TemplateRow | null;
  /** 话术图（parseGraphDoc 结果）——触发列/绑定线数据源。 */
  graph: GraphDoc;
  /** 步骤草稿（页面层持有）;本组件只上报变更,不做保存。 */
  draft: FlowStep[];
  onDraftChange: (next: FlowStep[]) => void;
  /** qa_id → 词条问题摘要（快答徽章文案）;缺省=徽章显示「词条缺失」。 */
  qaLabels?: Record<string, string>;
  /** 分支罐头录音状态（key=分支 resp 原文含标记,逐字节）;缺省=不显示状态点。 */
  branchCanned?: Record<string, BranchCannedStatus>;
  /** 点锚点 pill「补录」：生成该分支录音（页面层回调,admin/root 配额闸在 CP 端）。 */
  onPregenBranch?: (resp: string) => void;
  /** 「去意图管理」深链（全程意图横条尾部按钮）。 */
  onOpenIntents?: () => void;
  /** 意图拖线→步 chip：该意图绑定设为跳到第 N 步,立即落库 graph_json（页面层回调）。 */
  onBindJump?: (intentId: string, stepNo: number) => Promise<void> | void;
  /** 点意图跳步连线：解除该意图绑定（页面层回调）。未传=连线不可点。 */
  onUnbindJump?: (intentId: string) => Promise<void> | void;
}) {
  const { graph, draft, qaLabels, branchCanned } = props;
  const session = useSession();
  const tplId = String(props.tpl?.id ?? "");
  // B4 owner 只读兜底（与旧 FlowCanvas 同款口径;CP PUT/publish 闸兜底）。
  const isManager = Boolean(
    session && (session.anonymous || session.role === "admin" || session.role === "root"),
  );
  const uid = session?.user_id ?? "";
  const readOnly = Boolean(tplId) && !isManager && !(uid !== "" && String(props.tpl?.owner_user_id ?? "") === uid);
  // 把手/解绑开关分两档：分支连线=改草稿（只需非只读）;意图连线=落库 graph_json（需回调在场,
  // 缺回调时把手不可连,与旧画布 canBind 同纪律）。
  const canBindBranch = !readOnly;
  const canBindGraph = !readOnly && typeof props.onBindJump === "function";
  const canUnbindBranch = !readOnly;
  const canUnbindGraph = !readOnly && typeof props.onUnbindJump === "function";

  const stepCount = draft.length;
  // 当前步（1-based）：默认第 1 步;表单视图删步/导入替换后钳回 1..len,超界回落最后一步。
  const [currentStep, setCurrentStep] = useState(1);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [globalOpen, setGlobalOpen] = useState(false);
  // 容器尺寸就绪守门（error#004/#015,旧画布同款）：挂载竞态期 0 尺寸不挂 ReactFlow。
  const sizedWrap = useSized<HTMLDivElement>();

  // 步数收缩（表单视图删步等）→ 钳制当前步;空草稿保持 1（空态提示接管画布）。
  useEffect(() => {
    setCurrentStep((cur) => (cur > stepCount ? Math.max(stepCount, 1) : cur));
  }, [stepCount]);

  // 换模板：抽屉/全程意图展开收起、当前步回 1（id 不变的应用保存不打扰在途状态）。
  useEffect(() => {
    setDrawerOpen(false);
    setGlobalOpen(false);
    setCurrentStep(1);
  }, [tplId]);

  // —— 宇宙派生（lib/step-canvas 纯函数;本组件不含任何布局/解析逻辑） ——
  const u = useMemo(
    () => stepUniverse(draft, graph, currentStep, { qaLabels }),
    [draft, graph, currentStep, qaLabels],
  );
  const lay = useMemo(() => (u ? layoutStepUniverse(u) : null), [u]);
  const rail = useMemo(() => buildRail(draft, graph), [draft, graph]);

  // —— 写路径（全部上报宿主草稿;保存=工作站层「应用」） ——
  const updateStep = useCallback(
    (idx: number, patch: Partial<FlowStep>) =>
      props.onDraftChange(draft.map((s, i) => (i === idx ? { ...s, ...patch } : s))),
    [draft, props.onDraftChange],
  );
  const openDrawer = useCallback(() => setDrawerOpen(true), []);

  // —— RF 受控图 ——
  const rfNodes: Node[] = useMemo(
    () =>
      (lay?.nodes ?? []).map((n) => {
        if (n.kind === "step") {
          return {
            id: n.id, type: "scStep" as const, position: { x: n.x, y: n.y },
            draggable: false, deletable: false,
            data: { ...n, onOpen: openDrawer, canBind: canBindBranch, branchCanned, onPregenBranch: props.onPregenBranch },
          };
        }
        if (n.kind === "intent") {
          return {
            id: n.id, type: "scIntent" as const, position: { x: n.x, y: n.y },
            draggable: false, deletable: false, data: { ...n, canBind: canBindGraph },
          };
        }
        return {
          id: n.id, type: n.kind === "chip" ? ("scChip" as const) : ("scQaBadge" as const),
          position: { x: n.x, y: n.y }, draggable: false, deletable: false,
          data: { ...n, canBind: canBindBranch },
        };
      }),
    [lay, openDrawer, canBindBranch, canBindGraph, branchCanned, props.onPregenBranch],
  );

  const rfEdges: Edge[] = useMemo(
    () =>
      (lay?.edges ?? []).map((e) => {
        if (e.kind === "ghost") {
          // ghost=引擎默认推进的可视化,不是配置：interactive:false 令它不可点不可选——
          // 点线「解除」手势只属于偏离动作（分支/意图线）,默认行为没有「解除」可言。
          return {
            id: e.id, source: e.source, target: e.target,
            sourceHandle: e.sourceHandle, targetHandle: e.targetHandle,
            label: e.label,
            interactive: false, selectable: false, deletable: false,
            style: { stroke: "#9ca3af", strokeWidth: 1.6, strokeDasharray: "2 7" },
            labelStyle: { fontSize: 10, fill: "#6b7280" },
          };
        }
        if (e.kind === "branch") {
          // 分支动作线（实线琥珀）：可点=解除该分支动作（剥标记,应答文字保留）。
          return {
            id: e.id, source: e.source, target: e.target,
            sourceHandle: e.sourceHandle, targetHandle: e.targetHandle,
            label: e.label,
            data: { kind: e.kind, branchIndex: e.branchIndex },
            selectable: canUnbindBranch, deletable: false,
            style: {
              stroke: "#d97706", strokeWidth: 2,
              ...(canUnbindBranch ? { cursor: "pointer" } : {}),
            },
            labelStyle: { fill: "#92400e", fontSize: 10 },
            labelBgStyle: { fill: "#fef3c7" },
          };
        }
        // 意图绑定线（虚线琥珀,旧画布 jump 边同款）：只有 jump_step 可点解绑——
        // 播快答/打铃绑定在「意图管理」里改,画布点线不承担（语义不属于换绑）。
        const isJump = e.action === "jump_step";
        return {
          id: e.id, source: e.source, target: e.target,
          sourceHandle: e.sourceHandle, targetHandle: e.targetHandle,
          label: e.label,
          data: { kind: e.kind, intentId: e.intentId, action: e.action },
          selectable: isJump && canUnbindGraph, deletable: false,
          style: {
            stroke: "#d97706", strokeWidth: 1.8, strokeDasharray: "6 4",
            ...(isJump && canUnbindGraph ? { cursor: "pointer" } : {}),
          },
          labelStyle: { fill: "#92400e", fontSize: 10 },
          labelBgStyle: { fill: "#fef3c7" },
        };
      }),
    [lay, canUnbindBranch, canUnbindGraph],
  );

  // 画布连线：分支锚点→终点=写该分支动作标记（草稿层）;意图出线→步 chip=落库跳步绑定。
  // branchIndex 从 sourceHandle 解析（"a3"→3）——handle 就是锚点寻址的权威,不靠边 data 猜。
  // 非法组合（chip→x / qa→x / 意图→收线·打铃等）一律静默忽略。
  const onConnect = useCallback(
    (conn: Connection) => {
      if (readOnly) return;
      const src = String(conn.source ?? "");
      const handle = String(conn.sourceHandle ?? "");
      const tgt = String(conn.target ?? "");
      if (src === "sc:step" && /^a\d+$/.test(handle)) {
        const branchIndex = Number(handle.slice(1));
        if (tgt === "sc:tgt:refuse") {
          props.onDraftChange(setStepBranchAction(draft, currentStep, branchIndex, "refuse"));
        } else if (tgt === "sc:tgt:bell") {
          props.onDraftChange(setStepBranchAction(draft, currentStep, branchIndex, "handoff"));
        } else if (tgt.startsWith("sc:tgt:step:")) {
          const n = Number(tgt.slice("sc:tgt:step:".length));
          if (Number.isInteger(n) && n >= 1) {
            props.onDraftChange(setStepBranchAction(draft, currentStep, branchIndex, "jump", n));
          }
        }
        return;
      }
      if (src.startsWith("sc:int:") && handle === "out") {
        if (!tgt.startsWith("sc:tgt:step:")) return;
        const n = Number(tgt.slice("sc:tgt:step:".length));
        if (!Number.isInteger(n) || n < 1) return;
        void props.onBindJump?.(src.slice("sc:int:".length), n);
      }
    },
    [readOnly, draft, currentStep, props.onDraftChange, props.onBindJump],
  );

  // 点连线=解除动作：分支线剥标记（文字保留）;意图 jump_step 线解除绑定（确认文案同旧画布——
  // 若该意图同时挂着播快答绑定,解绑会一并解除,文案统一明说,让操作员自己拿主意）。
  const onEdgeClick = useCallback(
    (_ev: unknown, edge: Edge) => {
      const d = (edge.data ?? {}) as { kind?: string; branchIndex?: number; intentId?: string; action?: string };
      if (d.kind === "branch") {
        if (!canUnbindBranch) return;
        const bi = Number(d.branchIndex);
        if (!Number.isInteger(bi) || bi < 0) return;
        if (!window.confirm("解除这条应对的动作？解除后 AI 答完这句话照常推进。")) return;
        props.onDraftChange(clearStepBranchAction(draft, currentStep, bi));
        return;
      }
      if (d.kind === "intent" && d.action === "jump_step") {
        if (!canUnbindGraph) return;
        const intentId = String(d.intentId ?? "");
        if (!intentId) return;
        if (!window.confirm(`解除「${edge.label ?? "该意图"}」的连线？解除后这条意图命中时只答话、不再跳步。`)) return;
        void props.onUnbindJump?.(intentId);
      }
    },
    [canUnbindBranch, canUnbindGraph, draft, currentStep, props.onDraftChange, props.onUnbindJump],
  );

  const drawerStep = u ? draft[u.index] : undefined;

  return (
    <section className="card space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="label">场景画布</span>
        <span className="text-[11px] muted">
          一步=一场景：左列切步；画布=这一步的宇宙（左=触发、中=本步、右=去向）
        </span>
        {u && (
          <span
            className="rounded-full bg-muted px-2 py-0.5 text-[10px] muted"
            title="默认推进是引擎行为，画布上只是提示，改不了"
          >
            {u.ghost.label}
          </span>
        )}
        {!readOnly && (
          <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] text-amber-700">
            拖分支/意图圆点到右侧终点＝设动作；点连线＝解除
          </span>
        )}
      </div>
      {/* 全程意图横条：steps=[] 的意图对每张画布都生效,不铺节点（防重复铺同一批）,
          收成一条横条点开看列表;编辑仍走「意图管理」。 */}
      {u && u.globalIntents.length > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-[11px]">
          <button
            type="button"
            className="flex w-full items-center gap-1 text-left font-medium text-amber-700"
            onClick={() => setGlobalOpen((v) => !v)}
          >
            <span aria-hidden>{globalOpen ? "▾" : "▸"}</span>
            全程意图 ×{u.globalIntents.length}（所有步骤都听）
          </button>
          {globalOpen && (
            <div className="mt-1.5 space-y-1">
              {u.globalIntents.map((it) => (
                <div key={it.intentId} className={`flex flex-wrap items-center gap-1 ${it.enabled ? "" : "opacity-50"}`}>
                  <span className="font-medium text-amber-800">🎯 {it.label}</span>
                  {it.keywords.map((k) => (
                    <span key={k} className="max-w-[10em] truncate rounded-sm bg-background px-1 text-[10px] muted">{k}</span>
                  ))}
                  {it.judge && (
                    <span
                      className="rounded-sm bg-amber-100 px-1 text-[10px] text-amber-700"
                      title="关键词没听清时，AI 会按这段描述再判断一次"
                    >
                      判据
                    </span>
                  )}
                  {!it.enabled && <span className="rounded-sm bg-muted px-1 text-[10px]">已停用</span>}
                </div>
              ))}
              {props.onOpenIntents && (
                <button
                  type="button"
                  className="text-(--live) hover:underline"
                  onClick={props.onOpenIntents}
                >
                  去意图管理 →
                </button>
              )}
            </div>
          )}
        </div>
      )}
      <div className="flex items-stretch gap-3">
        {/* StepRail 自身渲染 w-full（列内自适应）——横向两栏里给它一个定宽 shrink-0
            容器,否则它的 100% flex-basis 会把右侧 flex-1 画布挤塌成零宽。
            高度钉画布同高 600：步列超出在列内滚动（加步不改页面总高,2026-09-26
            Ethan 反馈「整个画面被顶得很长」）;加一步按钮随列滚,不悬浮。 */}
        <div className="flex h-[600px] w-60 shrink-0 flex-col">
          <StepRail
            rail={rail}
            current={currentStep}
            onSelect={setCurrentStep}
            readOnly={readOnly}
            onAddStep={readOnly ? undefined : () => props.onDraftChange([...draft, { goal: "", ref: "" }])}
          />
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <div className="flex items-stretch gap-3">
            {/* 尺寸就绪才挂 ReactFlow（error#004/#015 官方修法,旧画布同款守门）：容器量到
                非零宽高前渲染空占位——挂载竞态期 0 尺寸=布局警告+节点量不到、拖拽即
                「not initialized」。空草稿不挂 ReactFlow,直接给空态提示。 */}
            <div ref={sizedWrap.ref} className="relative h-[600px] min-w-0 flex-1 rounded-lg border border-(--card-border)">
              {stepCount === 0 ? (
                <div className="flex h-full items-center justify-center px-4 text-center text-xs muted">
                  {readOnly
                    ? "这套话术还没有步骤（共享话术由主管维护）。"
                    : "这套话术还没有步骤——点左栏「+ 加一步」开始。"}
                </div>
              ) : sizedWrap.ready ? (
                <ReactFlow
                  nodes={rfNodes}
                  edges={rfEdges}
                  nodeTypes={NODE_TYPES}
                  fitView
                  fitViewOptions={FIT_VIEW_OPTIONS}
                  minZoom={0.2}
                  deleteKeyCode={null}
                  onConnect={onConnect}
                  onEdgeClick={onEdgeClick}
                >
                  <Background gap={24} />
                  <Controls />
                  <MiniMap pannable zoomable />
                </ReactFlow>
              ) : (
                <div className="flex h-full items-center justify-center text-xs muted">画布加载中…</div>
              )}
              {/* overlay 抽屉挂在画布容器内（absolute 锚 relative 容器）——见 StepDrawer 头注。 */}
              {drawerOpen && u && drawerStep && (
                <StepDrawer
                  stepNo={u.stepNo}
                  step={drawerStep}
                  stepCount={stepCount}
                  readOnly={readOnly}
                  onGoal={(v) => updateStep(u.index, { goal: v })}
                  onRef={(v) => updateStep(u.index, { ref: v })}
                  onSay={(v) => updateStep(u.index, { say: v })}
                  onEmotion={(v) => updateStep(u.index, { emotion: v })}
                  onClose={() => setDrawerOpen(false)}
                />
              )}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
