"""bokctl.commands.demo_setup —— `bok demo-setup`（新 Mac 一键演示档，2026-10-06）。

把一套新装机直接写进 demo-cloud 姿势（A 线全云：豆包 SAUC ASR + DeepSeek
a_reply/judge/settle + MiniMax TTS；B 线 MT 本地 :1236），链路=
`bootstrap.sh` → `bok demo-setup` → `bok serve`。与 2026-10-05 demo-cloud wave
（`servers._cloud_posture` 姿势判定 + ensure 收窄）配套：那半边只「读设置库
判云本地」，本命令是「写设置库进演示档」的另一半。

- **栈下直写**（与被自动化的手工手术同姿势）：sqlite 直写
  `global_settings` 的 asr_json/tts_json/model_routing_json 三列；CP 在跑时
  直写=CP 内存态与磁盘漂移+锁竞争，故 :8000 健康即拒绝（`bok down` 后再跑）。
- **read-modify-write / preserve-others**：asr 段只动 provider/api_key/
  resource_id 三键（vad/打断调参住 vad_json 独立列天然不碰；asr 段内的
  language_mode/engine/endpoint 等原键保留）；tts 段只动 provider/api_key；
  路由表只覆写五车道与 demo-cloud 预置，其余车道/预置原样保留。
- **幂等**：同值重跑收敛（结果逐键一致）；换 key 重跑=更新该键。
- **零 bok_voice_core import**（_cloud_posture 同款纪律：CLI 裸环境跑）——
  路由表形状按 packages/core/bok_voice_core/model_routes.py 同款规则轻量
  镜像（provider 规范化、`extra.enable_thinking` 布尔），不 import 共享契约包。
- **密钥纪律**：key 值绝不打印/回显，确认行只给 `<set:Nch>` 掩码形态；key
  只落设置库（不进仓不进日志）。预置 demo-cloud 快照剥 api_key（CP 保存面
  同款语义：套档不携带密钥，apply 时车道现有 key 保留）。

车道面（与 AGENTS.md「当前生产姿态」demo-cloud 档一致）：
  a_reply/judge = openai deepseek-flash thinking-off（对话/判据关思考换首字延迟）
  settle        = openai deepseek-v4-pro thinking-ON（纪要显式开——不对称口径）
  mining        = openai deepseek-v4-pro thinking-off（沉淀关思考）
  mt            = local {base_url:"", model:--mt-model 或 ""}（空=env 缺省链 :1236）

patch 缝：`cmd_demo_setup`（PATCH_TARGETS → 本模块）、`paths.app_data_dir`
（tmp DB 注入）、`core.healthy`（CP-up 拒绝腿打桩）——一切穿模块对象 call-time
取（cli 分家纪律）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime

from bokctl import core, paths

# 路由五车道与 provider 常量（model_routes.LANES/PROVIDER_* 的轻量镜像——
# 零 bok_voice_core import 纪律；值漂移由 tests/test_demo_setup.py 对拍钉住）。
_LANES = ("a_reply", "judge", "mt", "settle", "mining")
_PRESET_NAME = "demo-cloud"
_DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"

# 演示档车道面（thinking 开关按「对话/判据关、纪要开」不对称口径）。
_DEMO_LANE_MODELS = {
    "a_reply": "deepseek-flash",
    "judge": "deepseek-flash",
    "settle": "deepseek-v4-pro",
    "mining": "deepseek-v4-pro",
}
_DEMO_LANE_THINKING = {
    "a_reply": False,
    "judge": False,
    "settle": True,
    "mining": False,
}

# 新库建表 DDL：与 business-db models.GlobalSetting 的 create_all(sqlite) 逐列
# 同形（CP 启动 create_all checkfirst 跳过、_ensure_column 全列在场 no-op）。
_CREATE_GLOBAL_SETTINGS_SQL = """
CREATE TABLE IF NOT EXISTS global_settings (
    id VARCHAR(64) NOT NULL,
    asr_json TEXT NOT NULL DEFAULT '{}',
    llm_json TEXT NOT NULL DEFAULT '{}',
    tts_json TEXT NOT NULL DEFAULT '{}',
    vad_json TEXT NOT NULL DEFAULT '{}',
    sip_json TEXT NOT NULL DEFAULT '',
    campaign_json TEXT NOT NULL DEFAULT '',
    sms_json TEXT NOT NULL DEFAULT '',
    model_routing_json TEXT NOT NULL DEFAULT '',
    policy VARCHAR(64) NOT NULL DEFAULT 'offline_first',
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id)
)
"""

# 缺行补插的整行默认值（与 ORM default 同值；updated_at 走 sqlite DATETIME
# 常规存储格式，ORM 读回可解析——INSERT 时现取时刻补上，见 _apply_demo_settings）。
_ROW_DEFAULTS = {
    "asr_json": "{}",
    "llm_json": "{}",
    "tts_json": "{}",
    "vad_json": "{}",
    "sip_json": "",
    "campaign_json": "",
    "sms_json": "",
    "model_routing_json": "",
    "policy": "offline_first",
}


def _mask(key: str) -> str:
    """掩码确认形态：只回长度不回值（`<set:36ch>`）。"""
    return f"<set:{len(key)}ch>"


def _resolve_args(args: argparse.Namespace) -> dict:
    """argv 优先、env 兜底地解析六参；三个 key 缺一即 ValueError（人话清单）。"""
    def pick(flag: str, env_key: str, default: str = "") -> str:
        val = str(getattr(args, flag, "") or "").strip()
        if val:
            return val
        return str(os.environ.get(env_key, "") or "").strip() or default

    values = {
        "asr_key": pick("asr_key", "BOK_DEMO_ASR_KEY"),
        "asr_resource_id": pick("asr_resource_id", "BOK_DEMO_ASR_RESOURCE_ID"),
        "tts_key": pick("tts_key", "BOK_DEMO_TTS_KEY"),
        "deepseek_key": pick("deepseek_key", "BOK_DEMO_DEEPSEEK_KEY"),
        "deepseek_base_url": pick(
            "deepseek_base_url", "BOK_DEMO_DEEPSEEK_BASE_URL",
            _DEEPSEEK_DEFAULT_BASE_URL,
        ),
        "mt_model": pick("mt_model", "BOK_DEMO_MT_MODEL"),
    }
    missing = [
        f"--{f}（或 env {e}）"
        for f, e in (
            ("asr-key", "BOK_DEMO_ASR_KEY"),
            ("tts-key", "BOK_DEMO_TTS_KEY"),
            ("deepseek-key", "BOK_DEMO_DEEPSEEK_KEY"),
        )
        if not values[{"asr-key": "asr_key", "tts-key": "tts_key",
                       "deepseek-key": "deepseek_key"}[f]]
    ]
    if missing:
        raise ValueError("缺少必填凭据: " + "、".join(missing))
    return values


def _parse_routing(raw: str) -> dict:
    """宽容解析路由列（坏 JSON/形状错 → 空文档，不 raise；parse_routing 镜像）。"""
    text = str(raw or "").strip()
    if not text:
        return {"lanes": {}, "presets": {}}
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {"lanes": {}, "presets": {}}
    if not isinstance(data, dict):
        return {"lanes": {}, "presets": {}}
    lanes = data.get("lanes") if isinstance(data.get("lanes"), dict) else {}
    presets = data.get("presets") if isinstance(data.get("presets"), dict) else {}
    return {"lanes": lanes, "presets": presets}


def _demo_lane_cfg(provider_lane: str, values: dict) -> dict:
    """云端演示车道条目（DeepSeek 系）；mt 走 local 空 base_url=env 缺省链。"""
    if provider_lane == "mt":
        return {"provider": "local", "base_url": "", "model": values["mt_model"], "api_key": ""}
    return {
        "provider": "openai",
        "base_url": values["deepseek_base_url"],
        "model": _DEMO_LANE_MODELS[provider_lane],
        "api_key": values["deepseek_key"],
        "extra": {"enable_thinking": _DEMO_LANE_THINKING[provider_lane]},
    }


def _apply_demo_settings(db_path, values: dict) -> dict:
    """栈下直写三段（read-modify-write），返回写后读回的掩码摘要。

    新库=建 global_settings 全形表（CP create_all/_ensure_column 幂等接管）；
    缺行=整行默认值补插后再 UPDATE 目标列。返回值不含任何密钥明文。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=5)
    try:
        con.execute(_CREATE_GLOBAL_SETTINGS_SQL)
        row = con.execute(
            "SELECT asr_json, tts_json, model_routing_json FROM global_settings"
            " WHERE id='global'"
        ).fetchone()
        if row is None:
            cols = ", ".join((*_ROW_DEFAULTS, "updated_at"))
            marks = ", ".join("?" for _ in cols.split(", "))
            insert_vals = (
                *_ROW_DEFAULTS.values(),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
            )
            con.execute(
                f"INSERT INTO global_settings (id, {cols}) VALUES ('global', {marks})",
                insert_vals,
            )
            asr, tts, routing_raw = {}, {}, ""
        else:
            asr, tts, routing_raw = row[0], row[1], row[2]
            # 列存坏 JSON → 视同空段（preserve-others 无从谈起，但不吞列）。
            try:
                asr = json.loads(asr) if asr else {}
            except (ValueError, TypeError):
                asr = {}
            try:
                tts = json.loads(tts) if tts else {}
            except (ValueError, TypeError):
                tts = {}
            if not isinstance(asr, dict):
                asr = {}
            if not isinstance(tts, dict):
                tts = {}

        # ① asr 段：provider+凭据三键覆写，其余键原样保留。
        asr["provider"] = "doubao"
        asr["api_key"] = values["asr_key"]
        asr["resource_id"] = values["asr_resource_id"]
        # ② tts 段：provider+api_key 覆写，其余键（音色/克隆清单等）原样保留。
        tts["provider"] = "minimax"
        tts["api_key"] = values["tts_key"]

        # ③ 路由表：五车道覆写 + demo-cloud 预置快照（剥 key），其余原样保留。
        routing = _parse_routing(routing_raw)
        merged_lanes = dict(routing["lanes"])
        for lane in _LANES:
            merged_lanes[lane] = _demo_lane_cfg(lane, values)
        presets = dict(routing["presets"])
        presets[_PRESET_NAME] = {
            lane: {**cfg, "api_key": ""} for lane, cfg in merged_lanes.items()
        }

        con.execute(
            "UPDATE global_settings SET asr_json=:asr, tts_json=:tts,"
            " model_routing_json=:routing WHERE id='global'",
            {
                "asr": json.dumps(asr, ensure_ascii=False),
                "tts": json.dumps(tts, ensure_ascii=False),
                "routing": json.dumps(
                    {"lanes": merged_lanes, "presets": presets}, ensure_ascii=False
                ),
            },
        )
        con.commit()
    finally:
        con.close()

    return {
        "asr_provider": "doubao",
        "asr_key_mask": _mask(values["asr_key"]),
        "asr_resource_id_set": bool(values["asr_resource_id"]),
        "tts_provider": "minimax",
        "tts_key_mask": _mask(values["tts_key"]),
        "deepseek_base_url": values["deepseek_base_url"],
        "deepseek_key_mask": _mask(values["deepseek_key"]),
        "mt_model": values["mt_model"],
        "lanes": {lane: _demo_lane_cfg(lane, values) for lane in _LANES},
        "preset": _PRESET_NAME,
    }


def cmd_demo_setup(args: argparse.Namespace) -> int:
    """一键配置演示档入口。拒绝 CP 在跑；写库；打印掩码确认与姿势摘要。"""
    if core.healthy(8000):
        print("[demo-setup] 拒绝：control-plane (:8000) 在跑——设置库直写必须栈下执行。")
        print("  先 `python tools/bok.py down` 再重跑本命令（改完 `bok serve` 拉起即吃新档）。")
        return 2
    try:
        values = _resolve_args(args)
    except ValueError as exc:
        print(f"[demo-setup] {exc}")
        return 2

    db_path = paths.app_data_dir() / "bok_voice.db"
    summary = _apply_demo_settings(db_path, values)

    print(f"[demo-setup] 设置库 {db_path}")
    print(f"  asr.provider={summary['asr_provider']} asr.api_key={summary['asr_key_mask']}"
          f" resource_id={'set' if summary['asr_resource_id_set'] else '(缺省)'}")
    print(f"  tts.provider={summary['tts_provider']} tts.api_key={summary['tts_key_mask']}")
    print(f"  model_routing: deepseek base_url={summary['deepseek_base_url']}"
          f" api_key={summary['deepseek_key_mask']}")
    for lane in _LANES:
        cfg = summary["lanes"][lane]
        if lane == "mt":
            tail = f"model={cfg['model'] or '(env 缺省链 :1236)'}"
        else:
            thinking = bool((cfg.get("extra") or {}).get("enable_thinking"))
            tail = f"model={cfg['model']} thinking={'on' if thinking else 'off'}"
        print(f"    {lane}: provider={cfg['provider']} {tail}")
    print(f"  presets.{summary['preset']} 已写入（api_key 已剥，套档保留现有密钥）")
    print("[demo-setup] 下次 `bok serve` 的姿势: asr=cloud(豆包) tts=cloud(MiniMax)"
          " llm=cloud(DeepSeek) mt=local(:1236)")
    print("[demo-setup] 首跑 ensure 集合将为 {'mt'}（本地盘只落 MT 翻译模型）。")
    print("[demo-setup] 回切演示档: POST /api/model-routing/presets/demo-cloud/apply"
          "（切走后想回来用它一键切回）。")
    return 0


def run(args: argparse.Namespace) -> int:
    return cmd_demo_setup(args)
