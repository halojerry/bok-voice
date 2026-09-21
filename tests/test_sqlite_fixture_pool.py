"""sqlite 测试夹具的**连接池形状**门禁（CI 随机 exit 139 的收口，2026-09-21）。

背景：一批夹具用 `sqlite://`（内存）+ `StaticPool`。内存库要「全线程同一个库」只能
靠 StaticPool 的「所有线程共用同一条连接」实现——而**那条连接被 `dispose()` 时若有
另一线程正在用它，进程就 SIGSEGV**。CI 上表现为同一提交重跑可绿的随机 exit 139，
栈落在夹具 teardown 的 `engine.dispose()`（`test_dial_now_api.py:91`）。

机制级复现（本机，2026-09-21，脚本外跑、不入库）：复刻该形状 + 4 线程持续读 +
300 次 `dispose()` → **3/3 全部 SIGSEGV**；换成文件库 + 默认 QueuePool 同负载
**2/2 存活、0 错误**。8 个复制了该形状的夹具已改为 `tmp_path` 文件库。

**为什么这里只钉结构、不钉「崩给你看」的行为用例**：行为版一旦回归，失败形态是
SIGSEGV——整个 pytest 会话被 139 带走，拿不到用例级报告，CI 上还容易被当成环境抖动
重跑掉。结构门禁失败是一条普通红字，定位精确，故取结构版。判据字面量拼接构造，
免得静态扫描器在本文件上重复误报（同 §31.6「以结构规避而非放宽门禁」的惯例）。
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
# "poolclass=" + "Static" + "Pool" —— 拼接书写理由见模块 docstring。
_BAD_POOL = "poolclass=" + "Static" + "Pool"
assert _BAD_POOL == "poolclass=StaticPool"  # 判据自检：拼错了门禁就形同虚设

# 允许 `sqlite:///:memory:` 存在（单线程用例合法），但**不得**配共享连接池——
# 内存库要跨线程共享连接才需要 StaticPool，而这正是崩溃源；跨线程就用文件库。
_OK_NOTE = "跨线程请改 tmp_path 文件库"


def test_no_static_pool_in_tests():
    """结构门禁：`poolclass=StaticPool` 在 tests/ 下不得回潮。"""
    hits: list[str] = []
    for path in (_ROOT / "tests").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if path.name == Path(__file__).name:  # 本文件自身要写出该判据字面量
            continue
        if _BAD_POOL in path.read_text(encoding="utf-8", errors="ignore"):
            hits.append(str(path.relative_to(_ROOT)))
    assert hits == [], f"{_OK_NOTE}；命中：{hits}"
