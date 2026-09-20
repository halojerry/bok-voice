"use client";

// 变量按钮化插入（2026-09-20 重设计第 10 点）：运营不认识 `{}` 语法——
// 在话术文本框上方给出可点变量按钮，点击把 `{变量}` 插到光标处（无光标=追加尾部）。
// 目录单一事实源 = lib/var-panel PLACEHOLDER_CATALOG（与 agent flow.py object_vars 逐键对齐）。

import { useRef } from "react";
import { PLACEHOLDER_CATALOG } from "@/lib/var-panel";

/** 按钮面：每条目录取首个键展示（title=全部别名），插值恒用该键（合法占位符）。 */
const VAR_BUTTONS = PLACEHOLDER_CATALOG.map((e) => ({
  insert: e.keys[0],
  aliases: e.keys,
}));

/**
 * 带变量按钮行的受控 textarea。
 * - 插入点=当前光标（selectionStart..End 区间替换）；受控刷新后用 rAF 恢复光标到插入段之后。
 * - 通用透传 className/placeholder/rows，替换现有 textarea 只需包一层。
 */
export function VarTextarea(props: {
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  className?: string;
  placeholder?: string;
  /** 变量行提示文案（默认短说明）。 */
  hint?: string;
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const caretRef = useRef<number | null>(null);

  function insert(token: string) {
    if (props.disabled) return;
    const el = ref.current;
    const value = props.value ?? "";
    if (!el) {
      props.onChange(`${value}${token}`);
      return;
    }
    const start = el.selectionStart ?? value.length;
    const end = el.selectionEnd ?? start;
    const next = value.slice(0, start) + token + value.slice(end);
    caretRef.current = start + token.length;
    props.onChange(next);
  }

  // 受控值刷新后把光标放回插入点（跳过这一步光标会落到串尾）。
  function restoreCaret() {
    const pos = caretRef.current;
    if (pos === null) return;
    caretRef.current = null;
    requestAnimationFrame(() => {
      const el = ref.current;
      if (el) el.setSelectionRange(pos, pos);
    });
  }

  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-1">
        <span className="text-[10px] muted">{props.hint ?? "点变量插入到光标处："}</span>
        {VAR_BUTTONS.map((b) => (
          <button
            key={b.insert}
            type="button"
            disabled={props.disabled}
            title={`别名：${b.aliases.map((k) => `{${k}}`).join(" / ")}`}
            className="rounded-full border border-(--card-border) px-1.5 py-0.5 font-mono text-[10px] transition hover:border-(--live) hover:text-(--live-ink) disabled:opacity-50"
            onClick={() => insert(`{${b.insert}}`)}
          >
            {b.insert}
          </button>
        ))}
      </div>
      <textarea
        ref={ref}
        className={props.className}
        placeholder={props.placeholder}
        value={props.value}
        disabled={props.disabled}
        onChange={(e) => props.onChange(e.target.value)}
        onSelect={restoreCaret}
      />
    </div>
  );
}
