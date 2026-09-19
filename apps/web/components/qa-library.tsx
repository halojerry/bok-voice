"use client";

// 问答库（PRD 3.4 / 需求7，2026-09-20 第二批）：通话中按字面命中即播罐头快答。
// 表格+弹窗配置模式；编辑字段=引擎真实面（问法/回答/优先级/语言/挂步/启用），
// 回答文本带变量按钮（VarTextarea）。多轮行为（播完跳转/通知人工）属意图绑定，
// 在「意图管理」里挂 play_qa 动作——弹窗内有指引说明。
// 写路径=api.createQa / api.patchQa / api.deleteQa（与 /qa 页同一条）。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { VarTextarea } from "@/components/var-insert";
import { previewVoice } from "@/lib/preview";

const LANGS: [string, string][] = [
  ["zh", "普通话"],
  ["cantonese", "粤语"],
  ["en", "English"],
];
const LANG_LABEL: Record<string, string> = Object.fromEntries(LANGS);

type QaRow = {
  id?: string;
  question_text?: string;
  answer_text?: string;
  lang?: string;
  scope?: string;
  step_index?: number;
  template_id?: string;
  owner_user_id?: string;
  enabled?: boolean;
  priority?: number;
};

const EMPTY_FORM = {
  question_text: "",
  answer_text: "",
  lang: "zh",
  /** -1=全程通用（scope global）；≥0=挂到该步（scope step, step_index）。 */
  step: -1,
  enabled: true,
  priority: "10",
};

const inputCls =
  "w-full rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--live)";

/** 条目编辑弹窗（受控；保存时钳位+按挂步档产出 scope/step_index/template_id）。 */
function QaModal(props: {
  form: typeof EMPTY_FORM;
  editingId: string | null;
  stepCount: number;
  templateId: string;
  lang: string;
  readOnly: boolean;
  onChange: (next: typeof EMPTY_FORM) => void;
  onClose: () => void;
  onSubmit: () => void;
  busy: boolean;
  err: string;
}) {
  const { form, readOnly } = props;
  const patch = (p: Partial<typeof EMPTY_FORM>) => props.onChange({ ...form, ...p });
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-foreground/30 p-4">
      <div className="w-full max-w-xl space-y-3 rounded-xl border border-(--card-border) bg-background p-4 shadow-lg">
        <div className="flex items-center justify-between">
          <span className="label">{props.editingId ? "编辑问答" : "新建问答"}</span>
          <button className="btn-ghost px-2 py-0.5 text-xs" onClick={props.onClose}>关闭 ✕</button>
        </div>

        <label className="block">
          <span className="text-xs muted">客户问法 *（0.90 字面匹配——客户要问到几乎一样的说法才命中）</span>
          <input
            className={`mt-1 ${inputCls}`}
            maxLength={200}
            value={form.question_text}
            disabled={readOnly}
            placeholder="如：你们是哪家公司"
            onChange={(e) => patch({ question_text: e.target.value })}
          />
        </label>

        <div>
          <span className="text-xs muted">标准回答 *（命中即跳过 AI 直接播这条，建议先物化录音）</span>
          <div className="mt-1">
            <VarTextarea
              className={`h-24 resize-none ${inputCls}`}
              value={form.answer_text}
              disabled={readOnly}
              onChange={(v) => patch({ answer_text: v })}
            />
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <label className="block">
            <span className="text-xs muted">语言</span>
            <select
              className={`mt-1 ${inputCls}`}
              value={form.lang}
              disabled={readOnly}
              onChange={(e) => patch({ lang: e.target.value })}
            >
              {LANGS.map(([v, l]) => (
                <option key={v} value={v}>{l}</option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="text-xs muted">优先级（小者先）</span>
            <input
              type="number"
              min={0}
              max={1000}
              className={`mt-1 ${inputCls}`}
              value={form.priority}
              disabled={readOnly}
              onChange={(e) => patch({ priority: e.target.value })}
            />
          </label>
          <label className="block">
            <span className="text-xs muted">挂到哪一步</span>
            <select
              className={`mt-1 ${inputCls}`}
              value={String(form.step)}
              disabled={readOnly}
              onChange={(e) => patch({ step: Math.round(Number(e.target.value)) })}
            >
              <option value="-1">全程通用</option>
              {Array.from({ length: props.stepCount }, (_, i) => (
                <option key={i} value={i}>第 {i + 1} 步</option>
              ))}
            </select>
          </label>
        </div>

        <label className="flex items-center gap-1.5 text-[11px] muted">
          <input
            type="checkbox"
            className="size-3 accent-(--live)"
            checked={form.enabled}
            disabled={readOnly}
            onChange={(e) => patch({ enabled: e.target.checked })}
          />
          启用
        </label>

        <p className="rounded-lg bg-muted/60 px-2 py-1.5 text-[11px] leading-relaxed muted">
          提示：命中本条后想继续多轮（播完跳到某步 / 通知人工），在「意图管理」里给意图挂
          「播快答」动作并选中本条。回答里可点上方变量按钮插入 {"{姓名}"} 等占位符。
        </p>

        {props.err && <p className="text-sm text-red-600">{props.err}</p>}
        <div className="flex justify-end gap-2">
          <button className="btn-ghost text-xs" onClick={props.onClose}>取消</button>
          <button className="btn-primary text-xs" disabled={props.busy || readOnly} onClick={props.onSubmit}>
            {props.busy ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** 问答库 tab：表格（查询/重置/编辑/删除/试听/补料）+ 弹窗编辑 + 常用问题包一键导入。 */
export default function QaLibrary(props: {
  accountId: string;
  templateId: string;
  /** 模板语言（新建/导入默认值）。 */
  lang: string;
  stepCount: number;
  readOnly: boolean;
  /** 种子包：本语言常用问答（一键导入，scope=step 0 挂首步）。 */
  seedPack: { question: string; answer: string }[];
}) {
  const { accountId, templateId, readOnly } = props;
  const [rows, setRows] = useState<QaRow[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [query, setQuery] = useState("");
  const [modal, setModal] = useState<{ form: typeof EMPTY_FORM; editingId: string | null } | null>(null);
  const [busy, setBusy] = useState("");
  const [formErr, setFormErr] = useState("");
  const [note, setNote] = useState("");
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.listQaAll(accountId);
      setRows(Array.isArray(data) ? data : []);
      setErr("");
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [accountId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const el = new Audio();
    el.preload = "none";
    audioRef.current = el;
    return () => {
      el.pause();
    };
  }, []);

  // 本模板相关条目：挂本模板步的 + 全程通用的（全程通用对任何模板生效）。
  const scoped = useMemo(() => {
    const list = rows ?? [];
    return list.filter((r) => {
      const tid = String(r.template_id ?? "");
      const scope = String(r.scope ?? "global");
      return scope === "global" ? true : tid === templateId;
    });
  }, [rows, templateId]);

  const filtered = useMemo(() => {
    const q = query.trim();
    const list = q ? scoped.filter((r) => String(r.question_text ?? "").includes(q) || String(r.answer_text ?? "").includes(q)) : scoped;
    return [...list].sort((a, b) => (Number(a.priority ?? 10) - Number(b.priority ?? 10)) || String(a.question_text ?? "").localeCompare(String(b.question_text ?? "")));
  }, [scoped, query]);

  async function submit() {
    if (!modal) return;
    const question = modal.form.question_text.trim();
    const answer = modal.form.answer_text.trim();
    if (!question || !answer) return setFormErr("问法和回答都不能为空。");
    const prioRaw = modal.form.priority.trim() === "" ? 10 : Math.round(Number(modal.form.priority));
    const priority = Number.isFinite(prioRaw) ? Math.max(0, Math.min(prioRaw, 1000)) : 10;
    const step = Math.max(-1, Math.round(Number(modal.form.step) || -1));
    const payload: Record<string, unknown> = {
      question_text: question,
      answer_text: answer,
      lang: modal.form.lang,
      // 全程通用=global（不绑模板）；挂步=step + template_id（模板内生效）。
      scope: step < 0 ? "global" : "step",
      step_index: step < 0 ? -1 : step,
      template_id: step < 0 ? "" : templateId,
      enabled: modal.form.enabled,
      priority,
    };
    setBusy("save");
    setFormErr("");
    try {
      if (modal.editingId) await api.patchQa(modal.editingId, payload);
      else await api.createQa({ ...payload, account_id: accountId });
      setModal(null);
      await refresh();
    } catch (e) {
      setFormErr(String(e));
    } finally {
      setBusy("");
    }
  }

  async function removeRow(r: QaRow) {
    const id = String(r.id ?? "");
    if (!window.confirm(`确认删除问答「${String(r.question_text ?? id)}」？`)) return;
    setBusy(`${id}:del`);
    try {
      await api.deleteQa(id);
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy("");
    }
  }

  async function pregen(r: QaRow) {
    const id = String(r.id ?? "");
    setBusy(`${id}:pre`);
    setNote("");
    try {
      await api.pregenQa([id]);
      setNote(`已提交补料（条目 ${id}）——物化完成前试听会现场合成。`);
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy("");
    }
  }

  function audition(r: QaRow) {
    const id = String(r.id ?? "");
    setBusy(`${id}:play`);
    void (async () => {
      try {
        const blob = await previewVoice({
          provider: "minimax",
          text: String(r.answer_text ?? ""),
          voice: "",
          language: String(r.lang ?? props.lang),
          sample_rate: 24000,
          cannedEntryId: id || undefined,
        });
        const el = audioRef.current;
        if (!el || !blob) {
          setNote("合成失败：请确认已在设置页配置 MiniMax API Key。");
          return;
        }
        el.pause();
        el.src = URL.createObjectURL(blob);
        await el.play();
      } catch {
        setNote("播放失败：请检查系统音量/输出设备。");
      } finally {
        setBusy("");
      }
    })();
  }

  /** 一键导入种子包（跳过已存在同问法的；scope=step 挂首步绑定本模板）。 */
  async function importSeed() {
    const existing = new Set((rows ?? []).map((r) => String(r.question_text ?? "").trim()));
    const pending = props.seedPack.filter((s) => !existing.has(s.question));
    if (pending.length === 0) {
      setNote("常用问题包已全部导入过。");
      return;
    }
    if (!window.confirm(`导入 ${pending.length} 条「${LANG_LABEL[props.lang] ?? props.lang}」常用问答？（挂到本模板第 1 步）`)) return;
    setBusy("seed");
    try {
      for (const s of pending) {
        await api.createQa({
          question_text: s.question,
          answer_text: s.answer,
          lang: props.lang,
          scope: "step",
          step_index: 0,
          template_id: templateId,
          enabled: true,
          priority: 10,
          account_id: accountId,
        });
      }
      setNote(`已导入 ${pending.length} 条常用问答（录音沉淀 tab 可看物化状态并补料）。`);
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="card space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <input
          className="input h-8 max-w-56 text-xs"
          placeholder="按问法/回答搜索"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button className="btn-ghost text-xs" onClick={() => setQuery("")}>重置</button>
        <div className="ml-auto flex gap-2">
          {!readOnly && (
            <button
              className="btn-ghost text-xs"
              disabled={busy === "seed"}
              onClick={() => void importSeed()}
              title="预置的通用问答（防诈质疑/公司身份/赔付说明等）一键入库"
            >
              {busy === "seed" ? "导入中…" : "导入常用问题包"}
            </button>
          )}
          {!readOnly && (
            <button
              className="btn-primary text-xs"
              onClick={() => {
                setFormErr("");
                setModal({ form: { ...EMPTY_FORM, lang: props.lang }, editingId: null });
              }}
            >
              ＋新建问答
            </button>
          )}
        </div>
      </div>

      <p className="text-xs muted">
        显示：挂本模板的条目 + 全程通用条目。命中按 0.90 字面相似度——客户要问到几乎一样的说法。
      </p>
      {err && <ErrorState message={err} />}
      {note && <p className="text-xs text-(--live-ink)">{note}</p>}
      {loading && rows === null ? (
        <LoadingState />
      ) : filtered.length === 0 ? (
        <EmptyState label="暂无问答条目——可点「导入常用问题包」或新建。" />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-(--card-border) muted">
                <th className="py-1.5 pr-2 font-medium">问法</th>
                <th className="py-1.5 pr-2 font-medium">回答</th>
                <th className="py-1.5 pr-2 font-medium">优先级</th>
                <th className="py-1.5 pr-2 font-medium">语言</th>
                <th className="py-1.5 pr-2 font-medium">挂载</th>
                <th className="py-1.5 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {filtered.slice(0, 100).map((r) => {
                const id = String(r.id ?? "");
                const stepIdx = Number(r.step_index ?? -1);
                const scope = String(r.scope ?? "global");
                return (
                  <tr key={id} className="border-b border-(--card-border)/60 align-top">
                    <td className="max-w-56 py-2 pr-2 font-medium">
                      <span className="line-clamp-1">{String(r.question_text ?? "-")}</span>
                    </td>
                    <td className="max-w-56 py-2 pr-2">
                      <span className="line-clamp-1 muted">{String(r.answer_text ?? "-")}</span>
                    </td>
                    <td className="py-2 pr-2 muted">P{Number(r.priority ?? 10)}</td>
                    <td className="py-2 pr-2 muted">{LANG_LABEL[String(r.lang ?? "zh")] ?? String(r.lang ?? "-")}</td>
                    <td className="py-2 pr-2 muted">
                      {scope === "global" ? "全程通用" : `第 ${stepIdx + 1} 步`}
                      {r.enabled === false && <span className="ml-1 rounded-sm bg-muted px-1 text-[10px]">停用</span>}
                    </td>
                    <td className="py-2">
                      <div className="flex gap-2">
                        <button
                          className="text-(--live)"
                          disabled={busy === `${id}:play`}
                          onClick={() => audition(r)}
                        >
                          {busy === `${id}:play` ? "合成中…" : "试听"}
                        </button>
                        <button
                          className="underline decoration-dotted"
                          disabled={busy === `${id}:pre`}
                          onClick={() => void pregen(r)}
                          title="物化本条罐头录音（对齐运行时音色）"
                        >
                          {busy === `${id}:pre` ? "补料中…" : "补料"}
                        </button>
                        {!readOnly && (
                          <>
                            <button
                              className="underline decoration-dotted"
                              onClick={() => {
                                setFormErr("");
                                setModal({
                                  editingId: id,
                                  form: {
                                    question_text: String(r.question_text ?? ""),
                                    answer_text: String(r.answer_text ?? ""),
                                    lang: String(r.lang ?? props.lang),
                                    step: scope === "global" ? -1 : Math.max(0, stepIdx),
                                    enabled: r.enabled !== false,
                                    priority: String(r.priority ?? 10),
                                  },
                                });
                              }}
                            >
                              编辑
                            </button>
                            <button className="text-red-600" disabled={busy === `${id}:del`} onClick={() => void removeRow(r)}>
                              删除
                            </button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {filtered.length > 100 && <p className="mt-1 text-[11px] muted">共 {filtered.length} 条，显示前 100 条。</p>}
        </div>
      )}

      {modal && (
        <QaModal
          form={modal.form}
          editingId={modal.editingId}
          stepCount={props.stepCount}
          templateId={templateId}
          lang={props.lang}
          readOnly={readOnly}
          onChange={(form) => setModal({ ...modal, form })}
          onClose={() => setModal(null)}
          onSubmit={() => void submit()}
          busy={busy === "save"}
          err={formErr}
        />
      )}
    </section>
  );
}
