"""测试对象命名族判定(单源)——agent 心跳豁免与 CP 挖掘过滤共用。"""

from __future__ import annotations

import re

# 与 E2E 脚本/压测的对象命名约定同步(clean-testdata 前缀族);
# 改这里要同步 scripts/e2e_* 与 AGENTS.md「测试对象心跳豁免」段。
TEST_OBJECT_NAME_RE = re.compile(r"^(E2E-|soak\d*-?|并发|LOAD-|边角-|多轮-|probe)")


def is_test_object_name(name: str | None) -> bool:
    return bool(name) and bool(TEST_OBJECT_NAME_RE.match(str(name)))
