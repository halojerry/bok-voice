"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  StartAudio,
  VoiceAssistantControlBar,
  useAgent,
  useAgentExpression,
  useAudioPlayback,
  useSession,
  useTranscriptions,
} from "@livekit/components-react";
import { ConnectionState, TokenSource, Track, type Room } from "livekit-client";
import { api } from "@/lib/api";
import { describeConnectError, friendlyErrorText, useControlPlaneReady } from "@/lib/api-ready";
import { applyOutputDevice, listAudioDevicesOf, requestMicPermission, saveMicDevice, savedMicDevice, savedOutputDevice, switchWebOutputDevice, webCanSwitchOutput, isTauriShell, type AudioDeviceInfo } from "@/lib/audio";
import { AgentSessionProvider } from "@/components/agents-ui/agent-session-provider";
import { VoiceAgentInterface } from "@/components/VoiceAgentInterface";
import { useAccount } from "@/components/account-context";

function AgentStateLabel({ state }: { state: string }) {
  const map: Record<string, { label: string; color: string }> = {
    idle: { label: "待机", color: "bg-neutral-500" },
    "pre-connect-buffering": { label: "预连接缓冲", color: "bg-amber-400" },
    connecting: { label: "连接中", color: "bg-neutral-400" },
    initializing: { label: "初始化", color: "bg-amber-400" },
    listening: { label: "聆听中", color: "bg-emerald-400" },
    thinking: { label: "思考中", color: "bg-sky-400" },
    speaking: { label: "说话中", color: "bg-fuchsia-400" },
    disconnected: { label: "已断开", color: "bg-neutral-600" },
    failed: { label: "失败", color: "bg-red-500" },
  };
  const item = map[state] ?? map.connecting;
  return (
    <span className="inline-flex items-center gap-2 text-sm">
      <span className={`h-2.5 w-2.5 rounded-full ${item.color} animate-pulse`} />
      {item.label}
    </span>
  );
}

/**
 * 官方 Agents UI 会话面板：LiveKit Aura 可视化（情绪驱动颜色）+ 官方控制条。
 * 转写暂用 useTranscriptions 自绘（视觉已对齐官方；官方 AgentChatTranscript 需 Tailwind v4，见 AGENT.md）。
 */
function LiveAgentPanel({ room }: { room: Room | null }) {
  const { state, microphoneTrack, identity, failureReasons } = useAgent();
  const { mood } = useAgentExpression();
  const transcriptions = useTranscriptions();
  const agentIdentity = identity ?? "agent";
  // 按时间正序(旧→新,最新在底部),像常规聊天一样自动滚到底看最新一条。
  const recent = useMemo(() => transcriptions.slice(-20), [transcriptions]);
  const agentState = state ?? "connecting";
  const listRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    // 新气泡到达时滚到容器底部 = 最新消息,不被旧消息顶开。
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [recent]);
  const speaking = agentState === "speaking";

  // 实时分析：基于本通转写实时统计（非挂断后结算值）。
  const liveStats = useMemo(() => {
    const texts = transcriptions
      .map((t) => String(t.text ?? ""))
      .filter(Boolean);
    if (texts.length === 0) return null;
    const all = texts.join(" ");
    const turnCount = texts.length;
    const density = Math.round(all.length / Math.max(1, turnCount));
    const fillers = (all.match(/嗯|啊|那个|就是|咁|啦|uh|um/gi) ?? []).length;
    const hedges = (all.match(/可能|大概|应该|我觉得|我諗/gi) ?? []).length;
    return { turnCount, density, fillers, hedges };
  }, [transcriptions]);

  return (
    <div className="flex h-full min-h-0 flex-1 flex-col overflow-hidden">
      {/* 转写时间线：固定占中栏剩余空间(至少 280px 高,内部滚动),绝不会被压没。
          对话记录始终可见,多轮后在该区域内滚,不把页面顶走。 */}
      <div ref={listRef} className="flex min-h-[280px] flex-1 flex-col gap-2 overflow-y-auto px-1 py-2">
        {recent.length === 0 && (
          <div className="flex flex-1 items-center justify-center font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-[var(--stage-muted)]">
            等待对话…
          </div>
        )}
        {recent.map((t, i) => {
          const identity = t.participantInfo?.identity ?? "";
          const text = String(t.text ?? "");
          const isAgent = identity === agentIdentity;
          const ts = t.streamInfo?.timestamp ? new Date(t.streamInfo.timestamp).toLocaleTimeString([], { hour12: false }) : "";
          return (
            <div key={`${identity}-${i}`} className={`flex ${isAgent ? "justify-start" : "justify-end"}`}>
              <div
                className={`max-w-[80%] rounded-lg px-3 py-2 font-mono text-[11px] leading-relaxed ${
                  isAgent
                    ? "bg-[var(--accent)] text-[var(--accent-ink)]"
                    : "bg-white/10 text-[var(--foreground)]"
                }`}
              >
                <span className="mr-1.5 font-bold uppercase">{isAgent ? "AGENT" : "YOU"}</span>
                {ts && <span className="ml-1 mr-1 opacity-60">{ts}</span>}
                {text}
              </div>
            </div>
          );
        })}
        {speaking && recent.length > 0 && (
          <div className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--stage-value)]">
            <span className="h-1.5 w-1.5 rounded-full bg-[var(--stage-value)] animate-pulse" />
            AI 说话中
          </div>
        )}
      </div>

      {/* 中央：官方点阵可视化（mood 驱动色）；sm 尺寸并 shrink-0，把纵向空间让给转写 */}
      <div className="flex shrink-0 flex-col items-center justify-center gap-1 py-2">
        <div className="flex items-center gap-4">
          <AgentStateLabel state={agentState} />
          <VoiceAgentInterface
            size="sm"
            state={agentState}
            mood={mood}
            audioTrack={microphoneTrack}
            showMoodLabel
          />
        </div>
        {/* 官方失败态显性化:agent/会话失败不能只显示一个「失败」点,把原因亮出来。
            useAgent 未连接会话时 failureReasons 可能为 null——空值守卫,避免开页即崩。 */}
        {(failureReasons?.length ?? 0) > 0 && (
          <div className="max-w-[420px] text-center text-xs text-red-500">
            连接失败：{(failureReasons ?? []).join("；")}
          </div>
        )}
      </div>

      {/* 实时分析（基于本通转写实时统计） */}
      {liveStats && (
        <div className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 border-t border-[var(--card-border)] px-2 py-1.5 text-[10px] text-[var(--muted)]">
          <span>轮次 <b className="text-[var(--foreground)]">{liveStats.turnCount}</b></span>
          <span>均每轮字数 <b className="text-[var(--foreground)]">{liveStats.density}</b></span>
          <span>填充词 <b className="text-[var(--foreground)]">{liveStats.fillers}</b></span>
          <span>犹豫词 <b className="text-[var(--foreground)]">{liveStats.hedges}</b></span>
        </div>
      )}

      {/* 控制条（AgentSessionProvider 已内置音频渲染）；设备切换已移到右侧「音频设备」卡片 */}
      <div className="flex shrink-0 flex-col items-center gap-2 border-t border-[var(--card-border)] py-2">
        <div className="flex items-center justify-center gap-3">
          <StartAudio label="点击开启声音" />
          <VoiceAssistantControlBar />
        </div>
      </div>
    </div>
  );
}

/** 麦克风实时波形：分析本地发布的 mic track，确认声音真的在采集/上行。 */
function MicLevelMeter({ room }: { room: Room | null }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [active, setActive] = useState(false);
  const [error, setError] = useState("");
  const [level, setLevel] = useState(0); // 0-100 峰值
  const [trackInfo, setTrackInfo] = useState("");

  useEffect(() => {
    if (!room) return;
    let raf = 0;
    let analyser: AnalyserNode | null = null;
    let data: Uint8Array<ArrayBuffer> | null = null;
    let ctx: AudioContext | null = null;

    const grab = () => {
      const pub = room.localParticipant.getTrackPublication(Track.Source.Microphone);
      const mt = pub?.track as { mediaStreamTrack?: MediaStreamTrack } | undefined;
      const mst = mt?.mediaStreamTrack;
      if (!mst || mst.readyState !== "live") {
        setActive(false);
        setTrackInfo("无 live 麦克风 track(未发布/被拒)");
        return;
      }
      setTrackInfo(`${mst.label || "麦克风"} · ${mst.readyState} · ${mst.getSettings?.().deviceId ? "deviceId 已定" : "deviceId 未定"}`);
      try {
        const AC = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
        if (!AC) return;
        ctx = new AC();
        const src = ctx.createMediaStreamSource(new MediaStream([mst]));
        analyser = ctx.createAnalyser();
        analyser.fftSize = 512;
        src.connect(analyser);
        data = new Uint8Array(new ArrayBuffer(analyser.frequencyBinCount));
        setActive(true);
        setError("");
      } catch (e) {
        setError(String(e));
      }
    };

    const draw = () => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const g = canvas.getContext("2d");
      if (!g) return;
      const W = canvas.width, H = canvas.height;
      g.clearRect(0, 0, W, H);
      g.fillStyle = "rgba(255,255,255,0.04)";
      g.fillRect(0, 0, W, H);
      if (analyser && data) {
        analyser.getByteTimeDomainData(data);
        // RMS → 电平(0-100)
        let sum = 0;
        for (let i = 0; i < data.length; i++) {
          const v = (data[i] - 128) / 128;
          sum += v * v;
        }
        const rms = Math.sqrt(sum / data.length);
        setLevel(Math.min(100, Math.round(rms * 220)));
        g.strokeStyle = "var(--accent, #22d3ee)";
        g.lineWidth = 2;
        g.beginPath();
        for (let i = 0; i < data.length; i++) {
          const x = (i / data.length) * W;
          const y = (data[i] / 255) * H;
          i === 0 ? g.moveTo(x, y) : g.lineTo(x, y);
        }
        g.stroke();
      }
      raf = requestAnimationFrame(draw);
    };

    // room 连接后本地 mic track 才出现：轮询一小段等 track 就绪。
    const tryStart = () => {
      grab();
      if (!analyser) {
        setTimeout(tryStart, 300);
        return;
      }
      draw();
    };
    tryStart();

    const onTrack = () => { grab(); if (analyser) draw(); };
    room.localParticipant.on("trackPublished", onTrack);
    return () => {
      cancelAnimationFrame(raf);
      room.localParticipant.off("trackPublished", onTrack);
      if (ctx) void ctx.close().catch(() => {});
    };
  }, [room]);

  return (
    <div className="mt-2">
      <div className="flex items-center justify-between">
        <span className="text-[10px] text-[var(--muted)]">麦克风输入波形</span>
        <span className={`text-[10px] ${active ? "text-emerald-400" : "text-[var(--muted)]"}`}>
          {active ? `● 采集中 ${level > 3 ? `音量 ${level}` : "(静音)"}` : error ? "无法分析" : "未采集"}
        </span>
      </div>
      <canvas ref={canvasRef} width={260} height={40} className="mt-1 w-full rounded bg-black/20" />
      {trackInfo && <p className="mt-0.5 truncate text-[9px] text-[var(--muted)]" title={trackInfo}>{trackInfo}</p>}
      {error && <p className="mt-1 text-[10px] text-red-300">{error}</p>}
    </div>
  );
}

/** 音频设备卡片：麦克风(Web enumerate) + 扬声器(桌面 CoreAudio / 浏览器 setSinkId)。 */
function AudioDevicesCard({ room }: { room: Room | null }) {
  const [micDevices, setMicDevices] = useState<AudioDeviceInfo[]>([]);
  const [micId, setMicId] = useState("");
  const [micNote, setMicNote] = useState("");
  const [outputDevices, setOutputDevices] = useState<AudioDeviceInfo[]>([]);
  // 扬声器可用性与已存值都放 state，挂载后再读(避免 SSR 读 localStorage 造成 Hydration 不匹配)
  const [outputCanSwitch, setOutputCanSwitch] = useState(false);
  const [outId, setOutId] = useState("");
  useEffect(() => {
    setOutputCanSwitch(isTauriShell() || webCanSwitchOutput());
    setOutId(savedOutputDevice());
  }, []);

  const refreshMic = async () => {
    const mics = await listAudioDevicesOf("input").catch(() => []);
    setMicDevices(mics);
    const savedMic = savedMicDevice();
    const next = savedMic && mics.some((m) => m.id === savedMic)
      ? savedMic
      : mics.find((m) => m.is_default)?.id ?? mics[0]?.id ?? "";
    setMicId(next);
    if (next) saveMicDevice(next);
    if (next && next !== savedMic && room) {
      room.switchActiveDevice("audioinput", next, false).catch(() => {});
    }
  };
  useEffect(() => {
    if (room) refreshMic();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [room]);
  useEffect(() => {
    if (outputCanSwitch) listAudioDevicesOf("output").then(setOutputDevices).catch(() => {});
  }, [outputCanSwitch]);

  const changeOutput = async (id: string) => {
    if (!id) return;
    setOutId(id);
    try {
      localStorage.setItem("bok.audio.out", id);
    } catch {
      /* ignore */
    }
    if (isTauriShell()) {
      await applyOutputDevice(id);
    } else if (room) {
      await switchWebOutputDevice(room, id);
    }
  };

  return (
    <div className="rounded-lg bg-white/5 p-3">
      <span className="label mb-2 block">音频设备</span>
      <div className="space-y-2 text-xs">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[var(--muted)]">麦克风</span>
          <div className="flex min-w-0 items-center gap-1">
            <select
              className="max-w-[150px] rounded-lg border border-[var(--card-border)] bg-transparent px-2 py-1 text-xs outline-none focus:border-[var(--accent)]"
              value={micId}
              onChange={(e) => {
                const id = e.target.value;
                if (!id) return;
                setMicId(id);
                saveMicDevice(id);
                void room?.switchActiveDevice("audioinput", id, false).catch(() => {});
              }}
            >
              {micDevices.length === 0 && <option value="">未检测到麦克风</option>}
              {micDevices.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                  {m.is_default ? "（默认）" : ""}
                </option>
              ))}
            </select>
            <button
              className="rounded-lg border border-[var(--card-border)] px-2 py-1 opacity-70 hover:opacity-100"
              onClick={async () => {
                const ok = await requestMicPermission();
                setMicNote(ok ? "" : "麦克风权限被拒绝。请在 系统设置 › 隐私与安全性 › 麦克风 中允许本应用。");
                await refreshMic();
              }}
            >
              刷新
            </button>
          </div>
        </div>
        {micDevices.length === 0 && micNote && <p className="text-red-300">{micNote}</p>}
        <div className="flex items-center justify-between gap-2">
          <span className="text-[var(--muted)]">扬声器</span>
          {outputCanSwitch ? (
            <select
              className="max-w-[150px] rounded-lg border border-[var(--card-border)] bg-transparent px-2 py-1 text-xs outline-none focus:border-[var(--accent)]"
              value={outId}
              onChange={(e) => { void changeOutput(e.target.value); }}
            >
              <option value="" disabled>跟随系统</option>
              {outputDevices.map((d) => (
                <option key={d.id} value={d.id}>{d.name}</option>
              ))}
            </select>
          ) : (
            <span className="opacity-70">跟随系统默认</span>
          )}
        </div>
        {!room && <p className="text-[var(--muted)]">接通后可用</p>}
        <MicLevelMeter room={room} />
      </div>
    </div>
  );
}

/** 未接通空态：官方点阵（connecting 演示态）替代手绘 canvas */
function IdleStage() {
  return (
    <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 p-6 text-center">
      <VoiceAgentInterface state="connecting" size="md" />
      <p className="stage-value stage-glow mt-2">Live Agent</p>
      <p className="text-sm text-[var(--foreground)]">点击「接通」开始与 AI 助手对话</p>
      <p className="text-xs text-[var(--muted)]">浏览器将请求麦克风权限</p>
    </div>
  );
}

const PROVIDER_FIELDS: [string, string][] = [
  ["asr", "ASR"],
  ["llm", "LLM"],
  ["tts", "TTS"],
  ["vad", "VAD"],
];

function str(v: unknown, fallback = "-") {
  return v === undefined || v === null || v === "" ? fallback : String(v);
}

export function CallStudio({ callId = "" }: { callId?: string }) {
  const { accountId: ACCOUNT } = useAccount();
  // 记住本账号上一次使用的人设/对象：新建通话默认恢复它(而非恒取列表第一个),
  // 挂断后切新人设/对象 → 接通即用新选择,唔会悄悄回到上个对话的档案。
  const lastKey = (kind: "persona" | "object") => `bok.call.${kind}.${ACCOUNT}`;
  const lsGet = (k: string): string => {
    try {
      return typeof window !== "undefined" ? window.localStorage.getItem(k) ?? "" : "";
    } catch {
      return "";
    }
  };
  const lsSet = (k: string, v: string) => {
    try {
      window.localStorage.setItem(k, v);
    } catch {
      /* ignore */
    }
  };
  const [stateCallId, setStateCallId] = useState(callId);
  const callIdRef = useRef(callId);
  // hydrate（打开历史通话）写入的 objId/personaId 唔算「用户选择」——唔入「上次选择」账，
  // 否则看过一眼旧通话就会污染之后新建通话的默认档案（2026-09-05 审查 P2）。
  const suppressPersist = useRef(0);
  const objIdRef = useRef("");
  const personaIdRef = useRef("");
  const [objects, setObjects] = useState<Record<string, unknown>[]>([]);
  const [personas, setPersonas] = useState<Record<string, unknown>[]>([]);
  const [objId, setObjId] = useState("");
  const [personaId, setPersonaId] = useState("");
  objIdRef.current = objId;
  personaIdRef.current = personaId;
  const [object, setObject] = useState<Record<string, unknown> | null>(null);
  const [persona, setPersona] = useState<Record<string, unknown> | null>(null);
  const [mode, setMode] = useState<"simulation" | "live">("simulation");
  const [settlement, setSettlement] = useState<Record<string, unknown> | null>(null);
  const [objectTopics, setObjectTopics] = useState<Record<string, unknown>[]>([]);
  const [settings, setSettings] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);
  // 服务就绪自愈：桌面壳异步拉起整栈，首次加载失败后在 Control Plane 就绪时自动重拉。
  const cp = useControlPlaneReady();
  const loadAttemptRef = useRef(-1);

  // 官方会话：CP /api/token 已说官方 TokenSource 契约({serverUrl, participantToken})，
  // 这里直透响应体、零键名映射；TokenSource.custom 自带 exp 前缓存与自动续签。
  // （此处不用 TokenSource.endpoint+useSession options：「新建通话」要先把刚拿到的
  //   callId 同步进请求——useSession options 经 render 传播，时序上拿不到本轮 id，
  //   custom 闭包读 callIdRef 恒为最新。）
  const tokenSource = useMemo(
    () =>
      TokenSource.custom(async () => {
        const id = callIdRef.current;
        if (!id) throw new Error("no call id");
        return await api.token({ account_id: ACCOUNT, call_id: id });
      }),
    [],
  );
  const session = useSession(tokenSource);
  const { canPlayAudio, startAudio } = useAudioPlayback(session.room);

  // 真实连接态：以房间状态为准，而不是「callId 非空」冒充。修复了带历史通话 id
  // 进来自动显示"已连接"、却只有一个会真挂断的按钮、无法接通的隐患。
  const roomConnected = session.room.state === ConnectionState.Connected;
  const isJoiningExisting = Boolean(stateCallId);
  // 主管操作（暂停/接管/转人工）状态；挂断走 leave()。
  const [superviseMsg, setSuperviseMsg] = useState("");
  const [superviseBusy, setSuperviseBusy] = useState(false);
  // WhatsApp 對接通知:開住工作台期間 poll call 狀態,offered/captured → 面板內橫幅。
  const [waStatus, setWaStatus] = useState("");
  const [waNum, setWaNum] = useState("");
  const [waHandling, setWaHandling] = useState(false);

  // WhatsApp 對接:開住工作台時 3s poll call 狀態(offered/captured→面板橫幅;handled→收起)。
  useEffect(() => {
    if (!stateCallId) return;
    let stopped = false;
    const load = async () => {
      try {
        const c = (await api.getCall(stateCallId)) as Record<string, unknown> & {
          whatsapp_status?: string;
          customer_whatsapp?: string;
        };
        if (stopped) return;
        setWaStatus(String(c.whatsapp_status ?? ""));
        setWaNum(String(c.customer_whatsapp ?? ""));
      } catch {
        /* control-plane 一時唔得就等下輪 */
      }
    };
    load();
    const t = setInterval(load, 3000);
    return () => {
      stopped = true;
      clearInterval(t);
    };
  }, [stateCallId]);

  async function markWaHandled() {
    const id = callIdRef.current;
    if (!id) return;
    setWaHandling(true);
    try {
      await api.markWhatsappHandled(id);
      setWaStatus("handled");
    } catch (e) {
      setSuperviseMsg(friendlyErrorText(String(e)));
    } finally {
      setWaHandling(false);
    }
  }

  async function supervisorAct(kind: "pause" | "resume" | "takeover" | "transfer") {
    const id = callIdRef.current;
    if (!id) return;
    setSuperviseBusy(true);
    setSuperviseMsg("");
    try {
      const fn =
        kind === "pause" ? api.supervisorPause
        : kind === "resume" ? api.supervisorResume
        : kind === "takeover" ? api.supervisorTakeover
        : api.supervisorTransfer;
      const r = await fn(id);
      const status = (r as { status?: string })?.status;
      setSuperviseMsg(`${kind === "pause" ? "已暂停" : kind === "resume" ? "已恢复" : kind === "takeover" ? "已转人工接管" : "已转人工"}${status ? `（状态：${status}）` : ""}`);
    } catch (e) {
      setSuperviseMsg(friendlyErrorText(String(e)));
    } finally {
      setSuperviseBusy(false);
    }
  }

  // Load selectable objects + personas and default to the first. Runs on mount and
  // again on each Control Plane offline→ready transition (desktop cold start), so
  // the "接通" button never stays dead behind a one-shot network error.
  useEffect(() => {
    if (loadAttemptRef.current === cp.attempt) return;
    loadAttemptRef.current = cp.attempt;
    // 工作台模式（callId 非空）：档案由 hydrate 从服务端解析,列表默认选择唔跑——
    // 两者异步竞态会把历史通话档案覆写成「列表首个/上次」。
    if (callId) return;
    let cancelled = false;
    Promise.all([api.listObjects(ACCOUNT), api.listPersonas()])
      .then(([objs, pers]) => {
        if (cancelled) return;
        if (Array.isArray(objs)) setObjects(objs);
        if (Array.isArray(pers)) setPersonas(pers);
        // 默认选「上次用的人设/对象」(存在且在列表内);否则取第一个。
        const lastObj = lsGet(lastKey("object"));
        const lastPers = lsGet(lastKey("persona"));
        if (objs?.length) {
          const picked = objs.find((o) => String(o.id) === lastObj) ? lastObj : String(objs[0].id);
          setObjId(picked);
        }
        if (pers?.length) {
          const picked = pers.find((p) => String(p.id) === lastPers) ? lastPers : String(pers[0].id);
          setPersonaId(picked);
        }
        setError(null);
      })
      .catch((e) => {
        if (!cancelled) setError(friendlyErrorText(String(e)));
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cp.attempt, ACCOUNT]);

  // 拉取全局设置：右栏 Provider 卡显示实际生效的 provider(而非硬编码)。
  useEffect(() => {
    let cancelled = false;
    api.getSettings().then((s) => { if (!cancelled) setSettings(s); }).catch(() => {});
    return () => { cancelled = true; };
  }, [cp.attempt]);

  // For an existing call (e.g. /calls/[id]), hydrate everything from the server.
  useEffect(() => {
    if (!callId) return;
    setStateCallId(callId);
    callIdRef.current = callId;
    api
      .getCall(callId)
      .then((c) => {
        if (c.object_id && String(c.object_id) !== objIdRef.current) suppressPersist.current += 1;
        if (c.persona_id && String(c.persona_id) !== personaIdRef.current) suppressPersist.current += 1;
        if (c.object_id) setObjId(String(c.object_id));
        if (c.persona_id) setPersonaId(String(c.persona_id));
        if (c.mode) setMode(c.mode as "simulation" | "live");
      })
      .catch((e) => setError(friendlyErrorText(String(e))));
  }, [callId]);

  // Fetch object / persona when selected or resolved from a call.
  useEffect(() => {
    if (!objId) return;
    api.getObject(objId).then(setObject).catch(() => {});
    // 该对象历史沉淀主题（结算时 Summarizer 蒸馏写入），用于左栏展示。
    api.getObjectTopics(objId).then(setObjectTopics).catch(() => {});
  }, [objId]);
  useEffect(() => {
    if (!personaId) return;
    api.getPersona(personaId).then(setPersona).catch(() => {});
  }, [personaId]);

  // 记住每次选择：新建通话/挂断後还原到上次用的人设与对象。
  useEffect(() => {
    if (!objId) return;
    if (suppressPersist.current > 0) {
      suppressPersist.current -= 1;
      return;
    }
    lsSet(lastKey("object"), objId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [objId]);
  useEffect(() => {
    if (!personaId) return;
    if (suppressPersist.current > 0) {
      suppressPersist.current -= 1;
      return;
    }
    lsSet(lastKey("persona"), personaId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [personaId]);

  // Load settlement only for an existing call view (e.g. /calls/[id]).
  useEffect(() => {
    if (!callId) return;
    api
      .getSettlement(callId)
      .then(setSettlement)
      .catch(() => setSettlement(null));
  }, [callId]);

  async function connect() {
    setError(null);
    setConnecting(true);
    let phase: "create-call" | "join-session" = "create-call";
    try {
      let id = stateCallId;
      if (!id) {
        // 会话默认语言：人设语言优先（AI 该用什么语言与客户沟通，是用户对人设的明确设定），
        // 其次对象(客户)语言，最后普通话。此前恒取对象语言且对象默认 "vi" 会导致语言失效。
        const callLang = String(persona?.language || object?.language || "zh");
        const created = await api.createCall({
          account_id: ACCOUNT,
          object_id: objId,
          persona_id: personaId,
          mode,
          direction: "webrtc",
          language: callLang,
        });
        id = String(created.id);
        setStateCallId(id);
        callIdRef.current = id;
      } else {
        // Join 已结束嘅 call:LiveKit room 已清,簽咗 token 去 join 會 401
        // (前端會誤報「令牌校驗失敗」)。直接攔截,叫用戶開新通話。
        const cur = (await api.getCall(id).catch(() => null)) as
          | (Record<string, unknown> & { status?: string })
          | null;
        if (cur && String(cur.status ?? "") === "ended") {
          setConnecting(false);
          setError("该通话已结束（房间已关闭），无法重新接通。请返回列表发起新通话。");
          return;
        }
      }
      setConnecting(false);
      phase = "join-session";
      // 应用用户选择的音频设备：麦克风先设默认采集设备（session.start 开麦时会采用），
      // 扬声器：桌面壳切系统默认输出；浏览器经 livekit setSinkId。
      const micDeviceId = savedMicDevice();
      const outputDeviceId = savedOutputDevice();
      // 非 exact：设备不存在/已插拔时回退默认，避免采集失败（exact 会 reject）。
      if (micDeviceId) await session.room.switchActiveDevice("audioinput", micDeviceId, false).catch(() => {});
      if (outputDeviceId) {
        if (isTauriShell()) await applyOutputDevice(outputDeviceId).catch(() => {});
        else if (webCanSwitchOutput()) await switchWebOutputDevice(session.room, outputDeviceId).catch(() => {});
      }
      // 连接前预缓冲：建房/agent join 需 1-2s，用户此时可能已开口（喂你好），
      // preConnectBuffer 把这段采集缓冲在连接后回放给 agent，避免「接通吃头字」。
      // （agent 侧 1.7.1 的 pre_connect_audio 默认已开。）
      await session.room.localParticipant.setMicrophoneEnabled(true, undefined, { preConnectBuffer: true }).catch(() => {});
      await session.start({ tracks: { microphone: { enabled: true } } });
      // 确保本地麦克风真正发布：session.start 的 tracks 选项在部分 livekit 版本不生效，
      // 显式 setMicrophoneEnabled 才可靠（否则 agent 收不到用户声音 → 对话"没输入"）。
      try {
        const pub = await session.room.localParticipant.setMicrophoneEnabled(true);
        if (!pub) {
          console.warn("mic publish returned no track — 检查浏览器麦克风权限");
        }
      } catch (e) {
        console.warn("enable microphone failed", e);
        setError("无法开启麦克风：请检查浏览器地址栏的麦克风权限是否允许。");
        setConnecting(false);
        return;
      }
      if (!canPlayAudio) startAudio().catch(() => {});
    } catch (e) {
      console.error("connect failed", e);
      setError(describeConnectError(e, phase));
      setConnecting(false);
    }
  }

  async function leave() {
    // 先断开官方会话，再挂断 + 结算（业务流保留）。
    try {
      await session.end();
    } catch {
      /* ignore */
    }
    if (stateCallId) {
      try {
        await api.hangup(stateCallId);
        const s = await api.getSettlement(stateCallId);
        setSettlement(s);
      } catch (e) {
        // 未有结算(未 settle)时 /settlement 404 属预期,唔刷 console。
        if (!String(e).includes("404")) console.warn("settle failed", e);
      }
    }
    setStateCallId("");
    callIdRef.current = "";
  }

  return (
    <div className="grid grid-cols-[280px_1fr_300px] gap-6 lg:h-[calc(100vh-7.5rem)]">
      {/* 左：对象档案 / 人设 */}
      <section className="card flex min-h-0 flex-col gap-4 overflow-y-auto">
        <div className="flex items-center justify-between">
          <span className="label">对象档案</span>
          {!stateCallId && (
            <select
              className="select px-2 py-1 text-xs"
              value={mode}
              onChange={(e) => setMode(e.target.value as "simulation" | "live")}
            >
              <option value="simulation">训练模式</option>
              <option value="live">真实业务</option>
            </select>
          )}
        </div>

        {!stateCallId && (
          <>
            <select className="select" value={objId} onChange={(e) => setObjId(e.target.value)}>
              {objects.length === 0 && <option value="">请先建档对象</option>}
              {objects.map((o) => (
                <option key={String(o.id)} value={String(o.id)}>
                  {str(o.display_name)}（{str(o.role_template)}）
                </option>
              ))}
            </select>
            <select
              className="select"
              value={personaId}
              onChange={(e) => setPersonaId(e.target.value)}
            >
              {personas.length === 0 && <option value="">默认人设</option>}
              {personas.map((p) => (
                <option key={String(p.id)} value={String(p.id)}>
                  {str(p.name)} · {str(p.company)}
                </option>
              ))}
            </select>
          </>
        )}

        <h2 className="text-lg font-semibold">{object ? str(object.display_name) : "未选择对象"}</h2>
        {object && (
          <p className="text-xs text-[var(--muted)]">
            {str(object.role_template)} / {str(object.language)} {object.phone ? `· ${str(object.phone)}` : ""}
          </p>
        )}
        {object?.background && (
          <p className="rounded-lg bg-white/5 p-3 text-sm text-[var(--muted)]">{String(object.background)}</p>
        )}

        <div className="rounded-lg bg-white/5 p-3 text-sm">
          <span className="label mb-1 block">历史主题</span>
          {objectTopics.length === 0 ? (
            <p className="text-[var(--muted)]">暂无（挂断结算后自动沉淀）</p>
          ) : (
            <ul className="space-y-1.5">
              {objectTopics.slice(-5).map((t) => (
                <li key={String(t.id ?? t.topic ?? "")} className="text-[var(--muted)]">
                  <span className="text-[var(--foreground)]">{str(t.topic)}</span>
                  {str(t.summary) ? ` — ${str(t.summary)}` : ""}
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="rounded-lg bg-white/5 p-3 text-sm">
          <span className="label mb-1 block">我方人设</span>
          {persona ? (
            <>
              <p className="font-medium">{str(persona.name)}</p>
              <p className="text-[var(--muted)]">
                {str(persona.company)} · {str(persona.tone)}
              </p>
            </>
          ) : (
            <p className="text-[var(--muted)]">默认人设未配置</p>
          )}
        </div>
        <div className="mt-auto rounded-lg border border-dashed border-[var(--card-border)] p-3 text-xs text-[var(--muted)]">
          对象档案与知识库由当前账号注入，挂断后自动沉淀到该账号。
        </div>
      </section>

      {/* 中：官方 LiveKit 会话台（AgentSessionProvider + 官方可视化） */}
      <section className="card flex min-h-0 flex-col">
        <div className="mb-4 flex items-center justify-between">
          <div className="text-xs text-[var(--muted)]">
            {stateCallId ? `会话 ${stateCallId}` : "新建会话"}
          </div>
          <div className="flex flex-col items-end gap-2">
            <div className="flex gap-2">
              {!roomConnected ? (
                <div className="flex flex-col items-end gap-1">
                  {!stateCallId && objId && (
                    <p className="text-[10px] text-[var(--muted)]">
                      接通后：{str(objects.find((o) => String(o.id) === objId)?.display_name)}{" "}
                      · {str(personas.find((p) => String(p.id) === personaId)?.name ?? "默认人设")}
                    </p>
                  )}
                  <button className="btn-primary" onClick={connect} disabled={connecting || (!stateCallId && !objId)}>
                    {connecting ? "接通中…" : error ? "重试接通" : isJoiningExisting ? "接通 / 进房" : "接通"}
                  </button>
                  {connecting && (
                    <p className="animate-pulse text-[11px] text-sky-300">
                      正在创建会话并接通…（约几秒，随后显示「初始化中」）
                    </p>
                  )}
                </div>
              ) : (
                <button className="btn-ghost" onClick={leave}>
                  挂断
                </button>
              )}
            </div>
            {roomConnected && isJoiningExisting && (
              <div className="flex flex-col items-end gap-1">
                <div className="flex gap-1.5">
                  <button className="btn-ghost px-2 py-0.5 text-[11px]" disabled={superviseBusy} onClick={() => supervisorAct("pause")}>
                    暂停 AI
                  </button>
                  <button className="btn-ghost px-2 py-0.5 text-[11px]" disabled={superviseBusy} onClick={() => supervisorAct("resume")}>
                    恢复 AI
                  </button>
                  <button className="btn-ghost px-2 py-0.5 text-[11px]" disabled={superviseBusy} onClick={() => supervisorAct("takeover")}>
                    接管
                  </button>
                  <button className="btn-ghost px-2 py-0.5 text-[11px]" disabled={superviseBusy} onClick={() => supervisorAct("transfer")}>
                    转人工
                  </button>
                </div>
                {superviseMsg && <span className="text-[10px] text-[var(--muted)]">{superviseMsg}</span>}
              </div>
            )}
          </div>
        </div>

        {error && <p className="rounded-lg bg-red-500/10 p-3 text-sm text-red-300">{error}</p>}
        {!error && !cp.ready && (
          <p className="rounded-lg bg-white/5 p-3 text-sm text-[var(--muted)]">
            本地服务启动中…（Control Plane / ASR / TTS），就绪后会自动加载对象与人设，请稍候。
          </p>
        )}
        {!stateCallId && objects.length === 0 && (
          <p className="mb-3 rounded-lg bg-white/5 p-3 text-sm text-[var(--muted)]">
            请先在「对象」页建档一个对象，再回到这里接通。
          </p>
        )}

        {/* WhatsApp 對接橫幅:客戶俾咗號碼/應承加 → 面板內提示,唔影響 AI 通話 */}
        {(waStatus === "captured" || waStatus === "offered") && (
          <div className="wa-flash mb-3 rounded-lg border border-[var(--accent)] bg-[var(--card)] p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="text-xs font-semibold text-[var(--accent)]">
                  📱 WhatsApp 待对接
                  <span className="ml-2 rounded bg-[var(--accent)]/15 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider">
                    {waStatus === "captured" ? "已拿到号码" : "客户已应承加"}
                  </span>
                </p>
                {waStatus === "captured" && waNum ? (
                  <p className="mt-1 font-mono text-lg tracking-wider">{waNum}</p>
                ) : (
                  <p className="mt-1 text-xs text-[var(--muted)]">客户应承咗加专员,等紧佢俾号码 / 由专员主动联系。</p>
                )}
              </div>
              <div className="flex shrink-0 gap-2">
                {waStatus === "captured" && waNum && (
                  <button
                    className="btn-ghost text-xs"
                    onClick={async () => {
                      try {
                        await navigator.clipboard.writeText(waNum);
                        setSuperviseMsg("号码已复制");
                      } catch {
                        /* clipboard 失敗靜默 */
                      }
                    }}
                  >
                    复制号码
                  </button>
                )}
                <button className="btn-primary text-xs" disabled={waHandling} onClick={markWaHandled}>
                  {waHandling ? "标记中…" : "标记已对接"}
                </button>
              </div>
            </div>
          </div>
        )}

        <div className="flex min-h-0 flex-1 flex-col">
          <AgentSessionProvider session={session} volume={1} muted={false}>
            {roomConnected ? <LiveAgentPanel room={session.room} /> : <IdleStage />}
          </AgentSessionProvider>
        </div>
      </section>

      {/* 右：Provider / 音频 / 结算 */}
      <section className="card flex min-h-0 flex-col gap-4 overflow-y-auto">
        <div className="rounded-lg bg-white/5 p-3">
          <span className="label">Provider 服务状态</span>
          <div className="mt-2 space-y-1 text-sm">
            {PROVIDER_FIELDS.map(([kind, label]) => (
              <p key={kind} className="flex justify-between">
                <span className="text-[var(--muted)]">{label}</span>
                <span className="inline-flex items-center gap-1.5 text-emerald-400">
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                  已连接
                </span>
              </p>
            ))}
          </div>
        </div>
        <AudioDevicesCard room={session.room} />
        <div className="rounded-lg bg-white/5 p-3 text-sm">
          <span className="label mb-1 block">结算</span>
          {settlement ? (
            <>
              <p className="flex justify-between">
                <span className="text-[var(--muted)]">状态</span>
                <span className="text-emerald-400">{str(settlement.status)}</span>
              </p>
              {str(settlement.summary) && (
                <p className="mt-2 border-t border-[var(--card-border)] pt-2 text-xs leading-relaxed text-[var(--muted)]">
                  {str(settlement.summary)}
                </p>
              )}
              {Array.isArray(settlement.new_topics) && settlement.new_topics.length > 0 && (
                <div className="mt-2 border-t border-[var(--card-border)] pt-2">
                  <span className="label mb-1 block">本轮新沉淀话题</span>
                  <ul className="space-y-1 text-xs text-[var(--muted)]">
                    {(settlement.new_topics as Record<string, unknown>[]).map((t, i) => (
                      <li key={i}>{str(t.topic)}</li>
                    ))}
                  </ul>
                </div>
              )}
              <p className="mt-1 break-all text-xs text-[var(--muted)]">通话文档：{str(settlement.transcript_doc_path)}</p>
              <p className="mt-1 break-all text-xs text-[var(--muted)]">结算文档：{str(settlement.settlement_doc_path)}</p>
            </>
          ) : (
            <p className="text-xs text-[var(--muted)]">挂断后自动沉淀：通话文档 / 对象主题 / 全局洞察 / 成本。</p>
          )}
        </div>
      </section>
    </div>
  );
}
