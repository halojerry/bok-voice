"""bokctl.commands.misc —— `tts-pregen` / `tts-mine` / `clean-testdata`
（G2 W③ 自 core 搬入；tts 两件携带 W③ 随波真修：本地 dict 改名 sub_env、
本地 run 结果改名 res，防遮蔽 bokctl.env / bokctl.proc 模块——W② d5aeab0
的机械改写 `env._bake_ssl_cert_file(env,…)` 曾令两命令实弹必炸）。

出站 HTTP 护栏 `_http_call` 仍住 bokctl.core，穿 ``core.X`` call-time 取；
SSL 烘焙穿 ``env.X``；路径锚穿 ``paths.X``（patch 缝=模块属性）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from bokctl import core, env, paths


def cmd_tts_pregen(extra: list[str] | None = None) -> int:
    """离线批量预合成 TTS 本地缓存(docs/superpowers/specs/2026-09-08-tts-cache-design.md)。

    额外参数原样透传给 scripts/runtime/pregen_tts.py(--greetings/--objects/--fillers/--cp/--model)。
    子进程带仓库 PYTHONPATH 与 SSL_CERT_FILE(certifi)——venv 无系统 CA,
    MiniMax WSS 无此必炸。
    """
    # 本地 dict 名 sub_env(原 env)：W③ 前置修复——本地 `env` 遮蔽 bokctl.env
    # 模块,d5aeab0 的机械改写 `env._bake_ssl_cert_file(...)` 实际解析到 dict 属性
    # =AttributeError(--help 不执行函数体,单测未覆盖=潜伏)。proc→res 同理
    # (遮蔽 bokctl.proc 模块,纯防遮蔽改名)。
    sub_env = {"PYTHONPATH": paths._repo_pythonpath(), "PYTHONUNBUFFERED": "1"}
    env._bake_ssl_cert_file(sub_env, paths.repo_python())
    res = subprocess.run(
        [str(paths.repo_python()), str(paths.ROOT / "scripts" / "runtime" / "pregen_tts.py"), *(extra or [])],
        env={**os.environ, **sub_env},
    )
    return res.returncode


def cmd_tts_mine(extra: list[str] | None = None) -> int:
    """高频问答对挖掘报告(快答库,PR-3)。参数透传给 scripts/runtime/mine_qa.py。

    --apply N 把前 N 条入库为 qa_entries(source=mined);入库后跑
    `bok.py tts-pregen` 物化应答音频,闸门只认缓存有音频的条目。
    """
    # 本地 dict 名 sub_env/res：同 cmd_tts_pregen 的遮蔽修复(见彼处注释)。
    sub_env = {"PYTHONPATH": paths._repo_pythonpath(), "PYTHONUNBUFFERED": "1"}
    env._bake_ssl_cert_file(sub_env, paths.repo_python())
    res = subprocess.run(
        [str(paths.repo_python()), str(paths.ROOT / "scripts" / "runtime" / "mine_qa.py"), *(extra or [])],
        env={**os.environ, **sub_env},
    )
    return res.returncode


# clean-testdata 的 CP base（模块级读 env：进程启动时即定值；同时把「env 读取」
# 移出函数作用域（路径正则/鉴权头在闭包外构造，函数内只留白名单闸+_http_call，
# 静态污点分析可完整看见净化链）
_CP_CLEAN_BASE_URL = os.environ.get("BOK_CP_URL", "http://127.0.0.1:8000")


def cmd_clean_testdata() -> int:
    """清理历史测试数据(QA B3/B7,2026-09-09):对象下拉曾被 300+ E2E/soak 残留灌满。

    默认 dry-run 只打印;`--apply` 才真删(经 CP API,审计可追溯)。范围:
    ①对象 display_name 匹配测试前缀;②人设同名同公司重复(保留最早)。
    """
    import re as _re

    base = _CP_CLEAN_BASE_URL
    apply_mode = "--apply" in sys.argv
    token = os.environ.get("BOK_CP_TOKEN", "")
    # 数据驱动路径白名单：删除目标的 id 来自 CP 响应（o['id']），只认
    # objects/personas 资源 + uuid hex 段，其余（含 ../、斜杠夹带）一律拒发。
    _cp_path_re = _re.compile(r"^/api/(objects|personas)(/[A-Za-z0-9._-]{1,64})?$")
    _auth_headers = {"Authorization": f"Bearer {token}"} if token else None

    def _get(path: str):
        if not _cp_path_re.match(path):
            raise ValueError(f"CP 路径未过白名单（拒发）: {path!r}")
        status, raw = core._http_call(f"{base}{path}", headers=_auth_headers, timeout_s=15)
        if status != 200:
            raise RuntimeError(f"CP GET {path} -> HTTP {status}")
        return json.loads(raw.decode())

    def _delete(path: str) -> None:
        if not _cp_path_re.match(path):
            raise ValueError(f"CP 路径未过白名单（拒发）: {path!r}")
        status, _raw = core._http_call(f"{base}{path}", "DELETE", headers=_auth_headers, timeout_s=15)
        if status not in (200, 204):
            raise RuntimeError(f"CP DELETE {path} -> HTTP {status}")

    pat = _re.compile(r"^(E2E-|soak\d*-?|并发|LOAD-|边角-|多轮-|probe)")
    objs = _get("/api/objects")
    stale = [o for o in objs if pat.match(str(o.get("display_name") or ""))]
    print(f"objects: total={len(objs)} stale-matched={len(stale)}")
    for o in stale:
        print(f"  - {o['id']} {o.get('display_name')}")
        if apply_mode:
            _delete(f"/api/objects/{o['id']}")

    seen: set[tuple[str, str]] = set()
    dupes = []
    for p_ in _get("/api/personas"):
        k = (str(p_.get("name") or ""), str(p_.get("company") or ""))
        if k in seen:
            dupes.append(p_)
        else:
            seen.add(k)
    print(f"personas: duplicate-matched={len(dupes)}")
    for p_ in dupes:
        print(f"  - {p_['id']} {p_.get('name')} / {p_.get('company')}")
        if apply_mode:
            _delete(f"/api/personas/{p_['id']}")

    if not apply_mode:
        print("dry-run: 未删除任何数据。加 --apply 执行。")
    else:
        print("apply done。")
    return 0


def run(args) -> int:
    if args.cmd == "tts-pregen":
        return cmd_tts_pregen(getattr(args, "extra", None))
    if args.cmd == "tts-mine":
        return cmd_tts_mine(getattr(args, "extra", None))
    return cmd_clean_testdata()
