"use client";

/**
 * 罐头音试听卡（2026-09-11 症状④）：垫话/ QA 罐头此前没有任何试听入口。
 * - 垫话：GET /api/tts/filler-preview 直接吐源码 wav 资产（零云调用,随机换句）。
 * - QA 罐头：词条来自 /api/qa-entries；试听走 /api/tts/preview（MiniMax 云合成,
 *   语速已与运行时同一条语言档规则 zh/粤 1.2——试了就是真通话的节奏）。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, apiBase } from "@/lib/api";

const LANGS = [
  { value: "zh", label: "普通话" },
  { value: "cantonese", label: "粤语" },
  { value: "en", label: "English" },
] as const;

type QaEntry = {
  id?: string;
  lang?: string;
  question_text?: string;
  answer_text?: string;
  voice_id?: string;
};

export default function CannedAuditionCard() {
  const [lang, setLang] = useState<string>("zh");
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  const [entries, setEntries] = useState<QaEntry[]>([]);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    const el = new Audio();
    el.preload = "none";
    audioRef.current = el;
    return () => {
      el.pause();
    };
  }, []);

  const play = useCallback(async (url: string, tag: string) => {
    const el = audioRef.current;
    if (!el) return;
    setBusy(tag);
    setNote("");
    try {
      el.pause();
      el.src = url;
      await el.play();
      setNote("播放中…");
    } catch {
      setNote("播放失败：请检查系统音量/输出设备。");
    } finally {
      setBusy("");
    }
  }, [audioRef]);

  const playBlob = useCallback(
    async (p: Promise<Blob | null>, tag: string, why?: string | (() => string)) => {
      const blob = await p;
      if (!blob) {
        const reason = typeof why === "function" ? why() : (why ?? "");
        setNote(`合成失败${reason ? `：${reason}` : "：请确认已在「TTS 语音合成」配置 MiniMax API Key。"}`);
        return;
      }
      await play(URL.createObjectURL(blob), tag);
    },
    [play],
  );

  const previewFiller = useCallback(() => {
    const i = Math.floor(Math.random() * 13);
    void playBlob(
      fetch(`${apiBase()}/api/tts/filler-preview?lang=${lang}&i=${i}`).then((r) => (r.ok ? r.blob() : null)),
      `filler-${lang}`,
    );
  }, [lang, playBlob]);

  const loadQa = useCallback(async () => {
    setBusy("qa-load");
    try {
      const rows = (await api.listQaEntries(1)) as QaEntry[];
      setEntries((Array.isArray(rows) ? rows : []).filter((e) => (e.lang ?? "") === lang).slice(0, 8));
    } catch {
      setEntries([]);
    } finally {
      setBusy("");
    }
  }, [lang]);

  useEffect(() => {
    void loadQa();
  }, [loadQa]);

  const previewQa = useCallback(
    (e: QaEntry) => {
      let why = "";
      void playBlob(
        api
          .previewTts({
            provider: "minimax",
            text: String(e.answer_text ?? ""),
            voice: String(e.voice_id ?? ""),
            language: lang,
            sample_rate: 24000,
          })
          .catch((err: unknown) => {
            why = String(err ?? "");
            return null;
          }),
        `qa-${e.id ?? ""}`,
        () => why,
      );
    },
    [lang, playBlob],
  );

  return (
    <section className="card flex flex-col gap-3">
      <span className="label">罐头音试听</span>
      <div className="flex flex-wrap items-center gap-2">
        {LANGS.map((l) => (
          <button
            key={l.value}
            className={`rounded-full px-2.5 py-1 text-[11px] ${
              lang === l.value ? "bg-(--accent) font-medium text-(--accent-ink)" : "border border-(--card-border) text-(--stage-muted)"
            }`}
            onClick={() => setLang(l.value)}
          >
            {l.label}
          </button>
        ))}
      </div>
      <div className="flex items-center gap-2">
        <button className="btn-ghost" onClick={previewFiller} disabled={busy === `filler-${lang}`}>
          {busy === `filler-${lang}` ? "装载中…" : "随机试听垫话"}
        </button>
        <span className="text-xs muted">源码资产 · 零云调用</span>
      </div>
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between">
          <span className="text-xs muted">QA 罐头词条（按字面命中才会在通话里出声）</span>
          <button className="text-xs underline decoration-dotted" onClick={() => void loadQa()}>
            刷新
          </button>
        </div>
        {entries.length === 0 ? (
          <p className="text-xs muted">该语言暂无启用的词条。</p>
        ) : (
          <ul className="flex max-h-56 flex-col gap-1 overflow-y-auto">
            {entries.map((e, idx) => (
              <li key={e.id ?? idx} className="flex items-center justify-between gap-2 rounded-md border border-(--card-border) px-2 py-1.5">
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs">{e.question_text}</div>
                  <div className="truncate text-[10px] muted">{e.answer_text}</div>
                </div>
                <button
                  className="shrink-0 rounded-full border border-(--card-border) px-2 py-0.5 text-[11px]"
                  onClick={() => previewQa(e)}
                  disabled={busy === `qa-${e.id ?? ""}`}
                >
                  {busy === `qa-${e.id ?? ""}` ? "合成中…" : "试听"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      {note && <p className="text-xs muted">{note}</p>}
    </section>
  );
}
