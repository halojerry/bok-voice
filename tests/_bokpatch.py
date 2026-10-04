"""bok patch 夹具(G2 W①,2026-10-04)。

为什么存在:bok.py 已退为门面 launcher(实现在 bokctl.core)——直接
``monkeypatch.setattr(bok, "X", v)`` 只改门面命名空间,core 内函数读自己的
模块全局,补丁静默失效(假绿)。本夹具把「属性名 → 权威模块」收成一张表:
W① 全部指向 bokctl.core;W② 起域模块逐个搬出,搬哪个域就改哪几行——
测试面零再改(治理计划 §G2 纪律:动的是映射表,不是 212 处调用点)。
"""
from __future__ import annotations

# 权威定义所在模块映射。W② 搬运时逐行改道,例如:
#   "app_data_dir": "bokctl.paths",
#   "_FORWARD_ENV": "bokctl.env",
PATCH_TARGETS: dict[str, str] = {
    # prod 域(W② 搬出)
    "cmd_prod": "bokctl.prod",
}

_DEFAULT_TARGET = "bokctl.core"


def bok_target(name: str) -> str:
    """属性名 → '模块.属性' 定位串。"""
    return f"{PATCH_TARGETS.get(name, _DEFAULT_TARGET)}.{name}"


def patch_bok(monkeypatch, name: str, value, *, raising: bool = True) -> None:
    """按权威模块打补丁(默认 bokctl.core;PATCH_TARGETS 可改道)。"""
    monkeypatch.setattr(bok_target(name), value, raising=raising)
