"use client";

import { useEffect, useState } from "react";
import InterpretConsole from "@/components/interpret-console";
import { useAccount } from "@/components/account-context";
import { useToast } from "@/components/toast";
import { api } from "@/lib/api";
import { friendlyErrorText } from "@/lib/api-ready";
import { MINIMAX_VOICE_ENTRIES } from "@/lib/minimax-voices";
import { buildVoiceSelectOptions, previewSampleText, resolvePreviewLang } from "@/lib/voice-options";
import { playAudioBlob, previewVoice } from "@/lib/preview";

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
  const toast = useToast();
  const [myLang, setMyLang] = useState("zh");
  const [otherLang, setOtherLang] = useState("en");
  // 会话级音色(2026-10-09 回滚口径, Ethan 拍板)：我方/对方各选一把 MiniMax
  // 音色（与人设页同源目录——静态目录按槽位语言过滤 + 云端克隆全语言可选），
  // 空=跟随设置。不做人设级绑定（2026-10-08 配音语义版作废）。映射=老口径：
  // voices[myLang]=我方音色（我听到的译文声——译员耳语）、
  // voices[otherLang]=对方音色（对方听到的译文声），两槽各自独立。
  const [myVoice, setMyVoice] = useState("");
  const [otherVoice, setOtherVoice] = useState("");
  // 试听只播已物化缓存（apps/web/public/minimax-auditions/*.mp3，seed 脚本
  // 全量真合成落盘），不现场合成烧云——缓存缺席=toast 提示，不回落 live。
  const [previewing, setPreviewing] = useState<"" | "my" | "other">("");
  // MiniMax 云端克隆音色：全语言槽可选（克隆音色无语言绑定）。
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

  /** 会话级音色下拉：首项=跟随设置 + MiniMax 静态目录（按槽位语言过滤）+
   *  云端克隆（全语言槽可选，匹配槽位语言的克隆置顶）——装配统一走
   *  lib/voice-options.buildVoiceSelectOptions，目录源与人设页同源。 */
  function voiceOptions(lang: string) {
    return buildVoiceSelectOptions({
      catalog: MINIMAX_VOICE_ENTRIES,
      slotLang: lang,
      minimaxClones: cloneVoices,
      firstOption: { value: "", label: "（默认，跟随设置）" },
    });
  }

  /** 试听（缓存直放）：物化 mp3 命中即播（零云费零延迟）；未物化（克隆/未跑
   *  seed 的部署）toast 提示，绝不现场合成。 */
  async function previewMyVoice(which: "my" | "other") {
    const voice = which === "my" ? myVoice : otherVoice;
    const lang = which === "my" ? myLang : otherLang;
    if (!voice || previewing) return;
    setPreviewing(which);
    try {
      const blob = await previewVoice({
        voice,
        text: previewSampleText(resolvePreviewLang(voice, { fallback: lang })),
        allowLive: false,
      });
      await playAudioBlob(blob);
    } catch {
      toast.error("该音色暂无本地试听缓存（试听只播已物化缓存，不现场合成）");
    } finally {
      setPreviewing("");
    }
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
            <span className="muted">我方讲</span>
            <select
              className="select"
              value={myLang}
              onChange={(e) => {
                setMyLang(e.target.value);
                setMyVoice(""); // 语言换了,音色目录跟着换,旧选择重置
              }}
            >
              {SOURCE_LANGS.map((l) => (
                <option key={l.value} value={l.value}>
                  {l.label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-1 flex-col gap-1 text-xs">
            <span className="muted">对方讲</span>
            <select
              className="select"
              value={otherLang}
              onChange={(e) => {
                setOtherLang(e.target.value);
                setOtherVoice("");
              }}
            >
              {TARGET_LANGS.map((l) => (
                <option key={l.value} value={l.value}>
                  {l.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="flex gap-3">
          <div className="flex flex-1 flex-col gap-1 text-xs">
            <span className="muted">我方音色（我听到的译文声——译员耳语）</span>
            <div className="flex gap-2">
              <select className="select min-w-0 flex-1" value={myVoice} onChange={(e) => setMyVoice(e.target.value)}>
                {voiceOptions(myLang).map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="stage-btn shrink-0"
                disabled={!myVoice || previewing !== ""}
                onClick={() => void previewMyVoice("my")}
              >
                {previewing === "my" ? "试听中…" : "试听"}
              </button>
            </div>
          </div>
          <div className="flex flex-1 flex-col gap-1 text-xs">
            <span className="muted">对方音色（对方听到的译文声）</span>
            <div className="flex gap-2">
              <select className="select min-w-0 flex-1" value={otherVoice} onChange={(e) => setOtherVoice(e.target.value)}>
                {voiceOptions(otherLang).map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="stage-btn shrink-0"
                disabled={!otherVoice || previewing !== ""}
                onClick={() => void previewMyVoice("other")}
              >
                {previewing === "other" ? "试听中…" : "试听"}
              </button>
            </div>
          </div>
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
          （译员耳语默认开，进房后控制台「译员耳语」开关可关）；缺省真全双工（远程双方各戴耳机，
          互不闭麦），仅两人同机外放时在控制台开「同机模式闭麦」防串译。
          说话中按句出译文（不必等停嘴）。语言对在建房时钉死——请先选好再创建。进房后按「启动传译」才开始。
          音色与人设页同源（MiniMax 目录按语言过滤 + 云端克隆），试听只播本地缓存；不选则用设置页「分语言音色」&gt; 默认。
        </p>
        <button className="stage-btn-primary w-fit" disabled={busy} onClick={startConsole}>
          创建一体台会话
        </button>
        {error && <p className="text-sm text-red-600">{error}</p>}
      </section>
    </div>
  );
}
