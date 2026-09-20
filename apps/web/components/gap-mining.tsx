"use client";

// 快路覆盖率 + 漏网轮采集（L-① 学习驾驶舱，2026-09-20）+ 话术分支/意图词提案
// （L-② 同日，同一张画面的另外两条教法）。两段：①这套话术的回复里多少是脚本/
// 录音直接播的（快路）、多少要 AI 现场组织；②反复出现、AI 每次都要现场想答案的
// 客户问法——运营确认后三条路任选：做成快答（问答词条）/ 做成话术分支（写进该步
// ref 的「如果客户…→…」行）/ 做成意图词（给流程图意图加关键词）。后两条写入的是
// 模板草稿（published_json 不动），要生效还需去话术页发布。
// 数据来自 GET /api/stats/template-proposals（coverage/gaps 与 L-① 同源同形 +
// proposals）；快答采集走 POST /api/stats/llm-gaps/adopt（问答库同闸同审计），
// 提案采纳走 POST /api/stats/template-proposals/adopt（模板 PUT 同闸链+版本快照）。
// 权限：本组件由 studio 页在「场景学习」tab（reports 键）挂载；快答按钮需 qa 键，
// 提案按钮需 templates 键（写的是话术）。

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import {
  branchLinePreview,
  branchRouteHint,
  buildBranchAdoptItem,
  buildIntentAdoptItem,
  intentRouteHint,
  proposalGapKey,
  proposalsForGap,
  type ProposalGapRow,
  type TemplateProposal,
  type TemplateProposalsReport,
} from "@/lib/gap-proposals";
import {
  buildReanswerItem,
  buildRetireItem,
  driftKey,
  driftKindLabel,
  driftResultMessage,
  driftSummary,
  type QaDriftAdoptResult,
  type QaDriftProposal,
  type QaDriftReport,
} from "@/lib/qa-drift";
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

function rowKey(row: ProposalGapRow): string {
  return proposalGapKey(row.template_id, row.customer_text);
}

export default function GapMining({ templateId }: { templateId?: string }) {
  const { accountId } = useAccount();
  const session = useSession();
  const canQa = hasPage(session, "qa");
  const canTemplates = hasPage(session, "templates");

  const [data, setData] = useState<TemplateProposalsReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [showDetail, setShowDetail] = useState(false);

  const load = useCallback(() => {
    let alive = true;
    setLoading(true);
    api
      .templateProposals(accountId, templateId ? { templateId } : {})
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
  const [editing, setEditing] = useState<{ row: ProposalGapRow; answer: string } | null>(null);
  const [adopting, setAdopting] = useState(false);
  const [adoptErr, setAdoptErr] = useState("");
  const [doneMsg, setDoneMsg] = useState("");
  // 已处理行（含「库里已有同问法」的幂等结果）：key → 提示语
  const [adopted, setAdopted] = useState<Record<string, string>>({});
  // ---- L-② 提案确认（分支应答 / 意图关键词都可先改再写） ----
  const [branchEdit, setBranchEdit] = useState<{ p: TemplateProposal; text: string } | null>(null);
  const [intentEdit, setIntentEdit] = useState<{ p: TemplateProposal; kw: string } | null>(null);

  // ---- L-③ 在库词条体检（通知面）：独立取数，drift 失败不影响覆盖率/漏网轮渲染 ----
  const [drift, setDrift] = useState<QaDriftReport | null>(null);
  const [driftErr, setDriftErr] = useState("");
  const [driftLoading, setDriftLoading] = useState(false);
  const [driftEdit, setDriftEdit] = useState<{ p: QaDriftProposal; text: string } | null>(null);
  const [driftDone, setDriftDone] = useState<Record<string, QaDriftAdoptResult>>({});
  const [driftAdopting, setDriftAdopting] = useState(false);
  const [driftMsg, setDriftMsg] = useState("");

  const loadDrift = useCallback(async () => {
    let alive = true;
    setDriftLoading(true);
    api
      .qaDrift(accountId)
      .then((r) => {
        if (!alive) return;
        setDrift(r && typeof r === "object" ? r : null);
        setDriftErr("");
      })
      .catch((e) => {
        if (!alive) return;
        setDrift(null);
        setDriftErr(String(e));
      })
      .finally(() => {
        if (alive) setDriftLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [accountId]);

  useEffect(() => {
    void loadDrift();
  }, [loadDrift]);

  function startAdopt(row: ProposalGapRow) {
    setEditing({ row, answer: row.sample_answer || "" });
    setAdoptErr("");
    setDoneMsg("");
  }

  function startBranchEdit(p: TemplateProposal) {
    setBranchEdit({ p, text: p.branch_resp || "" });
    setAdoptErr("");
    setDoneMsg("");
  }

  function startIntentEdit(p: TemplateProposal) {
    setIntentEdit({ p, kw: p.keyword || "" });
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

  // ---- L-② 提案采纳（写模板草稿；幂等/闸链在服务端） ----
  async function confirmBranchAdopt() {
    if (!branchEdit) return;
    const p = branchEdit.p;
    const text = branchEdit.text.trim();
    if (!text) {
      setAdoptErr("回答内容不能为空。");
      return;
    }
    setAdopting(true);
    setAdoptErr("");
    try {
      const res = await api.adoptTemplateProposals({ accountId, items: [buildBranchAdoptItem(p, text)] });
      const r = res?.results?.[0];
      const msg =
        r?.created === false
          ? "这句分支已经在话术里了，没有重复写。"
          : `已写进话术第 ${Number(p.step) || "?"} 步的分支。改动存的是草稿，记得去话术页发布才会生效。`;
      setAdopted((prev) => ({ ...prev, [p.key]: msg }));
      setBranchEdit(null);
      setDoneMsg(msg);
    } catch (e) {
      setAdoptErr(String(e));
    } finally {
      setAdopting(false);
    }
  }

  async function confirmIntentAdopt() {
    if (!intentEdit) return;
    const p = intentEdit.p;
    const kw = intentEdit.kw.trim();
    if (!kw) {
      setAdoptErr("关键词不能为空。");
      return;
    }
    setAdopting(true);
    setAdoptErr("");
    try {
      const res = await api.adoptTemplateProposals({ accountId, items: [buildIntentAdoptItem(p, kw)] });
      const r = res?.results?.[0];
      const msg =
        r?.created === false
          ? "这个关键词已经在话术里了，没有重复写。"
          : `已加进意图「${p.intent_label || p.intent_id}」的关键词。改动存的是草稿，记得去话术页发布才会生效。`;
      setAdopted((prev) => ({ ...prev, [p.key]: msg }));
      setIntentEdit(null);
      setDoneMsg(msg);
    } catch (e) {
      setAdoptErr(String(e));
    } finally {
      setAdopting(false);
    }
  }

  // ---- L-③ 采纳（改答案 / 删词条）：一个入口按 kind 分派 ----
  async function confirmDrift() {
    if (!driftEdit) return;
    const p = driftEdit.p;
    const text = driftEdit.text.trim();
    if (p.kind === "reanswer" && !text) {
      setDriftErr("新答案不能为空。");
      return;
    }
    setDriftAdopting(true);
    setDriftErr("");
    try {
      const res = await api.adoptQaDrift({
        accountId,
        items: [p.kind === "retire" ? buildRetireItem(p) : buildReanswerItem(p, text)],
      });
      const r = res?.results?.[0];
      if (r) setDriftDone((prev) => ({ ...prev, [p.key]: r }));
      setDriftEdit(null);
      setDriftMsg(driftResultMessage(r));
    } catch (e) {
      setDriftErr(String(e));
    } finally {
      setDriftAdopting(false);
    }
  }

  const coverage = data?.coverage ?? null;
  const gaps = (data?.gaps ?? []).filter(Boolean);
  const proposals = (data?.proposals ?? []).filter(Boolean);
  const driftSum = driftSummary(drift);
  const driftProposals = (drift?.proposals ?? []).filter(Boolean);
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
          这些问法反复出现，但每次都要 AI 自己想答案。每句话有三种教法，确认后才会生效：
          做成快答（下次直接播放录音）、做成话术分支（教到那一步的话术里）、做成意图词（听到这类话就按意图跳步或播快答）。
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
            const routes = proposalsForGap(proposals, row.template_id, row.customer_text);
            const branch = routes.branch;
            const intent = routes.intent;
            const branchDone = branch ? adopted[branch.key] : "";
            const intentDone = intent ? adopted[intent.key] : "";
            const branchHint = branchRouteHint(row.template_id, branch);
            const intentHint = intentRouteHint(row.template_id, intent);
            const branchUsable = Boolean(branch?.available) && canTemplates && !branchDone;
            const intentUsable = Boolean(intent?.available) && canTemplates && !intentDone;
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
                {/* L-② 同一句话的另外两条教法：写成话术分支 / 加成意图关键词 */}
                <div className="mt-2 space-y-1.5 border-t border-(--card-border) pt-2">
                  <p className="text-[11px] font-medium muted">这句话还可以教给话术本身：</p>
                  {/* 路由一：做成话术分支（写进该步 ref 的「如果客户…→…」行） */}
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    {branchUsable ? (
                      <button
                        className="btn-ghost shrink-0 text-xs"
                        disabled={adopting}
                        onClick={() => branch && startBranchEdit(branch)}
                      >
                        做成话术分支{Number(branch?.step) > 0 ? `（第 ${Number(branch?.step)} 步）` : ""}
                      </button>
                    ) : branchDone ? (
                      <span className="text-xs text-emerald-600">✓ {branchDone}</span>
                    ) : (
                      <span className="text-[11px] muted">{branchHint}</span>
                    )}
                    {branch?.available && !canTemplates && !branchDone && (
                      <span className="text-[11px] muted">需要话术权限才能写入。</span>
                    )}
                  </div>
                  {branchEdit?.p.key === branch?.key && (
                    <div className="rounded-lg bg-muted/60 p-2">
                      <p className="text-xs font-medium">确认做成话术分支</p>
                      <p className="mt-0.5 text-[11px] muted">
                        客户说「{branch?.branch_cond}」→ AI 按下面这句答（可以先改再存，存进第 {Number(branch?.step)} 步的话术草稿）：
                      </p>
                      <textarea
                        className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent p-2 text-xs"
                        rows={3}
                        value={branchEdit.text}
                        onChange={(e) => setBranchEdit({ p: branchEdit.p, text: e.target.value })}
                        placeholder="AI 该怎么回答（可用当时说的那句）"
                      />
                      <p className="mt-1 truncate text-[11px] muted">
                        将写入：{branchLinePreview(branchEdit.p.branch_cond, branchEdit.text)}
                      </p>
                      <div className="mt-2 flex gap-2">
                        <button className="btn-primary text-xs" disabled={adopting} onClick={confirmBranchAdopt}>
                          {adopting ? "写入中…" : "确认写入话术"}
                        </button>
                        <button className="btn-ghost text-xs" disabled={adopting} onClick={() => setBranchEdit(null)}>
                          取消
                        </button>
                      </div>
                    </div>
                  )}
                  {/* 路由二：做成意图词（给流程图意图加关键词） */}
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    {intentUsable ? (
                      <button
                        className="btn-ghost shrink-0 text-xs"
                        disabled={adopting}
                        onClick={() => intent && startIntentEdit(intent)}
                      >
                        做成意图词{intent?.intent_label ? `（「${intent.intent_label}」）` : ""}
                      </button>
                    ) : intentDone ? (
                      <span className="text-xs text-emerald-600">✓ {intentDone}</span>
                    ) : (
                      <span className="text-[11px] muted">{intentHint}</span>
                    )}
                    {intent?.available && !canTemplates && !intentDone && (
                      <span className="text-[11px] muted">需要话术权限才能写入。</span>
                    )}
                  </div>
                  {intentEdit?.p.key === intent?.key && (
                    <div className="rounded-lg bg-muted/60 p-2">
                      <p className="text-xs font-medium">确认做成意图词</p>
                      <p className="mt-0.5 text-[11px] muted">
                        客户话里出现下面的词，就当作「{intentEdit.p.intent_label || intentEdit.p.intent_id}」处理（跳步或播快答）。
                        关键词可以先改：
                      </p>
                      <input
                        className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent p-2 text-xs"
                        value={intentEdit.kw}
                        onChange={(e) => setIntentEdit({ p: intentEdit.p, kw: e.target.value })}
                        placeholder="触发词，比如「投诉」「几时到」"
                      />
                      <div className="mt-2 flex gap-2">
                        <button className="btn-primary text-xs" disabled={adopting} onClick={confirmIntentAdopt}>
                          {adopting ? "写入中…" : "确认加入意图"}
                        </button>
                        <button className="btn-ghost text-xs" disabled={adopting} onClick={() => setIntentEdit(null)}>
                          取消
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
          {doneMsg && !editing && <p className="text-xs text-emerald-600">{doneMsg}</p>}
        </div>
      )}

      {/* ---- L-③ 在库快答体检：以「通知」形式送达，确认后才会动库 ---- */}
      <div className="border-t border-(--card-border) pt-3">
        <span className="label">已入库快速回答的体检（哪里该改、哪里可以删）</span>
        <p className="mt-0.5 text-xs muted">
          这里检查已经在库的快速回答：哪条播了客户又问一遍（该改答案）、哪条压根没人问过（可以删）。
          下面都是建议，确认后才会改。
        </p>
      </div>

      {driftLoading && <LoadingState />}
      {!driftLoading && driftErr && (
        <>
          <ErrorState message={driftErr} />
          <button className="btn-ghost text-xs" onClick={() => void loadDrift()}>
            重试
          </button>
        </>
      )}
      {!driftLoading && !driftErr && driftSum.total === 0 && (
        <EmptyState
          label={
            driftSum.scanned === 0
              ? "库里还没有快速回答，先去「快答库」页面建几条。"
              : `最近 ${driftSum.windowCalls} 通通话里，库里的快速回答都没发现问题。`
          }
        />
      )}
      {!driftLoading && !driftErr && driftSum.total > 0 && (
        <div className="space-y-2">
          <p className="text-xs muted">
            最近 {driftSum.windowCalls} 通通话、库里 {driftSum.scanned} 条快速回答，发现有 {driftSum.total} 条该处理：
            改答案 {driftSum.reanswer} 条、建议删 {driftSum.retire} 条。
          </p>
          {driftProposals.map((p) => {
            const done = driftDone[p.key];
            const isEditing = driftEdit?.p.key === p.key;
            return (
              <div key={p.key} className="rounded-lg border border-(--card-border) p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <p className="text-sm font-medium">「{p.question_text}」</p>
                  <span className="rounded bg-muted/60 px-1.5 py-0.5 text-[10px] font-medium">
                    {driftKindLabel(p.kind)}
                  </span>
                </div>
                <p className="mt-0.5 text-xs muted">{p.headline}</p>
                <p className="mt-1 text-xs muted">{p.detail}</p>
                {p.current_answer && (
                  <p className="mt-1 text-[11px] muted">现在的回答：{p.current_answer}</p>
                )}
                {done ? (
                  <p className="mt-1 text-[11px] text-emerald-600">✓ {driftResultMessage(done)}</p>
                ) : canQa ? (
                  <button
                    className="btn-ghost mt-2 text-xs"
                    disabled={driftAdopting}
                    onClick={() => {
                      setDriftMsg("");
                      setDriftErr("");
                      setDriftEdit({ p, text: p.suggested_answer || "" });
                    }}
                  >
                    {/* 触发按钮与确认按钮用不同文案:「确认删掉」在展开前后同名，
                        运营会看不出有没有点到(自动化也分不清) */}
                    {p.kind === "retire" ? "删掉这条" : "改这条答案"}
                  </button>
                ) : (
                  <p className="mt-1 text-[11px] muted">需要问答权限才能改动。</p>
                )}
                {isEditing && (
                  <div className="mt-2 rounded-lg bg-muted/60 p-2">
                    {p.kind === "retire" ? (
                      <>
                        <p className="text-xs font-medium">确认删掉这条快速回答？</p>
                        <p className="mt-0.5 text-[11px] muted">
                          删掉后电话里遇到这个问法会改由 AI 现场回答，以后不会再播这段录音。
                        </p>
                      </>
                    ) : (
                      <>
                        <p className="text-xs font-medium">确认改成下面这句</p>
                        <p className="mt-0.5 text-[11px] muted">
                          可以先改再存；保存后会重新录音，录完在电话里生效。
                        </p>
                        <textarea
                          className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent p-2 text-xs"
                          rows={3}
                          value={driftEdit.text}
                          onChange={(e) => setDriftEdit({ p, text: e.target.value })}
                          placeholder="改成什么回答"
                        />
                      </>
                    )}
                    <div className="mt-2 flex gap-2">
                      <button className="btn-primary text-xs" disabled={driftAdopting} onClick={confirmDrift}>
                        {driftAdopting ? "处理中…" : p.kind === "retire" ? "确认删掉" : "确认改答案"}
                      </button>
                      <button className="btn-ghost text-xs" disabled={driftAdopting} onClick={() => setDriftEdit(null)}>
                        取消
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
          {driftMsg && <p className="text-xs text-emerald-600">{driftMsg}</p>}
        </div>
      )}
    </section>
  );
}
