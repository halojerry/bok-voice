"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { ErrorState, LoadingState } from "@/components/app-shell";
import CannedAuditionCard from "@/components/canned-audition";
import { SETTING_CARDS, POLICY_META, DEFAULT_PROVIDER, type ProviderKind, type FieldMeta } from "@/lib/settings-meta";
import { previewLangForVoice } from "@/lib/minimax-voices";
import { startRecording, type RecorderHandle } from "@/lib/recorder";
import {
  listAudioDevicesOf,
  requestMicPermission,
  savedMicDevice,
  savedOutputDevice,
  saveMicDevice,
  saveOutputDevice,
  webCanSwitchOutput,
  type AudioDeviceInfo,
} from "@/lib/audio";
import { friendlyErrorText } from "@/lib/api-ready";

type ProviderForm = Record<string, unknown> & { provider?: string };

const EMPTY_FORM: Record<ProviderKind, ProviderForm> & { policy: string } & { sip: ProviderForm } = {
  asr: { provider: DEFAULT_PROVIDER.asr, language_mode: "auto", language: "" },
  llm: { provider: DEFAULT_PROVIDER.llm, local_model: "" },
  tts: { provider: DEFAULT_PROVIDER.tts, voice_mode: "single", speaker: "", sample_rate: 24000, minimax_clones_json: "[]" },
  vad: { provider: DEFAULT_PROVIDER.vad, max_buffered_speech: 15, min_speech_duration: 0.15, min_silence_duration: 0.45, sensitivity: 0.75, interruption: true },
  // 外呼（SIP）段：与 business-db default_settings()["sip"] 逐键同形。
  sip: { mode: "mock", trunk_id: "", address: "", auth_username: "", auth_password: "", numbers: [], ringing_timeout_s: 30, max_call_duration_s: 600 },
  policy: "offline_first",
};

function FieldInput({
  field,
  value,
  onChange,
}: {
  field: FieldMeta;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const base =
    "w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)";
  if (field.type === "select") {
    const options = field.options ?? [];
    const isBool = options.some((o) => o.value === "true" || o.value === "false");
    const raw = value === undefined || value === null ? (isBool ? "true" : "") : String(value);
    return (
      <select
        className={`mt-1 ${base}`}
        value={raw}
        onChange={(e) => {
          const v = e.target.value;
          onChange(isBool ? v === "true" : v);
        }}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
        {raw && !options.some((o) => o.value === raw) && (
          <option key={`custom-${raw}`} value={raw}>
            自定义：{raw}
          </option>
        )}
      </select>
    );
  }
  return (
    <input
      type={field.type === "number" ? "number" : field.type === "secret" ? "password" : "text"}
      className={`mt-1 ${base}`}
      placeholder={field.placeholder ?? field.key}
      value={value === undefined || value === null ? "" : String(value)}
      min={field.min}
      max={field.max}
      step={field.step}
      onChange={(e) => onChange(field.type === "number" ? Number(e.target.value) : e.target.value)}
    />
  );
}

function ProviderCard({
  kind,
  value,
  onChange,
}: {
  kind: ProviderKind;
  value: ProviderForm;
  onChange: (next: ProviderForm) => void;
}) {
  const meta = SETTING_CARDS.find((c) => c.kind === kind)!;
  const provider = String(value.provider ?? DEFAULT_PROVIDER[kind]);
  const providerMeta = meta.providers.find((p) => p.value === provider);
  return (
    <section className="card">
      <span className="label">{meta.title}</span>
      <p className="mt-1 text-xs muted">{meta.desc}</p>
      <div className="mt-3 space-y-2">
        <div>
          <select
            className="w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)"
            value={provider}
            onChange={(e) => onChange({ ...value, provider: e.target.value })}
          >
            {meta.providers.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
          {providerMeta?.hint && <p className="mt-1 text-xs muted">{providerMeta.hint}</p>}
        </div>
        {kind === "llm" && (provider === "local_openai" || provider === "mlx") && value.local_model ? (
          <p className="rounded-lg bg-muted/60 p-2 text-[11px] muted">
            本地模型切换需<b className="text-(--foreground)">重启本地服务</b>生效（`bok serve` 或点「本机桌面服务」重启）；重启后通话与蒸馏都用所选模型。
          </p>
        ) : null}
        {meta.fields.filter((f) => !f.advanced && (!f.providers || f.providers.includes(provider))).map((field) => (
          <label key={field.key} className="block">
            <span className="text-xs text-(--stage-muted)">{field.label}</span>
            <FieldInput field={field} value={value[field.key]} onChange={(v) => onChange({ ...value, [field.key]: v })} />
            {field.hint && <p className="mt-1 text-xs muted">{field.hint}</p>}
            {field.preview && kind === "tts" && (
              <VoicePreview provider={provider} fieldKey={field.key} voice={String(value[field.key] ?? "")} />
            )}
          </label>
        ))}
        {meta.fields.some((f) => f.advanced) && (
          <details className="rounded-lg border border-(--card-border) p-2 text-sm">
            <summary className="cursor-pointer text-xs muted hover:text-accent">
              高级（旧按语言分音色，仅兼容旧数据）
            </summary>
            <div className="mt-2 space-y-2">
              {meta.fields.filter((f) => f.advanced && (!f.providers || f.providers.includes(provider))).map((field) => (
                <label key={field.key} className="block">
                  <span className="text-xs text-(--stage-muted)">{field.label}</span>
                  <FieldInput field={field} value={value[field.key]} onChange={(v) => onChange({ ...value, [field.key]: v })} />
                  {field.hint && <p className="mt-1 text-xs muted">{field.hint}</p>}
                  {field.preview && kind === "tts" && (
                    <VoicePreview provider={provider} fieldKey={field.key} voice={String(value[field.key] ?? "")} />
                  )}
                </label>
              ))}
            </div>
          </details>
        )}
      </div>
    </section>
  );
}

/** 音色字段的「试听」按钮：调 /api/tts/preview 播放当前选中音色。 */
function VoicePreview({ provider, fieldKey, voice }: { provider: string; fieldKey: string; voice: string }) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  if (!voice) return null;
  async function play() {
    if (!voice) return;
    setBusy(true);
    setErr("");
    try {
      // 云端音色：试听语言按音色 ID 判定（Cantonese_*→粤语示例），否则粤语音色会被用来
      // 念普通话文字 → 广式普通话。本地 Qwen3（serena/vivian…）仍是多语本地音色，按字段语言。
      const isCloud = provider === "minimax" || provider === "minimax_streaming" || provider === "volcano_streaming";
      const lang = isCloud ? previewLangForVoice(voice) : fieldKey === "speaker_cantonese" ? "cantonese" : fieldKey === "speaker_en" ? "en" : "zh";
      const text =
        lang === "cantonese"
          ? "你好，我係想問下件貨而家到咗邊度？唔該幫我 check 下 status 呀。"
          : lang === "en"
            ? "Hello, I'd like to ask about your delivery."
            : "你好，我想了解一下你们的产品和服务。";
      const blob = await api.previewTts({ provider, text, voice, language: lang, sample_rate: 24000 });
      if (url) URL.revokeObjectURL(url);
      const u = URL.createObjectURL(blob);
      setUrl(u);
      const el = new Audio(u);
      el.play().catch(() => {});
    } catch (e) {
      setErr(friendlyErrorText(String(e)));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="mt-1 flex items-center gap-2">
      <button className="btn-ghost px-2 py-0.5 text-[11px]" onClick={play} disabled={busy}>
        {busy ? "合成中…" : url ? "试听已选音色" : "试听"}
      </button>
      {url && <audio controls src={url} className="h-6 w-44" />}
      {err && <span className="text-[11px] text-red-300">{err}</span>}
    </div>
  );
}

/** 外呼（SIP）卡片：mode 决定后端；real 档才显示 trunk/鉴权字段组。 */
function SipCard({ value, onChange }: { value: ProviderForm; onChange: (next: ProviderForm) => void }) {
  const base =
    "mt-1 w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)";
  const mode = String(value.mode ?? "mock");
  const set = (key: string, v: unknown) => onChange({ ...value, [key]: v });
  const numbers = Array.isArray(value.numbers) ? (value.numbers as string[]) : [];
  const hasPassword = Boolean(value.has_auth_password) || Boolean(value.auth_password);
  // 站点注册 trunk（P1.5 T3）：站点来自 CP 站点表；注册成功把返回 id 回填 trunk_id 字段。
  const [sites, setSites] = useState<Record<string, unknown>[]>([]);
  const [siteId, setSiteId] = useState("");
  const [trunkBusy, setTrunkBusy] = useState(false);
  const [trunkNote, setTrunkNote] = useState<{ text: string; error?: boolean } | null>(null);
  // 建站（P1.5 T7）：站点表此前只有 repo 层入口，空库时下拉只能提示「请先在后端
  // 登记站点」——这里补最小建站表单（name + livekit_url 两字段，其余走默认）。
  const [showSiteForm, setShowSiteForm] = useState(false);
  const [newSiteName, setNewSiteName] = useState("");
  const [newSiteUrl, setNewSiteUrl] = useState("");
  const [siteBusy, setSiteBusy] = useState(false);
  const [siteNote, setSiteNote] = useState<{ text: string; error?: boolean } | null>(null);

  useEffect(() => {
    if (mode !== "real") return;
    api.listSites()
      .then((rows) => {
        setSites(rows);
        setSiteId((cur) => (cur && rows.some((r) => String(r.id) === cur) ? cur : String(rows[0]?.id ?? "")));
      })
      .catch(() => setSites([]));
  }, [mode]);

  async function registerTrunk() {
    setTrunkNote(null);
    setTrunkBusy(true);
    try {
      const res = await api.registerSipTrunk(siteId, {
        address: String(value.address ?? "").trim(),
        auth_username: String(value.auth_username ?? ""),
        auth_password: String(value.auth_password ?? ""),
        numbers,
      });
      set("trunk_id", res.trunk_id);
      setSites((rows) => rows.map((r) => (String(r.id) === siteId ? { ...r, trunk_id: res.trunk_id } : r)));
      // 掩码回显下表单密码为空=已存密码不回传：此时注册走 IP 白名单模式，提示用户
      // 免得以为用的是已存密码。
      const ipMode = !String(value.auth_password ?? "") && hasPassword;
      setTrunkNote({
        text:
          `已注册 ${res.trunk_id}，Trunk ID 已回填——记得保存设置。` +
          (ipMode ? "（表单密码留空，本次按 IP 白名单模式注册；要密码鉴权重填后再注册一次）" : ""),
      });
    } catch (e) {
      setTrunkNote({ text: friendlyErrorText(String(e)), error: true });
    } finally {
      setTrunkBusy(false);
    }
  }

  async function createSite() {
    setSiteNote(null);
    setSiteBusy(true);
    try {
      const row = await api.createSite({
        name: newSiteName.trim(),
        livekit_url: newSiteUrl.trim(),
      });
      const id = String(row.id ?? "");
      // CP 侧幂等：同账号同名返回既有行——据此如实提示「已存在」而不是谎报新建。
      const existed = sites.some((r) => String(r.id) === id);
      setSites((rows) => (existed ? rows.map((r) => (String(r.id) === id ? row : r)) : [...rows, row]));
      setSiteId(id);
      setNewSiteName("");
      setNewSiteUrl("");
      setShowSiteForm(false);
      setSiteNote({
        text: existed
          ? `已存在同名站点「${String(row.name ?? id)}」，已为你选中（未重复建）。`
          : `站点「${String(row.name ?? id)}」已建好——可继续注册 trunk。`,
      });
    } catch (e) {
      setSiteNote({ text: friendlyErrorText(String(e)), error: true });
    } finally {
      setSiteBusy(false);
    }
  }

  return (
    <section className="card">
      <span className="label">外呼（SIP）</span>
      <p className="mt-1 text-xs muted">
        mock=本机派生真语音被叫（演示/E2E），real=经 SIP trunk 拨真号码。环境变量
        <code className="mx-1">BOK_SIP_MODE</code>是运维级覆盖，设置后此处不生效。
      </p>
      <div className="mt-3 space-y-2">
        <label className="block">
          <span className="text-xs text-(--stage-muted)">拨号后端</span>
          <select className={base} value={mode} onChange={(e) => set("mode", e.target.value)}>
            <option value="mock">mock（本机派生被叫）</option>
            <option value="real">real（SIP trunk 真拨号）</option>
          </select>
        </label>
        {mode === "real" && (
          <>
            <label className="block">
              <span className="text-xs text-(--stage-muted)">Trunk ID</span>
              <input
                className={base}
                placeholder="ST_xxxxxxxx"
                value={String(value.trunk_id ?? "")}
                onChange={(e) => set("trunk_id", e.target.value)}
              />
              <p className="mt-1 text-xs muted">LiveKit SIP trunk 的 ID（sip_trunk_id）。</p>
            </label>
            <label className="block">
              <span className="text-xs text-(--stage-muted)">SIP 地址 / 网关</span>
              <input
                className={base}
                placeholder="sip.example.com"
                value={String(value.address ?? "")}
                onChange={(e) => set("address", e.target.value)}
              />
            </label>
            <label className="block">
              <span className="text-xs text-(--stage-muted)">鉴权用户名</span>
              <input
                className={base}
                value={String(value.auth_username ?? "")}
                onChange={(e) => set("auth_username", e.target.value)}
              />
            </label>
            <label className="block">
              <span className="text-xs text-(--stage-muted)">鉴权密码</span>
              <input
                type="password"
                className={base}
                placeholder={hasPassword ? "已保存（留空即不修改）" : ""}
                value={String(value.auth_password ?? "")}
                onChange={(e) => set("auth_password", e.target.value)}
              />
              <p className="mt-1 text-xs muted">留空保存=保留已存密码。</p>
            </label>
            <label className="block">
              <span className="text-xs text-(--stage-muted)">许可主叫号（逗号分隔）</span>
              <input
                className={base}
                placeholder="+8613800138000, +8613800138001"
                value={numbers.join(", ")}
                onChange={(e) =>
                  set(
                    "numbers",
                    e.target.value
                      .split(",")
                      .map((n) => n.trim())
                      .filter(Boolean),
                  )
                }
              />
            </label>
            <div className="rounded-lg border border-(--card-border) p-2">
              <span className="text-xs text-(--stage-muted)">注册 trunk 到站点</span>
              <p className="mt-1 text-xs muted">
                用上面的地址/主叫号/鉴权在当前站点创建 LiveKit outbound trunk，成功后 Trunk ID 自动回填
                （campaign 按站点取 trunk；密码不会回显）。
              </p>
              <select
                className={base}
                value={siteId}
                onChange={(e) => setSiteId(e.target.value)}
                disabled={sites.length === 0}
              >
                {sites.length === 0 ? (
                  <option value="">（暂无站点——用下方「+ 新建站点」建一个）</option>
                ) : (
                  sites.map((s) => (
                    <option key={String(s.id)} value={String(s.id)}>
                      {String(s.name || s.id)}
                      {s.trunk_id ? `（已注册 ${String(s.trunk_id)}）` : ""}
                    </option>
                  ))
                )}
              </select>
              <button
                className="btn-ghost mt-2 px-2 py-0.5 text-[11px]"
                onClick={() => {
                  setSiteNote(null);
                  setShowSiteForm((v) => !v);
                }}
              >
                {showSiteForm ? "取消新建" : "+ 新建站点"}
              </button>
              {showSiteForm && (
                <div className="mt-2 rounded-lg border border-(--card-border) p-2">
                  <label className="block">
                    <span className="text-xs text-(--stage-muted)">站点名</span>
                    <input
                      className={base}
                      placeholder="hk-edge"
                      value={newSiteName}
                      onChange={(e) => setNewSiteName(e.target.value)}
                    />
                  </label>
                  <label className="mt-2 block">
                    <span className="text-xs text-(--stage-muted)">LiveKit 地址</span>
                    <input
                      className={base}
                      placeholder="wss://vps.example:7880"
                      value={newSiteUrl}
                      onChange={(e) => setNewSiteUrl(e.target.value)}
                    />
                    <p className="mt-1 text-xs muted">站点 LiveKit 的 ws:// / wss:// 地址（VPS 同机=ws://127.0.0.1:7880）。</p>
                  </label>
                  <button
                    className="btn-ghost mt-2 px-2 py-0.5 text-[11px]"
                    onClick={createSite}
                    disabled={!newSiteName.trim() || siteBusy}
                  >
                    {siteBusy ? "创建中…" : "创建站点"}
                  </button>
                  <p className="mt-1 text-xs muted">同名站点已存在时直接复用（不会重复建）。</p>
                </div>
              )}
              <button
                className="btn-ghost mt-2 px-2 py-0.5 text-[11px]"
                onClick={registerTrunk}
                disabled={!siteId || trunkBusy}
              >
                {trunkBusy ? "注册中…" : "注册 trunk"}
              </button>
              {trunkNote && (
                <p className={`mt-1 text-[11px] ${trunkNote.error ? "text-red-300" : "muted"}`}>{trunkNote.text}</p>
              )}
              {siteNote && (
                <p className={`mt-1 text-[11px] ${siteNote.error ? "text-red-300" : "muted"}`}>{siteNote.text}</p>
              )}
            </div>
          </>
        )}
        <div className="grid grid-cols-2 gap-2">
          <label className="block">
            <span className="text-xs text-(--stage-muted)">振铃超时（秒）</span>
            <input
              type="number"
              className={base}
              value={String(value.ringing_timeout_s ?? 30)}
              onChange={(e) => set("ringing_timeout_s", Number(e.target.value))}
            />
          </label>
          <label className="block">
            <span className="text-xs text-(--stage-muted)">单通最长时长（秒）</span>
            <input
              type="number"
              className={base}
              value={String(value.max_call_duration_s ?? 600)}
              onChange={(e) => set("max_call_duration_s", Number(e.target.value))}
            />
            <p className="mt-1 text-xs muted">mock 档由本地计时器兜底收线。</p>
          </label>
        </div>
      </div>
    </section>
  );
}

function AudioDevicesCard() {

  const [mic, setMic] = useState<AudioDeviceInfo[]>([]);
  const [outs, setOuts] = useState<AudioDeviceInfo[]>([]);
  const [micId, setMicId] = useState("");
  const [outId, setOutId] = useState("");
  const [note, setNote] = useState("");

  const refresh = useCallback(async () => {
    const mics = await listAudioDevicesOf("input").catch(() => []);
    setMic(mics);
    const savedMic = savedMicDevice();
    if (savedMic && mics.some((m) => m.id === savedMic)) setMicId(savedMic);
    else if (mics.some((m) => m.is_default)) setMicId(mics.find((m) => m.is_default)!.id);
    else if (mics.length > 0) setMicId(mics[0].id);

    const outsArr = await listAudioDevicesOf("output").catch(() => []);
    setOuts(outsArr);
    const savedOut = savedOutputDevice();
    if (savedOut && outsArr.some((o) => o.id === savedOut)) setOutId(savedOut);
    else if (outsArr.some((o) => o.is_default)) setOutId(outsArr.find((o) => o.is_default)!.id);
    else if (outsArr.length > 0) setOutId(outsArr[0].id);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const canSetOutput = webCanSwitchOutput();

  return (
    <section className="card">
      <span className="label">音频设备</span>
      <p className="mt-1 text-xs muted">
        浏览器模式下仅 Chromium 内核支持切换扬声器输出；所选输出在接通通话时应用。
      </p>
      <div className="mt-3 space-y-3">
        <div>
          <span className="text-xs text-(--stage-muted)">麦克风（输入）</span>
          <div className="mt-1 flex gap-2">
            <select
              className="flex-1 rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)"
              value={micId}
              onChange={(e) => { setMicId(e.target.value); saveMicDevice(e.target.value); }}
            >
              {mic.length === 0 && <option value="">未检测到麦克风</option>}
              {mic.map((m) => (
                <option key={m.id} value={m.id}>{m.name}{m.is_default ? "（系统默认）" : ""}</option>
              ))}
            </select>
            <button
              className="btn-ghost text-xs"
              onClick={async () => {
                const ok = await requestMicPermission();
                setNote(ok ? "麦克风权限已开启，正在刷新设备…" : "麦克风权限被拒绝。请在 系统设置 › 隐私与安全性 › 麦克风 中允许本应用。");
                await refresh();
              }}
            >
              刷新
            </button>
          </div>
          {mic.length === 0 && (
            <p className="mt-1 text-xs text-red-300">
              未检测到麦克风或未授权。请先点击「刷新」授权；若仍为空，到系统设置开启麦克风权限后重启应用。
            </p>
          )}
        </div>

        <div>
          <span className="text-xs text-(--stage-muted)">扬声器 / 输出</span>
          {canSetOutput ? (
            <select
              className="mt-1 w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)"
              value={outId}
              onChange={(e) => {
                const id = e.target.value;
                setOutId(id);
                // 纯持久化：setSinkId 需要 room 已连接，接通通话时自动应用
                //（CallStudio join-time / interpret 一体台按端恢复）。
                saveOutputDevice(id);
              }}
            >
              {outs.length === 0 && <option value="">未检测到输出设备</option>}
              {outs.map((o) => (
                <option key={o.id} value={o.id}>{o.name}{o.is_default ? "（系统默认）" : ""}</option>
              ))}
            </select>
          ) : (
            <p className="mt-1 text-xs muted">
              当前浏览器（WebKit）不支持网页切换扬声器。输出跟随系统默认设备，请在系统声音设置中选择。
            </p>
          )}
        </div>
        {note && <p className="text-xs muted">{note}</p>}
      </div>
    </section>
  );
}

/** 设置页主视图只留最常用的语音字段；其余（base_url/采样率/分语言音色）收进「更多」。 */
const VOICE_SIMPLE_KEYS = ["api_key", "voice_mode", "speaker", "instruct"];

function FieldRow({
  field,
  kind,
  provider,
  value,
  onChange,
}: {
  field: FieldMeta;
  kind: ProviderKind;
  provider: string;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  return (
    <label className="block">
      <span className="text-xs text-(--stage-muted)">{field.label}</span>
      <FieldInput field={field} value={value} onChange={onChange} />
      {field.hint && <p className="mt-1 text-xs muted">{field.hint}</p>}
      {field.preview && kind === "tts" && (
        <VoicePreview provider={provider} fieldKey={field.key} voice={String(value ?? "")} />
      )}
    </label>
  );
}

/** MiniMax 云端克隆清单条目（tts.minimax_clones_json，路线 B）。 */
type MinimaxClone = { voice_id: string; label?: string; sample_lang?: string; created_at?: string; activated?: boolean };

function parseClones(raw: unknown): MinimaxClone[] {
  try {
    const data = JSON.parse(String(raw ?? "[]"));
    return Array.isArray(data) ? (data as MinimaxClone[]) : [];
  } catch {
    return [];
  }
}

/**
 * 「克隆我的声音」面板（MiniMax 云端 voice clone）：录音/上传参考音频（官方要求
 * ≥10s）→ CP 两步克隆（files/upload → voice_clone）。拍板「先克隆不激活」：
 * 克隆 0 费用；MiniMax 规则 7 天内未用于合成会删、首次合成收 ¥9.9/音色——
 * 试听/首次会话使用即激活。
 */
function MinimaxClonePanel({ clones, onChange }: { clones: MinimaxClone[]; onChange: (next: MinimaxClone[]) => void }) {
  const [label, setLabel] = useState("");
  const [refFile, setRefFile] = useState<File | null>(null);
  const [recording, setRecording] = useState(false);
  const [recSec, setRecSec] = useState(0);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const recRef = useRef<RecorderHandle | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  async function toggleRecording() {
    setErr("");
    if (recording) {
      const handle = recRef.current;
      recRef.current = null;
      if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
      setRecording(false);
      setRecSec(0);
      if (!handle) return;
      try {
        const wav = await handle.stop();
        if (wav.size < 4096) { setErr("录音太短，官方要求参考音频至少 10 秒。"); return; }
        setRefFile(new File([wav], `clone-ref-${Date.now()}.wav`, { type: "audio/wav" }));
      } catch (e) { setErr(`录音失败：${String(e)}`); }
      return;
    }
    try {
      recRef.current = await startRecording(60000);
      setRecording(true);
      setRecSec(0);
      timerRef.current = setInterval(() => setRecSec((s) => s + 1), 1000);
    } catch (e) { setErr(`无法开始录音：${String(e)}`); }
  }

  async function submit() {
    setErr("");
    if (!refFile) { setErr("请先录音或上传参考音频（≥10 秒，wav/mp3/m4a）。"); return; }
    setBusy(true);
    try {
      const body = new FormData();
      body.append("file", refFile);
      body.append("label", label || `我的声音-${new Date().toLocaleDateString()}`);
      body.append("sample_lang", "zh");
      const created = await api.registerMinimaxVoice(body);
      onChange([...clones, {
        voice_id: String(created.voice_id ?? ""),
        label: String(created.label ?? label ?? ""),
        sample_lang: String(created.sample_lang ?? "zh"),
        created_at: String(created.created_at ?? ""),
        activated: false,
      }]);
      setRefFile(null);
      setLabel("");
    } catch (e) {
      setErr(friendlyErrorText(String(e)));
    } finally {
      setBusy(false);
    }
  }

  async function remove(voiceId: string) {
    setErr("");
    setBusy(true);
    try {
      await api.deleteMinimaxVoice(voiceId);
      onChange(clones.filter((c) => c.voice_id !== voiceId));
    } catch (e) {
      setErr(friendlyErrorText(String(e)));
    } finally {
      setBusy(false);
    }
  }

  async function preview(voiceId: string, lang: string) {
    setErr("");
    setBusy(true);
    try {
      const text = lang === "en" ? "Hello, this is my cloned voice." : "你好，这是用我的声音克隆的音色。";
      const blob = await api.previewTts({ provider: "minimax", text, voice: voiceId, language: lang || "zh", sample_rate: 24000 });
      new Audio(URL.createObjectURL(blob)).play().catch(() => {});
    } catch (e) {
      setErr(friendlyErrorText(String(e)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <details className="rounded-lg border border-(--card-border) p-2 text-sm">
      <summary className="cursor-pointer text-xs muted hover:text-accent">克隆我的声音（MiniMax 云端）</summary>
      <p className="mt-2 text-xs muted">
        念 10 秒~1 分钟干净人声（普通话/粤语样本均可），克隆成云端音色后可在分语言音色与
        同传会话中选用。<strong>克隆本身免费</strong>；MiniMax 规则：7 天内未用于合成会过期，
        首次合成（试听/会话使用）激活并计费约 ¥9.9/音色。需账号完成实名认证。
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button className="btn-ghost text-xs" disabled={busy} onClick={toggleRecording}>
          {recording ? `停止录音（${recSec}s）` : "录音 10 秒+"}
        </button>
        <label className="btn-ghost cursor-pointer text-xs">
          上传音频
          <input type="file" accept="audio/wav,audio/mpeg,audio/mp4" className="hidden"
            onChange={(e) => { const f = e.target.files?.[0]; if (f) setRefFile(f); }} />
        </label>
        <input
          className="w-40 rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--accent)"
          placeholder="标签（如：我的声音）"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
        <button className="btn-primary text-xs" disabled={busy || !refFile} onClick={submit}>
          {busy ? "处理中…" : "克隆到 MiniMax"}
        </button>
        {refFile && <span className="text-xs muted">{refFile.name}</span>}
      </div>
      {clones.length > 0 && (
        <ul className="mt-2 space-y-1">
          {clones.map((c) => (
            <li key={c.voice_id} className="flex flex-wrap items-center gap-2 text-xs">
              <span className="font-medium">{c.label || c.voice_id}</span>
              {!c.activated && <span className="rounded bg-amber-500/15 px-1 text-amber-500">未激活</span>}
              <span className="muted">{c.voice_id}</span>
              <button className="btn-ghost px-1 py-0 text-xs" disabled={busy}
                onClick={() => preview(c.voice_id, c.sample_lang || "zh")}>试听（激活）</button>
              <button className="btn-ghost px-1 py-0 text-xs text-red-400" disabled={busy}
                onClick={() => remove(c.voice_id)}>删除</button>
            </li>
          ))}
        </ul>
      )}
      {err && <p className="mt-2 text-xs text-red-400">{err}</p>}
    </details>
  );
}

/**
 * 语音与凭据卡（主视图唯一保留的引擎卡）：provider + Key + 默认音色 + 语气指令。
 * 分发形态下这些凭据由云端下发（spec §9）；单机形态仍从这里改。
 */
function VoiceCard({ value, onChange }: { value: ProviderForm; onChange: (next: ProviderForm) => void }) {
  const meta = SETTING_CARDS.find((c) => c.kind === "tts")!;
  const provider = String(value.provider ?? DEFAULT_PROVIDER.tts);
  const providerMeta = meta.providers.find((p) => p.value === provider);
  const visible = meta.fields.filter((f) => !f.advanced && (!f.providers || f.providers.includes(provider)));
  const simple = visible.filter((f) => VOICE_SIMPLE_KEYS.includes(f.key));
  const rest = visible.filter((f) => !VOICE_SIMPLE_KEYS.includes(f.key));
  const advanced = meta.fields.filter((f) => f.advanced && (!f.providers || f.providers.includes(provider)));
  // MiniMax 云端克隆清单（存 tts.minimax_clones_json）——合并进分语言三键下拉，
  // 全语言槽可选（克隆音色无语言绑定，language_boost 按请求生效）。
  const clones = parseClones(value.minimax_clones_json);
  const cloneOptions = clones.map((c) => ({ value: String(c.voice_id), label: `克隆 · ${c.label || c.voice_id}` }));
  const withClones = (field: FieldMeta): FieldMeta =>
    cloneOptions.length > 0 && field.key.startsWith("speaker_")
      ? { ...field, options: [...(field.options ?? []), ...cloneOptions] }
      : field;
  const setClones = (next: MinimaxClone[]) =>
    onChange({ ...value, minimax_clones_json: JSON.stringify(next) });
  return (
    <section className="card">
      <span className="label">语音与凭据</span>
      <p className="mt-1 text-xs muted">
        客户听到的声音与云端凭据（MiniMax / DeepSeek 等，持久化保存、重启不丢）。音色也可在「人设」页按人设绑定。
      </p>
      <div className="mt-3 space-y-2">
        <div>
          <select
            className="w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)"
            value={provider}
            onChange={(e) => onChange({ ...value, provider: e.target.value })}
          >
            {meta.providers.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
          {providerMeta?.hint && <p className="mt-1 text-xs muted">{providerMeta.hint}</p>}
        </div>
        {simple.map((field) => (
          <FieldRow
            key={field.key}
            field={withClones(field)}
            kind="tts"
            provider={provider}
            value={value[field.key]}
            onChange={(v) => onChange({ ...value, [field.key]: v })}
          />
        ))}
        {(rest.length > 0 || advanced.length > 0) && (
          <details className="rounded-lg border border-(--card-border) p-2 text-sm">
            <summary className="cursor-pointer text-xs muted hover:text-accent">更多语音参数（服务地址 / 采样率 / 分语言音色）</summary>
            <div className="mt-2 space-y-2">
              {[...rest, ...advanced].map((field) => (
                <FieldRow
                  key={field.key}
                  field={withClones(field)}
                  kind="tts"
                  provider={provider}
                  value={value[field.key]}
                  onChange={(v) => onChange({ ...value, [field.key]: v })}
                />
              ))}
            </div>
          </details>
        )}
        <MinimaxClonePanel clones={clones} onChange={setClones} />
      </div>
    </section>
  );
}

export default function SettingsPage() {
  const [form, setForm] = useState<Record<string, any>>(EMPTY_FORM);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);
  const [health, setHealth] = useState("");

  useEffect(() => {
    api.getSettings()
      .then((settings) => {
        const s = settings as Record<string, any>;
        setForm({
          asr: { ...EMPTY_FORM.asr, ...(s.asr ?? {}) },
          llm: { ...EMPTY_FORM.llm, ...(s.llm ?? {}) },
          tts: { ...EMPTY_FORM.tts, ...(s.tts ?? {}) },
          vad: { ...EMPTY_FORM.vad, ...(s.vad ?? {}) },
          sip: { ...EMPTY_FORM.sip, ...(s.sip ?? {}) },
          policy: s.policy ?? "offline_first",
        });
      })
      .catch((e) => setErr(friendlyErrorText(String(e))))
      .finally(() => setLoading(false));
  }, []);

  async function save() {
    setErr(null);
    setOk(false);
    try {
      const payload = {
        asr: { ...EMPTY_FORM.asr, ...form.asr },
        llm: { ...EMPTY_FORM.llm, ...form.llm },
        tts: { ...EMPTY_FORM.tts, ...form.tts },
        vad: { ...EMPTY_FORM.vad, ...form.vad },
        sip: { ...EMPTY_FORM.sip, ...form.sip },
        policy: form.policy ?? "offline_first",
      };
      await api.saveSettings(payload);
      setOk(true);
    } catch (e) {
      setErr(friendlyErrorText(String(e)));
    }
  }

  async function testHealth(kind: "asr" | "tts") {
    setHealth("");
    try {
      const result = kind === "asr" ? await api.asrHealth() : await api.ttsHealth();
      setHealth(`${kind.toUpperCase()} OK: ${JSON.stringify(result)}`);
    } catch (e) {
      setHealth(`${kind.toUpperCase()} failed: ${friendlyErrorText(String(e))}`);
    }
  }

  const policyValue = form.policy ?? "offline_first";
  const policyOption = POLICY_META.options.find((o) => o.value === policyValue);

  // 引擎参数收进「开发者参数」折叠区：语音卡之外的 ASR/LLM/VAD 与运行策略。
  const devCards: ProviderKind[] = ["asr", "llm", "vad"];

  return (
    <div>
      <div className="mb-8">
        <h1 className="page-title">设置</h1>
        <p className="page-sub">语音与凭据 · 音频设备 · 外呼</p>
      </div>

      {loading ? (
        <LoadingState />
      ) : (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <VoiceCard value={form.tts ?? {}} onChange={(next) => setForm({ ...form, tts: next })} />
          <AudioDevicesCard />
          <SipCard value={form.sip ?? {}} onChange={(next) => setForm({ ...form, sip: next })} />
          <CannedAuditionCard />
          <details className="rounded-xl border border-(--card-border) bg-(--card) p-4 lg:col-span-2">
            <summary className="cursor-pointer text-sm font-medium">
              开发者参数（ASR / LLM / VAD / 运行策略）
            </summary>
            <p className="mt-2 text-xs muted">
              面向本机部署与排障：引擎 Provider、识别语言、VAD 与打断、本地模型、运行策略。
              默认值已按当前本地栈校准，日常使用无需改动；改本地模型需重启本地服务生效。
            </p>
            <div className="mt-4 grid grid-cols-1 gap-6 lg:grid-cols-2">
              {devCards.map((kind) => (
                <ProviderCard key={kind} kind={kind} value={form[kind] ?? {}} onChange={(next) => setForm({ ...form, [kind]: next })} />
              ))}
              <section className="card">
                <span className="label">{POLICY_META.title}</span>
                <select
                  className="mt-3 w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)"
                  value={policyValue}
                  onChange={(e) => setForm({ ...form, policy: e.target.value })}
                >
                  {POLICY_META.options.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
                {policyOption?.hint && <p className="mt-2 text-xs muted">{policyOption.hint}</p>}
                <p className="mt-2 text-xs muted">策略与 Provider 会在下一次建立通话时应用到 Agent 会话。</p>
              </section>
            </div>
          </details>
          <div className="flex flex-wrap items-end gap-3 lg:col-span-2">
            <button className="btn-primary" onClick={save}>保存设置</button>
            <button className="btn-ghost" onClick={() => testHealth("asr")}>测试 ASR</button>
            <button className="btn-ghost" onClick={() => testHealth("tts")}>测试 TTS</button>
            {ok && <span className="text-sm text-emerald-400">已保存。</span>}
            {health && <span className="text-sm muted">{health}</span>}
          </div>
          {err && <div className="lg:col-span-2"><ErrorState message={err} /></div>}
        </div>
      )}
    </div>
  );
}
