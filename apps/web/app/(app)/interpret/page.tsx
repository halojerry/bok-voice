"use client";

import { useEffect, useState } from "react";
import InterpretConsole from "@/components/interpret-console";
import { useAccount } from "@/components/account-context";
import { api } from "@/lib/api";
import { friendlyErrorText } from "@/lib/api-ready";
import { minimaxVoiceOptionsFor } from "@/lib/minimax-voices";

/**
 * 双端同声传译(B 线 v2)——坐席一体台单模式(2026-09-12 用户拍板:同传只保留
 * 坐席一体台·单页双通道,双端「我方建房/对方加入」模式移除)。
 *
 * 单页接入同传房间 me/other 两个身份(两人同机各一支麦),双向译文默认都从
 * 系统扬声器出声;语言对建房时钉死——先选好再创建。会话落 CP(kind=interpret),
 * 结束时 settle → 总结/知识沉淀复用 A 线。
 */

const LANGS = [
  { value: "zh", label: "普通话" },
  { value: "cantonese", label: "粤语" },
  { value: "en", label: "English" },
];

export default function InterpretPage() {
  const { accountId: ACCOUNT } = useAccount();
  const [myLang, setMyLang] = useState("zh");
  const [otherLang, setOtherLang] = useState("en");
  // 会话级音色(2026-09-17):我方/对方语言各选一把 MiniMax 音色,空=跟随设置。
  const [myVoice, setMyVoice] = useState("");
  const [otherVoice, setOtherVoice] = useState("");
  // MiniMax 云端克隆音色（路线 B）：全语言槽可选（克隆音色无语言绑定）。
  const [cloneVoices, setCloneVoices] = useState<Array<{ voice_id: string; label?: string }>>([]);
  useEffect(() => {
    api.listMinimaxVoices()
      .then((rows) => setCloneVoices(rows.map((r) => ({ voice_id: String(r.voice_id ?? ""), label: r.label ? String(r.label) : undefined }))))
      .catch(() => setCloneVoices([]));
  }, []);
  const [glossary, setGlossary] = useState("");
  const [callId, setCallId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /** 会话级音色下拉：首项=跟随设置 + MiniMax 静态目录 + 云端克隆（全语言槽）。 */
  function voiceOptions(lang: string) {
    return [
      { value: "", label: "（默认，跟随设置）" },
      ...minimaxVoiceOptionsFor(lang),
      ...cloneVoices.map((c) => ({ value: c.voice_id, label: `克隆 · ${c.label || c.voice_id}` })),
    ];
  }

  async function startConsole() {
    setError(null);
    setBusy(true);
    try {
      const voices: Record<string, string> = {};
      if (myVoice) voices[myLang] = myVoice;
      if (otherVoice) voices[otherLang] = otherVoice;
      const created = await api.createCall({
        account_id: ACCOUNT,
        object_id: "",
        kind: "interpret",
        mode: "live",
        direction: "interpret",
        language: myLang,
        target_lang: otherLang,
        glossary,
        voices_json: Object.keys(voices).length ? JSON.stringify(voices) : "",
      });
      const id = String((created as { id?: string }).id ?? "");
      if (!id) {
        setBusy(false);
        setError("创建一体台会话失败。");
        return;
      }
      setCallId(id);
      setBusy(false);
    } catch (e) {
      setBusy(false);
      setError(friendlyErrorText(String(e)));
    }
  }

  if (callId) {
    return (
      <InterpretConsole
        account={ACCOUNT}
        callId={callId}
        myLang={myLang}
        otherLang={otherLang}
        onExit={() => {
          setCallId("");
          setError(null);
        }}
      />
    );
  }

  return (
    <div className="mx-auto w-full max-w-3xl">
      <section className="card flex flex-col gap-4">
        <span className="label">坐席一体台 · 单页双通道</span>
        <div className="flex gap-3">
          <label className="flex flex-1 flex-col gap-1 text-xs">
            <span className="text-(--stage-muted)">我方讲</span>
            <select
              className="select"
              value={myLang}
              onChange={(e) => {
                setMyLang(e.target.value);
                setMyVoice(""); // 语言换了,音色目录跟着换,旧选择重置
              }}
            >
              {LANGS.map((l) => (
                <option key={l.value} value={l.value}>
                  {l.label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-1 flex-col gap-1 text-xs">
            <span className="text-(--stage-muted)">对方讲</span>
            <select
              className="select"
              value={otherLang}
              onChange={(e) => {
                setOtherLang(e.target.value);
                setOtherVoice("");
              }}
            >
              {LANGS.map((l) => (
                <option key={l.value} value={l.value}>
                  {l.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="flex gap-3">
          <label className="flex flex-1 flex-col gap-1 text-xs">
            <span className="text-(--stage-muted)">我方音色（可选，rev 双向出声开启时为我方译文声）</span>
            <select className="select" value={myVoice} onChange={(e) => setMyVoice(e.target.value)}>
              {voiceOptions(myLang).map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-1 flex-col gap-1 text-xs">
            <span className="text-(--stage-muted)">对方音色（可选，对方听到的译文声）</span>
            <select className="select" value={otherVoice} onChange={(e) => setOtherVoice(e.target.value)}>
              {voiceOptions(otherLang).map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label className="flex flex-col gap-1 text-xs">
          <span className="text-(--stage-muted)">术语表（可选，治专名误听与译名漂移）</span>
          <textarea
            className="textarea min-h-20"
            value={glossary}
            onChange={(e) => setGlossary(e.target.value)}
            placeholder={"每条「源词=译文」或纯词条，逗号/分号/换行分隔。如：\n顺丰=SF Express；拼多多=Pinduoduo\n林总（纯词条=原样保留）"}
          />
        </label>
        <p className="text-xs leading-relaxed text-(--stage-muted)">
          两人各一支麦：一个页面同时接入本会话两端，同页看双向原文+译文字幕。听感拓扑——
          <strong>对方听到我方译文的 TTS</strong>，<strong>我方听到对方原声</strong>（像直接通话），
          对方→我方的译文只显示文字不出声；我方译文播报时自动暂让对方麦克风防串译。
          说话中按句出译文（不必等停嘴）。语言对在建房时钉死——请先选好再创建。进房后按「启动传译」才开始。
          音色可按语言另选（MiniMax 云端音色）；不选则用设置页「分语言音色」。我方音色仅在 rev
          双向出声开启时用于我方译文（默认我方纯字幕）。
        </p>
        <button className="stage-btn-primary w-fit" disabled={busy} onClick={startConsole}>
          创建一体台会话
        </button>
        {error && <p className="text-sm text-red-600">{error}</p>}
      </section>
    </div>
  );
}
