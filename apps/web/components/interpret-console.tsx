"use client";

/**
 * 坐席一体台（B 线同机双设备组）：同一台电脑前 我方 + 对象 各一套耳机麦，
 * 一个控制台同时接入同传房间的 me/other 两个身份。
 *
 * - me 身份走 interpret 页同款官方会话（AgentSessionProvider + useTranscriptions）
 *   ——字幕沿用已验证管线（原文+译文气泡）。
 * - other 身份是纯手动 livekit Room：只负责「对象的麦克风收音 + 对象扬声器放音」，
 *   不重复渲染字幕（同一房间两边看到的是同一份字幕）。
 * - 两组设备（对象/我方：麦克风 + 扬声器）各自独立选择与静音；Chromium 才支持
 *   per-room setSinkId → 本组件在非 Chromium（Tauri WKWebView 等）不可用，入口已拦。
 * - 离开 = 结束我方连接 + hangup 整个 call（两个 interpreter 与对象端一起被踢）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ConnectionState, Room, RoomEvent, TokenSource } from "livekit-client";
import { useSession, useTranscriptions } from "@livekit/components-react";
import { AgentSessionProvider } from "@/components/agents-ui/agent-session-provider";
import { api, CONTROL_PLANE_URL } from "@/lib/api";
import { describeConnectError } from "@/lib/api-ready";
import {
  listAudioDevicesOf,
  requestMicPermission,
  savedMicDevice,
  savedOutputDevice,
  saveMicDevice,
  saveOutputDevice,
  switchWebOutputDevice,
  webCanSwitchOutput,
  type AudioDeviceInfo,
} from "@/lib/audio";

const LANG_SHORT: Record<string, string> = { zh: "中", cantonese: "粤", en: "EN" };

export type ConsoleProps = {
  account: string;
  callId: string;
  myLang: string;
  otherLang: string;
  onExit: () => void;
};

export default function InterpretConsole({ account, callId, myLang, otherLang, onExit }: ConsoleProps) {
  // 一体台要求 Chromium 双输出(setSinkId);Tauri WKWebView 无法 per-room 路由。
  const [unsupported, setUnsupported] = useState(false);
  useEffect(() => {
    if (!webCanSwitchOutput()) setUnsupported(true);
  }, []);

  // ---- 设备枚举(两端共用一份列表,各存各的选择) ----
  const [micDevices, setMicDevices] = useState<AudioDeviceInfo[]>([]);
  const [outDevices, setOutDevices] = useState<AudioDeviceInfo[]>([]);
  useEffect(() => {
    requestMicPermission()
      .then(async () => {
        setMicDevices(await listAudioDevicesOf("input"));
        setOutDevices(await listAudioDevicesOf("output"));
      })
      .catch(() => {});
  }, []);

  // ---- 官方会话(我方 me)：TokenSource.custom 直连 CP,身份钉 me-<callId> ----
  const tokenMe = useMemo(
    () =>
      TokenSource.custom(async () => {
        return await fetchToken(account, callId, "me");
      }),
    [account, callId],
  );
  const meSession = useSession(tokenMe, { roomName: callId });
  const otherRoomRef = useRef<Room | null>(null);

  const [meConnected, setMeConnected] = useState(false);
  const [otherConnected, setOtherConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // 我方设备(带 me 后缀持久化)
  const [meMicId, setMeMicId] = useState("");
  const [meOutId, setMeOutId] = useState("");
  // 对象设备(带 other 后缀持久化)
  const [othMicId, setOthMicId] = useState("");
  const [othOutId, setOthOutId] = useState("");
  const [meMicOn, setMeMicOn] = useState(true);
  const [othMicOn, setOthMicOn] = useState(true);
  const [otherOnline, setOtherOnline] = useState(false);

  useEffect(() => {
    setMeMicId(savedMicDevice("me"));
    setMeOutId(savedOutputDevice("me"));
    setOthMicId(savedMicDevice("other"));
    setOthOutId(savedOutputDevice("other"));
  }, []);

  // ---- 我方连接(照抄 interpret 页已验证路径) ----
  useEffect(() => {
    if (meSession.room.state === ConnectionState.Connected || meSession.room.state === ConnectionState.Connecting) return;
    let cancelled = false;
    (async () => {
      setBusy(true);
      try {
        // 连接 effect 可能早于设备恢复 state,直接用已存值兜底。
        const micId = meMicId || savedMicDevice("me");
        const outId = meOutId || savedOutputDevice("me");
        if (micId) await meSession.room.switchActiveDevice("audioinput", micId, false).catch(() => {});
        await meSession.room.localParticipant
          .setMicrophoneEnabled(true, undefined, { preConnectBuffer: true })
          .catch(() => {});
        await meSession.start({ tracks: { microphone: { enabled: true } } });
        try {
          await meSession.room.localParticipant.setMicrophoneEnabled(true);
        } catch {
          setError("无法开启我方麦克风：请检查浏览器麦克风权限。");
        }
        setMeConnected(true);
        if (outId) await switchWebOutputDevice(meSession.room, outId).catch(() => {});
      } catch (e) {
        if (!cancelled) setError(describeConnectError(e, "join-session"));
      } finally {
        if (!cancelled) setBusy(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meSession]);

  // ---- 对象连接(纯手动 Room:麦克风收音 + 扬声器放音) ----
  useEffect(() => {
    let cancelled = false;
    let room: Room | null = null;
    (async () => {
      try {
        const tok = await fetchToken(account, callId, "other");
        room = new Room();
        otherRoomRef.current = room;
        // 远端参与者上线(我方已在房间里)→ 状态灯;断开即清理。
        room.on(RoomEvent.ParticipantConnected, () => {
          if (!cancelled) setOtherOnline(true);
        });
        room.on(RoomEvent.ParticipantDisconnected, () => {
          if (!cancelled) setOtherOnline(false);
        });
        // 连接 effect 可能早于设备恢复 state,直接用已存值兜底。
        const micId = othMicId || savedMicDevice("other");
        const outId = othOutId || savedOutputDevice("other");
        if (micId) await room.switchActiveDevice("audioinput", micId, false).catch(() => {});
        await room.connect(tok.serverUrl, tok.participantToken);
        if (cancelled) return;
        await room.localParticipant.setMicrophoneEnabled(othMicOn);
        if (outId) await switchWebOutputDevice(room, outId).catch(() => {});
        // 我方 agent 在房间 → 我方 human 也是远端对象视角的参与者
        setOtherOnline(true);
        setOtherConnected(true);
        await room.startAudio().catch(() => {});
      } catch (e) {
        if (!cancelled) setError(describeConnectError(e, "join-session"));
      }
    })();
    return () => {
      cancelled = true;
      const r = otherRoomRef.current;
      otherRoomRef.current = null;
      if (r) r.disconnect().catch(() => {});
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [callId]);

  // ---- 设备选择应用 ----
  const pickMeMic = useCallback(
    (id: string) => {
      setMeMicId(id);
      saveMicDevice(id, "me");
      if (meConnected) meSession.room.switchActiveDevice("audioinput", id, false).catch(() => {});
    },
    [meSession, meConnected],
  );
  const pickMeOut = useCallback(
    (id: string) => {
      setMeOutId(id);
      saveOutputDevice(id, "me");
      if (meConnected) switchWebOutputDevice(meSession.room, id).catch(() => {});
    },
    [meSession, meConnected],
  );
  const pickOthMic = useCallback((id: string) => {
    setOthMicId(id);
    saveMicDevice(id, "other");
    const r = otherRoomRef.current;
    if (r) r.switchActiveDevice("audioinput", id, false).catch(() => {});
  }, []);
  const pickOthOut = useCallback((id: string) => {
    setOthOutId(id);
    saveOutputDevice(id, "other");
    const r = otherRoomRef.current;
    if (r) switchWebOutputDevice(r, id).catch(() => {});
  }, []);

  const toggleMeMic = useCallback(async () => {
    try {
      const next = !meMicOn;
      await meSession.room.localParticipant.setMicrophoneEnabled(next);
      setMeMicOn(next);
    } catch {
      /* ignore */
    }
  }, [meMicOn, meSession]);
  const toggleOthMic = useCallback(async () => {
    const r = otherRoomRef.current;
    if (!r) return;
    try {
      const next = !othMicOn;
      await r.localParticipant.setMicrophoneEnabled(next);
      setOthMicOn(next);
    } catch {
      /* ignore */
    }
  }, [othMicOn]);

  const leave = useCallback(async () => {
    try {
      await meSession.end();
    } catch {
      /* ignore */
    }
    const r = otherRoomRef.current;
    otherRoomRef.current = null;
    if (r) await r.disconnect().catch(() => {});
    await api.hangup(callId).catch(() => {});
    onExit();
  }, [meSession, callId, onExit]);

  const sameDeviceWarning =
    (meMicId && meMicId === othMicId) || (meOutId && meOutId === othOutId)
      ? "我方与对象选中了同一台设备——同机双人请各用一副耳机/一支麦。"
      : "";

  if (unsupported) {
    return (
      <div className="card p-6">
        <p className="text-sm text-amber-300">
          坐席一体台需要「双扬声器独立路由」，仅桌面 Chrome（Chromium 内核 setSinkId）支持；当前内核不支持，请在 Chrome 中打开使用。
        </p>
        <button className="stage-btn-secondary mt-4" onClick={onExit}>
          返回
        </button>
      </div>
    );
  }

  return (
    <AgentSessionProvider session={meSession} volume={1} muted={false}>
      <ConsoleLive
        room={meSession.room}
        myLang={myLang}
        otherLang={otherLang}
        meConnected={meConnected}
        otherConnected={otherConnected}
        error={error}
        busy={busy}
        micDevices={micDevices}
        outDevices={outDevices}
        meMicId={meMicId}
        meOutId={meOutId}
        othMicId={othMicId}
        othOutId={othOutId}
        meMicOn={meMicOn}
        othMicOn={othMicOn}
        sameDeviceWarning={sameDeviceWarning}
        pickMeMic={pickMeMic}
        pickMeOut={pickMeOut}
        pickOthMic={pickOthMic}
        pickOthOut={pickOthOut}
        toggleMeMic={toggleMeMic}
        toggleOthMic={toggleOthMic}
        leave={leave}
      />
    </AgentSessionProvider>
  );
}

type LiveProps = {
  room: Room;
  myLang: string;
  otherLang: string;
  meConnected: boolean;
  otherConnected: boolean;
  error: string | null;
  busy: boolean;
  micDevices: AudioDeviceInfo[];
  outDevices: AudioDeviceInfo[];
  meMicId: string;
  meOutId: string;
  othMicId: string;
  othOutId: string;
  meMicOn: boolean;
  othMicOn: boolean;
  sameDeviceWarning: string;
  pickMeMic: (id: string) => void;
  pickMeOut: (id: string) => void;
  pickOthMic: (id: string) => void;
  pickOthOut: (id: string) => void;
  toggleMeMic: () => void;
  toggleOthMic: () => void;
  leave: () => void;
};

function DeviceGroup(props: {
  title: string;
  subtitle: string;
  micId: string;
  outId: string;
  micOn: boolean;
  micDevices: AudioDeviceInfo[];
  outDevices: AudioDeviceInfo[];
  pickMic: (id: string) => void;
  pickOut: (id: string) => void;
  toggleMic: () => void;
  connected: boolean;
}) {
  return (
    <section className="flex flex-col gap-2 rounded-lg border border-[var(--card-border)] bg-white/5 p-3">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">{props.title}</span>
        <span className="flex items-center gap-1.5 text-[10px] text-[var(--stage-muted)]">
          <span className={`h-1.5 w-1.5 rounded-full ${props.connected ? "bg-emerald-400" : "bg-neutral-500"}`} />
          {props.connected ? "已接入" : props.subtitle}
        </span>
      </div>
      <label className="flex flex-col gap-1 text-xs">
        <span className="text-[var(--stage-muted)]">麦克风</span>
        <select className="select" value={props.micId} onChange={(e) => props.pickMic(e.target.value)}>
          <option value="">系统默认</option>
          {props.micDevices.map((d) => (
            <option key={d.id} value={d.id}>
              {d.name}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1 text-xs">
        <span className="text-[var(--stage-muted)]">扬声器（独立路由）</span>
        <select className="select" value={props.outId} onChange={(e) => props.pickOut(e.target.value)}>
          <option value="">系统默认</option>
          {props.outDevices.map((d) => (
            <option key={d.id} value={d.id}>
              {d.name}
            </option>
          ))}
        </select>
      </label>
      <button className="stage-btn-secondary mt-1" onClick={props.toggleMic} disabled={!props.connected}>
        {props.micOn ? "静音" : "开麦"}
      </button>
    </section>
  );
}

function ConsoleLive(p: LiveProps) {
  const transcriptions = useTranscriptions();
  const listRef = useRef<HTMLDivElement | null>(null);
  const items = useMemo(() => transcriptions.slice(-60), [transcriptions]);
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items]);

  return (
    <div className="grid grid-cols-[1fr_320px] gap-6 lg:h-[calc(100vh-7.5rem)]">
      {/* 中：共享字幕时间线(我方+对象原文与译文) */}
      <section className="card flex min-h-0 flex-col gap-3 overflow-hidden">
        <div className="flex shrink-0 items-center justify-between">
          <span className="label">一体台会话 · 双语字幕</span>
          <span className="text-xs text-[var(--stage-muted)]">
            {p.meConnected ? (p.otherConnected ? "我方+对象已接入" : "接入中…") : "连接中…"}
          </span>
        </div>
        <div ref={listRef} className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-1 py-2">
          {items.length === 0 && (
            <div className="flex flex-1 items-center justify-center font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--stage-muted)]">
              等说话…开口即译
            </div>
          )}
          {items.map((t, i) => {
            const who = whoIs(t, p.room, p.myLang, p.otherLang);
            return (
              <div key={`${who.text}-${i}`} className={`flex ${who.side === "right" ? "justify-end" : "justify-start"}`}>
                <div
                  className={`max-w-[85%] rounded-lg px-3 py-2 text-[13px] leading-relaxed ${
                    who.kind === "dst"
                      ? "border border-[var(--card-border)] bg-white/5 text-[var(--foreground)]"
                      : "bg-[var(--accent)] text-[var(--accent-ink)]"
                  }`}
                >
                  <span className="mr-1.5 font-mono text-[10px] font-bold uppercase opacity-70">{who.text}</span>
                  {String(t.text ?? "")}
                </div>
              </div>
            );
          })}
        </div>
        {p.sameDeviceWarning && <p className="shrink-0 text-xs text-amber-300">{p.sameDeviceWarning}</p>}
        {p.error && <p className="shrink-0 text-xs text-red-400">{p.error}</p>}
      </section>

      {/* 右：两端设备组 */}
      <aside className="flex min-h-0 flex-col gap-4 overflow-y-auto">
        <DeviceGroup
          title="对象端"
          subtitle="等待接入"
          connected={p.otherConnected}
          micId={p.othMicId}
          outId={p.othOutId}
          micOn={p.othMicOn}
          micDevices={p.micDevices}
          outDevices={p.outDevices}
          pickMic={p.pickOthMic}
          pickOut={p.pickOthOut}
          toggleMic={p.toggleOthMic}
        />
        <DeviceGroup
          title="我方端"
          subtitle="连接中…"
          connected={p.meConnected}
          micId={p.meMicId}
          outId={p.meOutId}
          micOn={p.meMicOn}
          micDevices={p.micDevices}
          outDevices={p.outDevices}
          pickMic={p.pickMeMic}
          pickOut={p.pickMeOut}
          toggleMic={p.toggleMeMic}
        />
        <button className="stage-btn-secondary mt-auto" onClick={p.leave} disabled={p.busy}>
          结束一体台会话
        </button>
      </aside>
    </div>
  );
}

type Bubble = { text: string; side: "left" | "right"; kind: "src" | "dst" };

type TextStreamEntry = {
  text?: unknown;
  participantInfo?: { identity?: string };
  streamInfo?: { attributes?: Record<string, string> };
};

/** 字幕归属（照抄 interpret 页 subtitleLabel 的解析，另给译文标注听众端）：
 * 人端 identity=原文说话方;agent 转写看 lk.transcribed_track_id——指向人端轨=原文,
 * 指向 agent 自己的 trans-<lang> 轨=译文。译文听众 = 该目标语言那一端。 */
function whoIs(t: TextStreamEntry, room: Room, myLang: string, otherLang: string): Bubble {
  const id = String(t.participantInfo?.identity ?? "");
  if (id.startsWith("me-")) return { text: "我方", side: "right", kind: "src" };
  if (id.startsWith("other-")) return { text: "对象", side: "left", kind: "src" };
  const trackSid = t.streamInfo?.attributes?.["lk.transcribed_track_id"] ?? "";
  if (trackSid) {
    const pools = [room.remoteParticipants.values(), [room.localParticipant].values()];
    for (const pool of pools) {
      for (const participant of pool) {
        for (const pub of Object.values(participant.trackPublications ?? {})) {
          if (pub?.trackSid !== trackSid) continue;
          const owner = String(participant.identity ?? "");
          if (owner.startsWith("me-")) return { text: "我方", side: "right", kind: "src" };
          if (owner.startsWith("other-")) return { text: "对象", side: "left", kind: "src" };
          const name = String(pub.trackName ?? "");
          if (name.startsWith("trans-")) {
            const lang = name.slice("trans-".length);
            const ear = LANG_SHORT[lang] ?? lang;
            return { text: `译文·${ear}`, side: lang === myLang ? "right" : "left", kind: "dst" };
          }
        }
      }
    }
  }
  return { text: "同传", side: "left", kind: "dst" };
}

async function fetchToken(account: string, callId: string, role: "me" | "other"): Promise<{ serverUrl: string; participantToken: string }> {
  const resp = await fetch(`${CONTROL_PLANE_URL}/api/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ account_id: account, call_id: callId, participant_identity: `${role}-${callId}` }),
  });
  if (!resp.ok) throw new Error(`token http ${resp.status}`);
  const data = (await resp.json()) as { serverUrl?: string; participantToken?: string };
  if (!data.serverUrl || !data.participantToken) throw new Error("token 响应缺 serverUrl/participantToken");
  return { serverUrl: data.serverUrl, participantToken: data.participantToken };
}
