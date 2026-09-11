"use client";

/**
 * 坐席一体台（单页双通道工作台,参考金喜同传的单页操作形态）:
 * 两人同机各一支麦,一个控制台同时接入同传房间的 me/other 两个身份,
 * 双向译文默认都从系统当前扬声器出声(共享输出),同页看双向原文+译文字幕。
 *
 * - me 身份走 interpret 页同款官方会话(AgentSessionProvider + useTranscriptions)
 *   ——字幕沿用已验证管线(lk.transcription 全量广播,同页双向原文译文都看得到)。
 * - other 身份是纯手动 livekit Room:只负责「对象的麦克风收音 + 对象译文放音」,
 *   不重复渲染字幕(同一房间两边看到的是同一份字幕)。
 * - 输出两档:共享扬声器(默认,系统当前输出,任何内核可用,Mac/Windows 通吃)/
 *   独立双输出(高级,Chromium setSinkId,两人各戴一副耳机时用)。
 * - 自动半双工(默认开):任一方向译文出声时自动暂让对向麦克风——共享扬声器外放,
 *   麦克风会拾到译文原声,不暂让会把译文再翻译一遍(串译死循环)。
 * - 离开 = 结束我方连接 + hangup 整个 call(两个 interpreter 与对象端一起被踢)。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ConnectionState,
  Room,
  RoomEvent,
  TokenSource,
  type RemoteTrack,
  type RemoteTrackPublication,
} from "livekit-client";
import { useSession, useTranscriptions } from "@livekit/components-react";
import { AgentSessionProvider } from "@/components/agents-ui/agent-session-provider";
import { api, apiBase } from "@/lib/api";
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
  // 独立双输出(两个 room 各自 setSinkId)仅 Chromium 可用;探测放 effect 避开 SSR。
  const [canDual, setCanDual] = useState(false);
  useEffect(() => {
    setCanDual(webCanSwitchOutput());
  }, []);
  const [outputMode, setOutputMode] = useState<"shared" | "dual">("shared");
  const outputModeRef = useRef(outputMode);
  useEffect(() => {
    outputModeRef.current = outputMode;
  }, [outputMode]);

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
  // deps 用稳定的 Room 实例而非 meSession 对象:后者是 useMemo 产物、身份随本地轨
  // publish/mute 变动(半双工每轮暂让都在换),旧 deps [meSession] 令连接 effect 反复
  // 重跑、leave 窗口还会重连污染结算(2026-09-11 审计 P1-3)。
  const meRoom = meSession.room;
  const otherRoomRef = useRef<Room | null>(null);
  // other 房间重建计数:驱动半双工 watcher 重新挂载(重挂/StrictMode 下 watcher
  // 曾盯着已 disconnect 的死房,othHeld 永不生效,2026-09-11 审计 P1-4)。
  const [otherRoomVersion, setOtherRoomVersion] = useState(0);
  const leavingRef = useRef(false);
  const [leaving, setLeaving] = useState(false);

  const [meConnected, setMeConnected] = useState(false);
  const [otherConnected, setOtherConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [startedAt, setStartedAt] = useState<number | null>(null);

  // 我方设备(带 me 后缀持久化)
  const [meMicId, setMeMicId] = useState("");
  const [meOutId, setMeOutId] = useState("");
  // 对象设备(带 other 后缀持久化)
  const [othMicId, setOthMicId] = useState("");
  const [othOutId, setOthOutId] = useState("");
  const [meMicOn, setMeMicOn] = useState(true);
  const [othMicOn, setOthMicOn] = useState(true);

  useEffect(() => {
    setMeMicId(savedMicDevice("me"));
    setMeOutId(savedOutputDevice("me"));
    setOthMicId(savedMicDevice("other"));
    setOthOutId(savedOutputDevice("other"));
  }, []);

  // ---- 自动半双工(防串译,默认开) ----
  // meHeld   = 对方译文(trans-<我方语言>,rev)出声中 → 我方麦暂让;
  // othHeld  = 我方译文(trans-<对方语言>,fwd)经共享扬声器外放中 → 对方麦暂让
  //            (对方麦拾到英文译文再翻一遍就是串译死循环)。
  const [halfDuplex, setHalfDuplex] = useState(true);
  const [meHeld, setMeHeld] = useState(false);
  const [othHeld, setOthHeld] = useState(false);

  // ---- 我方连接(照抄 interpret 页/CallStudio 已验证路径) ----
  useEffect(() => {
    if (meRoom.state === ConnectionState.Connected || meRoom.state === ConnectionState.Connecting) return;
    let cancelled = false;
    (async () => {
      setBusy(true);
      try {
        // 连接 effect 可能早于设备恢复 state,直接用已存值兜底。
        const micId = meMicId || savedMicDevice("me");
        const outId = meOutId || savedOutputDevice("me");
        if (micId) await meRoom.switchActiveDevice("audioinput", micId, false).catch(() => {});
        // 麦克风采集放进 session.start 的 tracks(与 token/连房并行,CallStudio 同款)。
        // 绝不能先在未连接的房间上 await setMicrophoneEnabled——发布等连接、连接又
        // 等这行返回,互等死到 livekit 内部 ~15s 超时,且超时会 track.stop() 杀掉
        // 已采集的麦克风轨、错误被吞,房间照常连接 = 「已接入」假象 + fwd 收不到
        // 任何音频(2026-09-11 同传审计:09-10「fwd 进房 6 分钟零译文」根因)。
        await meSession.start({
          tracks: { microphone: { enabled: true, publishOptions: { preConnectBuffer: true } } },
        });
        // 确保我方麦克风真正发布:失败(权限被拒/设备被占)显式报错并把开关拉回
        // 现实,不再静默装「已接入」。
        try {
          const pub = await meRoom.localParticipant.setMicrophoneEnabled(true);
          setMeMicOn(Boolean(pub));
          if (!pub) setError("无法开启我方麦克风：请检查浏览器麦克风权限——已连接,但同传听不到我方说话。");
        } catch {
          setMeMicOn(false);
          setError("无法开启我方麦克风：请检查浏览器麦克风权限——已连接,但同传听不到我方说话。");
        }
        // 共享扬声器(默认)不碰输出路由;独立双输出才 setSinkId。
        if (outputModeRef.current === "dual" && outId) {
          await switchWebOutputDevice(meRoom, outId).catch(() => {});
        }
      } catch (e) {
        // session.start 内部 token/连房与麦克风并行:麦克风失败时房间可能仍连上。
        const raw = e instanceof Error ? e.message : String(e ?? "");
        if (/notallowed|permission|notreadable|track invalid|device in use/i.test(raw)) {
          if (!cancelled) {
            setMeMicOn(false);
            setError("无法开启我方麦克风：请检查浏览器麦克风权限——已连接,但同传听不到我方说话。");
          }
        } else if (!cancelled) setError(describeConnectError(e, "join-session"));
      } finally {
        // 无条件复位:cancelled(权限拒绝/依赖重挂载)路径若不复位,结束按钮
        // 会永久锁死(2026-09-09 QA B5 实测);busy 只表达「连接尝试进行中」。
        setBusy(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meRoom]);

  // meConnected 由房间状态驱动,不押在 start() resolve 上——其末尾「等 agent 就绪」
  // 无超时、agent 慢/异常时永久挂起,旧写法 meConnected/时钟/busy 全部死在 await 后
  // (2026-09-11 审计 P0:一体台恒显「连接中」的直接根因)。
  useEffect(() => {
    if (meRoom.state !== ConnectionState.Connected) return;
    setMeConnected(true);
    setStartedAt((prev) => prev ?? Date.now());
    setBusy(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meRoom, meRoom.state]);

  // ---- 对象连接(纯手动 Room:麦克风收音 + 译文放音) ----
  useEffect(() => {
    // 顺序契约(2026-09-12 一体台「没翻译」根因):RoomAgentDispatch 只在首个
    // 参与者建房时生效且只挂 me 端 token——两端同时起跑时 other(无 dispatch)
    // 抢先建房,me 的 dispatch 永不激活=同传 AI 拉不起来。other 必须等 me 连上
    // (房间已由 me 建好)再进。
    if (!meConnected) return;
    let cancelled = false;
    let room: Room | null = null;
    (async () => {
      try {
        const tok = await fetchToken(account, callId, "other");
        // fetch 挂起期间 cleanup 已跑:直接放弃。旧写法在此之后才查 cancelled,
        // 会把 room 完整连上后弃管(无 disconnect),靠同身份重连互踢才收敛。
        if (cancelled) return;
        room = new Room();
        otherRoomRef.current = room;
        setOtherRoomVersion((v) => v + 1);
        // 译文出声(2026-09-12「听不到我方话的同传」根因):手动 Room 没有
        // AgentSessionProvider 的全轨音频渲染——连接时 startAudio() 只 attach
        // 当时的轨,而 fwd 的 trans-<对方语言> 译文轨是**之后**说话才发布订阅的,
        // 永远没有 audio element=永远无声(me 端中文译文走官方渲染器所以听得到)。
        // TrackSubscribed 即刻 attach;autoplay 被拦时首次点击恢复。
        room.on(RoomEvent.TrackSubscribed, (track) => {
          if (track.kind !== "audio") return;
          const el = track.attach();
          el.autoplay = true;
          el.style.display = "none";
          document.body.appendChild(el);
          el.play().catch(() => {
            const resume = () => {
              el.play().catch(() => {});
              document.removeEventListener("click", resume);
            };
            document.addEventListener("click", resume);
          });
        });
        room.on(RoomEvent.TrackUnsubscribed, (track) => {
          track.detach().forEach((el) => el.remove());
        });
        // 连接 effect 可能早于设备恢复 state,直接用已存值兜底。
        const micId = othMicId || savedMicDevice("other");
        const outId = othOutId || savedOutputDevice("other");
        if (micId) await room.switchActiveDevice("audioinput", micId, false).catch(() => {});
        await room.connect(tok.serverUrl, tok.participantToken);
        if (cancelled) {
          room.disconnect().catch(() => {});
          return;
        }
        await room.localParticipant.setMicrophoneEnabled(othMicOn);
        // 共享扬声器(默认)不碰输出路由;独立双输出才 setSinkId。
        if (outputModeRef.current === "dual" && outId) {
          await switchWebOutputDevice(room, outId).catch(() => {});
        }
        setOtherConnected(true);
        setOtherRoomVersion((v) => v + 1);
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
  }, [callId, meConnected]);

  // ---- 设备选择应用 ----
  const pickMeMic = useCallback(
    (id: string) => {
      setMeMicId(id);
      saveMicDevice(id, "me");
      if (meConnected) meRoom.switchActiveDevice("audioinput", id, false).catch(() => {});
    },
    [meSession, meConnected],
  );
  const pickMeOut = useCallback(
    (id: string) => {
      setMeOutId(id);
      saveOutputDevice(id, "me");
      if (outputModeRef.current === "dual" && meConnected) switchWebOutputDevice(meRoom, id).catch(() => {});
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
    if (outputModeRef.current === "dual" && r) switchWebOutputDevice(r, id).catch(() => {});
  }, []);

  // ---- 麦克风开关:按钮只改人工意图,生效值由本 effect 统一投到房间 ----
  // 生效值 = 人工开关 && !自动暂让——暂让结束后按人工意图恢复,人工静音始终优先。
  const toggleMeMic = useCallback(() => setMeMicOn((v) => !v), []);
  const toggleOthMic = useCallback(() => setOthMicOn((v) => !v), []);
  useEffect(() => {
    if (!meConnected) return;
    meRoom.localParticipant.setMicrophoneEnabled(meMicOn && !meHeld).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meMicOn, meHeld, meConnected]);
  useEffect(() => {
    const r = otherRoomRef.current;
    if (!otherConnected || !r) return;
    r.localParticipant.setMicrophoneEnabled(othMicOn && !othHeld).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [othMicOn, othHeld, otherConnected]);

  // ---- 输出模式切换:对已连接房间重投路由 ----
  useEffect(() => {
    if (outputMode !== "dual" || !canDual || !meConnected) return;
    const id = meOutId || savedOutputDevice("me");
    if (id) switchWebOutputDevice(meRoom, id).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outputMode, meOutId, meConnected, canDual]);
  useEffect(() => {
    const r = otherRoomRef.current;
    if (outputMode !== "dual" || !canDual || !otherConnected || !r) return;
    const id = othOutId || savedOutputDevice("other");
    if (id) switchWebOutputDevice(r, id).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outputMode, othOutId, otherConnected, canDual]);
  // 切回共享扬声器:显式回系统默认输出(sinkId="default"),避免残留上一档路由。
  useEffect(() => {
    if (outputMode !== "shared" || !canDual) return;
    if (meConnected) meRoom.switchActiveDevice("audiooutput", "default", false).catch(() => {});
    const r = otherRoomRef.current;
    if (otherConnected && r) r.switchActiveDevice("audiooutput", "default", false).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outputMode, meConnected, otherConnected, canDual]);

  // ---- 自动半双工:监听两个房间各自收到的 trans-* 译文轨出声 ----
  useEffect(() => {
    if (!halfDuplex) {
      setMeHeld(false);
      setOthHeld(false);
      return;
    }
    const stopMe = watchTransAudio(meRoom, setMeHeld);
    const stopOth = watchTransAudio(otherRoomRef.current, setOthHeld);
    return () => {
      stopMe();
      stopOth();
    };
    // otherRoomVersion:other 房间(重)建后重挂 watcher,防盯死房。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [halfDuplex, meConnected, otherConnected, otherRoomVersion]);

  // 同设备告警:同麦永远要提示;同扬声器只在独立双输出档才是问题(共享档本来就共用)。
  const sameDeviceWarning = [
    meMicId && meMicId === othMicId ? "我方与对象选中了同一支麦克风——两人请各用一支。" : "",
    outputMode === "dual" && meOutId && meOutId === othOutId ? "独立双输出选中了同一台扬声器——请各用一副耳机。" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <AgentSessionProvider session={meSession} volume={1} muted={false}>
      <ConsoleLive
        room={meRoom}
        myLang={myLang}
        otherLang={otherLang}
        meConnected={meConnected}
        otherConnected={otherConnected}
        error={error}
        busy={busy}
        leaving={leaving}
        micDevices={micDevices}
        outDevices={outDevices}
        meMicId={meMicId}
        meOutId={meOutId}
        othMicId={othMicId}
        othOutId={othOutId}
        meMicOn={meMicOn}
        othMicOn={othMicOn}
        meHeld={meHeld}
        othHeld={othHeld}
        halfDuplex={halfDuplex}
        outputMode={outputMode}
        canDual={canDual}
        startedAt={startedAt}
        sameDeviceWarning={sameDeviceWarning}
        pickMeMic={pickMeMic}
        pickMeOut={pickMeOut}
        pickOthMic={pickOthMic}
        pickOthOut={pickOthOut}
        toggleMeMic={toggleMeMic}
        toggleOthMic={toggleOthMic}
        setHalfDuplex={setHalfDuplex}
        setOutputMode={setOutputMode}
        leave={leave}
      />
    </AgentSessionProvider>
  );

  async function leave() {
    // 防抖:CP 断房后台化之前,hangup 慢时连点曾打出 7 连发(2026-09-10 实证);
    // onExit 收进 finally——任何一步挂起/抛错都放行退出,不再把用户锁在页面里。
    if (leavingRef.current) return;
    leavingRef.current = true;
    setLeaving(true);
    try {
      try {
        await Promise.race([meSession.end(), new Promise((r) => setTimeout(r, 5000))]);
      } catch {
        /* ignore */
      }
      const r = otherRoomRef.current;
      otherRoomRef.current = null;
      if (r) await Promise.race([r.disconnect(), new Promise((res) => setTimeout(res, 3000))]).catch(() => {});
      await api.hangup(callId).catch((e) => {
        if (!String(e).includes("404")) console.warn("hangup failed", e);
      });
    } finally {
      leavingRef.current = false;
      setLeaving(false);
      onExit();
    }
  }
}

type LiveProps = {
  room: Room;
  myLang: string;
  otherLang: string;
  meConnected: boolean;
  otherConnected: boolean;
  error: string | null;
  busy: boolean;
  leaving: boolean;
  micDevices: AudioDeviceInfo[];
  outDevices: AudioDeviceInfo[];
  meMicId: string;
  meOutId: string;
  othMicId: string;
  othOutId: string;
  meMicOn: boolean;
  othMicOn: boolean;
  meHeld: boolean;
  othHeld: boolean;
  halfDuplex: boolean;
  outputMode: "shared" | "dual";
  canDual: boolean;
  startedAt: number | null;
  sameDeviceWarning: string;
  pickMeMic: (id: string) => void;
  pickMeOut: (id: string) => void;
  pickOthMic: (id: string) => void;
  pickOthOut: (id: string) => void;
  toggleMeMic: () => void;
  toggleOthMic: () => void;
  setHalfDuplex: (v: boolean) => void;
  setOutputMode: (v: "shared" | "dual") => void;
  leave: () => void;
};

function Stat({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-(--card-border) bg-white/5 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-(--stage-muted)">{label}</div>
      <div className="mt-0.5 flex items-center font-mono text-[12px]">{children}</div>
    </div>
  );
}

function Dot({ on }: { on: boolean }) {
  return <span className={`mr-1.5 inline-block h-1.5 w-1.5 rounded-full ${on ? "bg-emerald-400" : "bg-neutral-500"}`} />;
}

function SessionClock({ startedAt }: { startedAt: number | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (startedAt == null) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [startedAt]);
  if (startedAt == null) return <span>—</span>;
  const s = Math.max(0, Math.floor((now - startedAt) / 1000));
  const hh = String(Math.floor(s / 3600)).padStart(2, "0");
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return (
    <span>
      {hh}:{mm}:{ss}
    </span>
  );
}

function ConsoleLive(p: LiveProps) {
  const transcriptions = useTranscriptions();
  const listRef = useRef<HTMLDivElement | null>(null);
  const [filter, setFilter] = useState<"both" | "me" | "other">("both");
  const [clearedCount, setClearedCount] = useState(0);
  const items = useMemo(() => transcriptions.slice(-80), [transcriptions]);
  const offset = transcriptions.length - items.length;
  const dstCount = useMemo(
    () => transcriptions.filter((t) => whoIs(t, p.room, p.myLang, p.otherLang).kind === "dst").length,
    [transcriptions, p.room, p.myLang, p.otherLang],
  );
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items]);
  const liveBusy = p.meHeld || p.othHeld;

  return (
    <div className="flex flex-col gap-4 lg:h-[calc(100vh-7.5rem)]">
      <div className="grid shrink-0 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {/* ① 声音设备卡 */}
        <section className="card flex flex-col gap-3 p-4">
          <span className="label">声音设备</span>
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-(--stage-muted)">我方麦克风</span>
            <select className="select" value={p.meMicId} onChange={(e) => p.pickMeMic(e.target.value)}>
              <option value="">系统默认</option>
              {p.micDevices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-(--stage-muted)">对方麦克风</span>
            <select className="select" value={p.othMicId} onChange={(e) => p.pickOthMic(e.target.value)}>
              <option value="">系统默认</option>
              {p.micDevices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
          </label>
          <div className="flex flex-col gap-1.5 text-xs">
            <span className="text-(--stage-muted)">扬声器输出</span>
            <div className="grid grid-cols-2 gap-2">
              <label
                className={`flex items-center gap-1.5 rounded-md border border-(--card-border) px-2 py-1.5 ${
                  p.outputMode === "shared" ? "ring-1 ring-(--accent)" : ""
                }`}
              >
                <input type="radio" name="out-mode" checked={p.outputMode === "shared"} onChange={() => p.setOutputMode("shared")} />
                共享扬声器
              </label>
              <label
                className={`flex items-center gap-1.5 rounded-md border border-(--card-border) px-2 py-1.5 ${
                  p.outputMode === "dual" ? "ring-1 ring-(--accent)" : ""
                } ${p.canDual ? "" : "opacity-50"}`}
                title={p.canDual ? "两人各戴一副耳机,分路独立输出" : "需要桌面 Chrome(setSinkId)"}
              >
                <input
                  type="radio"
                  name="out-mode"
                  disabled={!p.canDual}
                  checked={p.outputMode === "dual"}
                  onChange={() => p.setOutputMode("dual")}
                />
                独立双输出
              </label>
            </div>
            {p.outputMode === "shared" ? (
              <p className="text-[10px] leading-relaxed text-(--stage-muted)">
                双向译文都从系统当前扬声器出声;任何内核可用(Mac/Windows),跟系统走。
              </p>
            ) : (
              <div className="grid grid-cols-2 gap-2">
                <label className="flex flex-col gap-1">
                  <span className="text-(--stage-muted)">我方扬声器</span>
                  <select className="select" value={p.meOutId} onChange={(e) => p.pickMeOut(e.target.value)}>
                    <option value="">系统默认</option>
                    {p.outDevices.map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-(--stage-muted)">对方扬声器</span>
                  <select className="select" value={p.othOutId} onChange={(e) => p.pickOthOut(e.target.value)}>
                    <option value="">系统默认</option>
                    {p.outDevices.map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.name}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            )}
            {!p.canDual && (
              <p className="text-[10px] text-(--stage-muted)">独立双输出需桌面 Chrome;当前内核走共享扬声器。</p>
            )}
          </div>
        </section>

        {/* ② 会话监控卡 */}
        <section className="card flex flex-col gap-3 p-4">
          <span className="label">会话监控</span>
          <div className="grid grid-cols-2 gap-2">
            <Stat label="我方连接">
              <Dot on={p.meConnected} />
              {p.meConnected ? "已接入" : "连接中"}
            </Stat>
            <Stat label="对象连接">
              <Dot on={p.otherConnected} />
              {p.otherConnected ? "已接入" : "未接入"}
            </Stat>
            <Stat label="同传服务">
              <Dot on={dstCount > 0 || liveBusy} />
              {dstCount > 0 || liveBusy ? "出译中" : "待命"}
            </Stat>
            <Stat label="半双工">{p.halfDuplex ? (liveBusy ? "暂让中" : "值守") : "关闭"}</Stat>
            <Stat label="会话时长">
              <SessionClock startedAt={p.startedAt} />
            </Stat>
            <Stat label="翻译条数">{dstCount}</Stat>
          </div>
          <p className="text-xs leading-relaxed text-(--stage-muted)">
            语言对:我方 {LANG_SHORT[p.myLang] ?? p.myLang} ⇄ 对方 {LANG_SHORT[p.otherLang] ?? p.otherLang}
            (建房时已钉死,换语言对需结束并重建房间)。
          </p>
        </section>

        {/* ③ 控制卡 */}
        <section className="card flex flex-col gap-2.5 p-4">
          <span className="label">控制</span>
          <div className="grid grid-cols-2 gap-2">
            <button className="stage-btn-secondary" onClick={p.toggleMeMic} disabled={!p.meConnected}>
              {p.meHeld ? "暂让中…" : p.meMicOn ? "静音我方麦" : "开我方麦"}
            </button>
            <button className="stage-btn-secondary" onClick={p.toggleOthMic} disabled={!p.otherConnected}>
              {p.othHeld ? "暂让中…" : p.othMicOn ? "静音对方麦" : "开对方麦"}
            </button>
          </div>
          <label className="flex items-start gap-2 text-xs leading-relaxed">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={p.halfDuplex}
              onChange={(e) => p.setHalfDuplex(e.target.checked)}
            />
            <span>自动半双工:译文播报时暂让对向麦克风,防共享扬声器串译(外放建议开;戴耳机可关)</span>
          </label>
          <button className="stage-btn-secondary" onClick={() => setClearedCount(transcriptions.length)}>
            清空字幕
          </button>
          {/* 结束按钮只在「已在退出中」时禁用(防 7 连发),连接/busy 中都保持可点。 */}
          <button className="stage-btn-secondary mt-auto text-red-300" onClick={p.leave} disabled={p.leaving}>
            {p.leaving ? "结束中…" : "结束一体台会话"}
          </button>
        </section>
      </div>

      {/* ④ 双语字幕卡 */}
      <section className="card flex min-h-[320px] flex-1 flex-col gap-2 overflow-hidden">
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
          <span className="label">一体台 · 双语字幕</span>
          <div className="flex items-center gap-1.5">
            {([
              ["both", "双方"],
              ["me", "仅我方"],
              ["other", "仅对方"],
            ] as const).map(([v, label]) => (
              <button
                key={v}
                onClick={() => setFilter(v)}
                className={`rounded-full px-2.5 py-1 text-[11px] ${
                  filter === v
                    ? "bg-(--accent) font-medium text-(--accent-ink)"
                    : "border border-(--card-border) text-(--stage-muted)"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
        <div ref={listRef} className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-1 py-2">
          {items.length === 0 && (
            <div className="flex flex-1 items-center justify-center font-mono text-[10px] uppercase tracking-[0.16em] text-(--stage-muted)">
              等说话…开口即译
            </div>
          )}
          {items.map((t, i) => {
            const idx = offset + i;
            if (idx < clearedCount) return null;
            const who = whoIs(t, p.room, p.myLang, p.otherLang);
            if (filter === "me" && who.side !== "right") return null;
            if (filter === "other" && who.side !== "left") return null;
            return (
              <div key={`${who.text}-${idx}`} className={`flex ${who.side === "right" ? "justify-end" : "justify-start"}`}>
                <div
                  className={`max-w-[85%] rounded-lg px-3 py-2 text-[13px] leading-relaxed ${
                    who.kind === "dst"
                      ? "border border-(--card-border) bg-white/5 text-(--foreground)"
                      : "bg-(--accent) text-(--accent-ink)"
                  }`}
                >
                  <span className="mr-1.5 font-mono text-[10px] font-bold uppercase opacity-70">{who.text}</span>
                  {String(t.text ?? "")}
                </div>
              </div>
            );
          })}
        </div>
        {liveBusy && (
          <p className="shrink-0 text-[11px] text-amber-300">
            {p.othHeld ? "我方译文播报中 · 对方麦克风暂让" : "对方译文播报中 · 我方麦克风暂让"}
          </p>
        )}
        {p.sameDeviceWarning && <p className="shrink-0 text-xs text-amber-300">{p.sameDeviceWarning}</p>}
        {p.error && <p className="shrink-0 text-xs text-red-400">{p.error}</p>}
      </section>
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
  if (id.startsWith("me-")) return { text: "我方说的", side: "right", kind: "src" };
  if (id.startsWith("other-")) return { text: "对方说的", side: "left", kind: "src" };
  const trackSid = t.streamInfo?.attributes?.["lk.transcribed_track_id"] ?? "";
  if (trackSid) {
    const pools = [room.remoteParticipants.values(), [room.localParticipant].values()];
    for (const pool of pools) {
      for (const participant of pool) {
        for (const pub of Object.values(participant.trackPublications ?? {})) {
          if (pub?.trackSid !== trackSid) continue;
          const owner = String(participant.identity ?? "");
          if (owner.startsWith("me-")) return { text: "我方说的", side: "right", kind: "src" };
          if (owner.startsWith("other-")) return { text: "对方说的", side: "left", kind: "src" };
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

/**
 * 监听一个房间的 trans-* 译文音轨出声,出声(含 600ms 余量)期间 setHeld(true)。
 * analyser 挂不上(内核限制/上下文 suspended)就静默退化——半双工不生效但链路不受影响,
 * 用户仍可用手动静音按钮兜底。
 */
function watchTransAudio(room: Room | null, setHeld: (v: boolean) => void): () => void {
  if (!room) return () => {};
  let ctx: AudioContext | null = null;
  const nodes = new Map<string, { source: MediaStreamAudioSourceNode; analyser: AnalyserNode }>();

  const attach = (track: RemoteTrack, pub: RemoteTrackPublication) => {
    if (!String(pub.trackName ?? "").startsWith("trans-")) return;
    try {
      ctx = ctx ?? new AudioContext();
      if (ctx.state === "suspended") ctx.resume().catch(() => {});
      const source = ctx.createMediaStreamSource(new MediaStream([track.mediaStreamTrack]));
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      source.connect(analyser);
      nodes.set(track.sid, { source, analyser });
    } catch {
      /* ignore */
    }
  };
  const detach = (track: RemoteTrack) => {
    nodes.delete(track.sid);
  };
  const onSub = (track: RemoteTrack, pub: RemoteTrackPublication) => attach(track, pub);
  room.on(RoomEvent.TrackSubscribed, onSub);
  room.on(RoomEvent.TrackUnsubscribed, detach);
  // 监听启动晚于发轨(如连接已完成)时,补挂已在房的轨。
  for (const participant of room.remoteParticipants.values()) {
    for (const pub of participant.trackPublications.values()) {
      const t = pub.track;
      if (t) attach(t as RemoteTrack, pub);
    }
  }

  const buf = new Float32Array(512);
  let busyUntil = 0;
  let heldSince = 0;
  let quietUntil = 0;
  const timer = window.setInterval(() => {
    // 看门狗(2026-09-11 审计 P0-3):AudioContext 被系统挂起(WKWebView 切后台/
    // 长会话音频路由切换)时 analyser 数据会冻结在最后一帧——冻结在响段令
    // busyUntil 无限续期、对向麦克风被永久暂让(fail-closed = 零翻译)。持续
    // resume + 单次连续 hold 超 10s 强制释放并给 5s 说话冷却窗(fail-open 串译
    // 优于永久压麦,用户可用手动静音按钮兜底)。
    if (ctx && ctx.state !== "running") ctx.resume().catch(() => {});
    const now = Date.now();
    let loud = false;
    if (now >= quietUntil) {
      for (const { analyser } of nodes.values()) {
        analyser.getFloatTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
        if (Math.sqrt(sum / buf.length) > 0.012) {
          loud = true;
          break;
        }
      }
    }
    if (loud) busyUntil = now + 600;
    const held = now < busyUntil;
    if (held && !heldSince) heldSince = now;
    if (!held) heldSince = 0;
    if (heldSince && now - heldSince > 10_000) {
      busyUntil = 0;
      heldSince = 0;
      quietUntil = now + 5_000;
    }
    setHeld(held);
  }, 120);

  return () => {
    window.clearInterval(timer);
    room.off(RoomEvent.TrackSubscribed, onSub);
    room.off(RoomEvent.TrackUnsubscribed, detach);
    for (const { source } of nodes.values()) {
      try {
        source.disconnect();
      } catch {
        /* ignore */
      }
    }
    nodes.clear();
    if (ctx) ctx.close().catch(() => {});
  };
}

async function fetchToken(account: string, callId: string, role: "me" | "other"): Promise<{ serverUrl: string; participantToken: string }> {
  const resp = await fetch(`${apiBase()}/api/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ account_id: account, call_id: callId, participant_identity: `${role}-${callId}` }),
  });
  if (!resp.ok) throw new Error(`token http ${resp.status}`);
  const data = (await resp.json()) as { serverUrl?: string; participantToken?: string };
  if (!data.serverUrl || !data.participantToken) throw new Error("token 响应缺 serverUrl/participantToken");
  return { serverUrl: data.serverUrl, participantToken: data.participantToken };
}
