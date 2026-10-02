"use client";

// 表格导入（2026-09-25）：CSV 上传 → 解析 → 预览（N 行将导入 / M 行跳过及原因）→ 确认 →
// 调用方写入 → 结果摘要。三个入口共用：/qa 快答条目、studio 主流程话术（整表替换）、
// studio 意图（整表替换）。CSV 解析/转义/示例生成纯函数在 lib/csv.ts；本组件只管
// 交互壳与预览，不碰 API、不感知业务形状（泛型 T 由调用方定义，parseRow/onImport 注入）。

import { useEffect, useMemo, useState } from "react";
import { downloadCsv, isCommentRow, parseCsv } from "@/lib/csv";

/** 列契约：表头识别 + 预览列头 + 示例模板的注释行说明。 */
export type ImportColumn = { key: string; label: string; hint?: string };

/**
 * 单行解析结果。统一形状（非判别联合）：本仓 tsconfig strict=false，泛型参数的
 * 判别联合在负向分支不被收窄（TS2614 实测）——ok/data/reason 三字段直读零收窄需求。
 * 构造走 rowOk/rowSkip，invariant：ok=true ⇒ data 非 null、reason 空。
 */
export type ParsedRow<T> = { ok: boolean; data: T | null; reason: string };

/** 解析成功行。 */
export function rowOk<T>(data: T): ParsedRow<T> {
  return { ok: true, data, reason: "" };
}

/** 跳过行（reason=给人话的跳过原因，预览逐行展示）。data 恒 null——仓内 strict=false
 * 下 `T | null` 的 null 被抹掉，这里用断言落 null（运行时形状以本文件不变量为准）。 */
export function rowSkip<T>(reason: string): ParsedRow<T> {
  return { ok: false, data: null as T, reason };
}

/** 写入结果摘要（调用方在 onImport 里逐行/单发写入后回报）。 */
export type ImportResult = { done: number; failed: number; errors: string[] };

/** 预览表最多渲染的行数（超出折叠计数，解析与导入不受影响）。 */
const PREVIEW_LIMIT = 50;

/** 结果摘要最多列出的错误条数。 */
const RESULT_ERROR_LIMIT = 8;

/**
 * 示例模板 CSV 行：注释行（列说明，导入时忽略）+ 表头 + 示例数据。
 * 三个入口的「下载示例模板」按钮与组件内下载共用，保证表头识别与示例同源。
 */
export function buildExampleCsvRows(
  columns: ImportColumn[],
  exampleRows: string[][],
): string[][] {
  const comment =
    "# 列说明：" +
    columns.map((c) => `${c.label}${c.hint ? `（${c.hint}）` : ""}`).join("；") +
    "。本注释行导入时自动忽略。";
  return [[comment], columns.map((c) => c.label), ...exampleRows];
}

type RowView<T> = { cells: string[]; parsed: ParsedRow<T> };

export default function TableImport<T>(props: {
  open: boolean;
  title: string;
  /** 一句话说明导入语义（如「整表替换」/「逐条新增」），预览页顶部展示。 */
  description: string;
  columns: ImportColumn[];
  /** 示例数据行（不含表头与注释行）。 */
  exampleRows: string[][];
  exampleFilename: string;
  /** 单行解析（纯函数；调用方闭包捕获业务上下文如 stepCount/词条表）。prev=本文件里
   *  已解析的先前各行（跨行去重用：意图 ID/显示名重复在预览期即标跳过）。 */
  parseRow: (row: string[], prev: ParsedRow<T>[]) => ParsedRow<T>;
  /** 确认后执行写入；返回结果摘要（QA=逐行 POST；流程/意图=组装单键保存）。 */
  onImport: (rows: T[]) => Promise<ImportResult>;
  /** 覆盖型导入的二次确认文案（入参=将导入行数）；不给则只走预览确认。 */
  confirmText?: (okCount: number) => string;
  onClose: () => void;
}) {
  const { open, title, description, columns, exampleRows, exampleFilename, parseRow, onImport, onClose } = props;
  const [stage, setStage] = useState<"pick" | "preview" | "result">("pick");
  const [rows, setRows] = useState<RowView<T>[]>([]);
  const [note, setNote] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ImportResult | null>(null);

  // 每次打开重置到选文件态（父组件条件渲染亦可，这里双保险）。
  useEffect(() => {
    if (open) {
      setStage("pick");
      setRows([]);
      setNote("");
      setErr("");
      setBusy(false);
      setResult(null);
    }
  }, [open]);

  const okRows = useMemo((): T[] => {
    const out: T[] = [];
    for (const r of rows) if (r.parsed.ok && r.parsed.data !== null) out.push(r.parsed.data);
    return out;
  }, [rows]);
  const skipCount = rows.length - okRows.length;

  async function handleFile(file: File) {
    setErr("");
    setNote("");
    let text = "";
    try {
      text = await file.text(); // UTF-8；BOM 由 parseCsv 剥
    } catch (e) {
      setErr(`读取文件失败：${String(e)}`);
      return;
    }
    const all = parseCsv(text).filter((r) => !isCommentRow(r));
    if (all.length === 0) {
      setErr("文件里没有可导入的行。");
      return;
    }
    // 表头识别：首行逐列等于列契约的表头名才认；否则全按数据行解析（提示一句）。
    const isHeader = columns.every((c, i) => String(all[0][i] ?? "").trim() === c.label);
    const dataRows = isHeader ? all.slice(1) : all;
    if (dataRows.length === 0) {
      setErr("文件里只有表头，没有数据行。");
      return;
    }
    setNote(isHeader ? "" : "未识别到表头行，已按默认列序把全部行当数据处理。");
    const parsed: RowView<T>[] = [];
    for (const cells of dataRows) {
      parsed.push({ cells, parsed: parseRow(cells, parsed.map((r) => r.parsed)) });
    }
    setRows(parsed);
    setResult(null);
    setStage("preview");
  }

  async function confirmImport() {
    if (okRows.length === 0 || busy) return;
    if (props.confirmText && !window.confirm(props.confirmText(okRows.length))) return;
    setBusy(true);
    setErr("");
    try {
      const res = await onImport(okRows);
      setResult(res);
      setStage("result");
    } catch (e) {
      setErr(`导入失败：${String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  if (!open) return null;

  const cellText = (v: string) => {
    const t = String(v ?? "");
    return t.length > 40 ? `${t.slice(0, 40)}…` : t.replace(/\n/g, "⏎");
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-foreground/30 p-4">
      <div className="w-full max-w-3xl space-y-3 rounded-xl border border-(--card-border) bg-background p-4 shadow-lg">
        <div className="flex items-center justify-between">
          <span className="label">{title}</span>
          <button className="btn-ghost px-2 py-0.5 text-xs" disabled={busy} onClick={onClose}>
            关闭 ✕
          </button>
        </div>

        {stage === "pick" && (
          <div className="space-y-3">
            <p className="text-xs muted">{description}</p>
            <div className="rounded-lg border border-dashed border-(--card-border) p-4 text-center">
              <input
                type="file"
                accept=".csv,text/csv,text/plain"
                disabled={busy}
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) void handleFile(f);
                  e.target.value = ""; // 同名文件二次选择也能触发 onChange
                }}
              />
              <p className="mt-2 text-[11px] muted">UTF-8 CSV（Excel 另存为 CSV 即可，兼容带 BOM）；列见示例模板。</p>
            </div>
            <div className="flex items-center gap-2">
              <button
                className="btn-ghost text-xs"
                onClick={() => downloadCsv(exampleFilename, buildExampleCsvRows(columns, exampleRows))}
              >
                下载示例模板
              </button>
              <span className="text-[11px] muted">列：{columns.map((c) => c.label).join(" / ")}</span>
            </div>
            {err && <p className="text-xs text-red-600">{err}</p>}
          </div>
        )}

        {stage === "preview" && (
          <div className="space-y-3">
            <p className="text-xs">
              共 {rows.length} 行：<b className="text-emerald-700">{okRows.length} 行将导入</b>
              {skipCount > 0 && <b className="text-amber-700">；{skipCount} 行跳过</b>}
              <span className="ml-2 text-[11px] muted">（{description}）</span>
            </p>
            {note && <p className="text-[11px] text-amber-700">{note}</p>}
            <div className="max-h-96 overflow-auto rounded-lg border border-(--card-border)">
              <table className="w-full text-left text-xs">
                <thead className="sticky top-0 bg-(--card)">
                  <tr className="border-b border-(--card-border) muted">
                    <th className="px-2 py-1.5 font-medium">行</th>
                    {columns.map((c) => (
                      <th key={c.key} className="px-2 py-1.5 font-medium" title={c.hint}>{c.label}</th>
                    ))}
                    <th className="px-2 py-1.5 font-medium">状态</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.slice(0, PREVIEW_LIMIT).map((r, i) => {
                    const reason = r.parsed.reason;
                    return (
                      <tr key={i} className="border-b border-(--card-border)/60 align-top">
                        <td className="px-2 py-1.5 muted">{i + 1}</td>
                        {columns.map((c, ci) => (
                          <td key={c.key} className="max-w-48 px-2 py-1.5">
                            <span className={r.parsed.ok ? "" : "muted"} title={String(r.cells[ci] ?? "")}>
                              {cellText(String(r.cells[ci] ?? ""))}
                            </span>
                          </td>
                        ))}
                        <td className="px-2 py-1.5">
                          {r.parsed.ok ? (
                            <span className="text-emerald-700">✓ 将导入</span>
                          ) : (
                            <span className="text-amber-700" title={reason}>跳过：{reason}</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {rows.length > PREVIEW_LIMIT && (
                <p className="px-2 py-1.5 text-[11px] muted">共 {rows.length} 行，预览前 {PREVIEW_LIMIT} 行（导入按全部行执行）。</p>
              )}
            </div>
            {err && <p className="text-xs text-red-600">{err}</p>}
            <div className="flex items-center justify-between gap-2">
              <button className="btn-ghost text-xs" disabled={busy} onClick={() => setStage("pick")}>
                ← 重选文件
              </button>
              <div className="flex items-center gap-2">
                <button
                  className="btn-ghost text-xs"
                  onClick={() => downloadCsv(exampleFilename, buildExampleCsvRows(columns, exampleRows))}
                >
                  下载示例模板
                </button>
                <button className="btn-primary text-xs" disabled={busy || okRows.length === 0} onClick={() => void confirmImport()}>
                  {busy ? "导入中…" : `确认导入 ${okRows.length} 行`}
                </button>
              </div>
            </div>
          </div>
        )}

        {stage === "result" && result && (
          <div className="space-y-3">
            <p className="text-sm">
              {result.failed === 0 ? (
                <span className="text-emerald-700">导入完成：成功 {result.done} 条。</span>
              ) : (
                <span>
                  导入完成：<span className="text-emerald-700">成功 {result.done} 条</span>
                  ，<span className="text-amber-700">失败 {result.failed} 条</span>。
                </span>
              )}
            </p>
            {result.errors.length > 0 && (
              <div className="space-y-1 rounded-lg bg-muted/60 p-3">
                {result.errors.slice(0, RESULT_ERROR_LIMIT).map((e, i) => (
                  <p key={i} className="text-xs text-amber-700">{e}</p>
                ))}
                {result.errors.length > RESULT_ERROR_LIMIT && (
                  <p className="text-[11px] muted">…等共 {result.errors.length} 条失败。</p>
                )}
              </div>
            )}
            <div className="flex justify-end">
              <button className="btn-primary text-xs" onClick={onClose}>完成</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
