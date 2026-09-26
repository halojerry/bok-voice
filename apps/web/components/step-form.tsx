"use client";

// 结构化「步骤答法」编辑器（2026-09-25 列表编辑卡片结构化）：
// 每步三件 = 正稿 textarea（AI 主要说的话）+ 分支行编辑器（条件/动作/应答/删）+
// 注意 textarea（多行=多条注意行）——取代列表卡片里 正稿+分支+注意 混写的单个大 textarea。
//
// 拆装/序列化/动作标记全部走 lib/flow-canvas 纯函数镜像（parseStepRefParts/
// serializeStepRef/parseBranchAction/composeBranchResp——agent flow.py
// _BRANCH_LINE_RE/_NOTE_LINE_RE/_BRANCH_ACTION_RE 的逐语义移植）,round-trip 无损：
// 未知行原样保留、箭头两侧空格形态保真（F8）、无标记分支零改写。
//
// 与 components/flow-canvas.tsx 答法抽屉同语义（该文件保持原样,后续另行收编）：
//   - parts 持组件态：装载 parseStepRefParts 拆,每次编辑即时 serializeStepRef 写回
//     调用方的 step.ref（「即时序列化回草稿」模式,与抽屉 updateParts 同款）;
//   - 编辑面三件拆装：条件 / 动作（+jump 步号）/ 纯文本——动作标记只活在 resp 原文里,
//     文本域恒显示剥标记后的内容,编辑时 composeBranchResp 重组回 resp（切动作不丢字）;
//   - 不搬录音状态点/补录按钮（那是画布侧 canned-status 数据面,列表页没有该数据面）。

import { useEffect, useRef, useState } from "react";
import type { BranchAction, StepBranch, StepRefParts } from "@/lib/flow-canvas";
import {
  BRANCH_ACTIONS,
  composeBranchResp,
  parseBranchAction,
  parseStepRefParts,
  serializeStepRef,
} from "@/lib/flow-canvas";
import { VarTextarea } from "@/components/var-insert";

const inputCls =
  "w-full rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--live)";

/** jump 步号输入钳制（列表卡片口径 1..步数）：非 1..999 整数一律按 1（与 lib
 * clampJumpStep 同底）,再钳到 stepCount 上限（步数未知/非法=只按 1..999 兜底）。
 * 只用于步号输入框的 onChange——文本/动作切换路径不钳,没编辑到的步号不偷改。 */
export function clampJumpStepInRange(step: number, stepCount?: number): number {
  const n = Number(step);
  let v = Number.isInteger(n) && n >= 1 && n <= 999 ? n : 1;
  if (Number.isInteger(stepCount) && (stepCount as number) >= 1) {
    v = Math.min(v, stepCount as number);
  }
  return v;
}

/** 单条分支行编辑器（可复用子组件）：条件 input + 动作下拉（jump 多一个步号输入）+
 * 应答 textarea + 删按钮。受控：branch.resp 恒为原文含动作标记;编辑经 composeBranchResp
 * 重组,arrow（解析记下的箭头原始分隔）由 spread 原样保留（F8 分隔符保真）。 */
export function BranchRowEditor(props: {
  branch: StepBranch;
  onChange: (next: StepBranch) => void;
  onDelete: () => void;
  /** jump 步号输入上限（当前草稿步数）;缺省 999。 */
  stepCount?: number;
  disabled?: boolean;
}) {
  const { branch, disabled } = props;
  // 编辑面三件拆装：条件 / 动作（+跳步步号）/ 纯文本——动作标记只活在 resp 原文里,
  // 文本域恒显示剥标记后的内容,编辑时 composeBranchResp 重组回 resp（切动作不丢字）。
  const info = parseBranchAction(branch.resp);
  const setResp = (action: BranchAction, step: number, text: string) =>
    props.onChange({ ...branch, resp: composeBranchResp(action, step, text) });
  const stepTotal = Number.isInteger(props.stepCount) && (props.stepCount as number) >= 1 ? (props.stepCount as number) : 0;
  return (
    <div className="space-y-1 rounded-lg border border-(--card-border) bg-background p-1.5">
      <input
        className={inputCls}
        value={branch.cond}
        disabled={disabled}
        placeholder="客户说…（如：问为什么赔）"
        onChange={(e) => props.onChange({ ...branch, cond: e.target.value })}
      />
      <div className="flex items-center gap-1">
        <select
          className={inputCls}
          value={info.action}
          disabled={disabled}
          title="AI 听到这句话后要做什么"
          onChange={(e) => {
            const next = e.target.value as BranchAction;
            // 切动作不丢文本;jump 步号沿用旧值（原本无标记/非 jump 时默认 1）。
            const step = info.action === "jump" ? info.step : 1;
            setResp(next, step, info.text);
          }}
        >
          {BRANCH_ACTIONS.map((a) => (
            <option key={a.value} value={a.value}>{a.label}</option>
          ))}
        </select>
        {info.action === "jump" && (
          <input
            type="number"
            className="w-16 shrink-0 rounded-lg border border-(--card-border) bg-transparent px-1.5 py-1 text-xs outline-hidden focus:border-(--live)"
            min={1}
            max={stepTotal || 999}
            value={info.step || 1}
            disabled={disabled}
            title={`跳到第几步（从 1 开始数${stepTotal ? `，共 ${stepTotal} 步` : ""}）`}
            onChange={(e) =>
              setResp("jump", clampJumpStepInRange(Number(e.target.value), props.stepCount), info.text)
            }
          />
        )}
      </div>
      <p className="text-[10px] muted">
        {BRANCH_ACTIONS.find((a) => a.value === info.action)?.hint}
      </p>
      <textarea
        className={`h-14 resize-none ${inputCls}`}
        value={info.text}
        disabled={disabled}
        placeholder="AI 就答…（一两句话，写要点即可）"
        onChange={(e) =>
          // 分支应答是单行注入（序列化按行拼 ref）,换行折叠成空格防劈裂分支行。
          setResp(
            info.action,
            info.action === "jump" ? info.step : 0,
            e.target.value.replace(/[\r\n]+/g, " "),
          )
        }
      />
      <div className="flex justify-end">
        <button
          className="btn-ghost shrink-0 px-1.5 py-0 text-xs text-red-600"
          disabled={disabled}
          onClick={props.onDelete}
        >
          删
        </button>
      </div>
    </div>
  );
}

/** 步骤答法三件编辑器（可复用）：正稿 + 分支行列表（＋加一条应对）+ 注意。
 * parts 持组件态：装载按 refText 拆装,每次编辑即时 serializeStepRef 经 onChange 写回
 * 调用方的 step.ref;外部 refText 变化（换步/上下移/删步/导入/模板重载）自动重拆,
 * 自己写回的 ref 不重拆（否则受控输入会在编辑中被规范化值打断）。
 * 空分支（点了「加一条应对」还没填）只活在组件态,serializeStepRef 不产垃圾行——
 * ref 不被空行污染;填一半（缺条件或缺应答）降级普通行,文字不丢。 */
export function StepRefForm(props: {
  /** 当前步 ref 原文（受控源;prop 名避用 ref=React 保留 prop）。 */
  refText: string;
  /** 每次编辑回调:参数=serializeStepRef(next) 即时序列化结果,调用方写回 step.ref。 */
  onChange: (serializedRef: string) => void;
  /** 步骤总数（jump 步号输入 1..stepCount）;缺省 999。 */
  stepCount?: number;
  disabled?: boolean;
  scriptPlaceholder?: string;
  notesPlaceholder?: string;
}) {
  const refText = String(props.refText ?? "");
  const [parts, setParts] = useState<StepRefParts>(() => parseStepRefParts(refText));
  // 自己写回的规范化 ref;外部 refText 与它不同=这步被外部改过（换步/移动/导入）→ 重拆。
  const mineRef = useRef<string>(serializeStepRef(parseStepRefParts(refText)));
  useEffect(() => {
    if (refText === mineRef.current) return;
    const next = parseStepRefParts(refText);
    mineRef.current = serializeStepRef(next);
    setParts(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refText]);

  function update(next: StepRefParts) {
    mineRef.current = serializeStepRef(next);
    setParts(next);
    props.onChange(mineRef.current);
  }
  const setBranch = (i: number, patch: Partial<StepBranch>) =>
    update({ ...parts, branches: parts.branches.map((b, j) => (j === i ? { ...b, ...patch } : b)) });

  return (
    <div className="space-y-1.5">
      <div>
        <span className="text-xs muted">正稿（AI 主要说的话，写要点即可，AI 用自己的语气讲）</span>
        <div className="mt-0.5">
          <VarTextarea
            className={`h-24 resize-none ${inputCls}`}
            value={parts.script}
            disabled={props.disabled}
            placeholder={props.scriptPlaceholder ?? "AI 主要说的话…（可含 {变量}）"}
            onChange={(v) => update({ ...parts, script: v })}
          />
        </div>
      </div>
      <div>
        <span className="text-xs muted">客户如果这样说 → AI 怎么做（每条一个应对）</span>
        <div className="mt-1 space-y-1.5">
          {parts.branches.map((b, i) => (
            <BranchRowEditor
              key={i}
              branch={b}
              stepCount={props.stepCount}
              disabled={props.disabled}
              onChange={(next) => setBranch(i, next)}
              onDelete={() => update({ ...parts, branches: parts.branches.filter((_, j) => j !== i) })}
            />
          ))}
          <button
            className="btn-ghost px-2 py-0.5 text-xs"
            disabled={props.disabled}
            onClick={() => update({ ...parts, branches: [...parts.branches, { cond: "", resp: "" }] })}
          >
            ＋ 加一条应对
          </button>
        </div>
      </div>
      <label className="block">
        <span className="text-xs muted">注意（给 AI 的补充提醒，多行=多条，如必讲的号码）</span>
        <textarea
          className={`mt-0.5 h-16 resize-none ${inputCls}`}
          value={parts.notes}
          disabled={props.disabled}
          placeholder={props.notesPlaceholder ?? "如：赔付只能进微信零钱"}
          onChange={(e) => update({ ...parts, notes: e.target.value })}
        />
      </label>
    </div>
  );
}
