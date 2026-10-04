"""本地 TTS sidecar 门控单测（2026-09-27，全云端形态跳过 :8788）。

`tools/bok.py::_local_tts_needed` 与 agent 装配 `effective_tts_provider`
（F-11：persona.tts_provider 覆盖 > 全局 tts.provider > 缺省 qwen3_tts）
同一条规则的单侧钉子。判据错一条 = 该起不起（通话哑）或不该起乱起
（2GB 权重白占内存），所以缺省/损坏路径必须保守=拉起（旧行为零变化）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bok  # noqa: E402
from _bokpatch import patch_bok  # noqa: E402


def _make_db(tmp_path: Path, *, provider: str | None, personas: list[str]) -> Path:
    db_path = tmp_path / "bok_voice.db"
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE global_settings (id TEXT PRIMARY KEY, tts_json TEXT)")
    tts_json = json.dumps({"provider": provider}) if provider is not None else None
    con.execute(
        "INSERT INTO global_settings (id, tts_json) VALUES ('global', ?)", (tts_json,)
    )
    con.execute("CREATE TABLE persona_profiles (tts_provider TEXT)")
    for p in personas:
        con.execute("INSERT INTO persona_profiles (tts_provider) VALUES (?)", (p,))
    con.commit()
    con.close()
    return db_path


def _gate(monkeypatch, tmp_path: Path, env: str | None = None) -> tuple[bool, str]:
    patch_bok(monkeypatch, "app_data_dir", lambda: tmp_path)
    if env is None:
        monkeypatch.delenv("BOK_LOCAL_TTS", raising=False)
    else:
        monkeypatch.setenv("BOK_LOCAL_TTS", env)
    return bok._local_tts_needed()


def test_cloud_global_minimax_skips_local(monkeypatch, tmp_path):
    """全局 minimax + 无人设覆盖 → 跳过（本仓真形态：生产恒云端 MiniMax）。"""
    _make_db(tmp_path, provider="minimax", personas=["minimax", ""])
    needed, why = _gate(monkeypatch, tmp_path)
    assert needed is False
    assert "minimax" in why


def test_empty_provider_defaults_local(monkeypatch, tmp_path):
    """provider 缺省 = qwen3_tts（本地）→ 必须拉起（旧行为零变化）。"""
    _make_db(tmp_path, provider="", personas=["", "minimax"])
    needed, _ = _gate(monkeypatch, tmp_path)
    assert needed is True


def test_none_provider_column_defaults_local(monkeypatch, tmp_path):
    _make_db(tmp_path, provider=None, personas=[])
    needed, _ = _gate(monkeypatch, tmp_path)
    assert needed is True


def test_persona_override_back_to_local_wins(monkeypatch, tmp_path):
    """全局云端但一个人设覆盖 qwen3_tts → 拉起（该人设通话走本地引擎）。"""
    _make_db(tmp_path, provider="minimax", personas=["minimax", "qwen3_tts"])
    needed, why = _gate(monkeypatch, tmp_path)
    assert needed is True
    assert "qwen3_tts" in why


def test_volcano_and_fake_count_cloud(monkeypatch, tmp_path):
    _make_db(tmp_path, provider="volcano_streaming", personas=[])
    needed, _ = _gate(monkeypatch, tmp_path)
    assert needed is False
    _make_db(tmp_path, provider="fake", personas=[])
    needed, _ = _gate(monkeypatch, tmp_path)
    assert needed is False


def test_env_forces_both_ways(monkeypatch, tmp_path):
    """BOK_LOCAL_TTS 显式优先：=1 全云端也拉（E2E 探针渲染客户话音直打 :8788）。"""
    _make_db(tmp_path, provider="minimax", personas=[])
    needed, why = _gate(monkeypatch, tmp_path, env="1")
    assert needed is True
    assert "强制" in why
    # =0 本地形态也跳过（逃生口反向）。
    _make_db(tmp_path, provider="", personas=[])
    needed, why = _gate(monkeypatch, tmp_path, env="0")
    assert needed is False
    assert "强制" in why


def test_missing_db_conservative_start(monkeypatch, tmp_path):
    """无设置库 → 拉起（保守=部署零漂移）。"""
    needed, _ = _gate(monkeypatch, tmp_path)
    assert needed is True


def test_corrupt_db_conservative_start(monkeypatch, tmp_path):
    db = tmp_path / "bok_voice.db"
    db.write_bytes(b"not a sqlite file")
    needed, _ = _gate(monkeypatch, tmp_path)
    assert needed is True


# ---- model_path 判据收紧(2026-09-28):TTS 模型目录必须有 speech_tokenizer/ ----
# LM Studio 重新下载把 Base-8bit/speech_tokenizer 整个弄丢,config.json 还在 →
# 旧判据通过 → lmstudio 残缺副本压过 app-data 完整副本 → sidecar 加载「成功」、
# 合成时才炸 `Speech tokenizer not loaded`(本地车道整通哑)。


def _mk_model_dir(base, repo, *, with_config=True, with_tokenizer=False):
    d = base / repo
    d.mkdir(parents=True, exist_ok=True)
    if with_config:
        (d / "config.json").write_text("{}", encoding="utf-8")
    if with_tokenizer:
        (d / "speech_tokenizer").mkdir(parents=True, exist_ok=True)
    return d


def test_usable_model_dir_extra_required(monkeypatch, tmp_path):
    broken = _mk_model_dir(tmp_path, "broken", with_config=True, with_tokenizer=False)
    good = _mk_model_dir(tmp_path, "good", with_config=True, with_tokenizer=True)
    assert bok._usable_model_dir(broken) is True  # 旧判据:config.json 即真
    assert bok._usable_model_dir(broken, extra_required="speech_tokenizer") is False
    assert bok._usable_model_dir(good, extra_required="speech_tokenizer") is True


def test_tts_model_path_skips_broken_lmstudio_copy(monkeypatch, tmp_path):
    # lmstudio=残缺(config.json 无 tokenizer) / app-data=完整 → 必须选 app-data
    patch_bok(monkeypatch, "is_packaged", lambda: False)
    patch_bok(monkeypatch, "is_mac", lambda: True)
    patch_bok(monkeypatch, "_lmstudio_models_dir", lambda: tmp_path / "lmstudio")
    patch_bok(monkeypatch, "model_dir", lambda repo: tmp_path / "appdata" / repo.replace("/", "--"))
    _mk_model_dir(tmp_path / "lmstudio", "mlx-community/TTS-Base", with_config=True, with_tokenizer=False)
    _mk_model_dir(tmp_path / "appdata", "mlx-community--TTS-Base", with_config=True, with_tokenizer=True)
    got = bok.model_path({"tts_clone": "mlx-community/TTS-Base"}, "tts_clone")
    assert "appdata" in got
    # 非 TTS 键不收紧(照旧 config.json 判据)
    got2 = bok.model_path({"asr": "mlx-community/TTS-Base"}, "asr")
    assert "lmstudio" in got2
