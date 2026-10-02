"use client";

// 学习 tab（W3 T2 自 studio 页原样迁入）：话术优化分析 + 高频问答对挖掘，
// 新增 AI 聚类采纳面板（挖掘候选 → 本地 LLM 聚类 → 一键采纳入库）。
// 权限：本 tab 由 studio 页按 reports 键挂载；聚类操作按钮需 qa 键
// （CP 端点 _gate_page("qa")），无键只读并提示。

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { useAccount } from "@/components/account-context";
import { hasPage, useSession } from "@/components/session-context";

const LANG_LABEL: Record<string, string> = {
  zh: "中文",
  cantonese: "粤语",
  en: "英语",
  vi: "越南语",
};

type ClusterItem = Record<string, unknown>;

/** 裸 dict 防御取值：候选行可能平铺（question）也可能包在 row 里（junk 的 {row,reason}）。 */
function fieldOf(it: ClusterItem | undefined, keys: string[]): string {
  if (!it) return "";
  const row = (it.row && typeof it.row === "object" ? it.row : {}) as ClusterItem;
  for (const src of [it, row]) {
    for (const k of keys) {
      const v = src[k];
      if (typeof v === "string" && v.trim() !== "") return v;
      if (typeof v === "number") return String(v);
    }
  }
  return "";
}

type ClusterPlan = {
  variants: ClusterItem[];
  fresh: ClusterItem[];
  junk: ClusterItem[];
  hotwords: ClusterItem[];
  model: string;
};

/** dry 响应 → 四组清单（T1 冻结 {variants[],fresh[],junk[],model,counts}；EX-H1
 * 追加 hotwords 段 {hotwords:{candidates[],counts}}；字段名防御收窄，缺组按空数组降级）。 */
function parsePlan(data: Record<string, unknown> | null | undefined): ClusterPlan {
  const arr = (v: unknown) => (Array.isArray(v) ? (v as ClusterItem[]) : []);
  const hw = data?.hotwords as Record<string, unknown> | undefined;
  return {
    variants: arr(data?.variants),
    fresh: arr(data?.fresh),
    junk: arr(data?.junk),
    hotwords: arr(hw?.candidates),
    model: String(data?.model ?? ""),
  };
}

function variantQuestion(it: ClusterItem): string {
  return fieldOf(it, ["question", "question_text"]);
}
function variantAnswer(it: ClusterItem): string {
  return fieldOf(it, ["answer", "answer_text"]);
}
function variantTarget(it: ClusterItem): string {
  return fieldOf(it, ["target", "target_id", "cluster_head_id"]);
}
function variantTargetQuestion(it: ClusterItem): string {
  return fieldOf(it, ["target_question", "head_question", "target_question_text"]);
}
function itemLang(it: ClusterItem): string {
  return fieldOf(it, ["lang", "language"]);
}
function itemCalls(it: ClusterItem): string {
  return fieldOf(it, ["calls", "hit"]);
}
function junkReason(it: ClusterItem): string {
  return fieldOf(it, ["reason", "note"]);
}

// ---- 热词沉淀（EX-H1）----

const HOTWORD_KIND_LABEL: Record<string, string> = {
  polish_fix: "听错纠正",
  near_miss: "要求重复",
  gap_ngram: "高频词",
};
const HOTWORD_VERDICT_LABEL: Record<string, string> = {
  adopt: "建议采纳",
  reject: "建议丢弃",
};

function hotwordWord(it: ClusterItem): string {
  return fieldOf(it, ["word"]);
}
function hotwordFreq(it: ClusterItem): string {
  return fieldOf(it, ["freq"]);
}
function hotwordKind(it: ClusterItem): string {
  return fieldOf(it, ["kind"]);
}
function hotwordVerdict(it: ClusterItem): string {
  return fieldOf(it, ["verdict"]);
}
/** 第一条证据样本 raw→fixed（fixed 为空=仅原始转写，非纠错类）。 */
function hotwordEvidence(it: ClusterItem): { raw: string; fixed: string } | null {
  const arr = Array.isArray(it.evidence) ? (it.evidence as ClusterItem[]) : [];
  if (arr.length === 0) return null;
  const e = arr[0];
  return {
    raw: typeof e.raw === "string" ? e.raw : "",
    fixed: typeof e.fixed === "string" ? e.fixed : "",
  };
}

/** 聚类候选上限：dry 与 apply **必须同参**——CP 勾选采纳守卫按 (账号,参数) 找
 * 新鲜计划缓存，apply 参数与 dry 不符即 409「请重新生成」（不同参数是另一份计划）。 */
const CLUSTER_LIMIT = 30;

/** 采纳子集：勾选下标 → select=[{kind,i}]（CP apply 入参,缺省=全部）。 */
function buildSelect(variantChecks: boolean[], freshChecks: boolean[]) {
  const select: { kind: string; i: number }[] = [];
  variantChecks.forEach((on, i) => {
    if (on) select.push({ kind: "variant", i });
  });
  freshChecks.forEach((on, i) => {
    if (on) select.push({ kind: "fresh", i });
  });
  return select;
}

/** 热词采纳子集：勾选下标（CP hotword_select 入参；空数组=不采纳任何热词）。 */
function buildHotwordSelect(hotwordChecks: boolean[]): number[] {
  const sel: number[] = [];
  hotwordChecks.forEach((on, i) => {
    if (on) sel.push(i);
  });
  return sel;
}

const ADOPT_TIP =
  "已采纳。新词条需补录罐头音：问答库右键重新录音，或主管在设置页触发 pregen。";

const HOTWORD_ADOPT_TIP = "热词已采纳（挖掘自真实通话转写，采纳后下一通生效）。";

export default function StudyTab() {
  const { accountId } = useAccount();
  const session = useSession();
  const canQa = hasPage(session, "qa");

  // ---- 报告数据（自 studio 页迁入,逻辑不变） ----
  const [insights, setInsights] = useState<Record<string, unknown> | null>(null);
  const [insightsErr, setInsightsErr] = useState("");
  const [pairs, setPairs] = useState<Record<string, unknown>[]>([]);
  const [pairsErr, setPairsErr] = useState("");
  const [reportsLoading, setReportsLoading] = useState(false);

  const loadReports = useCallback(() => {
    let alive = true;
    setReportsLoading(true);
    void (async () => {
      const [r1, r2] = await Promise.allSettled([
        api.scriptInsights(accountId),
        api.qaPairs(accountId, 20),
      ]);
      if (!alive) return;
      if (r1.status === "fulfilled") {
        setInsights((r1.value ?? null) as Record<string, unknown>);
        setInsightsErr("");
      } else {
        setInsights(null);
        setInsightsErr(String(r1.reason));
      }
      if (r2.status === "fulfilled") {
        setPairs(Array.isArray(r2.value) ? r2.value : []);
        setPairsErr(Array.isArray(r2.value) ? "" : "问答对报告响应形状异常。");
      } else {
        setPairs([]);
        setPairsErr(String(r2.reason));
      }
      setReportsLoading(false);
    })();
    return () => {
      alive = false;
    };
  }, [accountId]);

  useEffect(() => loadReports(), [loadReports]);

  // ---- 聚类采纳面板 ----
  const [plan, setPlan] = useState<ClusterPlan | null>(null);
  const [variantChecks, setVariantChecks] = useState<boolean[]>([]);
  const [freshChecks, setFreshChecks] = useState<boolean[]>([]);
  const [hotwordChecks, setHotwordChecks] = useState<boolean[]>([]);
  const [qaQuestionById, setQaQuestionById] = useState<Record<string, string>>({});
  const [generating, setGenerating] = useState(false);
  const [adopting, setAdopting] = useState(false);
  const [clusterErr, setClusterErr] = useState("");
  const [okMsg, setOkMsg] = useState("");

  async function generatePlan() {
    setGenerating(true);
    setClusterErr("");
    setOkMsg("");
    try {
      // CLUSTER_LIMIT 口径：够一轮采纳、又不拖长本地 LLM 聚类时长。
      const data = await api.qaCluster(accountId, { limit: CLUSTER_LIMIT });
      const p = parsePlan(data);
      setPlan(p);
      setVariantChecks(p.variants.map(() => true)); // 变体+新词条默认全勾
      setFreshChecks(p.fresh.map(() => true));
      // 热词默认只勾 LLM 建议采纳（adopt）的项——reject 项仍展示，运营可自行覆盖。
      setHotwordChecks(p.hotwords.map((it) => hotwordVerdict(it) === "adopt"));
      // dry 计划的 variant 行只带目标词条 id（cluster_head_id）不带问法——
      // 拉词条列表建 id→问法映射供展示（失败降级只显示 id,不阻塞计划）。
      try {
        const rows = await api.listQaAll(accountId);
        if (Array.isArray(rows)) {
          setQaQuestionById(
            Object.fromEntries(rows.map((r) => [String(r.id ?? ""), String(r.question_text ?? "")])),
          );
        }
      } catch {
        setQaQuestionById({});
      }
    } catch (e) {
      setPlan(null);
      setClusterErr(String(e)); // 409 单飞冲突 / 503 LLM 失败等原样进 ErrorState
    } finally {
      setGenerating(false);
    }
  }

  async function adoptSelected() {
    if (!plan) return;
    setAdopting(true);
    setClusterErr("");
    const hotwordSel = buildHotwordSelect(hotwordChecks);
    const qaSel = buildSelect(variantChecks, freshChecks);
    try {
      await api.qaCluster(accountId, {
        apply: true,
        limit: CLUSTER_LIMIT, // 与 dry 同参：否则 CP 守卫按参数找不到计划缓存 → 409
        select: qaSel,
        hotword_select: hotwordSel,
      });
      setPlan(null); // 成功后清面板
      setVariantChecks([]);
      setFreshChecks([]);
      setHotwordChecks([]);
      setOkMsg(qaSel.length === 0 && hotwordSel.length > 0 ? HOTWORD_ADOPT_TIP : ADOPT_TIP);
      loadReports(); // 重拉 qaPairs（挖掘报告与词条库同步）
    } catch (e) {
      setClusterErr(String(e));
    } finally {
      setAdopting(false);
    }
  }

  const adoptableCount =
    variantChecks.filter(Boolean).length +
    freshChecks.filter(Boolean).length +
    hotwordChecks.filter(Boolean).length;

  const itemRowCls = "rounded-lg bg-muted/60 p-2 text-xs";

  return (
    <section className="card space-y-4">
      {reportsLoading && <LoadingState />}
      {!reportsLoading && (
        <>
          {insightsErr && <ErrorState message={insightsErr} />}
          {pairsErr && <ErrorState message={pairsErr} />}
          {insights && (
            <div className="rounded-lg border border-(--card-border) p-3">
              <span className="label">高频问题（对象议题聚合）</span>
              {(() => {
                const issues = Array.isArray(insights.top_issues)
                  ? (insights.top_issues as Record<string, unknown>[])
                  : [];
                if (issues.length === 0) return <p className="mt-1 text-xs muted">暂无数据。</p>;
                return (
                  <div className="mt-1 space-y-1">
                    {issues.map((it, i) => (
                      <p key={i} className="text-xs muted">
                        <span className="font-bold text-(--live-ink)">{i + 1}.</span>{" "}
                        {String(it.topic ?? "-")} · 提及 {Number(it.mentions ?? 0)} 次
                      </p>
                    ))}
                  </div>
                );
              })()}
              <p className="mt-2 text-[11px] muted">
                共 {Number(insights.calls ?? 0)} 通通话 · {Number(insights.turns_total ?? 0)} 轮对话
              </p>
            </div>
          )}
          {pairs.length > 0 && (
            <div className="rounded-lg border border-(--card-border) p-3">
              <span className="label">高频问答对挖掘（TOP {pairs.length}）</span>
              <div className="mt-1 space-y-2">
                {pairs.map((p, i) => (
                  <div key={i} className="rounded-lg bg-muted/60 p-2 text-xs">
                    <p className="font-medium">{String(p.question ?? "-")}</p>
                    <p className="mt-0.5 line-clamp-2 muted">{String(p.answer ?? "")}</p>
                    <p className="mt-0.5 muted">
                      {LANG_LABEL[String(p.lang ?? "")] ?? String(p.lang ?? "-")} · {Number(p.calls ?? 0)} 通命中
                    </p>
                  </div>
                ))}
              </div>
            </div>
          )}
          {!insights && !insightsErr && pairs.length === 0 && !pairsErr && (
            <EmptyState label="暂无学习报告数据。" />
          )}

          {/* AI 聚类采纳：qa 键才有操作（CP 端点同闸）,无键提示后仍可看报告 */}
          <div className="rounded-lg border border-(--card-border) p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="label">AI 聚类采纳</span>
              {canQa && (
                <div className="flex items-center gap-2">
                  {plan && (
                    <button
                      className="btn-primary text-xs"
                      disabled={adopting || generating || adoptableCount === 0}
                      onClick={adoptSelected}
                    >
                      {adopting ? "采纳中…" : `采纳选中（${adoptableCount}）`}
                    </button>
                  )}
                  <button
                    className="btn-ghost text-xs"
                    disabled={generating || adopting}
                    onClick={generatePlan}
                  >
                    {generating ? "聚类中…" : plan ? "重新生成聚类计划" : "生成聚类计划"}
                  </button>
                </div>
              )}
            </div>
            {!canQa && (
              <p className="mt-1 text-xs muted">需要问答库权限（qa）才能生成/采纳聚类计划。</p>
            )}
            {generating && (
              <p className="mt-1 text-xs text-(--live)">本地 LLM 聚类约需数十秒，请稍候…</p>
            )}
            {clusterErr && <div className="mt-2"><ErrorState message={clusterErr} /></div>}
            {okMsg && <p className="mt-2 text-xs text-emerald-600">{okMsg}</p>}
            {!generating && plan && canQa && (
              <div className="mt-2 space-y-3">
                <p className="text-[11px] muted">
                  {plan.model && `聚类模型：${plan.model} · `}
                  变体 {plan.variants.length} · 新词条 {plan.fresh.length} · 丢弃 {plan.junk.length}
                  {plan.hotwords.length > 0 && ` · 热词 ${plan.hotwords.length}`}
                  （丢弃组仅展示，不入库）
                </p>
                {plan.variants.length === 0 && plan.fresh.length === 0 && plan.junk.length === 0 && (
                  <EmptyState label="本轮没有可聚类的候选（需 ≥5 通通话的问答对）。" />
                )}
                {plan.variants.length > 0 && (
                  <div>
                    <span className="label">变体 → 并入既有词条</span>
                    <div className="mt-1 space-y-2">
                      {plan.variants.map((it, i) => (
                        <label key={i} className={`${itemRowCls} flex cursor-pointer items-start gap-2`}>
                          <input
                            type="checkbox"
                            className="mt-0.5 size-3.5 accent-(--live)"
                            checked={variantChecks[i] ?? true}
                            onChange={(e) =>
                              setVariantChecks((prev) => prev.map((v, j) => (j === i ? e.target.checked : v)))
                            }
                          />
                          <span className="min-w-0 flex-1">
                            <p className="font-medium">{variantQuestion(it) || "(空问法)"}</p>
                            <p className="mt-0.5 muted">
                              将并入：
                              {qaQuestionById[variantTarget(it)] || variantTargetQuestion(it) || "(目标词条)"}
                              {variantTarget(it) && ` · id ${variantTarget(it)}`}
                            </p>
                            <p className="mt-0.5 line-clamp-2 muted">继承答案：{variantAnswer(it)}</p>
                          </span>
                        </label>
                      ))}
                    </div>
                  </div>
                )}
                {plan.fresh.length > 0 && (
                  <div>
                    <span className="label">新词条</span>
                    <div className="mt-1 space-y-2">
                      {plan.fresh.map((it, i) => (
                        <label key={i} className={`${itemRowCls} flex cursor-pointer items-start gap-2`}>
                          <input
                            type="checkbox"
                            className="mt-0.5 size-3.5 accent-(--live)"
                            checked={freshChecks[i] ?? true}
                            onChange={(e) =>
                              setFreshChecks((prev) => prev.map((v, j) => (j === i ? e.target.checked : v)))
                            }
                          />
                          <span className="min-w-0 flex-1">
                            <p className="font-medium">{variantQuestion(it) || "(空问法)"}</p>
                            <p className="mt-0.5 line-clamp-2 muted">{variantAnswer(it)}</p>
                            <p className="mt-0.5 muted">
                              {LANG_LABEL[itemLang(it)] ?? (itemLang(it) || "-")}
                              {itemCalls(it) && ` · ${itemCalls(it)} 通命中`}
                            </p>
                          </span>
                        </label>
                      ))}
                    </div>
                  </div>
                )}
                {plan.junk.length > 0 && (
                  <div>
                    <span className="label">丢弃（只读）</span>
                    <div className="mt-1 space-y-1">
                      {plan.junk.map((it, i) => (
                        <p key={i} className={`${itemRowCls} muted`}>
                          {variantQuestion(it) || "(空问法)"} · {junkReason(it) || "junk"}
                        </p>
                      ))}
                    </div>
                  </div>
                )}
                <div>
                  <span className="label">热词沉淀（ASR 偏置）</span>
                  <p className="mt-0.5 text-[11px] muted">
                    挖掘自真实通话转写，采纳后下一通生效（写进本账号热词表，喂 ASR 识别偏置）。
                  </p>
                  {plan.hotwords.length === 0 ? (
                    <p className="mt-1 text-xs muted">本轮没有可沉淀的热词候选。</p>
                  ) : (
                    <div className="mt-1 space-y-2">
                      {plan.hotwords.map((it, i) => {
                        const ev = hotwordEvidence(it);
                        const verdict = hotwordVerdict(it);
                        return (
                          <label key={i} className={`${itemRowCls} flex cursor-pointer items-start gap-2`}>
                            <input
                              type="checkbox"
                              className="mt-0.5 size-3.5 accent-(--live)"
                              checked={hotwordChecks[i] ?? false}
                              onChange={(e) =>
                                setHotwordChecks((prev) => prev.map((v, j) => (j === i ? e.target.checked : v)))
                              }
                            />
                            <span className="min-w-0 flex-1">
                              <p className="font-medium">
                                {hotwordWord(it) || "(空词)"}
                                <span className="ml-1 rounded bg-muted px-1 text-[10px] muted">
                                  {LANG_LABEL[itemLang(it)] ?? (itemLang(it) || "-")}
                                </span>
                                {hotwordKind(it) && (
                                  <span className="ml-1 rounded bg-muted px-1 text-[10px] muted">
                                    {HOTWORD_KIND_LABEL[hotwordKind(it)] ?? hotwordKind(it)}
                                  </span>
                                )}
                                {hotwordFreq(it) && <span className="ml-1 muted">×{hotwordFreq(it)}</span>}
                              </p>
                              <p className="mt-0.5 muted">
                                {HOTWORD_VERDICT_LABEL[verdict] ?? (verdict || "-")}
                                {junkReason(it) && ` · ${junkReason(it)}`}
                              </p>
                              {ev && (
                                <p className="mt-0.5 line-clamp-2 muted">
                                  证据：{ev.raw}
                                  {ev.fixed && ev.fixed !== ev.raw && ` → ${ev.fixed}`}
                                </p>
                              )}
                            </span>
                          </label>
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        </>
      )}
    </section>
  );
}
