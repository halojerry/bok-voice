"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Check } from "lucide-react";
import { api } from "@/lib/api";
import { friendlyErrorText } from "@/lib/api-ready";
import { useAccount } from "@/components/account-context";
import ListenPanel from "@/components/listen-panel";

type CallRow = Record<string, unknown> & { id?: string; call_id?: string; status?: string };
type TurnRow = Record<string, unknown>;

const PENDING_WA = ["offered", "captured"];
const LANG_LABEL: Record<string, string> = { zh: "中文", cantonese: "粤语", en: "英语" };
/** 实时字段（话术步/最近一句）每条通话要拉一次 turns，设上限防 N 路打爆 CP。 */
const MAX_ENRICH = 8;

const idOf = (c: CallRow) => String(c.id ?? c.call_id ?? "");

function langLabel(v: unknown): string {
  const s = String(v ?? "");
  return LANG_LABEL[s] ?? (s || "—");
}

/** CP 的 created_at 是 naive UTC（无时区后缀）：补 Z 再解析，否则被当本地时间差 8 小时。 */
function parseTs(v: unknown): number {
  const s = String(v ?? "").trim();
  if (!s) return 0;
  const hasTz = /([zZ]|[+-]\d{2}:?\d{2})$/.test(s);
  const t = Date.parse(hasTz ? s : `${s}Z`);
  return Number.isNaN(t) ? 0 : t;
}

function elapsedLabel(createdAt: unknown): string {
  const t = parseTs(createdAt);
  if (!t) return "";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  return s % 60 ? `${m} 分 ${s % 60} 秒` : `${m} 分钟`;
}

/** 最近一句客户原话（turns 账本 speaker=customer；老数据无 speaker 时按 role=user 兜底）。 */
function lastCustomerLine(turns?: TurnRow[]): string {
  if (!turns?.length) return "";
  for (let i = turns.length - 1; i >= 0; i--) {
    const t = turns[i];
    const speaker = String(t.speaker ?? "");
    const role = String(t.role ?? "");
    if (speaker === "customer" || (!speaker && role === "user")) {
      const text = String(t.transcript ?? "").trim();
      if (text) return text;
    }
  }
  return "";
}

/** 当前话术步（agent 上报已 1-based；0=该通无话术）。 */
function currentStep(turns?: TurnRow[]): number {
  if (!turns?.length) return 0;
  for (let i = turns.length - 1; i >= 0; i--) {
    const step = Number(turns[i].template_step ?? 0);
    if (step > 0) return step;
  }
  return 0;
}

export default function SupervisorPage() {
  const { accountId } = useAccount();
  const [rows, setRows] = useState<CallRow[]>([]);
  const [turns, setTurns] = useState<Record<string, TurnRow[]>>({});
  const [err, setErr] = useState<string | null>(null);
  const [objName, setObjName] = useState<Record<string, string>>({});
  const [copied, setCopied] = useState<string | null>(null);
  const [listenId, setListenId] = useState<string | null>(null);
  const [busy, setBusy] = useState("");

  const refresh = useCallback(async () => {
    try {
      const data = await api.activeCalls();
      setRows((Array.isArray(data) ? data : []) as CallRow[]);
      setErr(null);
    } catch (e) {
      setErr(friendlyErrorText(String(e)));
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, [refresh]);

  useEffect(() => {
    (async () => {
      try {
        const objs = await api.listObjects(accountId);
        const m: Record<string, string> = {};
        (Array.isArray(objs) ? objs : []).forEach((o) => {
          const rec = o as Record<string, unknown>;
          m[String(rec.id ?? "")] = String(rec.display_name ?? "");
        });
        setObjName(m);
      } catch {
        /* objects 拉唔到唔影響 WhatsApp 通知 */
      }
    })();
  }, [accountId]);

  // 实时字段富化：为前 N 路通话拉 turns（话术步/最近一句客户话），4s 一轮。
  const idsKey = useMemo(() => rows.slice(0, MAX_ENRICH).map(idOf).filter(Boolean).join(","), [rows]);
  useEffect(() => {
    if (!idsKey) return;
    const ids = idsKey.split(",");
    let cancelled = false;
    const load = async () => {
      const pairs = await Promise.all(
        ids.map(async (id) => {
          try {
            return [id, (await api.getTurns(id)) as TurnRow[]] as const;
          } catch {
            return null;
          }
        }),
      );
      if (cancelled) return;
      setTurns((prev) => {
        const next = { ...prev };
        for (const p of pairs) if (p) next[p[0]] = p[1];
        return next;
      });
    };
    void load();
    const t = setInterval(load, 4000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [idsKey]);

  // 深链：/supervisor?listen=<callId>（静态导出用 query，不开动态路由）。
  useEffect(() => {
    const m = window.location.search.match(/[?&]listen=([^&]+)/);
    if (m) setListenId(decodeURIComponent(m[1]));
  }, []);
  const openListen = (id: string) => {
    setListenId(id);
    try {
      window.history.replaceState(null, "", `/supervisor/?listen=${encodeURIComponent(id)}`);
    } catch {
      /* 历史 API 失败不影响面板 */
    }
  };
  const closeListen = () => {
    setListenId(null);
    try {
      window.history.replaceState(null, "", "/supervisor/");
    } catch {
      /* 同上 */
    }
  };

  const labelOf = (c: CallRow) =>
    String(objName[String(c.object_id ?? "")] ?? c.object_name ?? c.call_id ?? c.id ?? "-");
  const waStatus = (c: CallRow) => String(c.whatsapp_status ?? "");
  const waNum = (c: CallRow) => String(c.customer_whatsapp ?? "");

  const pending = rows.filter((c) => PENDING_WA.includes(waStatus(c)));
  const activeCount = rows.filter((c) => String(c.status ?? "active") === "active").length;
  const pausedCount = rows.length - activeCount;

  async function copyNum(num: string) {
    try {
      await navigator.clipboard.writeText(num);
      setCopied(num);
      setTimeout(() => setCopied(null), 1200);
    } catch {
      /* clipboard 失敗靜默 */
    }
  }

  async function handleDone(c: CallRow) {
    try {
      await api.markWhatsappHandled(idOf(c));
      await refresh();
    } catch (e) {
      setErr(friendlyErrorText(String(e)));
    }
  }

  async function act(id: string, key: string, fn: (id: string) => Promise<unknown>) {
    setErr(null);
    setBusy(`${id}:${key}`);
    try {
      await fn(id);
      await refresh();
    } catch (e) {
      setErr(friendlyErrorText(String(e)));
    } finally {
      setBusy("");
    }
  }

  const confirmAct = (c: CallRow, key: string, fn: (id: string) => Promise<unknown>, question: string) => {
    if (!window.confirm(question)) return;
    void act(idOf(c), key, fn);
  };

  return (
    <div>
      <div className="mb-6 flex items-start justify-between">
        <div>
          <h1 className="page-title">主管台</h1>
          <p className="page-sub">
            实时总控：静默旁听（无提示、留审计）· 暂停 / 接管 / 挂断；详情与转写在工作台看。
          </p>
        </div>
        <button className="btn-ghost" onClick={() => refresh()}>刷新</button>
      </div>

      <div className="mb-6 flex flex-wrap gap-3 text-sm">
        <span className="card px-4 py-2">
          进行中 <b className="text-(--live)">{activeCount}</b>
        </span>
        <span className="card px-4 py-2">
          已暂停 <b className={pausedCount ? "text-amber-700" : "muted"}>{pausedCount}</b>
        </span>
        <span className="card px-4 py-2">
          WhatsApp 待对接 <b className={pending.length ? "text-(--live)" : "muted"}>{pending.length}</b>
        </span>
      </div>

      {err && <p className="mb-4 rounded-lg bg-red-500/10 p-3 text-sm text-red-600">{err}</p>}

      {listenId && (
        <div className="mb-6">
          <ListenPanel
            callId={listenId}
            label={labelOf(rows.find((c) => idOf(c) === listenId) ?? { id: listenId })}
            onClose={closeListen}
          />
        </div>
      )}

      {/* WhatsApp 對接橫幅區:有待對接 call 先顯示,撳「已對接」就收起 */}
      {pending.length > 0 && (
        <section className="mb-6 space-y-3">
          {pending.map((c) => {
            const id = idOf(c);
            const st = waStatus(c);
            const num = waNum(c);
            const isCaptured = st === "captured";
            return (
              <div key={`banner-${id}`} className="wa-flash rounded-lg border border-(--live) bg-(--card) p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-sm font-semibold text-(--live)">
                      📱 WhatsApp 待对接
                      <span className="ml-2 rounded-sm bg-(--live-soft) px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-(--live-ink)">
                        {isCaptured ? "已拿到号码" : "客户已应承加"}
                      </span>
                    </p>
                    <p className="mt-1 text-sm text-(--foreground)">
                      {labelOf(c)} <span className="muted">· {id}</span>
                    </p>
                    {isCaptured && num ? (
                      <p className="mt-0.5 font-mono text-lg tracking-wider text-(--foreground)">
                        {num}
                        {copied === num && <span className="ml-2 text-xs text-emerald-600">已复制 <Check className="h-3.5 w-3.5" /></span>}
                      </p>
                    ) : (
                      <p className="mt-0.5 text-xs muted">客户应承咗加专员,等紧佢俾号码 / 由专员主动联系。</p>
                    )}
                  </div>
                  <div className="flex shrink-0 flex-wrap gap-2">
                    {isCaptured && num && (
                      <button className="btn-ghost text-xs" onClick={() => copyNum(num)}>
                        {copied === num ? "已复制" : "复制号码"}
                      </button>
                    )}
                    <button className="btn-primary text-xs" onClick={() => handleDone(c)}>标记已对接</button>
                    <Link href={`/calls?call=${encodeURIComponent(id)}`} className="btn-ghost text-xs">
                      进入工作台
                    </Link>
                  </div>
                </div>
              </div>
            );
          })}
        </section>
      )}

      <section className="card">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="font-semibold">通话</h2>
          <span className="text-xs muted">{rows.length} 路</span>
        </div>
        {rows.length === 0 ? (
          <p className="text-sm muted">暂无通话。有进行中(active)或已暂停(paused)的通话会显示在这里。</p>
        ) : (
          <div className="space-y-3">
            {rows.map((c) => {
              const id = idOf(c);
              const status = String(c.status ?? "active");
              const paused = status === "paused" || Boolean(c.escalated_to_human);
              const wa = waStatus(c);
              const waPending = PENDING_WA.includes(wa);
              const step = currentStep(turns[id]);
              const lastLine = lastCustomerLine(turns[id]);
              const isBusy = busy.startsWith(`${id}:`);
              return (
                <div key={id} className={`rounded-lg p-4 ${waPending ? "wa-flash bg-muted/60" : "bg-muted/60"}`}>
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="truncate font-medium">
                        {labelOf(c)}
                        {waPending && (
                          <span className="ml-2 rounded-sm bg-(--live-soft) px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-(--live-ink)">
                            WhatsApp {wa === "captured" && waNum(c) ? waNum(c) : "待对接"}
                          </span>
                        )}
                      </p>
                      <p className="mt-0.5 text-xs muted">
                        {langLabel(c.language)} · {step ? `话术第 ${step} 步` : "无话术"} ·{" "}
                        {elapsedLabel(c.created_at) ? `已进行 ${elapsedLabel(c.created_at)}` : "—"} · {id}
                      </p>
                      {lastLine && (
                        <p className="mt-1 truncate text-xs text-(--foreground)/80">客户：「{lastLine}」</p>
                      )}
                    </div>
                    <span
                      className={`inline-flex shrink-0 items-center gap-1.5 text-xs ${
                        paused ? "text-amber-700" : "text-emerald-600"
                      }`}
                    >
                      <span className={`h-2 w-2 rounded-full animate-pulse ${paused ? "bg-amber-400" : "bg-emerald-500"}`} />
                      {paused ? "AI 已暂停" : "进行中"}
                    </span>
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <button className="btn-primary text-xs" onClick={() => openListen(id)}>静默旁听</button>
                    {!paused && (
                      <button className="btn-ghost text-xs" disabled={isBusy} onClick={() => void act(id, "pause", api.supervisorPause)}>
                        暂停 AI
                      </button>
                    )}
                    {paused && (
                      <button className="btn-ghost text-xs" disabled={isBusy} onClick={() => void act(id, "resume", api.supervisorResume)}>
                        恢复 AI
                      </button>
                    )}
                    {!c.escalated_to_human && (
                      <button
                        className="btn-ghost text-xs"
                        disabled={isBusy}
                        onClick={() =>
                          confirmAct(c, "takeover", api.supervisorTakeover, `确认接管「${labelOf(c)}」？接管后 AI 停止自动应答。`)
                        }
                      >
                        接管（人工）
                      </button>
                    )}
                    <button
                      className="btn-ghost text-xs"
                      disabled={isBusy}
                      onClick={() =>
                        confirmAct(c, "transfer", api.supervisorTransfer, `确认转人工？将结束 AI 通话并断开房间：「${labelOf(c)}」。`)
                      }
                    >
                      转人工
                    </button>
                    <button
                      className="btn-ghost text-xs text-red-600/80 hover:text-red-600"
                      disabled={isBusy}
                      onClick={() => confirmAct(c, "hangup", api.hangup, `确认挂断「${labelOf(c)}」？通话将结束并触发结算。`)}
                    >
                      挂断
                    </button>
                    <Link href={`/calls?call=${encodeURIComponent(id)}`} className="btn-ghost text-xs text-(--live)">
                      进入工作台
                    </Link>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
