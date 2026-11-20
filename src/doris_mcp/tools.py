"""全部 MCP 工具定义（纯只读）。

约定：
- 工具里绝不抛异常（mcp 2.x 会把它包成 UnexpectedToolError，真实原因会丢），
  所有失败都返回 {"ok": false, "error": {...}} 信封。
- 任何拼接进来的标识符走 `quote_ident`，字面量走 `quote_str`。
- 语句形态均来自 doris-1.2.4 实测，见 capabilities.py 与 docs/design.md。
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .capabilities import capabilities_for, live_checks, profile_for
from .config import Settings
from .db import DorisPool, dumps, error_envelope
from .sql import SqlSafetyError, check_read_only, quote_ident, quote_str


def _ro(title: str) -> ToolAnnotations:
    """所有工具共用的注解：只读、非破坏、幂等、访问外部数据库。

    open_world_hint=True 表示会与外部实体（Doris 实例）交互；它只是提示，不是安全边界，
    真正的只读保证由 sql.check_read_only 强制。
    """
    return ToolAnnotations(
        title=title,
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )

# 元数据查询可能远超用户可见行数上限（分区、宽表），用大 cap 且不 clamp
META_ROWS = 5000
# 诊断类内部查询用短超时：本实例的"超时"多是连接假死，短超时 + 重试比等 60s 划算
DIAG_TIMEOUT = 15.0
# SHOW FRONTENDS 正常情况下 <1s；个别实例上会拖到十几秒，给个上限后降级
FE_TIMEOUT = 10.0

STALE_FRESHNESS_HOURS = 24
COMPACTION_WARN_ROWSETS = 10


# ---------------------------------------------------------------- 行解析
def _index(columns: list[dict[str, Any]]) -> dict[str, int]:
    return {c["name"].lower(): i for i, c in enumerate(columns)}


def _cell(row: list[Any], idx: dict[str, int], key: str) -> Any:
    i = idx.get(key.lower())
    return row[i] if i is not None and i < len(row) else None


def _dicts(result: dict[str, Any]) -> list[dict[str, Any]]:
    keys = [c["name"] for c in result["columns"]]
    return [{k: row[i] if i < len(row) else None for i, k in enumerate(keys)} for row in result["rows"]]


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if value is None:
        return None
    return str(value).strip().lower() in {"true", "yes", "1"}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _human_bytes(num: float | None) -> str | None:
    if num is None:
        return None
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return None


def _unix_to_text(value: Any) -> str | None:
    """rowsets.CREATION_TIME 是 unix 秒，其它表可能是 datetime。"""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    number = _int(value)
    if number is None or number <= 0:
        return str(value)
    try:
        return dt.datetime.fromtimestamp(number).isoformat(sep=" ")
    except (OSError, OverflowError, ValueError):
        return str(value)


def _parse_datetime(value: Any) -> dt.datetime | None:
    """SHOW 语句的时间列可能是 datetime，也可能是 str 或 unix 秒。"""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    text = _text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    number = _int(text)
    if number is not None and number > 0:
        try:
            return dt.datetime.fromtimestamp(number)
        except (OSError, OverflowError, ValueError):
            return None
    return None


def _strip_cluster(name: str) -> str:
    """SHOW PROC '/dbs' 的 DbName 形如 default_cluster:sg_ods。"""
    return name.split(":", 1)[1] if ":" in name else name


def _state_summary(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    summary: dict[str, int] = {}
    for row in rows:
        state = _text(row.get(key)) or "UNKNOWN"
        summary[state] = summary.get(state, 0) + 1
    return summary


def _page(
    items: list[Any],
    limit: int,
    offset: int,
    total: int | None = None,
    *,
    already_sliced: bool = False,
) -> dict[str, Any]:
    """统一的列表分页信封：total / count / offset / limit / items / has_more / next_offset。

    already_sliced=True 用于"服务端已按 LIMIT/OFFSET 取回"的场景，避免重复切片。
    """
    total = len(items) if total is None else total
    window = items if already_sliced else items[offset: offset + limit]
    next_offset = offset + len(window)
    has_more = next_offset < total
    return {
        "total": total,
        "count": len(window),
        "offset": offset,
        "limit": limit,
        "items": window,
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
    }


# ---------------------------------------------------------------- 注册
def register(mcp: MCPServer, pool: DorisPool, settings: Settings) -> None:
    async def _run(sql: str, *, database: str | None = None, max_rows: int | None = None,
                   clamp: bool = True) -> str:
        return dumps(await pool.execute(sql, database=database, max_rows=max_rows, clamp=clamp))

    def _fail(sql: str, message: str, *, hint: str | None = None,
              type_: str = "invalid_argument") -> str:
        return dumps(error_envelope(sql, type_=type_, message=message, hint=hint,
                                    target=settings.endpoint))

    def _meta(sql: str, *, database: str | None = None) -> Any:
        """元数据/诊断查询：大 cap + 短超时（超时可重试）。"""
        return pool.execute(sql, database=database, max_rows=META_ROWS, clamp=False,
                            timeout=DIAG_TIMEOUT)

    # ============================================================ 元数据
    @mcp.tool(name="doris_ping", annotations=_ro("连通性检查"))
    async def doris_ping() -> str:
        """检查连通性并返回 Doris 版本、默认库、服务器时间与结果限制。连通性排查的第一步。"""
        payload = await pool.execute(
            "SELECT @@version_comment AS version, DATABASE() AS current_database, "
            "NOW() AS server_time, USER() AS connected_user",
            max_rows=1,
        )
        if not payload.get("ok"):
            return dumps(payload)
        version = await pool.server_version_text()
        payload["doris_version"] = version
        payload["capability_profile"] = profile_for(version)
        payload["limits"] = {
            "max_rows": settings.doris_max_rows,
            "query_timeout_s": settings.doris_query_timeout,
            "read_only": True,
            "retries": settings.doris_retries,
        }
        return dumps(payload)

    @mcp.tool(name="doris_list_databases", annotations=_ro("列出数据库"))
    async def doris_list_databases(
        catalog: Annotated[str | None, Field(
            description="可选，catalog 名；默认当前 catalog（internal）")] = None,
        limit: Annotated[int, Field(description="本页返回条数", ge=1, le=500)] = 50,
        offset: Annotated[int, Field(description="跳过条数，用于翻页", ge=0)] = 0,
    ) -> str:
        """列出数据库，分页返回。

        internal：走 information_schema.schemata（1.2 的 SHOW DATABASES 不支持 LIMIT，
        这里在服务端分页）；指定其他 catalog 时用 SHOW DATABASES FROM，取回后本地切页。

        返回：{ok, total, count, offset, limit, items[{name}], has_more, next_offset, source}
        """
        target = (catalog or "internal").strip()
        try:
            if target.lower() != "internal":
                sql = f"SHOW DATABASES FROM {quote_ident(target)}"
                result = await pool.execute(sql, max_rows=META_ROWS, clamp=False)
                if not result.get("ok"):
                    return dumps(result)
                names = [_text(row[0]) for row in result["rows"] if row]
                payload = {"ok": True, "catalog": target, "source": sql,
                           **_page([{"name": n} for n in names], limit, offset)}
                return dumps(payload)

            counted = await _meta("SELECT COUNT(*) AS n FROM information_schema.schemata")
            if not counted.get("ok"):
                return dumps(counted)
            total = _int(_dicts(counted)[0].get("n")) or 0
            page = await _meta(
                "SELECT SCHEMA_NAME FROM information_schema.schemata "
                f"ORDER BY SCHEMA_NAME LIMIT {int(limit)} OFFSET {int(offset)}"
            )
            if not page.get("ok"):
                return dumps(page)
            names = [_text(row[0]) for row in page["rows"] if row]
            return dumps(
                {"ok": True, "catalog": "internal", "source": "information_schema.schemata",
                 **_page([{"name": n} for n in names], limit, offset, total, already_sliced=True)}
            )
        except SqlSafetyError as exc:
            return _fail("SHOW DATABASES", str(exc))

    @mcp.tool(name="doris_list_catalogs", annotations=_ro("列出 Catalog"))
    async def doris_list_catalogs() -> str:
        """列出 catalog（SHOW CATALOGS，1.2 可用，4 列）。"""
        return await _run("SHOW CATALOGS")

    @mcp.tool(name="doris_list_tables", annotations=_ro("列出表"))
    async def doris_list_tables(
        database: Annotated[str, Field(description="数据库名")],
        name_like: Annotated[str | None, Field(description="可选，表名 LIKE 模式，如 ods_%")] = None,
        catalog: Annotated[str | None, Field(description="可选，catalog 名；默认 internal")] = None,
        limit: Annotated[int, Field(description="本页返回条数", ge=1, le=500)] = 50,
        offset: Annotated[int, Field(description="跳过条数，用于翻页", ge=0)] = 0,
    ) -> str:
        """列出某个库下的表，分页返回，并区分表与视图。

        internal：走 information_schema.tables（1.2 的 SHOW TABLES 不支持 LIMIT），
        WHERE TABLE_SCHEMA + 可选 LIKE 在服务端过滤；其他 catalog 用 SHOW TABLES FROM，
        取回后本地切页。

        返回：{ok, total, count, offset, limit, items[{name, type}], has_more, next_offset, source}
        """
        target = (catalog or "internal").strip()
        try:
            if target.lower() != "internal":
                sql = f"SHOW TABLES FROM {quote_ident(target)}.{quote_ident(database)}"
                if name_like:
                    sql += f" LIKE {quote_str(name_like)}"
                result = await pool.execute(sql, max_rows=META_ROWS, clamp=False)
                if not result.get("ok"):
                    return dumps(result)
                names = [_text(row[0]) for row in result["rows"] if row]
                return dumps(
                    {"ok": True, "catalog": target, "database": database, "source": sql,
                     **_page([{"name": n, "type": None} for n in names], limit, offset)}
                )

            where = f"TABLE_SCHEMA = {quote_str(database)}"
            if name_like:
                where += f" AND TABLE_NAME LIKE {quote_str(name_like)}"
            counted = await _meta(
                f"SELECT COUNT(*) AS n FROM information_schema.tables WHERE {where}"
            )
            if not counted.get("ok"):
                return dumps(counted)
            total = _int(_dicts(counted)[0].get("n")) or 0
            page = await _meta(
                "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.tables "
                f"WHERE {where} ORDER BY TABLE_NAME LIMIT {int(limit)} OFFSET {int(offset)}"
            )
            if not page.get("ok"):
                return dumps(page)
            items = [
                {"name": _text(row[0]), "type": _text(row[1]) or None}
                for row in page["rows"] if row
            ]
            return dumps(
                {"ok": True, "catalog": "internal", "database": database,
                 "source": "information_schema.tables",
                 **_page(items, limit, offset, total, already_sliced=True)}
            )
        except SqlSafetyError as exc:
            return _fail("SHOW TABLES", str(exc))

    @mcp.tool(name="doris_describe_table", annotations=_ro("查看表结构"))
    async def doris_describe_table(
        database: Annotated[str, Field(description="数据库名")],
        table: Annotated[str, Field(description="表名")],
        include_indexes: Annotated[bool, Field(description="是否返回索引信息")] = False,
        include_basic: Annotated[bool, Field(description="是否返回引擎/行数/创建时间/表注释")] = False,
        include_ddl: Annotated[bool, Field(description="是否返回 SHOW CREATE TABLE 的 DDL")] = False,
    ) -> str:
        """查看表结构：字段名、类型、是否可空、是否 key 列、默认值、Extra（聚合类型等）、注释。
        注释取自 SHOW FULL COLUMNS —— 1.2 上最完整的列元数据来源。
        include_basic 用 SHOW TABLE STATUS 补引擎/行数/创建时间（官方 get_table_context 的 basic 段）。
        """
        try:
            show_sql = f"SHOW FULL COLUMNS FROM {quote_ident(database)}.{quote_ident(table)}"
        except SqlSafetyError as exc:
            return _fail("SHOW FULL COLUMNS", str(exc))

        result = await _meta(show_sql)
        if not result.get("ok"):
            return dumps(result)

        idx = _index(result["columns"])
        columns = []
        for row in result["rows"]:
            columns.append(
                {
                    "name": _cell(row, idx, "Field"),
                    "type": _cell(row, idx, "Type"),
                    "nullable": _bool(_cell(row, idx, "Null")),
                    "key": _bool(_cell(row, idx, "Key")),
                    "default": _default_value(_cell(row, idx, "Default")),
                    "extra": _cell(row, idx, "Extra") or None,
                    "comment": _cell(row, idx, "Comment") or "",
                }
            )

        payload: dict[str, Any] = {
            "ok": True,
            "database": database,
            "table": table,
            "column_count": len(columns),
            "columns": columns,
            "elapsed_ms": result["elapsed_ms"],
        }
        if include_basic:
            payload["basic"] = await _table_basic(pool, database, table)
        if include_ddl:
            payload["ddl"] = await _table_ddl(pool, database, table)
        if include_indexes:
            payload["indexes"] = await _fetch_indexes(pool, database, table)
        return dumps(payload)

    @mcp.tool(name="doris_get_table_size", annotations=_ro("表与库的数据量"))
    async def doris_get_table_size(
        database: Annotated[str, Field(description="数据库名")],
        table: Annotated[str | None, Field(
            description="可选，表名；不传则返回该库所有表的大小")] = None,
    ) -> str:
        """看表/库的数据量。不传 table：SHOW DATA（必须先选库，返回每表 Size + ReplicaCount）。
        传 table：SHOW PARTITIONS 汇总 DataSize，给出总字节数与分区数。
        """
        if not table:
            result = await pool.execute("SHOW DATA", database=database, max_rows=META_ROWS, clamp=False)
            if not result.get("ok"):
                return dumps(result)
            tables: list[dict[str, Any]] = []
            totals: dict[str, Any] = {}
            for row in _dicts(result):
                name = _text(row.get("TableName"))
                # SHOW DATA 末尾是汇总行：Total / Quota / Left，不是表
                if name in {"Total", "Quota", "Left"}:
                    totals[name.lower()] = _text(row.get("Size"))
                    continue
                tables.append(
                    {
                        "table": name,
                        "size_text": _text(row.get("Size")),
                        "size_mb": _float(row.get("Size")),
                        "replica_count": _int(row.get("ReplicaCount")),
                    }
                )
            return dumps(
                {"ok": True, "database": database, "table_count": len(tables), "tables": tables,
                 "total_size_text": totals.get("total"), "quota_text": totals.get("quota"),
                 "left_text": totals.get("left"),
                 "source": "SHOW DATA（Size 按 Doris 约定单位为 MB，保留原始字符串）"}
            )

        try:
            sql = f"SHOW PARTITIONS FROM {quote_ident(database)}.{quote_ident(table)}"
        except SqlSafetyError as exc:
            return _fail("SHOW PARTITIONS", str(exc))
        result = await _meta(sql)
        if not result.get("ok"):
            return dumps(result)

        rows = _dicts(result)
        if not rows:
            return dumps({"ok": True, "database": database, "table": table,
                          "partition_count": 0, "note": "该表没有分区（或表不存在）"})
        sizes = [_float(r.get("DataSize")) or 0.0 for r in rows]
        total_mb = round(sum(sizes), 3)
        return dumps(
            {
                "ok": True,
                "database": database,
                "table": table,
                "partition_count": len(rows),
                "total_data_size_mb": total_mb,
                "total_data_size": _human_bytes(total_mb * 1024 * 1024),
                "partitions": [
                    {"partition": _text(r.get("PartitionName")),
                     "state": _text(r.get("State")),
                     "data_size_mb": _float(r.get("DataSize"))}
                    for r in rows
                ],
                "unit_note": "DataSize 按 Doris 约定单位为 MB",
            }
        )

    @mcp.tool(name="doris_get_table_partitions", annotations=_ro("分区与数据新鲜度"))
    async def doris_get_table_partitions(
        database: Annotated[str, Field(description="数据库名")],
        table: Annotated[str, Field(description="表名")],
    ) -> str:
        """看分区清单与数据新鲜度（SHOW PARTITIONS，17 列）。
        VisibleVersionTime 是分区最新可见版本时间，用它算滞后（官方 monitor_data_freshness）。
        """
        try:
            sql = (
                f"SHOW PARTITIONS FROM {quote_ident(database)}.{quote_ident(table)}"
            )
        except SqlSafetyError as exc:
            return _fail("SHOW PARTITIONS", str(exc))
        result = await _meta(sql)
        if not result.get("ok"):
            return dumps(result)

        rows = _dicts(result)
        partitions = []
        latest: dt.datetime | None = None
        for row in rows:
            visible_at = _parse_datetime(row.get("VisibleVersionTime"))
            if visible_at is not None and (latest is None or visible_at > latest):
                latest = visible_at
            partitions.append(
                {
                    "partition": _text(row.get("PartitionName")),
                    "state": _text(row.get("State")),
                    "visible_version": _int(row.get("VisibleVersion")),
                    "visible_version_time": visible_at.isoformat(sep=" ") if visible_at else None,
                    "partition_key": _text(row.get("PartitionKey")),
                    "buckets": _int(row.get("Buckets")),
                    "replication_num": _int(row.get("ReplicationNum")),
                    "data_size_mb": _float(row.get("DataSize")),
                }
            )

        lag_hours = None
        if latest is not None:
            lag_hours = round((dt.datetime.now() - latest).total_seconds() / 3600, 2)

        payload: dict[str, Any] = {
            "ok": True,
            "database": database,
            "table": table,
            "partition_count": len(partitions),
            "partitions": partitions,
            "latest_visible_version_time": latest.isoformat(sep=" ") if latest else None,
            "freshness_lag_hours": lag_hours,
        }
        if lag_hours is not None and lag_hours > STALE_FRESHNESS_HOURS:
            payload["warnings"] = [
                f"最新可见版本已滞后 {lag_hours} 小时（阈值 {STALE_FRESHNESS_HOURS}h），确认导入是否正常"
            ]
        if not rows:
            payload["note"] = "该表没有分区（或表不存在）"
        return dumps(payload)

    @mcp.tool(name="doris_inspect_external_catalog", annotations=_ro("浏览 Catalog"))
    async def doris_inspect_external_catalog(
        catalog: Annotated[str, Field(description="catalog 名，如 internal 或 hive")],
        database: Annotated[str | None, Field(
            description="可选，库名；不传则列出该 catalog 下的库")] = None,
    ) -> str:
        """浏览 catalog：只传 catalog 时列库（SHOW DATABASES FROM c）；
        带 database 时列表（SHOW TABLES FROM c.db）。
        """
        try:
            if database:
                sql = f"SHOW TABLES FROM {quote_ident(catalog)}.{quote_ident(database)}"
            else:
                sql = f"SHOW DATABASES FROM {quote_ident(catalog)}"
        except SqlSafetyError as exc:
            return _fail("SHOW", str(exc))
        result = await pool.execute(sql, max_rows=settings.doris_max_rows)
        if not result.get("ok"):
            return dumps(result)
        payload = dict(result)
        payload["catalog"] = catalog
        if catalog.lower() == "internal":
            payload["note"] = "internal 就是当前实例本身，等价于 doris_list_databases / doris_list_tables"
        return dumps(payload)

    # ============================================================ 查询
    @mcp.tool(name="doris_execute_query", annotations=_ro("执行只读 SQL"))
    async def doris_execute_query(
        sql: Annotated[str, Field(description="只读 SQL：SELECT / SHOW / DESC / EXPLAIN / WITH")],
        database: Annotated[str | None, Field(description="可选，执行前切换到的库")] = None,
        max_rows: Annotated[int | None, Field(description="可选，本次返回的最大行数")] = None,
    ) -> str:
        """执行只读 SQL 并返回列与行。非 SELECT/SHOW/DESC/EXPLAIN/WITH 一律拒绝，
        本服务没有任何写通道。结果超出 max_rows 时 truncated=true。
        """
        reason = check_read_only(sql)
        if reason:
            return _fail(sql, reason)

        if database:
            try:
                quote_ident(database)
            except SqlSafetyError as exc:
                return _fail(sql, str(exc))

        return await _run(sql, database=database, max_rows=max_rows)

    @mcp.tool(name="doris_explain_query", annotations=_ro("查看执行计划"))
    async def doris_explain_query(
        sql: Annotated[str, Field(description="要分析的只读 SQL（SELECT/WITH）")],
        format: Annotated[str, Field(description="plan（默认）/ verbose / graph")] = "plan",
        database: Annotated[str | None, Field(description="可选，切换到的库")] = None,
    ) -> str:
        """看执行计划：EXPLAIN / EXPLAIN VERBOSE / EXPLAIN GRAPH。
        graph 输出 ASCII 执行图，排查 join/scan 分布最直观（1.2 实测可用）。
        """
        mode = (format or "plan").strip().lower()
        prefix = {"plan": "EXPLAIN", "verbose": "EXPLAIN VERBOSE", "graph": "EXPLAIN GRAPH"}.get(mode)
        if prefix is None:
            return _fail("EXPLAIN", f"format 只支持 plan / verbose / graph，收到 `{format}`")

        body = sql.strip().rstrip(";")
        if body.upper().startswith("EXPLAIN"):
            return _fail(sql, "不要再嵌一层 EXPLAIN，把原始 SQL 传进来即可")
        reason = check_read_only(body)
        if reason:
            return _fail(sql, f"EXPLAIN 只接受只读 SQL：{reason}")

        result = await pool.execute(f"{prefix} {body}", database=database, max_rows=META_ROWS, clamp=False)
        if not result.get("ok"):
            return dumps(result)

        lines = [_text(row[0]) for row in result["rows"] if row]
        return dumps(
            {
                "ok": True,
                "format": mode,
                "sql": body,
                "line_count": len(lines),
                "plan": "\n".join(lines),
                "elapsed_ms": result["elapsed_ms"],
            }
        )

    @mcp.tool(name="doris_get_query_profile", annotations=_ro("查询 Profile"))
    async def doris_get_query_profile(
        query_id: Annotated[str, Field(description="query id，可从当前查询或日志里拿")],
    ) -> str:
        """取某个查询的 profile（SHOW QUERY PROFILE "/id"）。只对执行过的 query id 有效，
        取不到时 Doris 返回 1105 not found。
        """
        candidate = query_id.strip().strip('"').strip("'")
        if not candidate or not all(c.isalnum() or c in "-_:/" for c in candidate):
            return _fail("SHOW QUERY PROFILE", f"query_id 格式不合法：{query_id!r}")
        # 1.2 要求 path 以 / 开头：SHOW QUERY PROFILE "/<id>"，否则报 Query path must starts with '/'
        path = candidate if candidate.startswith("/") else f"/{candidate}"
        sql = f'SHOW QUERY PROFILE "{path}"'
        result = await pool.execute(sql, max_rows=META_ROWS, clamp=False, timeout=DIAG_TIMEOUT)
        if not result.get("ok"):
            err = result.get("error", {})
            if "not found" in _text(err.get("message")).lower():
                err["hint"] = "该 query id 不存在或 profile 已过期；先用 doris_list_active_tasks 或日志拿到真实 id"
                result["error"] = err
            return dumps(result)
        lines = [_text(row[0]) for row in result["rows"] if row]
        return dumps(
            {"ok": True, "query_id": candidate, "line_count": len(lines),
             "profile": "\n".join(lines), "elapsed_ms": result["elapsed_ms"]}
        )

    # ============================================================ 集群
    @mcp.tool(name="doris_get_cluster_overview", annotations=_ro("集群概览"))
    async def doris_get_cluster_overview(
        include_nodes: Annotated[bool, Field(description="是否包含 FE / BE 节点明细")] = True,
    ) -> str:
        """集群概览：FE 节点（SHOW FRONTENDS）、BE 节点（information_schema.backends）、
        库表统计（SHOW PROC '/statistic'）、库容量（SHOW PROC '/dbs'）。
        FE 查询给短超时，取不到就降级为只给 BE，不阻塞整体返回。
        """
        backend_sql = (
            "SELECT BackendId, IP, Alive, TabletNum, DataUsedCapacity, AvailCapacity, "
            "TotalCapacity, UsedPct, Version, LastHeartbeat FROM information_schema.backends"
        )
        backends = await _meta(backend_sql)
        frontends = await pool.execute(
            "SHOW FRONTENDS", max_rows=META_ROWS, clamp=False, timeout=FE_TIMEOUT
        )
        statistic = await _meta("SHOW PROC '/statistic'")
        dbs = await _meta("SHOW PROC '/dbs'")
        for payload in (backends, statistic, dbs):
            if not payload.get("ok"):
                return dumps(payload)

        fe_nodes: list[dict[str, Any]] = []
        fe_note = None
        if frontends.get("ok"):
            for row in _dicts(frontends):
                fe_nodes.append(
                    {
                        "name": _text(row.get("Name")),
                        "ip": _text(row.get("IP")),
                        "role": _text(row.get("Role")),
                        "is_master": _bool(row.get("IsMaster")),
                        "alive": _bool(row.get("Alive")),
                        "join": _bool(row.get("Join")),
                        "query_port": _int(row.get("QueryPort")),
                        "http_port": _int(row.get("HttpPort")),
                        "edit_log_port": _int(row.get("EditLogPort")),
                        "replayed_journal_id": _int(row.get("ReplayedJournalId")),
                        "last_heartbeat": _text(row.get("LastHeartbeat")),
                        "version": _text(row.get("Version")) or None,
                    }
                )
        else:
            err = frontends.get("error", {})
            fe_note = (
                "FE 清单获取失败（SHOW FRONTENDS 超时或报错），仅返回 BE："
                f"{err.get('code')} {str(err.get('message'))[:80]}"
            )

        nodes = []
        total_capacity = total_used = 0
        alive = 0
        used_pcts: list[float] = []
        for row in _dicts(backends):
            is_alive = _bool(row.get("Alive"))
            alive += 1 if is_alive else 0
            used = _int(row.get("DataUsedCapacity")) or 0
            capacity = _int(row.get("TotalCapacity")) or 0
            pct = _float(row.get("UsedPct"))
            total_used += used
            total_capacity += capacity
            if pct is not None:
                used_pcts.append(pct)
            nodes.append(
                {
                    "backend_id": _int(row.get("BackendId")),
                    "ip": _text(row.get("IP")),
                    "alive": is_alive,
                    "tablet_num": _int(row.get("TabletNum")),
                    "data_used": _human_bytes(used),
                    "total_capacity": _human_bytes(capacity),
                    "disk_used_pct": pct,
                    "version": _text(row.get("Version")),
                    "last_heartbeat": _text(row.get("LastHeartbeat")),
                }
            )

        db_stats = []
        for row in _dicts(statistic):
            name = _text(row.get("DbName"))
            # 末尾汇总行的 DbId 是 'Total'、DbName 位置被挤成数字，要跳过
            if _text(row.get("DbId")).lower() == "total" or ":" not in name:
                continue
            db_stats.append(
                {
                    "database": _strip_cluster(name),
                    "table_num": _int(row.get("TableNum")),
                    "partition_num": _int(row.get("PartitionNum")),
                    "tablet_num": _int(row.get("TabletNum")),
                    "replica_num": _int(row.get("ReplicaNum")),
                }
            )

        db_sizes = []
        for row in _dicts(dbs):
            db_sizes.append(
                {
                    "database": _strip_cluster(_text(row.get("DbName"))),
                    "size_text": _text(row.get("Size")),
                    "quota_text": _text(row.get("Quota")),
                    "replica_count": _int(row.get("ReplicaCount")),
                }
            )

        payload: dict[str, Any] = {
            "ok": True,
            "summary": {
                "backend_count": len(nodes),
                "backend_alive": alive,
                "frontend_count": len(fe_nodes),
                "frontend_alive": sum(1 for f in fe_nodes if f["alive"]),
                "frontend_master": next((f["ip"] for f in fe_nodes if f["is_master"]), None),
                "data_used": _human_bytes(total_used),
                "total_capacity": _human_bytes(total_capacity),
                "disk_used_pct": round(max(used_pcts), 2) if used_pcts else None,
                "database_count": len(db_stats),
                "table_num": sum(d["table_num"] or 0 for d in db_stats),
                "tablet_num": sum(d["tablet_num"] or 0 for d in db_stats),
            },
            "databases": db_stats,
            "database_sizes": db_sizes,
        }
        if fe_note:
            payload["note"] = fe_note
        if include_nodes:
            payload["backends"] = nodes
            payload["frontends"] = fe_nodes
        return dumps(payload)

    @mcp.tool(name="doris_get_compaction_status", annotations=_ro("Compaction 状态"))
    async def doris_get_compaction_status(
        top_n: Annotated[int, Field(description="返回版本堆积最多的前 N 个 tablet", ge=1, le=100)] = 10,
    ) -> str:
        """排查 compaction 落后：按 tablet 统计 rowset（版本）数量（information_schema.rowsets）。
        单个 tablet 的 rowset 数持续偏高说明 compaction 跟不上写入。
        """
        total = await _meta(
            "SELECT COUNT(*) AS rowsets, SUM(DATA_DISK_SIZE) AS bytes, "
            "SUM(ROWSET_NUM_ROWS) AS rows_count FROM information_schema.rowsets"
        )
        if not total.get("ok"):
            return dumps(total)
        top = await _meta(
            "SELECT TABLET_ID, COUNT(*) AS rowset_count, SUM(DATA_DISK_SIZE) AS bytes, "
            "MIN(CREATION_TIME) AS created_at FROM information_schema.rowsets "
            f"GROUP BY TABLET_ID ORDER BY rowset_count DESC LIMIT {int(top_n)}"
        )
        if not top.get("ok"):
            return dumps(top)

        totals = _dicts(total)
        overall = totals[0] if totals else {}
        tablets = [
            {
                "tablet_id": _int(row.get("TABLET_ID")),
                "rowset_count": _int(row.get("rowset_count")),
                "data_size": _human_bytes(_int(row.get("bytes"))),
                "oldest_rowset_created_at": _unix_to_text(row.get("created_at")),
            }
            for row in _dicts(top)
        ]
        payload: dict[str, Any] = {
            "ok": True,
            "total_rowsets": _int(overall.get("rowsets")),
            "total_rows": _int(overall.get("rows_count")),
            "total_data_size": _human_bytes(_int(overall.get("bytes"))),
            "top_tablets": tablets,
            "source": "information_schema.rowsets（1.2 无 compaction 系统表，按 rowset 间接判断）",
        }
        worst = tablets[0]["rowset_count"] if tablets else None
        if worst and worst > COMPACTION_WARN_ROWSETS:
            payload["warnings"] = [
                (
                    f"tablet {tablets[0]['tablet_id']} 有 {worst} 个 rowset"
                    f"（阈值 {COMPACTION_WARN_ROWSETS}），可能存在 compaction 落后"
                )
            ]
        return dumps(payload)

    @mcp.tool(name="doris_list_active_tasks", annotations=_ro("当前正在执行的查询"))
    async def doris_list_active_tasks() -> str:
        """当前正在执行的查询（SHOW PROC '/current_queries'，7 列）。空结果表示当前无活跃查询。"""
        result = await _meta("SHOW PROC '/current_queries'")
        if not result.get("ok"):
            return dumps(result)
        queries = []
        for row in _dicts(result):
            queries.append(
                {
                    "query_id": _text(row.get("QueryId")),
                    "connection_id": _int(row.get("ConnectionId")),
                    "database": _text(row.get("Database")),
                    "user": _text(row.get("User")),
                    "scan_bytes": _int(row.get("ScanBytes")),
                    "process_rows": _int(row.get("ProcessRows")),
                    "exec_time": _text(row.get("ExecTime")),
                }
            )
        return dumps({"ok": True, "query_count": len(queries), "queries": queries})

    @mcp.tool(name="doris_get_runtime_capabilities", annotations=_ro("能力矩阵"))
    async def doris_get_runtime_capabilities(
        probe: Annotated[bool, Field(description="是否额外跑几条廉价语句做实时抽查")] = False,
    ) -> str:
        """返回当前实例的能力矩阵（1.2 实测结论）：哪些语法可用、哪些会挂死或不存在。
        写 SQL 前先查一次，能避免用 2.0+ 语法踩坑。probe=true 时附带实时抽查结果。
        """
        version = await pool.server_version_text()
        capabilities = capabilities_for(version)
        payload: dict[str, Any] = {
            "ok": True,
            "doris_version": version,
            "capability_profile": profile_for(version),
            "supported": [c for c in capabilities if c["supported"]],
            "unsupported": [c for c in capabilities if not c["supported"]],
        }
        if not capabilities:
            payload["note"] = "未知版本，未提供能力矩阵（本项目只校准了 Doris 1.2）"

        if probe:
            live: dict[str, Any] = {}
            for name, sql in live_checks():
                result = await pool.execute(sql, max_rows=1)
                live[name] = {"ok": bool(result.get("ok"))}
                if not result.get("ok"):
                    err = result.get("error", {})
                    live[name]["error"] = f"{err.get('code')} {err.get('message')}"
            payload["live"] = live
        return dumps(payload)

    # ============================================================ 导入
    @mcp.tool(name="doris_get_ingestion_status", annotations=_ro("导入任务状态"))
    async def doris_get_ingestion_status(
        kind: Annotated[str, Field(description="all（默认）/ load / routine_load")] = "all",
        database: Annotated[str | None, Field(
            description="可选，库上下文；SHOW LOAD / SHOW ROUTINE LOAD 需要连接已选库")] = None,
        limit: Annotated[int, Field(description="每类返回的最近任务数", ge=1, le=200)] = 10,
    ) -> str:
        """导入任务状态：SHOW LOAD（17 列，Broker/Routine 一次性导入）与
        SHOW ROUTINE LOAD（19 列，常驻例行导入）。默认两类都给。
        """
        mode = (kind or "all").strip().lower()
        if mode not in {"all", "load", "routine_load"}:
            return _fail("SHOW LOAD", f"kind 只支持 all / load / routine_load，收到 `{kind}`")

        context = database or settings.doris_database
        fallback = False
        if not context:
            context, fallback = "information_schema", True

        payload: dict[str, Any] = {"ok": True, "kind": mode, "database_context": context}

        if mode in {"all", "load"}:
            result = await pool.execute("SHOW LOAD", database=context,
                                        max_rows=settings.doris_max_rows)
            if not result.get("ok"):
                return dumps(result)
            rows = _dicts(result)
            payload["load"] = {
                "count": len(rows),
                "state_summary": _state_summary(rows, "State"),
                "recent": [
                    {
                        "job_id": _int(r.get("JobId")),
                        "label": _text(r.get("Label")),
                        "state": _text(r.get("State")),
                        "type": _text(r.get("Type")),
                        "create_time": _text(r.get("CreateTime")),
                        "error_msg": _text(r.get("ErrorMsg")) or None,
                    }
                    for r in rows[:limit]
                ],
            }

        if mode in {"all", "routine_load"}:
            result = await pool.execute("SHOW ROUTINE LOAD", database=context,
                                        max_rows=settings.doris_max_rows)
            if not result.get("ok"):
                return dumps(result)
            rows = _dicts(result)
            payload["routine_load"] = {
                "count": len(rows),
                "state_summary": _state_summary(rows, "State"),
                "recent": [
                    {
                        "id": _int(r.get("Id")),
                        "name": _text(r.get("Name")),
                        "database": _text(r.get("DbName")),
                        "table": _text(r.get("TableName")),
                        "state": _text(r.get("State")),
                        "lag": _text(r.get("Lag")),
                        "reason_of_state_changed": _text(r.get("ReasonOfStateChanged")) or None,
                    }
                    for r in rows[:limit]
                ],
            }

        if not payload.get("load", {}).get("count") and not payload.get("routine_load", {}).get("count"):
            payload["note"] = "当前没有任何导入任务（或该账号看不到）"
        if fallback:
            payload["note"] = (
                "未指定 database 且未配置 DORIS_DATABASE，已用 information_schema 作为上下文；"
                "SHOW LOAD / SHOW ROUTINE LOAD 按当前库过滤，可能看不到目标库的任务"
            )
        return dumps(payload)

    @mcp.tool(name="doris_list_udfs", annotations=_ro("列出 UDF"))
    async def doris_list_udfs(
        name_like: Annotated[str | None, Field(description="可选，函数名 LIKE 模式")] = None,
        database: Annotated[str | None, Field(
            description="可选，库上下文；1.2 的 SHOW FUNCTIONS 要求连接已选库")] = None,
        limit: Annotated[int, Field(description="本页返回条数", ge=1, le=500)] = 50,
        offset: Annotated[int, Field(description="跳过条数，用于翻页", ge=0)] = 0,
    ) -> str:
        """列出 UDF，分页返回。

        SHOW FUNCTIONS 在 1.2 不支持 LIMIT，也不支持 SHOW GLOBAL FUNCTIONS（语法错误），
        且要求连接已选库；因此取回后在本地切页（UDF 数量有限，代价可忽略）。

        返回：{ok, total, count, offset, limit, items[{name}], has_more, next_offset}
        """
        sql = "SHOW FUNCTIONS"
        if name_like:
            sql += f" LIKE {quote_str(name_like)}"
        context = database or settings.doris_database or "information_schema"
        result = await pool.execute(sql, database=context, max_rows=settings.doris_max_rows)
        if not result.get("ok"):
            return dumps(result)
        names = [_text(row[0]) for row in result["rows"] if row]
        payload: dict[str, Any] = {"ok": True, "source": sql,
                                   **_page([{"name": n} for n in names], limit, offset)}
        if not database and not settings.doris_database:
            payload["note"] = f"未配置默认库，已用 {context} 作为上下文；UDF 是全局对象，不受影响"
        if not names:
            payload["note"] = "该实例未注册自定义函数（内置函数不在此列出）"
        return dumps(payload)


# ---------------------------------------------------------------- 内部辅助
def _default_value(value: Any) -> Any:
    if value is None:
        return None
    text = str(value)
    return None if text.upper() == "NULL" else text


async def _table_basic(pool: DorisPool, database: str, table: str) -> dict[str, Any]:
    sql = f"SHOW TABLE STATUS FROM {quote_ident(database)} LIKE {quote_str(table)}"
    result = await pool.execute(sql, max_rows=META_ROWS, clamp=False)
    if not result.get("ok"):
        return {"error": result.get("error", {}).get("message", "SHOW TABLE STATUS 失败")}
    rows = _dicts(result)
    if not rows:
        return {}
    row = rows[0]
    return {
        "name": _text(row.get("Name")),
        "engine": _text(row.get("Engine")),
        "rows": _int(row.get("Rows")),
        "avg_row_length": _int(row.get("Avg_row_length")),
        "create_time": _text(row.get("Create_time")) or None,
        "update_time": _text(row.get("Update_time")) or None,
        "comment": _text(row.get("Comment")),
    }


async def _table_ddl(pool: DorisPool, database: str, table: str) -> Any:
    sql = f"SHOW CREATE TABLE {quote_ident(database)}.{quote_ident(table)}"
    result = await pool.execute(sql, max_rows=2, clamp=False)
    if not result.get("ok"):
        return {"error": result.get("error", {}).get("message", "SHOW CREATE TABLE 失败")}
    if not result.get("rows"):
        return None
    row = result["rows"][0]
    return row[-1]


async def _fetch_indexes(pool: DorisPool, database: str, table: str) -> list[dict[str, Any]]:
    sql = f"SHOW INDEX FROM {quote_ident(database)}.{quote_ident(table)}"
    result = await pool.execute(sql, max_rows=META_ROWS, clamp=False)
    if not result.get("ok"):
        return [{"error": result.get("error", {}).get("message", "SHOW INDEX 失败")}]

    idx = _index(result["columns"])
    grouped: dict[str, dict[str, Any]] = {}
    for row in result["rows"]:
        name = _text(_cell(row, idx, "Key_name"))
        entry = grouped.setdefault(
            name,
            {
                "name": name,
                "type": _cell(row, idx, "Index_type"),
                "unique": _bool(_cell(row, idx, "Non_unique")) is False,
                "columns": [],
            },
        )
        entry["columns"].append(_cell(row, idx, "Column_name"))
    return list(grouped.values())
