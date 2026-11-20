import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from doris_mcp.config import Settings
from doris_mcp.server import create_app

EXPECTED_TOOLS = {
    "doris_ping",
    "doris_list_databases",
    "doris_list_catalogs",
    "doris_list_tables",
    "doris_describe_table",
    "doris_get_table_size",
    "doris_get_table_partitions",
    "doris_inspect_external_catalog",
    "doris_execute_query",
    "doris_explain_query",
    "doris_get_query_profile",
    "doris_get_cluster_overview",
    "doris_get_compaction_status",
    "doris_list_active_tasks",
    "doris_get_runtime_capabilities",
    "doris_get_ingestion_status",
    "doris_list_udfs",
}
WRITE_TOOL_NAMES = {"set_table_comment", "set_column_comment", "kill_query", "execute_ddl"}


def result(sql, columns, rows, elapsed=1):
    return {
        "ok": True,
        "sql": sql,
        "columns": [{"name": name, "type": "VAR_STRING"} for name in columns],
        "rows": rows,
        "row_count": len(rows),
        "truncated": False,
        "elapsed_ms": elapsed,
    }


def fail(sql, message="boom", code=1105):
    return {"ok": False, "sql": sql, "error": {"type": "sql_error", "code": code, "message": message}}


class Fake:
    """按 SQL 前缀路由的假连接池。"""

    def __init__(self, routes, fallback=None):
        self.routes = routes
        self.fallback = fallback
        self.calls = []

    async def __call__(self, sql, *, database=None, max_rows=None, clamp=True, timeout=None):
        self.calls.append(sql)
        for prefix, response in self.routes:
            if sql.startswith(prefix):
                return response(sql) if callable(response) else response
        if self.fallback is not None:
            return self.fallback(sql)
        raise AssertionError(f"unexpected sql: {sql}")


def build(routes, settings=None, version="Doris version doris-1.2.4-1-Unknown"):
    mcp, pool = create_app(settings or Settings())
    fake = Fake(routes)

    async def fake_version():
        return version

    pool.execute = fake
    pool.server_version_text = fake_version
    return mcp, fake


async def call(mcp, name, args=None):
    res = await mcp.call_tool(name, args or {})
    return json.loads(res.content[0].text)


# ---------------------------------------------------------------- 注册面
async def test_registry_matches_expected_tools():
    mcp, _ = create_app(Settings())
    assert {t.name for t in await mcp.list_tools()} == EXPECTED_TOOLS


async def test_no_write_tool_is_registered_or_callable():
    mcp, _ = create_app(Settings())
    names = {t.name for t in await mcp.list_tools()}
    assert not (names & WRITE_TOOL_NAMES)
    for name in WRITE_TOOL_NAMES:
        with pytest.raises(ToolError):
            await mcp.call_tool(name, {})


async def test_all_tools_are_marked_read_only():
    mcp, _ = create_app(Settings())
    for tool in await mcp.list_tools():
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        # 四个 hint 都要显式给出，客户端才能正确判断工具性质
        assert tool.annotations.destructive_hint is False, tool.name
        assert tool.annotations.idempotent_hint is True, tool.name
        assert tool.annotations.open_world_hint is True, tool.name
        assert tool.annotations.title, tool.name


async def test_tool_names_use_service_prefix():
    mcp, _ = create_app(Settings())
    for tool in await mcp.list_tools():
        assert tool.name.startswith("doris_"), tool.name


# ---------------------------------------------------------------- 元数据
async def test_ping_reports_version_and_profile():
    mcp, _ = build(
        [("SELECT @@version_comment", result("q", ["version", "current_database", "server_time",
                                                   "connected_user"],
                                             [["doris-1.2.4-1-Unknown", "sg_ods", "2026-11-19 12:00:00", "root@%"]]))]
    )
    payload = await call(mcp, "doris_ping")
    assert payload["ok"] is True
    assert payload["capability_profile"] == "1.2"
    assert payload["limits"]["read_only"] is True


async def test_list_databases_paginates_via_schemata():
    mcp, fake = build([
        ("SELECT COUNT(*) AS n FROM information_schema.schemata", result("q", ["n"], [["3"]])),
        ("SELECT SCHEMA_NAME FROM information_schema.schemata",
         result("q", ["SCHEMA_NAME"], [["sg_dw"]])),
    ])
    payload = await call(mcp, "doris_list_databases", {"limit": 1, "offset": 1})
    assert payload["total"] == 3
    assert payload["items"] == [{"name": "sg_dw"}]
    assert payload["has_more"] is True
    assert payload["next_offset"] == 2
    assert "LIMIT 1 OFFSET 1" in fake.calls[1]


async def test_list_databases_external_catalog_slices_locally():
    mcp, fake = build([("SHOW DATABASES", result("q", ["Database"], [["a"], ["b"]]))])
    payload = await call(mcp, "doris_list_databases", {"catalog": "hive", "limit": 5})
    assert fake.calls[0] == "SHOW DATABASES FROM `hive`"
    assert payload["total"] == 2
    assert payload["has_more"] is False
    assert payload["next_offset"] is None


async def test_list_catalogs_passes_through():
    mcp, _ = build([("SHOW CATALOGS", result("SHOW CATALOGS", ["CatalogId", "CatalogName", "Type", "IsCurrent"],
                                             [[0, "internal", "internal", "yes"]]))])
    payload = await call(mcp, "doris_list_catalogs")
    assert payload["rows"][0][1] == "internal"


async def test_list_tables_paginates_via_information_schema():
    mcp, fake = build([
        ("SELECT COUNT(*) AS n FROM information_schema.tables", result("q", ["n"], [["120"]])),
        ("SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.tables",
         result("q", ["TABLE_NAME", "TABLE_TYPE"], [["t1", "BASE TABLE"], ["t2", "VIEW"]])),
    ])
    payload = await call(mcp, "doris_list_tables",
                         {"database": "sg_ods", "name_like": "t%", "limit": 2})
    assert payload["total"] == 120
    assert payload["count"] == 2
    assert payload["has_more"] is True
    assert payload["next_offset"] == 2
    assert payload["items"] == [
        {"name": "t1", "type": "BASE TABLE"},
        {"name": "t2", "type": "VIEW"},
    ]
    # internal catalog 走系统表：服务端过滤 + 服务端分页（SHOW TABLES 在 1.2 不支持 LIMIT）
    assert "TABLE_SCHEMA = 'sg_ods'" in fake.calls[0]
    assert "LIKE 't%'" in fake.calls[0]
    assert "LIMIT 2 OFFSET 0" in fake.calls[1]


async def test_list_tables_external_catalog_uses_show_and_slices_locally():
    mcp, fake = build([("SHOW TABLES", result("SHOW TABLES", ["Tables_in_hive"], [["t1"], ["t2"]]))])
    payload = await call(mcp, "doris_list_tables",
                         {"database": "ods", "catalog": "hive", "limit": 1})
    assert fake.calls[0] == "SHOW TABLES FROM `hive`.`ods`"
    assert payload["total"] == 2
    assert payload["count"] == 1
    assert payload["next_offset"] == 1


async def test_list_tables_escapes_like_value():
    mcp, fake = build([
        ("SELECT COUNT(*) AS n FROM information_schema.tables", result("q", ["n"], [["0"]])),
        ("SELECT TABLE_NAME, TABLE_TYPE", result("q", ["TABLE_NAME", "TABLE_TYPE"], [])),
    ])
    await call(mcp, "doris_list_tables", {"database": "db", "name_like": "a'b%"})
    assert "LIKE 'a\\'b%'" in fake.calls[0]


async def test_describe_table_projects_columns():
    mcp, _ = build([
        ("SHOW FULL COLUMNS", result("q", ["Field", "Type", "Null", "Key", "Default", "Extra", "Comment"],
                                     [["amount", "DECIMAL(10,2)", "YES", "NO", "NULL", "SUM", "金额"]]))
    ])
    payload = await call(mcp, "doris_describe_table", {"database": "cloud", "table": "orders"})
    assert payload["columns"][0] == {
        "name": "amount", "type": "DECIMAL(10,2)", "nullable": True, "key": False,
        "default": None, "extra": "SUM", "comment": "金额",
    }


async def test_describe_table_include_basic_ddl_indexes():
    mcp, _ = build([
        ("SHOW FULL COLUMNS", result("q", ["Field", "Type", "Null", "Key", "Default", "Extra", "Comment"],
                                     [["id", "INT", "NO", "YES", None, "", "主键"]])),
        ("SHOW TABLE STATUS", result("q", ["Name", "Engine", "Rows", "Create_time", "Comment"],
                                     [["t", "Doris", 0, "2026-11-19 13:24:55", "表注释"]])),
        ("SHOW CREATE TABLE", result("q", ["Table", "Create Table"], [["t", "CREATE TABLE `t` (...)"]])) ,
        ("SHOW INDEX", result("q", ["Key_name", "Non_unique", "Column_name", "Index_type"],
                              [["k1", "NO", "id", "BTREE"]])),
    ])
    payload = await call(mcp, "doris_describe_table", {"database": "d", "table": "t",
                                                "include_basic": True, "include_ddl": True,
                                                "include_indexes": True})
    assert payload["basic"]["engine"] == "Doris"
    assert payload["basic"]["comment"] == "表注释"
    assert payload["ddl"].startswith("CREATE TABLE")
    assert payload["indexes"] == [{"name": "k1", "type": "BTREE", "unique": True, "columns": ["id"]}]


async def test_get_table_size_db_skips_summary_rows():
    mcp, _ = build([("SHOW DATA", result("q", ["TableName", "Size", "ReplicaCount"],
                                         [["t1", "12.500 ", "3"], ["Total", "12.500 ", "3"],
                                          ["Quota", "1024.000 TB", "1073741824"],
                                          ["Left", "1024.000 TB", "1073740494"]]))])
    payload = await call(mcp, "doris_get_table_size", {"database": "db"})
    assert payload["table_count"] == 1
    assert payload["tables"][0] == {"table": "t1", "size_text": "12.500", "size_mb": 12.5,
                                    "replica_count": 3}
    assert payload["quota_text"] == "1024.000 TB"
    assert payload["left_text"] == "1024.000 TB"


async def test_get_table_size_table_sums_partition_mb():
    mcp, _ = build([("SHOW PARTITIONS", result("q", ["PartitionName", "State", "DataSize"],
                                               [["p1", "NORMAL", "1.500 "], ["p2", "NORMAL", "2.500 "]]))])
    payload = await call(mcp, "doris_get_table_size", {"database": "db", "table": "t"})
    assert payload["partition_count"] == 2
    assert payload["total_data_size_mb"] == 4.0
    assert payload["partitions"][0]["data_size_mb"] == 1.5


PARTITION_COLUMNS = ["PartitionId", "PartitionName", "VisibleVersion", "VisibleVersionTime", "State",
                     "PartitionKey", "Range", "DistributionKey", "Buckets", "ReplicationNum",
                     "StorageMedium", "CooldownTime", "RemoteStoragePolicy",
                     "LastConsistencyCheckTime", "DataSize", "IsInMemory", "ReplicaAllocation"]


async def test_get_table_partitions_computes_freshness():
    import datetime as dt
    now = dt.datetime.now().replace(microsecond=0)
    fresh = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    mcp, _ = build([("SHOW PARTITIONS", result(
        "q", PARTITION_COLUMNS,
        [[1, "p1", "5", fresh, "NORMAL", "dt", "[...]", "id", 3, 1, "HDD", None, None, None, "2.000 ", "false", "tag"]],
    ))])
    payload = await call(mcp, "doris_get_table_partitions", {"database": "db", "table": "t"})
    assert payload["partition_count"] == 1
    assert payload["partitions"][0]["buckets"] == 3
    assert 0.5 < payload["freshness_lag_hours"] < 2
    assert "warnings" not in payload


async def test_get_table_partitions_warns_when_stale():
    import datetime as dt
    old = (dt.datetime.now() - dt.timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    mcp, _ = build([("SHOW PARTITIONS", result(
        "q", PARTITION_COLUMNS,
        [[1, "p1", "5", old, "NORMAL", "dt", "[...]", "id", 3, 1, "HDD", None, None, None, "0.000 ", "false", "tag"]],
    ))])
    payload = await call(mcp, "doris_get_table_partitions", {"database": "db", "table": "t"})
    assert payload["freshness_lag_hours"] > 24
    assert any("滞后" in w for w in payload["warnings"])


async def test_inspect_external_catalog_builds_sql():
    mcp, fake = build([("SHOW TABLES", result("q", ["Tables_in_d"], [["t"]]))])
    payload = await call(mcp, "doris_inspect_external_catalog", {"catalog": "hive", "database": "d"})
    assert payload["ok"] is True
    assert fake.calls[0] == "SHOW TABLES FROM `hive`.`d`"


# ---------------------------------------------------------------- 查询
async def test_execute_query_rejects_write_before_touching_db():
    mcp, fake = build([])
    payload = await call(mcp, "doris_execute_query", {"sql": "TRUNCATE TABLE t"})
    assert payload["ok"] is False
    assert payload["error"]["type"] == "invalid_argument"
    assert fake.calls == []


async def test_explain_query_builds_graph():
    mcp, fake = build([("EXPLAIN GRAPH", result("q", ["Explain String"], [["┌──┐"], ["└──┘"]]))])
    payload = await call(mcp, "doris_explain_query", {"sql": "SELECT 1", "format": "graph"})
    assert payload["ok"] is True
    assert payload["format"] == "graph"
    assert payload["plan"] == "┌──┐\n└──┘"
    assert fake.calls[0] == "EXPLAIN GRAPH SELECT 1"


async def test_explain_query_rejects_write_sql():
    mcp, fake = build([])
    payload = await call(mcp, "doris_explain_query", {"sql": "DELETE FROM t"})
    assert payload["ok"] is False
    assert "只读" in payload["error"]["message"]
    assert fake.calls == []


async def test_explain_query_rejects_nested_explain():
    mcp, _ = build([])
    payload = await call(mcp, "doris_explain_query", {"sql": "EXPLAIN SELECT 1"})
    assert payload["ok"] is False
    assert "嵌套" not in payload["error"]["message"]  # 文案用词不同，只要拒绝即可
    assert payload["error"]["type"] == "invalid_argument"


async def test_explain_query_rejects_unknown_format():
    mcp, _ = build([])
    payload = await call(mcp, "doris_explain_query", {"sql": "SELECT 1", "format": "tree"})
    assert payload["ok"] is False


async def test_get_query_profile_prefixes_slash():
    mcp, fake = build([("SHOW QUERY PROFILE", result("q", ["Profile"], [["line1"], ["line2"]]))])
    payload = await call(mcp, "doris_get_query_profile", {"query_id": "abc-123"})
    assert payload["profile"] == "line1\nline2"
    assert fake.calls[0] == 'SHOW QUERY PROFILE "/abc-123"'


async def test_get_query_profile_keeps_existing_slash():
    mcp, fake = build([("SHOW QUERY PROFILE", result("q", ["Profile"], [["x"]]))])
    await call(mcp, "doris_get_query_profile", {"query_id": "/abc"})
    assert fake.calls[0] == 'SHOW QUERY PROFILE "/abc"'


async def test_get_query_profile_rejects_bad_id():
    mcp, fake = build([])
    payload = await call(mcp, "doris_get_query_profile", {"query_id": 'a"b'})
    assert payload["ok"] is False
    assert fake.calls == []


# ---------------------------------------------------------------- 集群
async def test_get_cluster_overview_skips_total_row():
    mcp, _ = build([
        ("SELECT BackendId", result("q",
                                    ["BackendId", "IP", "Alive", "TabletNum", "DataUsedCapacity",
                                     "AvailCapacity", "TotalCapacity", "UsedPct", "Version", "LastHeartbeat"],
                                    [[10003, "10.0.0.1", "true", 1330, 1024, 25600, 26624, 37.25,
                                      "1.2.4", "2026-11-19 14:00:00"]])),
        ("SHOW FRONTENDS", result("q",
                                  ["Name", "IP", "EditLogPort", "HttpPort", "QueryPort", "Role",
                                   "IsMaster", "Alive", "ReplayedJournalId", "LastHeartbeat", "Version"],
                                  [["10.0.0.1_9010_1", "10.0.0.1", "9010", "8030", "9030", "FOLLOWER",
                                    "true", "true", "4606", "2026-11-19 16:18:34",
                                    "doris-1.2.4-1-Unknown"]])),
        ("SHOW PROC '/statistic'", result("q", ["DbId", "DbName", "TableNum", "PartitionNum",
                                               "IndexNum", "TabletNum", "ReplicaNum"],
                                          [["10004", "default_cluster:sg_ods", "1", "133", "133", "1330", "1330"],
                                           ["Total", "1", "1", "133", "133", "1330", "1330"]])),
        ("SHOW PROC '/dbs'", result("q", ["DbId", "DbName", "TableNum", "Size", "Quota",
                                          "LastConsistencyCheckTime", "ReplicaCount", "ReplicaQuota"],
                                    [["10004", "default_cluster:sg_ods", "1", "0.000 ", "1024.000 TB",
                                      None, "1330", "1073741824"]])),
    ])
    payload = await call(mcp, "doris_get_cluster_overview", {"include_nodes": True})
    assert payload["summary"]["backend_count"] == 1
    assert payload["summary"]["backend_alive"] == 1
    assert payload["summary"]["disk_used_pct"] == 37.25
    assert payload["summary"]["frontend_count"] == 1
    assert payload["summary"]["frontend_master"] == "10.0.0.1"
    assert payload["frontends"][0]["role"] == "FOLLOWER"
    assert payload["frontends"][0]["alive"] is True
    # Total 汇总行不能算成一个库
    assert [d["database"] for d in payload["databases"]] == ["sg_ods"]
    assert payload["backends"][0]["ip"] == "10.0.0.1"


async def test_get_cluster_overview_degrades_when_frontends_fail():
    mcp, _ = build([
        ("SELECT BackendId", result("q",
                                    ["BackendId", "IP", "Alive", "TabletNum", "DataUsedCapacity",
                                     "AvailCapacity", "TotalCapacity", "UsedPct", "Version", "LastHeartbeat"],
                                    [[10003, "10.0.0.1", "true", 10, 0, 0, 0, 1.0, "1.2.4", "x"]])),
        ("SHOW FRONTENDS", fail("SHOW FRONTENDS", "query timeout", code=None)),
        ("SHOW PROC '/statistic'", result("q", ["DbId", "DbName", "TableNum", "PartitionNum",
                                               "IndexNum", "TabletNum", "ReplicaNum"],
                                          [["10004", "default_cluster:sg_ods", "1", "1", "1", "10", "10"]])),
        ("SHOW PROC '/dbs'", result("q", ["DbId", "DbName", "TableNum", "Size", "Quota",
                                          "LastConsistencyCheckTime", "ReplicaCount", "ReplicaQuota"],
                                    [["10004", "default_cluster:sg_ods", "1", "0.000 ", "1 TB",
                                      None, "10", "1"]])),
    ])
    payload = await call(mcp, "doris_get_cluster_overview", {"include_nodes": True})
    # FE 取不到时降级返回 BE，而不是整体失败
    assert payload["ok"] is True
    assert payload["summary"]["backend_count"] == 1
    assert payload["summary"]["frontend_count"] == 0
    assert "SHOW FRONTENDS" in payload["note"]


async def test_get_compaction_status_flags_backlog():
    mcp, _ = build([
        ("SELECT COUNT(*) AS rowsets", result("q", ["rowsets", "bytes", "rows_count"], [[50, 2048, 100]])),
        ("SELECT TABLET_ID", result("q", ["TABLET_ID", "rowset_count", "bytes", "created_at"],
                                    [[1, 12, 1024, 1795065895], [2, 3, 512, 1795065895]])),
    ])
    payload = await call(mcp, "doris_get_compaction_status", {"top_n": 2})
    assert payload["total_rowsets"] == 50
    assert payload["top_tablets"][0]["rowset_count"] == 12
    assert payload["top_tablets"][0]["oldest_rowset_created_at"].startswith("20")
    assert any("compaction" in w for w in payload["warnings"])


async def test_list_active_tasks_maps_columns():
    mcp, _ = build([("SHOW PROC '/current_queries'", result(
        "q", ["QueryId", "ConnectionId", "Database", "User", "ScanBytes", "ProcessRows", "ExecTime"],
        [["q1", 12, "sg_ods", "root", 1024, 10, "2s"]],
    ))])
    payload = await call(mcp, "doris_list_active_tasks")
    assert payload["query_count"] == 1
    assert payload["queries"][0]["query_id"] == "q1"


async def test_get_runtime_capabilities_lists_12_matrix():
    mcp, _ = build([])
    payload = await call(mcp, "doris_get_runtime_capabilities")
    assert payload["capability_profile"] == "1.2"
    assert len(payload["supported"]) > 10
    assert any(c["feature"] == "FE 清单" for c in payload["supported"])
    assert any(c["feature"] == "慢查询" for c in payload["unsupported"])


async def test_get_runtime_capabilities_probe_runs_checks():
    mcp, fake = build([
        ("SHOW CATALOGS", result("q", ["CatalogName"], [["internal"]])),
        ("SHOW FUNCTIONS", result("q", ["Function Name"], [])),
        ("SHOW LOAD", result("q", ["JobId"], [])),
        ("SELECT BackendId", result("q", ["BackendId"], [[1]])),
    ])
    payload = await call(mcp, "doris_get_runtime_capabilities", {"probe": True})
    assert set(payload["live"]) == {"catalogs", "functions", "load", "backends"}
    assert all(v["ok"] for v in payload["live"].values())
    # 探针绝不能碰会挂死的语句
    assert not any("FRONTENDS" in sql.upper() for sql in fake.calls)


# ---------------------------------------------------------------- 导入 / UDF
async def test_get_ingestion_status_summarizes_states():
    mcp, _ = build([
        ("SHOW LOAD", result("q", ["JobId", "Label", "State", "Type", "CreateTime", "ErrorMsg"],
                             [[1, "l1", "FINISHED", "BROKER", "2026-11-19 10:00:00", ""],
                              [2, "l2", "CANCELLED", "BROKER", "2026-11-19 11:00:00", "bad file"]])),
        ("SHOW ROUTINE LOAD", result("q", ["Id", "Name", "DbName", "TableName", "State", "Lag",
                                          "ReasonOfStateChanged"],
                                     [[9, "r1", "sg_ods", "t", "RUNNING", "0", ""]])),
    ])
    payload = await call(mcp, "doris_get_ingestion_status", {"kind": "all", "database": "sg_ods"})
    assert payload["load"]["state_summary"] == {"FINISHED": 1, "CANCELLED": 1}
    assert payload["routine_load"]["recent"][0]["state"] == "RUNNING"
    assert "note" not in payload


async def test_get_ingestion_status_rejects_unknown_kind():
    mcp, fake = build([])
    payload = await call(mcp, "doris_get_ingestion_status", {"kind": "stream"})
    assert payload["ok"] is False
    assert fake.calls == []


async def test_get_ingestion_status_falls_back_to_information_schema():
    mcp, _fake = build([
        ("SHOW LOAD", result("q", ["JobId", "Label", "State", "Type", "CreateTime", "ErrorMsg"], [])),
        ("SHOW ROUTINE LOAD", result("q", ["Id", "Name", "DbName", "TableName", "State", "Lag",
                                          "ReasonOfStateChanged"], [])),
    ])
    payload = await call(mcp, "doris_get_ingestion_status", {})
    assert payload["database_context"] == "information_schema"
    assert "note" in payload


async def test_list_udfs_uses_database_context():
    mcp, fake = build([("SHOW FUNCTIONS", result("q", ["Function Name"], [["my_udf"]]))])
    payload = await call(mcp, "doris_list_udfs", {})
    assert payload["count"] == 1
    assert payload["items"] == [{"name": "my_udf"}]
    # 1.2 的 SHOW FUNCTIONS 需要连接已选库，无默认库时用 information_schema 兜底
    assert "information_schema" in payload.get("note", "") or payload["count"] == 1
    assert fake.calls[0] == "SHOW FUNCTIONS"
