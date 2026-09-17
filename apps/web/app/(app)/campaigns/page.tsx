"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { LoadingState, EmptyState, ErrorState } from "@/components/app-shell";

type CallWindow = { days: number[]; start: string; end: string };
type Redispatch = { max_attempts?: number; interval_minutes?: number; on?: string[] };
type Campaign = {
  id: string; name: string; status: string; language: string; gap_seconds: number;
  template_id?: string; persona_id?: string;
  call_windows?: CallWindow[]; max_concurrency?: number; redispatch?: Redispatch;
  created_at: string; items?: Item[]; progress?: Progress;
};
type Progress = Record<string, number>;
type Item = { id: string; object_id: string; phone: string; status: string; call_id: string; scenario: string; attempts?: number };
type Obj = { id: string; display_name: string; phone?: string };
type Ref = { id: string; name?: string };

const STATUS_LABEL: Record<string, string> = {
  draft: "草稿", running: "进行中", paused: "已暂停", done: "已完成", stopped: "已停止",
};
const ITEM_LABEL: Record<string, string> = {
  pending: "待拨", dialing: "拨号中", in_call: "通话中", done: "完成",
  no_answer: "无人接", rejected: "拒接", failed: "失败", skipped: "跳过",
};

/** 进度条分段（顺序即堆叠顺序）：接通 / 在途 / 无人接 / 拒接 / 失败 / 跳过 / 待拨。 */
const SEGMENTS: { key: string; label: string; cls: string }[] = [
  { key: "done", label: "完成", cls: "bg-emerald-400" },
  { key: "in_call", label: "通话中", cls: "bg-sky-400" },
  { key: "dialing", label: "拨号中", cls: "bg-sky-300" },
  { key: "no_answer", label: "无人接", cls: "bg-neutral-400" },
  { key: "rejected", label: "拒接", cls: "bg-amber-400" },
  { key: "failed", label: "失败", cls: "bg-red-400" },
  { key: "skipped", label: "跳过", cls: "bg-neutral-600" },
  { key: "pending", label: "待拨", cls: "bg-white/20" },
];

const LANG_LABEL: Record<string, string> = { zh: "中文", cantonese: "粤语", en: "英语" };

/** mock 演练剧本（真 SIP 拨号忽略）：answer=正常接听 / no_answer=无人接 / reject=拒接 / hangup_mid=中途挂断。 */
const SCENARIOS = [
  { value: "", label: "默认（正常接听）" },
  { value: "answer", label: "正常接听" },
  { value: "no_answer", label: "无人接听" },
  { value: "reject", label: "拒接" },
  { value: "hangup_mid", label: "中途挂断" },
];

/** 外呼时段编辑（2026-09-17 调度三字段）：星期 ISO 1..7（1=周一），≤3 组；days 空行提交前过滤，非法项由服务端静默丢弃。 */
const MAX_WINDOWS = 3;
const WEEK_DAYS: { value: number; label: string }[] = [
  { value: 1, label: "周一" }, { value: 2, label: "周二" }, { value: 3, label: "周三" },
  { value: 4, label: "周四" }, { value: 5, label: "周五" }, { value: 6, label: "周六" }, { value: 7, label: "周日" },
];
const REDISPATCH_OUTCOMES: { value: string; label: string }[] = [
  { value: "no_answer", label: "未接听" },
  { value: "rejected", label: "拒接" },
  { value: "failed", label: "失败" },
];

const dayLabel = (d: number) => WEEK_DAYS.find((x) => x.value === d)?.label ?? String(d);

/** 时段摘要紧凑串：连续星期段「周一~周五」、非连续「/」分隔；多窗「 · 」相连。 */
function summarizeWindows(windows: CallWindow[]): string {
  if (!windows.length) return "";
  return windows
    .map((w) => {
      const days = [...w.days].sort((a, b) => a - b);
      const runs: number[][] = [];
      for (const d of days) {
        const last = runs[runs.length - 1];
        if (last && d === last[last.length - 1] + 1) last.push(d);
        else runs.push([d]);
      }
      const dayPart = runs
        .map((r) => (r.length === 1 ? dayLabel(r[0]) : `${dayLabel(r[0])}~${dayLabel(r[r.length - 1])}`))
        .join("/");
      return `${dayPart} ${w.start}-${w.end}`;
    })
    .join(" · ");
}

/** 时段/并发/重拨三组控件共享值（向导①步与详情编辑弹层同源同构）。 */
type SchedValue = {
  call_windows: CallWindow[];
  max_concurrency: number;
  redispatch_on: boolean;
  redispatch_max: number;
  redispatch_interval: number;
  redispatch_outcomes: string[];
};

const EMPTY_SCHED: SchedValue = {
  call_windows: [],
  max_concurrency: 1,
  redispatch_on: false,
  redispatch_max: 2,
  redispatch_interval: 30,
  redispatch_outcomes: ["no_answer"],
};

/** POST/PUT 提交体（同构）：call_windows 过滤 days 空行；并发钳 ≥0；重拨关=不传键。 */
function buildSchedBody(s: SchedValue): Record<string, unknown> {
  const body: Record<string, unknown> = {
    call_windows: s.call_windows
      .map((w) => ({ days: [...w.days].sort((a, b) => a - b), start: w.start, end: w.end }))
      .filter((w) => w.days.length > 0 && Boolean(w.start) && Boolean(w.end)),
    max_concurrency: Math.max(0, Math.floor(Number(s.max_concurrency) || 0)),
  };
  if (s.redispatch_on) {
    body.redispatch = {
      max_attempts: Math.min(5, Math.max(1, Math.floor(Number(s.redispatch_max) || 0))),
      interval_minutes: Math.max(1, Number(s.redispatch_interval) || 0),
      on: REDISPATCH_OUTCOMES.map((o) => o.value).filter((v) => s.redispatch_outcomes.includes(v)),
    };
  }
  return body;
}

type Form = SchedValue & {
  name: string; object_ids: string[]; template_id: string; persona_id: string;
  language: string; gap_seconds: number; site_id: string;
  scenarios: Record<string, string>;
  scripts: Record<string, string>;
  mock_speak_interval_s: number;
};

const EMPTY_FORM: Form = {
  name: "", object_ids: [], template_id: "", persona_id: "",
  language: "zh", gap_seconds: 5, site_id: "",
  scenarios: {}, scripts: {}, mock_speak_interval_s: 0,
  ...EMPTY_SCHED,
};

function ProgressBar({ progress }: { progress?: Progress }) {
  const total = progress?.total ?? 0;
  if (!total) return <div className="h-1.5 w-full rounded-full bg-white/10" />;
  return (
    <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-white/10">
      {SEGMENTS.map((s) => {
        const n = progress?.[s.key] ?? 0;
        if (!n) return null;
        return <div key={s.key} className={s.cls} style={{ width: `${(n / total) * 100}%` }} title={`${s.label} ${n}`} />;
      })}
    </div>
  );
}

function ProgressLegend({ progress }: { progress?: Progress }) {
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] muted">
      {SEGMENTS.map((s) => (
        <span key={s.key} className="inline-flex items-center gap-1">
          <span className={`h-2 w-2 rounded-sm ${s.cls}`} />
          {s.label} {progress?.[s.key] ?? 0}
        </span>
      ))}
    </div>
  );
}

/** 时段/并发/重拨编辑器：向导①步与详情「编辑配置」弹层共用（提交体都走 buildSchedBody）。 */
function SchedulingEditor({ value, onChange }: { value: SchedValue; onChange: (v: SchedValue) => void }) {
  const addWindow = () =>
    onChange({ ...value, call_windows: [...value.call_windows, { days: [1, 2, 3, 4, 5], start: "09:00", end: "18:00" }] });
  const removeWindow = (idx: number) =>
    onChange({ ...value, call_windows: value.call_windows.filter((_, i) => i !== idx) });
  const toggleWindowDay = (idx: number, day: number) =>
    onChange({
      ...value,
      call_windows: value.call_windows.map((w, i) => {
        if (i !== idx) return w;
        const days = w.days.includes(day)
          ? w.days.filter((d) => d !== day)
          : [...w.days, day].sort((a, b) => a - b);
        return { ...w, days };
      }),
    });
  const setWindowField = (idx: number, key: "start" | "end", v: string) =>
    onChange({ ...value, call_windows: value.call_windows.map((w, i) => (i === idx ? { ...w, [key]: v } : w)) });
  const toggleOutcome = (v: string) =>
    onChange({
      ...value,
      redispatch_outcomes: value.redispatch_outcomes.includes(v)
        ? value.redispatch_outcomes.filter((x) => x !== v)
        : [...value.redispatch_outcomes, v],
    });

  return (
    <div className="space-y-3">
      <div className="space-y-2">
        <span className="text-xs text-(--stage-muted)">外呼时段</span>
        {value.call_windows.map((w, idx) => (
          <div key={idx} className="flex flex-wrap items-center gap-2">
            <div className="flex flex-wrap items-center gap-1">
              {WEEK_DAYS.map((d) => (
                <label key={d.value} className="flex cursor-pointer items-center gap-1 rounded px-1 py-0.5 text-xs hover:bg-white/5">
                  <input type="checkbox" checked={w.days.includes(d.value)} onChange={() => toggleWindowDay(idx, d.value)} />
                  {d.label}
                </label>
              ))}
            </div>
            <input
              type="time"
              className="rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--accent)"
              value={w.start}
              onChange={(e) => setWindowField(idx, "start", e.target.value)}
            />
            <span className="text-xs muted">至</span>
            <input
              type="time"
              className="rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--accent)"
              value={w.end}
              onChange={(e) => setWindowField(idx, "end", e.target.value)}
            />
            <button className="btn-ghost text-xs" onClick={() => removeWindow(idx)}>删除</button>
          </div>
        ))}
        {value.call_windows.length === 0 && (
          <p className="text-xs muted">不设置 = 全天可拨；最多 {MAX_WINDOWS} 组。</p>
        )}
        {value.call_windows.length < MAX_WINDOWS && (
          <button className="btn-ghost text-xs" onClick={addWindow}>+ 添加时段</button>
        )}
      </div>

      <label className="block max-w-52">
        <span className="text-xs text-(--stage-muted)">最大并发</span>
        <input
          type="number"
          min={0}
          className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)"
          value={value.max_concurrency}
          onChange={(e) => onChange({ ...value, max_concurrency: Math.max(0, Math.floor(Number(e.target.value) || 0)) })}
        />
        <p className="mt-1 text-xs muted">0 = 不限制；默认 1 = 逐通串行。</p>
      </label>

      <div className="space-y-2">
        <label className="flex cursor-pointer items-center gap-2">
          <input
            type="checkbox"
            checked={value.redispatch_on}
            onChange={(e) => onChange({ ...value, redispatch_on: e.target.checked })}
          />
          <span className="text-xs text-(--stage-muted)">自动重拨</span>
          <span className="text-xs muted">未接通时按策略回队重拨</span>
        </label>
        {value.redispatch_on && (
          <div className="flex flex-wrap items-center gap-3 text-xs">
            <label className="flex items-center gap-1">
              最多
              <input
                type="number"
                min={1}
                max={5}
                className="w-16 rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--accent)"
                value={value.redispatch_max}
                onChange={(e) => onChange({ ...value, redispatch_max: Math.floor(Number(e.target.value) || 0) })}
              />
              次
            </label>
            <label className="flex items-center gap-1">
              间隔
              <input
                type="number"
                min={1}
                className="w-16 rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--accent)"
                value={value.redispatch_interval}
                onChange={(e) => onChange({ ...value, redispatch_interval: Number(e.target.value) || 0 })}
              />
              分钟
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <span className="muted">结果：</span>
              {REDISPATCH_OUTCOMES.map((o) => (
                <label key={o.value} className="flex cursor-pointer items-center gap-1">
                  <input type="checkbox" checked={value.redispatch_outcomes.includes(o.value)} onChange={() => toggleOutcome(o.value)} />
                  {o.label}
                </label>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** 三步向导：①基本信息 ②名单 ③演练设置与汇总。 */
function CampaignWizard({
  objects,
  refs,
  sites,
  form,
  setForm,
  onCancel,
  onCreated,
}: {
  objects: Obj[];
  refs: { templates: Ref[]; personas: Ref[] };
  sites: Ref[];
  form: Form;
  setForm: (f: Form) => void;
  onCancel: () => void;
  onCreated: () => Promise<void>;
}) {
  const [step, setStep] = useState(1);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const selectable = objects.filter((o) => o.phone);
  const filtered = useMemo(() => {
    const q = query.trim();
    if (!q) return selectable;
    return selectable.filter((o) => (o.display_name || "").includes(q) || (o.phone || "").includes(q));
  }, [objects, query]);
  const selectedSet = useMemo(() => new Set(form.object_ids), [form.object_ids]);
  const selectedObjects = selectable.filter((o) => selectedSet.has(o.id));

  const toggle = (id: string) =>
    setForm({
      ...form,
      object_ids: selectedSet.has(id) ? form.object_ids.filter((x) => x !== id) : [...form.object_ids, id],
    });
  const selectAllFiltered = () => {
    const next = new Set(form.object_ids);
    filtered.forEach((o) => next.add(o.id));
    setForm({ ...form, object_ids: Array.from(next) });
  };
  const clearAll = () => setForm({ ...form, object_ids: [] });

  const create = async () => {
    setBusy(true);
    setErr("");
    try {
      // scripts 只在有内容时下发：空串行会被 CP 清洗，但带键会存一个空数组。
      const scripts: Record<string, string[]> = {};
      for (const o of selectedObjects) {
        const raw = (form.scripts[o.id] || "").trim();
        if (raw) scripts[o.id] = raw.split("\n").map((s) => s.trim()).filter(Boolean);
      }
      const body: Record<string, unknown> = {
        name: form.name,
        object_ids: form.object_ids,
        template_id: form.template_id,
        persona_id: form.persona_id,
        language: form.language,
        gap_seconds: form.gap_seconds,
      };
      if (form.site_id) body.site_id = form.site_id;
      if (Object.keys(scripts).length) body.scripts = scripts;
      if (form.mock_speak_interval_s > 0) body.mock_speak_interval_s = form.mock_speak_interval_s;
      const scenarios = Object.fromEntries(Object.entries(form.scenarios).filter(([, v]) => v));
      if (Object.keys(scenarios).length) body.scenarios = scenarios;
      Object.assign(body, buildSchedBody(form));
      await api.createCampaign(body);
      await onCreated();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const canNext1 = Boolean(form.name.trim());
  const canCreate = canNext1 && form.object_ids.length > 0;

  return (
    <section className="card">
      <div className="mb-3 flex items-center justify-between">
        <span className="label">新建战役</span>
        <div className="flex items-center gap-2 text-xs muted">
          {[1, 2, 3].map((n) => (
            <span key={n} className={n === step ? "text-(--stage-value)" : ""}>
              {n === 1 ? "① 基本" : n === 2 ? "② 名单" : "③ 演练"}
            </span>
          ))}
        </div>
      </div>

      {step === 1 && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="block">
            <span className="text-xs text-(--stage-muted)">战役名称</span>
            <input
              className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="如：0901 老客回访"
            />
          </label>
          <label className="block">
            <span className="text-xs text-(--stage-muted)">通话语言</span>
            <select
              className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)"
              value={form.language}
              onChange={(e) => setForm({ ...form, language: e.target.value })}
            >
              <option value="zh">中文</option>
              <option value="cantonese">粤语</option>
              <option value="en">English</option>
            </select>
          </label>
          <label className="block">
            <span className="text-xs text-(--stage-muted)">话术</span>
            <select
              className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)"
              value={form.template_id}
              onChange={(e) => setForm({ ...form, template_id: e.target.value })}
            >
              <option value="">对象默认话术</option>
              {refs.templates.map((t) => <option key={t.id} value={t.id}>{t.name || t.id}</option>)}
            </select>
            <p className="mt-1 text-xs muted">选定后整波通话用该话术（建单即快照）；留空则各自用对象卡绑定的话术。</p>
          </label>
          <label className="block">
            <span className="text-xs text-(--stage-muted)">人设</span>
            <select
              className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)"
              value={form.persona_id}
              onChange={(e) => setForm({ ...form, persona_id: e.target.value })}
            >
              <option value="">默认人设</option>
              {refs.personas.map((p) => <option key={p.id} value={p.id}>{p.name || p.id}</option>)}
            </select>
          </label>
          <label className="block">
            <span className="text-xs text-(--stage-muted)">站点</span>
            <select
              className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)"
              value={form.site_id}
              onChange={(e) => setForm({ ...form, site_id: e.target.value })}
            >
              <option value="">默认站点（settings 单站点）</option>
              {sites.map((s) => <option key={s.id} value={s.id}>{s.name || s.id}</option>)}
            </select>
            <p className="mt-1 text-xs muted">挂站点后拨号 trunk 用站点注册值，未挂用 settings 兜底。</p>
          </label>
          <label className="block">
            <span className="text-xs text-(--stage-muted)">两通间隔（秒）</span>
            <input
              type="number"
              min={1}
              className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)"
              value={form.gap_seconds}
              onChange={(e) => setForm({ ...form, gap_seconds: Number(e.target.value) || 5 })}
            />
          </label>
          <div className="space-y-3 sm:col-span-2">
            <SchedulingEditor value={form} onChange={(v) => setForm({ ...form, ...v })} />
          </div>
        </div>
      )}

      {step === 2 && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <input
              className="min-w-40 flex-1 rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)"
              placeholder="搜对象名 / 电话"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <button className="btn-ghost text-xs" onClick={selectAllFiltered}>全选当前 {filtered.length}</button>
            <button className="btn-ghost text-xs" onClick={clearAll}>清空</button>
            <span className="text-xs muted">已选 {form.object_ids.length}</span>
          </div>
          <div className="max-h-64 space-y-1 overflow-auto rounded-lg border border-(--card-border) p-2">
            {filtered.length === 0 && <p className="p-2 text-xs muted">没有可拨对象（对象需在「对象」页填电话）。</p>}
            {filtered.map((o) => (
              <label key={o.id} className="flex cursor-pointer items-center gap-2 rounded px-1 py-1 text-sm hover:bg-white/5">
                <input type="checkbox" checked={selectedSet.has(o.id)} onChange={() => toggle(o.id)} />
                <span className="min-w-0 flex-1 truncate">{o.display_name || o.id}</span>
                <span className="font-mono text-xs muted">{o.phone}</span>
              </label>
            ))}
          </div>
        </div>
      )}

      {step === 3 && (
        <div className="space-y-3">
          <p className="text-xs muted">
            演练设置只对 mock 外呼（本机派生被叫）生效，真 SIP 拨号自动忽略；不填则被叫按语言默认接听。
          </p>
          <label className="block max-w-52">
            <span className="text-xs text-(--stage-muted)">被叫句间隔（秒，0=默认）</span>
            <input
              type="number"
              min={0}
              className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)"
              value={form.mock_speak_interval_s}
              onChange={(e) => setForm({ ...form, mock_speak_interval_s: Number(e.target.value) || 0 })}
            />
          </label>
          <div className="max-h-64 space-y-3 overflow-auto rounded-lg border border-(--card-border) p-3">
            {selectedObjects.map((o) => (
              <div key={o.id} className="space-y-1">
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate text-sm">{o.display_name || o.id}</span>
                  <select
                    className="rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--accent)"
                    value={form.scenarios[o.id] ?? ""}
                    onChange={(e) => setForm({ ...form, scenarios: { ...form.scenarios, [o.id]: e.target.value } })}
                  >
                    {SCENARIOS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
                  </select>
                </div>
                <textarea
                  rows={2}
                  className="w-full rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--accent)"
                  placeholder="被叫台词，每行一句（留空=默认台词）"
                  value={form.scripts[o.id] ?? ""}
                  onChange={(e) => setForm({ ...form, scripts: { ...form.scripts, [o.id]: e.target.value } })}
                />
              </div>
            ))}
          </div>
          <div className="rounded-lg bg-white/5 p-3 text-xs">
            <p>
              {form.name || "（未命名）"} · {LANG_LABEL[form.language] ?? form.language} ·{" "}
              {form.object_ids.length} 个对象 · 间隔 {form.gap_seconds}s
            </p>
            <p className="mt-1 muted">
              话术：{form.template_id ? refs.templates.find((t) => t.id === form.template_id)?.name || form.template_id : "对象默认"}；
              创建后为草稿，需在列表点「启动」开始拨号。
            </p>
          </div>
        </div>
      )}

      {err && <p className="mt-2 text-xs text-red-300">{err}</p>}

      <div className="mt-4 flex items-center gap-2">
        {step > 1 && <button className="btn-ghost text-xs" onClick={() => setStep(step - 1)}>上一步</button>}
        {step < 3 && (
          <button className="btn-primary text-xs" disabled={step === 1 ? !canNext1 : form.object_ids.length === 0}
                  onClick={() => setStep(step + 1)}>
            下一步
          </button>
        )}
        {step === 3 && (
          <button className="btn-primary text-xs" disabled={busy || !canCreate} onClick={() => void create()}>
            {busy ? "创建中…" : `创建战役（${form.object_ids.length} 对象）`}
          </button>
        )}
        <button className="btn-ghost text-xs" onClick={onCancel}>取消</button>
      </div>
    </section>
  );
}

export default function CampaignsPage() {
  const [list, setList] = useState<Campaign[] | null>(null);
  const [objects, setObjects] = useState<Obj[]>([]);
  const [refs, setRefs] = useState<{ templates: Ref[]; personas: Ref[] }>({ templates: [], personas: [] });
  const [sites, setSites] = useState<Ref[]>([]);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [form, setForm] = useState<Form>(EMPTY_FORM);
  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Campaign | null>(null);
  const [itemFilter, setItemFilter] = useState("");
  const [busyId, setBusyId] = useState("");
  const [err, setErr] = useState("");

  const reload = useCallback(async () => {
    try { setList(await api.listCampaigns() as Campaign[]); } catch { setList([]); }
  }, []);

  const loadDetail = useCallback(async (id: string) => {
    try {
      setDetail(await api.getCampaign(id) as Campaign);
    } catch (e) { setErr(String(e)); }
  }, []);

  useEffect(() => {
    void reload();
    void api.listObjects().then((o) => setObjects(o as Obj[])).catch(() => setObjects([]));
    void api.listTemplates().then((t) => setRefs((p) => ({ ...p, templates: t as Ref[] }))).catch(() => {});
    void api.listPersonas().then((p) => setRefs((prev) => ({ ...prev, personas: p as Ref[] }))).catch(() => {});
    void api.listSites().then((s) => setSites(s as Ref[])).catch(() => setSites([]));
  }, [reload]);

  // 深链：/campaigns?open=<id>（静态导出用 query，不开动态路由）。
  useEffect(() => {
    const m = window.location.search.match(/[?&]open=([^&]+)/);
    if (m) setOpenId(decodeURIComponent(m[1]));
  }, []);
  useEffect(() => {
    if (openId) void loadDetail(openId);
  }, [openId, loadDetail]);

  // 轮询：列表有 running 战役 或 详情战役在跑 → 3s 刷新（停栈/全终态时零请求）。
  useEffect(() => {
    const anyRunning = Boolean(list?.some((c) => c.status === "running")) || detail?.status === "running";
    if (!anyRunning) return;
    const t = setInterval(() => {
      void reload();
      if (openId) void loadDetail(openId);
    }, 3000);
    return () => clearInterval(t);
  }, [list, detail, openId, reload, loadDetail]);

  const act = async (id: string, label: string, fn: (id: string) => Promise<unknown>) => {
    setErr("");
    setBusyId(id + label);
    try {
      await fn(id);
      await reload();
      if (openId === id) await loadDetail(id);
    } catch (e) {
      setErr(String(e));
    } finally { setBusyId(""); }
  };

  const stopCampaign = (c: Campaign) => {
    if (!window.confirm(`确认停止「${c.name || c.id}」？停止是终态，不能再启动。`)) return;
    void act(c.id, "stop", api.stopCampaign);
  };
  const removeCampaign = (c: Campaign) => {
    if (!window.confirm(`确认删除「${c.name || c.id}」及其名单？此操作不可恢复。`)) return;
    void act(c.id, "delete", api.deleteCampaign);
  };

  // 编辑配置（draft/paused/stopped）：只改时段/并发/重拨三字段，running 由服务端 409 锁定。
  const [editOpen, setEditOpen] = useState(false);
  const [editSched, setEditSched] = useState<SchedValue>(EMPTY_SCHED);
  const [editBusy, setEditBusy] = useState(false);

  const openEdit = () => {
    if (!detail) return;
    const rd = detail.redispatch;
    const rawOn = rd?.on;
    const on: string[] = Array.isArray(rawOn)
      ? rawOn.filter((x) => REDISPATCH_OUTCOMES.some((o) => o.value === x))
      : [];
    const hasRd = Number(rd?.max_attempts ?? 0) > 0 && on.length > 0;
    setEditSched({
      call_windows: (detail.call_windows ?? []).map((w) => ({
        days: [...w.days],
        start: w.start || "09:00",
        end: w.end || "18:00",
      })),
      max_concurrency: Number(detail.max_concurrency ?? 1),
      redispatch_on: hasRd,
      redispatch_max: hasRd ? Number(rd?.max_attempts) : EMPTY_SCHED.redispatch_max,
      redispatch_interval: hasRd ? Number(rd?.interval_minutes) : EMPTY_SCHED.redispatch_interval,
      redispatch_outcomes: on.length ? [...on] : [...EMPTY_SCHED.redispatch_outcomes],
    });
    setErr("");
    setEditOpen(true);
  };

  const saveEdit = async () => {
    if (!openId) return;
    setEditBusy(true);
    setErr("");
    try {
      await api.updateCampaign(openId, buildSchedBody(editSched));
      setEditOpen(false);
      await loadDetail(openId);
      await reload();
    } catch (e) {
      const msg = String(e);
      // 409（running 锁定）与其它 4xx 都走页面既有错误提示；409 附中文指引。
      setErr(msg.includes("409") ? `运行中请先暂停（${msg}）` : msg);
    } finally {
      setEditBusy(false);
    }
  };

  const objName = (id: string) => objects.find((o) => o.id === id)?.display_name || id || "—";

  if (openId) {
    const items = (detail?.items ?? []).filter((it) => !itemFilter || it.status === itemFilter);
    const schedSummary = detail ? summarizeWindows(detail.call_windows ?? []) : "";
    const rdInfo = detail?.redispatch;
    const rdSummary = Number(rdInfo?.max_attempts ?? 0) > 0
      ? `${rdInfo?.max_attempts}次/${rdInfo?.interval_minutes}分`
      : "";
    return (
      <div className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h1 className="page-title">{detail?.name || openId}</h1>
            <p className="page-sub">
              {STATUS_LABEL[detail?.status ?? ""] ?? detail?.status ?? "加载中"} ·{" "}
              {LANG_LABEL[detail?.language ?? ""] ?? detail?.language} · 名单 {detail?.progress?.total ?? 0}
              {schedSummary ? <> · 时段 {schedSummary}</> : <> · 时段 不限</>}
              {" "}· 并发 {(detail?.max_concurrency ?? 1) === 0 ? "不限" : detail?.max_concurrency ?? 1}
              {rdSummary ? <> · 重拨 {rdSummary}</> : null}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {detail && detail.status !== "running" && (
              <button className="btn-ghost text-xs" disabled={editBusy} onClick={() => (editOpen ? setEditOpen(false) : openEdit())}>
                {editOpen ? "收起编辑" : "编辑配置"}
              </button>
            )}
            {detail?.status === "running" && <button className="btn-ghost text-xs" onClick={() => act(openId, "pause", api.pauseCampaign)}>暂停</button>}
            {detail?.status === "paused" && <button className="btn-primary text-xs" onClick={() => act(openId, "start", api.startCampaign)}>继续</button>}
            {(detail?.status === "running" || detail?.status === "paused") && (
              <button className="btn-ghost text-xs" onClick={() => detail && stopCampaign(detail)}>停止</button>
            )}
            <button className="btn-ghost text-xs" onClick={() => { setOpenId(null); setDetail(null); setItemFilter(""); setEditOpen(false); }}>← 返回列表</button>
          </div>
        </div>

        {err && <ErrorState message={err} />}

        <section className="card space-y-3">
          <ProgressBar progress={detail?.progress} />
          <ProgressLegend progress={detail?.progress} />
          <div className="flex items-center gap-2">
            <span className="text-xs muted">明细筛选</span>
            <select
              className="rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--accent)"
              value={itemFilter}
              onChange={(e) => setItemFilter(e.target.value)}
            >
              <option value="">全部（{detail?.items?.length ?? 0}）</option>
              {Object.entries(ITEM_LABEL).map(([k, v]) => (
                <option key={k} value={k}>{v}（{(detail?.items ?? []).filter((i) => i.status === k).length}）</option>
              ))}
            </select>
          </div>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left muted">
                <th className="py-1">对象</th><th>电话</th><th>状态</th><th>次</th><th>剧本</th><th>通话</th>
              </tr>
            </thead>
            <tbody>
              {items.map((it) => (
                <tr key={it.id} className="border-t border-(--card-border)">
                  <td className="py-1.5">{objName(it.object_id)}</td>
                  <td className="font-mono text-xs">{it.phone || "—"}</td>
                  <td>{ITEM_LABEL[it.status] || it.status}</td>
                  <td className="text-xs muted" title="已尝试拨打次数">{it.attempts ?? "—"}</td>
                  <td>{it.scenario || "—"}</td>
                  <td>
                    {it.call_id ? (
                      <Link className="text-accent underline" href={`/calls?call=${encodeURIComponent(it.call_id)}`}>
                        进入工作台
                      </Link>
                    ) : "—"}
                  </td>
                </tr>
              ))}
              {items.length === 0 && (
                <tr><td colSpan={6} className="py-3 text-xs muted">没有符合筛选的名单项。</td></tr>
              )}
            </tbody>
          </table>
        </section>

        {editOpen && (
          <section className="card space-y-3">
            <div className="flex items-center justify-between">
              <span className="label">编辑配置（时段 / 并发 / 重拨）</span>
              <button className="btn-ghost text-xs" onClick={() => setEditOpen(false)}>关闭</button>
            </div>
            <SchedulingEditor value={editSched} onChange={setEditSched} />
            <div className="flex items-center gap-2">
              <button className="btn-primary text-xs" disabled={editBusy} onClick={() => void saveEdit()}>
                {editBusy ? "保存中…" : "保存"}
              </button>
              <button className="btn-ghost text-xs" onClick={() => setEditOpen(false)}>取消</button>
              <span className="text-xs muted">进行中战役不可改（先暂停）；保存成功后立即刷新。</span>
            </div>
          </section>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h1 className="page-title">外呼战役</h1>
          <p className="page-sub">批量外呼：建波次 → 启动 → 盯进度；名单项可直达对应通话工作台</p>
        </div>
        <button className="btn-primary" onClick={() => { setForm(EMPTY_FORM); setWizardOpen(true); }}>+ 新建战役</button>
      </div>

      {err && <ErrorState message={err} />}

      {wizardOpen && (
        <CampaignWizard
          objects={objects}
          refs={refs}
          sites={sites}
          form={form}
          setForm={setForm}
          onCancel={() => setWizardOpen(false)}
          onCreated={async () => {
            // 只清「本波特有的」名称/名单/演练设置；话术/人设/语言/间隔是跨波复用意图。
            setForm({
              ...EMPTY_FORM,
              template_id: form.template_id, persona_id: form.persona_id,
              language: form.language, gap_seconds: form.gap_seconds,
            });
            setWizardOpen(false);
            await reload();
          }}
        />
      )}

      {list === null ? (
        <LoadingState />
      ) : list.length === 0 ? (
        <EmptyState label="暂无战役，点右上角「新建战役」开始。" />
      ) : (
        <div className="grid grid-cols-1 gap-3">
          {list.map((c) => {
            const busy = busyId.startsWith(c.id);
            return (
              <section key={c.id} className="card space-y-3">
                <div className="flex flex-wrap items-center gap-3">
                  <span className="font-medium">{c.name || c.id}</span>
                  <span className="text-xs muted">{STATUS_LABEL[c.status] || c.status}</span>
                  <span className="text-xs muted">
                    {LANG_LABEL[c.language] ?? c.language} · 名单 {c.progress?.total ?? 0} · 间隔 {c.gap_seconds}s
                  </span>
                  <span className="text-xs muted">创建 {String(c.created_at ?? "").slice(0, 10)}</span>
                  <div className="ml-auto flex items-center gap-2">
                    {c.status === "draft" && <button className="btn-primary text-xs" disabled={busy} onClick={() => void act(c.id, "start", api.startCampaign)}>启动</button>}
                    {c.status === "running" && <button className="btn-ghost text-xs" disabled={busy} onClick={() => void act(c.id, "pause", api.pauseCampaign)}>暂停</button>}
                    {c.status === "paused" && <button className="btn-primary text-xs" disabled={busy} onClick={() => void act(c.id, "start", api.startCampaign)}>继续</button>}
                    {(c.status === "running" || c.status === "paused") && (
                      <button className="btn-ghost text-xs" disabled={busy} onClick={() => stopCampaign(c)}>停止</button>
                    )}
                    {c.status !== "running" && (
                      <button className="btn-ghost text-xs text-red-300/80 hover:text-red-300" disabled={busy} onClick={() => removeCampaign(c)}>删除</button>
                    )}
                    <button className="btn-ghost text-xs" onClick={() => setOpenId(c.id)}>详情 →</button>
                  </div>
                </div>
                <ProgressBar progress={c.progress} />
                <ProgressLegend progress={c.progress} />
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}
