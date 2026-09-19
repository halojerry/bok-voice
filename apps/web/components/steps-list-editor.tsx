"use client";

// 步骤列表编辑器（2026-09-20 易用性改版）：完全受控（value/onChange），
// 草稿状态归工作站页面层——「未保存/应用」全局按钮的唯一数据面之一。
// 与画布（FlowCanvas 同样受控）编辑同一份草稿：来回切换视图不再丢修改。
// 字段面与原 TemplateEditor 分步块一致（目标/参考说法+变量按钮/直念/情绪/
// 排序/删除），文案按普通人视角重写；表格导入与三语示例复用 template-editor 纯函数。

import { useState } from "react";
import {
  parseStepsFromTable, STEPS_EXAMPLES, LANGS,
  type FlowStep,
} from "@/components/template-editor";
import { VarTextarea } from "@/components/var-insert";

const textarea =
  "w-full resize-none rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--live)";

const EMOTIONS: [string, string][] = [
  ["", "自动"],
  ["calm", "平稳自然"],
  ["sad", "低沉柔和（致歉/安抚）"],
  ["happy", "轻快亲切"],
  ["surprised", "惊讶上扬"],
];

export default function StepsListEditor(props: {
  /** 步骤草稿（工作站层持有；本组件只上报变更）。 */
  value: FlowStep[];
  onChange: (next: FlowStep[]) => void;
  readOnly?: boolean;
  /** 模板语言（填示例用）。 */
  lang: string;
}) {
  const { value, readOnly } = props;
  const [showTableImport, setShowTableImport] = useState(false);
  const [tableText, setTableText] = useState("");
  const [tableMsg, setTableMsg] = useState<string | null>(null);

  const setStep = (i: number, patch: Partial<FlowStep>) =>
    props.onChange(value.map((s, j) => (j === i ? { ...s, ...patch } : s)));
  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= value.length) return;
    const next = [...value];
    [next[i], next[j]] = [next[j], next[i]];
    props.onChange(next);
  };

  function importTable() {
    const { steps: parsed, error } = parseStepsFromTable(tableText);
    if (error) {
      setTableMsg(error);
      return;
    }
    props.onChange([...value, ...parsed]);
    setTableMsg(`已从表格导入 ${parsed.length} 步。`);
    setShowTableImport(false);
    setTableText("");
  }

  function fillExample(lang: string) {
    const example = STEPS_EXAMPLES[lang];
    if (!example) return;
    if (value.length > 0 && !window.confirm("填入示例会替换当前全部步骤，继续？")) return;
    props.onChange(example.map((s) => ({ ...s })));
  }

  return (
    <section className="card space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="label">步骤列表（通话从第 1 步开始，讲完一步进下一步）</span>
        <div className="flex flex-wrap gap-2">
          <button
            className="btn-ghost text-xs"
            disabled={readOnly}
            onClick={() => props.onChange([...value, { goal: "", ref: "" }])}
          >
            + 加一步
          </button>
          <button className="btn-ghost text-xs" disabled={readOnly} onClick={() => setShowTableImport((v) => !v)}>
            {showTableImport ? "收起表格导入 ▲" : "从表格粘贴导入 ▼"}
          </button>
          {LANGS.map(([v, l]) => (
            <button key={v} className="btn-ghost text-xs" disabled={readOnly} onClick={() => fillExample(v)}>
              填{l}示例
            </button>
          ))}
        </div>
      </div>

      <p className="text-[11px] leading-relaxed muted">
        每一步填「AI 主要说什么」（写要点即可，AI 会用自己的语气讲）；下面可加「客户如果这样说 →
        就这样答」的应对。改完记得点右上角「应用」才会保存。
      </p>

      {showTableImport && (
        <div className="rounded-lg border border-dashed border-(--card-border) p-2">
          <p className="text-[11px] leading-relaxed muted">
            从表格（Excel/Sheets/CSV）粘贴：<b>每行一步</b>，第一列=这一步的目的，第二列=AI 主要说什么。
          </p>
          <textarea
            className={`mt-1.5 h-24 ${textarea} text-xs`}
            placeholder={"目的\tAI 说什么\n开场确认\t你好，请问係咪{姓名}？我哋係{物流公司}…"}
            value={tableText}
            disabled={readOnly}
            onChange={(e) => { setTableText(e.target.value); setTableMsg(null); }}
          />
          <div className="mt-1.5 flex items-center gap-2">
            <button className="btn-primary px-3 py-1 text-xs" disabled={readOnly} onClick={importTable}>
              导入为步骤
            </button>
            {tableMsg && <span className="text-xs muted">{tableMsg}</span>}
          </div>
        </div>
      )}

      <div className="space-y-3">
        {value.length === 0 && (
          <p className="text-sm muted">还没有步骤——点「+ 加一步」或「填示例」开始。</p>
        )}
        {value.map((st, i) => (
          <div key={i} className="rounded-lg border border-(--card-border) bg-muted/60 p-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-bold text-(--live-ink)">第 {i + 1} 步</span>
              <div className="flex gap-1">
                <button className="btn-ghost px-1.5 py-0 text-xs" disabled={readOnly || i === 0} onClick={() => move(i, -1)}>↑ 上移</button>
                <button className="btn-ghost px-1.5 py-0 text-xs" disabled={readOnly || i === value.length - 1} onClick={() => move(i, 1)}>↓ 下移</button>
                <button
                  className="btn-ghost px-1.5 py-0 text-xs text-red-600"
                  disabled={readOnly}
                  onClick={() => {
                    if (window.confirm(`删除第 ${i + 1} 步？`)) props.onChange(value.filter((_, j) => j !== i));
                  }}
                >
                  删除
                </button>
              </div>
            </div>
            <input
              className="mt-1.5 w-full rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--live)"
              placeholder="这一步的目的（给你自己看的备注，如：确认对方身份）"
              value={st.goal}
              disabled={readOnly}
              onChange={(e) => setStep(i, { goal: e.target.value })}
            />
            <div className="mt-1.5">
              <VarTextarea
                className={`h-24 ${textarea} text-xs`}
                value={st.ref}
                disabled={readOnly}
                onChange={(v) => setStep(i, { ref: v })}
              />
            </div>
            <label className="mt-1 flex items-center gap-1.5 text-[11px] muted">
              <input
                type="checkbox"
                className="size-3 accent-(--live)"
                checked={Boolean(st.say)}
                disabled={readOnly}
                onChange={(e) => setStep(i, { say: e.target.checked })}
              />
              逐字照念（AI 不自由发挥，一字不差念上面第一行——通知、承诺类内容建议勾）
            </label>
            {Boolean(st.say) && (
              <label className="mt-1 flex items-center gap-1.5 text-[11px] muted">
                念的语气
                <select
                  className="rounded-lg border border-(--card-border) bg-transparent px-1.5 py-0.5 text-xs outline-hidden focus:border-(--live)"
                  value={st.emotion ?? ""}
                  disabled={readOnly}
                  onChange={(e) => setStep(i, { emotion: e.target.value })}
                >
                  {EMOTIONS.map(([v, l]) => (
                    <option key={v} value={v}>{l}</option>
                  ))}
                </select>
              </label>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
