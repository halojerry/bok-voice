"""P1 SV-CPU ASR 引擎车道(2026-10-01 三层解耦计划:CPU 耳朵/MPS 大脑)。

契约:
- sidecar ``/api/start`` 收 ``engine``;session 级路由——sensevoice=CPU 车道
  (partial/finish 走 _partial_sv/_finish_sv),缺省/qwen3=旧 Qwen3 路径逐字节
  不变;模型缺席 fail-open 回旧引擎+告警。
- 插件 ``Qwen3ASRSTT(engine=...)`` 两个 start_params 位点下发。
- 装配解析 ``_asr_engine_from_cfg``:env ``BOK_ASR_ENGINE`` > ``asr_json.engine``
  > 缺省旧路(验证门后翻 sensevoice,只动该函数一处)。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

import os

os.environ.setdefault("QWEN3_ASR_BACKEND", "mlx")

PLUGINS_SRC = (
    ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py"
).read_text(encoding="utf-8")
AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(
    encoding="utf-8"
)
INTERP_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "interpret.py").read_text(
    encoding="utf-8"
)
SIDECAR_SRC = (
    ROOT / "services" / "qwen3-asr-sidecar" / "app.py"
).read_text(encoding="utf-8")


def _load_sidecar_app():
    spec = importlib.util.spec_from_file_location(
        "qwen3_asr_sidecar_engine", ROOT / "services" / "qwen3-asr-sidecar" / "app.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- 引擎归一(纯函数) ----


def test_norm_engine_maps_and_defaults():
    mod = _load_sidecar_app()
    assert mod._norm_engine("") == ""
    assert mod._norm_engine("qwen3") == ""
    assert mod._norm_engine("MLX") == ""
    assert mod._norm_engine("sensevoice") == "sensevoice"
    assert mod._norm_engine("SV") == "sensevoice"
    assert mod._norm_engine("garbage") == ""  # 未知值保守回旧行为


def test_sv_lang_label_mapping():
    mod = _load_sidecar_app()
    assert mod._sv_lang_label("cantonese") == "Cantonese"
    assert mod._sv_lang_label("yue") == "Cantonese"
    assert mod._sv_lang_label("zh") == "Chinese"
    assert mod._sv_lang_label("en") == "English"
    assert mod._sv_lang_label("") == ""


def test_engine_fallback_when_model_missing(monkeypatch, tmp_path, capsys):
    """缺模型 fail-open:start(engine=sensevoice) 落回旧路径+一行告警。"""
    mod = _load_sidecar_app()
    monkeypatch.setattr(mod, "SV_MODEL_DIR", str(tmp_path / "nope"))
    svc = mod.ASRService()
    sid = svc.start(language="cantonese", engine="sensevoice")
    assert svc._sessions[sid]["engine"] == "", "模型缺席必须回旧引擎"
    assert "sensevoice engine fallback" in capsys.readouterr().out


def test_engine_persists_when_model_present(monkeypatch, tmp_path):
    mod = _load_sidecar_app()
    d = tmp_path / "sv"
    d.mkdir()
    (d / "model.int8.onnx").write_bytes(b"x")
    (d / "tokens.txt").write_bytes(b"x")
    monkeypatch.setattr(mod, "SV_MODEL_DIR", str(d))
    svc = mod.ASRService()
    sid = svc.start(language="cantonese", engine="sv")
    assert svc._sessions[sid]["engine"] == "sensevoice"
    # 缺省/未知 → 旧路径(session 无 engine 键值即为 "")
    sid2 = svc.start(language="zh")
    assert svc._sessions[sid2]["engine"] == ""


# ---- SV 引擎行为面(fake 识别器;真模型由 eval_sensevoice/实弹门覆盖) ----


class _FakeSVStream:
    def __init__(self, text):
        self.result = type("R", (), {"text": text})()

    def accept_waveform(self, sample_rate, waveform):
        pass


class _FakeSVRec:
    def __init__(self, text="SenseVoice 假输出"):
        self._text = text
        self.decode_calls = 0

    def create_stream(self):
        return _FakeSVStream(self._text)

    def decode_stream(self, s):
        self.decode_calls += 1


def _voiced_pcm(seconds: float = 1.5) -> bytes:
    return b"\x00\x19" * int(16000 * seconds)


def _fake_sv_dir(monkeypatch, mod, tmp_path):
    """start() 的模型在场闸要过:指一个带两文件的假目录(SV_MODEL_DIR 与 bok
    下发的 repo 目录不同源——bok 走 env 显式下发,sidecar 缺省只兜手动安装)。"""
    d = tmp_path / "sv"
    d.mkdir(exist_ok=True)
    (d / "model.int8.onnx").write_bytes(b"x")
    (d / "tokens.txt").write_bytes(b"x")
    monkeypatch.setattr(mod, "SV_MODEL_DIR", str(d))


def test_partial_sv_writes_ledger_fields(monkeypatch, tmp_path):
    """_partial_sv 落账与 _partial_mlx 同款:partial_text/partial_covered/
    last_partial_at → 增量 finish 判据面零改动复用;间隔门/忙锁/FINAL 停发同款。"""
    mod = _load_sidecar_app()
    _fake_sv_dir(monkeypatch, mod, tmp_path)
    fake = _FakeSVRec("你好嗎")
    svc = mod.ASRService()
    svc._sv_models = {k: fake for k in ("auto", "zh", "en", "ja", "ko", "yue")}
    sid = svc.start(language="cantonese", engine="sensevoice", partial_ms="1")
    s = svc._sessions[sid]
    out = svc.chunk(sid, _voiced_pcm(1.5))
    assert out["partial"] is True
    assert out["text"] == "你好嗎"
    assert out["language"] == "Cantonese"
    assert s["partial_text"] == "你好嗎"
    assert s["partial_covered"] == len(s["chunks"])
    assert s["last_partial_at"] > 0
    # 间隔门:partial_ms=1 放行过一次后,紧接第二窗到点也放;把档拉高再验门
    s["partial_ms"] = 60000
    out2 = svc.chunk(sid, _voiced_pcm(1.0))
    assert out2["text"] == "你好嗎" and fake.decode_calls == 1, "间隔未到回缓存不解码"


def test_partial_sv_final_stops_and_busy_lock(monkeypatch, tmp_path):
    mod = _load_sidecar_app()
    _fake_sv_dir(monkeypatch, mod, tmp_path)
    fake = _FakeSVRec("锁测试")
    svc = mod.ASRService()
    svc._sv_models = {k: fake for k in ("auto", "zh", "en", "ja", "ko", "yue")}
    sid = svc.start(language="zh", engine="sensevoice")
    s = svc._sessions[sid]
    s["partials_done"] = True
    out = svc.chunk(sid, _voiced_pcm())
    assert fake.decode_calls == 0, "FINAL 后停发"
    # 忙锁:持有 inf_lock 时回缓存
    s2 = svc.start(language="zh", engine="sensevoice")
    sess2 = svc._sessions[s2]
    sess2["inf_lock"].acquire()
    try:
        svc.chunk(s2, _voiced_pcm())
        assert fake.decode_calls == 0, "锁忙跳过"
    finally:
        sess2["inf_lock"].release()


def test_finish_sv_full_decode_and_language_label(monkeypatch, tmp_path):
    """finish:整段全量解码 + 会话语言映射标签;置信度键缺席(插件回旧行为)。"""
    mod = _load_sidecar_app()
    _fake_sv_dir(monkeypatch, mod, tmp_path)
    fake = _FakeSVRec("我嘅 WhatsApp 係六六九九四五")
    svc = mod.ASRService()
    svc._sv_models = {k: fake for k in ("auto", "zh", "en", "ja", "ko", "yue")}
    sid = svc.start(language="cantonese", engine="sensevoice")
    svc._sessions[sid]["chunks"].extend(_voiced_pcm(2.0))
    out = svc.finish(sid)
    assert out["text"] == "我嘅 WhatsApp 係六六九九四五"
    assert out["language"] == "Cantonese"
    assert out["partial"] is False
    assert out.get("confidence") is None


# ---- 插件/装配接线源级 pin ----


def test_asr_engine_from_cfg_resolution(monkeypatch):
    from agent_runtime.providers.livekit_plugins import _asr_engine_from_cfg

    monkeypatch.delenv("BOK_ASR_ENGINE", raising=False)
    # 缺省=sensevoice(2026-10-01 验证门全绿后翻定);回滚键 env/asr_json=qwen3
    assert _asr_engine_from_cfg({}) == "sensevoice"
    assert _asr_engine_from_cfg({"engine": "qwen3"}) == ""
    assert _asr_engine_from_cfg({"engine": "sensevoice"}) == "sensevoice"
    assert _asr_engine_from_cfg({"engine": "SV"}) == "sensevoice"
    assert _asr_engine_from_cfg({"engine": "garbage"}) == "sensevoice"
    # env 终极覆盖
    monkeypatch.setenv("BOK_ASR_ENGINE", "qwen3")
    assert _asr_engine_from_cfg({}) == ""
    assert _asr_engine_from_cfg({"engine": "sensevoice"}) == ""
    monkeypatch.setenv("BOK_ASR_ENGINE", "sensevoice")
    assert _asr_engine_from_cfg({"engine": "qwen3"}) == "sensevoice"


def test_wiring_source_pins():
    """插件两位点/装配/endpoint/engine 落账接线源级 pin。"""
    # 插件:engine 属性 + 两个 start_params 位点
    assert "engine: str = \"\"," in PLUGINS_SRC
    assert 'self._engine = str(engine or "").strip()' in PLUGINS_SRC
    assert PLUGINS_SRC.count('start_params["engine"] = self._stt_._engine') == 2
    # 装配:A/B 线同解析器
    assert "engine=_asr_engine_from_cfg(asr_cfg)," in AGENT_SRC
    assert "engine=_asr_engine_from_cfg(asr_cfg)," in INTERP_SRC
    # sidecar:endpoint 参数 + 路由 + SV 引擎头
    assert "engine: str = \"\"," in SIDECAR_SRC
    assert 'session.get("engine") == "sensevoice"' in SIDECAR_SRC
    assert "def _partial_sv(" in SIDECAR_SRC
    assert "def _finish_sv(" in SIDECAR_SRC
    assert "def _norm_engine(" in SIDECAR_SRC
    # bok:模型收编 + 目录下发 + 依赖
    bok = (ROOT / "tools" / "bok.py").read_text(encoding="utf-8")
    assert '"sensevoice": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"' in bok
    assert 'asr_env["QWEN3_ASR_SV_MODEL_DIR"]' in bok
    reqs = (ROOT / "services" / "qwen3-asr-sidecar" / "requirements.txt").read_text(
        encoding="utf-8"
    )
    assert "sherpa-onnx" in reqs
