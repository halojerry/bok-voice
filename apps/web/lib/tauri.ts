"use client";

/**
 * 前端到 Tauri 桌面壳的桥接薄封装。
 * - 直接按需转发 __TAURI_INTERNALS__.invoke（非 Tauri 环境返回失败态），
 *   不依赖任何 desktop 侧 TS 文件。
 */
export interface ServiceStatus {
  name: string;
  port: number;
  up: boolean;
}
export interface HealthReport {
  app_data_dir: string;
  services: ServiceStatus[];
}
export interface AudioDevice {
  id: string;
  name: string;
  is_default: boolean;
}

type Invoke = (cmd: string, args?: unknown) => Promise<unknown>;
function invoke(): Invoke | null {
  if (typeof window === "undefined") return null;
  return (window as unknown as { __TAURI_INTERNALS__?: { invoke: Invoke } }).__TAURI_INTERNALS__?.invoke ?? null;
}

export function isTauri(): boolean {
  return invoke() !== null;
}

export async function listAudioDevices(kind: "input" | "output"): Promise<AudioDevice[]> {
  const fn = invoke();
  if (!fn) throw new Error("not running in Tauri");
  return (await fn("list_audio_devices", { kind })) as AudioDevice[];
}

export async function setSystemOutput(deviceId: string): Promise<string> {
  const fn = invoke();
  if (!fn) throw new Error("not running in Tauri");
  return (await fn("set_system_output", { deviceId })) as string;
}

/** 开机自启（登录时）：读取当前状态（非 Tauri 环境抛错；默认关闭）。 */
export async function getAutostart(): Promise<boolean> {
  const fn = invoke();
  if (!fn) throw new Error("not running in Tauri");
  return (await fn("get_autostart")) as boolean;
}

/** 开机自启（登录时）：写入状态，返回写入后从系统读回的实际状态。 */
export async function setAutostart(enabled: boolean): Promise<boolean> {
  const fn = invoke();
  if (!fn) throw new Error("not running in Tauri");
  return (await fn("set_autostart", { enabled })) as boolean;
}
