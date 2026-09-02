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

export interface AudioDevice {
  id: string;
  name: string;
  is_default: boolean;
}

export type AudioDeviceKind = "input" | "output";

/** macOS：枚举系统音频输入/输出设备（Windows/浏览器环境返回空，前端走 web 枚举）。 */
export async function listAudioDevices(kind: AudioDeviceKind): Promise<AudioDevice[]> {
  if (!invoke) throw new Error("not running in Tauri");
  return (await invoke("list_audio_devices", { kind })) as AudioDevice[];
}

/** macOS：把系统默认输出切到指定设备（A 线 <audio> 与 B 线 WebAudio 都走系统输出）。 */
export async function setSystemOutput(deviceId: string): Promise<string> {
  if (!invoke) throw new Error("not running in Tauri");
  return (await invoke("set_system_output", { deviceId })) as string;
}
