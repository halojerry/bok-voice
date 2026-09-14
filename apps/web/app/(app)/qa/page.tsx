"use client";

/**
 * 快答库（Q→A 检索快路）管理页：词条 CRUD。
 * 后端契约：apps/control-plane main.py /api/qa-entries（GET/POST/PATCH/DELETE）。
 * 运行时匹配（apps/agent qa_gate.py）：默认字面档 0.6×字面余弦 + 0.4×子串、阈值 0.90，只认字面写法；
 * 部署侧设 QA_EMBEDDING_MODEL（或 KB_EMBEDDING_MODEL）可切语义档：mlx embedding + 纯余弦，默认阈值 0.72。
 * 命中后播 tts-cache 预物化音频（没物化就不会命中）；数字/拒绝/收线/收号码步/推进轮全旁路。
 * 物化状态没有查询端点（唯一入口=人设保存自动触发 / scripts/pregen_tts.py 脚本），页面只做静态提示。
 */

import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { useAccount } from "@/components/account-context";

const LANGS = [
  ["zh", "普通话"],
  ["cantonese", "粤语"],
  ["en", "English"],
] as const;

function langLabel(v: string): string {
  return LANGS.find((l) => l[0] === v)?.[1] ?? v;
}

type Row = Record<string, unknown>;

/** 运营视角的「第 N 步」从 1 起；落库存 step_index = N-1（运行时 flow.current 是 0 基）。 */
const EMPTY_FORM = {
  question_text: "",
  answer_text: "",
  lang: "zh",
  scope: "global",
  step_no: 1,
  enabled: true,
};

const inputCls =
  "w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)";

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      className={`rounded-full px-2.5 py-1 text-[11px] ${
        active ? "bg-(--accent) font-medium text-(--accent-ink)" : "border border-(--card-border) text-(--stage-muted)"
      }`}
      onClick={onClick}
    >
      {children}
    </button>
  );
}

export default function QaPage() {
  const { accountId } = useAccount();
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);

  // 过滤器：语言 / 启停 / 作用域（客户端过滤，条目量级小）。
  const [fLang, setFLang] = useState<string>("");
  const [fEnabled, setFEnabled] = useState<string>(""); // "" 全部 | "1" 启用 | "0" 停用
  const [fScope, setFScope] = useState<string>(""); // "" 全部 | "global" | "step"

  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState(EMPTY_FORM);

  async function refresh() {
    setLoading(true);
    try {
      const data = await api.listAllQaEntries(accountId);
      setRows(Array.isArray(data) ? data : []);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId]);

  const filtered = useMemo(() => {
    return rows.filter((r) => {
      if (fLang && String(r.lang ?? "") !== fLang) return false;
      if (fEnabled && String(Number(Boolean(r.enabled))) !== fEnabled) return false;
      if (fScope && String(r.scope ?? "global") !== fScope) return false;
      return true;
    });
  }, [rows, fLang, fEnabled, fScope]);

  function edit(row: Row) {
    setEditingId(String(row.id ?? ""));
    const scope = String(row.scope ?? "global");
    setForm({
      question_text: String(row.question_text ?? ""),
      answer_text: String(row.answer_text ?? ""),
      lang: String(row.lang ?? "zh"),
      scope,
      step_no: Math.max(1, Number(row.step_index ?? 0) + 1),
      enabled: Boolean(row.enabled),
    });
    setOk(false);
  }

  function cancelEdit() {
    setEditingId(null);
    setForm(EMPTY_FORM);
    setErr(null);
  }

  async function save() {
    if (!form.question_text.trim() || !form.answer_text.trim()) {
      setErr("问法和答法都要填写。");
      return;
    }
    if (form.scope === "step" && (!Number.isFinite(form.step_no) || form.step_no < 1)) {
      setErr("作用域选了「仅限某一步」，请填写从 1 开始的步序号。");
      return;
    }
    setErr(null);
    setOk(false);
    const payload = {
      question_text: form.question_text.trim(),
      answer_text: form.answer_text.trim(),
      lang: form.lang,
      scope: form.scope,
      step_index: form.scope === "step" ? Math.round(form.step_no) - 1 : -1,
      enabled: form.enabled,
    };
    try {
      if (editingId) await api.updateQaEntry(editingId, payload);
      else await api.createQaEntry({ ...payload, account_id: accountId, source: "curated" });
      setEditingId(null);
      setForm(EMPTY_FORM);
      setOk(true);
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  }

  async function toggleEnabled(row: Row) {
    try {
      await api.updateQaEntry(String(row.id ?? ""), { enabled: !row.enabled });
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  }

  async function remove(id: string) {
    if (!window.confirm("确认删除该词条？删除后该问法将回落到大模型临场回答。")) return;
    try {
      await api.deleteQaEntry(id);
      if (editingId === id) cancelEdit();
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  }

  return (
    <div>
      <div className="mb-6">
        <h1 className="page-title">快答库</h1>
        <p className="page-sub">高频问答对 · 客户问法命中后跳过大模型，直接播预生成的回答音频</p>
      </div>

      {/* 页头说明卡：匹配现状——运营改词条前最需要的上下文 */}
      <div className="card mb-6">
        <span className="label">改词条前先读：快答怎么才会命中</span>
        <ul className="mt-2 space-y-2 text-xs leading-relaxed muted">
          <li>
            · <span className="text-(--foreground)">只认字面写法</span>：客户说的话要和问法写法高度一致（字面相似度 ≥ 0.90）才命中。
            同一个意思的不同说法（口语/简繁变体）请<span className="text-(--foreground)">一条一条分别添加</span>，改写说法不会自动匹配。
            （语义匹配已接线：部署侧配置 <span className="text-(--foreground)">QA_EMBEDDING_MODEL</span> 后按语义相似命中，阈值随之下调；
            未配置时就是上面的字面档——这行文字以字面档为准。）
          </li>
          <li>
            · <span className="text-(--foreground)">命中的效果</span>：跳过大模型，直接播预先合成好的回答音频（约 50ms 出声）。
            没命中也不亏——照常走大模型临场回答。
          </li>
          <li>
            · <span className="text-(--foreground)">新词条要先「物化」才会生效</span>：每条词条的回答音频要按人设音色预先合成进缓存，
            没物化的条目不会命中（等于白存）。保存人设（音色相关修改）时会自动物化；也可以手动跑
            <span className="text-(--foreground)"> python tools/bok.py tts-pregen --qa</span>。改了答法要重新物化。本页暂无法查询物化状态。
          </li>
          <li>
            · <span className="text-(--foreground)">这几类话不走快答（保护性旁路）</span>：客户报号码/话里带数字、明确拒绝、收线阶段、
            索取 WhatsApp/微信号的环节、流程推进轮——照常交给大模型和流程引擎处理，录词条时不用绕开这些场景。
          </li>
          <li>· 通话语言每通固定，只会匹配对应语言的词条；「仅限某一步」的条目只在话术推进到该步时参与匹配。</li>
        </ul>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_440px]">
        <section className="card">
          <div className="flex flex-wrap items-center gap-4">
            <span className="label">词条列表（{filtered.length}/{rows.length}）</span>
            <div className="flex flex-wrap items-center gap-1.5">
              <Chip active={fLang === ""} onClick={() => setFLang("")}>全部语言</Chip>
              {LANGS.map(([v, l]) => (
                <Chip key={v} active={fLang === v} onClick={() => setFLang(v)}>{l}</Chip>
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <Chip active={fEnabled === ""} onClick={() => setFEnabled("")}>全部状态</Chip>
              <Chip active={fEnabled === "1"} onClick={() => setFEnabled("1")}>启用</Chip>
              <Chip active={fEnabled === "0"} onClick={() => setFEnabled("0")}>停用</Chip>
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <Chip active={fScope === ""} onClick={() => setFScope("")}>全部作用域</Chip>
              <Chip active={fScope === "global"} onClick={() => setFScope("global")}>全局</Chip>
              <Chip active={fScope === "step"} onClick={() => setFScope("step")}>按步</Chip>
            </div>
          </div>

          {err && <ErrorState message={err} />}
          {loading ? (
            <LoadingState />
          ) : filtered.length === 0 ? (
            <EmptyState label={rows.length === 0 ? "暂无词条，请在右侧新建。" : "没有符合筛选条件的词条。"} />
          ) : (
            <div className="mt-3 space-y-3">
              {filtered.map((row) => {
                const id = String(row.id ?? "");
                const enabled = Boolean(row.enabled);
                const scope = String(row.scope ?? "global");
                const source = String(row.source ?? "curated");
                return (
                  <div key={id} className="rounded-lg bg-white/5 p-4">
                    <div className="flex items-start justify-between gap-3">
                      <p className="min-w-0 font-medium">{String(row.question_text ?? "-")}</p>
                      <div className="flex shrink-0 gap-2">
                        <button className="btn-ghost text-xs" onClick={() => edit(row)}>编辑</button>
                        <button className="btn-ghost text-xs text-red-300" onClick={() => remove(id)}>删除</button>
                      </div>
                    </div>
                    <p className="mt-1 line-clamp-3 text-xs muted">{String(row.answer_text ?? "")}</p>
                    <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px]">
                      <button
                        className="flex items-center gap-1.5 rounded-full border border-(--card-border) px-2 py-0.5"
                        onClick={() => toggleEnabled(row)}
                        title={enabled ? "点击停用（停用后不再参与匹配）" : "点击启用"}
                      >
                        <span className={`h-1.5 w-1.5 rounded-full ${enabled ? "bg-emerald-400" : "bg-neutral-500"}`} />
                        {enabled ? "启用中" : "已停用"}
                      </button>
                      <span className="rounded-full border border-(--card-border) px-2 py-0.5 text-(--stage-muted)">
                        {langLabel(String(row.lang ?? "zh"))}
                      </span>
                      <span
                        className={`rounded-full px-2 py-0.5 ${
                          source === "mined"
                            ? "border border-amber-400/40 text-amber-300"
                            : "border border-emerald-400/40 text-emerald-300"
                        }`}
                        title={source === "mined" ? "来自通话转写高频配对挖掘" : "运营手动添加"}
                      >
                        {source === "mined" ? "挖掘" : "精选"}
                      </span>
                      <span className="rounded-full border border-(--card-border) px-2 py-0.5 text-(--stage-muted)">
                        {scope === "step" ? `第 ${Number(row.step_index ?? 0) + 1} 步` : "全局"}
                      </span>
                      <span className="text-(--stage-muted)">命中 {Number(row.hit_count ?? 0)} 次</span>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section>

        <section className="card space-y-3">
          <span className="label">{editingId ? "编辑词条" : "新建词条"}</span>
          <label className="block">
            <span className="text-xs text-(--stage-muted)">问法（客户的原话写法）</span>
            <textarea
              className={`mt-1 h-16 resize-none ${inputCls}`}
              placeholder="如：好的，那怎么赔付？"
              value={form.question_text}
              onChange={(e) => setForm({ ...form, question_text: e.target.value })}
            />
            <span className="mt-1 block text-[11px] leading-relaxed muted">
              同一意思可加多条，字面匹配只认写法本身——客户换个说法就要再录一条。
            </span>
          </label>
          <label className="block">
            <span className="text-xs text-(--stage-muted)">答法（命中后念出的回答）</span>
            <textarea
              className={`mt-1 h-24 resize-none ${inputCls}`}
              placeholder="如：您放心，赔付会直接到您的微信钱包，一赔二，不用自己贴钱。"
              value={form.answer_text}
              onChange={(e) => setForm({ ...form, answer_text: e.target.value })}
            />
            <span className="mt-1 block text-[11px] leading-relaxed muted">
              命中后逐字念这段话，请写成完整、可直念的句子（不是给 AI 的要点）。
            </span>
          </label>
          <label className="block">
            <span className="text-xs text-(--stage-muted)">语言</span>
            <select
              className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm"
              value={form.lang}
              onChange={(e) => setForm({ ...form, lang: e.target.value })}
            >
              {LANGS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
          <label className="block">
            <span className="text-xs text-(--stage-muted)">作用域</span>
            <select
              className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm"
              value={form.scope}
              onChange={(e) => setForm({ ...form, scope: e.target.value })}
            >
              <option value="global">全局（任何对话步都可命中）</option>
              <option value="step">仅限某一步（话术推进到该步才匹配）</option>
            </select>
          </label>
          {form.scope === "step" && (
            <label className="block">
              <span className="text-xs text-(--stage-muted)">生效步序（第几步，从 1 开始）</span>
              <input
                type="number"
                min={1}
                className={`mt-1 ${inputCls}`}
                value={Number.isFinite(form.step_no) ? form.step_no : ""}
                onChange={(e) => setForm({ ...form, step_no: e.target.value === "" ? Number.NaN : Number(e.target.value) })}
              />
            </label>
          )}
          <label className="flex items-center gap-2 text-xs text-(--stage-muted)">
            <input
              type="checkbox"
              className="size-3.5 accent-(--accent)"
              checked={form.enabled}
              onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
            />
            启用（停用的条目保留在库里但不参与匹配）
          </label>
          <div className="flex items-center gap-3">
            <button className="btn-primary" onClick={save}>{editingId ? "保存修改" : "创建词条"}</button>
            {editingId && <button className="btn-ghost" onClick={cancelEdit}>取消</button>}
            {ok && <span className="text-sm text-emerald-400">已保存。记得重新物化音频，否则不会命中。</span>}
          </div>
        </section>
      </div>
    </div>
  );
}
