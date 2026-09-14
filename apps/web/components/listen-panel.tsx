"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { LiveKitRoom, RoomAudioRenderer, useTranscriptions } from "@livekit/components-react";
import { api } from "@/lib/api";
import { friendlyErrorText } from "@/lib/api-ready";

type TokenInfo = { serverUrl: string; participantToken: string };

/**
 * 房间内实时字幕：token 只有 canSubscribe，天然「只看不听」；
 * 用官方 useTranscriptions 收 lk.transcription 流（agent 侧全量广播）。
 */
function Captions() {
  const transcriptions = useTranscriptions();
  const tail = transcriptions.slice(-6);
  return (
    <div className="max-h-44 space-y-1 overflow-auto rounded-lg bg-black/20 p-2 text-xs">
      {tail.length === 0 && <p className="muted">等待实时字幕…（旁听中不发声、不打断通话）</p>}
      {tail.map((t, i) => (
        <p key={`${String(t.participantInfo?.identity ?? "p")}-${i}`} className="leading-snug">
          <span className="muted">{String(t.participantInfo?.identity ?? "").slice(0, 16) || "—"}：</span>
          {String(t.text ?? "")}
        </p>
      ))}
    </div>
  );
}

/**
 * 主管静默旁听面板：进房只订阅（CP 签发的 listen token can_publish 全关），
 * 播放双方音轨 + 实时字幕；被听方无任何提示（产品拍板），但 start/stop 均留审计。
 */
export default function ListenPanel({
  callId,
  label,
  onClose,
}: {
  callId: string;
  label?: string;
  onClose: () => void;
}) {
  const [token, setToken] = useState<TokenInfo | null>(null);
  const [err, setErr] = useState("");
  const [status, setStatus] = useState<"connecting" | "live" | "ended">("connecting");
  const startedRef = useRef(0);
  const finalizedRef = useRef(false);
  // 同一 callId 的签发请求只发一次（dev StrictMode 会双跑 effect：双签发=双审计
  // 行 + 同 identity 二次进房互相踢）。
  const inflightRef = useRef<{ id: string; p: Promise<TokenInfo> } | null>(null);

  // 结束回执（含时长）尽力而为：失败不影响关闭面板本身。
  const finalize = useCallback(async () => {
    if (finalizedRef.current) return;
    finalizedRef.current = true;
    const seconds = startedRef.current ? Math.round((Date.now() - startedRef.current) / 1000) : 0;
    try {
      await api.supervisorListenStop(callId, seconds);
    } catch {
      /* 审计回执尽力而为 */
    }
  }, [callId]);

  useEffect(() => {
    let cancelled = false;
    if (inflightRef.current?.id !== callId) {
      inflightRef.current = {
        id: callId,
        p: api.supervisorListen(callId).then((res) => ({
          serverUrl: String(res.serverUrl),
          participantToken: String(res.participantToken),
        })),
      };
    }
    inflightRef.current.p
      .then((t) => {
        if (!cancelled) setToken(t);
      })
      .catch((e) => {
        if (!cancelled) setErr(friendlyErrorText(String(e)));
      });
    return () => {
      cancelled = true;
    };
  }, [callId]);

  // 真离开页面（关标签/导航）才补 stop 审计——不能挂在 effect cleanup 上：
  // dev StrictMode 的模拟卸载会提前写 stop。
  useEffect(() => {
    const onHide = () => {
      void finalize();
    };
    window.addEventListener("pagehide", onHide);
    return () => window.removeEventListener("pagehide", onHide);
  }, [finalize]);

  const close = () => {
    void finalize();
    onClose();
  };

  return (
    <section className="card space-y-3 border-(--accent)">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <span className="label">静默旁听</span>
          <p className="mt-1 truncate text-xs muted">
            {label || callId} · 只订阅不发声，通话双方无提示；本次旁听已计入审计。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className={`inline-flex items-center gap-1.5 text-xs ${status === "live" ? "text-emerald-400" : "muted"}`}>
            <span
              className={`h-2 w-2 rounded-full ${status === "live" ? "animate-pulse bg-emerald-400" : "bg-neutral-500"}`}
            />
            {status === "live" ? "旁听中" : status === "connecting" ? "连接中…" : "已结束"}
          </span>
          <button className="btn-ghost text-xs" onClick={close}>
            结束旁听
          </button>
        </div>
      </div>

      {err && <p className="rounded-lg bg-red-500/10 p-2 text-xs text-red-300">{err}</p>}
      {!token && !err && <p className="text-xs muted">正在获取旁听凭证…</p>}

      {token && (
        <LiveKitRoom
          serverUrl={token.serverUrl}
          token={token.participantToken}
          connect
          audio={false}
          video={false}
          onConnected={() => {
            startedRef.current = Date.now();
            setStatus("live");
          }}
          onDisconnected={() => setStatus("ended")}
          onError={(e) => setErr(friendlyErrorText(String(e)))}
          className="space-y-2"
        >
          <RoomAudioRenderer />
          <Captions />
          <p className="text-[11px] muted">
            听不到声音时点一下页面任意处（浏览器自动播放限制）；旁听不影响通话，随时可「结束旁听」。
          </p>
        </LiveKitRoom>
      )}
    </section>
  );
}
