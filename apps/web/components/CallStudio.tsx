"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  StartAudio,
  VoiceAssistantControlBar,
  useAgent,
  useAgentExpression,
  useAudioPlayback,
  useSession,
  useSessionMessages,
  useTranscriptions,
  type UseSessionReturn,
} from "@livekit/components-react";
import { ConnectionState, TokenSource, Track, type Room } from "livekit-client";
import { api } from "@/lib/api";
import { describeConnectError, friendlyErrorText, useControlPlaneReady } from "@/lib/api-ready";
import { applyOutputDevice, listAudioDevicesOf, requestMicPermission, saveMicDevice, savedMicDevice, savedOutputDevice, switchWebOutputDevice, webCanSwitchOutput, isTauriShell, type AudioDeviceInfo } from "@/lib/audio";
import { AgentChatIndicator } from "@/components/agents-ui/agent-chat-indicator";
import { AgentChatTranscript } from "@/components/agents-ui/agent-chat-transcript";
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
      {/* 官方 AgentChatIndicator（motion 呼吸脉冲）替代手写 animate-pulse 点；
          状态色经 className 覆盖官方默认 bg-muted-foreground（cn 走 tailwind-merge 同组取末值） */}
      <AgentChatIndicator size="sm" className={item.color} />
      {item.label}
    </span>
  );
}

/**
 * 官方 Agents UI 会话面板：LiveKit Aura 可视化（情绪驱动颜色）+ 官方控制条。
 * 转写用官方 AgentChatTranscript（useSessionMessages 聚合语音转写+文字消息、自动滚底、
 * thinking 指示内置）；流式 partial 粒度的 useTranscriptions 保留给实时分析统计。
 */
function LiveAgentPanel({ room, session }: { room: Room | null; session: UseSessionReturn }) {
  const { state, microphoneTrack, failureReasons } = useAgent();
  const { mood } = useAgentExpression();
  const transcriptions = useTranscriptions();
  const { messages } = useSessionMessages(session);
  const agentState = state ?? "connecting";

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
      {/* 转写时间线：官方 AgentChatTranscript——自动滚底/滚到底按钮/thinking 指示组件内置，
          容器只管占位高度（至少 280px、占中栏剩余空间，绝不会被压没）。
          官方组件空列表时无占位内容，用绝对定位层保留「等待对话…」空态提示。 */}
      <div className="relative flex min-h-[280px] flex-1 flex-col overflow-hidden">
        <AgentChatTranscript agentState={agentState} messages={messages} className="px-1 py-2" />
        {messages.length === 0 && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-(--stage-muted)">
            等待对话…
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
        <div className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 border-t border-(--card-border) px-2 py-1.5 text-[10px] muted">
          <span>轮次 <b className="text-(--foreground)">{liveStats.turnCount}</b></span>
          <span>均每轮字数 <b className="text-(--foreground)">{liveStats.density}</b></span>
          <span>填充词 <b className="text-(--foreground)">{liveStats.fillers}</b></span>
          <span>犹豫词 <b className="text-(--foreground)">{liveStats.hedges}</b></span>
        </div>
      )}

      {/* 控制条（AgentSessionProvider 已内置音频渲染）；设备切换已移到右侧「音频设备」卡片 */}
      <div className="flex shrink-0 flex-col items-center gap-2 border-t border-(--card-border) py-2">
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
        <span className="text-[10px] muted">麦克风输入波形</span>
        <span className={`text-[10px] ${active ? "text-emerald-400" : "muted"}`}>
          {active ? `● 采集中 ${level > 3 ? `音量 ${level}` : "(静音)"}` : error ? "无法分析" : "未采集"}
        </span>
      </div>
      <canvas ref={canvasRef} width={260} height={40} className="mt-1 w-full rounded-sm bg-black/20" />
      {trackInfo && <p className="mt-0.5 truncate text-[9px] muted" title={trackInfo}>{trackInfo}</p>}
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
          <span className="muted">麦克风</span>
          <div className="flex min-w-0 items-center gap-1">
            <select
              className="max-w-[150px] rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--accent)"
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
              className="rounded-lg border border-(--card-border) px-2 py-1 opacity-70 hover:opacity-100"
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
          <span className="muted">扬声器</span>
          {outputCanSwitch ? (
            <select
              className="max-w-[150px] rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--accent)"
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
        {!room && <p className="muted">接通后可用</p>}
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
      <p className="text-sm text-(--foreground)">点击「接通」开始与 AI 助手对话</p>
      <p className="text-xs muted">浏览器将请求麦克风权限</p>
    </div>
  );
}

/** 历史/挂断通话的转写回放（2026-09-14 交互自洽）：useSessionMessages 只含活
 * 会话消息,ended 通话打开面板时中栏原本永远「等待对话…」,转写落库无入口——
 * 从 CP turns 拉全量,按角色着色回放。结算落库有写入窗口,前几轮短轮询补齐。 */
function HistoryTranscript({ callId }: { callId: string }) {
  const [turns, setTurns] = useState<Record<string, unknown>[]>([]);
  useEffect(() => {
    let stopped = false;
    let tries = 0;
    const load = async () => {
      try {
        const rows = await api.getTurns(callId);
        if (!stopped) setTurns(Array.isArray(rows) ? rows : []);
      } catch {
        /* CP 一时不可达等下轮 */
      }
    };
    void load();
    const t = setInterval(() => {
      tries += 1;
      if (tries > 6) {
        clearInterval(t);
        return;
      }
      void load();
    }, 2500);
    return () => {
      stopped = true;
      clearInterval(t);
    };
  }, [callId]);
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="flex-1 space-y-1.5 overflow-y-auto p-2">
        <p className="text-center text-[10px] font-bold uppercase tracking-[0.16em] text-(--stage-muted)">
          本通对话记录
        </p>
        {turns.length === 0 && <p className="text-center text-xs muted">暂无转写落库</p>}
        {turns.map((t, i) => {
          const role = String(t.role ?? "");
          const text = String(t.transcript ?? "").trim();
          if (!text) return null;
          return (
            <p
              key={String(t.id ?? i)}
              className={`rounded-lg px-3 py-1.5 text-sm leading-relaxed ${
                role === "user" ? "bg-(--accent)/10" : "bg-white/5"
              }`}
            >
              <span className="mr-2 text-[10px] font-medium muted">
                {role === "user" ? "客户" : "AI"}
              </span>
              {text}
            </p>
          );
        })}
      </div>
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

/**
 * 通话工作台外壳（2026-09-13 切换客户闭环）：
 * useSession 实例 end 之后不可复用——旧版挂断后切对象再「接通」挂在死 session
 * 上，只有刷新页面重新 mount 才活。外壳持 epoch：挂断/切换通话 → key 重挂 =
 * 全新 session + 全新转写 + 全新错误态，对象/人设选择经 localStorage「上次选择」
 * 保留——等同「重新进工作台」但零页面刷新。结算上提外壳：重挂后仍可读上一通。
 * 支持 /calls/new?object=&persona= 预选（对象页/通话列表「再拨」直达入口）。
 */
export function CallStudio({
  callId = "",
  onRequestNewCall,
}: {
  callId?: string;
  /** 嵌入模式（/calls?call=）下「用该对象发起新通话」由宿主页路由走
   * /calls/new?object=（退出内嵌工作台）；独立页(/calls/new)走 preset 重挂。 */
  onRequestNewCall?: (objectId: string) => void;
}) {
  const [epoch, setEpoch] = useState(0);
  const [settlement, setSettlement] = useState<Record<string, unknown> | null>(null);
  const [lastFinished, setLastFinished] = useState("");
  const [preset, setPreset] = useState({ object: "", persona: "" });
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    setPreset({ object: q.get("object") ?? "", persona: q.get("persona") ?? "" });
  }, []);
  return (
    <CallStudioInner
      key={`${epoch}:${callId}:${preset.object}:${preset.persona}`}
      callId={callId}
      initialObject={preset.object}
      initialPersona={preset.persona}
      settlement={settlement}
      setSettlement={setSettlement}
      lastFinishedCallId={lastFinished}
      onCycle={(id) => {
        if (id) setLastFinished(id);
        setEpoch((e) => e + 1);
      }}
      onReDialWithObject={(oid) => {
        if (onRequestNewCall) {
          onRequestNewCall(oid);
          return;
        }
        // 独立页：preset 重挂=已验证的 ?object= 同一条预选路径（原地状态手术
        // 与 hydrate/default-pick 时序打架，重挂是唯一干净的预选状态机）。
        setPreset({ object: oid, persona: "" });
        setEpoch((e) => e + 1);
      }}
    />
  );
}

function CallStudioInner({
  callId = "",
  initialObject = "",
  initialPersona = "",
  settlement,
  setSettlement,
  lastFinishedCallId = "",
  onCycle,
  onReDialWithObject,
}: {
  callId?: string;
  initialObject?: string;
  initialPersona?: string;
  settlement: Record<string, unknown> | null;
  setSettlement: (s: Record<string, unknown> | null) => void;
  lastFinishedCallId?: string;
  onCycle: (finishedCallId?: string) => void;
  onReDialWithObject: (objectId: string) => void;
}) {
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
  // 对象下拉可搜索(QA B3,2026-09-09):历史测试数据曾把下拉灌到 300+ 项。
  const [objFilter, setObjFilter] = useState("");
  const [personas, setPersonas] = useState<Record<string, unknown>[]>([]);
  const [objId, setObjId] = useState("");
  const [personaId, setPersonaId] = useState("");
  objIdRef.current = objId;
  personaIdRef.current = personaId;
  const [object, setObject] = useState<Record<string, unknown> | null>(null);
  const [persona, setPersona] = useState<Record<string, unknown> | null>(null);
  const [mode, setMode] = useState<"simulation" | "live">("simulation");
  const [objectTopics, setObjectTopics] = useState<Record<string, unknown>[]>([]);
  const [settings, setSettings] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);
  // 服务就绪自愈：桌面壳异步拉起整栈，首次加载失败后在 Control Plane 就绪时自动重拉。
  const cp = useControlPlaneReady();
  const loadAttemptRef = useRef(-1);
  const loadedModeRef = useRef("");

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
        // C2 幽灵重连闸(2026-09-13,call-6bd59b40):TokenSource 自带 exp 前自动
        // 续签,房间被删后的 livekit 全量重连会再来要 token——通话已 ended 时
        // 提前 throw,掐断「新 token→重连重建房→幽灵 job 重放开场白」链
        // (CP /api/token 侧同款 409 双保险)。
        const cur = (await api.getCall(id).catch(() => null)) as
          | (Record<string, unknown> & { status?: string })
          | null;
        if (cur && String(cur.status ?? "") === "ended") {
          throw new Error("call ended — refusing to renew token (ghost rejoin guard)");
        }
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
  // 「接通后 AI 初始化中」提示窗口(到点自动消失;期间面板状态灯同显)
  const [initHintUntil, setInitHintUntil] = useState(0);
  // ended 通话拦截：显示「用该对象发起新通话」入口(经外壳 preset 重挂,干净状态机)
  const [endedBlock, setEndedBlock] = useState(false);
  const [nowTick, setNowTick] = useState(0);
  useEffect(() => {
    if (!initHintUntil) return;
    const t = setInterval(() => setNowTick((v) => v + 1), 500);
    return () => clearInterval(t);
  }, [initHintUntil > 0]);

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
    // 工作台模式（callId 非空且有活跃会话）：档案由 hydrate 从服务端解析,列表
    // 默认选择唔跑——两者异步竞态会把历史通话档案覆写成「列表首个/上次」。
    // reDial（用该对象发起新通话）在 callId prop 为真的实例内回到新建模式,
    // stateCallId 清空 → 本 effect 重跑加载列表（callId prop 判会永久跳过）。
    if (callId && stateCallId) return;
    if (loadAttemptRef.current === cp.attempt && loadedModeRef.current === "new") return;
    loadAttemptRef.current = cp.attempt;
    loadedModeRef.current = "new";
    let cancelled = false;
    Promise.all([api.listObjects(ACCOUNT), api.listPersonas()])
      .then(([objs, pers]) => {
        if (cancelled) return;
        if (Array.isArray(objs)) setObjects(objs);
        if (Array.isArray(pers)) setPersonas(pers);
        // 默认选「上次用的人设/对象」(存在且在列表内);否则取第一个。
        // ?object=&persona= 预选优先(对象页/通话列表「再拨」直达入口);
        // 已有选择(reDial 预选)则保留,默认选择让位。
        const presetObj =
          initialObject && objs.some((o) => String(o.id) === initialObject) ? initialObject : "";
        const presetPers =
          initialPersona && pers.some((p) => String(p.id) === initialPersona) ? initialPersona : "";
        const lastObj = lsGet(lastKey("object"));
        const lastPers = lsGet(lastKey("persona"));
        if (objs?.length && !objIdRef.current) {
          const picked = presetObj
            ? presetObj
            : objs.find((o) => String(o.id) === lastObj)
              ? lastObj
              : String(objs[0].id);
          setObjId(picked);
        }
        if (pers?.length && !personaIdRef.current) {
          let picked = presetPers
            ? presetPers
            : pers.find((p) => String(p.id) === lastPers)
              ? lastPers
              : String(pers[0].id);
          // 对象跳转(「发起新通话」/「再拨」带 ?object=)时,人设语言优先匹配
          // 对象语言——否则粤语对象会配上次的普通话人设,接通即语言错配
          // (callLang 人设优先,2026-09-14 交互自洽修)。
          if (!presetPers && initialObject) {
            const objLang = str(
              (objs.find((o) => String(o.id) === initialObject) || {}).language
            ).trim();
            if (objLang) {
              const matched = pers.filter((p) => str(p.language).trim() === objLang);
              if (matched.length > 0) {
                const lastMatched = matched.find((p) => String(p.id) === lastPers);
                picked = String((lastMatched ?? matched[0]).id);
              }
            }
          }
          setPersonaId(picked);
        }
        setError(null);
      })
      .catch((e) => {
        if (!cancelled) setError(friendlyErrorText(String(e)));
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cp.attempt, ACCOUNT, stateCallId]);

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
        // ended 通话:点亮「用该对象发起新通话」并禁用接通按钮——别让用户点
        // 一次必然失败的「接通/进房」才看到提示(交互自洽,2026-09-14)。
        if (String(c.status ?? "") === "ended") setEndedBlock(true);
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
        setSettlement(null); // 新一通开始，清上一通结算展示
      } else {
        // Join 已结束嘅 call:LiveKit room 已清,簽咗 token 去 join 會 401
        // (前端會誤報「令牌校驗失敗」)。直接攔截,叫用戶開新通話。
        const cur = (await api.getCall(id).catch(() => null)) as
          | (Record<string, unknown> & { status?: string })
          | null;
        if (cur && String(cur.status ?? "") === "ended") {
          setConnecting(false);
          setEndedBlock(true); // 面板交代+发起新通话入口(按钮已同时禁用)
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
      // 连接前预缓冲 + 接通一步到位:麦克风采集放进 session.start 的 tracks
      // (与 token/连房并行,gum 即刻返回,连接完成后发布落地)。旧写法先在
      // 未连接的房间上 await setMicrophoneEnabled(preConnectBuffer)——发布要等
      // 连接、连接又等这行返回,互相等死到 livekit 内部 ~15s 超时才放行,
      // 即「首次接通 15.3s」根因(2026-09-06 浏览器探针实测:token 晚发 15.1s,
      // 三通真实通话同款 15.3-15.6s;旧 preConnectBuffer 语义不变,仍在
      // 连接完成前采集缓冲,agent 不吃头字)。
      await session.start({
        tracks: { microphone: { enabled: true, publishOptions: { preConnectBuffer: true } } },
      });
      setInitHintUntil(Date.now() + 8000);
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
    const finished = stateCallId;
    // 先断开官方会话，再挂断 + 结算（业务流保留）。
    try {
      await session.end();
    } catch {
      /* ignore */
    }
    if (stateCallId) {
      try {
        await api.hangup(stateCallId);
      } catch (e) {
        if (!String(e).includes("404")) console.warn("hangup failed", e);
      }
      // 结算重试(2026-09-09 QA B1):agent 侧 settle 喺 session close 后异步完成,
      // 首查 404 属「结算在途」;3 次×2s 俾佢跑完,消除结算卡假空态。
      for (let i = 0; i < 3; i++) {
        await new Promise((r) => setTimeout(r, i === 0 ? 800 : 2000));
        try {
          const s = await api.getSettlement(stateCallId);
          setSettlement(s);
          break;
        } catch {
          /* 404=在途,继续重试 */
        }
      }
    }
    setStateCallId("");
    callIdRef.current = "";
    // 切换客户闭环（2026-09-13）：session 实例 end 后不可复用，挂断即换 key
    // 重挂（外壳 epoch+1）——全新 session/转写/错误态，对象选择经 localStorage
    // 「上次选择」保留，下拉换客户直接接通，零页面刷新。带回落地通话 id,
    // 外壳保留「查看通话记录」入口(转写在会话页,工作台重挂后已清)。
    onCycle(finished);
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
            <input
              className="w-full rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--accent)"
              placeholder={`输入名称过滤对象（共 ${objects.length} 个，最多显示 50）`}
              value={objFilter}
              onChange={(e) => setObjFilter(e.target.value)}
            />
            <select className="select" value={objId} onChange={(e) => setObjId(e.target.value)}>
              {objects.length === 0 && <option value="">请先建档对象</option>}
              {(() => {
                const kw = objFilter.trim().toLowerCase();
                const matched = objects.filter((o) => !kw || str(o.display_name).toLowerCase().includes(kw));
                const list = matched.slice(0, 50);
                // 选中项必须永远在选项表里:只在全量 matched 判「在」的话,选中项
                // 排在 350+ 位时前 50 渲染不到它→DOM value 无匹配→浏览器回落
                // 第一项,显示与状态脱钩(2026-09-13 实测:reDial 预选显示成列表首项)。
                if (objId && !list.some((o) => String(o.id) === objId)) {
                  const sel = objects.find((o) => String(o.id) === objId);
                  if (sel) list.unshift(sel);
                }
                return list;
              })().map((o) => (
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
          <p className="text-xs muted">
            {str(object.role_template)} / {str(object.language)} {object.phone ? `· ${str(object.phone)}` : ""}
          </p>
        )}
        {object?.background && (
          <p className="rounded-lg bg-white/5 p-3 text-sm muted">{String(object.background)}</p>
        )}

        <div className="rounded-lg bg-white/5 p-3 text-sm">
          <span className="label mb-1 block">历史主题</span>
          {objectTopics.length === 0 ? (
            <p className="muted">暂无（挂断结算后自动沉淀）</p>
          ) : (
            <ul className="space-y-1.5">
              {objectTopics.slice(-5).map((t) => (
                <li key={String(t.id ?? t.topic ?? "")} className="muted">
                  <span className="text-(--foreground)">{str(t.topic)}</span>
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
              <p className="muted">
                {str(persona.company)} · {str(persona.tone)}
              </p>
            </>
          ) : (
            <p className="muted">默认人设未配置</p>
          )}
        </div>
        <div className="mt-auto rounded-lg border border-dashed border-(--card-border) p-3 text-xs muted">
          对象档案与知识库由当前账号注入，挂断后自动沉淀到该账号。
        </div>
      </section>

      {/* 中：官方 LiveKit 会话台（AgentSessionProvider + 官方可视化） */}
      <section className="card flex min-h-0 flex-col">
        <div className="mb-4 flex items-center justify-between">
          <div className="text-xs muted">
            {stateCallId ? `会话 ${stateCallId}` : "新建会话"}
          </div>
          <div className="flex flex-col items-end gap-2">
            <div className="flex gap-2">
              {!roomConnected ? (
                <div className="flex flex-col items-end gap-1">
                  {!stateCallId && objId && (
                    <p className="text-[10px] muted">
                      接通后：{str(objects.find((o) => String(o.id) === objId)?.display_name)}{" "}
                      · {str(personas.find((p) => String(p.id) === personaId)?.name ?? "默认人设")}
                    </p>
                  )}
                  <button
                    className="btn-primary"
                    onClick={connect}
                    disabled={connecting || endedBlock || (!stateCallId && !objId)}
                    title={endedBlock ? "该通话已结束，房间已关闭" : undefined}
                  >
                    {endedBlock
                      ? "通话已结束"
                      : connecting
                        ? "接通中…"
                        : error
                          ? "重试接通"
                          : isJoiningExisting
                            ? "接通 / 进房"
                            : "接通"}
                  </button>
                  {connecting && (
                    <p className="animate-pulse text-[11px] text-sky-300">
                      正在创建会话并接通…（约几秒，随后显示「初始化中」）
                    </p>
                  )}
                </div>
              ) : (
                <div className="flex items-center gap-2">
                  {initHintUntil > 0 && Date.now() < initHintUntil && (
                    <span className="animate-pulse text-[11px] text-sky-300" data-tick={nowTick}>
                      AI 初始化中…
                    </span>
                  )}
                  <button className="btn-ghost" onClick={leave}>
                    挂断
                  </button>
                </div>
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
                {superviseMsg && <span className="text-[10px] muted">{superviseMsg}</span>}
              </div>
            )}
          </div>
        </div>

        {error && (
          <div className="rounded-lg bg-red-500/10 p-3 text-sm text-red-300">
            <p>{error}</p>
          </div>
        )}
        {/* ended 通话面板(hydrate 即亮,不必先点一次接通吃报错):一句交代+直达发起新通话 */}
        {endedBlock && !roomConnected && (
          <div className="rounded-lg bg-white/5 p-3 text-sm">
            <p className="muted">该通话已结束（房间已关闭），无法重新接通。可直接用该对象发起新通话。</p>
            <button
              className="btn-ghost mt-2 text-xs"
              onClick={() => {
                const oid = objIdRef.current;
                setEndedBlock(false);
                if (oid) onReDialWithObject(oid);
              }}
            >
              用该对象发起新通话 →
            </button>
          </div>
        )}
        {!error && !cp.ready && (
          <p className="rounded-lg bg-white/5 p-3 text-sm muted">
            本地服务启动中…（Control Plane / ASR / TTS），就绪后会自动加载对象与人设，请稍候。
          </p>
        )}
        {!stateCallId && objects.length === 0 && (
          <p className="mb-3 rounded-lg bg-white/5 p-3 text-sm muted">
            请先在「对象」页建档一个对象，再回到这里接通。
          </p>
        )}

        {/* WhatsApp 對接橫幅:客戶俾咗號碼/應承加 → 面板內提示,唔影響 AI 通話 */}
        {(waStatus === "captured" || waStatus === "offered") && (
          <div className="wa-flash mb-3 rounded-lg border border-(--accent) bg-(--card) p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="text-xs font-semibold text-accent">
                  📱 WhatsApp 待对接
                  <span className="ml-2 rounded-sm bg-(--accent)/15 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider">
                    {waStatus === "captured" ? "已拿到号码" : "客户已应承加"}
                  </span>
                </p>
                {waStatus === "captured" && waNum ? (
                  <p className="mt-1 font-mono text-lg tracking-wider">{waNum}</p>
                ) : (
                  <p className="mt-1 text-xs muted">客户应承咗加专员,等紧佢俾号码 / 由专员主动联系。</p>
                )}
              </div>
              <div className="flex shrink-0 gap-2">
                {waStatus === "captured" && waNum && (
                  <button
                    className="btn-ghost text-xs"
                    onClick={async () => {
                      let ok = false;
                      try {
                        await navigator.clipboard.writeText(waNum);
                        ok = true;
                      } catch {
                        try {
                          const ta = document.createElement("textarea");
                          ta.value = waNum;
                          document.body.appendChild(ta);
                          ta.select();
                          ok = document.execCommand("copy");
                          document.body.removeChild(ta);
                        } catch {
                          ok = false;
                        }
                      }
                      setSuperviseMsg(ok ? "号码已复制" : "复制失败，请手动选择复制");
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
            {roomConnected ? (
              <LiveAgentPanel room={session.room} session={session} />
            ) : stateCallId ? (
              <HistoryTranscript callId={stateCallId} />
            ) : (
              <IdleStage />
            )}
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
                <span className="muted">{label}</span>
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
                <span className="muted">状态</span>
                <span className="text-emerald-400">{str(settlement.status)}</span>
              </p>
              {str(settlement.summary) && (
                <p className="mt-2 border-t border-(--card-border) pt-2 text-xs leading-relaxed muted">
                  {str(settlement.summary)}
                </p>
              )}
              {Array.isArray(settlement.new_topics) && settlement.new_topics.length > 0 && (
                <div className="mt-2 border-t border-(--card-border) pt-2">
                  <span className="label mb-1 block">本轮新沉淀话题</span>
                  <ul className="space-y-1 text-xs muted">
                    {(settlement.new_topics as Record<string, unknown>[]).map((t, i) => (
                      <li key={i}>{str(t.topic)}</li>
                    ))}
                  </ul>
                </div>
              )}
              <p className="mt-1 break-all text-xs muted">通话文档：{str(settlement.transcript_doc_path)}</p>
              {lastFinishedCallId && (
                <a
                  className="mt-2 inline-block text-xs text-accent"
                  href={`/calls?call=${encodeURIComponent(lastFinishedCallId)}`}
                >
                  查看通话记录（转写/逐轮）→
                </a>
              )}
              <p className="mt-1 break-all text-xs muted">结算文档：{str(settlement.settlement_doc_path)}</p>
            </>
          ) : (
            <p className="text-xs muted">挂断后自动沉淀：通话文档 / 对象主题 / 全局洞察 / 成本。</p>
          )}
        </div>
      </section>
    </div>
  );
}
