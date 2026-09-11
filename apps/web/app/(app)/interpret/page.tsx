"use client";

import { useState } from "react";
import InterpretConsole from "@/components/interpret-console";
import { useAccount } from "@/components/account-context";
import { api } from "@/lib/api";
import { friendlyErrorText } from "@/lib/api-ready";

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
  const [callId, setCallId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
            <select className="select" value={myLang} onChange={(e) => setMyLang(e.target.value)}>
              {LANGS.map((l) => (
                <option key={l.value} value={l.value}>
                  {l.label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-1 flex-col gap-1 text-xs">
            <span className="text-(--stage-muted)">对方讲</span>
            <select className="select" value={otherLang} onChange={(e) => setOtherLang(e.target.value)}>
              {LANGS.map((l) => (
                <option key={l.value} value={l.value}>
                  {l.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <p className="text-xs leading-relaxed text-(--stage-muted)">
          两人同机各一支麦：一个页面同时接入本会话两端，我方与对方的译文都从同一个扬声器出声
          （默认共享输出，Mac/Windows 任何内核可用），同页看双向原文+译文字幕；译文播报时自动
          暂让对向麦克风防串译。语言对在建房时钉死——请先选好再创建。两人各戴耳机分路听（独立双输出）
          为高级选项，需桌面 Chrome。
        </p>
        <button className="stage-btn-primary w-fit" disabled={busy} onClick={startConsole}>
          创建一体台会话
        </button>
        {error && <p className="text-sm text-red-400">{error}</p>}
      </section>
    </div>
  );
}
