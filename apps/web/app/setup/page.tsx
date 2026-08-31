"use client";

import { useCallback, useEffect, useState } from "react";
import { api, SetupStatus } from "@/lib/api";

type TauriInvoke = (cmd: string, args?: unknown) => Promise<unknown>;

function tauriInvoke(): TauriInvoke | null {
  if (typeof window === "undefined") return null;
  return (window as unknown as { __TAURI_INTERNALS__?: { invoke: TauriInvoke } }).__TAURI_INTERNALS__?.invoke ?? null;
}

export default function SetupPage() {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const invoke = tauriInvoke();
      if (invoke) {
        const raw = (await invoke("setup_status")) as string;
        setStatus(JSON.parse(raw) as SetupStatus);
      } else {
        setStatus(await api.setupStatus());
      }
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 3000);
    return () => clearInterval(timer);
  }, [refresh]);

  const startDownload = useCallback(async () => {
    setDownloading(true);
    setError("");
    try {
      const invoke = tauriInvoke();
      if (invoke) {
        await invoke("setup_download");
      } else {
        await api.setupDownload();
      }
    } catch (e) {
      setError(String(e));
      setDownloading(false);
    }
  }, []);

  const present = status?.models.filter((m) => m.present).length ?? 0;
  const total = status?.models.length ?? 0;

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-2xl flex-col items-center justify-center gap-6 px-6">
      <div className="w-full rounded-2xl border border-[var(--card-border)] bg-[var(--card)] p-6">
        <h1 className="mb-2 text-2xl font-semibold">首次设置</h1>
        <p className="mb-4 text-sm text-[var(--muted)]">正在检查本机模型。所需模型约 13GB，仅在缺失时下载。</p>
        {error && <p className="mb-4 text-sm text-red-300">{error}</p>}
        {!status && !error && <p className="text-sm text-[var(--muted)]">读取模型状态…</p>}
        {status && (
          <>
            <div className="mb-4 flex items-center gap-2 text-sm">
              <span className={status.ready ? "text-emerald-400" : "text-amber-400"}>
                {status.ready ? "已就绪" : `已就绪 ${present}/${total}`}
              </span>
              {downloading && <span className="text-[var(--muted)]">下载中…</span>}
            </div>
            <ul className="mb-4 space-y-2">
              {status.models.map((m) => (
                <li key={m.name} className="flex items-center justify-between rounded-lg border border-[var(--card-border)] px-3 py-2 text-xs">
                  <span className="font-mono">{m.name}</span>
                  <span className="text-[var(--muted)]">{m.repo}</span>
                  <span className={m.present ? "text-emerald-400" : "text-rose-400"}>{m.present ? "已就绪" : "待下载"}</span>
                </li>
              ))}
            </ul>
            {!status.ready && (
              <button
                className="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-semibold text-[var(--bg)] disabled:opacity-50"
                disabled={downloading}
                onClick={() => startDownload()}
              >
                {downloading ? "下载中…" : "下载缺失模型"}
              </button>
            )}
          </>
        )}
      </div>
    </main>
  );
}
