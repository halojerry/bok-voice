"""脚本面出站守卫单点（Mimosa SSRF 修复，2026-09-23，scan-…44f1c2c94126）。

探针/e2e/load 脚本的出站基址清一色「env 可配、默认环回」（CONTROL_PLANE_URL /
MLX_LLM_BASE_URL / sidecar 端口族）——坏配置或被污染的 env 会把带凭据的
请求（BOK_CP_TOKEN Bearer / LiveKit token）外送到任意主机。本模块=脚本侧
统一闸门：启动期对全部出站基址过「本地诊断白名单」（bok_voice_core.urlguard
的 assert_local_diag_url：http/https + 环回 host；云端 CP/LLM 真栈测试经
``BOK_PROBE_EXTRA_HOSTS=host1,host2`` 显式 opt-in）。

用法（脚本 URL 常量定义之后一行）::

    from urlguard_gate import gate
    gate(CONTROL_PLANE_URL)

多个基址一次过：``gate(CP_URL, LLM_URL)``。不过白名单 → stderr 一行说明
（含扩展口提示）+ SystemExit(2)，绝不带坏端点发请求。

自举：本模块自行把 packages/core 插 sys.path（与 mine_qa 同款），调用方
零 bootstrap。纯离线可测（tests/test_urlguard.py 覆盖底层语义）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_CORE = str(_ROOT / "packages" / "core")
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)

from bok_voice_core.urlguard import UrlGuardError, assert_local_diag_url  # noqa: E402


def gate(*urls: str, extra_hosts: "tuple[str, ...] | frozenset[str]" = ()) -> None:
    """脚本启动期出站守卫：全部 URL 过本地诊断白名单，任一不过即退出。

    fail-fast（SystemExit(2)）：诊断脚本面对的应是钉死端点，坏配置要在发
    第一个请求前死掉，不要半途而废留垃圾状态。env 扩展口=
    ``BOK_PROBE_EXTRA_HOSTS``（逗号分隔 host；云端测试显式声明目标）。
    """
    problems: list[str] = []
    for u in urls:
        try:
            assert_local_diag_url(str(u or ""), extra_hosts=extra_hosts)
        except UrlGuardError as exc:
            problems.append(str(exc))
    if problems:
        print(
            "[urlguard] 出站端点未过本地诊断白名单（云端测试请设 "
            "BOK_PROBE_EXTRA_HOSTS=host1,host2 显式扩展）:\n  "
            + "\n  ".join(problems),
            file=sys.stderr,
        )
        raise SystemExit(2)
