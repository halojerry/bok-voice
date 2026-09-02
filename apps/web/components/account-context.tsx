"use client";

import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { useControlPlaneReady } from "@/lib/api-ready";

type Settings = Record<string, unknown> | null;

interface AccountContextValue {
  accountId: string;
  settings: Settings;
  settingsLoading: boolean;
  health: boolean | null;
  refreshSettings: () => Promise<void>;
}

const AccountContext = createContext<AccountContextValue | null>(null);

export function AccountProvider({ children }: { children: React.ReactNode }) {
  const accountId = "acc-001";
  const [settings, setSettings] = useState<Settings>(null);
  const [settingsLoading, setSettingsLoading] = useState(true);
  const [health, setHealth] = useState<boolean | null>(null);
  const { attempt } = useControlPlaneReady();
  const lastAttemptRef = useRef(-1);

  async function refreshSettings() {
    setSettingsLoading(true);
    try {
      const next = await api.getSettings();
      setSettings(next);
    } catch {
      setSettings(null);
    } finally {
      setSettingsLoading(false);
    }
  }

  useEffect(() => {
    // 首次加载 + Control Plane 每次离线→就绪转换时自动重试一次。
    if (lastAttemptRef.current === attempt) return;
    lastAttemptRef.current = attempt;
    let cancelled = false;
    refreshSettings();
    api.health()
      .then((res) => { if (!cancelled) setHealth(Boolean(res.ok)); })
      .catch(() => { if (!cancelled) setHealth(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempt]);

  const value = useMemo(
    () => ({ accountId, settings, settingsLoading, health, refreshSettings }),
    [accountId, settings, settingsLoading, health],
  );

  return <AccountContext.Provider value={value}>{children}</AccountContext.Provider>;
}

export function useAccount() {
  const ctx = useContext(AccountContext);
  if (!ctx) throw new Error("useAccount must be used inside AccountProvider");
  return ctx;
}
