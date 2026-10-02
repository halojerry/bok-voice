"""模型路径解析：**空壳目录不得遮蔽真模型**（2026-09-21 实弹收口）。

现场：`~/.lmstudio/models/mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit` 只剩一个
`.cache/` 空壳（别家工具建的空目录），而 app-data 里那份是好的。旧判据 `dir.exists()`
认空壳为真并**优先**返回它 → TTS sidecar 加载失败 → 探针拿到 **0 字节客户音频**
（HTTP 200！）→ 表象是「agent 听不到客户、整通全哑」，三个 E2E 探针连带失败。

判据改为 `_usable_model_dir`（要 `config.json`）。本文件钉住四档，防回潮。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("bok_tool", _ROOT / "tools" / "bok.py")
bok = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bok)

REPO = "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit"


def _make_model(root: Path, repo: str) -> Path:
    """造一份最小可加载模型（本判据只认 config.json 这个入口）。"""
    d = root / repo
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps({"model_type": "qwen3_tts"}), encoding="utf-8")
    return d


def _make_shell(root: Path, repo: str) -> Path:
    """造一个空壳：目录在、非空（有 .cache/），但**没有 config.json**。"""
    d = root / repo
    (d / ".cache").mkdir(parents=True, exist_ok=True)
    return d


def _wire(monkeypatch, tmp_path) -> tuple[Path, Path]:
    lm_root = tmp_path / "lmstudio"
    app_root = tmp_path / "appdata"
    lm_root.mkdir()
    app_root.mkdir()
    monkeypatch.setattr(bok, "is_mac", lambda: True)
    monkeypatch.setattr(bok, "is_packaged", lambda: False)
    monkeypatch.setattr(bok, "_lmstudio_models_dir", lambda: lm_root)
    monkeypatch.setattr(bok, "app_data_dir", lambda: app_root)
    return lm_root, app_root


def test_shell_shadowing_appdata_must_not_win(monkeypatch, tmp_path):
    """本缺陷的核心判例：lmstudio 空壳 + app-data 真模型 → 取 app-data。"""
    lm_root, app_root = _wire(monkeypatch, tmp_path)
    _make_shell(lm_root, REPO)
    good = _make_model(app_root / "models", REPO.replace("/", "--"))
    assert bok.model_path({"m": REPO}, "m") == str(good)


def test_usable_lmstudio_still_wins(monkeypatch, tmp_path):
    """优先级未变：两边都能加载时仍取 lmstudio（mac dev 惯例）。"""
    lm_root, app_root = _wire(monkeypatch, tmp_path)
    good_lm = _make_model(lm_root, REPO)
    _make_model(app_root / "models", REPO.replace("/", "--"))
    assert bok.model_path({"m": REPO}, "m") == str(good_lm)


def test_shell_alone_falls_back_to_lmstudio_path(monkeypatch, tmp_path):
    """两边都没有可用模型 → 仍旧返回 lmstudio 路径（报错信息与旧版一致）。"""
    lm_root, app_root = _wire(monkeypatch, tmp_path)
    shell = _make_shell(lm_root, REPO)
    assert bok.model_path({"m": REPO}, "m") == str(shell)


def test_packaged_mode_ignores_lmstudio(monkeypatch, tmp_path):
    """packaged 档恒走 app-data（判据不参与）。"""
    lm_root, app_root = _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(bok, "is_packaged", lambda: True)
    _make_model(lm_root, REPO)
    good = _make_model(app_root / "models", REPO.replace("/", "--"))
    assert bok.model_path({"m": REPO}, "m") == str(good)


def test_usable_predicate_requires_config_json(tmp_path):
    """判据本体：空壳（只有 .cache/）不算可用，哪怕目录非空。"""
    shell = _make_shell(tmp_path, REPO)
    assert shell.exists() and any(shell.iterdir())  # 旧判据会在这里判真
    assert bok._usable_model_dir(shell) is False
    good = _make_model(tmp_path, REPO)
    assert bok._usable_model_dir(good) is True
