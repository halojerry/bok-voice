"""把 control-plane 的库结构从"代码跑出来"固化成可应用的 SQL(P0 Supabase 引导件)。

生成方式=真跑一遍 `control_plane.deps.build_engine()`(建表 + 幂等补列 + 数据迁移 +
pgvector 扩展/知识表),再用**源容器内**的 pg_dump 导 schema——因此产物与代码零漂移,
不手写 DDL。产出的 SQL 交给 Main 线程经 Supabase MCP 应用到团队真实 Supabase 项目。

用法(源库容器需已在跑,脚本不动它的生命周期):
    DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5434/postgres \
        python scripts/dump_postgres_ddl.py
    # 常用开关:
    #   --output scripts/.p0_supabase_schema.sql   产物路径(默认)
    #   --source-container pg-ddl                  源库容器名(默认 pg-ddl)
    #   --verify-image pgvector/pgvector:pg16      回环校验用的干净库镜像
    #   --keep                                     保留校验容器(默认跑完即删)
    #   --skip-verify                              只导出不校验(不推荐)

回环校验(默认开):另起一个干净容器 → 应用产物 → 再跑一次 build_engine() →
必须零 DDL 变更(dump 逐行一致),证明"产物 ≡ 代码"。

前置:
  * 源库镜像必须是带 pgvector 的 postgres:16(`pgvector/pgvector:pg16`)——
    否则 build_engine 的 `CREATE EXTENSION vector` 会失败并被吞,产物会**缺
    knowledge_chunks / 知识库整块**(日志可见 `[deps] vector schema skipped`)。
  * 需要 CP 依赖集(与 CI `pip install -e apps/control-plane` 相同):
    sqlalchemy / psycopg[binary] / pgvector / fastapi / livekit-api 等。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import socket
import subprocess
import sys
import time
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 与 scripts/mine_qa.py 同款:免 pip install 也能 import 仓库内包。
for _sub in ("apps/control-plane", "packages/core", "packages/business-db", "packages/knowledge", "packages/observability"):
    _p = str(ROOT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

DEFAULT_DB_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:5434/postgres"
DEFAULT_OUTPUT = ROOT / "scripts" / ".p0_supabase_schema.sql"
DEFAULT_SOURCE_CONTAINER = "pg-ddl"
DEFAULT_VERIFY_IMAGE = "pgvector/pgvector:pg16"
PG_USER = "postgres"
PG_DB = "postgres"

HEADER_TEMPLATE = """\
-- ============================================================================
-- Bok Voice control-plane 全量 schema —— P0 引导件(**生成物,勿手改**)
-- ============================================================================
-- 生成方式: scripts/dump_postgres_ddl.py
--   1) 对源库真跑 control_plane.deps.build_engine()(建表 + 幂等补列迁移 + 数据迁移);
--   2) 源容器内 pg_dump --schema-only 导出(见下方"源命令")。
--   schema 唯一真源 = packages/business-db ORM 模型 + deps.build_engine() 的幂等迁移;
--   **改表后必须重跑本脚本重新生成**,再应用到 Supabase。
--
-- 生成日期: {date}
-- 源镜像:   {source_image}
-- 源命令:   {dump_cmd}
-- 回环校验: {verify_image} 上应用本文件 + 重跑 build_engine() = 零 DDL 变更(生成时实测)
-- 规模:     CREATE TABLE {n_tables} 张 / CREATE INDEX {n_indexes} 条 / 数据语句 {n_data} 条
--           (--schema-only:正常应 0 条数据语句;带 DEFAULT/COMMENT 属 schema 本身)
--
-- 目标: 全新 Supabase(Postgres)项目首次引导。应用方式(Main 线程):
--   psql/SQL Editor 整文件执行(--no-owner --no-privileges,对象归执行者所有)。
--
-- 幂等性: 首次应用即可;重复应用会因 CREATE TABLE 已存在报错——后续变更请直接依赖
--   build_engine() 的启动幂等迁移,不要重复整文件执行。
--
-- pgvector 依赖(重要):
--   * knowledge_chunks.embedding 是 vector(384),故本文件含
--     `CREATE EXTENSION IF NOT EXISTS vector`。必须落在带 pgvector 的目标库上
--     (Supabase 内置,无需安装)。
--   * 若目标库把扩展装在别的 schema,可先手动
--     `create extension if not exists vector with schema extensions;`
--     再执行本文件(IF NOT EXISTS 会自动跳过重复创建)。
--
-- 数据面(本文件不含,由 CP 启动时幂等补齐):
--   * 垫话罐头 filler_entries 种子(三语×六场景)由 build_engine() 首启灌入;
--   * root 用户由 BOK_ROOT_USERNAME/BOK_ROOT_PASSWORD 置定时的 _seed_root_user() 建;
--   * 其余业务数据(账号/对象/话术/通话)按运营在 web 侧录入。
-- ============================================================================
"""

_DUMP_CMD = (
    "docker exec {container} pg_dump -U {user} --schema-only --no-owner "
    "--no-privileges {db}"
)


def _log(msg: str) -> None:
    print(f"[ddl] {msg}", flush=True)


def _run(cmd: list[str], *, stdin_file=None, capture: bool = True, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdin=stdin_file,
        capture_output=capture,
        text=True,
        timeout=timeout,
        check=True,
    )


def _docker_available() -> None:
    try:
        _run(["docker", "version", "--format", "{{.Server.Version}}"])
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SystemExit(f"[ddl] docker 不可用(本脚本依赖本机 docker CLI): {exc}")


def _container_exists(name: str) -> bool:
    proc = subprocess.run(
        ["docker", "inspect", "--type", "container", name],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def _source_image(container: str) -> str:
    try:
        return _run(["docker", "inspect", "--format", "{{.Config.Image}}", container]).stdout.strip()
    except subprocess.CalledProcessError:
        return "unknown"


def _wait_ready(container: str, timeout_s: int = 90) -> None:
    """轮询 pg_isready 直到可连(冷启动 postgres 首次 initdb 需几秒)。"""
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        proc = subprocess.run(
            ["docker", "exec", container, "pg_isready", "-U", PG_USER, "-q"],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return
        last = (proc.stdout + proc.stderr).strip()
        time.sleep(0.5)
    logs = subprocess.run(["docker", "logs", "--tail", "20", container], capture_output=True, text=True)
    raise SystemExit(f"[ddl] 容器 {container} 90s 未就绪: {last}\n{logs.stdout}{logs.stderr}")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _build_schema(url: str) -> tuple[list[str], object | None]:
    """真跑一次 build_engine();返回(被捕获的 stdout 行, engine)。

    DATABASE_URL 在调用前注入——build_engine 只认环境变量,不接参数。
    """
    os.environ["DATABASE_URL"] = url
    from control_plane.deps import build_engine  # noqa: PLC0415 - 延迟 import:先注入 env

    buf = StringIO()
    with redirect_stdout(buf):
        engine = build_engine()
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    for ln in lines:
        _log(f"  build_engine: {ln}")
    if engine is None:
        raise SystemExit("[ddl] build_engine() 返回 None(未识别到 DATABASE_URL)")
    return lines, engine


def _table_row_counts(engine: object) -> list[tuple[str, int]]:
    """源库现有行数速览(证明产物无数据行:行只存在于库里,不进 schema-only dump)。"""
    from sqlalchemy import inspect as sa_inspect, text  # noqa: PLC0415

    out: list[tuple[str, int]] = []
    insp = sa_inspect(engine)
    with engine.connect() as conn:  # type: ignore[attr-defined]
        for tbl in sorted(insp.get_table_names()):
            try:
                out.append((tbl, int(conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar() or 0)))
            except Exception:  # pragma: no cover - 单表查不动不影响主流程
                continue
    return out


def _pg_dump_sql(container: str) -> str:
    proc = _run(
        ["docker", "exec", container, "pg_dump", "-U", PG_USER, "--schema-only", "--no-owner", "--no-privileges", PG_DB]
    )
    return proc.stdout


def _count(pattern: str, sql: str) -> int:
    return len(re.findall(pattern, sql, flags=re.MULTILINE))


# pg_dump >= 16.10 / 17.6(CVE 修复)会在 dump 首尾插 `\restrict <随机 token>` /
# `\unrestrict <token>`——psql 专有元命令,且 token 每次随机。产物要经 Supabase MCP
# (或任何非 psql 客户端)整文件执行,留着会被当语法错误拒掉;psql 侧去掉也无害
# (它只是额外的收紧保护)。故生成时剥掉,校验比对时两边都剥。
_PSQL_META_RE = re.compile(r"^\s*\\(?:un)?restrict\b", re.IGNORECASE)


def _strip_psql_meta(sql: str) -> tuple[str, int]:
    """剥掉 \\restrict/\\unrestrict 元命令行;返回(新文本, 剥掉行数)。"""
    out: list[str] = []
    n = 0
    for ln in sql.splitlines():
        if _PSQL_META_RE.match(ln):
            n += 1
            continue
        out.append(ln)
    return "\n".join(out) + "\n", n


def _strip_comment_lines(sql: str) -> list[str]:
    return [
        ln.rstrip()
        for ln in sql.splitlines()
        if ln.strip() and not ln.lstrip().startswith("--")
    ]


def _diff_lines(a: list[str], b: list[str]) -> str:
    import difflib  # noqa: PLC0415

    diff = list(difflib.unified_diff(a, b, "artifact", "verify", lineterm="", n=1))
    return "\n".join(diff[:80]) if diff else ""


def _compose_artifact(
    body: str,
    *,
    date: str,
    source_image: str,
    source_container: str,
    verify_image: str,
    n_tables: int,
    n_indexes: int,
    n_data: int,
) -> str:
    header = HEADER_TEMPLATE.format(
        date=date,
        source_image=source_image,
        dump_cmd=_DUMP_CMD.format(container=source_container, user=PG_USER, db=PG_DB),
        verify_image=verify_image,
        n_tables=n_tables,
        n_indexes=n_indexes,
        n_data=n_data,
    )
    return header + body


def _verify_roundtrip(
    artifact_path: Path,
    *,
    verify_image: str,
    keep: bool,
    url_template: str,
) -> bool:
    """回环校验:干净库应用产物 → build_engine 必须零 DDL 变更 → dump 逐行一致。"""
    name = f"pg-verify-{os.getpid()}-{int(time.time())}"
    port = _free_port()
    verify_url = url_template.format(port=port)
    _log(f"回环校验:启动 {name} ({verify_image}) 端口 127.0.0.1:{port}")
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
    try:
        _run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:5432", "-e", "POSTGRES_PASSWORD=postgres", verify_image])
        _wait_ready(name)

        with artifact_path.open("r", encoding="utf-8") as fh:
            try:
                proc = _run(
                    ["docker", "exec", "-i", name, "psql", "-U", PG_USER, "-v", "ON_ERROR_STOP=1", PG_DB],
                    stdin_file=fh,
                )
            except subprocess.CalledProcessError as exc:
                _log("应用产物失败(psql 非零退出):")
                print((exc.stderr or exc.stdout or "")[-4000:], flush=True)
                return False
        applied = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        _log(f"应用产物: psql ON_ERROR_STOP=1 OK({len(applied)} 行输出,末行: {applied[-1] if applied else '-'})")

        _log("校验库跑 build_engine()(期望:建表全跳过、仅补数据种子)")
        _, v_engine = _build_schema(verify_url)
        try:
            v_engine.dispose()  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            pass
        # 比对两边都过元命令清洗:校验库的 dump 同样带随机 token 的 \restrict 行。
        verify_body_1, _ = _strip_psql_meta(_pg_dump_sql(name))

        artifact_lines = _strip_comment_lines(artifact_path.read_text(encoding="utf-8"))
        verify_lines = _strip_comment_lines(verify_body_1)
        if artifact_lines == verify_lines:
            _log(f"回环通过:应用后 dump 与产物逐行一致({len(verify_lines)} 行 DDL/DEFAULT/COMMENT)")
        else:
            _log("回环失败:应用后 dump 与产物有差异:")
            print(_diff_lines(artifact_lines, verify_lines), flush=True)
            return False

        _log("再跑一次 build_engine()(真 no-op 证明)")
        _, v_engine2 = _build_schema(verify_url)
        try:
            v_engine2.dispose()  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            pass
        verify_lines_2 = _strip_comment_lines(_strip_psql_meta(_pg_dump_sql(name))[0])
        if verify_lines_2 != verify_lines:
            _log("回环失败:第二次 build_engine() 后 schema 又变了(迁移不幂等)")
            print(_diff_lines(verify_lines, verify_lines_2), flush=True)
            return False
        _log("no-op 通过:第二次 build_engine() 后 schema 零变化")
        return True
    finally:
        if keep:
            _log(f"--keep:保留校验容器 {name}(DATABASE_URL={verify_url})")
        else:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
            _log(f"已删除校验容器 {name}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出 control-plane 全量 DDL(P0 Supabase 引导件)")
    ap.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL") or DEFAULT_DB_URL,
        help="源库 SQLAlchemy URL(默认 env DATABASE_URL,否则 " + DEFAULT_DB_URL + ")",
    )
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT), help="产物路径(默认 scripts/.p0_supabase_schema.sql)")
    ap.add_argument("--source-container", default=DEFAULT_SOURCE_CONTAINER, help="源库容器名(默认 pg-ddl)")
    ap.add_argument("--verify-image", default=DEFAULT_VERIFY_IMAGE, help=f"回环校验镜像(默认 {DEFAULT_VERIFY_IMAGE})")
    ap.add_argument("--verify-url-template", default="postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/postgres")
    ap.add_argument("--keep", action="store_true", help="保留回环校验容器(默认跑完即删)")
    ap.add_argument("--skip-verify", action="store_true", help="跳过回环校验(只导出)")
    args = ap.parse_args(argv)

    _docker_available()
    if not _container_exists(args.source_container):
        raise SystemExit(
            f"[ddl] 源库容器 {args.source_container} 不存在。先起一个(带 pgvector,否则会缺 knowledge_chunks):\n"
            f"  docker run -d --name {args.source_container} -p 127.0.0.1:5434:5432 "
            f"-e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg16"
        )
    if os.environ.get("BOK_ROOT_USERNAME") or os.environ.get("BOK_ROOT_PASSWORD"):
        _log("提示:检测到 BOK_ROOT_USERNAME/BOK_ROOT_PASSWORD——本脚本只跑 deps.build_engine,不跑 app startup,不会种 root 用户")

    date = _dt.date.today().isoformat()
    src_image = _source_image(args.source_container)
    _log(f"源库: {args.source_container} ({src_image}) url={args.database_url}")

    # ---- 1) 真跑 build_engine():建表 + 幂等补列 + 数据迁移 + pgvector ----
    _log("源库跑 build_engine()(建表 + 幂等迁移)")
    _, engine = _build_schema(args.database_url)
    counts = _table_row_counts(engine)
    nonempty = [(t, n) for t, n in counts if n]
    _log(f"源库表数: {len(counts)};有数据的表: {nonempty or '无'}")
    try:
        engine.dispose()  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        pass

    # ---- 2) 容器内 pg_dump 导 schema(版本与源库严格一致)----
    body, n_meta = _strip_psql_meta(_pg_dump_sql(args.source_container))
    if n_meta:
        _log(f"已剥掉 {n_meta} 行 psql 元命令(\\restrict/\\unrestrict,见 _PSQL_META_RE 注释)")
    n_tables = _count(r"^CREATE TABLE ", body)
    n_indexes = _count(r"^CREATE INDEX ", body)
    n_data = _count(r"^(INSERT INTO|COPY )", body)
    if n_tables == 0:
        raise SystemExit("[ddl] pg_dump 未产出 CREATE TABLE——检查源库是否为空或 url/容器不匹配")
    if n_data:
        _log(f"注意:dump 里出现 {n_data} 条数据语句(预期 0;--schema-only 只应含 schema)")

    artifact = _compose_artifact(
        body, date=date, source_image=src_image, source_container=args.source_container,
        verify_image=("(未校验)" if args.skip_verify else args.verify_image),
        n_tables=n_tables, n_indexes=n_indexes, n_data=n_data,
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(artifact, encoding="utf-8")
    _log(f"已写出 {out}(CREATE TABLE {n_tables} / CREATE INDEX {n_indexes} / 数据语句 {n_data})")

    # ---- 3) 回环校验:干净库应用产物 → build_engine 必须零 DDL 变更 ----
    if args.skip_verify:
        _log("--skip-verify:跳过回环校验")
        return 0

    ok = _verify_roundtrip(
        out,
        verify_image=args.verify_image,
        keep=args.keep,
        url_template=args.verify_url_template,
    )
    if not ok:
        _log("回环校验失败——产物不可用,先修代码再重跑")
        return 2
    _log("完成:产物已生成并通过回环校验")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
