"use client";

// 模型路由（2026-09-25 计划 §2.4/2.5，root 专属）：五车道 LLM 路由配置 + 档位预置。
// 红线：只有 root 能看/能配（admin/话务员不渲染——后端另有 403 兜底，这里只管入口面；
// 匿名单机形态 role=user，同样不渲染）。挂载点：设置页（app/(app)/settings/page.tsx）。
// 静态导出零动态段：预置名一律 encodeURIComponent 进 path/body（lib/api.ts 单点）。

import { useCallback, useEffect, useState } from "react";
import { api, apiBase, authHeaders, type ModelRoutingLaneConfig } from "@/lib/api";
import { ErrorState } from "@/components/app-shell";
import { useSession } from "@/components/session-context";
import { friendlyErrorText } from "@/lib/api-ready";

/** 五车道目录（键与 CP 契约逐字对齐；顺序=展示顺序）。 */
const LANES: [key: string, label: string, desc: string][] = [
  ["a_reply", "A线回复", "A 线通话回复主 LLM"],
  ["judge", "流程判定", "流程 judge + 意图判据"],
  ["mt", "同传翻译", "B 线同传翻译"],
  ["settle", "纪要结算", "纪要 / 蒸馏 / 润色"],
  ["mining", "挖掘聚类", "CP 挖掘 / 聚类 / 学习驾驶舱"],
];

/** 车道表单草稿：api_key 只进不出（读回恒掩码），has_api_key 只用于占位提示。 */
type LaneDraft = {
  provider: "local" | "openai";
  base_url: string;
  model: string;
  api_key: string;
  has_api_key: boolean;
  enable_thinking: boolean;
};

const EMPTY_LANE: LaneDraft = {
  provider: "local",
  base_url: "",
  model: "",
  api_key: "",
  has_api_key: false,
  enable_thinking: false,
};

function laneToDraft(l: ModelRoutingLaneConfig | undefined): LaneDraft {
  if (!l) return { ...EMPTY_LANE };
  return {
    provider: l.provider === "openai" ? "openai" : "local",
    base_url: String(l.base_url ?? ""),
    model: String(l.model ?? ""),
    api_key: "",
    has_api_key: Boolean(l.has_api_key),
    enable_thinking: Boolean(l.extra?.enable_thinking),
  };
}

/** PUT payload：api_key 空=保留旧值（CP 契约）。 */
function draftToPayload(d: LaneDraft) {
  return {
    provider: d.provider,
    base_url: d.base_url.trim(),
    model: d.model.trim(),
    api_key: d.api_key,
    extra: { enable_thinking: d.enable_thinking },
  };
}

type TestState = { busy: boolean; text: string; bad: boolean };

/** 车道中文名（结果面板提示用）。 */
function laneLabel(key: string): string {
  return LANES.find(([k]) => k === key)?.[1] ?? key;
}

/** 检测端点单条结果（GET /api/model-routing/detect 契约形状）。 */
type DetectEndpoint = { base_url: string; ok: boolean; models: string[]; error: string };

/** 结果面板每端点的填入选择（模型 + 目标车道），只在本地表单状态，不落库。 */
type FillSel = { model: string; lane: string };

const inputCls =
  "mt-1 w-full rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--live)";

/** 单车道小卡：本地/云端 toggle + 连接字段 + enable_thinking + 测连。 */
function LaneCard({
  label,
  desc,
  draft,
  test,
  onPatch,
  onTest,
}: {
  label: string;
  desc: string;
  draft: LaneDraft;
  test: TestState | undefined;
  onPatch: (p: Partial<LaneDraft>) => void;
  onTest: () => void;
}) {
  const cloud = draft.provider === "openai";
  return (
    <div className="rounded-lg border border-(--card-border) p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium">{label}</span>
        <select
          className="select px-1.5 py-0.5 text-[11px]"
          value={draft.provider}
          onChange={(e) => onPatch({ provider: e.target.value === "openai" ? "openai" : "local" })}
        >
          <option value="local">本地</option>
          <option value="openai">云端</option>
        </select>
      </div>
      <p className="mt-0.5 text-[11px] muted">{desc}</p>
      {label === "A线回复" && cloud && (
        <p className="mt-1.5 rounded-lg bg-amber-100 px-2 py-1 text-[11px] text-amber-700">
          云端档=通话内容数据出境
        </p>
      )}
      <label className="mt-2 block">
        <span className="text-[11px] muted">base_url</span>
        <input
          className={inputCls}
          placeholder={cloud ? "必填，如 https://api.deepseek.com/v1" : "留空=本地缺省端点"}
          value={draft.base_url}
          onChange={(e) => onPatch({ base_url: e.target.value })}
        />
      </label>
      <label className="mt-1 block">
        <span className="text-[11px] muted">model</span>
        <input
          className={inputCls}
          placeholder={cloud ? "必填，如 deepseek-chat" : "留空=本地缺省模型"}
          value={draft.model}
          onChange={(e) => onPatch({ model: e.target.value })}
        />
      </label>
      <label className="mt-1 block">
        <span className="text-[11px] muted">API Key{draft.has_api_key ? "（已配置）" : ""}</span>
        <input
          type="password"
          className={inputCls}
          placeholder={draft.has_api_key ? "已配置（留空保留）" : cloud ? "sk-…" : "仅云端档使用"}
          value={draft.api_key}
          onChange={(e) => onPatch({ api_key: e.target.value })}
        />
      </label>
      <label className="mt-1.5 flex items-center gap-1.5 text-[11px] muted">
        <input
          type="checkbox"
          className="accent-(--live)"
          checked={draft.enable_thinking}
          onChange={(e) => onPatch({ enable_thinking: e.target.checked })}
        />
        enable_thinking（Qwen 系思考开关，切云端建议核对）
      </label>
      <div className="mt-2 flex items-center gap-2">
        <button className="btn-ghost px-2 py-0.5 text-[11px]" disabled={test?.busy} title="按已保存配置发 max_tokens=1 探活" onClick={onTest}>
          {test?.busy ? "测连中…" : "测连"}
        </button>
        {test && !test.busy && test.text && (
          <span className={`text-[11px] ${test.bad ? "text-red-600" : "text-emerald-600"}`}>{test.text}</span>
        )}
      </div>
    </div>
  );
}

/** 「模型路由」卡（root 专属）：五车道配置 + 整卡保存 + 测连 + 档位预置条。 */
export default function ModelRoutingCard() {
  const session = useSession();
  const isRoot = session?.role === "root";
  const [lanes, setLanes] = useState<Record<string, LaneDraft> | null>(null);
  const [presets, setPresets] = useState<string[]>([]);
  const [presetSel, setPresetSel] = useState("");
  const [presetName, setPresetName] = useState("");
  const [loadErr, setLoadErr] = useState("");
  const [note, setNote] = useState<{ text: string; bad: boolean } | null>(null);
  const [saving, setSaving] = useState(false);
  const [presetBusy, setPresetBusy] = useState(false);
  const [tests, setTests] = useState<Record<string, TestState>>({});
  const [detectBusy, setDetectBusy] = useState(false);
  const [detectErr, setDetectErr] = useState("");
  const [detectResult, setDetectResult] = useState<DetectEndpoint[] | null>(null);
  const [fillSel, setFillSel] = useState<Record<string, FillSel>>({});

  const load = useCallback(async () => {
    try {
      const data = await api.getModelRouting();
      const next: Record<string, LaneDraft> = {};
      for (const [key] of LANES) next[key] = laneToDraft(data.lanes?.[key]);
      setLanes(next);
      const names = Object.keys(data.presets ?? {}).sort();
      setPresets(names);
      setPresetSel((cur) => (cur && names.includes(cur) ? cur : (names[0] ?? "")));
      setLoadErr("");
    } catch (e) {
      setLoadErr(String(e));
    }
  }, []);

  useEffect(() => {
    if (!isRoot) return;
    void load();
  }, [isRoot, load]);

  if (!isRoot) return null;

  const patchLane = (key: string, p: Partial<LaneDraft>) =>
    setLanes((cur) => (cur ? { ...cur, [key]: { ...cur[key], ...p } } : cur));

  const setTest = (key: string, s: TestState) =>
    setTests((cur) => ({ ...cur, [key]: s }));

  async function save() {
    if (!lanes) return;
    setSaving(true);
    setNote(null);
    try {
      const payload: Record<string, unknown> = {};
      for (const [key] of LANES) payload[key] = draftToPayload(lanes[key]);
      await api.saveModelRouting(payload);
      // 保存后重拉：has_api_key（新配 key）与 400 校验后的落库值以服务端为准。
      await load();
      setNote({ text: "已保存。路由改动对下一通通话生效；本地引擎内换 model 仍需重启对应本地服务。", bad: false });
    } catch (e) {
      setNote({ text: friendlyErrorText(String(e)), bad: true });
    } finally {
      setSaving(false);
    }
  }

  /** 检测本地端点：回读可达端点+模型清单（只读，绝不落库）。 */
  async function detectEndpoints() {
    setDetectBusy(true);
    setDetectErr("");
    try {
      const res = await fetch(`${apiBase()}/api/model-routing/detect`, { headers: authHeaders() });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const data = (await res.json()) as { endpoints?: DetectEndpoint[] };
      const eps = Array.isArray(data.endpoints) ? data.endpoints : [];
      setDetectResult(eps);
      // 每可达端点默认选第一个模型 + a_reply 车道（用户可改）。
      const sel: Record<string, FillSel> = {};
      for (const ep of eps) {
        if (ep.ok) sel[ep.base_url] = { model: ep.models[0] ?? "", lane: "a_reply" };
      }
      setFillSel(sel);
    } catch (e) {
      setDetectErr(friendlyErrorText(String(e)));
      setDetectResult(null);
    } finally {
      setDetectBusy(false);
    }
  }

  /** 把检测到的端点+模型填进目标车道的本地表单草稿（用户核对后再保存）。 */
  function fillLane(baseUrl: string) {
    const sel = fillSel[baseUrl];
    if (!sel) return;
    patchLane(sel.lane, { provider: "local", base_url: baseUrl, model: sel.model });
    setNote({
      text: `已填入「${laneLabel(sel.lane)}」：${baseUrl}${sel.model ? ` · ${sel.model}` : ""}。核对后点「保存」生效（检测不落库）。`,
      bad: false,
    });
  }

  async function testLane(key: string) {
    setTest(key, { busy: true, text: "", bad: false });
    try {
      const r = await api.testModelRouting(key);
      if (r.ok) {
        setTest(key, { busy: false, text: `✓ ${r.latency_ms}ms · ${r.model || "-"}`, bad: false });
      } else {
        setTest(key, { busy: false, text: r.error || "探活失败", bad: true });
      }
    } catch (e) {
      setTest(key, { busy: false, text: friendlyErrorText(String(e)), bad: true });
    }
  }

  async function applyPreset() {
    if (!presetSel) return;
    setPresetBusy(true);
    setNote(null);
    try {
      const r = await api.applyModelRoutingPreset(presetSel);
      // 套用=服务端整表覆盖；表单按响应 lanes 刷新（掩码 lanes，api_key 保留原位）。
      const applied = r.lanes ?? {};
      setLanes((cur) => {
        if (!cur) return cur;
        const next = { ...cur };
        for (const [key] of LANES) if (applied[key]) next[key] = laneToDraft(applied[key]);
        return next;
      });
      setNote({ text: `已套用预置「${presetSel}」：五车道整表覆盖，下一通通话生效。`, bad: false });
    } catch (e) {
      setNote({ text: friendlyErrorText(String(e)), bad: true });
    } finally {
      setPresetBusy(false);
    }
  }

  async function savePreset() {
    const name = presetName.trim();
    if (!name) {
      setNote({ text: "请先输入预置名称。", bad: true });
      return;
    }
    setPresetBusy(true);
    setNote(null);
    try {
      await api.createModelRoutingPreset(name);
      setPresetName("");
      await load();
      setPresetSel(name);
      setNote({ text: `已把当前五车道配置另存为预置「${name}」。`, bad: false });
    } catch (e) {
      setNote({ text: friendlyErrorText(String(e)), bad: true });
    } finally {
      setPresetBusy(false);
    }
  }

  async function deletePreset() {
    if (!presetSel) return;
    if (!window.confirm(`确认删除预置「${presetSel}」？`)) return;
    setPresetBusy(true);
    setNote(null);
    try {
      await api.deleteModelRoutingPreset(presetSel);
      await load();
      setNote({ text: `已删除预置「${presetSel}」。`, bad: false });
    } catch (e) {
      setNote({ text: friendlyErrorText(String(e)), bad: true });
    } finally {
      setPresetBusy(false);
    }
  }

  const reachable = detectResult?.filter((ep) => ep.ok) ?? [];
  const unreachable = detectResult?.filter((ep) => !ep.ok) ?? [];

  return (
    <section className="card">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <span className="label">模型路由</span>
          <p className="mt-1 text-xs muted">
            五车道 LLM 路由：本地=现状环境变量行为（字段留空即缺省），云端=OpenAI 兼容端点。
            改动下一通通话生效；本地引擎内换 model 仍需重启对应本地服务。
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            className="btn-ghost"
            disabled={detectBusy}
            title="并行探本地端点 /v1/models，一键把 base_url+模型填入车道（不落库）"
            onClick={() => void detectEndpoints()}
          >
            {detectBusy ? "检测中…" : "检测本地端点"}
          </button>
          <button className="btn-primary shrink-0" disabled={saving || !lanes} onClick={save}>
            {saving ? "保存中…" : "保存"}
          </button>
        </div>
      </div>

      {(detectBusy || detectErr || detectResult !== null) && (
        <div className="mt-3 rounded-lg border border-(--card-border) p-3">
          <span className="text-xs muted">本地端点检测</span>
          {detectBusy && <p className="mt-1.5 text-[11px] muted">检测中…</p>}
          {!detectBusy && detectErr && (
            <p className="mt-1.5 text-[11px] text-red-600">{detectErr}</p>
          )}
          {!detectBusy && !detectErr && detectResult !== null && reachable.length === 0 && (
            <p className="mt-1.5 text-[11px] muted">
              未发现可达的本地端点。
              {unreachable.length > 0 && ` 不可达: ${unreachable.map((ep) => ep.base_url).join("、")}`}
            </p>
          )}
          {!detectBusy && !detectErr && reachable.length > 0 && (
            <div className="mt-1.5 flex flex-col gap-1.5">
              {reachable.map((ep) => {
                const sel = fillSel[ep.base_url] ?? { model: "", lane: "a_reply" };
                return (
                  <div key={ep.base_url} className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-[11px] break-all">{ep.base_url}</span>
                    {ep.error === "auth" && <span className="text-[11px] muted">（需鉴权）</span>}
                    <select
                      className="select px-1.5 py-0.5 text-[11px]"
                      value={sel.model}
                      onChange={(e) =>
                        setFillSel((cur) => ({ ...cur, [ep.base_url]: { ...sel, model: e.target.value } }))
                      }
                    >
                      {ep.models.length === 0 && <option value="">（无模型清单）</option>}
                      {ep.models.map((m) => (
                        <option key={`${ep.base_url}:${m}`} value={m}>
                          {m}
                        </option>
                      ))}
                    </select>
                    <select
                      className="select px-1.5 py-0.5 text-[11px]"
                      value={sel.lane}
                      onChange={(e) =>
                        setFillSel((cur) => ({ ...cur, [ep.base_url]: { ...sel, lane: e.target.value } }))
                      }
                    >
                      {LANES.map(([k, l]) => (
                        <option key={k} value={k}>
                          {l}
                        </option>
                      ))}
                    </select>
                    <button
                      className="btn-ghost px-2 py-0.5 text-[11px]"
                      onClick={() => fillLane(ep.base_url)}
                    >
                      填入
                    </button>
                  </div>
                );
              })}
              {unreachable.length > 0 && (
                <p className="text-[11px] muted">
                  不可达: {unreachable.map((ep) => ep.base_url).join("、")}
                </p>
              )}
            </div>
          )}
        </div>
      )}

      {loadErr && <div className="mt-3"><ErrorState message={loadErr} /></div>}

      {lanes && (
        <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {LANES.map(([key, label, desc]) => (
            <LaneCard
              key={key}
              label={label}
              desc={desc}
              draft={lanes[key] ?? EMPTY_LANE}
              test={tests[key]}
              onPatch={(p) => patchLane(key, p)}
              onTest={() => void testLane(key)}
            />
          ))}
        </div>
      )}

      {note && (
        <p className={`mt-3 text-xs ${note.bad ? "text-red-600" : "text-emerald-600"}`}>{note.text}</p>
      )}

      <div className="mt-4 rounded-lg border border-(--card-border) p-3">
        <span className="text-xs muted">档位预置</span>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <select
            className="select text-xs"
            value={presetSel}
            disabled={presets.length === 0}
            onChange={(e) => setPresetSel(e.target.value)}
          >
            {presets.length === 0 && <option value="">（暂无预置）</option>}
            {presets.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
          <button className="btn-ghost text-xs" disabled={!presetSel || presetBusy} onClick={applyPreset}>
            套用
          </button>
          <button className="btn-ghost text-xs text-red-600" disabled={!presetSel || presetBusy} onClick={deletePreset}>
            删除
          </button>
          <input
            className="input w-40 text-xs"
            placeholder="预置名称（如：演示档）"
            value={presetName}
            onChange={(e) => setPresetName(e.target.value)}
          />
          <button className="btn-ghost text-xs" disabled={!presetName.trim() || presetBusy} onClick={savePreset}>
            另存为
          </button>
        </div>
        <p className="mt-1.5 text-[11px] muted">
          套用=五车道整表覆盖；预置只存路由值不含密钥（密钥保留原位，套档不清 key）。
        </p>
      </div>
    </section>
  );
}
