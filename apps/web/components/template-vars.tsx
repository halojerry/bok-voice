"use client";

// 变量 tab（W3 T2）：占位符目录 + 无效占位告警 + 对象预览。
// 渲染语义在 lib/var-panel.ts（零 import 纯函数,镜像 agent flow.py object_vars/
// render_template_text/step_say_text）；本组件只做取数与呈现。

import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { ErrorState, LoadingState } from "@/components/app-shell";
import {
  fourSectionsToSteps,
  jsonToSteps,
  LANGS,
  type TemplateRow,
} from "@/components/template-editor";
import {
  PLACEHOLDER_CATALOG,
  renderableLines,
  scanPlaceholders,
  type LegacySections,
  type PlaceholderHit,
  type VarFlowStep,
  type VarTransform,
} from "@/lib/var-panel";

const TRANSFORM_LABELS: Record<VarTransform, string> = {
  raw: "原文替换",
  digitsCn: "数字逐位转汉字（7890→七八九零）",
  digitsCnTail4: "单号末 4 位逐位汉字",
  asciiTail4: "末 4 位保留数字（英文念法）",
};

const langText = (raw: string) => LANGS.find((l) => l[0] === raw)?.[1] ?? raw;

export default function TemplateVarsTab({ tpl, accountId }: { tpl: TemplateRow; accountId: string }) {
  // ---- 模板步骤：steps_json 优先,空则旧四段转步骤（flow.py template_to_steps 同款） ----
  const steps: VarFlowStep[] = useMemo(
    () => {
      const parsed = jsonToSteps(tpl.steps_json);
      if (parsed.length > 0) return parsed;
      return fourSectionsToSteps({
        opening: tpl.opening ?? "",
        core: tpl.core ?? "",
        objection: tpl.objection ?? "",
        closing: tpl.closing ?? "",
      });
    },
    [tpl.steps_json, tpl.opening, tpl.core, tpl.objection, tpl.closing],
  );
  const legacy: LegacySections = useMemo(
    () => ({
      opening: tpl.opening,
      core: tpl.core,
      objection: tpl.objection,
      closing: tpl.closing,
    }),
    [tpl.opening, tpl.core, tpl.objection, tpl.closing],
  );

  // ---- 占位符扫描：目录命中 + 无效占位（steps 空时纯函数内扫 legacy 四段） ----
  const hits: PlaceholderHit[] = useMemo(
    () => scanPlaceholders(steps.length > 0 ? steps : [], legacy),
    [steps, legacy],
  );
  const unknownHits = hits.filter((h) => h.unknown);
  const countForKey = (keys: string[]) =>
    hits
      .filter((h) => keys.includes(h.key.trim()))
      .reduce((sum, h) => sum + h.count, 0);

  // ---- 对象选择器（默认首对象,CallStudio objs[0] 先例）+ 预览 ----
  const [objects, setObjects] = useState<Record<string, unknown>[]>([]);
  const [objectsErr, setObjectsErr] = useState("");
  const [objectsLoading, setObjectsLoading] = useState(true);
  const [selectedId, setSelectedId] = useState("");
  const [previewId, setPreviewId] = useState("");

  useEffect(() => {
    let alive = true;
    setObjectsLoading(true);
    api.listObjects(accountId)
      .then((data) => {
        if (!alive) return;
        const list = Array.isArray(data) ? data : [];
        setObjects(list);
        setObjectsErr("");
        // 默认选首对象（同 CallStudio：列表就绪且无既有选择时 objs[0]）。
        if (list.length > 0) setSelectedId((prev) => prev || String(list[0].id));
      })
      .catch((e) => {
        if (alive) setObjectsErr(String(e));
      })
      .finally(() => {
        if (alive) setObjectsLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [accountId]);

  const previewObj = useMemo(
    () => objects.find((o) => String(o.id ?? "") === previewId) ?? null,
    [objects, previewId],
  );
  const lines = useMemo(
    () => (previewObj ? renderableLines(steps, previewObj) : []),
    [steps, previewObj],
  );
  const objLang = String(previewObj?.language ?? "").trim();
  const tplLang = String(tpl.language ?? "zh");
  const langMatch = objLang !== "" && objLang === tplLang;

  return (
    <section className="card space-y-4">
      {/* ① 占位符目录表 */}
      <div className="rounded-lg border border-(--card-border) p-3">
        <span className="label">占位符目录</span>
        <p className="mt-1 text-[11px] muted">
          占位符写在目标或参考说法里，通话装配时按对象替换；对象缺该字段时保留原占位
          （AI 会向客户询问，不会编造）。
        </p>
        <div className="mt-2 space-y-1">
          {PLACEHOLDER_CATALOG.map((entry) => {
            const used = countForKey(entry.keys);
            return (
              <div
                key={entry.column + entry.keys[0]}
                className={`flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg px-2 py-1.5 text-xs ${
                  used > 0 ? "bg-muted/60" : "muted opacity-60"
                }`}
              >
                <span className="font-mono">
                  {entry.keys.map((k, i) => (
                    <span key={k}>
                      {i > 0 && " / "}
                      {`{${k}}`}
                    </span>
                  ))}
                </span>
                <span>← 对象字段 {entry.column}</span>
                <span className="muted">{TRANSFORM_LABELS[entry.transform]}</span>
                {entry.channelDefault && (
                  <span className="muted">（对象未指定时：中文→微信，其余→WhatsApp）</span>
                )}
                <span className="ml-auto shrink-0">
                  {used > 0 ? (
                    <span className="rounded-sm bg-(--live-soft) px-1.5 py-0.5 text-[10px] text-(--live-ink)">
                      本模板使用 {used} 处
                    </span>
                  ) : (
                    <span className="rounded-sm bg-muted px-1.5 py-0.5 text-[10px]">未使用</span>
                  )}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {/* ② 无效占位符告警 */}
      {unknownHits.length > 0 && (
        <div className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">
          ⚠ 发现 {unknownHits.length} 个无效占位符（不在目录内，通话中会原样保留不被替换）：
          {unknownHits.map((h) => (
            <span key={h.key} className="font-mono">
              {`{${h.key}}`}
              {h.stepIdxs.length > 0 && `（第 ${h.stepIdxs.map((i) => i + 1).join("、")} 步）`}
              {" "}
            </span>
          ))}
        </div>
      )}
      {steps.length === 0 && (
        <p className="text-xs muted">该模板暂无步骤内容，先到「话术流程」tab 配置话术。</p>
      )}

      {/* ③ 对象预览 */}
      <div className="rounded-lg border border-(--card-border) p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="label">对象预览</span>
          <div className="flex items-center gap-2">
            {objectsLoading ? (
              <LoadingState />
            ) : (
              <select
                className="rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs"
                value={selectedId}
                onChange={(e) => setSelectedId(e.target.value)}
              >
                {objects.length === 0 && <option value="">（暂无对象）</option>}
                {objects.map((o) => (
                  <option key={String(o.id ?? "")} value={String(o.id ?? "")}>
                    {String(o.display_name ?? o.id ?? "-")}
                  </option>
                ))}
              </select>
            )}
            <button
              className="btn-ghost text-xs"
              disabled={!selectedId || selectedId === previewId}
              onClick={() => setPreviewId(selectedId)}
            >
              用该对象预览
            </button>
          </div>
        </div>
        {objectsErr && <div className="mt-2"><ErrorState message={objectsErr} /></div>}
        {!objectsErr && objects.length === 0 && !objectsLoading && (
          <p className="mt-2 text-xs muted">账号下暂无对象，请先到「对象」页新建。</p>
        )}
        {!previewObj && objects.length > 0 && (
          <p className="mt-2 text-xs muted">选择对象后点「用该对象预览」，查看开场白与直念步的实际念出效果。</p>
        )}
        {previewObj && (
          <div className="mt-2 space-y-2">
            <p className="flex flex-wrap items-center gap-2 text-xs">
              <span className="rounded-sm bg-muted px-1.5 py-0.5 text-[10px]">
                模板语言：{langText(tplLang)}
              </span>
              <span className="rounded-sm bg-muted px-1.5 py-0.5 text-[10px]">
                对象语言：{objLang ? langText(objLang) : "（未设置）"}
              </span>
              {langMatch ? (
                <span className="rounded-sm bg-emerald-100 px-1.5 py-0.5 text-[10px] text-emerald-700">
                  语言匹配 · 开场白可直念
                </span>
              ) : (
                <span className="rounded-sm bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-700">
                  模板语言≠对象语言 · 开场白回退通用语（不直念本模板开场）
                </span>
              )}
            </p>
            {lines.length === 0 ? (
              <p className="text-xs muted">该模板没有开场白行或直念步可预览。</p>
            ) : (
              <div className="space-y-2">
                {lines.map((l) => (
                  <div
                    key={`${l.stepIdx}:${l.kind}`}
                    className={`rounded-lg p-2 text-xs ${l.dropped ? "bg-red-50" : "bg-muted/60"}`}
                  >
                    <p className="flex flex-wrap items-center gap-2 muted">
                      <span className="rounded-sm bg-background px-1.5 py-0.5 text-[10px]">
                        第 {l.stepIdx + 1} 步 · {l.kind === "opening" ? "开场白" : "直念步"}
                      </span>
                      {l.dropped && (
                        <span className="rounded-sm bg-red-100 px-1.5 py-0.5 text-[10px] text-red-700">
                          ⚠ 该行含缺失变量，通话中会跳过/回退通用开场白
                        </span>
                      )}
                    </p>
                    <p className="mt-1 muted">原文：{l.raw}</p>
                    <p className={l.dropped ? "mt-0.5 text-red-700" : "mt-0.5"}>念出：{l.rendered}</p>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
