"use client";

/**
 * 前端到 Tauri 桌面壳的桥接薄封装。
 * - desktop/src/bridge.ts 是给 web 工程引用的真实实现；这里按需转发，
 *   避免直接依赖 __TAURI_INTERNALS__（非 Tauri 环境返回失败态）。
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
