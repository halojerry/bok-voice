"use client";

import { useEffect, useState } from "react";

type ServiceStatus = { name: string; port: number; up: boolean };
type HealthReport = { app_data_dir: string; services: ServiceStatus[] };

type TauriInvoke = (cmd: string, args?: unknown) => Promise<unknown>;

function invoke(): TauriInvoke | null {
  if (typeof window === "undefined") return null;
  return (window as unknown as { __TAURI_INTERNALS__?: { invoke: TauriInvoke } }).__TAURI_INTERNALS__?.invoke ?? null;
}

async function health(): Promise<HealthReport | null> {
  const fn = invoke();
  if (!fn) return null;
  return (await fn("health")) as HealthReport;
}

async function startServices(): Promise<string> {
  const fn = invoke();
  if (!fn) return "no-desktop";
  return (await fn("start")) as string;
}

async function stopServices(): Promise<string> {
  const fn = invoke();
  if (!fn) return "no-desktop";
  return (await fn("stop")) as string;
}

async function openLogs(): Promise<string> {
  const fn = invoke();
  if (!fn) return "no-desktop";
  return (await fn("open_logs")) as string;
}

export default function DesktopStatus() {
  const [report, setReport] = useState<HealthReport | null>(null);
  const [reason, setReason] = useState<string>("");

  useEffect(() => {
    if (!invoke()) {
      setReason("当前为浏览器模式，未运行在桌面壳内。");
      return;
    }
    const refresh = () => health().then(setReport).catch(() => setReason("无法读取服务状态"));
    refresh();
    const timer = setInterval(refresh, 4000);
    return () => clearInterval(timer);
  }, []);

  if (reason) {
    return (
      <div className="rounded-xl border border-[var(--card-border)] bg-[var(--card)] p-4 text-sm text-muted-foreground">
        {reason}
      </div>
    );
  }
  if (!report) {
    return <div className="rounded-xl border border-[var(--card-border)] p-4 text-sm text-muted-foreground">正在读取桌面服务状态…</div>;
  }

  const up = report.services.filter((s) => s.up).length;
  return (
    <div className="rounded-xl border border-[var(--card-border)] bg-[var(--card)] p-4">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold">本机桌面服务</h3>
        <span className="text-xs text-muted-foreground">
          {up}/{report.services.length} 在线
        </span>
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {report.services.map((s) => (
          <div
            key={s.name}
            className="flex items-center justify-between rounded-lg border border-[var(--card-border)] px-3 py-2 text-xs"
          >
            <span>{s.name}</span>
            <span className={s.up ? "text-emerald-400" : "text-rose-400"}>
              {s.up ? "UP" : "DOWN"}
            </span>
          </div>
        ))}
      </div>
      <p className="mt-2 truncate text-xs text-muted-foreground">数据目录：{report.app_data_dir}</p>
      <div className="mt-3 flex gap-2">
        <button className="rounded-lg border border-[var(--card-border)] px-3 py-1.5 text-xs hover:border-[var(--accent)]" onClick={() => startServices()}>
          启动服务
        </button>
        <button className="rounded-lg border border-[var(--card-border)] px-3 py-1.5 text-xs hover:border-[var(--accent)]" onClick={() => stopServices()}>
          停止服务
        </button>
        <button className="rounded-lg border border-[var(--card-border)] px-3 py-1.5 text-xs hover:border-[var(--accent)]" onClick={() => openLogs()}>
          打开日志目录
        </button>
      </div>
    </div>
  );
}
