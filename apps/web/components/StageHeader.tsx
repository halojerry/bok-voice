"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

const NAV = [
  { href: "/calls", label: "会话" },
  { href: "/objects", label: "对象" },
  { href: "/knowledge", label: "知识库" },
  { href: "/personas", label: "人设" },
  { href: "/templates", label: "话术" },
  { href: "/translate", label: "同传" },
  { href: "/supervisor", label: "主管台" },
  { href: "/reports", label: "报表" },
  { href: "/settings", label: "设置" },
];

/**
 * 全站统一顶部导航（取自首页舞台顶栏）。所有页面共用，保证整套版式一致。
 * @param status 右侧状态徽标；默认显示版本号。
 */
export function StageHeader({ status }: { status?: ReactNode }) {
  const pathname = usePathname();
  const isActive = (href: string) =>
    href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);

  return (
    <header className="mx-auto flex h-16 w-full max-w-7xl items-center justify-between gap-4 px-6 lg:px-10">
      <Link href="/" className="flex shrink-0 items-center gap-2.5">
        <span className="flex h-9 w-9 items-center justify-center rounded bg-[var(--stage-value)] font-mono text-base font-bold text-[#01191c]">
          B
        </span>
        <span className="text-[15px] font-medium tracking-tight">Bok Voice</span>
      </Link>

      <nav className="hidden items-center gap-7 text-sm text-[var(--stage-muted)] md:flex">
        {NAV.map((n) => (
          <Link
            key={n.href}
            href={n.href}
            className={`transition hover:text-[var(--foreground)] ${
              isActive(n.href) ? "text-[var(--stage-value)]" : ""
            }`}
          >
            {n.label}
          </Link>
        ))}
      </nav>

      <div className="flex shrink-0 items-center gap-4">
        <span className="hidden items-center gap-2 text-xs text-[var(--stage-muted)] sm:inline-flex">
          {status ?? (
            <>
              <span className="h-2 w-2 rounded-full bg-[var(--stage-value)]" />
              <span className="font-mono">v0.1.0</span>
            </>
          )}
        </span>
        <Link href="/calls/new" className="stage-btn-primary">
          进入工作台
        </Link>
      </div>
    </header>
  );
}
