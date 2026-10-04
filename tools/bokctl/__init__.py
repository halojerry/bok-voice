"""bokctl —— bok.py 的包化骨架(G2 W①,2026-10-04)。

core.py 承载今天的全部实现;bok.py 退为门面 launcher。W② 起域模块逐个
从 core 搬出,搬运纪律与 PATCH_TARGETS 见 tests/_bokpatch.py。
"""
# W② 起:域模块在此并 import(此刻 core/prod;core 内 `from bokctl import prod`
# 靠 sys.modules 部分初始化解析,prod 只在调用期穿 core.X 取名,无环风险)。
from . import core, prod  # noqa: F401

