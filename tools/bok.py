"""bok 门面 launcher(G2 W①,2026-10-04)。

实现已整体迁至 ``bokctl.core``(0 行改动搬运,除三处 __file__ 锚点校正);
本文件只做两件事:①把 core 的全部名字(含下划线面——``import *`` 不带,
测试/门禁直接点名 ``bok._FORWARD_ENV`` 等)镜像到 ``bok`` 模块面;②CLI
入口分发。任何行为改动都进 ``bokctl/``,本文件保持瘦壳(W② 起域模块从
core 逐个搬出,搬运纪律与 PATCH_TARGETS 见 tests/_bokpatch.py)。
"""
import sys as _sys
from pathlib import Path as _Path

_HERE = _Path(__file__).resolve().parent
if str(_HERE) not in _sys.path:  # 兼容「从任意 cwd 直接执行本文件」
    _sys.path.insert(0, str(_HERE))

import bokctl.core as _core  # noqa: E402

# 镜像 core 全部顶层名字(不含 dunder)。函数仍住在 core——这里的赋值只是
# 让 `from bok import X` / `bok.X` 继续成立;monkeypatch 必须打在权威定义
# 处(tests/_bokpatch.py 单点)。
_ns = {k: v for k, v in vars(_core).items() if not k.startswith("__")}
globals().update(_ns)
__all__ = list(_ns)

if __name__ == "__main__":
    raise SystemExit(_core.main())
