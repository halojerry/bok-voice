"use client";

// 快路覆盖率 + 漏网轮采集（L-① 学习驾驶舱，2026-09-20）。
// 两段：①这套话术的回复里多少是脚本/录音直接播的（快路）、多少要 AI 现场组织；
// ②反复出现、AI 每次都要现场想答案的客户问法——运营确认后一键采集为问答词条。
// 数据来自 GET /api/stats/llm-gaps（只读聚合）；采集走 POST /api/stats/llm-gaps/adopt
// （与问答库新建同闸同审计，同问法已存在时幂等不重复建）。
// 权限：本组件由 studio 页在「场景学习」tab（reports 键）挂载；采集按钮需 qa 键。

import { useCallback, useEffect, useState } from "react";
import { api, type LlmGapRow, type LlmGapsReport } from "@/lib/api";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { useAccount } from "@/components/account-context";
import { hasPage, useSession } from "@/components/session-context";

const LANG_LABEL: Record<string, string> = {
  zh: "中文",
  cantonese: "粤语",
  en: "英语",
  vi: "越南语",
};

// 「按来源细分」的中文标签（折叠小字用；未知值原样展示）。
const SOURCE_LABELS: Record<string, string> = {
  script: "话术脚本直念",
  qa_fastpath: "问答库录音直出",
  llm: "AI 现场组织",
  "qa-fastpath": "问答库命中",
  "graph-play": "流程图快答",
  "branch-canned": "分支录音",
  "branch-refuse": "分支收线话术",
  "flow-say": "话术步骤直念",
  "graph-jump": "流程图跳步",
  "branch-jump": "分支跳步",
  "branch-notify": "转人工通知（照常作答）",
  "defer-ack": "缓一缓应承",
};
function sourceLabel(key: string): string {
  if (SOURCE_LABELS[key]) return SOURCE_LABELS[key];
  if (key.startsWith("stall-")) return "AI 卡顿重试";
  return key || "早期通话（未标记）";
}

function rowKey(row: LlmGapRow): string {
  return `${row.template_id || ""}|${row.customer_text}`;
}

export default function GapMining({ templateId }: { templateId?: string }) {
  const { accountId } = useAccount();
  const session = useSession();
  const canQa = hasPage(session, "qa");

  const [data, setData] = useState<LlmGapsReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [showDetail, setShowDetail] = useState(false);

  const load = useCallback(() => {
    let alive = true;
    setLoading(true);
    api
      .llmGaps(accountId, templateId ? { templateId } : {})
      .then((r) => {
        if (!alive) return;
        setData(r && typeof r === "object" && r.coverage ? r : null);
        setErr("");
      })
      .catch((e) => {
        if (!alive) return;
        setData(null);
        setErr(String(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [accountId, templateId]);

  useEffect(() => load(), [load]);

  // ---- 采集确认（行内展开，答案可编辑） ----
  const [editing, setEditing] = useState<{ row: LlmGapRow; answer: string } | null>(null);
  const [adopting, setAdopting] = useState(false);
  const [adoptErr, setAdoptErr] = useState("");
  const [doneMsg, setDoneMsg] = useState("");
  // 已处理行（含「库里已有同问法」的幂等结果）：key → 提示语
  const [adopted, setAdopted] = useState<Record<string, string>>({});

  function startAdopt(row: LlmGapRow) {
    setEditing({ row, answer: row.sample_answer || "" });
    setAdoptErr("");
    setDoneMsg("");
  }

  async function confirmAdopt() {
    if (!editing) return;
    const row = editing.row;
    const answer = editing.answer.trim();
    if (!answer) {
      setAdoptErr("回答内容不能为空。");
      return;
    }
    setAdopting(true);
    setAdoptErr("");
    try {
      const res = await api.adoptGapEntry({
        accountId,
        questionText: row.customer_text,
        answerText: answer,
        lang: row.lang || "zh",
        templateId: row.template_id || templateId || "",
        step: row.step || 0,
      });
      const msg =
        res?.created === false
          ? "问答库里已经有这条问法的词条了，没有重复创建。"
          : "已采集成问答词条。记得去问答库补录录音，客户再问就能直接播放。";
      setAdopted((prev) => ({ ...prev, [rowKey(row)]: msg }));
      setEditing(null);
      setDoneMsg(msg);
    } catch (e) {
      setAdoptErr(String(e));
    } finally {
      setAdopting(false);
    }
  }

  const coverage = data?.coverage ?? null;
  const gaps = (data?.gaps ?? []).filter(Boolean);
  const pct = coverage ? Math.round((Number(coverage.fastpath_ratio) || 0) * 100) : 0;

  return (
    <section className="card space-y-4">
      <div>
        <span className="label">快路覆盖率</span>
        <p className="mt-0.5 text-xs muted">
          通话里 AI 的回答有的是照稿念（话术脚本、问答录音），有的要 AI 现场组织语言。照稿念的部分又快又稳，这里看它占了多少。
        </p>
      </div>

      {loading && <LoadingState />}
      {!loading && err && (
        <>
          <ErrorState message={err} />
          <button className="btn-ghost text-xs" onClick={load}>
            重试
          </button>
        </>
      )}
      {!loading && !err && coverage && (
        <>
          {coverage.turns === 0 ? (
            <EmptyState label="这套话术还没有通话记录。" />
          ) : (
            <>
              <div className="flex flex-wrap items-end gap-4">
                <div>
                  <p className="text-3xl font-bold text-(--live)">{pct}%</p>
                  <p className="mt-0.5 text-xs muted">照稿念的回复占比</p>
                </div>
                <div className="flex gap-2">
                  <div className="rounded-lg bg-muted/60 px-3 py-2 text-center">
                    <p className="text-lg font-semibold">{Number(coverage.fastpath) || 0}</p>
                    <p className="text-[11px] muted">脚本/录音直出</p>
                  </div>
                  <div className="rounded-lg bg-muted/60 px-3 py-2 text-center">
                    <p className="text-lg font-semibold">{Number(coverage.llm) || 0}</p>
                    <p className="text-[11px] muted">AI 现场组织</p>
                  </div>
                  <div className="rounded-lg bg-muted/60 px-3 py-2 text-center">
                    <p className="text-lg font-semibold">{Number(coverage.turns) || 0}</p>
                    <p className="text-[11px] muted">回复轮合计</p>
                  </div>
                </div>
              </div>
              <p className="text-xs muted">
                {pct >= 60
                  ? `${pct}% 的回复是脚本或录音直接播的，不需要 AI 现场组织语言。`
                  : `${pct}% 的回复是脚本或录音直接播的；占比越高，回答越快越稳。`}
              </p>
              <div>
                <button
                  className="text-[11px] muted underline underline-offset-2"
                  onClick={() => setShowDetail((v) => !v)}
                >
                  {showDetail ? "收起来源细分" : "按来源细分"}
                </button>
                {showDetail && (
                  <div className="mt-1 space-y-0.5 rounded-lg bg-muted/40 p-2">
                    {Object.entries(coverage.by_gen ?? {}).map(([k, n]) => (
                      <p key={`g-${k}`} className="text-[11px] muted">
                        {sourceLabel(k)}：{Number(n) || 0} 轮
                      </p>
                    ))}
                    {Object.entries(coverage.by_provider ?? {}).map(([k, n]) => (
                      <p key={`p-${k}`} className="text-[11px] muted">
                        {sourceLabel(k)}：{Number(n) || 0} 轮
                      </p>
                    ))}
                  </div>
                )}
              </div>
            </>
          )}
        </>
      )}

      <div className="border-t border-(--card-border) pt-3">
        <span className="label">漏网轮候选（客户这么说，AI 是现场组织的）</span>
        <p className="mt-0.5 text-xs muted">
          这些问法反复出现，但每次都要 AI 自己想答案。把常出现的采集成问答词条，下次就能直接播放录好的回答。
        </p>
      </div>

      {!loading && !err && gaps.length === 0 && coverage && coverage.turns > 0 && (
        <EmptyState label="没有反复出现的漏网问法。" />
      )}

      {gaps.length > 0 && (
        <div className="space-y-2">
          {gaps.map((row) => {
            const key = rowKey(row);
            const isEditing = editing?.row === row;
            return (
              <div key={key} className="rounded-lg border border-(--card-border) p-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium">「{row.customer_text}」</p>
                    <p className="mt-0.5 text-xs muted">
                      出现 {Number(row.count) || 0} 轮 · 涉及 {Number(row.calls) || 0} 通通话
                      {Number(row.step) > 0 ? ` · 话术第 ${Number(row.step)} 步` : ""}
                      {row.lang && row.lang !== "zh" ? ` · ${LANG_LABEL[row.lang] ?? row.lang}` : ""}
                    </p>
                    {row.sample_answer && (
                      <p className="mt-1 line-clamp-2 text-xs muted">AI 当时说：{row.sample_answer}</p>
                    )}
                    {row.sample_call_id && (
                      <p className="mt-0.5 text-[11px] muted">可回听通话：{row.sample_call_id}</p>
                    )}
                  </div>
                  {canQa &&
                    (adopted[key] ? (
                      <span className="shrink-0 text-xs text-emerald-600">✓ 已采集</span>
                    ) : (
                      <button className="btn-primary shrink-0 text-xs" disabled={adopting} onClick={() => startAdopt(row)}>
                        采集为问答词条
                      </button>
                    ))}
                </div>
                {!canQa && <p className="mt-1 text-[11px] muted">需要问答库权限（问答库页同款）才能采集。</p>}
                {isEditing && (
                  <div className="mt-2 rounded-lg bg-muted/60 p-2">
                    <p className="text-xs font-medium">确认采集</p>
                    <p className="mt-0.5 text-[11px] muted">
                      客户问法「{row.customer_text}」→ 采集成问答词条（回答可以先改再存）：
                    </p>
                    <textarea
                      className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent p-2 text-xs"
                      rows={3}
                      value={editing.answer}
                      onChange={(e) => setEditing({ row, answer: e.target.value })}
                      placeholder="AI 该怎么回答（可用当时说的那句）"
                    />
                    {adoptErr && <div className="mt-1">
                      <ErrorState message={adoptErr} />
                    </div>}
                    <div className="mt-2 flex gap-2">
                      <button className="btn-primary text-xs" disabled={adopting} onClick={confirmAdopt}>
                        {adopting ? "采集中…" : "确认采集"}
                      </button>
                      <button className="btn-ghost text-xs" disabled={adopting} onClick={() => setEditing(null)}>
                        取消
                      </button>
                    </div>
                  </div>
                )}
                {!isEditing && adopted[key] && <p className="mt-1 text-[11px] text-emerald-600">{adopted[key]}</p>}
              </div>
            );
          })}
          {doneMsg && !editing && <p className="text-xs text-emerald-600">{doneMsg}</p>}
        </div>
      )}
    </section>
  );
}
