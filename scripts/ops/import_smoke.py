#!/usr/bin/env python3
"""逐脚本 import 冒烟（G1c 验收件，2026-10-04）。

为什么存在:G1 引导头保证「脚本挪进任何桶后裸 import 兄弟模块继续解析」——
但这是运行期性质,静态检查(语法/引用扫描)看不见。本件对每个受管脚本按
「自身目录进 sys.path[0] + 裸 import 名」的姿势逐个**进程内**装载执行:
搬错桶/漏改 import/头丢失 = ImportError 立刻现形(治理计划 §G1c 验收令)。

隔离纪律(进程内装载的保真三件套):
  * 每个脚本装载前记 sys.path 快照、装载后截回——上一脚本的 G1 引导头
    插的目录不泄漏给下一脚本的解析(否则会掩盖下一脚本自己的头坏了);
  * 每轮后把**全部受管脚本名**从 sys.modules 弹出——`import urlguard_gate`
    命中缓存而不走本轮解析的假绿被消灭;
  * SystemExit/一切异常都算 FAIL——顶层副作用炸(如 import 期 argparse)现形。

分类纪律(自维护,不养冻结 skip 清单):
  * SKIP-DEP = ModuleNotFoundError 且缺的模块不是受管脚本名(torch 等
    精简环境不装的训练栈)——照实打印,不算失败;缺的名字若是受管脚本名
    → FAIL(桶闭包破了);
  * 其余一切失败 = FAIL(ImportError/NameError/SyntaxError/SystemExit…)。

已知边界(相对子进程形态的取舍):无每文件独立超时——仓纪律=脚本顶层零
阻塞副作用(main 守卫),本件本地全量绿即证明;CI 步骤级超时兜底灾难。

用法:
  .venv312/bin/python scripts/ops/import_smoke.py
  .venv312/bin/python scripts/ops/import_smoke.py --fail-fast
退出码:全部成功或 SKIP-DEP → 0;任一 FAIL → 1。
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

import argparse
import importlib.util
import re
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
EXCLUDE_PARTS = frozenset({"cuda", "artifacts", "__pycache__", "archive"})
# archive/ = 退役件,不承诺可 import(CI 也不跑);cuda/ = 独立交付包。


def managed_scripts() -> list[Path]:
    return sorted(
        p
        for p in SCRIPTS.rglob("*.py")
        if not (EXCLUDE_PARTS & set(p.relative_to(SCRIPTS).parts))
    )


def classify(exc: BaseException, managed_stems: set[str]) -> tuple[str, str]:
    """(verdict, detail):verdict ∈ fail|skip-dep(调用方先试装载,成功不打这)。"""
    # 找异常链里最根因的 ModuleNotFoundError
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in chain:
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    for e in reversed(chain):
        m = re.search(r"No module named '([^']+)'", repr(e))
        if m and e.__class__.__name__ == "ModuleNotFoundError":
            if m.group(1).split(".")[0] not in managed_stems:
                return "skip-dep", f"缺第三方依赖 {m.group(1)}"
            return "fail", f"仓内模块解析失败: {m.group(1)}(桶闭包破了?)"
    tail = "\n".join(traceback.format_exception(exc)[-3:]).strip()
    return "fail", tail


def main() -> int:
    ap = argparse.ArgumentParser(description="逐脚本 import 冒烟(G1c):每个受管脚本按直接运行姿势进程内装载")
    ap.add_argument("--fail-fast", action="store_true", help="首个 FAIL 即停")
    args = ap.parse_args()

    files = managed_scripts()
    stems = {p.stem for p in files}
    fails: list[tuple[str, str]] = []
    skips: list[tuple[str, str]] = []
    ok = 0
    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        path_snapshot = list(sys.path)
        sys.path.insert(0, str(p.parent))
        try:
            spec = importlib.util.spec_from_file_location(p.stem, p)
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            # 注册后再 exec:dataclasses._is_type 解析字符串注解时要查
            # sys.modules[cls.__module__].__dict__——不注册则含 dataclass 的模块
            # (带 from __future__ import annotations)必 NoneType 假炸。
            sys.modules[p.stem] = mod
            spec.loader.exec_module(mod)
            ok += 1
            print(f"ok     {rel}")
        except BaseException as exc:  # SystemExit 也算失败(顶层副作用)
            verdict, detail = classify(exc, stems)
            if verdict == "skip-dep":
                skips.append((rel, detail))
                print(f"SKIP-DEP {rel}: {detail}")
            else:
                fails.append((rel, detail))
                print(f"FAIL   {rel}: {detail}")
        finally:
            sys.path[:] = path_snapshot
            for s in stems:  # 消缓存:下一脚本必须走自己的解析
                sys.modules.pop(s, None)

        if fails and args.fail_fast:
            break

    print(f"\n[import-smoke] total={len(files)} ok={ok} skip-dep={len(skips)} fail={len(fails)}")
    if skips:
        print("[import-smoke] skip-dep 明细(第三方缺席,精简环境预期内):")
        for rel, detail in skips:
            print(f"  - {rel}: {detail}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
