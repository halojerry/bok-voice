// Thin wrapper around the Tauri IPC bridge.
// Imported by the web app only when running inside the Tauri desktop shell.
// Exposes service status / start / stop / open-logs / manifest.

export interface ServiceStatus {
  name: string;
  port: number;
  up: boolean;
}

export interface HealthReport {
  app_data_dir: string;
  services: ServiceStatus[];
}

const invoke = (window as unknown as { __TAURI_INTERNALS__?: { invoke: (cmd: string, args?: unknown) => Promise<unknown> } })
  ?.__TAURI_INTERNALS__?.invoke;

export function isTauri(): boolean {
  return !!invoke;
}

export async function desktopHealth(): Promise<HealthReport> {
  if (!invoke) throw new Error("not running in Tauri");
  return (await invoke("health")) as HealthReport;
}

export async function desktopStart(): Promise<string> {
  if (!invoke) throw new Error("not running in Tauri");
  return (await invoke("start")) as string;
}

export async function desktopStop(): Promise<string> {
  if (!invoke) throw new Error("not running in Tauri");
  return (await invoke("stop")) as string;
}

export async function desktopOpenLogs(): Promise<string> {
  if (!invoke) throw new Error("not running in Tauri");
  return (await invoke("open_logs")) as string;
}

export async function desktopManifest(): Promise<string> {
  if (!invoke) throw new Error("not running in Tauri");
  return (await invoke("manifest")) as string;
}
