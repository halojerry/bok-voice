"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { LoadingState, EmptyState } from "@/components/app-shell";

type Campaign = {
  id: string; name: string; status: string; language: string; gap_seconds: number;
  created_at: string; items?: Item[]; progress?: Record<string, number>;
};
type Item = { id: string; object_id: string; phone: string; status: string; call_id: string; scenario: string };
type Obj = { id: string; display_name: string; phone: string };
type Ref = { id: string; name: string };

const STATUS_LABEL: Record<string, string> = {
  draft: "草稿", running: "进行中", paused: "已暂停", done: "已完成", stopped: "已停止",
};
const ITEM_LABEL: Record<string, string> = {
  pending: "待拨", dialing: "拨号中", in_call: "通话中", done: "完成",
  no_answer: "无人接", rejected: "拒接", failed: "失败", skipped: "跳过",
};

const EMPTY_FORM = {
  name: "", object_ids: [] as string[], template_id: "", persona_id: "",
  language: "zh", gap_seconds: 5,
};

export default function CampaignsPage() {
  const [list, setList] = useState<Campaign[] | null>(null);
  const [detail, setDetail] = useState<Record<string, Campaign>>({});
  const [objects, setObjects] = useState<Obj[]>([]);
  const [refs, setRefs] = useState<{ templates: Ref[]; personas: Ref[] }>({ templates: [], personas: [] });
  const [form, setForm] = useState(EMPTY_FORM);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const reload = useCallback(async () => {
    try { setList(await api.listCampaigns() as Campaign[]); } catch { setList([]); }
  }, []);

  useEffect(() => {
    void reload();
    void api.listObjects().then((o) => setObjects(o as Obj[])).catch(() => setObjects([]));
    // 下拉取数照 objects 页姿势（listTemplates/listPersonas 都只需 account_id 缺省）。
    void api.listTemplates().then((t) => setRefs((p) => ({ ...p, templates: t as Ref[] }))).catch(() => {});
    void api.listPersonas().then((p) => setRefs((prev) => ({ ...prev, personas: p as Ref[] }))).catch(() => {});
  }, [reload]);

  // 已展开详情的轮询：只对展开过的战役续拉（列表本身不带 items）。
  // running 门控：无 running 战役（null/全 draft/done/stopped）时整轮 skip，停栈态不再打 CP。
  useEffect(() => {
    const ids = Object.keys(detail);
    const t = setInterval(() => {
      if (!list?.some((c) => c.status === "running")) return;
      void reload();
      for (const id of ids) {
        if (detail[id]?.status !== "running") continue;
        void api.getCampaign(id).then((d) =>
          setDetail((prev) => ({ ...prev, [id]: d as Campaign }))).catch(() => {});
      }
    }, 3000);
    return () => clearInterval(t);
  }, [reload, detail, list]);

  const create = async () => {
    setBusy(true);
    setErr("");
    try {
      await api.createCampaign(form);
      // 只清「本波特有的」名称与名单；话术/人设/语言/间隔是跨波复用意图，
      // 尤其 language——连建粤语波回落 zh 会静默误拨错语言。
      setForm({
        ...EMPTY_FORM,
        template_id: form.template_id, persona_id: form.persona_id,
        language: form.language, gap_seconds: form.gap_seconds,
      });
      await reload();
    } catch (e) {
      setErr(String(e));
    } finally { setBusy(false); }
  };

  const act = async (id: string, fn: (id: string) => Promise<unknown>) => {
    setErr("");
    try {
      await fn(id);
      await reload();
    } catch (e) {
      // 非法迁移(409)等错误面：保留上次列表，仅提示。
      setErr(String(e));
    }
  };

  const openDetail = async (id: string) => {
    try {
      const d = await api.getCampaign(id) as Campaign;
      setDetail((p) => ({ ...p, [id]: d }));
    } catch (e) { setErr(String(e)); }
  };

  const toggleObj = (id: string) => setForm((f) => ({
    ...f,
    object_ids: f.object_ids.includes(id) ? f.object_ids.filter((x) => x !== id) : [...f.object_ids, id],
  }));

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-medium">外呼战役</h1>
      <section className="space-y-2 rounded border p-4">
        <div className="flex flex-wrap items-center gap-2">
          <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
                 placeholder="战役名称" className="rounded border px-2 py-1 text-sm" />
          <select value={form.template_id} onChange={(e) => setForm({ ...form, template_id: e.target.value })}
                  className="rounded border px-2 py-1 text-sm">
            <option value="">默认话术</option>
            {refs.templates.map((t) => <option key={t.id} value={t.id}>{t.name || t.id}</option>)}
          </select>
          <select value={form.persona_id} onChange={(e) => setForm({ ...form, persona_id: e.target.value })}
                  className="rounded border px-2 py-1 text-sm">
            <option value="">默认人设</option>
            {refs.personas.map((p) => <option key={p.id} value={p.id}>{p.name || p.id}</option>)}
          </select>
          <select value={form.language} onChange={(e) => setForm({ ...form, language: e.target.value })} className="rounded border px-2 py-1 text-sm">
            <option value="zh">中文</option><option value="cantonese">粤语</option><option value="en">English</option>
          </select>
          <input type="number" min={1} value={form.gap_seconds}
                 onChange={(e) => setForm({ ...form, gap_seconds: Number(e.target.value) || 5 })}
                 className="w-20 rounded border px-2 py-1 text-sm" />
          <span className="text-xs muted">间隔秒</span>
          <button disabled={busy || !form.object_ids.length || !form.name} onClick={() => void create()}
                  className="rounded border px-3 py-1 text-sm disabled:opacity-50">创建战役（{form.object_ids.length} 对象）</button>
        </div>
        <div className="max-h-48 overflow-auto text-sm">
          {objects.map((o) => (
            <label key={o.id} className={`mr-3 inline-flex items-center gap-1 ${o.phone ? "" : "opacity-40"}`}>
              <input type="checkbox" disabled={!o.phone} checked={form.object_ids.includes(o.id)}
                     onChange={() => toggleObj(o.id)} />
              {o.display_name}{o.phone ? `（${o.phone}）` : "（无电话）"}
            </label>
          ))}
        </div>
        {err && <p className="text-xs text-red-600">{err}</p>}
      </section>
      {list === null ? <LoadingState /> : list.length === 0 ? <EmptyState label="暂无战役" /> : list.map((c) => (
        <section key={c.id} className="space-y-2 rounded border p-4">
          <div className="flex flex-wrap items-center gap-3">
            <span className="font-medium">{c.name || c.id}</span>
            <span className="text-xs muted">{STATUS_LABEL[c.status] || c.status}</span>
            <span className="text-xs muted">
              {c.progress?.answered ?? 0}/{c.progress?.total ?? 0} 有结果
            </span>
            {c.status === "draft" && <button onClick={() => void act(c.id, api.startCampaign)} className="rounded border px-2 py-0.5 text-sm">启动</button>}
            {c.status === "running" && <button onClick={() => void act(c.id, api.pauseCampaign)} className="rounded border px-2 py-0.5 text-sm">暂停</button>}
            {c.status === "paused" && <button onClick={() => void act(c.id, api.startCampaign)} className="rounded border px-2 py-0.5 text-sm">继续</button>}
            {(c.status === "running" || c.status === "paused") && <button onClick={() => void act(c.id, api.stopCampaign)} className="rounded border px-2 py-0.5 text-sm">停止</button>}
            <button onClick={() => void openDetail(c.id)}
                    className="rounded border px-2 py-0.5 text-sm">详情</button>
          </div>
          {detail[c.id]?.items && (
            <table className="w-full text-sm">
              <thead><tr className="text-left muted"><th className="py-1">对象</th><th>电话</th><th>状态</th><th>剧本</th><th>通话</th></tr></thead>
              <tbody>
                {detail[c.id].items!.map((it) => (
                  <tr key={it.id} className="border-t">
                    <td className="py-1.5">{objects.find((o) => o.id === it.object_id)?.display_name || it.object_id || "—"}</td>
                    <td className="font-mono">{it.phone || "—"}</td>
                    <td>{ITEM_LABEL[it.status] || it.status}</td>
                    <td>{it.scenario || "—"}</td>
                    <td>{it.call_id ? <Link className="underline" href="/calls">{it.call_id}</Link> : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      ))}
    </div>
  );
}
