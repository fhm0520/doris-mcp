"""应用装配：连接池 + MCPServer + 工具注册。

安全策略与官方 doris-mcp-server 一致：**纯只读**。官方 55 个子工具全部只读
（`doris_admin` 域明确不注册），本项目同样不提供任何写语句通道，也没有放开写的开关。
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import __version__
from .config import Settings
from .db import DorisPool
from .tools import register

INSTRUCTIONS = """Apache Doris 1.2.x（doris-1.2.4-1-Unknown）专用的只读 MCP Server。
17 个工具全部以 doris_ 前缀命名，避免与其他数据库类 MCP 混用。

推荐流程：
1. 不确定库表时，先 doris_list_databases / doris_list_catalogs / doris_list_tables（都支持 limit+offset 翻页）。
2. 写 SQL 前先 doris_describe_table 确认字段名、类型和注释（include_basic / include_ddl 可看引擎与建表语句）。
3. 任意只读 SQL 用 doris_execute_query。
4. 慢 SQL 用 doris_explain_query（format=graph 看执行图），需要 profile 时用 doris_get_query_profile。
5. 体量问题看 doris_get_table_size / doris_get_table_partitions（含数据新鲜度）。
6. 集群与导入：doris_get_cluster_overview / doris_get_compaction_status / doris_get_ingestion_status。
7. 不确定某个语法在 1.2 上是否可用，先调 doris_get_runtime_capabilities，不要靠猜。

本服务是只读的：只能执行 SELECT / SHOW / DESC / EXPLAIN / WITH，没有任何写工具。
需要改注释、建表等写操作，请在 Doris 侧用别的客户端执行。

Doris 1.2 限制（写成 2.0+ 语法会报错）：
- SHOW FRONTENDS 取 FE；SHOW PROC '/frontends' 在部分实例上很慢（十几秒），别用它
- BE 信息用 information_schema.backends（列名是 IP）
- 没有 information_schema.active_queries、__internal_schema.audit_log、mv_infos()、SHOW WORKLOAD GROUPS
- 没有 SHOW GLOBAL FUNCTIONS，UDF 用 SHOW FUNCTIONS
- SHOW DATA 必须先选库；SHOW DATA FROM db 报 unknown table
- 没有倒排索引 / ANN / Variant / BM25
- 并行度变量是 parallel_fragment_exec_instance_num，不是 parallel_pipeline_task_num
"""


def create_app(settings: Settings | None = None) -> tuple[MCPServer, DorisPool]:
    settings = settings or Settings()
    pool = DorisPool(settings)
    mcp = MCPServer(name="doris_mcp", version=__version__, instructions=INSTRUCTIONS)
    register(mcp, pool, settings)
    return mcp, pool
