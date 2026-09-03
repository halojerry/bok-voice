"use client";

import { AccountProvider, useAccount } from "@/components/account-context";
import { StageHeader } from "@/components/StageHeader";
import { friendlyErrorText } from "@/lib/api-ready";

function StatusBadge() {
  const { health, settingsLoading } = useAccount();
  if (settingsLoading) return <span className="font-mono">loading</span>;
  if (health === false) return <span className="text-xs text-red-300">控制面离线</span>;
  return (
    <span className="hidden items-center gap-2 text-xs text-[var(--stage-muted)] sm:inline-flex">
      <span className="h-2 w-2 rounded-full bg-[var(--stage-value)]" />
      <span className="font-mono">v0.1.0</span>
    </span>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <AccountProvider>
      <div className="stage-shell min-h-screen w-full">
        <StageHeader status={<StatusBadge />} />
        <main className="mx-auto w-full max-w-7xl px-6 pb-10 pt-2 lg:px-10">{children}</main>
      </div>
    </AccountProvider>
  );
}

export function LoadingState({ label = "加载中…" }: { label?: string }) {
  return <p className="text-sm text-[var(--muted)]">{label}</p>;
}

export function EmptyState({ label = "暂无数据" }: { label?: string }) {
  return <p className="text-sm text-[var(--muted)]">{label}</p>;
}

export function ErrorState({ message }: { message: string }) {
  return (
    <p className="rounded-lg bg-red-500/10 p-3 text-sm text-red-300">
      {friendlyErrorText(message)}
    </p>
  );
}
