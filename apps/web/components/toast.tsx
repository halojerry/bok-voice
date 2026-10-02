"use client";

// Toast 反馈系统（2026-10-02 UX 根因四·交互逻辑）：此前全站零反馈面——多数 mutation
// 成功无任何信号（对象保存/启停/转移静默翻面）、失败一半进 console、保存按钮可连点
// 双发 POST。本文件是唯一公共契约：
//   const toast = useToast();
//   toast.success("已保存");            // 3.5s 自动消失
//   toast.error(String(e));             // 6s 自动消失，点击可提前关
//   toast.info("…");                    // 3.5s
// 手写零新依赖（tw-animate-css 的 animate-in 类已在 globals.css 引入）；
// Provider 挂在 app-shell（全站单例），消费点禁止自建平行反馈面。
import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";
import { CheckCircle2, CircleAlert, Info } from "lucide-react";

export type ToastKind = "success" | "error" | "info";
export type ToastApi = {
  success: (msg: string) => void;
  error: (msg: string) => void;
  info: (msg: string) => void;
};

type ToastItem = { id: number; kind: ToastKind; msg: string };

const ToastCtx = createContext<ToastApi | null>(null);

const AUTO_DISMISS_MS: Record<ToastKind, number> = {
  success: 3500,
  info: 3500,
  error: 6000,
};

const KIND_STYLE: Record<ToastKind, string> = {
  success: "border-emerald-500/40 text-emerald-700 dark:text-emerald-400",
  error: "border-red-500/40 text-red-700 dark:text-red-400",
  info: "border-(--card-border) text-foreground",
};

const KIND_ICON: Record<ToastKind, React.ReactNode> = {
  success: <CheckCircle2 className="h-4 w-4 shrink-0" aria-hidden="true" />,
  error: <CircleAlert className="h-4 w-4 shrink-0" aria-hidden="true" />,
  info: <Info className="h-4 w-4 shrink-0" aria-hidden="true" />,
};

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const seqRef = useRef(0);

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const push = useCallback(
    (kind: ToastKind, msg: string) => {
      seqRef.current += 1;
      const id = seqRef.current;
      // 上限 4 条：老的后进先出挤掉，防错误风暴刷屏。
      setItems((prev) => [...prev.slice(-3), { id, kind, msg }]);
      window.setTimeout(() => dismiss(id), AUTO_DISMISS_MS[kind]);
    },
    [dismiss],
  );

  const api = useMemo<ToastApi>(
    () => ({
      success: (msg) => push("success", msg),
      error: (msg) => push("error", msg),
      info: (msg) => push("info", msg),
    }),
    [push],
  );

  return (
    <ToastCtx.Provider value={api}>
      {children}
      {/* aria-live:屏幕阅读器播报;pointer-events-none 容器不挡操作,单条可点关闭。 */}
      <div aria-live="polite" className="pointer-events-none fixed bottom-4 right-4 z-[100] flex w-80 flex-col gap-2">
        {items.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => dismiss(t.id)}
            className={`pointer-events-auto flex items-start gap-2 rounded-xl border bg-(--card) px-3 py-2 text-left text-xs shadow-lg animate-in fade-in slide-in-from-bottom-2 ${KIND_STYLE[t.kind]}`}
          >
            {KIND_ICON[t.kind]}
            <span className="min-w-0 break-words leading-5">{t.msg}</span>
          </button>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastCtx);
  if (!ctx) {
    // Provider 缺席（异常挂载形态）不炸消费点：退化 no-op 并留痕。
    return {
      success: () => console.warn("[toast] provider missing"),
      error: (m) => console.warn("[toast] provider missing:", m),
      info: () => console.warn("[toast] provider missing"),
    };
  }
  return ctx;
}
