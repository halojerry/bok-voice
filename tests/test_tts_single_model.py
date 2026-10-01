"""TTS sidecar 单模型条件加载 + worker 空闲池收编单测（2026-10-02 内存瘦身 P-5C）。

背景（内存普查实测）：TTS sidecar :8788 常驻 6.67GB——启动时同线程 eager 双载
preset+clone 两个 1.7B-8bit（各 ~2.9GB）。本机 A 线生产走 MiniMax 云，本地 TTS
只服务探针/克隆音色车道：**registry 为空（无注册克隆音色）时 clone 纯属浪费
~2.9GB**。修法=启动期条件加载（绝无运行期懒加载——uvicorn 线程池懒加载在 MPS
上 segfault，见 app.py `load()` 注释）：

- registry 非空 → 双载；registry 空 → 只载 preset；`BOK_TTS_BOTH_MODELS=1`
  强制双载（逃生键，回旧行为）。
- `ensure_loaded` 按需化：注册 voice（∈registry）→ 要 clone；其余 → 只要 preset。
  clone 未载而被请求 → 503 人话（带 BOK_TTS_BOTH_MODELS=1 与 /v1/voices/register）。
- livekit worker 空闲子进程池：框架缺省 prod=min(cpu,4)（本机 3 worker=12 个
  空转 idle ≈3.1GB，框架无 env 旋钮），两入口 WorkerOptions 显式 `num_idle_processes=1`。

harness 沿 test_local_cantonese_lane.py：sidecar 目录进 sys.path + fastapi
importorskip；app.py 的 mlx/torch 重依赖全在函数体内 import，测试用假模块注入
（monkeypatch sys.modules），不起 uvicorn、不碰真模型、不碰端口。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "qwen3-tts-sidecar"))

pytest.importorskip("fastapi")  # sidecar 依赖 fastapi；缺失则整模块跳过
tts_app = importlib.import_module("app")


# ---------- helpers ----------


def _write_registry(path: Path, voices: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                v: {
                    "voice_id": v,
                    "ref_audio": f"/tmp/{v}.wav",
                    "ref_text": "你好。",
                    "language": "zh",
                }
                for v in voices
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def registry_path(monkeypatch, tmp_path: Path) -> Path:
    """隔离 sidecar 模块级状态：registry 路径 / 后端 / 加载 env 全由用例定。"""
    path = tmp_path / "voice_registry.json"
    monkeypatch.setattr(tts_app, "VOICE_REGISTRY_PATH", path)
    monkeypatch.setattr(tts_app, "BACKEND", "mlx")
    monkeypatch.delenv("QWEN3_TTS_DISABLE_LOAD", raising=False)
    monkeypatch.delenv("BOK_TTS_BOTH_MODELS", raising=False)
    monkeypatch.setenv("QWEN3_TTS_WARMUP", "0")  # 假模型不跑 warmup
    return path


def _install_fake_mlx(monkeypatch) -> list[str]:
    """注入假 mlx_audio.tts.utils.load_model，返回调用账本。"""
    calls: list[str] = []

    def _fake_load_model(repo: str):
        calls.append(repo)
        return f"fake:{repo}"

    mod = types.ModuleType("mlx_audio.tts.utils")
    mod.load_model = _fake_load_model  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", types.ModuleType("mlx_audio"))
    monkeypatch.setitem(sys.modules, "mlx_audio.tts", types.ModuleType("mlx_audio.tts"))
    monkeypatch.setitem(sys.modules, "mlx_audio.tts.utils", mod)
    return calls


def _install_fake_torch_stack(monkeypatch) -> list[str]:
    """注入假 torch + qwen_tts.Qwen3TTSModel，返回 from_pretrained 调用账本。"""
    calls: list[str] = []

    class _FakeModel:
        def __init__(self, repo: str) -> None:
            self.repo = repo

    class _Qwen3TTSModel:
        @staticmethod
        def from_pretrained(repo: str, **kwargs):
            calls.append(repo)
            return _FakeModel(repo)

    torch_mod = types.ModuleType("torch")
    torch_mod.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch_mod.backends = types.SimpleNamespace(
        mps=types.SimpleNamespace(is_available=lambda: False)
    )
    torch_mod.bfloat16 = object()
    torch_mod.float32 = object()
    qwen_mod = types.ModuleType("qwen_tts")
    qwen_mod.Qwen3TTSModel = _Qwen3TTSModel
    monkeypatch.setitem(sys.modules, "torch", torch_mod)
    monkeypatch.setitem(sys.modules, "qwen_tts", qwen_mod)
    return calls


def _service_with_registry(registry_path: Path, voices: list[str]):
    """构造注册表为 voices 的新 service（不 load 模型，手动置预设档）。"""
    _write_registry(registry_path, voices)
    svc = tts_app.TTSService()
    svc._preset_model = "preset-model"
    svc._clone_model = None
    return svc


class _FakeUpload:
    filename = "ref.wav"


# ---------- 启动期条件加载（mlx 路径） ----------


def test_empty_registry_loads_preset_only(registry_path, monkeypatch):
    """registry 空 → 只载 preset，clone（~2.9GB）不载。"""
    calls = _install_fake_mlx(monkeypatch)
    svc = tts_app.TTSService()
    svc.load()
    assert calls == [tts_app.DEFAULT_PRESET_MODEL]
    assert svc._preset_model == f"fake:{tts_app.DEFAULT_PRESET_MODEL}"
    assert svc._clone_model is None
    assert svc._load_error is None


def test_nonempty_registry_loads_both(registry_path, monkeypatch):
    """registry 非空（注册克隆音色在用）→ 双载（旧行为）。"""
    _write_registry(registry_path, ["clone_a"])
    calls = _install_fake_mlx(monkeypatch)
    svc = tts_app.TTSService()
    svc.load()
    assert calls == [tts_app.DEFAULT_PRESET_MODEL, tts_app.DEFAULT_CLONE_MODEL]
    assert svc._clone_model == f"fake:{tts_app.DEFAULT_CLONE_MODEL}"
    assert svc._load_error is None


def test_both_models_env_forces_clone_on_empty_registry(registry_path, monkeypatch):
    """BOK_TTS_BOTH_MODELS=1 → 无条件双载（逃生键）。"""
    monkeypatch.setenv("BOK_TTS_BOTH_MODELS", "1")
    calls = _install_fake_mlx(monkeypatch)
    svc = tts_app.TTSService()
    svc.load()
    assert calls == [tts_app.DEFAULT_PRESET_MODEL, tts_app.DEFAULT_CLONE_MODEL]
    assert svc._clone_model == f"fake:{tts_app.DEFAULT_CLONE_MODEL}"


def test_torch_path_same_gate(registry_path, monkeypatch):
    """torch/MPS 路径同判据：registry 空只载 preset；非空双载。"""
    monkeypatch.setattr(tts_app, "BACKEND", "transformers")
    calls = _install_fake_torch_stack(monkeypatch)
    svc = tts_app.TTSService()
    svc.load()
    assert calls == [tts_app.DEFAULT_PRESET_MODEL]
    assert svc._clone_model is None

    _write_registry(registry_path, ["clone_a"])
    svc2 = tts_app.TTSService()
    svc2.load()
    # 两次 load 各自恒载 preset；第二次因 registry 非空追加 clone。
    assert calls == [
        tts_app.DEFAULT_PRESET_MODEL,
        tts_app.DEFAULT_PRESET_MODEL,
        tts_app.DEFAULT_CLONE_MODEL,
    ]
    assert svc2._clone_model is not None


def test_models_loaded_observation_line(registry_path, capsys):
    """启动观测行 `TTS_MODELS loaded=preset[,clone] registry_n=N`。"""
    svc = _service_with_registry(registry_path, ["clone_a"])
    svc._log_models_loaded()
    out = capsys.readouterr().out
    assert "TTS_MODELS" in out
    assert "loaded=preset" in out
    assert "registry_n=1" in out
    assert "clone" not in out.split("TTS_MODELS", 1)[1]

    svc._clone_model = "clone-model"
    svc._log_models_loaded()
    out = capsys.readouterr().out
    assert "loaded=preset,clone" in out


# ---------- ensure_loaded 按需校验（函数级，不起 uvicorn） ----------


def test_registered_voice_requires_clone_503(registry_path):
    """请求注册 voice 而 clone 未载 → 503 人话（两条逃生路都在提示里）。"""
    svc = _service_with_registry(registry_path, ["clone_a"])
    with pytest.raises(tts_app.HTTPException) as ei:
        svc.ensure_loaded(voice="clone_a")
    assert ei.value.status_code == 503
    detail = str(ei.value.detail)
    assert "BOK_TTS_BOTH_MODELS=1" in detail
    assert "/v1/voices/register" in detail


def test_preset_voice_passes_without_clone(registry_path):
    """非注册 voice 走 preset 通道：clone 不在场也放行（按需语义）。"""
    svc = _service_with_registry(registry_path, ["clone_a"])
    svc.ensure_loaded(voice="Vivian")
    # clone 载入后，注册 voice 与零参保守闸都放行。
    svc._clone_model = "clone-model"
    svc.ensure_loaded(voice="clone_a")
    svc.ensure_loaded()


def test_zero_arg_conservative_when_registry_nonempty(registry_path):
    """零参调用（endpoint 基础闸）无 voice 语境：registry 非空即要求 clone。"""
    svc = _service_with_registry(registry_path, ["clone_a"])
    with pytest.raises(tts_app.HTTPException) as ei:
        svc.ensure_loaded()
    assert ei.value.status_code == 503
    assert "BOK_TTS_BOTH_MODELS=1" in str(ei.value.detail)

    # registry 空 + clone 未载 → 零参放行（preset 在场即可）。
    svc2 = _service_with_registry(registry_path, [])
    svc2.ensure_loaded()


def test_register_requires_clone_503(registry_path):
    """注册端点写 clone：未载时 503（require_clone=True）。"""
    svc = _service_with_registry(registry_path, [])
    with pytest.raises(tts_app.HTTPException) as ei:
        svc.ensure_loaded(require_clone=True)
    assert ei.value.status_code == 503
    assert "/v1/voices/register" in str(ei.value.detail)


def test_register_voice_endpoint_503_when_clone_not_loaded(registry_path):
    """register_voice 走 require_clone 闸：未载 503（在读文件之前就 fail-fast）。"""
    svc = _service_with_registry(registry_path, [])
    with pytest.raises(tts_app.HTTPException) as ei:
        asyncio.run(
            svc.register_voice(file=_FakeUpload(), voice_id="v", ref_text="你好。")
        )
    assert ei.value.status_code == 503
    assert "BOK_TTS_BOTH_MODELS=1" in str(ei.value.detail)


def test_synthesize_registered_voice_503_when_clone_not_loaded(registry_path):
    """合成路径被注册 voice 请求而 clone 未载 → 503（整段与流式同闸）。"""
    svc = _service_with_registry(registry_path, ["clone_a"])
    with pytest.raises(tts_app.HTTPException) as ei:
        svc.synthesize(text="你好。", language="zh", voice="clone_a")
    assert ei.value.status_code == 503
    assert "BOK_TTS_BOTH_MODELS=1" in str(ei.value.detail)

    with pytest.raises(tts_app.HTTPException):
        list(svc.synthesize_chunks(text="你好。", language="zh", voice="clone_a"))


def test_preset_missing_and_load_error_503(registry_path):
    """preset 缺失 / 载入报错：503 语义不回归。"""
    svc = _service_with_registry(registry_path, [])
    svc._preset_model = None
    with pytest.raises(tts_app.HTTPException) as ei:
        svc.ensure_loaded(voice="Vivian")
    assert ei.value.status_code == 503
    assert "preset" in str(ei.value.detail)

    svc._load_error = "boom"
    with pytest.raises(tts_app.HTTPException) as ei2:
        svc.ensure_loaded()
    assert ei2.value.status_code == 503
    assert "model not ready" in str(ei2.value.detail)


# ---------- 源级 pin ----------


def test_app_source_gate_pins():
    """条件加载判据与懒加载铁律的源级 pin（防重构静默回退双载）。"""
    src = (ROOT / "services" / "qwen3-tts-sidecar" / "app.py").read_text(encoding="utf-8")
    assert 'os.environ.get("BOK_TTS_BOTH_MODELS") == "1"' in src
    assert "return bool(self._registry)" in src  # registry 判据
    assert src.count("if load_clone:") == 2  # 两后端各一处条件化
    # clone 载入语句（mlx load_model / torch from_pretrained）的**外层块头**必须
    # 恰是 `if load_clone:`——旧的无条件双载与任何运行期懒加载写法都过不了这条。
    lines = src.splitlines()
    guarded = 0
    for i, line in enumerate(lines):
        if (
            "_clone_model = mlx_load_model(" in line
            or "_clone_model = Qwen3TTSModel.from_pretrained(" in line
        ):
            indent = len(line) - len(line.lstrip())
            guard = None
            for j in range(i - 1, -1, -1):
                prev = lines[j]
                if not prev.strip():
                    continue
                if len(prev) - len(prev.lstrip()) < indent:  # 缩进回退 = 外层块头
                    guard = prev.strip()
                    break
            assert guard == "if load_clone:", (i + 1, line, guard)
            guarded += 1
    assert guarded == 2


def test_worker_idle_pool_pinned_to_one():
    """三入口 WorkerOptions 显式 num_idle_processes=1（框架缺省 prod=min(cpu,4)）。"""
    for rel in (
        "apps/agent/agent_runtime/agent.py",
        "apps/agent/agent_runtime/interpret.py",
        "apps/agent/agent_runtime/realtime_demo.py",
    ):
        src = (ROOT / rel).read_text(encoding="utf-8")
        anchor = src.index("cli.run_app(")
        window = src[anchor : anchor + 1200]
        assert "num_idle_processes=1" in window, rel


def test_worker_load_threshold_pinned_high():
    """三入口 load_threshold 钉 0.99（第十七波 FLOW20 全哑根修：livekit load=
    整机 psutil.cpu_percent，共享机 0.7 缺省=桌面噪音拒派空房全哑）。
    BOK_WORKER_LOAD_THRESHOLD env 可调，源级 pin 防重构静默回缺省。"""
    for rel in (
        "apps/agent/agent_runtime/agent.py",
        "apps/agent/agent_runtime/interpret.py",
        "apps/agent/agent_runtime/realtime_demo.py",
    ):
        src = (ROOT / rel).read_text(encoding="utf-8")
        anchor = src.index("cli.run_app(")
        window = src[anchor : anchor + 1400]
        assert 'os.environ.get("BOK_WORKER_LOAD_THRESHOLD", "0.99")' in window, rel