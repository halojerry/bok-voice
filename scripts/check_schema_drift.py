#!/usr/bin/env python3
"""Supabase schema 漂移门禁:scripts/.p0_supabase_schema.sql 产物 ≡ build_engine() 代码?

为什么存在:产物是「生成物,勿手改」,但生成后代码会继续演进——deps.build_engine()
加列/改模型而忘记重跑 scripts/dump_postgres_ddl.py,新部署就拿旧 schema 引导,再靠
启动幂等迁移悄悄补列(静默事故)。本门禁在真 Postgres(pgvector)上把**两个漂移方向**
都钉死:

  * 产物过期(缺列/缺表/缺索引):产物应用后的库上再跑 build_engine,它若还想做
    DDL,dump 必然多出行 → 漂移;
  * 产物含死对象(代码已删的表/列):build_engine 从不 DROP(全 additive),单靠
    上一条比不出来——所以另建一个**全新库只跑 build_engine** 作「代码真源基线」,
    与产物库 dump 做双向行级比对:基线没有而产物有的行 = 死对象,反之 = 产物缺。

方法(为何不比 DDL 文本):产物本身就是 pg_dump 产物;ORM/text DDL 与 pg_dump 输出
在引号/换行/类型拼写上永远对不齐,逐字 diff 全是噪声。把两边都落成真库、经**同一把
pg_dump 量尺**(同容器同版本客户端)度量后,归一化只需剥 psql 元命令/注释行
(复用 dump_postgres_ddl.py 的 _strip_psql_meta/_strip_comment_lines)。

流程:
  ① 起一次性容器 schema-drift-*(宿主口默认 5441,镜像必须带 pgvector)或直连
     DATABASE_URL(CI 服务容器模式,不碰 docker)/复用 --source-container;
  ② 剥元命令后 psql ON_ERROR_STOP=1 应用产物 → dump A;
  ③ 同库跑 build_engine()(幂等迁移入口,smoke_postgres 同款 sys.path 自举)→
     dump B;被 except 吞掉的 [deps] 迁移失败行直接判漂移(迁移想做 DDL 而失败,
     dump 看不见 = 假绿洞);
  ④ 另建全新库只跑 build_engine() → dump F;
  ⑤ 归一化后逐行比对:A==B 且 A==F(行多重集相等;仅顺序差异视为等价——pg_dump
     的 presentation order 不属 schema 语义)⇒ exit 0;否则打印完整 unified diff。

用法:
    python scripts/check_schema_drift.py                     # 自起容器(127.0.0.1:5441)
    python scripts/check_schema_drift.py --keep-container    # 调试:保留自起的容器
    DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres \\
        python scripts/check_schema_drift.py                 # 直连模式(CI 服务容器)
    python scripts/check_schema_drift.py --source-container pg-ddl \\
        --database-url postgresql+psycopg://postgres:postgres@127.0.0.1:5434/postgres

退出码:0=等价(产物 ≡ 代码);1=检出漂移(含被吞迁移失败);2=环境不齐
  (docker 不可用/容器起不来/产物应用失败/dump 空/build_engine 返回 None/
  直连模式缺 psql·pg_dump 或客户端版本低于服务端)。

前置:镜像必须带 pgvector(vanilla postgres:16 会让 psql 应用卡死在 CREATE EXTENSION
  vector、令 build_engine 静默跳过 knowledge_chunks——产物含知识库整块,无从等价)。
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import io
import os
import re
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent

# smoke_postgres/dump_postgres_ddl 同款路径自举:本地裸 venv 也能 import 仓库内包;
# CI 里 workflow 已 pip install -e 各包,插入重复路径是无害 no-op。
for _sub in (
    "apps/control-plane",
    "packages/core",
    "packages/business-db",
    "packages/knowledge",
    "packages/observability",
):
    _p = str(ROOT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# 复用姊妹脚本的归一化/容器姿势(别复制逻辑):_strip_psql_meta/_strip_comment_lines/
# _pg_dump_sql 同款 flags/_wait_ready/_docker_available/_container_exists/_build_schema;
# smoke_postgres 的 _check_deps_diagnostics 语义(被吞迁移=失败)。
import dump_postgres_ddl as _ddl  # noqa: E402
import smoke_postgres as _smoke  # noqa: E402

PG_USER = _ddl.PG_USER
PG_DB = _ddl.PG_DB
DEFAULT_ARTIFACT = ROOT / "scripts" / ".p0_supabase_schema.sql"
DEFAULT_IMAGE = "pgvector/pgvector:pg16"  # 必须带 pgvector,见模块 docstring 前置
DEFAULT_PORT = 5441  # 5432-5438 常被本机其他栈占用,门禁专属宿主口
CONTAINER_PREFIX = "schema-drift"
_DUMP_FLAGS = ("--schema-only", "--no-owner", "--no-privileges")
_LOCAL_DUMP_TIMEOUT_S = 300


class GateEnvError(RuntimeError):
    """环境不齐(docker/容器/应用失败/dump 空/客户端缺失)→ exit 2。"""


class DriftDetected(RuntimeError):
    """产物与代码不等价(或迁移被吞)→ exit 1。"""


def _log(msg: str) -> None:
    print(f"[drift] {msg}", flush=True)


def _normalize(raw: str) -> list[str]:
    """pg_dump 原文 → 比对行:剥 psql 元命令(\\restrict 等)再剥注释行/空行。"""
    body, _n_meta = _ddl._strip_psql_meta(raw)
    return _ddl._strip_comment_lines(body)


def _assert_has_tables(lines: list[str], what: str) -> None:
    if not any(ln.startswith("CREATE TABLE ") for ln in lines):
        raise GateEnvError(f"{what} 的归一化 dump 里没有 CREATE TABLE(库空/应用失败/dump 失败)")


def _coerce_psycopg_url(url: str) -> str:
    """与 smoke_postgres.main 同款:postgresql:// 会被 SQLAlchemy 解析成 psycopg2(本仓不装)。"""
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _mask_url(url: str) -> str:
    from sqlalchemy.engine.url import make_url

    return make_url(url).render_as_string(hide_password=True)


# ---------------------------------------------------------------------------
# dump / 应用(psql)/ 建删库 —— 容器(docker exec)与直连(本地客户端)双通道
# ---------------------------------------------------------------------------

def _dump_schema_sql(container: str | None, url: str, db: str) -> str:
    """--schema-only --no-owner --no-privileges;容器模式走容器内 pg_dump(版本与库严格一致)。"""
    try:
        if container is not None:
            proc = subprocess.run(
                ["docker", "exec", container, "pg_dump", "-U", PG_USER, *_DUMP_FLAGS, db],
                capture_output=True, text=True, timeout=_LOCAL_DUMP_TIMEOUT_S, check=True,
            )
        else:
            from sqlalchemy.engine.url import make_url

            u = make_url(url)
            proc = subprocess.run(
                [
                    "pg_dump", *_DUMP_FLAGS,
                    "-h", str(u.host), "-p", str(u.port), "-U", str(u.username), db,
                ],
                capture_output=True, text=True, timeout=_LOCAL_DUMP_TIMEOUT_S,
                env=dict(os.environ, PGPASSWORD=u.password or ""),
            )
            if proc.returncode != 0:
                raise subprocess.CalledProcessError(proc.returncode, proc.args, proc.stdout, proc.stderr)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise GateEnvError(f"pg_dump({db}) 失败: {exc}\n{stderr[-2000:]}") from exc
    return proc.stdout


def _apply_artifact(container: str | None, url: str, sql_text: str) -> None:
    """psql ON_ERROR_STOP=1 整文件应用产物;任何语句失败即环境错误(exit 2)。"""
    from sqlalchemy.engine.url import make_url

    if container is not None:
        cmd = ["docker", "exec", "-i", container, "psql", "-U", PG_USER, "-v", "ON_ERROR_STOP=1", PG_DB]
        env = None
    else:
        u = make_url(url)
        cmd = [
            "psql", "-v", "ON_ERROR_STOP=1",
            "-h", str(u.host), "-p", str(u.port), "-U", str(u.username), "-d", str(u.database),
        ]
        env = dict(os.environ, PGPASSWORD=u.password or "")
    proc = subprocess.run(cmd, input=sql_text, capture_output=True, text=True, env=env, timeout=_LOCAL_DUMP_TIMEOUT_S)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-4000:]
        raise GateEnvError(f"应用产物失败(psql 非零退出 {proc.returncode}):\n{tail}")


def _assert_local_dump_tools(url: str) -> None:
    """直连模式前置:本地要有 psql+pg_dump,且 pg_dump 大版本 >= 服务端(pg_dump 硬要求)。"""
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    for tool in ("psql", "pg_dump"):
        if subprocess.run(["which", tool], capture_output=True).returncode != 0:
            raise GateEnvError(
                f"直连模式需要本地 {tool} 客户端(容器模式才有服务端同款工具)。"
                "装 postgresql-client(>=服务端大版本),或改用 --source-container。"
            )
    admin = create_engine(url, poolclass=NullPool)
    try:
        with admin.connect() as conn:
            server_num = int(conn.execute(text("SHOW server_version_num")).scalar() or 0)
    finally:
        with contextlib.suppress(Exception):
            admin.dispose()
    ver = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True)
    m = re.search(r"(\d+)\.", ver.stdout)
    client_major = int(m.group(1)) if m else 0
    server_major = server_num // 10000
    if client_major < server_major:
        raise GateEnvError(
            f"本地 pg_dump 大版本 {client_major} < 服务端 {server_major}(pg_dump 拒绝跨大版本导出)。"
            "装 postgresql-client-16 或改用 --source-container 让 dump 走容器内客户端。"
        )


def _create_database(base_url: str, name: str) -> str:
    """AUTOCOMMIT 建全新库(代码真源基线用);返回指向它的 SQLAlchemy URL。"""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine.url import make_url
    from sqlalchemy.pool import NullPool

    admin = create_engine(base_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        with contextlib.suppress(Exception):
            admin.dispose()
    return make_url(base_url).set(database=name).render_as_string(hide_password=False)


def _drop_database(base_url: str, name: str) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    with contextlib.suppress(Exception):
        admin = create_engine(base_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
        try:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        finally:
            with contextlib.suppress(Exception):
                admin.dispose()


def _wait_connectable(url: str, timeout_s: int = 60) -> None:
    """直连模式等库可连(服务容器即便有 healthcheck 也留一道防御)。"""
    from sqlalchemy import create_engine, select
    from sqlalchemy.pool import NullPool

    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        eng = create_engine(url, poolclass=NullPool)
        try:
            with eng.connect() as conn:
                conn.execute(select(1))
            with contextlib.suppress(Exception):
                eng.dispose()
            return
        except Exception as exc:  # noqa: BLE001 - 轮询期任何连接错误都只记下重试
            last = f"{type(exc).__name__}: {exc}"
            with contextlib.suppress(Exception):
                eng.dispose()
            time.sleep(0.5)
    raise GateEnvError(f"数据库 {timeout_s}s 内不可连({_mask_url(url)}): {last}")


# ---------------------------------------------------------------------------
# 容器生命周期(自起模式)
# ---------------------------------------------------------------------------

def _start_container(image: str, port: int) -> str:
    name = f"{CONTAINER_PREFIX}-{os.getpid()}-{int(time.time())}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
    try:
        subprocess.run(
            [
                "docker", "run", "-d", "--rm", "--name", name,
                "-p", f"127.0.0.1:{port}:5432", "-e", "POSTGRES_PASSWORD=postgres", image,
            ],
            capture_output=True, text=True, timeout=300, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise GateEnvError(
            f"起容器失败(镜像 {image},宿主口 127.0.0.1:{port} 可能被占,可 --port 换口): {exc}\n{stderr[-2000:]}"
        ) from exc
    _ddl._wait_ready(name)
    return name


def _remove_container(name: str) -> None:
    with contextlib.suppress(Exception):
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=60)


# ---------------------------------------------------------------------------
# build_engine 调用 + 被吞迁移诊断
# ---------------------------------------------------------------------------

def _run_build_engine(url: str, where: str):
    """真跑 deps.build_engine()(建表+幂等补列+数据迁移+pgvector);被吞的 [deps]
    迁移失败直接判漂移——ALTER 失败不会改 dump,只比 dump 会放走这类假绿。"""
    _log(f"{where}: 跑 build_engine()(建表 + 幂等迁移)")
    lines, engine = _ddl._build_schema(url)  # 已逐行打 [ddl] build_engine: 前缀日志
    if engine is None:  # _build_schema 内部已 SystemExit,这里是双保险
        raise GateEnvError(f"{where}: build_engine() 返回 None(未识别到 DATABASE_URL)")
    buf = io.StringIO()
    try:
        # smoke 的诊断器会把自己审过的每行再打一遍——上文已打过,这里静音,失败再回放。
        with contextlib.redirect_stdout(buf):
            _smoke._check_deps_diagnostics(lines)
    except _smoke.SmokeFailure as exc:
        raise DriftDetected(
            f"{where}: build_engine 出现被 except 吞掉的迁移失败(PG 上静默降级,"
            f"dump 看不见但部署库会缺列/缺种子): {exc}\n{buf.getvalue().strip()}"
        ) from exc
    return engine


# ---------------------------------------------------------------------------
# 比对与报告
# ---------------------------------------------------------------------------

def _print_diff(a: list[str], b: list[str], label_a: str, label_b: str) -> None:
    for ln in difflib.unified_diff(a, b, label_a, label_b, lineterm="", n=3):
        print(ln, flush=True)


def _compare(a_lines: list[str], b_lines: list[str], f_lines: list[str]) -> bool:
    """三 dump 比对;返回是否漂移。diff 全文打印(不截断)。

    * B vs A:产物应用后 build_engine 仍想做 DDL = 产物缺对象(产物过期);
    * F vs A:按行多重集比对(仅顺序差异=pg_dump presentation order,不算漂移)。
      基线独有 = 产物缺(过期);产物独有 = 代码已删的死对象。
    """
    drift = False
    if a_lines != b_lines:
        drift = True
        _log("漂移信号 ①:产物应用后的库上 build_engine 仍改变了 schema(产物缺对象)")
        extra = Counter(b_lines) - Counter(a_lines)
        _log(f"  build_engine 新增 {sum(extra.values())} 行(缺的列/表/索引):")
        for ln in sorted(extra):
            print(f"  + {ln}", flush=True)
        _print_diff(a_lines, b_lines, "artifact-db@apply", "artifact-db@build_engine")
    if a_lines == f_lines:
        return drift
    only_a = Counter(a_lines) - Counter(f_lines)
    only_f = Counter(f_lines) - Counter(a_lines)
    if not only_a and not only_f:
        _log("产物库 vs 代码基线:仅行序差异(pg_dump presentation order),多重集相等 → 视为等价")
        return drift
    drift = True
    _log("漂移信号 ②:产物 ≠ 代码真源基线(全新库只跑 build_engine)")
    _log(f"  产物独有 {sum(only_a.values())} 行(代码已删的死对象);基线独有 {sum(only_f.values())} 行(产物缺失)")
    for ln in sorted(only_a):
        print(f"  - {ln}   (artifact 独有:死对象或过期定义)", flush=True)
    for ln in sorted(only_f):
        print(f"  + {ln}   (代码基线独有:产物缺失)", flush=True)
    _print_diff(sorted(a_lines), sorted(f_lines), "artifact", "code-baseline")
    return drift


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Supabase schema 漂移门禁(产物 scripts/.p0_supabase_schema.sql vs build_engine 代码)",
    )
    ap.add_argument("--artifact", default=str(DEFAULT_ARTIFACT), help=f"产物路径(默认 {DEFAULT_ARTIFACT})")
    ap.add_argument("--image", default=DEFAULT_IMAGE, help=f"自起容器镜像,必须带 pgvector(默认 {DEFAULT_IMAGE})")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"自起容器宿主口(默认 {DEFAULT_PORT})")
    ap.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL") or "",
        help="SQLAlchemy URL(默认 env DATABASE_URL;给了它且无容器参数=直连模式,不碰 docker)",
    )
    ap.add_argument(
        "--source-container",
        default="",
        help="复用已在跑的源库容器名(dump 走 docker exec;URL 取 --database-url/env,默认 :5434 同 dump_postgres_ddl)",
    )
    ap.add_argument("--keep-container", action="store_true", help="保留自起的容器(调试;默认跑完即删)")
    ap.add_argument(
        "--start-container",
        action="store_true",
        help="强制自起容器模式(即使 env 里有 DATABASE_URL;容器自建 URL,忽略该 env)",
    )
    return ap.parse_args(argv)


def _resolve_target(args: argparse.Namespace) -> tuple[str, str | None, bool]:
    """返回 (sqlalchemy_url, container_name|None, owns_container)。"""
    if args.source_container:
        _ddl._docker_available()
        if not _ddl._container_exists(args.source_container):
            raise GateEnvError(
                f"--source-container {args.source_container} 不存在。先起一个(带 pgvector):\n"
                f"  docker run -d --name {args.source_container} -p 127.0.0.1:5434:5432 "
                f"-e POSTGRES_PASSWORD=postgres {DEFAULT_IMAGE}"
            )
        url = args.database_url or "postgresql+psycopg://postgres:postgres@127.0.0.1:5434/postgres"
        return _coerce_psycopg_url(url), args.source_container, False
    if args.start_container or not args.database_url:
        _ddl._docker_available()
        if args.database_url and args.start_container:
            _log("--start-container 生效:忽略 DATABASE_URL,容器自建 URL")
        container = _start_container(args.image, args.port)
        _log(f"自起容器 {container}(镜像 {args.image},宿主口 127.0.0.1:{args.port})")
        url = f"postgresql+psycopg://{PG_USER}:postgres@127.0.0.1:{args.port}/{PG_DB}"
        return url, container, True
    url = _coerce_psycopg_url(args.database_url.strip())
    if not url.startswith("postgresql+psycopg://"):
        raise GateEnvError(f"本门禁只跑 Postgres(需要 postgresql+psycopg://),收到 {url.split('://')[0]}://")
    _log(f"直连模式(CI 服务容器/外部库): {_mask_url(url)}")
    _assert_local_dump_tools(url)
    _wait_connectable(url)
    return url, None, False


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    artifact = Path(args.artifact)
    if not artifact.is_file():
        print(f"[drift] FATAL: 产物不存在: {artifact}", flush=True)
        return 2

    started = time.time()
    fresh_db = f"bok_drift_fresh_{uuid.uuid4().hex[:10]}"
    url = ""
    container: str | None = None
    owns_container = False
    try:
        url, container, owns_container = _resolve_target(args)

        # ---- ② 应用产物 → dump A ----
        raw = artifact.read_text(encoding="utf-8")
        apply_sql, n_meta = _ddl._strip_psql_meta(raw)
        if n_meta:
            _log(f"应用前剥掉 {n_meta} 行 psql 元命令(\\restrict/\\unrestrict)")
        _log(f"应用产物 {artifact.name} 到 {PG_DB} 库(psql ON_ERROR_STOP=1)")
        _apply_artifact(container, url, apply_sql)
        a_lines = _normalize(_dump_schema_sql(container, url, PG_DB))
        _assert_has_tables(a_lines, "产物应用后")
        _log(f"dump A(产物应用后): {len(a_lines)} 行归一化 DDL")

        # ---- ③ 同库跑 build_engine → dump B ----
        engine = _run_build_engine(url, "产物库")
        with contextlib.suppress(Exception):
            engine.dispose()
        b_lines = _normalize(_dump_schema_sql(container, url, PG_DB))
        _assert_has_tables(b_lines, "产物库迁移后")
        _log(f"dump B(产物库+build_engine): {len(b_lines)} 行归一化 DDL")

        # ---- ④ 全新库只跑 build_engine = 代码真源基线 → dump F ----
        fresh_url = _create_database(url, fresh_db)
        f_engine = _run_build_engine(fresh_url, f"基线库 {fresh_db}")
        with contextlib.suppress(Exception):
            f_engine.dispose()
        f_lines = _normalize(_dump_schema_sql(container, fresh_url, fresh_db))
        _assert_has_tables(f_lines, "代码基线库")
        _log(f"dump F(代码基线): {len(f_lines)} 行归一化 DDL")
    except DriftDetected as exc:
        print(f"\nSCHEMA DRIFT: {exc}", flush=True)
        return 1
    except GateEnvError as exc:
        print(f"\nGATE ENV ERROR: {exc}", flush=True)
        return 2
    finally:
        if url:
            _drop_database(url, fresh_db)
        if owns_container:
            if args.keep_container:
                _log(f"--keep-container:保留容器 {container}(DATABASE_URL={url})")
            else:
                _remove_container(str(container))
                _log(f"已清理容器 {container}")

    # ---- ⑤ 双向比对 ----
    if _compare(a_lines, b_lines, f_lines):
        print(
            "\nSCHEMA DRIFT DETECTED——产物与 build_engine 代码不等价。"
            "改表后请重跑 scripts/dump_postgres_ddl.py 重新生成产物再提交。",
            flush=True,
        )
        return 1
    _log(f"零漂移:产物 ≡ 代码({len(a_lines)} 行 DDL 多重集相等,{time.time() - started:.1f}s)")
    print("SCHEMA DRIFT CHECK OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
