"""W1c 垫话云车道解禁（2026-10-06 demo-quality wave）。

旧档（2026-10-03）云 a_reply 车道 auto-off——call-6a8133f6 实弹翻案：云档一轮
commit_to_audio≈2.0s > 客户耐心，生成空窗无垫话遮蔽=死寂直接诱发连环重问
（Family A 加速器）。现在云车道默认也 arm（``BOK_FILLER_CLOUD`` 默认 "1"），
"0" 回旧 auto-off；``BOK_FILLER`` 显式 1/0 仍最高优先。

决策收进纯函数 ``_filler_cloud_gate``（本文件行为测试）；装配点接线走源级 pin。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "agent"))

from _bok_src import bok_source  # noqa: E402
from agent_runtime.agent import _filler_cloud_gate  # noqa: E402

AGENT_SRC = (_ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(
    encoding="utf-8"
)


# ------------------------------------------------------------------ 纯函数两臂


def test_default_cloud_lane_minimal():
    """2026-10-08 定调缺省=emm 级（call-e077ded6 实弹:整句垫话与 LLM 轮
    (breath)/(emm) 开口标记叠出重复思考感;云腿速度够快不遮整句）。"""
    assert _filler_cloud_gate("", True, "") == (True, "cloud-minimal")


def test_cloud_zero_falls_back_to_old_auto_off():
    """逃生口：BOK_FILLER_CLOUD="0" 回旧 auto-off 档。"""
    assert _filler_cloud_gate("", True, "0") == (False, "cloud-auto-off")


def test_cloud_explicit_one_arms():
    assert _filler_cloud_gate("", True, "1") == (True, "cloud-armed")


def test_bok_filler_env_beats_cloud_gate():
    """BOK_FILLER 显式 1/0 最高优先（A/B 双向覆盖语义保留）。"""
    assert _filler_cloud_gate("1", True, "0") == (True, "env-forced-on")
    assert _filler_cloud_gate("0", True, "") == (False, "env-forced-off")
    assert _filler_cloud_gate("0", False, "") == (False, "env-forced-off")


def test_local_lane_untouched():
    """本地车道不触碰实例闸（None=不调 set_enabled,沿用模块 env 缺省=旧行为）。"""
    assert _filler_cloud_gate("", False, "") == (None, "local-default")
    assert _filler_cloud_gate("", False, "0") == (None, "local-default")


def test_junk_cloud_env_treated_as_minimal():
    """坏值不回落 auto-off（缺省=minimal 语义,只有字面 "0" 才关）。"""
    assert _filler_cloud_gate("", True, "bogus") == (True, "cloud-minimal")
    assert _filler_cloud_gate("", True, " 0 ") == (False, "cloud-auto-off")


def test_cloud_explicit_minimal_and_emm_aliases():
    assert _filler_cloud_gate("", True, "minimal") == (True, "cloud-minimal")
    assert _filler_cloud_gate("", True, "emm") == (True, "cloud-minimal")
    assert _filler_cloud_gate("", True, " MINIMAL ") == (True, "cloud-minimal")


# ------------------------------------------------------------------ 接线 pin


def test_agent_wiring_source_pins():
    """装配点走 _filler_cloud_gate + 双向打点在场（字符串变更须过本测试认账）。"""
    assert "os.environ.get(\"BOK_FILLER_CLOUD\", \"\").strip()" in AGENT_SRC
    assert "[agent] filler armed (cloud lane, BOK_FILLER_CLOUD=1)" in AGENT_SRC
    assert "[agent] filler auto-off (cloud lane, BOK_FILLER_CLOUD=0)" in AGENT_SRC
    # 2026-10-08 minimal 缺省:装配点连带类别钉死（emm 级资产,无整句）
    assert "_filler.restrict_category(\"minimal\")" in AGENT_SRC
    assert "[agent] filler armed minimal" in AGENT_SRC
    # 实例闸仍以 set_enabled(_filler_on) 单点落地
    assert "_filler.set_enabled(_filler_on)" in AGENT_SRC


def test_restrict_category_forces_minimal_select():
    """fillers 契约:钉死类别时 _select 跳过罐头匹配/分类器,只出该类资产。"""
    import agent_runtime.fillers as fl

    d = object.__new__(fl.FillerDirector)
    d._force_cat = None
    d._last_bucket = ""
    d._current_bucket = lambda: "default"
    d._pick = lambda lang, cat, bucket: {"text": "嗯。", "file": None}  # 只回 entry(真 _pick 契约)
    d.restrict_category("minimal")
    assert d._force_cat == "minimal"
    entry, cat = d._select("zh")
    assert cat == "minimal" and entry["text"] == "嗯。"
    d.restrict_category(None)
    assert d._force_cat is None  # 解除=本地车道全类别旧行为


def test_forward_env_registered():
    """BOK_FILLER_CLOUD 已立法（prod 封闭 env 面可达，tools/bokctl/env.py）。"""
    assert '"BOK_FILLER_CLOUD"' in bok_source()
