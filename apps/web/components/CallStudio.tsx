"use client";

import { useEffect, useMemo, useState } from "react";
import {
  LiveKitRoom,
  RoomAudioRenderer,
  VoiceAssistantControlBar,
  BarVisualizer,
  StartAudio,
  useVoiceAssistant,
  useTranscriptions,
} from "@livekit/components-react";
import { api } from "@/lib/api";

function AgentStateLabel({ state }: { state: string }) {
  const map: Record<string, { label: string; color: string }> = {
    connecting: { label: "连接中", color: "bg-neutral-400" },
    initializing: { label: "初始化", color: "bg-amber-400" },
    listening: { label: "聆听中", color: "bg-emerald-400" },
    thinking: { label: "思考中", color: "bg-sky-400" },
    speaking: { label: "说话中", color: "bg-fuchsia-400" },
    disconnected: { label: "已断开", color: "bg-neutral-600" },
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
 * Official LiveKit voice-assistant panel, rendered as a child of <LiveKitRoom>.
 * Uses the canonical @livekit/components-react pieces: BarVisualizer (waveform +
 * state transitions), RoomAudioRenderer (assistant audio), VoiceAssistantControlBar
 * (mic/track controls) and useTranscriptions (live user + agent transcriptions).
 */
function LiveAgentPanel() {
  const { state, audioTrack, agent } = useVoiceAssistant();
  const transcriptions = useTranscriptions();
  const agentIdentity = agent?.identity ?? "agent";
  const recent = useMemo(() => transcriptions.slice(-16).reverse(), [transcriptions]);

  return (
    <div className="flex h-full flex-1 flex-col">
      {/* Transcript timeline (official-style chat bubbles) */}
      <div className="flex flex-1 flex-col gap-3 overflow-y-auto px-1 py-2">
        {recent.length === 0 && (
          <div className="flex flex-1 items-center justify-center text-sm text-[var(--muted)]">
            等待对话…
          </div>
        )}
        {recent.map((t, i) => {
          const identity = t.participantInfo?.identity ?? "";
          const text = String(t.text ?? "");
          const isAgent = identity === agentIdentity;
          return (
            <div key={`${identity}-${i}`} className={`flex ${isAgent ? "justify-start" : "justify-end"}`}>
              <div
                className={`max-w-[80%] rounded-2xl px-3 py-2 text-sm ${
                  isAgent ? "bg-[var(--accent)] text-[var(--accent-ink)]" : "bg-white/10 text-[var(--foreground)]"
                }`}
              >
                {text}
              </div>
            </div>
          );
        })}
      </div>

      {/* Central visualizer + state */}
      <div className="flex flex-col items-center gap-3 py-4">
        <AgentStateLabel state={state} />
        <div className="h-24 w-64">
          <BarVisualizer state={state} track={audioTrack} barCount={32} />
        </div>
      </div>

      {/* Audio renderer + autoplay unlock + control bar */}
      <div className="flex items-center justify-center gap-3 border-t border-[var(--card-border)] py-3">
        <RoomAudioRenderer />
        <StartAudio label="点击开启声音" />
        <VoiceAssistantControlBar />
      </div>
    </div>
  );
}

const ACCOUNT = "acc-001";

const PROVIDER_NOTE: [string, string][] = [
  ["ASR", "sherpa（本地）"],
  ["LLM", "DeepSeek"],
  ["TTS", "火山流式"],
  ["VAD", "Silero"],
];

function str(v: unknown, fallback = "-") {
  return v === undefined || v === null || v === "" ? fallback : String(v);
}

export function CallStudio({ callId = "" }: { callId?: string }) {
  const [stateCallId, setStateCallId] = useState(callId);
  const [objects, setObjects] = useState<Record<string, unknown>[]>([]);
  const [personas, setPersonas] = useState<Record<string, unknown>[]>([]);
  const [objId, setObjId] = useState("");
  const [personaId, setPersonaId] = useState("");
  const [object, setObject] = useState<Record<string, unknown> | null>(null);
  const [persona, setPersona] = useState<Record<string, unknown> | null>(null);
  const [mode, setMode] = useState<"simulation" | "live">("simulation");
  const [settlement, setSettlement] = useState<Record<string, unknown> | null>(null);
  const [creds, setCreds] = useState<{ url?: string; token?: string; roomName?: string }>({});
  const [error, setError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);

  // Load selectable objects + personas and default to the first.
  useEffect(() => {
    Promise.all([api.listObjects(ACCOUNT), api.listPersonas()])
      .then(([objs, pers]) => {
        if (Array.isArray(objs)) setObjects(objs);
        if (Array.isArray(pers)) setPersonas(pers);
        if (objs?.length) setObjId(String(objs[0].id));
        if (pers?.length) setPersonaId(String(pers[0].id));
      })
      .catch((e) => setError(String(e)));
  }, []);

  // For an existing call (e.g. /calls/[id]), hydrate everything from the server.
  useEffect(() => {
    if (!callId) return;
    setStateCallId(callId);
    api
      .getCall(callId)
      .then((c) => {
        if (c.object_id) setObjId(String(c.object_id));
        if (c.persona_id) setPersonaId(String(c.persona_id));
        if (c.mode) setMode(c.mode as "simulation" | "live");
      })
      .catch((e) => setError(String(e)));
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
  // For a brand-new call we wait until hangup, then settle + refresh below.
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
    try {
      let id = stateCallId;
      if (!id) {
        const created = await api.createCall({
          account_id: ACCOUNT,
          object_id: objId,
          persona_id: personaId,
          mode,
          direction: "webrtc",
          language: object?.language ?? "zh",
        });
        id = String(created.id);
        setStateCallId(id);
      }
      const res = await api.token({ account_id: ACCOUNT, call_id: id });
      setCreds({ url: res.url, token: res.token, roomName: res.roomName });
    } catch (e) {
      console.error("connect failed", e);
      setError("接通失败：请检查 Control Plane 是否运行，且已选择对象与人设。");
    } finally {
      setConnecting(false);
    }
  }

  async function leave() {
    // Hang up, then auto-settle (idempotent) and refresh the settlement panel.
    if (stateCallId) {
      try {
        await api.hangup(stateCallId);
        await api.settle(stateCallId);
        const s = await api.getSettlement(stateCallId);
        setSettlement(s);
      } catch (e) {
        console.warn("settle failed", e);
      }
    }
    setCreds({});
  }

  const roomConnected = !!creds.token;
  const metrics = (settlement?.metrics ?? {}) as Record<string, unknown>;

  return (
    <div className="grid grid-cols-[280px_1fr_300px] gap-6">
      {/* 左：对象档案 / 人设 */}
      <section className="card flex flex-col gap-4">
        <div className="flex items-center justify-between">
          <span className="label">对象档案</span>
          {!stateCallId && (
            <select
              className="rounded-lg border border-[var(--card-border)] bg-transparent px-2 py-1 text-xs outline-none"
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
            <select
              className="w-full rounded-xl border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none"
              value={objId}
              onChange={(e) => setObjId(e.target.value)}
            >
              {objects.length === 0 && <option value="">请先建档对象</option>}
              {objects.map((o) => (
                <option key={String(o.id)} value={String(o.id)}>
                  {str(o.display_name)}（{str(o.role_template)}）
                </option>
              ))}
            </select>
            <select
              className="w-full rounded-xl border border-[var(--card-border)] bg-transparent px-3 py-2 text-sm outline-none"
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
          <p className="rounded-xl bg-white/5 p-3 text-sm text-[var(--muted)]">{String(object.background)}</p>
        )}

        <div className="rounded-xl bg-white/5 p-3 text-sm">
          <span className="label mb-1 block">历史主题</span>
          <p className="text-[var(--muted)]">
            {Array.isArray(settlement?.new_topics) && settlement.new_topics.length ? "已沉淀（见右侧结算）" : "暂无（挂断后自动沉淀）"}
          </p>
        </div>
        <div className="rounded-xl bg-white/5 p-3 text-sm">
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
        <div className="mt-auto rounded-xl border border-dashed border-[var(--card-border)] p-3 text-xs text-[var(--muted)]">
          对象档案与知识库由当前账号注入，挂断后自动沉淀到该账号。
        </div>
      </section>

      {/* 中：官方 LiveKit 通话台 */}
      <section className="card flex min-h-[520px] flex-col">
        <div className="mb-4 flex items-center justify-between">
          <div className="text-xs text-[var(--muted)]">
            {creds.roomName ?? (stateCallId ? `会话 ${stateCallId}` : "新建会话")}
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
        {!stateCallId && objects.length === 0 && (
          <p className="mb-3 rounded-lg bg-white/5 p-3 text-sm text-[var(--muted)]">
            请先在「对象」页建档一个对象，再回到这里接通。
          </p>
        )}

        {roomConnected ? (
          <LiveKitRoom serverUrl={creds.url} token={creds.token} audio connect>
            <LiveAgentPanel />
          </LiveKitRoom>
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center text-center">
            <div className="flex h-24 w-24 items-center justify-center rounded-full border border-[var(--card-border)] text-3xl">📞</div>
            <p className="mt-4 text-sm text-[var(--muted)]">点击「接通」开始与 AI 助手对话</p>
            <p className="mt-1 text-xs text-[var(--muted)]">浏览器将请求麦克风权限</p>
          </div>
        )}
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
              <div key={k} className="rounded-xl bg-white/5 p-3">
                <p className="text-xs text-[var(--muted)]">{k}</p>
                <p className="mt-1 text-lg font-semibold">{v}</p>
              </div>
            ))}
          </div>
        </div>
        <div className="rounded-xl bg-white/5 p-3">
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
        <div className="rounded-xl bg-white/5 p-3 text-sm">
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
