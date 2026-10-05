"""bokctl.commands —— 子命令实现包（G2 W③，2026-10-05）。

一个子命令一个模块；本 ``__init__`` 只留 docstring、**零 submodule import**
（环安全：命令模块互引一律 ``from bokctl import X`` 拿模块对象、call-time
属性取用，加载序由 bokctl.core → bokctl.cli 的显式 import 链钉住）。子命令
名 → 模块的分派表住 bokctl.cli（_COMMANDS）。
"""
