// 在库快答词条体检纯函数（L-③ 2026-09-20，语义镜像 CP control_plane/qa_drift.py）：
// 提案的采纳项组装、结果人话文案（幂等 / 待管理员补录音 / 补录音失败）、种类徽标、
// 报告摘要。都是纯函数，供 components/gap-mining.tsx 消费。
//
// 本文件必须保持零 import（test/qa-drift.test.mjs 按 flow-canvas.test.mjs 同款
// 单文件转译直载），类型自持——字段名与 GET /api/stats/qa-drift 出仓契约逐字对齐。

/** 提案种类：reanswer=改答案；retire=删词条。 */
export type QaDriftKind = "reanswer" | "retire";

/** GET /api/stats/qa-drift 的 proposals[] 元素（CP 契约形状）。 */
export type QaDriftProposal = {
  key: string;
  kind: QaDriftKind;
  reason: string;
  qa_id: string;
  question_text: string;
  current_answer: string;
  suggested_answer: string;
  lang: string;
  scope: string;
  occurrences: number;
  fired: number;
  repeats: number;
  hits: number;
  headline: string;
  detail: string;
};

export type QaDriftReport = {
  window_calls: number;
  entries_scanned: number;
  counts: { reanswer: number; retire: number };
  proposals: QaDriftProposal[];
  generated_at: number;
};

export type QaDriftAdoptItem = {
  key: string;
  kind: QaDriftKind;
  qa_id: string;
  text: string;
};

export type QaDriftAdoptResult = {
  key: string;
  kind: QaDriftKind;
  qa_id: string;
  created: boolean;
  needs_pregen: boolean;
  pregen: Record<string, unknown> | null;
  detail: string;
};

export type QaDriftAdoptResponse = {
  results: QaDriftAdoptResult[];
  adopted: number;
};

/** 提案去重键（与 CP proposal_key 同式：kind|qa_id）。 */
export function driftKey(kind: QaDriftKind, qaId: string): string {
  return `${kind}|${qaId || ""}`;
}

/** 种类徽标文案（运营看得懂的词，不用「漂移/失效」这类术语）。 */
export function driftKindLabel(kind: QaDriftKind): string {
  return kind === "retire" ? "建议删掉" : "建议改答案";
}

/** 报告摘要（驾驶舱大数/铃铛数字用）。 */
export function driftSummary(report: QaDriftReport | null): {
  windowCalls: number;
  scanned: number;
  reanswer: number;
  retire: number;
  total: number;
} {
  const r = report ?? ({} as QaDriftReport);
  const reanswer = Number(r.counts?.reanswer) || 0;
  const retire = Number(r.counts?.retire) || 0;
  return {
    windowCalls: Number(r.window_calls) || 0,
    scanned: Number(r.entries_scanned) || 0,
    reanswer,
    retire,
    total: reanswer + retire,
  };
}

/** 组装「改答案」采纳项（text=人工改后的答案；其余字段回传供服务端键校验）。 */
export function buildReanswerItem(p: QaDriftProposal, text: string): QaDriftAdoptItem {
  return { key: p.key, kind: "reanswer", qa_id: p.qa_id, text: String(text ?? "").trim() };
}

/** 组装「删词条」采纳项（无文本）。 */
export function buildRetireItem(p: QaDriftProposal): QaDriftAdoptItem {
  return { key: p.key, kind: "retire", qa_id: p.qa_id, text: "" };
}

/** 补录音失败了没？（pregen 是 detached 子进程，失败不该说成「正在录」） */
export function pregenFailed(pregen: Record<string, unknown> | null): boolean {
  if (!pregen) return false;
  const status = String(pregen.status ?? "");
  return status === "failed" || status === "error";
}

/**
 * 采纳结果 → 人话提示。四态互斥，按序判：
 * ① 幂等无写入；② 改了答案但没补录音（非 admin）；③ 补录音失败；④ 成功。
 */
export function driftResultMessage(res: QaDriftAdoptResult | undefined | null): string {
  if (!res) return "已处理。";
  if (!res.created) {
    return res.kind === "retire"
      ? "这条已经删过了，没有重复删。"
      : "这条的答案已经是这句了，没有重复改。";
  }
  if (res.kind === "retire") return "已删掉这条快速回答，以后不会再播了。";
  if (res.needs_pregen) {
    return "答案已改好。还需要管理员重新录音一次，电话里才会换上新答案。";
  }
  if (pregenFailed(res.pregen)) {
    return "答案已改好，但重新录音没成功，请到「快答库」页面手动补录一次。";
  }
  return "答案已改好，正在重新录音，录完就会在电话里生效。";
}
