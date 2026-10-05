"""云端演示档姿势单测（2026-10-05 demo-cloud wave）。

`servers._cloud_posture` 读设置库（asr_json/model_routing_json）判 serve 侧
云/本地姿势，`_cmd_up_services` 按姿势收窄 ensure 与云腿拉起：
- 保守法：DB 缺失/损坏/列缺 → 全本地（旧行为零变化）；
- worker 行为真相优先：BOK_DOUBAO_ASR=0 / BOK_MODEL_ROUTING=0 → 判本地
  （worker 会回退本地件，serve 不得跳过）；
- BOK_LOCAL_ASR / BOK_LOCAL_LLM 是 serve 侧闸（worker 不读、不进 _FORWARD_ENV）；
- ensure 收窄：全本地=cmd_download() 整表（only=None 逐字节同旧）；demo 档
  恰为 {"mt"}；空收窄集必须整支跳过（cmd_download(only=set()) 语义=整表）。

约定：tmp sqlite fixture + _start_proc/cmd_download 打桩，不碰真进程、不写
真设置库（只读姿势同 tests/test_local_tts_gate.py 先例）。
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

# ---- fixtures ---------------------------------------------------------------

_DEEPSEEK = {
    "provider": "openai",
    "base_url": "https://api.deepseek.example/v1",
    "model": "deepseek-flash",
}
_DOUBAO_ASR = {"provider": "doubao", "api_key": "tok-test"}
_DEMO_LANES = {
    "a_reply": dict(_DEEPSEEK),
    "judge": dict(_DEEPSEEK),
    "settle": dict(_DEEPSEEK),
    "mining": dict(_DEEPSEEK),
}
_POSTURE_DEMO = {
    "asr_cloud": True,
    "asr_why": "asr.provider=doubao(云凭据在场)",
    "llm_cloud": True,
    "llm_why": "路由 a_reply/judge/settle 全 openai",
    "settle_cloud": True,
    "settle_why": "路由 settle=openai",
    "mt_local": True,
    "mt_why": "路由 mt=local",
}
_ENV_KEYS = ("BOK_LOCAL_ASR", "BOK_LOCAL_LLM", "BOK_MODEL_ROUTING", "BOK_DOUBAO_ASR")


def _make_db(tmp_path: Path, *, asr=None, routing=None) -> Path:
    db_path = tmp_path / "bok_voice.db"
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE global_settings (id TEXT PRIMARY KEY, asr_json TEXT,"
        " model_routing_json TEXT)"
    )
    con.execute(
        "INSERT INTO global_settings (id, asr_json, model_routing_json)"
        " VALUES ('global', ?, ?)",
        (
            asr if isinstance(asr, str) or asr is None else json.dumps(asr),
            routing if isinstance(routing, str) or routing is None else json.dumps(routing),
        ),
    )
    con.commit()
    con.close()
    return db_path


def _posture(
    monkeypatch, tmp_path: Path, *, asr=None, routing=None, env=None, make_db=True
) -> dict:
    patch_bok(monkeypatch, "app_data_dir", lambda: tmp_path)
    for key in _ENV_KEYS:
        val = (env or {}).get(key)
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)
    if make_db:
        _make_db(tmp_path, asr=asr, routing=routing)
    return bok.servers._cloud_posture()


# ---- 姿势判定矩阵（_cloud_posture）------------------------------------------


def test_demo_cloud_full_posture(monkeypatch, tmp_path):
    """演示档全形：豆包 ASR + 路由全云 + mt 车道缺席 → 全云腿亮、mt=本地。"""
    p = _posture(monkeypatch, tmp_path, asr=_DOUBAO_ASR, routing={"lanes": _DEMO_LANES})
    assert p["asr_cloud"] is True and "doubao" in p["asr_why"]
    assert p["llm_cloud"] is True and p["settle_cloud"] is True
    assert p["mt_local"] is True, "mt 车道缺席=本地 :1236（B 线 MT 车道）"


def test_doubao_old_style_auth_counts_cloud(monkeypatch, tmp_path):
    """旧式鉴权（app_id+access_token 齐）与新式单 key 等价（agent 装配同判）。"""
    p = _posture(
        monkeypatch,
        tmp_path,
        asr={"provider": "doubao", "app_id": "aid", "access_token": "tok"},
    )
    assert p["asr_cloud"] is True


def test_doubao_alias_provider_counts_cloud(monkeypatch, tmp_path):
    p = _posture(monkeypatch, tmp_path, asr={"provider": "doubao_asr", "api_key": "k"})
    assert p["asr_cloud"] is True


def test_doubao_without_credentials_stays_local(monkeypatch, tmp_path):
    """豆包档但凭据空 → agent 装配会回退本地，serve 必须照常拉 :8787。"""
    p = _posture(monkeypatch, tmp_path, asr={"provider": "doubao", "api_key": ""})
    assert p["asr_cloud"] is False and "缺凭据" in p["asr_why"]


def test_missing_asr_and_routing_json_stay_local(monkeypatch, tmp_path):
    p = _posture(monkeypatch, tmp_path, asr=None, routing=None)
    assert p["asr_cloud"] is False
    assert p["llm_cloud"] is False and p["settle_cloud"] is False
    assert p["mt_local"] is True


def test_local_asr_provider_stays_local(monkeypatch, tmp_path):
    p = _posture(monkeypatch, tmp_path, asr={"provider": "qwen3_asr"})
    assert p["asr_cloud"] is False


def test_mixed_routing_llm_local_settle_cloud(monkeypatch, tmp_path):
    """a_reply 云 + judge 本地 → :1235 必须拉起；settle 单独可云。"""
    lanes = {
        "a_reply": dict(_DEEPSEEK),
        "judge": {"provider": "local", "base_url": "http://127.0.0.1:1235/v1"},
        "settle": dict(_DEEPSEEK),
    }
    p = _posture(monkeypatch, tmp_path, routing={"lanes": lanes})
    assert p["llm_cloud"] is False
    assert p["settle_cloud"] is True


def test_unknown_lane_provider_collapses_local(monkeypatch, tmp_path):
    """未知 provider 坍缩 local（model_routes._normalize_lane 同款）。"""
    lanes = {
        "a_reply": {"provider": "weird"},
        "judge": {"provider": "weird"},
        "settle": {"provider": "weird"},
    }
    p = _posture(monkeypatch, tmp_path, routing={"lanes": lanes})
    assert p["llm_cloud"] is False


def test_mt_lane_openai_is_not_local(monkeypatch, tmp_path):
    p = _posture(monkeypatch, tmp_path, routing={"lanes": {"mt": dict(_DEEPSEEK)}})
    assert p["mt_local"] is False


def test_routing_kill_switch_forces_local(monkeypatch, tmp_path):
    """BOK_MODEL_ROUTING=0 → 路由表忽略（env 缺省链=本地），即使设置面全云。"""
    p = _posture(
        monkeypatch,
        tmp_path,
        asr=_DOUBAO_ASR,
        routing={"lanes": _DEMO_LANES},
        env={"BOK_MODEL_ROUTING": "0"},
    )
    assert p["llm_cloud"] is False and p["settle_cloud"] is False
    assert p["mt_local"] is True
    assert p["asr_cloud"] is True, "LLM kill-switch 不波及 ASR 腿"


def test_malformed_routing_json_conservative_local(monkeypatch, tmp_path):
    p = _posture(monkeypatch, tmp_path, routing="not-json{")
    assert p["llm_cloud"] is False and p["settle_cloud"] is False


def test_local_asr_env_overrides_both_ways(monkeypatch, tmp_path):
    p = _posture(
        monkeypatch, tmp_path, asr=_DOUBAO_ASR, env={"BOK_LOCAL_ASR": "1"}
    )
    assert p["asr_cloud"] is False and "强制本地" in p["asr_why"]
    p = _posture(
        monkeypatch,
        tmp_path,
        asr={"provider": "qwen3_asr"},
        env={"BOK_LOCAL_ASR": "0"},
    )
    assert p["asr_cloud"] is True and "强制" in p["asr_why"]


def test_doubao_kill_switch_beats_cloud_settings(monkeypatch, tmp_path):
    """BOK_DOUBAO_ASR=0：worker 总闸回退本地 → 设置面写了豆包也判本地。"""
    p = _posture(monkeypatch, tmp_path, asr=_DOUBAO_ASR, env={"BOK_DOUBAO_ASR": "0"})
    assert p["asr_cloud"] is False


def test_local_llm_env_overrides_both_ways(monkeypatch, tmp_path):
    p = _posture(
        monkeypatch,
        tmp_path,
        routing={"lanes": _DEMO_LANES},
        env={"BOK_LOCAL_LLM": "1"},
    )
    assert p["llm_cloud"] is False and p["settle_cloud"] is False
    p = _posture(monkeypatch, tmp_path, routing=None, env={"BOK_LOCAL_LLM": "0"})
    assert p["llm_cloud"] is True and p["settle_cloud"] is True


def test_missing_db_conservative_local(monkeypatch, tmp_path):
    """无设置库 → 全本地（保守=旧行为零变化）。"""
    p = _posture(monkeypatch, tmp_path, make_db=False)
    assert p["asr_cloud"] is False and p["llm_cloud"] is False
    assert p["settle_cloud"] is False and p["mt_local"] is True


def test_corrupt_db_conservative_local(monkeypatch, tmp_path):
    (tmp_path / "bok_voice.db").write_bytes(b"not a sqlite file")
    p = _posture(monkeypatch, tmp_path, make_db=False)
    assert p["asr_cloud"] is False and p["llm_cloud"] is False
    assert "读取失败" in p["asr_why"]


# ---- ensure 收窄（_cmd_up_services × _ensure_only_for_posture）---------------


def _run_up(monkeypatch, tmp_path, *, posture, tts_needed, started=None, stub_llm=True,
            sidecar_exists=True):
    calls: dict = {}
    patch_bok(monkeypatch, "app_data_dir", lambda: tmp_path)
    patch_bok(monkeypatch, "is_mac", lambda: True)  # 钉 mac 模型表（llm_4b 不在表）
    patch_bok(
        monkeypatch, "cmd_download", lambda only=None: calls.setdefault("only", only) or 0
    )
    sidecar = tmp_path / "sidecar-py"
    if sidecar_exists:
        sidecar.write_text("")
    patch_bok(monkeypatch, "sidecar_python", lambda name: sidecar)
    patch_bok(monkeypatch, "model_path", lambda cur, key: str(tmp_path / key))
    patch_bok(monkeypatch, "healthy", lambda port: False)
    monkeypatch.setattr(bok.time, "sleep", lambda s: None)

    def fake_start(args, pidfile, logfile, env=None, cwd=None):
        if started is not None:
            started.append(" ".join(str(a) for a in args))
        return 1

    patch_bok(monkeypatch, "_start_proc", fake_start)
    patch_bok(monkeypatch, "_local_tts_needed", lambda: (tts_needed, "test"))
    patch_bok(monkeypatch, "_cloud_posture", lambda: posture)
    if stub_llm:
        patch_bok(monkeypatch, "_start_llm", lambda *a, **k: None)
        patch_bok(monkeypatch, "_start_settle_llm", lambda *a, **k: False)
    patch_bok(monkeypatch, "_start_mt_llm", lambda *a, **k: False)
    patch_bok(monkeypatch, "_start_laya", lambda *a, **k: False)
    patch_bok(monkeypatch, "_ports_down_after_grace", lambda targets, probe=None: [])
    rc = bok.servers._cmd_up_services(models_only=True)
    return rc, calls


def test_ensure_full_local_unchanged_full_table(monkeypatch, tmp_path):
    """全本地形状：整表 ensure（only=None）——既有本地用户逐字节零漂移。"""
    posture = {
        "asr_cloud": False, "asr_why": "",
        "llm_cloud": False, "llm_why": "",
        "settle_cloud": False, "settle_why": "",
        "mt_local": True, "mt_why": "",
    }
    rc, calls = _run_up(monkeypatch, tmp_path, posture=posture, tts_needed=True)
    assert rc == 0
    assert calls["only"] is None, "全本地必须走无参 cmd_download()（旧行为）"


def test_ensure_demo_posture_narrows_to_mt(monkeypatch, tmp_path):
    """演示档：ASR/LLM/settle/TTS 全云、MT 本地 → ensure 恰为 {"mt"}。"""
    rc, calls = _run_up(
        monkeypatch, tmp_path, posture=dict(_POSTURE_DEMO), tts_needed=False
    )
    assert rc == 0
    assert calls["only"] == {"mt"}


def test_ensure_asr_cloud_only_mixed(monkeypatch, tmp_path):
    """混合姿势：仅 ASR 上云（LLM/settle/TTS/mt 本地）→ asr/sensevoice 不下。"""
    posture = dict(_POSTURE_DEMO, llm_cloud=False, llm_why="", settle_cloud=False, settle_why="")
    rc, calls = _run_up(monkeypatch, tmp_path, posture=posture, tts_needed=True)
    assert rc == 0
    assert calls["only"] == {"tts_preset", "tts_clone", "llm", "settle", "mt"}


def test_ensure_all_cloud_mt_cloud_empty_set_skips_download(monkeypatch, tmp_path):
    """空收窄集必须整支跳过：cmd_download(only=set()) 的语义是「整表」，
    直接传会把全云姿势反向变全量下载。"""
    posture = dict(_POSTURE_DEMO, mt_local=False)
    rc, calls = _run_up(
        monkeypatch, tmp_path, posture=posture, tts_needed=False
    )
    assert rc == 0
    assert "only" not in calls, "空收窄集不得触发任何下载调用"


def test_ensure_never_includes_on_disk_optional_keys(monkeypatch, tmp_path):
    """embedding/laya/llm_draft 恒不进收窄集（盘上可选，服务侧「在盘才起」自洽）。"""
    posture = {
        "asr_cloud": False, "asr_why": "",
        "llm_cloud": False, "llm_why": "",
        "settle_cloud": False, "settle_why": "",
        "mt_local": True, "mt_why": "",
    }
    only = bok.servers._ensure_only_for_posture(posture, tts_needed=True)
    assert "embedding" not in only and "laya" not in only and "llm_draft" not in only


def test_sidecar_venv_check_gated_by_posture(monkeypatch, tmp_path, capsys):
    """2026-10-05 真栈实弹发现的回归钉：sidecar venv 存在性检查只对「本姿势
    真要拉起」的 sidecar 生效——云 ASR 档不要求 :8787 的 venv 在盘（干净
    worktree/演示新机不被用不上的 venv 卡 exit 2）；本地姿势缺 venv 照旧
    exit 2（旧行为不豁免）。"""
    # 云姿势：asr=cloud + tts=cloud → 两个 sidecar 都不需要 → 不因缺失 exit 2
    rc, _ = _run_up(monkeypatch, tmp_path, posture=dict(_POSTURE_DEMO),
                    tts_needed=False, sidecar_exists=False)
    assert rc == 0, "云腿对应的 sidecar venv 缺失不得阻断 serve"
    # 本地姿势：asr 要本地而 venv 缺 → 照旧 exit 2
    local_posture = {
        "asr_cloud": False, "asr_why": "",
        "llm_cloud": False, "llm_why": "",
        "settle_cloud": False, "settle_why": "",
        "mt_local": True, "mt_why": "",
    }
    rc2, _ = _run_up(monkeypatch, tmp_path, posture=local_posture,
                     tts_needed=True, sidecar_exists=False)
    assert rc2 == 2, "本地姿势缺 sidecar venv 必须照旧 exit 2（run setup 提示）"


def test_demo_posture_skips_asr_llm_settle_start_blocks(monkeypatch, tmp_path, capsys):
    """演示档：:8787/:1235/:1237 三个云腿拉起块全跳过 + skip 行如实打印。"""
    started: list[str] = []
    rc, calls = _run_up(
        monkeypatch,
        tmp_path,
        posture=dict(_POSTURE_DEMO),
        tts_needed=False,
        started=started,
        stub_llm=False,
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "asr sidecar :8787 skipped (cloud: asr.provider=doubao" in captured.out
    assert "llm :1235 skipped (cloud:" in captured.out
    assert "asr=skipped(cloud)" in captured.out, "ready 行如实标 skipped"
    assert "settle lane :1237 skipped (cloud:" in captured.err
    assert not any("--port 8787" in s for s in started)
    assert not any("--port 1235" in s for s in started)
    assert not any("--port 1237" in s for s in started)
    assert calls["only"] == {"mt"}


# ---- status 云腿 skipped 语义 ------------------------------------------------


def _patch_status_common(monkeypatch) -> None:
    patch_bok(monkeypatch, "healthy", lambda port: False)
    patch_bok(monkeypatch, "_worker_ports", lambda: ())
    patch_bok(monkeypatch, "_provider_health_summary", lambda *a, **k: None)


def test_status_cloud_rows_skipped_not_down(monkeypatch, tmp_path, capsys):
    patch_bok(monkeypatch, "app_data_dir", lambda: tmp_path)
    patch_bok(monkeypatch, "_cloud_posture", lambda: dict(_POSTURE_DEMO))
    patch_bok(
        monkeypatch, "_local_tts_needed", lambda: (False, "全云端 tts.provider=minimax")
    )
    _patch_status_common(monkeypatch)
    bok.commands.status.cmd_status()
    out = capsys.readouterr().out
    asr_lines = [row for row in out.splitlines() if row.strip().startswith("asr ")]
    assert asr_lines, "asr 行必须在场"
    assert all("skipped (cloud:" in row and "DOWN" not in row for row in asr_lines)
    llm_lines = [row for row in out.splitlines() if row.strip().startswith("llm ")]
    assert llm_lines, "llm 行必须在场"
    assert all("skipped (cloud:" in row and "DOWN" not in row for row in llm_lines)


def test_status_local_posture_still_down(monkeypatch, tmp_path, capsys):
    """posture 判本地时 :8787/:1235 缺席照旧 DOWN——只豁免云腿。"""
    posture = dict(_POSTURE_DEMO, asr_cloud=False, asr_why="", llm_cloud=False, llm_why="")
    patch_bok(monkeypatch, "app_data_dir", lambda: tmp_path)
    patch_bok(monkeypatch, "_cloud_posture", lambda: posture)
    patch_bok(monkeypatch, "_local_tts_needed", lambda: (True, "local"))
    _patch_status_common(monkeypatch)
    bok.commands.status.cmd_status()
    out = capsys.readouterr().out
    asr_lines = [row for row in out.splitlines() if row.strip().startswith("asr ")]
    llm_lines = [row for row in out.splitlines() if row.strip().startswith("llm ")]
    assert asr_lines and all("DOWN" in row for row in asr_lines)
    assert llm_lines and all("DOWN" in row for row in llm_lines)


# ---- patch 缝登记（防未来 patch 静默 default-to-core 假绿）--------------------


def test_patch_target_registration():
    assert bok_target("_cloud_posture") == "bokctl.servers._cloud_posture"
