"use client";

import { useEffect, useState } from "react";
import InterpretConsole from "@/components/interpret-console";
import { useAccount } from "@/components/account-context";
import { api } from "@/lib/api";
import { friendlyErrorText } from "@/lib/api-ready";
import { parseVoiceMap, primaryVoiceFor } from "@/lib/voice-map";

/**
 * 双端同声传译(B 线 v2)——坐席一体台单模式(2026-09-12 用户拍板:同传只保留
 * 坐席一体台·单页双通道,双端「我方建房/对方加入」模式移除)。
 *
 * 单页接入同传房间 me/other 两个身份(两人同机各一支麦),双向译文默认都从
 * 系统扬声器出声;语言对建房时钉死——先选好再创建。会话落 CP(kind=interpret),
 * 结束时 settle → 总结/知识沉淀复用 A 线。
 */

/** 源语（讲话方的 ASR 输入侧）：豆包 SAUC 实测只认三语——W2c 实测判决
 * （2026-10-07，reports/cloud-asr/ 留档）：豆包 seedasr 2.0 对 de/fr/ja/pt
 * 24/24 折叠 MISS（输出=中英混杂幻觉或空串，nostream 显式 language 标签
 * 同样 0/21=模型本身不行）。ASR 不认的方向不做（Ethan 裁定口径）。 */
const SOURCE_LANGS = [
  { value: "zh", label: "普通话" },
  { value: "cantonese", label: "粤语" },
  { value: "en", label: "English" },
];

/** 目标语（译文 TTS 出声侧）：七语全放——MT=DeepSeek 任意对、TTS=MiniMax
 * 四语目录（lib/minimax-voices.ts，audition 真合成验证）。即「我讲普通话、
 * 对方听到德语」今天就是好的；坏的只是对方讲德语（ASR 输入侧）。
 * 四语双向放行候选=按语种分 ASR 车道（MiniMax ASR 四语 24/24 满分，但它是
 * VAD 切段伪流式无热词，见 2026-10-03 评估）——独立评估票，未开。 */
const TARGET_LANGS = [
  ...SOURCE_LANGS,
  { value: "de", label: "德语" },
  { value: "fr", label: "法语" },
  { value: "ja", label: "日语" },
  { value: "pt", label: "葡萄牙语" },
];

export default function InterpretPage() {
  const { accountId: ACCOUNT } = useAccount();
  const [myLang, setMyLang] = useState("zh");
  const [otherLang, setOtherLang] = useState("en");
  // 音色=人设预设（2026-10-08 用户拍板统一）：我方/对方各选一个人设,声音跟
  // 人设走（reference_audio 整场主音色,A 线同款 collapse）——不再有独立于
  // 人设的音色目录下拉（此前「人设音色」+「我方/对方音色」=两套选择器,且
  // 目录按语言切换与人设预设对不上）。配音语义：我说的译文用我的声(对方
  // 听),对方说的译文用对方的声(我听)——每人说话保持自己的声音。
  const [personas, setPersonas] = useState<Array<Record<string, unknown>>>([]);
  const [myPersonaId, setMyPersonaId] = useState("");
  const [otherPersonaId, setOtherPersonaId] = useState("");
  useEffect(() => {
    api.listPersonas()
      .then((rows) => setPersonas(Array.isArray(rows) ? rows : []))
      .catch(() => setPersonas([]));
  }, []);
  /** 人设 → 整场主音色（lib/voice-map 单源解析,与 A 线 agent 收敛同规则）。 */
  function personaVoice(id: string): string {
    const p = personas.find((r) => String(r.id ?? "") === id);
    if (!p) return "";
    return primaryVoiceFor(String(p.language ?? "zh"), parseVoiceMap(p.reference_audio));
  }
  function personaLabel(p: Record<string, unknown>): string {
    const name = String(p.name ?? "") || String(p.id ?? "");
    const voice = primaryVoiceFor(String(p.language ?? "zh"), parseVoiceMap(p.reference_audio));
    return voice ? `${name} · ${voice}` : `${name}（未设音色）`;
  }
  const [glossary, setGlossary] = useState("");
  const [callId, setCallId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function startConsole() {
    setError(null);
    setBusy(true);
    try {
      // 配音语义映射：我方人设声=对方语言槽(对方听我说的话),对方人设声=
      // 我方语言槽(我听对方说的话)。voices_json 键=B 线七语(_norm_lang 已
      // 收 de/fr/ja/pt),不选人设的槽=跟随设置页分语言音色。
      const voices: Record<string, string> = {};
      const myV = personaVoice(myPersonaId);
      const otherV = personaVoice(otherPersonaId);
      if (myV) voices[otherLang] = myV;
      if (otherV) voices[myLang] = otherV;
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
            <span className="muted">我方讲</span>
            <select className="select" value={myLang} onChange={(e) => setMyLang(e.target.value)}>
              {SOURCE_LANGS.map((l) => (
                <option key={l.value} value={l.value}>
                  {l.label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-1 flex-col gap-1 text-xs">
            <span className="muted">对方讲</span>
            <select className="select" value={otherLang} onChange={(e) => setOtherLang(e.target.value)}>
              {TARGET_LANGS.map((l) => (
                <option key={l.value} value={l.value}>
                  {l.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="flex gap-3">
          <label className="flex flex-1 flex-col gap-1 text-xs">
            <span className="muted">我方人设（我说话的声音——对方听到的译文用它）</span>
            <select className="select" value={myPersonaId} onChange={(e) => setMyPersonaId(e.target.value)}>
              <option value="">（跟随设置默认音色）</option>
              {personas.map((p) => (
                <option key={String(p.id)} value={String(p.id)}>
                  {personaLabel(p)}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-1 flex-col gap-1 text-xs">
            <span className="muted">对方人设（对方说话的声音——我听到的译文用它）</span>
            <select className="select" value={otherPersonaId} onChange={(e) => setOtherPersonaId(e.target.value)}>
              <option value="">（跟随设置默认音色）</option>
              {personas.map((p) => (
                <option key={String(p.id)} value={String(p.id)}>
                  {personaLabel(p)}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label className="flex flex-col gap-1 text-xs">
          <span className="muted">术语表（可选，治专名误听与译名漂移）</span>
          <textarea
            className="textarea min-h-20"
            value={glossary}
            onChange={(e) => setGlossary(e.target.value)}
            placeholder={"每条「源词=译文」或纯词条，逗号/分号/换行分隔。如：\n顺丰=SF Express；拼多多=Pinduoduo\n林总（纯词条=原样保留）"}
          />
        </label>
        <p className="text-xs leading-relaxed muted">
          两人各一支麦：一个页面同时接入本会话两端，同页看双向原文+译文字幕。听感拓扑——
          <strong>对方听到我方译文的 TTS</strong>，<strong>我方听到对方原声 + 对方译文的译员耳语</strong>
          （译员耳语默认开，进房后控制台「译员耳语」开关可关）；我方译文播报时自动暂让对方麦克风防串译。
          说话中按句出译文（不必等停嘴）。语言对在建房时钉死——请先选好再创建。进房后按「启动传译」才开始。
          音色跟人设走（全场同声、跨语言不换声）——我方/对方人设的音色在「人设」页预设（含克隆）；
          不选人设=跟随设置页「分语言音色」&gt; 默认。
        </p>
        <button className="stage-btn-primary w-fit" disabled={busy} onClick={startConsole}>
          创建一体台会话
        </button>
        {error && <p className="text-sm text-red-600">{error}</p>}
      </section>
    </div>
  );
}
