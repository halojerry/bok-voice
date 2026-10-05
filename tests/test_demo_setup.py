"""bok demo-setup 单测（2026-10-06 新 Mac 一键演示档波）。

`commands.demo_setup` 栈下直写设置库三段（asr_json/tts_json/model_routing_json）：
- demo 车道面：a_reply/judge=deepseek-flash thinking-off、settle=deepseek-v4-pro
  thinking-ON（纪要不对称口径）、mining=deepseek-v4-pro thinking-off、
  mt=local 空 base_url（env 缺省链 :1236）；
- preset demo-cloud=扁平 lanes 快照且 api_key 全剥（CP 保存面同款语义）；
- preserve-others：asr/tts 段既有键与路由表既有车道/预置原样保留；
- 幂等：同值连跑两次逐字节收敛；换 key 重跑=更新该键；
- CP-up 拒绝：:8000 健康即退出非零并提示先 down（栈下直写红线）；
- 密钥纪律：key 明文绝不落 stdout（掩码 `<set:Nch>` 只回长度）；
- 新库建表形状与 business-db GlobalSetting create_all 同列集（CP 启动
  create_all checkfirst 跳过、_ensure_column no-op）。

约定：tmp sqlite fixture（patch_bok("app_data_dir", lambda: tmp_path)，tests/
test_cloud_posture.py 只读姿势同款），不碰真设置库、不碰真进程。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402
from _bokpatch import bok_target, patch_bok  # noqa: E402

# demo-setup 的 env 读取面（resolve 兜底）——测试统一清场防宿主机 ambient 污染。
_DEMO_ENV_KEYS = (
    "BOK_DEMO_ASR_KEY",
    "BOK_DEMO_ASR_RESOURCE_ID",
    "BOK_DEMO_TTS_KEY",
    "BOK_DEMO_DEEPSEEK_KEY",
    "BOK_DEMO_DEEPSEEK_BASE_URL",
    "BOK_DEMO_MT_MODEL",
)

_ASR_KEY = "k" * 36
_TTS_KEY = "m" * 32
_DS_KEY = "d" * 40
_DS_BASE = "https://api.deepseek.example/v1"

_ARGS = [
    "demo-setup",
    "--asr-key", _ASR_KEY,
    "--tts-key", _TTS_KEY,
    "--deepseek-key", _DS_KEY,
    "--deepseek-base-url", _DS_BASE,
]


def _clear_demo_env(monkeypatch, env: dict[str, str] | None = None) -> None:
    for key in _DEMO_ENV_KEYS:
        val = (env or {}).get(key)
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)


def _run_demo_setup(monkeypatch, tmp_path: Path, argv: list[str] | None = None,
                    *, healthy_8000: bool = False, env: dict[str, str] | None = None):
    """patch app_data_dir+healthy 后全链路跑 cmd_demo_setup，返回 (rc, capsys 输出)。"""
    patch_bok(monkeypatch, "app_data_dir", lambda: tmp_path)
    patch_bok(monkeypatch, "healthy", lambda port: healthy_8000)
    _clear_demo_env(monkeypatch, env)
    args = bok.parse_args(argv if argv is not None else _ARGS)
    return args, bok.commands.demo_setup.cmd_demo_setup(args)


def _read_row(tmp_path: Path) -> tuple[dict, dict, dict]:
    con = sqlite3.connect(tmp_path / "bok_voice.db")
    try:
        row = con.execute(
            "SELECT asr_json, tts_json, model_routing_json FROM global_settings"
            " WHERE id='global'"
        ).fetchone()
    finally:
        con.close()
    assert row is not None, "global 行必须在场"
    return json.loads(row[0]), json.loads(row[1]), json.loads(row[2])


# ---- 主链路：三段写入 + 掩码确认 ---------------------------------------------


def test_writes_three_sections_and_masks_output(monkeypatch, tmp_path, capsys) -> None:
    args, rc = _run_demo_setup(monkeypatch, tmp_path)
    assert rc == 0
    assert args.cmd == "demo-setup"
    asr, tts, routing = _read_row(tmp_path)

    assert asr["provider"] == "doubao"
    assert asr["api_key"] == _ASR_KEY
    assert "resource_id" not in asr or asr["resource_id"] == "", "未给 resource_id 不残留假值"
    assert tts["provider"] == "minimax"
    assert tts["api_key"] == _TTS_KEY

    lanes = routing["lanes"]
    assert set(lanes) == {"a_reply", "judge", "mt", "settle", "mining"}
    for lane in ("a_reply", "judge"):
        assert lanes[lane] == {
            "provider": "openai", "base_url": _DS_BASE, "model": "deepseek-flash",
            "api_key": _DS_KEY, "extra": {"enable_thinking": False},
        }, f"{lane} 车道=deepseek-flash thinking-off"
    assert lanes["settle"]["model"] == "deepseek-v4-pro"
    assert lanes["settle"]["extra"] == {"enable_thinking": True}, "settle 纪要=thinking ON"
    assert lanes["mining"] == {
        "provider": "openai", "base_url": _DS_BASE, "model": "deepseek-v4-pro",
        "api_key": _DS_KEY, "extra": {"enable_thinking": False},
    }
    assert lanes["mt"] == {"provider": "local", "base_url": "", "model": "", "api_key": ""}

    preset = routing["presets"]["demo-cloud"]
    assert set(preset) == {"a_reply", "judge", "mt", "settle", "mining"}, "预置=扁平 lanes 快照"
    assert all(cfg["api_key"] == "" for cfg in preset.values()), "预置 api_key 全剥"
    assert preset["settle"]["extra"] == {"enable_thinking": True}, "预置保留 thinking 档"
    assert preset["a_reply"]["model"] == "deepseek-flash"

    out = capsys.readouterr().out
    for secret in (_ASR_KEY, _TTS_KEY, _DS_KEY):
        assert secret not in out, "key 明文绝不回显"
    assert f"asr.api_key=<set:{len(_ASR_KEY)}ch>" in out
    assert f"tts.api_key=<set:{len(_TTS_KEY)}ch>" in out
    assert f"api_key=<set:{len(_DS_KEY)}ch>" in out
    assert "asr=cloud" in out and "tts=cloud" in out and "llm=cloud" in out
    assert "mt=local" in out
    assert "{'mt'}" in out, "ensure 收窄集提示"
    assert "demo-cloud" in out


def test_env_fallback_and_argv_wins(monkeypatch, tmp_path) -> None:
    """env 形态可用；argv 与 env 同设时 argv 优先（shell history 逃生门）。"""
    env = {
        "BOK_DEMO_ASR_KEY": "e" * 20,
        "BOK_DEMO_TTS_KEY": "f" * 20,
        "BOK_DEMO_DEEPSEEK_KEY": "g" * 20,
    }
    argv = ["demo-setup"]
    _run_demo_setup(monkeypatch, tmp_path, argv=argv, env=env)
    asr, tts, routing = _read_row(tmp_path)
    assert asr["api_key"] == "e" * 20 and tts["api_key"] == "f" * 20
    assert routing["lanes"]["a_reply"]["api_key"] == "g" * 20

    # argv 优先：同参再跑一次，argv 值覆盖 env 值。
    _run_demo_setup(monkeypatch, tmp_path, env=env)
    asr2, tts2, routing2 = _read_row(tmp_path)
    assert asr2["api_key"] == _ASR_KEY
    assert tts2["api_key"] == _TTS_KEY
    assert routing2["lanes"]["a_reply"]["api_key"] == _DS_KEY


def test_missing_required_keys_rejected(monkeypatch, tmp_path, capsys) -> None:
    args, rc = _run_demo_setup(monkeypatch, tmp_path, argv=["demo-setup", "--tts-key", _TTS_KEY])
    assert rc == 2
    out = capsys.readouterr().out
    assert "缺少必填凭据" in out
    assert "--asr-key" in out and "--deepseek-key" in out
    assert not (tmp_path / "bok_voice.db").exists(), "凭据不齐不得动库"


def test_resource_id_via_env_and_flag(monkeypatch, tmp_path) -> None:
    _run_demo_setup(
        monkeypatch, tmp_path,
        argv=_ARGS + ["--asr-resource-id", "res-123"],
    )
    asr, _tts, _routing = _read_row(tmp_path)
    assert asr["resource_id"] == "res-123"


def test_mt_model_flag_lands_in_lane_and_preset(monkeypatch, tmp_path) -> None:
    _run_demo_setup(monkeypatch, tmp_path, argv=_ARGS + ["--mt-model", "/models/Hy-MT2"])
    _asr, _tts, routing = _read_row(tmp_path)
    assert routing["lanes"]["mt"]["model"] == "/models/Hy-MT2"
    assert routing["presets"]["demo-cloud"]["mt"]["model"] == "/models/Hy-MT2"


# ---- preserve-others（read-modify-write 不清场）------------------------------


def _seed_db(tmp_path: Path) -> None:
    con = sqlite3.connect(tmp_path / "bok_voice.db")
    con.execute(
        "CREATE TABLE global_settings (id TEXT PRIMARY KEY, asr_json TEXT,"
        " tts_json TEXT, vad_json TEXT, model_routing_json TEXT)"
    )
    seed_routing = {
        "lanes": {
            "a_reply": {"provider": "local", "base_url": "http://127.0.0.1:1234/v1",
                        "model": "", "api_key": "", "extra": {"enable_thinking": False}},
        },
        "presets": {
            "local-9b": {"mt": {"provider": "local", "base_url": "", "model": "", "api_key": ""}},
        },
    }
    con.execute(
        "INSERT INTO global_settings (id, asr_json, tts_json, vad_json,"
        " model_routing_json) VALUES ('global', ?, ?, ?, ?)",
        (
            json.dumps({"provider": "qwen3_asr", "language_mode": "auto",
                        "engine": "whisper", "base_url": "http://127.0.0.1:9999"}),
            json.dumps({"provider": "qwen3_tts", "speaker_cantonese": "vivian",
                        "minimax_clones_json": "[]"}),
            json.dumps({"min_silence_duration": 0.28, "interruption": True}),
            json.dumps(seed_routing),
        ),
    )
    con.commit()
    con.close()


def test_preserves_other_fields_and_presets(monkeypatch, tmp_path) -> None:
    _seed_db(tmp_path)
    _run_demo_setup(monkeypatch, tmp_path)
    asr, tts, routing = _read_row(tmp_path)

    # asr 段：只动三键，language_mode/engine/base_url 保留。
    assert asr["provider"] == "doubao" and asr["api_key"] == _ASR_KEY
    assert asr["language_mode"] == "auto"
    assert asr["engine"] == "whisper"
    assert asr["base_url"] == "http://127.0.0.1:9999"

    # tts 段：音色/克隆清单保留。
    assert tts["provider"] == "minimax" and tts["api_key"] == _TTS_KEY
    assert tts["speaker_cantonese"] == "vivian"
    assert tts["minimax_clones_json"] == "[]"

    # 路由表：既有预置原样保留；demo-cloud 追加；a_reply 车道被演示档覆写。
    assert "local-9b" in routing["presets"], "既有预置不得被清场"
    assert routing["presets"]["local-9b"]["mt"]["provider"] == "local"
    assert routing["lanes"]["a_reply"]["provider"] == "openai"

    # vad_json 列是独立列——根本不在写入面，逐字节验证。
    con = sqlite3.connect(tmp_path / "bok_voice.db")
    try:
        vad = json.loads(con.execute(
            "SELECT vad_json FROM global_settings WHERE id='global'"
        ).fetchone()[0])
    finally:
        con.close()
    assert vad == {"min_silence_duration": 0.28, "interruption": True}, "vad 调参键原样"


# ---- 幂等 / 收敛 --------------------------------------------------------------


def test_idempotent_rerun_converges(monkeypatch, tmp_path) -> None:
    _seed_db(tmp_path)
    _run_demo_setup(monkeypatch, tmp_path)
    first = _read_row(tmp_path)
    _run_demo_setup(monkeypatch, tmp_path)
    second = _read_row(tmp_path)
    assert first == second, "同值连跑两次结果必须一致（逐字节收敛）"


def test_rerun_with_different_key_updates(monkeypatch, tmp_path) -> None:
    _run_demo_setup(monkeypatch, tmp_path)
    _run_demo_setup(monkeypatch, tmp_path, argv=[
        "demo-setup",
        "--asr-key", "n" * 12,
        "--tts-key", _TTS_KEY,
        "--deepseek-key", _DS_KEY,
    ])
    asr, _tts, routing = _read_row(tmp_path)
    assert asr["api_key"] == "n" * 12, "换 key 重跑=更新该键"
    assert routing["lanes"]["a_reply"]["api_key"] == _DS_KEY
    assert routing["presets"]["demo-cloud"]["a_reply"]["api_key"] == ""


# ---- 栈下红线：CP 在跑即拒绝 --------------------------------------------------


def test_refuses_when_control_plane_up(monkeypatch, tmp_path, capsys) -> None:
    _seed_db(tmp_path)
    before = _read_row(tmp_path)
    _args, rc = _run_demo_setup(monkeypatch, tmp_path, healthy_8000=True)
    assert rc == 2
    out = capsys.readouterr().out
    assert "down" in out, "必须提示先 bok down"
    assert _read_row(tmp_path) == before, "拒绝时不得动库"


# ---- 新库建表形状（CP create_all 幂等接管）------------------------------------


def test_fresh_db_table_shape_matches_orm_columns(monkeypatch, tmp_path) -> None:
    _run_demo_setup(monkeypatch, tmp_path)
    con = sqlite3.connect(tmp_path / "bok_voice.db")
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(global_settings)").fetchall()}
    finally:
        con.close()
    assert cols == {
        "id", "asr_json", "llm_json", "tts_json", "vad_json", "sip_json",
        "campaign_json", "sms_json", "model_routing_json", "policy", "updated_at",
    }, "建表列集=ORM GlobalSetting 全列（CP 启动 create_all/_ensure_column 零补丁）"


def test_routing_mirror_matches_contract_constants(monkeypatch, tmp_path) -> None:
    """零 bok_voice_core import 纪律下的轻量镜像对拍：车道集与 demo 档模型名/
    thinking 开关必须与共享契约消费方预期一致（值漂移在此红）。"""
    sys.path.insert(0, str(ROOT / "packages" / "core"))
    import bokctl.commands.demo_setup as demo_setup
    from bok_voice_core import model_routes  # noqa: E402

    assert tuple(demo_setup._LANES) == model_routes.LANES
    _run_demo_setup(monkeypatch, tmp_path)
    _asr, _tts, routing = _read_row(tmp_path)
    # 与 resolve_route 对拍：云端四车道解析结果即演示档预期（kill-switch 未设）。
    env: dict[str, str] = {}
    for lane in ("a_reply", "judge", "settle", "mining"):
        route = model_routes.resolve_route(lane, env, json.dumps(routing))
        cfg = routing["lanes"][lane]
        assert route.source == "routing", f"{lane} 必须吃路由表"
        assert route.provider == cfg["provider"]
        assert route.base_url == cfg["base_url"]
        assert route.model == cfg["model"]
        assert route.enable_thinking is cfg["extra"]["enable_thinking"]
    # mt=local 空 base_url：契约语义就是「不吃 local 档、回落 env 缺省链」——
    # 空环境=旧回退路径（""），环境给了 :1236 就指 :1236。
    mt_route = model_routes.resolve_route("mt", env, json.dumps(routing))
    assert mt_route.source == "env" and mt_route.base_url == "", (
        "mt local 空 base_url=回落 env 缺省链（契约语义）"
    )
    mt = model_routes.resolve_route("mt", {"MT_LLM_BASE_URL": "http://127.0.0.1:1236/v1"},
                                    json.dumps(routing))
    assert mt.provider == "local" and mt.base_url == "http://127.0.0.1:1236/v1", (
        "mt local 空 base_url=回落 env 缺省链（:1236）"
    )


# ---- 注册面（registry/patch 缝）-----------------------------------------------


def test_registry_and_patch_seams() -> None:
    import bokctl.cli

    assert bokctl.cli._COMMANDS["demo-setup"] is bokctl.commands.demo_setup
    assert callable(bokctl.commands.demo_setup.run)
    assert bok_target("cmd_demo_setup") == "bokctl.commands.demo_setup.cmd_demo_setup"
