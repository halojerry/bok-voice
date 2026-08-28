"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode, SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

const I = ({ children, className = "", ...rest }: IconProps) => (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.7}
    strokeLinecap="round"
    strokeLinejoin="round"
    className={`h-5 w-5 ${className}`}
    {...rest}
  >
    {children}
  </svg>
);

const NAV: { href: string; label: string; icon: ReactNode }[] = [
  { href: "/", label: "总览", icon: <I><path d="M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm10 0h6v-9h-6v9Zm0-16v5h6V4h-6Z" /></I> },
  { href: "/objects", label: "对象", icon: <I><circle cx="12" cy="8" r="4" /><path d="M4 20c0-3.3 3.6-6 8-6s8 2.7 8 6" /></I> },
  { href: "/calls", label: "会话", icon: <I><path d="M4 6c0 6.6 5.4 12 12 12h4v-2l-3-1-1 2c-1.6-.8-3-2-4.1-3.9L14 8l-2-1V4H6c-1.1 0-2 .9-2 2Z" /></I> },
  { href: "/calls/new", label: "新建通话", icon: <I><path d="M12 5v14M5 12h14" /></I> },
  { href: "/knowledge", label: "知识库", icon: <I><path d="M4 5a2 2 0 0 1 2-2h7l2 2h5a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5Z" /><path d="M8 10h8M8 14h5" /></I> },
  { href: "/personas", label: "人设", icon: <I><path d="M3 10v4a2 2 0 0 0 2 2h2l5 4V6L7 10H5a2 2 0 0 0-2 2Z" /><path d="M16 8a3 3 0 0 1 0 8" /></I> },
  { href: "/supervisor", label: "主管台", icon: <I><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 9h10M7 13h6" /></I> },
  { href: "/reports", label: "报表", icon: <I><path d="M4 20V10M10 20V4M16 20v-7M21 20H3" /></I> },
  { href: "/settings", label: "设置", icon: <I><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1A2 2 0 1 1 7 4.3l.1.1a1.7 1.7 0 0 0 1.9.3h.1a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9v.1a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z" /></I> },
];

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="flex min-h-screen">
      {/* 侧边栏 */}
      <aside className="flex w-60 shrink-0 flex-col border-r border-[var(--card-border)] bg-[var(--card)] px-3 py-5">
        <Link href="/" className="mb-7 flex items-center gap-2.5 px-3">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-[var(--accent)] font-mono text-sm font-bold text-[var(--accent-ink)]">
            B
          </span>
          <span className="text-[15px] font-medium tracking-tight">Bok Voice</span>
        </Link>

        <nav className="flex flex-col gap-0.5">
          {NAV.map((item) => {
            const active = pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`group relative flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition ${
                  active
                    ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                    : "text-[var(--muted)] hover:bg-[var(--card-soft)] hover:text-[var(--foreground)]"
                }`}
              >
                {active && (
                  <span className="absolute left-0 top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-full bg-[var(--accent)]" />
                )}
                <span className="flex h-5 w-5 items-center justify-center text-current">{item.icon}</span>
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className="mt-auto px-3">
          <div className="rounded-xl border border-[var(--card-border)] bg-[var(--card-soft)] p-3 text-xs text-[var(--muted)]">
            <p className="eyebrow">账号</p>
            <p className="mt-1.5">acc-001</p>
            <p className="mt-1">角色：操作员</p>
            <p className="mt-2 flex items-center gap-1.5">
              <span className="dot bg-[var(--accent)]" /> Agent 运行中
            </p>
          </div>
        </div>
      </aside>

      {/* 主区域 */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between border-b border-[var(--card-border)] px-6">
          <span className="eyebrow">实时语音客服工作台</span>
          <div className="flex items-center gap-4">
            <span className="text-xs text-[var(--muted)]">本地优先 · LiveKit</span>
            <span className="inline-flex items-center gap-1.5 text-xs text-[var(--muted)]">
              <span className="dot bg-[var(--accent)]" />
              <span className="font-mono">v0.1.0</span>
            </span>
          </div>
        </header>
        <main className="min-w-0 flex-1 p-6 lg:p-8">{children}</main>
      </div>
    </div>
  );
}
