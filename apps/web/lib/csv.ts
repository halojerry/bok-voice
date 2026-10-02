// CSV 纯函数（表格导入 2026-09-25）：解析 / 转义 / 生成示例模板下载，零 import
// （test/csv.test.mjs 沿 flow-canvas.test.mjs 同款装配：单文件 tsc 转译直载）。
//
// 契约：
//  - parseCsv：RFC4180 口味——双引号包裹字段内可含 逗号/换行/转义双引号（""→"）；
//    CRLF/LF/CR 均认行界；UTF-8 BOM（Excel 导出常态）剥掉；全空行丢弃。
//    引号出现在字段中间时按字面保留（宽容，不报错）。
//  - 注释行：首格以「#」开头（trim 后）——示例模板的列说明行，导入时由调用方跳过
//    （isCommentRow）。只认文件级行首，不碰引号字段内部的「#」。
//  - csvEscape/toCsv：逗号/引号/换行任一在场即整字段加引号；行界 CRLF（Excel 友好）；
//    downloadCsv 额外前置 BOM，保证 Excel 双开不乱码。

/** 剥 UTF-8 BOM（Excel 导出的 CSV 首字节常带）。 */
export function stripBom(text: string): string {
  const s = String(text ?? "");
  return s.charCodeAt(0) === 0xfeff ? s.slice(1) : s;
}

/**
 * CSV 文本 → 二维表（纯函数）。引号字段内逗号/换行/双引号转义全支持；
 * 返回值已剔除全空行（单元格全空白）。末行无换行符也照收。
 */
export function parseCsv(text: string): string[][] {
  const src = stripBom(text);
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  const pushField = () => {
    row.push(field.trim()); // 单元格 trim：Excel/人工粘贴常态带首尾空白，导入面一律净形
    field = "";
  };
  const pushRow = () => {
    pushField();
    rows.push(row);
    row = [];
  };
  for (let i = 0; i < src.length; i++) {
    const ch = src[i];
    if (inQuotes) {
      if (ch === '"') {
        if (src[i + 1] === '"') {
          field += '"'; // 转义双引号
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        field += ch; // 引号内的逗号/换行按字面收
      }
      continue;
    }
    if (ch === '"') {
      inQuotes = true;
      continue;
    }
    if (ch === ",") {
      pushField();
      continue;
    }
    if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && src[i + 1] === "\n") i++; // CRLF 合一行界
      pushRow();
      continue;
    }
    field += ch;
  }
  if (field !== "" || row.length > 0) pushRow(); // 文件不以换行结尾：末行照收
  return rows.filter((r) => !r.every((c) => String(c).trim() === ""));
}

/** 注释行（示例模板的列说明）：首格 trim 后以「#」开头。调用方在导入时跳过。 */
export function isCommentRow(row: string[]): boolean {
  return String(row[0] ?? "").trimStart().startsWith("#");
}

/** 单字段转义：含 逗号/引号/换行 即加引号，内部引号翻倍。 */
export function csvEscape(field: string): string {
  const s = String(field ?? "");
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** 二维表 → CSV 文本（行界 CRLF；bom=true 前置 BOM，Excel 双开不乱码）。 */
export function toCsv(rows: string[][], opts?: { bom?: boolean }): string {
  const body = rows.map((r) => r.map(csvEscape).join(",")).join("\r\n") + "\r\n";
  return (opts?.bom ? "\uFEFF" : "") + body;
}

/** 生成示例模板 CSV 并触发浏览器下载（BOM + CRLF；三个导入面共用）。 */
export function downloadCsv(filename: string, rows: string[][]): void {
  const blob = new Blob([toCsv(rows, { bom: true })], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** 逗号/顿号/分号/换行 分隔 → 去空、去重、保序的词条数组（关键词/多值格通用）。 */
export function splitListCell(text: string): string[] {
  const out: string[] = [];
  for (const part of String(text ?? "").split(/[,，、;；\n\r]+/)) {
    const t = part.trim();
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}

/** 布尔格（是/否）：是/true/1/y/yes → true；否/false/0/n/no/空 → false；其它 → null（调用方报错）。 */
export function parseBoolCell(text: string, fallback: boolean): boolean | null {
  const t = String(text ?? "").trim().toLowerCase();
  if (t === "") return fallback;
  if (["是", "true", "1", "y", "yes"].includes(t)) return true;
  if (["否", "false", "0", "n", "no"].includes(t)) return false;
  return null;
}
