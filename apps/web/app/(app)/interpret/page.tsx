"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { StartAudio, useAudioPlayback, useSession, useTranscriptions } from "@livekit/components-react";
import { ConnectionState, TokenSource, type Room } from "livekit-client";
import { api, CONTROL_PLANE_URL } from "@/lib/api";
import { describeConnectError, friendlyErrorText } from "@/lib/api-ready";
import {
  applyOutputDevice,
  isTauriShell,
  listAudioDevicesOf,
  requestMicPermission,
  saveMicDevice,
  saveOutputDevice,
  savedMicDevice,
  savedOutputDevice,
  switchWebOutputDevice,
  webCanSwitchOutput,
  type AudioDeviceInfo,
} from "@/lib/audio";
import { AgentSessionProvider } from "@/components/agents-ui/agent-session-provider";
import InterpretConsole from "@/components/interpret-console";
import { useAccount } from "@/components/account-context";

/**
 * 双端同声传译(B 线 v2):LiveKit 房间 me/other 双端 + 两个方向的 interpreter agent。
 * - 我方端「创建房间」:选语言对 → createCall(kind=interpret) → token(role=me);
 *   me 端 token 挂 RoomAgentDispatch,建房时自动拉起 fwd/rev 两个 interpreter。
 * - 对方端「加入房间」:输入房间号 → token(role=other)。
 * - 每端只发布自己的麦克风;interpreter 把说话方译文投到对方端(轨级订阅权限),
 *   双方原声互听不受影响。字幕来自 AgentSession 的 lk.transcription(全量双语)。
 * - 会话落 CP(kind=interpret),结束时 settle → 总结/知识沉淀复用 A 线。
 */

const LANGS = [
  { value: "zh", label: "普通话" },
  { value: "cantonese", label: "粤语" },
  { value: "en", label: "English" },
];

const LANG_SHORT: Record<string, string> = { zh: "中", cantonese: "粤", en: "EN" };

type Side = "me" | "other";

export default function InterpretPage() {
  const { accountId: ACCOUNT } = useAccount();
  const [side, setSide] = useState<Side | null>(null);
  const [myLang, setMyLang] = useState("zh");
  const [otherLang, setOtherLang] = useState("en");
  const [joinRoom, setJoinRoom] = useState("");
  const [callId, setCallId] = useState("");
  const callIdRef = useRef("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  // 一体台模式：同机双设备组（我方/对象各一组麦+扬声器），与双端模式互斥。
  const [consoleMode, setConsoleMode] = useState(false);

  // 官方 TokenSource.endpoint：直连 CP /api/token（官方契约），useSession options
  // 驱动官方请求体（room_name / participant_identity），CP 按 identity 前缀反推
  // 角色并挂 agent 分发。进房动作在渲染后的 effect 里（见下），options 时序安全。
  const tokenSource = useMemo(() => TokenSource.endpoint(`${CONTROL_PLANE_URL}/api/token`), []);
  const roomId = callId || joinRoom.trim();
  const session = useSession(tokenSource, {
    roomName: roomId || undefined,
    participantIdentity: side && roomId ? `${side}-${roomId}` : undefined,
  });
  const { canPlayAudio, startAudio } = useAudioPlayback(session.room);
  const connected = session.room.state === ConnectionState.Connected;

  const [micDevices, setMicDevices] = useState<AudioDeviceInfo[]>([]);
  const [outDevices, setOutDevices] = useState<AudioDeviceInfo[]>([]);
  const [micId, setMicId] = useState("");
  const [outId, setOutId] = useState("");
  const [micOn, setMicOn] = useState(true);

  useEffect(() => {
    requestMicPermission()
      .then(async () => {
        setMicDevices(await listAudioDevicesOf("input"));
        setOutDevices(await listAudioDevicesOf("output"));
      })
      .catch(() => {});
    setMicId(savedMicDevice());
    setOutId(savedOutputDevice());
  }, []);

  // 进房后应用已存设备:麦克风采集 + 输出(桌面切系统默认,浏览器 setSinkId)。
  useEffect(() => {
    if (!connected) return;
    (async () => {
      if (micId) await session.room.switchActiveDevice("audioinput", micId, false).catch(() => {});
      if (outId) {
        if (isTauriShell()) await applyOutputDevice(outId).catch(() => {});
        else if (webCanSwitchOutput()) await switchWebOutputDevice(session.room, outId).catch(() => {});
      }
    })();
  }, [connected, micId, outId, session.room]);

  const pickMic = useCallback(
    (id: string) => {
      setMicId(id);
      saveMicDevice(id);
      if (id) session.room.switchActiveDevice("audioinput", id, false).catch(() => {});
    },
    [session.room],
  );

  const pickOut = useCallback(
    (id: string) => {
      setOutId(id);
      saveOutputDevice(id);
      if (!id) return;
      if (isTauriShell()) applyOutputDevice(id).catch(() => {});
      else if (webCanSwitchOutput()) switchWebOutputDevice(session.room, id).catch(() => {});
    },
    [session.room],
  );

  async function startAs(newSide: Side) {
    setError(null);
    setBusy(true);
    try {
      let id = callIdRef.current;
      if (newSide === "me" && !id) {
        // 同传会话:language=我方语言,target_lang=对方语言,object 留空。
        const created = await api.createCall({
          account_id: ACCOUNT,
          object_id: "",
          kind: "interpret",
          mode: "live",
          direction: "interpret",
          language: myLang,
          target_lang: otherLang,
        });
        id = String((created as { id?: string }).id ?? "");
      }
      if (newSide === "other") {
        id = joinRoom.trim();
        if (!id) {
          setBusy(false);
          setError("请输入我方端给出的房间号。");
          return;
        }
        // 已结束的会话房间已清,token join 会 401(误报「令牌校验失败」),先拦住。
        const cur = (await api.getCall(id).catch(() => null)) as { status?: string } | null;
        if (!cur) {
          setBusy(false);
          setError("找不到该房间号,请与我方端核对。");
          return;
        }
        if (String(cur.status ?? "") === "ended") {
          setBusy(false);
          setError("该同传会话已结束,请让对面开新房间。");
          return;
        }
      }
      setSide(newSide);
      setCallId(id);
      callIdRef.current = id;
      setBusy(false);
    } catch (e) {
      setBusy(false);
      setError(friendlyErrorText(String(e)));
    }
  }

  async function startConsole() {
    setError(null);
    setBusy(true);
    try {
      const created = await api.createCall({
        account_id: ACCOUNT,
        object_id: "",
        kind: "interpret",
        mode: "live",
        direction: "interpret",
        language: myLang,
        target_lang: otherLang,
      });
      const id = String((created as { id?: string }).id ?? "");
      if (!id) {
        setBusy(false);
        setError("创建一体台会话失败。");
        return;
      }
      callIdRef.current = id;
      setCallId(id);
      setConsoleMode(true);
      setBusy(false);
    } catch (e) {
      setBusy(false);
      setError(friendlyErrorText(String(e)));
    }
  }

  // side 决定 token role 后才 useSession 连接:进房动作放到渲染后的 effect。
  // 一体台模式不走双端 join(同身份双连会被 LiveKit 拒),me/other 由一体台组件自理。
  useEffect(() => {
    if (consoleMode || !side || !callId || connected || session.room.state === ConnectionState.Connecting) return;
    let cancelled = false;
    (async () => {
      setBusy(true);
      try {
        if (micId) await session.room.switchActiveDevice("audioinput", micId, false).catch(() => {});
        // 连接前预缓冲：建房/agent join 需 1-2s,此时对方可能已开口,缓冲防「吃头字」。
        await session.room.localParticipant.setMicrophoneEnabled(true, undefined, { preConnectBuffer: true }).catch(() => {});
        await session.start({ tracks: { microphone: { enabled: true } } });
        try {
          await session.room.localParticipant.setMicrophoneEnabled(true);
        } catch {
          setError("无法开启麦克风:请检查浏览器麦克风权限。");
        }
        if (!canPlayAudio) startAudio().catch(() => {});
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
  }, [side, callId]);

  async function leave() {
    const id = callIdRef.current;
    try {
      await session.end();
    } catch {
      /* ignore */
    }
    // 我方端挂断 = 结束整个房间(两个 interpreter 与对方端一起被踢,触发 settle);
    // 对方端退出只断自己。
    if (id && side === "me") {
      await api.hangup(id).catch(() => {});
    }
    setSide(null);
    setCallId("");
    callIdRef.current = "";
    setConsoleMode(false);
  }

  async function toggleMic() {
    try {
      const next = !micOn;
      await session.room.localParticipant.setMicrophoneEnabled(next);
      setMicOn(next);
    } catch {
      /* ignore */
    }
  }

  if (!side && !consoleMode) {
    return (
      <div className="mx-auto grid w-full max-w-5xl gap-6 md:grid-cols-2">
        <section className="card flex flex-col gap-4">
          <span className="label">我方端 · 创建同传房间</span>
          <div className="flex gap-3">
            <label className="flex flex-1 flex-col gap-1 text-xs">
              <span className="text-[var(--stage-muted)]">我方讲</span>
              <select className="select" value={myLang} onChange={(e) => setMyLang(e.target.value)}>
                {LANGS.map((l) => (
                  <option key={l.value} value={l.value}>
                    {l.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-1 flex-col gap-1 text-xs">
              <span className="text-[var(--stage-muted)]">对方讲</span>
              <select className="select" value={otherLang} onChange={(e) => setOtherLang(e.target.value)}>
                {LANGS.map((l) => (
                  <option key={l.value} value={l.value}>
                    {l.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <button className="stage-btn-primary" disabled={busy} onClick={() => startAs("me")}>
            创建房间并开始
          </button>
          <p className="text-xs leading-relaxed text-[var(--stage-muted)]">
            创建后把房间号发给对方;双方各自选好麦克风/扬声器。AI 同传会把你讲的翻译成对方语言、
            只播给对方听,反之亦然;全文双语字幕都看得到。
          </p>
        </section>
        <section className="card flex flex-col gap-4">
          <span className="label">对方端 · 加入房间</span>
          <input
            className="select w-full"
            placeholder="输入房间号(如 call-1a2b3c4d)"
            value={joinRoom}
            onChange={(e) => setJoinRoom(e.target.value)}
          />
          <button className="stage-btn-primary" disabled={busy} onClick={() => startAs("other")}>
            加入房间
          </button>
          <p className="text-xs leading-relaxed text-[var(--stage-muted)]">
            加入后选好自己的麦克风与扬声器即可开讲。
          </p>
        </section>
        <section className="card flex flex-col gap-4 md:col-span-2">
          <span className="label">坐席一体台 · 同机双设备组</span>
          <p className="text-xs leading-relaxed text-[var(--stage-muted)]">
            我方与对象在同一台电脑前、各用一副耳机麦：一个控制台同时接入本会话两端，各自独立选
            麦克风/扬声器（双输出独立路由），中间一条双语字幕，全程一人操作。需桌面 Chrome
            （Chromium setSinkId 双输出）。
          </p>
          <button className="stage-btn-primary md:w-fit" disabled={busy} onClick={startConsole}>
            创建一体台会话
          </button>
        </section>
        {error && <p className="md:col-span-2 text-sm text-red-400">{error}</p>}
      </div>
    );
  }

  if (consoleMode && callId) {
    return (
      <InterpretConsole
        account={ACCOUNT}
        callId={callId}
        myLang={myLang}
        otherLang={otherLang}
        onExit={() => {
          setConsoleMode(false);
          setCallId("");
          callIdRef.current = "";
          setError(null);
        }}
      />
    );
  }

  return (
    <AgentSessionProvider session={session} volume={1} muted={false}>
      <InterpretLive
        room={session.room}
        side={side}
        callId={callId}
        roomName={session.room.name}
        connected={connected}
        canPlayAudio={canPlayAudio}
        micOn={micOn}
        micDevices={micDevices}
        outDevices={outDevices}
        micId={micId}
        outId={outId}
        pickMic={pickMic}
        pickOut={pickOut}
        toggleMic={toggleMic}
        leave={leave}
        copied={copied}
        onCopy={async () => {
          try {
            await navigator.clipboard.writeText(callId);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          } catch {
            /* ignore */
          }
        }}
        error={error}
        busy={busy}
      />
    </AgentSessionProvider>
  );
}

type LiveProps = {
  room: Room;
  side: Side;
  callId: string;
  roomName: string;
  connected: boolean;
  canPlayAudio: boolean;
  micOn: boolean;
  micDevices: AudioDeviceInfo[];
  outDevices: AudioDeviceInfo[];
  micId: string;
  outId: string;
  pickMic: (id: string) => void;
  pickOut: (id: string) => void;
  toggleMic: () => void;
  leave: () => void;
  copied: boolean;
  onCopy: () => void;
  error: string | null;
  busy: boolean;
};

function InterpretLive(p: LiveProps) {
  // 字幕:AgentSession 的 lk.transcription(原文+译文全量广播,音频才按权限定向)。
  const transcriptions = useTranscriptions();
  const listRef = useRef<HTMLDivElement | null>(null);
  const items = useMemo(() => transcriptions.slice(-40), [transcriptions]);
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items]);

  const otherOnline = useMemo(() => {
    const prefix = p.side === "me" ? "other-" : "me-";
    for (const [, rp] of p.room.remoteParticipants) {
      if (String(rp.identity ?? "").startsWith(prefix)) return true;
    }
    return false;
  }, [p.room, p.room.remoteParticipants, p.connected]);

  return (
    <div className="grid grid-cols-[1fr_280px] gap-6 lg:h-[calc(100vh-7.5rem)]">
      {/* 中:双语字幕时间线 */}
      <section className="card flex min-h-0 flex-col gap-3 overflow-hidden">
        <div className="flex shrink-0 items-center justify-between">
          <span className="label">双语字幕 · {p.connected ? "进行中" : "连接中…"}</span>
          <span className="flex items-center gap-3 text-xs text-[var(--stage-muted)]">
            <span>房间 {p.roomName}</span>
            <button className="stage-btn-secondary px-2 py-0.5" onClick={p.onCopy}>
              {p.copied ? "已复制" : "复制"}
            </button>
          </span>
        </div>
        {!p.canPlayAudio && <StartAudio label="点击开启声音" />}
        <div ref={listRef} className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-1 py-2">
          {items.length === 0 && (
            <div className="flex flex-1 items-center justify-center font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--stage-muted)]">
              {p.connected ? "等说话…开口即译" : "正在接入同传房间…"}
            </div>
          )}
          {items.map((t, i) => {
            const label = subtitleLabel(t, p.room);
            return (
              <div key={`${label.who}-${i}`} className={`flex ${label.who === "我方" ? "justify-end" : "justify-start"}`}>
                <div
                  className={`max-w-[85%] rounded-lg px-3 py-2 text-[13px] leading-relaxed ${
                    label.kind === "dst"
                      ? "border border-[var(--card-border)] bg-white/5 text-[var(--foreground)]"
                      : "bg-[var(--accent)] text-[var(--accent-ink)]"
                  }`}
                >
                  <span className="mr-1.5 font-mono text-[10px] font-bold uppercase opacity-70">
                    {label.who}
                    {label.kind === "dst" ? ` 译文${label.lang ? `·${label.lang}` : ""}` : ""}
                  </span>
                  {String(t.text ?? "")}
                </div>
              </div>
            );
          })}
        </div>
        {p.error && <p className="shrink-0 text-xs text-red-400">{p.error}</p>}
      </section>

      {/* 右:状态/设备/操作 */}
      <aside className="card flex min-h-0 flex-col gap-4 overflow-y-auto">
        <div className="flex flex-col gap-1">
          <span className="label">{p.side === "me" ? "我方端" : "对方端"}</span>
          <span className="text-xs text-[var(--stage-muted)]">
            对方{otherOnline ? "已在线" : "未在线"} · 同传 AI {p.connected ? "已就绪" : "接入中"}
          </span>
        </div>
        <label className="flex flex-col gap-1 text-xs">
          <span className="text-[var(--stage-muted)]">麦克风</span>
          <select className="select" value={p.micId} onChange={(e) => p.pickMic(e.target.value)}>
            <option value="">系统默认</option>
            {p.micDevices.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs">
          <span className="text-[var(--stage-muted)]">扬声器{isTauriShell() ? "(系统输出)" : ""}</span>
          <select className="select" value={p.outId} onChange={(e) => p.pickOut(e.target.value)}>
            <option value="">系统默认</option>
            {p.outDevices.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </label>
        <div className="mt-auto flex flex-col gap-2">
          <button className="stage-btn-primary" onClick={p.toggleMic} disabled={!p.connected}>
            {p.micOn ? "静音" : "开麦"}
          </button>
          <button className="stage-btn-secondary" onClick={p.leave} disabled={p.busy}>
            {p.side === "me" ? "结束同传" : "退出"}
          </button>
        </div>
      </aside>
    </div>
  );
}

type TextStreamEntry = {
  text?: unknown;
  participantInfo?: { identity?: string };
  streamInfo?: { attributes?: Record<string, string> };
};

/** 字幕归属:人端 identity=原文说话方;agent 转写看 lk.transcribed_track_id——
 * 指向人端轨=原文,指向 agent 自己的 trans-* 音轨=译文(轨名带目标语言)。 */
function subtitleLabel(
  t: TextStreamEntry,
  room: Room,
): { who: string; kind: "src" | "dst"; lang?: string } {
  const id = String(t.participantInfo?.identity ?? "");
  if (id.startsWith("me-")) return { who: "我方", kind: "src" };
  if (id.startsWith("other-")) return { who: "对方", kind: "src" };
  const trackSid = t.streamInfo?.attributes?.["lk.transcribed_track_id"] ?? "";
  if (trackSid) {
    const pools = [room.remoteParticipants.values(), [room.localParticipant].values()];
    for (const pool of pools) {
      for (const participant of pool) {
        for (const pub of Object.values(participant.trackPublications ?? {})) {
          if (pub?.trackSid !== trackSid) continue;
          const owner = String(participant.identity ?? "");
          if (owner.startsWith("me-")) return { who: "我方", kind: "src" };
          if (owner.startsWith("other-")) return { who: "对方", kind: "src" };
          const name = String(pub.trackName ?? "");
          if (name.startsWith("trans-")) {
            const lang = name.slice("trans-".length);
            return { who: lang === "en" ? "对方听到" : "我方听到", kind: "dst", lang: LANG_SHORT[lang] ?? lang };
          }
        }
      }
    }
  }
  return { who: "同传", kind: "dst" };
}
