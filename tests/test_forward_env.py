"""`_FORWARD_ENV` 立法门禁（2026-09-19，spec §5 卫生项）。

背景：`_agent_worker_env`/`_agent_prod_env` 是白名单 env——dev 靠 `_start_proc`
merge `os.environ` 全键皆活，prod（launchd/schtasks 封闭面）只带白名单。历史上
agent 实读 87 键只有 5 键进表，69 键在 prod 全是死门（BOK_FLOW_GRAPH prod
kill-switch、BOK_CP_TOKEN auth-on worker 上报，两次实弹同病）。

立法契约：**agent_runtime 新读一个 env 键，必须三选一登记**：
  ① 进 `bok._FORWARD_ENV`（运营可调键——kill-switch/调参/凭据）；
  ② bok.py 既有注入面已提供（computed 键如 MLX_LLM_MODEL、sidecar asr_env、
     `_interp_env` B 线注入）——静态扫 bok.py 全部 env 写入点自动认；
  ③ 进本文件 `_EXEMPT` 且写明理由（测试腿专用/OS 变量带回退）。
三处都不沾 → 本测试红——死门在 CI 层根治，唔靠人记。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bok  # noqa: E402

_AGENT_DIR = Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime"
_BOK_SRC = Path(__file__).resolve().parents[1] / "tools" / "bok.py"

# 豁免清单（键 → 理由）。动这里必须带理由；无理由的豁免 = 立法倒退。
_EXEMPT: dict[str, str] = {
    "SCRIPTED_LLM": "E2E 测试腿专用脚本化 LLM（agent.py 测试 shim，生产永设不了）",
    "SCRIPTED_LLM_EXPECT_KW": "同上（SCRIPTED_LLM 断言配件）",
    "SCRIPTED_LLM_OUTPUT": "同上（SCRIPTED_LLM 固定输出）",
    "USE_FAKE_MEDIA": "E2E 测试腿假媒体 shim，生产永设不了",
    "LOCALAPPDATA": "Windows OS 变量（tts_cache 数据目录，Path.home() 回退在手）",
}


def _agent_env_reads() -> set[str]:
    """agent_runtime 全部 `os.environ.get("KEY"`/`os.getenv("KEY"` 读取面（静态扫）。"""
    reads: set[str] = set()
    for path in sorted(_AGENT_DIR.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        reads |= set(re.findall(r'os\.environ\.get\(\s*"([A-Z][A-Z0-9_]+)"', src))
        reads |= set(re.findall(r'os\.getenv\(\s*"([A-Z][A-Z0-9_]+)"', src))
    return reads


def _bok_provides() -> set[str]:
    """bok.py 写进 worker env 的全部键（静态扫全部写入形态）+ _FORWARD_ENV 本体。

    覆盖形态：`env["X"]=`、`setdefault("X"`、dict 字面量 `"X": os.environ.get`、
    `_FORWARD_ENV` 元组、`for _k in (...)` 白名单元组、`for base in (...)` 的
    `*_REV` 动态展开。
    """
    src = _BOK_SRC.read_text(encoding="utf-8")
    provided = set(bok._FORWARD_ENV)
    provided |= set(re.findall(r'env\[?"([A-Z][A-Z0-9_]+)"?\]?\s*=', src))
    provided |= set(re.findall(r'setdefault\(\s*"([A-Z][A-Z0-9_]+)"', src))
    provided |= set(re.findall(r'"([A-Z][A-Z0-9_]+)":\s*os\.environ\.get', src))
    for tuple_pat in (r"for _k in \((.*?)\):",):
        for m in re.finditer(tuple_pat, src, re.S):
            provided |= set(re.findall(r'"([A-Z][A-Z0-9_]+)"', m.group(1)))
    m = re.search(r"for base in \((.*?)\):", src, re.S)
    if m:
        for base in re.findall(r'"([A-Z][A-Z0-9_]+)"', m.group(1)):
            provided |= {base, base + "_REV"}
    # 计算注入键（如 MLX_LLM_MODEL=model_path(...)）：字面量正则够不着——直接内省
    # 真实 worker env 输出补齐（函数纯 dict 构造，跑一次零副作用）。
    provided |= set(bok._agent_worker_env(bok.repo_python()))
    return provided


def test_every_agent_env_read_is_registered():
    """立法主判据：agent 实读键 ⊆ (_FORWARD_ENV ∪ bok 注入面 ∪ _EXEMPT)。

    新键未登记 → 本测试红，消息列缺口键——把键加进 `bok._FORWARD_ENV`（运营键）
    或 `_EXEMPT`（带理由）即绿。
    """
    unregistered = _agent_env_reads() - _bok_provides() - set(_EXEMPT)
    assert not unregistered, (
        f"agent_runtime 读取了 {len(unregistered)} 个未登记 env 键（prod 全是死门）："
        f"{sorted(unregistered)} —— 运营键进 bok._FORWARD_ENV，测试腿/OS 变量进本文件 "
        f"_EXEMPT（带理由）"
    )


def test_forward_env_alias_and_no_duplicates():
    """历史名 `_BOK_PASSTHROUGH_KEYS` 恒为表本体别名（调用面不散）；表内无重复。"""
    assert bok._BOK_PASSTHROUGH_KEYS is bok._FORWARD_ENV
    keys = list(bok._FORWARD_ENV)
    assert len(keys) == len(set(keys)), "表内有重复键"


def test_forward_env_keys_all_flow_to_dev_and_prod(monkeypatch):
    """表内每键真的流到两表（设值 → 在；这是立法的意义，唔止签名在表上）。"""
    sentinel_key = "BOK_LLM_FALLBACK"  # 立法前 prod 死门的代表键
    monkeypatch.setenv(sentinel_key, "0")
    dev = bok._agent_worker_env(bok.repo_python())
    prod = bok._agent_prod_env()
    assert dev.get(sentinel_key) == "0" and prod.get(sentinel_key) == "0"
    # 全表批量抽查：每个键设哨兵值后两表都必须带（防止表与 apply 函数脱钩）
    for key in bok._FORWARD_ENV:
        monkeypatch.setenv(key, f"sentinel-{key}")
    dev2 = bok._agent_worker_env(bok.repo_python())
    prod2 = bok._agent_prod_env()
    missing = [k for k in bok._FORWARD_ENV
               if dev2.get(k) != f"sentinel-{k}" or prod2.get(k) != f"sentinel-{k}"]
    assert not missing, f"表内键未流到 dev/prod worker env：{missing}"


def test_forward_env_absent_injects_nothing(monkeypatch):
    """未设 → 不注入（默认档零变化；表只透传运营显式设定）。"""
    for key in bok._FORWARD_ENV:
        monkeypatch.delenv(key, raising=False)
    assert not any(k in bok._agent_worker_env(bok.repo_python()) for k in bok._FORWARD_ENV)
    assert not any(k in bok._agent_prod_env() for k in bok._FORWARD_ENV)


def test_exempt_entries_still_read_by_agent():
    """豁免清单的键必须仍是 agent 实读键（豁免错键/删了读取面即红——防豁免面腐烂）。"""
    reads = _agent_env_reads()
    stale = [k for k in _EXEMPT if k not in reads]
    assert not stale, f"豁免清单里的键 agent 已不再读取（请移除豁免）：{stale}"
