"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";

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
    refreshSettings();
    api.health().then((res) => setHealth(Boolean(res.ok))).catch(() => setHealth(false));
  }, []);

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
