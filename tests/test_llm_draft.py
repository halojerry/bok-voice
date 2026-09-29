"""Draft 模型 speculative decoding 管线单测(2026-09-25,默认关)。

契约:BOK_LLM_DRAFT 默认 "0"——mlx_lm server 命令行**逐字节同旧**(无
--draft-model),prompt-cache-bytes 恒 4GB(P1.d 定档),draft 权重不随全量下载拉取;
="1" 且 draft 模型在盘才追加 --draft-model/--num-draft-tokens 3 两旗并把
cache 折到 3.5GB;模型缺席=无 draft 起服务不 fail,doctor 只出一行警告不进
fails。全部离线:不依赖真实模型在盘、不触网、不起进程。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bok  # noqa: E402

_DRAFT_REPO = "mlx-community/Qwen3-0.6B-4bit"


def _clear_draft_env(monkeypatch) -> None:
    for key in ("BOK_LLM_DRAFT", "BOK_LLM_DRAFT_MODEL",
                "BOK_LLM_PROMPT_CACHE_BYTES", "BOK_DEMO_PRESET"):
        monkeypatch.delenv(key, raising=False)


def test_draft_off_flags_empty(monkeypatch):
    """默认关(未设/"0")→ 旗标恒空——模型在盘也不追加(开关优先于在盘)。"""
    _clear_draft_env(monkeypatch)
    assert bok._llm_draft_flags({}) == []
    monkeypatch.setenv("BOK_LLM_DRAFT", "0")
    assert bok._llm_draft_flags({"llm_draft": _DRAFT_REPO}) == []


def test_draft_on_model_present_appends_flags(monkeypatch, tmp_path):
    """BOK_LLM_DRAFT=1 + 模型在盘 → 精确追加两旗(--num-draft-tokens 取默认 3)。"""
    _clear_draft_env(monkeypatch)
    monkeypatch.setenv("BOK_LLM_DRAFT", "1")
    monkeypatch.setenv("BOK_LLM_DRAFT_MODEL", str(tmp_path))
    assert bok._llm_draft_flags({"llm_draft": _DRAFT_REPO}) == [
        "--draft-model", str(tmp_path), "--num-draft-tokens", "3",
    ]


def test_draft_on_model_absent_no_flags(monkeypatch, tmp_path, capsys):
    """BOK_LLM_DRAFT=1 但模型缺席 → 空旗标(无 draft 起服务,不 fail)+一行明示。"""
    _clear_draft_env(monkeypatch)
    monkeypatch.setenv("BOK_LLM_DRAFT", "1")
    monkeypatch.setenv("BOK_LLM_DRAFT_MODEL", str(tmp_path / "not-on-disk"))
    assert bok._llm_draft_flags({"llm_draft": _DRAFT_REPO}) == []
    assert "draft" in capsys.readouterr().err


def test_draft_off_argv_byte_identical(monkeypatch):
    """draft 关 → mlx server 命令行逐字节同旧(钉死默认档零漂移)。"""
    _clear_draft_env(monkeypatch)
    argv = bok._mac_llm_server_argv(
        Path("py"), "/models/main-4b", "1239", {"llm_draft": _DRAFT_REPO})
    assert argv == [
        "py", "-m", "mlx_lm", "server",
        "--model", "/models/main-4b", "--host", "127.0.0.1", "--port", "1239",
        "--prompt-cache-size", "128",
        "--prompt-cache-bytes", "4GB",
        "--prefill-step-size", "512",
        "--chat-template-args", '{"enable_thinking":false}',
        "--log-level", "INFO",
    ]


def test_draft_on_argv_tail_and_cache_discount(monkeypatch, tmp_path):
    """draft 开 → argv 尾部追加两旗,cache-bytes 落 3.5GB(同一条命令行内一致,P1.d 折档)。"""
    _clear_draft_env(monkeypatch)
    monkeypatch.setenv("BOK_LLM_DRAFT", "1")
    monkeypatch.setenv("BOK_LLM_DRAFT_MODEL", str(tmp_path))
    argv = bok._mac_llm_server_argv(Path("py"), "/m", "1239", {})
    assert argv[-4:] == [
        "--draft-model", str(tmp_path), "--num-draft-tokens", "3",
    ]
    assert argv[-6:-4] == ["--log-level", "INFO"]
    assert argv[argv.index("--prompt-cache-bytes") + 1] == "3.5GB"


def test_cache_bytes_explicit_env_never_discounted(monkeypatch):
    """显式 BOK_LLM_PROMPT_CACHE_BYTES 最优先:draft 开也不折(用户直设=专家值)。"""
    _clear_draft_env(monkeypatch)
    monkeypatch.setenv("BOK_LLM_PROMPT_CACHE_BYTES", "12GB")
    assert bok._default_prompt_cache_bytes(draft_on=True) == "12GB"
    assert bok._default_prompt_cache_bytes(draft_on=False) == "12GB"


def test_cache_bytes_draft_discount_pairing(monkeypatch):
    """无显式 env:draft 开=3.5GB(权重+KV ~0.5GB 腾挪位),关=4GB。

    2026-09-29 v2 P1.d 定档：8 连打探针实测每通 cache +0.35-0.45GB，6GB 上限
    第 8-10 通打穿进 LRU 换页（生成段 tps 崩 2.6 实证）；4GB=日常 8-10 通
    工作集零换页。要回 6GB：env 显式覆盖。"""
    _clear_draft_env(monkeypatch)
    assert bok._default_prompt_cache_bytes(draft_on=False) == "4GB"
    assert bok._default_prompt_cache_bytes(draft_on=True) == "3.5GB"


def test_models_table_llm_draft_registration():
    """MODELS 表登记 + 可选增强语义:向导不门禁;windows 表(llama.cpp 后端)无此键。"""
    assert bok.MODELS["mac"].get("llm_draft") == _DRAFT_REPO
    assert "llm_draft" in bok.OPTIONAL_MODELS
    assert not bok.MODELS["windows"].get("llm_draft")


def test_draft_model_path_resolves_usable_layout(monkeypatch, tmp_path):
    """model_path 解析:mac lmstudio 布局在盘(config.json 在)→ 返回该路径。"""
    repo_dir = tmp_path / "mlx-community" / "Qwen3-0.6B-4bit"
    repo_dir.mkdir(parents=True)
    (repo_dir / "config.json").write_text("{}")
    monkeypatch.setenv("LMSTUDIO_MODELS_DIR", str(tmp_path))
    monkeypatch.setattr(bok, "is_packaged", lambda: False)
    monkeypatch.setattr(bok, "is_mac", lambda: True)
    monkeypatch.setenv("BOK_LLM_DRAFT_MODEL", "")
    resolved = bok._llm_draft_model({"llm_draft": _DRAFT_REPO})
    assert resolved == str(repo_dir)


def test_doctor_draft_warning_states(monkeypatch):
    """doctor 判定函数三态:默认关=""(零输出);开+缺席=警告文案;开+在盘=""。"""
    _clear_draft_env(monkeypatch)
    table = {"llm_draft": _DRAFT_REPO}
    assert bok._doctor_draft_warning(table) == ""

    monkeypatch.setenv("BOK_LLM_DRAFT", "1")
    monkeypatch.setattr(bok, "_model_present", lambda repo: False)
    warn = bok._doctor_draft_warning(table)
    assert "download --only llm_draft" in warn
    assert _DRAFT_REPO in warn

    monkeypatch.setattr(bok, "_model_present", lambda repo: True)
    assert bok._doctor_draft_warning(table) == ""
    # 表无条目(如 windows 表)同回 "",不炸。
    assert bok._doctor_draft_warning({}) == ""


def test_doctor_draft_warning_never_enters_fails(monkeypatch, capsys):
    """警告只进打印面:非 packaged 下 cmd_doctor 不因它追加 fails——用
    「doctor 末行语义」判:消息出现而末行不是 'doctor: warnings'(fails 空时
    应为 'doctor: OK')。真跑 cmd_doctor 需钉住全部探测面(端口/导入),此处
    改为源级钉死接线:调用点只 print、无 fails.append(_doctor_draft_warning)。"""
    src = (Path(__file__).resolve().parents[1] / "tools" / "bok.py").read_text(
        encoding="utf-8")
    assert "fails.append(_doctor_draft_warning" not in src
    assert "fails.append(draft_warn" not in src
    call_site = "draft_warn = _doctor_draft_warning(current)"
    assert call_site in src


def test_download_draft_gate_opt_in(monkeypatch, tmp_path):
    """下载闸:默认关 → 全量下载不拉 draft 权重;--only llm_draft 或
    BOK_LLM_DRAFT=1 才拉(fake hub 记账,零网络)。"""
    calls: list[str] = []

    def _fake_download(repo_id: str, local_dir: str, **kwargs) -> None:
        calls.append(repo_id)

    fake_hub = types.SimpleNamespace(snapshot_download=_fake_download)
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)
    monkeypatch.setattr(bok, "model_dir", lambda repo: tmp_path / "never" / repo)

    _clear_draft_env(monkeypatch)
    assert bok.cmd_download() == 0
    assert _DRAFT_REPO not in calls

    monkeypatch.setenv("BOK_LLM_DRAFT", "1")
    assert bok.cmd_download() == 0
    assert calls.count(_DRAFT_REPO) == 1

    monkeypatch.delenv("BOK_LLM_DRAFT", raising=False)
    assert bok.cmd_download(only={"llm_draft"}) == 0
    assert calls.count(_DRAFT_REPO) == 2
