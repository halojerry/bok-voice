"""装机选型与下载子集（2026-09-20 Ubuntu 节点七步制）：CLI 面与档位语义钉。

覆盖：
- `bok.py download --only k1 k2` 的 CLI 解析与分派（argv 进 cmd_download(only=set)）；
- `--only` 提到平台表未配置的键（mt/settle/llm_4b 空值档）→ 打印 skip 说明
  且不算失败（对应功能运行时回退主模型）；
- `resolve_llm_repo` 的档位语义：BOK_LLM_TIER=4b 且表内有 llm_4b → 用 4b；
  未配置 → 告警并回退默认档。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402


class _FakeHF(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("huggingface_hub")
        self.calls: list[str] = []

    def snapshot_download(self, repo_id: str, local_dir: str = "", **kw) -> str:  # noqa: ANN003
        self.calls.append(repo_id)
        return local_dir


def _install_fake_hf(monkeypatch) -> _FakeHF:
    fake = _FakeHF()
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)
    return fake


# ---- CLI：download --only ----


def test_parse_args_download_only():
    args = bok.parse_args(["download", "--only", "asr", "llm"])
    assert args.cmd == "download"
    assert args.only == ["asr", "llm"]
    assert bok.parse_args(["download"]).only is None


def test_main_dispatch_passes_only_to_download(monkeypatch):
    captured: dict = {}

    def fake_download(only=None):
        captured["only"] = only
        return 0

    monkeypatch.setattr(bok, "cmd_download", fake_download)
    assert bok.main(["download", "--only", "asr", "settle"]) == 0
    assert captured["only"] == {"asr", "settle"}
    assert bok.main(["download"]) == 0
    assert captured["only"] is None


# ---- 下载子集语义 ----


def test_cmd_download_only_filters_table(monkeypatch, tmp_path: Path):
    fake = _install_fake_hf(monkeypatch)
    monkeypatch.setattr(bok._platform, "system", lambda: "Linux")
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(bok, "_enable_hf_transfer", lambda: None)
    rc = bok.cmd_download(only={"asr"})
    assert rc == 0
    assert fake.calls == ["Qwen/Qwen3-ASR-1.7B"]  # 只下 asr 一项


def test_cmd_download_only_reports_unconfigured(capsys, monkeypatch, tmp_path: Path):
    fake = _install_fake_hf(monkeypatch)
    monkeypatch.setattr(bok._platform, "system", lambda: "Linux")
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(bok, "_enable_hf_transfer", lambda: None)
    rc = bok.cmd_download(only={"asr", "mt", "settle"})
    out = capsys.readouterr().out
    assert rc == 0
    # 非 mac 表当前无 mt/settle：逐项说明并跳过，不算失败
    assert "[skip] mt" in out and "[skip] settle" in out
    assert fake.calls == ["Qwen/Qwen3-ASR-1.7B"]


# ---- 档位：BOK_LLM_TIER ----


def test_resolve_llm_repo_tier_4b_configured(monkeypatch):
    monkeypatch.setattr(bok, "_settings_llm_local_model", lambda: "")
    monkeypatch.setenv("BOK_LLM_TIER", "4b")
    table = {"llm": "org/9B-GGUF", "llm_4b": "org/4B-GGUF"}
    assert bok.resolve_llm_repo(table) == "org/4B-GGUF"


def test_resolve_llm_repo_tier_unconfigured_falls_back(capsys, monkeypatch):
    monkeypatch.setattr(bok, "_settings_llm_local_model", lambda: "")
    monkeypatch.setenv("BOK_LLM_TIER", "4b")
    table = {"llm": "org/9B-GGUF", "llm_4b": ""}
    assert bok.resolve_llm_repo(table) == "org/9B-GGUF"
    assert "回退默认档" in capsys.readouterr().err


def test_resolve_llm_repo_settings_override_wins(monkeypatch):
    monkeypatch.setattr(bok, "_settings_llm_local_model", lambda: "org/custom")
    monkeypatch.setenv("BOK_LLM_TIER", "4b")
    table = {"llm": "org/9B-GGUF", "llm_4b": "org/4B-GGUF"}
    assert bok.resolve_llm_repo(table) == "org/custom"
