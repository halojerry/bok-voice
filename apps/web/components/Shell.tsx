"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV = [
  { href: "/", label: "总览", icon: "◉" },
  { href: "/objects", label: "对象", icon: "👤" },
  { href: "/calls/new", label: "通话", icon: "📞" },
  { href: "/knowledge", label: "知识库", icon: "📚" },
  { href: "/personas", label: "人设", icon: "🎙️" },
  { href: "/supervisor", label: "主管台", icon: "🗂️" },
  { href: "/reports", label: "报表", icon: "📈" },
  { href: "/settings", label: "设置", icon: "⚙️" },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  return (
    <div className="flex min-h-screen">
      <aside className="w-56 shrink-0 border-r border-[var(--card-border)] bg-[var(--card)] px-3 py-5">
        <div className="mb-6 flex items-center gap-2 px-2">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-[var(--accent)] text-black font-bold">B</span>
          <span className="font-semibold">Bok Voice</span>
        </div>
        <nav className="flex flex-col gap-1">
          {NAV.map((item) => {
            const active = pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`flex items-center gap-3 rounded-xl px-3 py-2 text-sm ${
                  active ? "bg-white/10 text-white" : "text-[var(--muted)] hover:bg-white/5 hover:text-white"
                }`}
              >
                <span className="w-5 text-center">{item.icon}</span>
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div className="mt-8 rounded-xl border border-[var(--card-border)] p-3 text-xs text-[var(--muted)]">
          <p>账号：acc-001</p>
          <p className="mt-1">角色：操作员</p>
          <p className="mt-1 flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full bg-emerald-400" /> Agent 运行中
          </p>
        </div>
      </aside>
      <div className="flex-1">
        <header className="flex h-14 items-center justify-between border-b border-[var(--card-border)] px-6">
          <span className="text-sm text-[var(--muted)]">实时语音客服工作台</span>
          <span className="text-xs text-[var(--muted)]">本地优先 · LiveKit</span>
        </header>
        <main className="p-6">{children}</main>
      </div>
    </div>
  );
}
