"""用 MCP 客户端起 stdio server，逐个调用全部只读工具。

用法：
  uv run python scripts/smoke.py
  uv run python scripts/smoke.py --db <db> --table <tbl>

期望结果写死在 build_calls 里：
  ok       必须成功
  rejected 必须被只读沙箱拒绝（且不落库）
  either   成功或报错都算通过（如 profile 需要真实 query id）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

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
WRITE_TOOL_NAMES = ("set_table_comment", "set_column_comment", "kill_query", "execute_ddl")


def build_calls(args: argparse.Namespace) -> list[tuple[str, dict, str]]:
    db, table = args.db, args.table
    calls: list[tuple[str, dict, str]] = [
        ("doris_ping", {}, "ok"),
        ("doris_list_catalogs", {}, "ok"),
        ("doris_list_databases", {}, "ok"),
        ("doris_list_databases", {"catalog": "internal"}, "ok"),
        ("doris_list_udfs", {}, "ok"),
        ("doris_list_udfs", {"name_like": "date%"}, "ok"),
        ("doris_execute_query", {"sql": "SELECT 1 AS ok"}, "ok"),
        ("doris_execute_query", {"sql": "ALTER TABLE t MODIFY COMMENT 'x'"}, "rejected"),
        ("doris_execute_query", {"sql": "DROP TABLE t"}, "rejected"),
        ("doris_list_active_tasks", {}, "ok"),
        ("doris_get_cluster_overview", {"include_nodes": True}, "ok"),
        ("doris_get_compaction_status", {"top_n": 5}, "ok"),
        ("doris_get_ingestion_status", {"kind": "all"}, "ok"),
        ("doris_get_runtime_capabilities", {"probe": True}, "ok"),
        ("doris_inspect_external_catalog", {"catalog": "internal"}, "ok"),
        ("doris_get_query_profile", {"query_id": "00000000-0000-0000-0000-000000000000"}, "either"),
    ]
    if db:
        calls += [
            ("doris_list_tables", {"database": db}, "ok"),
            ("doris_list_tables", {"database": db, "limit": 1, "offset": 0}, "ok"),
            ("doris_get_table_size", {"database": db}, "ok"),
            ("doris_inspect_external_catalog", {"catalog": "internal", "database": db}, "ok"),
        ]
    if db and table:
        calls += [
            ("doris_describe_table", {"database": db, "table": table}, "ok"),
            (
                "doris_describe_table",
                {"database": db, "table": table, "include_basic": True,
                 "include_ddl": True, "include_indexes": True},
                "ok",
            ),
            ("doris_get_table_size", {"database": db, "table": table}, "ok"),
            ("doris_get_table_partitions", {"database": db, "table": table}, "ok"),
            ("doris_explain_query", {"sql": f"SELECT * FROM {db}.{table} LIMIT 1"}, "ok"),
            ("doris_explain_query", {"sql": f"SELECT * FROM {db}.{table} LIMIT 1", "format": "graph"}, "ok"),
            ("doris_explain_query", {"sql": f"SELECT * FROM {db}.{table} LIMIT 1", "format": "verbose"}, "ok"),
            # EXPLAIN 只接受只读 SQL
            ("doris_explain_query", {"sql": "ALTER TABLE t MODIFY COMMENT 'x'"}, "rejected"),
        ]
    return calls


def summarize(name: str, payload: dict) -> str:
    key = name.removeprefix("doris_")
    if not payload.get("ok"):
        err = payload.get("error", {})
        return f"rejected type={err.get('type')} msg={str(err.get('message'))[:60]}"
    if key == "describe_table":
        parts = [f"cols={payload.get('column_count')}"]
        if payload.get("basic") is not None:
            parts.append(f"basic={payload['basic'] if isinstance(payload['basic'], str) else 'yes'}")
        if payload.get("ddl"):
            parts.append("ddl=yes")
        if payload.get("indexes") is not None:
            parts.append(f"indexes={len(payload['indexes'])}")
        return "ok " + " ".join(parts)
    if key in {"get_table_size", "get_table_partitions", "get_compaction_status", "get_ingestion_status"}:
        keys = [k for k in ("table_count", "partition_count", "total_rowsets", "freshness_lag_hours") if k in payload]
        detail = " ".join(f"{k}={payload[k]}" for k in keys)
        return f"ok {detail}".strip()
    if key in {"list_databases", "list_tables", "list_udfs"}:
        return (f"ok total={payload.get('total')} count={payload.get('count')} "
                f"has_more={payload.get('has_more')}")
    if key == "get_cluster_overview":
        summary = payload.get("summary", {})
        return f"ok be={summary.get('backend_alive')}/{summary.get('backend_count')} db={summary.get('database_count')}"
    if key == "explain_query":
        return f"ok format={payload.get('format')} lines={payload.get('line_count')}"
    if key == "get_runtime_capabilities":
        return f"ok supported={len(payload.get('supported', []))} unsupported={len(payload.get('unsupported', []))}"
    if key == "list_udfs":
        return f"ok count={payload.get('count')}"
    if "row_count" in payload:
        return f"ok row_count={payload['row_count']}"
    return "ok"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.getenv("SMOKE_DB", ""))
    parser.add_argument("--table", default=os.getenv("SMOKE_TABLE", ""))
    args = parser.parse_args()

    params = StdioServerParameters(command=sys.executable, args=["-m", "doris_mcp"], env={**os.environ})
    problems = 0
    async with stdio_client(params) as (read, write):  # noqa: SIM117 - 两层上下文分开更易读
        async with ClientSession(read, write) as session:
            await session.initialize()

            names = {t.name for t in (await session.list_tools()).tools}
            print(f"tools ({len(names)}): {', '.join(sorted(names))}")
            if names != EXPECTED_TOOLS:
                problems += 1
                print(f"  !! 工具集不符；多={sorted(names - EXPECTED_TOOLS)} 少={sorted(EXPECTED_TOOLS - names)}")
            leaked = names & set(WRITE_TOOL_NAMES)
            if leaked:
                problems += 1
                print(f"  !! 出现写工具：{sorted(leaked)}")

            for name in WRITE_TOOL_NAMES:
                result = await session.call_tool(name, {"database": "x", "table": "y", "comment": "z"})
                ok = result.is_error
                if not ok:
                    problems += 1
                print(f"- {name}(未注册)\n    -> {'OK(拒绝)' if ok else '!! 竟然可调用'}")
            print()

            for name, call_args, expected in build_calls(args):
                result = await session.call_tool(name, call_args)
                text = result.content[0].text if result.content else ""
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {"ok": False, "raw": text}

                got_ok = bool(payload.get("ok"))
                if expected == "ok":
                    passed = got_ok
                elif expected == "rejected":
                    passed = not got_ok and payload.get("error", {}).get("type") == "invalid_argument"
                else:
                    passed = True
                if not passed:
                    problems += 1
                print(f"- {name}{call_args if call_args else ''}")
                print(f"    -> {summarize(name, payload)}  [{'ok' if passed else '!!'}]")

            print(f"\n问题 {problems} 个")
            return 0 if problems == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
