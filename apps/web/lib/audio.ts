"use client";

/**
 * 音频输入/输出设备管理。
 * - 桌面壳（Tauri）：输入走 Web enumerateDevices（需先授权麦克风），输出走原生
 *   CoreAudio 系统默认输出切换（listAudioDevices/setSystemOutput）。
 * - 浏览器：输入走 enumerateDevices；输出仅在支持 setSinkId 的 Chromium 内核可用。
 * - 选择持久化到 localStorage（bok.audio.mic / bok.audio.out），重开 App 自动恢复。
 */
import { isTauri, listAudioDevices, setSystemOutput, type AudioDevice } from "@/lib/tauri";

export type AudioDeviceKind = "input" | "output";
export interface AudioDeviceInfo {
  id: string;
  name: string;
  is_default: boolean;
  kind: AudioDeviceKind;
}

const MIC_KEY = "bok.audio.mic";
const OUT_KEY = "bok.audio.out";

export function isTauriShell(): boolean {
  return typeof window !== "undefined" && isTauri();
}

function storage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
}

export function savedMicDevice(): string {
  return storage()?.getItem(MIC_KEY) ?? "";
}
export function savedOutputDevice(): string {
  return storage()?.getItem(OUT_KEY) ?? "";
}
export function saveMicDevice(id: string) {
  storage()?.setItem(MIC_KEY, id);
}
export function saveOutputDevice(id: string) {
  storage()?.setItem(OUT_KEY, id);
}

/** 请求一次麦克风权限并立即释放（用于让设备列表带 label / 触发系统授权弹窗）。 */
export async function requestMicPermission(): Promise<boolean> {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((t) => t.stop());
    return true;
  } catch {
    return false;
  }
}

/** 枚举 Web（enumerateDevices）音频设备；granted 决定是否先请求一次权限以拿到 label。 */
async function listWebDevices(kind: AudioDeviceKind, granted: boolean): Promise<AudioDeviceInfo[]> {
  if (!navigator.mediaDevices?.enumerateDevices) return [];
  if (!granted) await requestMicPermission();
  const devices = await navigator.mediaDevices.enumerateDevices();
  const want: MediaDeviceKind = kind === "input" ? "audioinput" : "audiooutput";
  return devices
    .filter((d) => d.kind === want && d.deviceId)
    .map((d) => ({
      id: d.deviceId,
      name: d.label || (kind === "input" ? "麦克风" : "扬声器"),
      is_default: d.deviceId === "default",
      kind,
    }));
}

/**
 * 列出设备。
 * - 输入（麦克风）：**一律走 Web enumerateDevices** —— 采集（getUserMedia）用的是
 *   WebRTC deviceId，原生 CoreAudio UID 与之不匹配，混用会导致「选了却无法输入」。
 *   列出前先请求一次麦克风权限（触发 TCC 授权框，拿到带 label 的设备）。
 * - 输出（扬声器）：桌面壳走原生 CoreAudio（uid 用于切系统默认输出）；浏览器回退
 *   Web 枚举（仅 Chromium 支持 setSinkId）。
 * 无授权/无设备时返回 []，交由 UI 提示去系统设置开启麦克风。
 */
export async function listAudioDevicesOf(kind: AudioDeviceKind): Promise<AudioDeviceInfo[]> {
  if (kind === "output" && isTauriShell()) {
    try {
      const native = await listAudioDevices("output");
      if (Array.isArray(native) && native.length > 0) {
        return native.map((d) => ({ id: d.id, name: d.name, is_default: d.is_default, kind }));
      }
    } catch {
      /* 原生枚举失败（Windows 占位）时回退 web */
    }
  }
  // input 永远 web，且需要先请求麦克风权限（触发 TCC 授权框，才能拿到带 label 的设备）；
  // output 的 web 枚举不弹麦克风授权。
  return listWebDevices(kind, kind === "output");
}

/** 桌面壳切换系统默认输出；浏览器环境（Chromium）由调用方自行 setSinkId。 */
export async function applyOutputDevice(deviceId: string): Promise<void> {
  if (!deviceId) return;
  if (isTauriShell()) {
    try {
      await setSystemOutput(deviceId);
      saveOutputDevice(deviceId);
    } catch (e) {
      console.warn("set system output failed", e);
    }
  }
}

/**
 * 浏览器里真正切换扬声器输出：把 room 的远端音频元素路由到目标设备。
 * livekit 的 Room.switchActiveDevice("audiooutput") 在 Chromium 会调用
 * setSinkId；Safari/WKWebView 不支持 setSinkId，会抛错，由调用方静默忽略。
 * 需要 room 已连接（发布音频后）才有效，因此接通后再调用最可靠。
 */
export async function switchWebOutputDevice(room: { switchActiveDevice: (kind: string, id: string, exact?: boolean) => Promise<boolean> }, deviceId: string): Promise<boolean> {
  if (!deviceId || !room || typeof room.switchActiveDevice !== "function") return false;
  if (!webCanSwitchOutput()) return false;
  try {
    await room.switchActiveDevice("audiooutput", deviceId, false);
    saveOutputDevice(deviceId);
    return true;
  } catch (e) {
    console.warn("switch web audiooutput failed", e);
    return false;
  }
}

/** Chromium 浏览器才支持网页 setSinkId（livekit 对 Safari/WKWebView 内核禁用了输出切换）。
 * 仅检测 setSinkId 存在会把 WKWebView/Safari 误判成可用——它们有这 API 但 livekit 内部
 * 对远端 audio 设 sinkId 会抛 "Failed to set sink id on remote audio track"。排除非 Chrome 的
 * WebKit(含 Tauri 桌面 WKWebView):桌面走 CoreAudio 切系统输出,不走 setSinkId。 */
export function webCanSwitchOutput(): boolean {
  if (typeof document === "undefined" || typeof navigator === "undefined") return false;
  const ua = navigator.userAgent || "";
  const isWebKit = /AppleWebKit/i.test(ua);
  const isChrome = /Chrome\//i.test(ua) && !/Edg\//i.test(ua);
  // WKWebView / Safari:UA 含 AppleWebKit 但非 Chromium → 不支持可靠 setSinkId。
  if (isWebKit && !isChrome) return false;
  const audio = document.createElement("audio");
  return typeof (audio as HTMLAudioElement & { setSinkId?: unknown }).setSinkId === "function";
}
