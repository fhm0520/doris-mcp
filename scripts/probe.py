"""对着真实 Doris 跑一遍关键语法，打印能力报告。

每条检查独立建连：1.2 上某些语句（如 SHOW PROC '/frontends'）可能把连接拖死，
复用同一个连接会导致后面全部假失败（表现为 code=0 空消息）。

用法：uv run python scripts/probe.py
"""

from __future__ import annotations

import contextlib
import os
import time

import pymysql

from doris_mcp.config import Settings

CHECKS: list[tuple[str, str]] = [
    ("version", "SELECT @@version_comment"),
    ("databases", "SHOW DATABASES"),
    ("show_proc_backends", "SHOW PROC '/backends'"),
    ("show_proc_frontends", "SHOW PROC '/frontends'"),
    ("info_schema_backends", "SELECT * FROM information_schema.backends LIMIT 1"),
    ("info_schema_rowsets", "SELECT * FROM information_schema.rowsets LIMIT 1"),
    ("info_schema_tables", "SELECT TABLE_NAME FROM information_schema.tables LIMIT 1"),
    ("info_schema_partitions", "SELECT PARTITION_NAME FROM information_schema.partitions LIMIT 1"),
    ("columns_column_comment", "SELECT COLUMN_COMMENT FROM information_schema.columns LIMIT 1"),
    ("active_queries", "SELECT * FROM information_schema.active_queries LIMIT 1"),
    ("show_proc_current_queries", "SHOW PROC '/current_queries'"),
    ("show_proc_slow_queries", "SHOW PROC '/slow_queries'"),
    ("show_proc_statistic", "SHOW PROC '/statistic'"),
    ("show_backends_2x", "SHOW BACKENDS"),
    ("show_frontends_2x", "SHOW FRONTENDS"),
    ("show_catalogs", "SHOW CATALOGS"),
    ("show_table_status", "SHOW TABLE STATUS FROM information_schema"),
    ("show_data", "SHOW DATA FROM information_schema"),
    ("show_workload_groups", "SHOW WORKLOAD GROUPS"),
    ("audit_internal_schema", "SELECT query_id FROM __internal_schema.audit_log LIMIT 1"),
    ("audit_doris_audit_db", "SELECT query_id FROM doris_audit_db__.doris_audit_tbl__ LIMIT 1"),
    ("mv_infos", 'SELECT * FROM mv_infos("database"="information_schema") LIMIT 1'),
    ("jobs_mv", 'SELECT * FROM jobs("type"="mv") LIMIT 1'),
    ("explain_graph", "EXPLAIN GRAPH SELECT 1"),
    ("explain_select", "EXPLAIN SELECT 1"),
    ("show_query_profile", 'SHOW QUERY PROFILE "/none"'),
    # 补官方工具时要用的语句（P1/P2 候选）
    ("show_load", "SHOW LOAD"),
    ("show_routine_load", "SHOW ROUTINE LOAD"),
    ("show_global_functions", "SHOW GLOBAL FUNCTIONS"),
    ("show_proc_dbs", "SHOW PROC '/dbs'"),
]

# 需要具体表才能测的语句：设置 PROBE_DB / PROBE_TABLE 后追加
TABLE_CHECKS: list[tuple[str, str]] = [
    ("show_create_table", "SHOW CREATE TABLE {tbl}"),
    ("show_partitions", "SHOW PARTITIONS FROM {tbl}"),
    ("show_index", "SHOW INDEX FROM {tbl}"),
    (
        "table_partitions_via_is",
        (
            "SELECT PARTITION_NAME, VISIBLE_VERSION_TIME, DATA_LENGTH "
            "FROM information_schema.partitions "
            "WHERE TABLE_SCHEMA='{db}' AND TABLE_NAME='{table}'"
        ),
    ),
]


def _once(s: Settings, sql: str) -> tuple[bool, str]:
    try:
        conn = pymysql.connect(
            host=s.doris_host,
            port=s.doris_port,
            user=s.doris_user,
            password=s.doris_password,
            database=s.doris_database or None,
            charset="utf8mb4",
            connect_timeout=s.doris_connect_timeout,
            read_timeout=25,
            autocommit=True,
        )
    except Exception as exc:
        msg = str(exc)
        # 实例抖动会被误判成"语法不支持"，这类错误必须标记为可重试
        transient = any(token in msg for token in ("2013", "10054", "2006"))
        return False, f"connect failed: {exc}", transient

    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(1)
        sample = str(rows[0])[:70] if rows else "<no rows>"
        return True, f"cols={len(cols)} {sample}", False
    except Exception as exc:
        code = exc.args[0] if getattr(exc, "args", None) else "?"
        msg = str(exc.args[1])[:70] if len(getattr(exc, "args", ())) > 1 else str(exc)[:70]
        transient = code in (0, 2006, 2013)
        return False, f"code={code} {msg}", transient
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def run_check(s: Settings, sql: str, attempts: int = 3) -> tuple[bool, str]:
    """实例有 10~23% 概率掐断连接，只对传输层错误重试，语法错误立即返回。"""
    detail = ""
    for i in range(attempts):
        ok, detail, transient = _once(s, sql)
        if ok or not transient:
            return ok, detail
        if i + 1 < attempts:
            time.sleep(0.3 * (i + 1))
    return False, f"{detail} (retried {attempts - 1}x)"


def main() -> int:
    s = Settings()
    print(f"target: {s.endpoint}\n")

    checks = list(CHECKS)
    db, table = os.getenv("PROBE_DB", ""), os.getenv("PROBE_TABLE", "")
    if db and table:
        fmt = {"db": db, "table": table, "tbl": f"`{db}`.`{table}`"}
        checks += [(name, sql.format(**fmt)) for name, sql in TABLE_CHECKS]
    else:
        print("（未设置 PROBE_DB / PROBE_TABLE，跳过表级检查）\n")

    print(f"{'check':30} {'result':12} detail")
    print("-" * 110)
    for name, sql in checks:
        ok, detail = run_check(s, sql)
        print(f"{name:30} {'OK' if ok else 'UNSUPPORTED':12} {detail}")
    print("\n提示：把整段输出贴回来即可收敛方言表。密码不会被打印。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
