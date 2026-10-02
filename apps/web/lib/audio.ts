"use client";

/**
 * 音频输入/输出设备管理（纯浏览器；Tauri 桌面壳已退役，2026-09-17）。
 * - 输入：enumerateDevices（需先授权麦克风）；采集用 WebRTC deviceId。
 * - 输出：livekit switchActiveDevice("audiooutput")=setSinkId，仅 Chromium 内核
 *   可靠（Safari/WKWebView 回退系统默认输出）。
 * - 选择持久化到 localStorage（bok.audio.mic / bok.audio.out），接通时自动恢复。
 */
export type AudioDeviceKind = "input" | "output";
export interface AudioDeviceInfo {
  id: string;
  name: string;
  is_default: boolean;
  kind: AudioDeviceKind;
  /**
   * Chrome 的物理设备组 id：同一台设备的输入与输出**共用**一个 groupId，且不随 id 轮换
   * （蓝牙重连后 deviceId 会换、groupId 不变）。用于「两个角色是不是同一台物理设备」的
   * 判定——只比设备名会把同型号的两支麦误判成同一台。
   */
  groupId: string;
}

const MIC_KEY = "bok.audio.mic";
const OUT_KEY = "bok.audio.out";

/** 设备选择持久化键：默认全局单组；一体台按端存（key="me"/"other" 等）。 */
function micKey(key?: string): string {
  return key ? `bok.audio.mic.${key}` : MIC_KEY;
}
function outKey(key?: string): string {
  return key ? `bok.audio.out.${key}` : OUT_KEY;
}

function storage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
}

export function savedMicDevice(key?: string): string {
  return storage()?.getItem(micKey(key)) ?? "";
}
export function savedOutputDevice(key?: string): string {
  return storage()?.getItem(outKey(key)) ?? "";
}
export function saveMicDevice(id: string, key?: string) {
  storage()?.setItem(micKey(key), id);
}
export function saveOutputDevice(id: string, key?: string) {
  storage()?.setItem(outKey(key), id);
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
      groupId: d.groupId ?? "",
    }));
}

/**
 * 列出设备。
 * - 输入（麦克风）：列出前先请求一次麦克风权限（拿到带 label 的设备）。
 * - 输出（扬声器）：Web 枚举不弹授权。
 * 无授权/无设备时返回 []，交由 UI 提示去系统设置开启麦克风。
 */
export async function listAudioDevicesOf(kind: AudioDeviceKind): Promise<AudioDeviceInfo[]> {
  return listWebDevices(kind, kind === "input");
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
  // 预校验(2026-10-01 第十三波):已存设备失效(拔掉/换机)时,livekit 内部对每个
  // 远端音轨 setSinkId 失败会先打 console.error「Failed to set sink id on remote
  // audio track」再返回——我们的 catch 永远接不到异常,存档也清不掉,每次挂轨都
  // 刷屏(call-231aa92a 窗口实弹 4 次/通)。改为调用前比对枚举清单:不在清单=
  // 失效存档,静默清档回系统默认,零报错。
  try {
    const outs = await listAudioDevicesOf("output");
    if (outs.length && !outs.some((d) => d.id === deviceId)) {
      saveOutputDevice("");
      return false;
    }
  } catch {
    /* 枚举失败不拦切换(权限异常等边缘),按旧路径走 */
  }
  try {
    await room.switchActiveDevice("audiooutput", deviceId, false);
    saveOutputDevice(deviceId);
    return true;
  } catch (e) {
    // 兜底:切换本身抛错(非 Chromium 内核的 setSinkId 缺席等)。
    console.warn("switch web audiooutput failed, 回退系统默认输出", e);
    saveOutputDevice("");
    return false;
  }
}

/** Chromium 浏览器才支持网页 setSinkId（livekit 对 Safari/WKWebView 内核禁用了输出切换）。
 * 仅检测 setSinkId 存在会把 WKWebView/Safari 误判成可用——它们有这 API 但 livekit 内部
 * 对远端 audio 设 sinkId 会抛 "Failed to set sink id on remote audio track"。排除非 Chrome 的
 * WebKit（Tauri 桌面 WKWebView 已随壳退役）。 */
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
