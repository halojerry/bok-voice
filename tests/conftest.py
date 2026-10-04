from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for part in (
    "packages/core",
    "packages/business-db",
    "packages/knowledge",
    "packages/observability",
    "apps/control-plane",
    "apps/agent",
    # scripts/（2026-09-23，SSRF 守卫接线）：按路径 exec_module 加载探针脚本的
    # 测试（test_load_cp_db_reservation 等）没有 scripts/ 在 sys.path——脚本侧
    # `from urlguard_gate import gate` 会 ModuleNotFoundError。此处单点补齐
    # （直接 `python scripts/x.py` 运行时 sys.path[0] 本就是 scripts/，零影响）。
    "scripts",
    # scripts/runtime/（2026-10-04 G1b）：产品运行时三件（pregen_tts/mock_callee/
    # mine_qa）入桶后，`import pregen_tts` 等 8 个测试文件的模块导入面。
    "scripts/runtime",
):
    path = ROOT / part
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# 结算闲时门（2026-09-25 车道卫生）测试面全局关闭：settle 相关测试的临时库里
# 常有 ACTIVE/RINGING 状态的通话行，默认 300s 等待会把每个 settle 测试拖成
# 5 分钟级。门的时间语义由 test_settle_idle_gate.py 单独钉；需要开门的测试
# 显式 setenv 覆盖本 setdefault 即可。
os.environ.setdefault("BOK_SETTLE_IDLE_WAIT_S", "0")

# 登录频控（30/min per username）测试面全局关闭：全量套件对同一批测试用户名的
# login 调用远超 30 次/分钟——CI 与本地全量都会在 test_scope/test_security_
# hardening 等处随机 429（曾经被误记为「序耦合 flaky」的真身）。频控本身的
# 行为由 test_login_rate_limit.py 单独钉（其 fixture delenv 后吃代码缺省 "1"）；
# 需要开闸的其他测试显式 setenv 覆盖即可。
os.environ.setdefault("BOK_LOGIN_RATE_LIMIT", "0")
