"use client";

// 步列导航（一步一画布改版，2026-09-26，docs/SCENE_CANVAS.md）：画布左侧的步骤窄卡列。
// 纯受控零内部状态——选中态由页面层 current 给、数据是 buildRail 派生产物：草稿每次
// 编辑整列随页面重渲染，「列与画布不一致」在结构上不可能发生。本组件只上报点击与加步。
// readOnly（已发布/无编辑权）时整列仍可看，但不可点、不出加步口——看与改分离。

import type { RailItem } from "@/lib/step-canvas";

// 徽标基底与画布步节点徽标同款口径（rounded-sm px-1 text-[10px]），只按语义分配色档。
const BADGE = "rounded-sm px-1 text-[10px] leading-4";

export default function StepRail(props: {
  /** buildRail(steps, graph) 产物（页面层持有并派生，本组件不自己算）。 */
  rail: RailItem[];
  /** 当前选中步号（1-based）。 */
  current: number;
  onSelect: (stepNo: number) => void;
  /** 只读=整列不可点（仍可看），并隐藏加一步。 */
  readOnly?: boolean;
  /** 底部「＋ 加一步」回调；未传则不渲染按钮。 */
  onAddStep?: () => void;
}) {
  const { rail, current, onSelect, readOnly, onAddStep } = props;
  return (
    <nav className="flex w-full flex-col gap-2" aria-label="步骤导航">
      {rail.length === 0 && <p className="text-sm muted">还没有步骤</p>}
      {rail.map((item) => {
        const isCurrent = item.stepNo === current;
        // 当前步用主题色边框+软底（与画布步节点同配色，一眼对上）；只读时非当前步
        // 不给 cursor/hover——不给「能点」的暗示，免得点了没反应被当成坏了。
        const cardCls = isCurrent
          ? "border-(--live) bg-(--live-soft)"
          : readOnly
            ? "border-(--card-border)"
            : "cursor-pointer border-(--card-border) hover:bg-muted/60";
        return (
          <div
            key={item.stepNo}
            className={`rounded-lg border p-2.5 ${cardCls}`}
            aria-current={isCurrent ? "step" : undefined}
            onClick={readOnly ? undefined : () => onSelect(item.stepNo)}
          >
            <p className="text-sm font-bold text-(--live-ink)">第 {item.stepNo} 步</p>
            {item.stepNo === 1 && (
              <p className="mt-0.5 text-[10px] text-(--live-ink)">● 电话接通先讲这一步</p>
            )}
            <p className={`mt-0.5 truncate ${item.goal ? "" : "muted"}`}>
              {item.goal || "(未写目的)"}
            </p>
            {(item.say || item.scene || item.branchCount > 0 || item.intentCount > 0) && (
              <div className="mt-1.5 flex flex-wrap items-center gap-1">
                {item.say && (
                  <span className={`${BADGE} bg-sky-100 text-sky-700`}>逐字照念</span>
                )}
                {item.scene && (
                  <span className={`${BADGE} bg-violet-100 text-violet-700`}>{item.scene}</span>
                )}
                {item.branchCount > 0 && (
                  <span className={`${BADGE} bg-muted`}>{item.branchCount} 条应对</span>
                )}
                {item.intentCount > 0 && (
                  <span className={`${BADGE} bg-amber-100 text-amber-700`}>
                    {item.intentCount} 意图
                  </span>
                )}
              </div>
            )}
          </div>
        );
      })}
      {!readOnly && onAddStep && (
        <button type="button" className="btn-ghost text-xs" onClick={onAddStep}>
          ＋ 加一步
        </button>
      )}
    </nav>
  );
}
