"""脚本面出站守卫单点（Mimosa SSRF 修复，2026-09-23，scan-…44f1c2c94126）。

探针/e2e/load 脚本的出站基址清一色「env 可配、默认环回」（CONTROL_PLANE_URL /
MLX_LLM_BASE_URL / sidecar 端口族）——坏配置或被污染的 env 会把带凭据的
请求（BOK_CP_TOKEN Bearer / LiveKit token）外送到任意主机。本模块=脚本侧
统一闸门，两档：

- ``gate()``＝「本地诊断白名单」档（bok_voice_core.urlguard 的
  assert_local_diag_url：http/https + 环回 host；云端 CP/LLM 真栈测试经
  ``BOK_PROBE_EXTRA_HOSTS=host1,host2`` 显式 opt-in）——默认环回的探针族用。
- ``gate_public()``＝「显式公网」档（assert_public_http_url：http/https +
  地址类判定，环回/私网/链路本地默认拒；私网/自建测试端点经
  ``BOK_PROBE_ALLOW_PRIVATE=1`` 显式 opt-in）——云端探针（默认打公网官方
  API）用，如 probe_cloud_asr_ab 的 CLOUD_URL。

用法（脚本 URL 常量定义之后一行）::

    from urlguard_gate import gate
    gate(CONTROL_PLANE_URL)

多个基址一次过：``gate(CP_URL, LLM_URL)``。不过白名单 → stderr 一行说明
（含扩展口提示）+ SystemExit(2)，绝不带坏端点发请求。

自举：本模块自行把 packages/core 插 sys.path（与 mine_qa 同款），调用方
零 bootstrap。纯离线可测（tests/test_urlguard.py 覆盖底层语义）。
"""

from __future__ import annotations
# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))


import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

_ROOT = Path(__file__).resolve().parents[2]
_CORE = str(_ROOT / "packages" / "core")
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)

from bok_voice_core.urlguard import (  # noqa: E402
    UrlGuardError,
    assert_local_diag_url,
    assert_public_http_url as _core_public_http_url,
)


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


# 公网档私网/自建端点 opt-in（布尔语义）。不复用 BOK_PROBE_EXTRA_HOSTS：那是
# 环回白名单的 host 字符串扩列（assert_local_diag_url 语义），与本档按地址类
# 判定不同构；dev 面 env，不进 _FORWARD_ENV（同 BOK_PROBE_EXTRA_HOSTS，
# worker/prod 不消费）。
_ALLOW_PRIVATE_ENV = "BOK_PROBE_ALLOW_PRIVATE"


def _allow_private_opt_in() -> bool:
    return os.environ.get(_ALLOW_PRIVATE_ENV, "").strip() == "1"


def assert_public_http_url(url: str, *, allow_private: "bool | None" = None) -> str:
    """scripts 侧公网出站档：core 同名守卫（地址类判定）+ 拒 userinfo 的薄包装。

    与 core ``bok_voice_core.urlguard.assert_public_http_url`` 同名：本模块
    是脚本侧门面，比 core 版多一道 userinfo 拒绝（user:pass@host 形态拒——
    探针端点 URL 不该携带凭据），且 ``allow_private`` 缺省跟随 env
    ``BOK_PROBE_ALLOW_PRIVATE``（"1"=放行私网，与 core 档 assert_local_diag_url
    读 BOK_PROBE_EXTRA_HOSTS 同款守卫内读 env 惯例）；显式传 True/False 覆盖
    env。其余校验深度与 core 档同深（**非仅字面校验**）：http/https scheme、
    host 非空；host 为 IP 字面量时按地址类拒环回/私网/链路本地/保留段；为
    域名时 DNS 解析后逐地址核类（DNS 失败 fail-closed）。私网放行（无论来自
    env 还是显式参）对链路本地（云元数据 169.254.169.254 族）无效——恒拒。
    返回原 url；拒=UrlGuardError。
    """
    if allow_private is None:
        allow_private = _allow_private_opt_in()
    parts = urlsplit(str(url or "").strip())
    if parts.username is not None or parts.password is not None:
        raise UrlGuardError("url must not carry userinfo (user:pass@host)")
    return _core_public_http_url(url, allow_private=allow_private)


def gate_public(*urls: str) -> None:
    """云端探针启动期出站守卫（公网档）：全部 URL 过公网档，任一不过即退出。

    与 ``gate()``（本地诊断白名单档）的分工：云端探针默认打公网官方 API——
    判定=地址类（公网域名/公网 IP 放行，环回/私网/链路本地/保留段默认拒），
    不是环回字符串白名单。私网/自建测试端点逃生口=env
    ``BOK_PROBE_ALLOW_PRIVATE=1`` 显式 opt-in（云元数据段无口子）。
    校验深度=启动期全验（含 DNS）——云端探针随后必发真请求，多一次解析
    零成本，DNS 坏在发第一个请求前正合 fail-fast。任一不过 → stderr 一行
    （含 opt-in 提示）+ SystemExit(2)。
    """
    allow_private = _allow_private_opt_in()
    problems: list[str] = []
    for u in urls:
        try:
            assert_public_http_url(str(u or ""), allow_private=allow_private)
        except UrlGuardError as exc:
            problems.append(str(exc))
    if problems:
        hint = (
            f"私网/自建测试端点请设 {_ALLOW_PRIVATE_ENV}=1 显式放行"
            if not allow_private
            else f"{_ALLOW_PRIVATE_ENV}=1 已放行私网——以下为元数据段等恒拒地址"
        )
        print(
            f"[urlguard] 公网出站端点未过闸（{hint}）:\n  " + "\n  ".join(problems),
            file=sys.stderr,
        )
        raise SystemExit(2)
