"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  MediaDeviceMenu,
  StartAudio,
  VoiceAssistantControlBar,
  useAgent,
  useAgentExpression,
  useAudioPlayback,
  useSession,
  useTranscriptions,
} from "@livekit/components-react";
import { RoomEvent, TokenSource, type Room } from "livekit-client";
import { api } from "@/lib/api";
import { describeConnectError, friendlyErrorText, useControlPlaneReady } from "@/lib/api-ready";
import { applyOutputDevice, listAudioDevicesOf, savedMicDevice, savedOutputDevice, webCanSwitchOutput, isTauriShell, type AudioDeviceInfo } from "@/lib/audio";
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
  const { state, microphoneTrack, identity } = useAgent();
  const { mood } = useAgentExpression();
  const transcriptions = useTranscriptions();
  const agentIdentity = identity ?? "agent";
  const recent = useMemo(() => transcriptions.slice(-16).reverse(), [transcriptions]);
  const agentState = state ?? "connecting";
  const scrollRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    scrollRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [recent]);
  const speaking = agentState === "speaking";
  const outputCanSwitch = isTauriShell() || webCanSwitchOutput();
  const [outputDevices, setOutputDevices] = useState<AudioDeviceInfo[]>([]);
  useEffect(() => {
    if (outputCanSwitch) listAudioDevicesOf("output").then(setOutputDevices).catch(() => {});
  }, [outputCanSwitch]);

  return (
    <div className="flex h-full flex-1 flex-col">
      {/* 转写时间线（官方风格：mono、按说话者着色） */}
      <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-1 py-2">
        {recent.length === 0 && (
          <div className="flex flex-1 items-center justify-center font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-[var(--stage-muted)]">
            等待对话…
          </div>
        )}
        {speaking && recent.length > 0 && (
          <div className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--stage-value)]">
            <span className="h-1.5 w-1.5 rounded-full bg-[var(--stage-value)] animate-pulse" />
            AI 说话中
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
      </div>
      <div ref={scrollRef} />

      {/* 中央：官方点阵可视化（mood 驱动色） */}
      <div className="flex flex-col items-center gap-3 py-4">
        <AgentStateLabel state={agentState} />
      <VoiceAgentInterface
          size="md"
          state={agentState}
          mood={mood}
          audioTrack={microphoneTrack}
          showMoodLabel
        />
      </div>

      {/* 控制条（AgentSessionProvider 已内置音频渲染）+ 设备切换 */}
      <div className="flex flex-col items-center gap-2 border-t border-[var(--card-border)] py-3">
        <div className="flex items-center justify-center gap-3">
          <StartAudio label="点击开启声音" />
          <VoiceAssistantControlBar />
        </div>
        {room && (
          <div className="flex items-center justify-center gap-3 text-xs text-[var(--muted)]">
            <span className="inline-flex items-center gap-1.5">
              <span className="opacity-70">麦克风</span>
              <MediaDeviceMenu
                kind="audioinput"
                requestPermissions
                initialSelection={savedMicDevice() || undefined}
                onActiveDeviceChange={(_kind, deviceId) => {
                  if (deviceId) {
                    try {
                      localStorage.setItem("bok.audio.mic", deviceId);
                    } catch {
                      /* ignore */
                    }
                    void room.switchActiveDevice("audioinput", deviceId).catch((e) => console.warn("mic switch failed", e));
                  }
                }}
              />
            </span>
            {outputCanSwitch ? (
              <span className="inline-flex items-center gap-1.5">
                <span className="opacity-70">扬声器</span>
                <select
                  className="rounded-lg border border-[var(--card-border)] bg-transparent px-2 py-1 text-xs outline-none focus:border-[var(--accent)]"
                  value={savedOutputDevice()}
                  onChange={(e) => {
                    const id = e.target.value;
                    try {
                      localStorage.setItem("bok.audio.out", id);
                    } catch {
                      /* ignore */
                    }
                    void applyOutputDevice(id);
                  }}
                >
                  <option value="" disabled>
                    跟随系统
                  </option>
                  {outputDevices.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name}
                    </option>
                  ))}
                </select>
              </span>
            ) : (
              <span className="opacity-70">扬声器跟随系统默认</span>
            )}
          </div>
        )}
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

const PROVIDER_NOTE: [string, string][] = [
  ["ASR", "Qwen3-ASR"],
  ["LLM", "本地 LLM"],
  ["TTS", "Qwen3-TTS"],
  ["VAD", "Silero"],
];

function str(v: unknown, fallback = "-") {
  return v === undefined || v === null || v === "" ? fallback : String(v);
}

export function CallStudio({ callId = "" }: { callId?: string }) {
  const { accountId: ACCOUNT } = useAccount();
  const [stateCallId, setStateCallId] = useState(callId);
  const callIdRef = useRef(callId);
  const [objects, setObjects] = useState<Record<string, unknown>[]>([]);
  const [personas, setPersonas] = useState<Record<string, unknown>[]>([]);
  const [objId, setObjId] = useState("");
  const [personaId, setPersonaId] = useState("");
  const [object, setObject] = useState<Record<string, unknown> | null>(null);
  const [persona, setPersona] = useState<Record<string, unknown> | null>(null);
  const [mode, setMode] = useState<"simulation" | "live">("simulation");
  const [settlement, setSettlement] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);
  // 服务就绪自愈：桌面壳异步拉起整栈，首次加载失败后在 Control Plane 就绪时自动重拉。
  const cp = useControlPlaneReady();
  const loadAttemptRef = useRef(-1);

  // 官方会话：TokenSource.custom 直连 control-plane，只做键名映射（serverUrl/participantToken）。
  const tokenSource = useMemo(
    () =>
      TokenSource.custom(async () => {
        const id = callIdRef.current;
        if (!id) throw new Error("no call id");
        const res = await api.token({ account_id: ACCOUNT, call_id: id });
        return { serverUrl: res.url, participantToken: res.token };
      }),
    [],
  );
  const session = useSession(tokenSource);
  const { canPlayAudio, startAudio } = useAudioPlayback(session.room);

  const roomConnected = Boolean(stateCallId) && !connecting;

  // Load selectable objects + personas and default to the first. Runs on mount and
  // again on each Control Plane offline→ready transition (desktop cold start), so
  // the "接通" button never stays dead behind a one-shot network error.
  useEffect(() => {
    if (loadAttemptRef.current === cp.attempt) return;
    loadAttemptRef.current = cp.attempt;
    let cancelled = false;
    Promise.all([api.listObjects(ACCOUNT), api.listPersonas()])
      .then(([objs, pers]) => {
        if (cancelled) return;
        if (Array.isArray(objs)) setObjects(objs);
        if (Array.isArray(pers)) setPersonas(pers);
        if (objs?.length) setObjId(String(objs[0].id));
        if (pers?.length) setPersonaId(String(pers[0].id));
        setError(null);
      })
      .catch((e) => {
        if (!cancelled) setError(friendlyErrorText(String(e)));
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cp.attempt, ACCOUNT]);

  // For an existing call (e.g. /calls/[id]), hydrate everything from the server.
  useEffect(() => {
    if (!callId) return;
    setStateCallId(callId);
    callIdRef.current = callId;
    api
      .getCall(callId)
      .then((c) => {
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
  }, [objId]);
  useEffect(() => {
    if (!personaId) return;
    api.getPersona(personaId).then(setPersona).catch(() => {});
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
      }
      setConnecting(false);
      phase = "join-session";
      // 应用用户选择的音频设备：麦克风先设默认采集设备（session.start 开麦时会采用），
      // 扬声器在桌面壳切系统默认输出。
      const micDeviceId = savedMicDevice();
      const outputDeviceId = savedOutputDevice();
      // 非 exact：设备不存在/已插拔时回退默认，避免采集失败（exact 会 reject）。
      if (micDeviceId) await session.room.switchActiveDevice("audioinput", micDeviceId, false).catch(() => {});
      if (outputDeviceId) await applyOutputDevice(outputDeviceId).catch(() => {});
      await session.start({ tracks: { microphone: { enabled: true } } });
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
        console.warn("settle failed", e);
      }
    }
    setStateCallId("");
    callIdRef.current = "";
  }

  const metrics = (settlement?.metrics ?? {}) as Record<string, unknown>;

  return (
    <div className="grid grid-cols-[280px_1fr_300px] gap-6">
      {/* 左：对象档案 / 人设 */}
      <section className="card flex flex-col gap-4">
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
          <p className="text-[var(--muted)]">
            {Array.isArray(settlement?.new_topics) && settlement.new_topics.length ? "已沉淀（见右侧结算）" : "暂无（挂断后自动沉淀）"}
          </p>
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
      <section className="card flex min-h-[520px] flex-col">
        <div className="mb-4 flex items-center justify-between">
          <div className="text-xs text-[var(--muted)]">
            {stateCallId ? `会话 ${stateCallId}` : "新建会话"}
          </div>
          <div className="flex gap-2">
            {!roomConnected ? (
              <button className="btn-primary" onClick={connect} disabled={connecting || (!stateCallId && !objId)}>
                {connecting ? "接通中…" : error ? "重试接通" : "接通"}
              </button>
            ) : (
              <button className="btn-ghost" onClick={leave}>
                挂断
              </button>
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

        <AgentSessionProvider session={session} volume={1} muted={false}>
          {roomConnected ? <LiveAgentPanel room={session.room} /> : <IdleStage />}
        </AgentSessionProvider>
      </section>

      {/* 右：实时分析 / Provider / 结算 */}
      <section className="card flex flex-col gap-4">
        <div>
          <span className="label">实时分析</span>
          <div className="mt-2 grid grid-cols-2 gap-2">
            {[
              ["表达密度", str(metrics.speech_density, "—")],
              ["填充词", str(metrics.filler_ratio, "—")],
              ["犹豫词", str(metrics.hesitation_ratio, "—")],
              ["平均轮次", metrics.avg_turn_seconds ? `${metrics.avg_turn_seconds}s` : "—"],
            ].map(([k, v]) => (
              <div key={k} className="rounded-lg bg-white/5 p-3">
                <p className="text-xs text-[var(--muted)]">{k}</p>
                <p className="mt-1 text-lg font-semibold">{v}</p>
              </div>
            ))}
          </div>
        </div>
        <div className="rounded-lg bg-white/5 p-3">
          <span className="label">Provider（会话清单锁定）</span>
          <div className="mt-2 space-y-1 text-sm">
            {PROVIDER_NOTE.map(([k, v]) => (
              <p key={k} className="flex justify-between">
                <span className="text-[var(--muted)]">{k}</span>
                <span>{v}</span>
              </p>
            ))}
          </div>
        </div>
        <div className="rounded-lg bg-white/5 p-3 text-sm">
          <span className="label mb-1 block">结算</span>
          {settlement ? (
            <>
              <p className="flex justify-between">
                <span className="text-[var(--muted)]">状态</span>
                <span className="text-emerald-400">{str(settlement.status)}</span>
              </p>
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
