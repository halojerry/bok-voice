"""W6 场景底噪(2026-10-06 demo-quality-wave)单测——manifest/场景解析、增益钳制、
duck 轨迹、资产无缝性,零 livekit 会话。

覆盖四块:
1. load_ambience_manifest:正常/坏 JSON/缺文件/形状不对(宽容=空目录永不炸,
   镜像 flow_graph parse 档);
2. resolve_scene:none/空/未知/资产缺失 → None 零行为;合法场景 → 条目;
3. 增益纯函数:clamp 窗、gain_trajectory 满 duck 恰好走完 fade 跨度、mix_chunk;
4. 资产面:三场景 wav 存在且 40.0s/24k/mono/16-bit、峰值 -26..-30dBFS 无削波、
   循环缝两侧 RMS 连续、SCENES 目录与 manifest 键集一致(与生成脚本三方钉)。
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "seed"))

import gen_ambience  # noqa: E402
from agent_runtime import ambience  # noqa: E402
from agent_runtime.ambience import (  # noqa: E402
    DEFAULT_GAIN_DB,
    DUCK_ATTEN_DB,
    DUCK_FADE_S,
    AmbienceLoopPlayer,
    clamp_gain_db,
    gain_slew_db_per_sample,
    gain_trajectory,
    load_ambience_manifest,
    mix_chunk,
    resolve_scene,
)

_AGENT_SRC = (Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(
    encoding="utf-8", errors="replace"
)


def test_agent_wiring_source_pins():
    """W6 接线 pin（test_filler_cloud 同款源级锚）：场景解析/duck 钩子/起播/收摊
    四挂点在场——字符串变更须过本测试认账，防后续重构静默丢底噪。"""
    assert 'os.environ.get("BOK_AMBIENT_SCENE", "").strip().lower()' in _AGENT_SRC
    assert "def _on_ambience_state" in _AGENT_SRC
    assert 'session.on("agent_state_changed", _on_ambience_state)' in _AGENT_SRC
    assert "_ambience.set_ducked(True)" in _AGENT_SRC
    assert "[agent] ambience started scene=" in _AGENT_SRC
    assert "_ambience.stop()" in _AGENT_SRC
    # 默认零行为：env 未设时 resolve_scene(None 面) → 不注册钩子不起播
    assert "if _ambience_scene is not None:" in _AGENT_SRC

_MANIFEST_OK = {
    "scenes": [
        {"scene": "office", "file": "office.wav", "loop_s": 40.0, "gain_db": -28.0, "license": "x"},
        {"scene": "car", "file": "car.wav", "loop_s": 40.0},
    ]
}


def _write_manifest(tmp_path: Path, data) -> Path:
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


# ---- 1. manifest 解析(宽容原则) ----


def test_manifest_normal(tmp_path):
    m = load_ambience_manifest(_write_manifest(tmp_path, _MANIFEST_OK))
    assert set(m) == {"office", "car"}
    assert m["office"]["file"] == "office.wav"
    assert m["office"]["loop_s"] == 40.0
    # 缺 gain_db 条目回落缺省播放增益
    assert m["car"]["gain_db"] == DEFAULT_GAIN_DB


def test_manifest_bad_json_is_empty(tmp_path):
    d = tmp_path
    (d / "manifest.json").write_text("{not json", encoding="utf-8")
    assert load_ambience_manifest(d) == {}


def test_manifest_missing_file_is_empty(tmp_path):
    assert load_ambience_manifest(tmp_path / "nope") == {}


@pytest.mark.parametrize(
    "data",
    [
        [1, 2, 3],  # 顶层 list 非条目
        {"scenes": "nope"},  # scenes 非 list
        {"scenes": [{"file": "a.wav"}, {"scene": "b"}]},  # 条目缺 scene/file 逐条跳
        {"office": "not-a-dict"},  # dict 形状值非 dict
        42,
    ],
)
def test_manifest_bad_shapes_are_empty_or_filtered(tmp_path, data):
    d = _write_manifest(tmp_path, data)
    m = load_ambience_manifest(d)
    assert all(isinstance(v, dict) and v.get("scene") and v.get("file") for v in m.values())


def test_manifest_accepts_alternate_shapes(tmp_path):
    # 顶层 list 形
    (tmp_path / "a").mkdir()
    m1 = load_ambience_manifest(_write_manifest(tmp_path / "a", [_MANIFEST_OK["scenes"][0]]))
    assert set(m1) == {"office"}
    # 手写 dict 形(scene 为键)
    (tmp_path / "b").mkdir()
    m2 = load_ambience_manifest(_write_manifest(tmp_path / "b", {"car": {"file": "car.wav"}}))
    assert set(m2) == {"car"}
    assert m2["car"]["scene"] == "car"


def test_manifest_bad_numbers_dropped_or_defaulted(tmp_path):
    d = _write_manifest(
        tmp_path,
        {"scenes": [{"scene": "office", "file": "o.wav", "loop_s": "xx", "gain_db": "yy"}]},
    )
    m = load_ambience_manifest(d)
    assert "loop_s" not in m["office"]
    assert m["office"]["gain_db"] == DEFAULT_GAIN_DB


# ---- 2. 场景解析 ----


def test_resolve_scene_none_and_unknown_are_zero_behavior():
    assert resolve_scene(None) is None
    assert resolve_scene("") is None
    assert resolve_scene("none") is None
    assert resolve_scene("  ") is None
    assert resolve_scene("spaceship") is None
    assert resolve_scene("OFFICE", manifest={}) is None  # 目录认得但清单空 → None


def test_resolve_scene_legal_requires_manifest_entry():
    m = {"office": {"scene": "office", "file": "office.wav"}}
    got = resolve_scene(" Office ", manifest=m)
    assert got is not None and got["scene"] == "office" and got["file"] == "office.wav"
    # 大小写归一
    assert resolve_scene("CAR", manifest={"car": {"scene": "car", "file": "car.wav"}})


def test_resolve_scene_default_manifest_matches_repo_assets():
    m = load_ambience_manifest()
    for name in ambience.SCENES:
        assert resolve_scene(name, manifest=m) is not None, f"scene {name} 缺资产条目"


# ---- 3. 增益纯函数 ----


def test_clamp_gain_db_bad_and_range():
    assert clamp_gain_db("abc") == DEFAULT_GAIN_DB
    assert clamp_gain_db(None) == DEFAULT_GAIN_DB
    assert clamp_gain_db(float("nan")) == DEFAULT_GAIN_DB
    assert clamp_gain_db(float("inf")) == DEFAULT_GAIN_DB
    assert clamp_gain_db(0.0) == -12.0  # 手滑写 0dB 被钳到封顶
    assert clamp_gain_db(-200.0) == -60.0
    assert clamp_gain_db(-28.0) == -28.0


def test_gain_trajectory_full_duck_completes_exactly_over_fade():
    rate = 48000
    slew = gain_slew_db_per_sample(DUCK_ATTEN_DB, DUCK_FADE_S, rate)
    n = int(rate * DUCK_FADE_S)
    new_cur, gains = gain_trajectory(-28.0, -40.0, n, slew)
    assert new_cur == pytest.approx(-40.0)
    assert gains[0] == pytest.approx(-28.0, abs=1e-9)
    assert gains[-1] < -39.99  # 末样本已几乎到位
    assert np.all(np.diff(gains) <= 1e-12)  # 单调下行
    assert np.all(gains >= -40.0 - 1e-9)  # 不过冲


def test_gain_trajectory_no_overshoot_and_constant():
    slew = gain_slew_db_per_sample(12.0, 0.2, 48000)
    # 目标差小于一步距离:半程截断,到点即停
    cur, g = gain_trajectory(-28.0, -30.0, 9600, slew)
    assert cur == pytest.approx(-30.0)
    assert g[-1] == pytest.approx(-30.0, abs=0.01)
    # 目标即当前:恒定
    cur, g = gain_trajectory(-28.0, -28.0, 100, slew)
    assert cur == -28.0 and np.all(g == -28.0)


def test_mix_chunk_scales_and_clips():
    seg = np.full(4, 32767, dtype=np.int16)
    unity = mix_chunk(seg, np.zeros(4))
    assert np.frombuffer(unity, dtype=np.int16).max() == 32767
    # -inf 级衰减 → 静音
    silent = mix_chunk(seg, np.full(4, -120.0))
    assert np.frombuffer(silent, dtype=np.int16).max() == 0


# ---- 4. 资产面(提交进仓的三场景 wav) ----


def test_scene_catalog_parity_with_manifest_and_generator():
    """三方钉:运行时 SCENES 目录 = manifest = 生成脚本配方键集。"""
    assert set(ambience.SCENES) == set(load_ambience_manifest())
    assert set(ambience.SCENES) == set(gen_ambience.SCENE_SEEDS)
    assert gen_ambience.DEFAULT_GAIN_DB == DEFAULT_GAIN_DB


def _load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        assert w.getsampwidth() == 2 and w.getnchannels() == 1, "必须 mono/16-bit(生成端契约)"
        return (
            np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64) / 32767.0,
            w.getframerate(),
        )


@pytest.mark.parametrize("scene", ["office", "callcenter", "car"])
def test_asset_exists_40s_no_clipping_seam_continuous(scene):
    path = ambience.AMBIENCE_ASSETS_DIR / f"{scene}.wav"
    assert path.is_file(), f"缺资产 {path}(跑 scripts/seed/gen_ambience.py 生成)"
    x, sr = _load_wav(path)
    assert sr == 24000
    assert len(x) == pytest.approx(40.0 * 24000), "资产必须 40.0s 整(manifest loop_s 契约)"
    peak = float(np.max(np.abs(x)))
    peak_db = 20.0 * np.log10(max(peak, 1e-9))
    assert -30.0 <= peak_db <= -26.0, f"峰值 {peak_db:.1f}dBFS 越窗(铺底不是前景)"
    assert peak < 0.5, "crossfade 区无削波(峰值须远离满幅)"
    # 循环点两侧 RMS 连续(缝上幅度统计不塌陷)
    win = 24000  # 1s
    rms_head = float(np.sqrt(np.mean(x[:win] ** 2)))
    rms_tail = float(np.sqrt(np.mean(x[-win:] ** 2)))
    assert 0.5 <= rms_tail / max(rms_head, 1e-12) <= 2.0
    # 缝上逐样本跳变受限(无爆音):末样本→首样本差 < 1% 满幅
    assert abs(x[0] - x[-1]) < 0.01


# ---- 5. 播放器状态机(零 livekit 会话;帧生成直驱真 rtc) ----


def _write_tiny_wav(d: Path, sr: int = 24000, dur_s: float = 0.5) -> Path:
    import wave as _wave

    pcm = (np.sin(np.linspace(0, 200 * 2 * np.pi, int(sr * dur_s))) * 8000).astype(np.int16)
    with _wave.open(str(d / "office.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return d


def _player(tmp_path, **kw) -> AmbienceLoopPlayer:
    entry = {"scene": "office", "file": "office.wav", "gain_db": -28.0}
    # 哨兵 player(状态机/帧生成直驱不碰 play(),仅令 available=True)
    return AmbienceLoopPlayer(object(), entry=entry, assets_dir=tmp_path, **kw)


def test_player_missing_asset_is_inert(tmp_path):
    p = AmbienceLoopPlayer(None, entry={"scene": "office", "file": "ghost.wav"}, assets_dir=tmp_path)
    assert p.available is False
    p.start()  # 零行为不炸
    assert p.running is False
    p.stop()  # 幂等
    assert p.running is False


def test_player_duck_target_flips_and_resets(tmp_path):
    _write_tiny_wav(tmp_path)
    p = _player(tmp_path)
    assert p.available and p.gain_db == -28.0
    p.set_ducked(True)
    assert p.ducked is True
    assert p._target_db == pytest.approx(-28.0 - DUCK_ATTEN_DB)
    p.set_ducked(True)  # 幂等
    p.set_ducked(False)
    assert p.ducked is False and p._target_db == pytest.approx(-28.0)
    p.stop()  # 复位
    assert p.ducked is False


def test_player_loop_frames_cyclic_and_duck_converges(tmp_path):
    """直驱 _frames_gen(真 livekit rtc,无 BackgroundAudioPlayer):帧率/样本数/
    环形取块/duck 轨迹收敛全链。"""
    from livekit import rtc as lk_rtc

    _write_tiny_wav(tmp_path, dur_s=0.1)  # 0.1s 源 → 48k 重采样 4800 样本,验证循环回绕
    p = _player(tmp_path)
    gen = p._frames_gen(lk_rtc)

    async def _scenario():
        frames = []
        for _ in range(6):
            frames.append(await gen.__anext__())
        for f in frames:
            assert isinstance(f, lk_rtc.AudioFrame)
            assert f.sample_rate == 48000 and f.num_channels == 1
            assert f.samples_per_channel == 960  # 20ms @48k
            assert len(bytes(f.data)) == 960 * 2  # int16 mono
        # duck:吃满 fade 跨度后 cur 收敛到 gain-12
        p.set_ducked(True)
        n_fade = int(48000 * DUCK_FADE_S / 960) + 1
        for _ in range(n_fade):
            await gen.__anext__()
        assert p._cur_db == pytest.approx(-28.0 - DUCK_ATTEN_DB, abs=1e-6)
        # 回涨:反向走回
        p.set_ducked(False)
        for _ in range(n_fade):
            await gen.__anext__()
        assert p._cur_db == pytest.approx(-28.0, abs=1e-6)
        # 循环回绕:连续吃远超源长度的帧数,生成器不枯竭,环形仍在出帧。
        for _ in range(3):
            await gen.__anext__()

    import asyncio

    asyncio.run(_scenario())  # 单事件循环(asyncio.run 收圈会 aclose 生成器,不可分段跑)


def test_player_instant_fade_jumps_to_target(tmp_path):
    _write_tiny_wav(tmp_path)
    p = _player(tmp_path, fade_s=0.0)
    p.set_ducked(True)
    new_cur, gains = gain_trajectory(p._cur_db, p._target_db, 10, p._slew)
    assert new_cur == pytest.approx(p._target_db)
    assert gains[0] == pytest.approx(-28.0)  # 首样本=当前电平(过渡起点)
    assert np.allclose(gains[1:], p._target_db)  # 次样本起瞬时到位


# ---- custom 档(2026-10-08 用户拍板「内置合成底噪像电流,自己上传音频」)----


def test_resolve_scene_custom_file(tmp_path):
    """custom=真房间录音循环:BOK_AMBIENT_FILE 绝对路径在场 → 条目带 gain 钳制;
    缺省增益 -28dB 与内置轨同轨;custom_gain_db 可覆盖。"""
    wav = tmp_path / "room.wav"
    wav.write_bytes(b"RIFF")  # 内容不校验(装载在 player,此处只判在场)
    e = ambience.resolve_scene("custom", custom_file=str(wav))
    assert e is not None and e["scene"] == "custom"
    assert e["file"] == str(wav)
    assert e["gain_db"] == -28.0
    e2 = ambience.resolve_scene("custom", custom_file=str(wav), custom_gain_db=-20.0)
    assert e2["gain_db"] == -20.0
    # 越界钳到天花板(底噪绝不抢道硬边界)
    e3 = ambience.resolve_scene("custom", custom_file=str(wav), custom_gain_db=6.0)
    assert e3["gain_db"] == ambience.GAIN_DB_CEIL


def test_resolve_scene_custom_missing_or_relative_zero_behavior(tmp_path):
    """宁缺宁炸:未设/非绝对路径/不在盘 → None 零行为(与 manifest 纪律同);
    custom 不进 SCENES/manifest 目录(目录↔清单 parity 钉零影响)。"""
    assert ambience.resolve_scene("custom", custom_file="") is None
    assert ambience.resolve_scene("custom", custom_file="room.wav") is None  # 相对路径拒
    assert ambience.resolve_scene("custom", custom_file=str(tmp_path / "nope.wav")) is None
    assert "custom" not in ambience.SCENES


def test_player_loads_custom_absolute_path(tmp_path):
    """播放器装载:绝对路径 wav 直接吃(Path 拼接绝对路径自替换),可用=available。"""
    _write_tiny_wav(tmp_path)
    wav = tmp_path / "office.wav"  # _write_tiny_wav 固定写这个名字
    entry = {"scene": "custom", "file": str(wav), "gain_db": -28.0}
    p = ambience.AmbienceLoopPlayer(None, entry=entry)
    # available 还要求 player 在场(轨道句柄);此处只钉资产装载:pcm 已按绝对路径吃进
    assert p._pcm is not None and len(p._pcm) > 0
