# doris-mcp

Apache Doris 的**只读 MCP Server**，适配版本：`doris-1.2.4-1-Unknown`。

提供 17 个只读工具，覆盖库表元数据、只读 SQL、执行计划、集群与导入状态。

## 环境要求

- Python **≥ 3.12**（项目已带 `.python-version`）
- [uv](https://docs.astral.sh/uv/)（负责 Python 版本与依赖）

## 初始化与运行

### 1. 安装 uv（已装可跳过）

```bash
# Windows PowerShell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. 初始化环境

```bash
cd doris-mcp              # 进入本项目目录（路径随你放置的位置）

uv python install 3.12   # 安装项目所需 Python（已有可跳过）
uv sync                  # 创建 .venv 并按 uv.lock 安装依赖（含 dev 组）
```

`uv sync` 会自动创建 `.venv`，不需要手动 `venv`/`pip`。如需在 shell 里直接用命令：

```bash
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
```

### 3. 配置连接

```bash
cp .env.example .env     # 然后按实际环境改 .env
```

或用 CLI 参数直连（参数优先级高于环境变量）：

```bash
uv run doris-mcp --doris-host 10.0.0.10 --doris-port 9030 \
                 --doris-user readonly_user --doris-password '***' \
                 --doris-database your_db
```

### 4. 验证

```bash
uv run doris-mcp --version
uv run doris-mcp --help
uv run python scripts/smoke.py --db <db> --table <tbl>   # 逐个调用 17 个工具
```

默认 `stdio` 传输，由 MCP 宿主拉起；直接命令行运行会等待协议输入，属正常现象。

## 提供的工具（17 个，全部只读）

### 元数据

| 工具 | 用途 | 数据源 |
|---|---|---|
| `doris_ping` | 连通性、版本、默认库、能力剖面 | `SELECT @@version_comment, ...` |
| `doris_list_catalogs` | catalog 清单 | `SHOW CATALOGS` |
| `doris_list_databases` | 库清单，可指定 catalog，`limit`/`offset` 分页 | `SHOW DATABASES [FROM c]` / `information_schema.schemata` |
| `doris_list_tables` | 表清单，支持 `LIKE` 过滤与 `limit`/`offset` 分页，区分表/视图 | `SHOW TABLES FROM [c.]db [LIKE]` / `information_schema.tables` |
| `doris_describe_table` | 字段名/类型/是否 key/默认值/聚合类型/**注释**；可选引擎行数、建表 DDL、索引 | `SHOW FULL COLUMNS`（+ `SHOW TABLE STATUS` / `SHOW CREATE TABLE` / `SHOW INDEX`） |
| `doris_get_table_size` | 库内各表大小；指定表则汇总分区 `DataSize` | `SHOW DATA` / `SHOW PARTITIONS` |
| `doris_get_table_partitions` | 分区清单 + 数据新鲜度滞后 | `SHOW PARTITIONS` |
| `doris_inspect_external_catalog` | 跨 catalog 浏览库表 | `SHOW DATABASES FROM c` / `SHOW TABLES FROM c.db` |

### 查询

| 工具 | 用途 | 数据源 |
|---|---|---|
| `doris_execute_query` | 执行只读 SQL | 原样执行（SELECT/SHOW/DESC/EXPLAIN/WITH） |
| `doris_explain_query` | 执行计划，`format=plan/verbose/graph`（graph 出 ASCII 执行图） | `EXPLAIN` / `EXPLAIN VERBOSE` / `EXPLAIN GRAPH` |
| `doris_get_query_profile` | 单个查询的 profile | `SHOW QUERY PROFILE "/id"` |

### 集群

| 工具 | 用途 | 数据源 |
|---|---|---|
| `doris_get_cluster_overview` | FE/BE 节点、容量、库表统计、库容量（FE 取不到时降级只给 BE） | `SHOW FRONTENDS` + `information_schema.backends` + `SHOW PROC '/statistic'` + `'/dbs'` |
| `doris_get_compaction_status` | 按 tablet 统计版本堆积，判断 compaction 落后 | `information_schema.rowsets` |
| `doris_list_active_tasks` | 当前正在跑的查询 | `SHOW PROC '/current_queries'` |
| `doris_get_runtime_capabilities` | 哪些语法在 1.2 上可用（可开 `probe` 实时抽查） | 内置能力矩阵 |

### 导入

| 工具 | 用途 | 数据源 |
|---|---|---|
| `doris_get_ingestion_status` | 导入任务状态与失败原因 | `SHOW LOAD` / `SHOW ROUTINE LOAD` |
| `doris_list_udfs` | 自定义函数清单，`limit`/`offset` 分页 | `SHOW FUNCTIONS` |

### 分页与返回结构

三个列表工具（`doris_list_databases` / `doris_list_tables` / `doris_list_udfs`）统一返回分页信封：

```json
{"ok":true,"total":120,"count":50,"offset":0,"limit":50,
 "items":[{"name":"ods_orders","type":"BASE TABLE"}],
 "has_more":true,"next_offset":50}
```

- 翻下一页：把 `next_offset` 当作 `offset` 再调一次；`has_more=false` 表示到底
- internal catalog 走 `information_schema`（schemata / tables）在**服务端**分页——
  1.2 的 `SHOW DATABASES` / `SHOW TABLES` 不支持 `LIMIT`
- 指定其他 catalog 时用 `SHOW ... FROM`，取回后本地切页，`items[].type` 为 `null`

### 工具注解

17 个工具都带 MCP `annotations`：`readOnlyHint=true`、`destructiveHint=false`、
`idempotentHint=true`、`openWorldHint=true`，并各有一个中文 `title`，宿主可据此判断工具性质。

### 典型问法

- "这个库有哪些表" → `doris_list_databases` → `doris_list_tables`
- "这表有哪些字段、注释是什么" → `doris_describe_table`
- "这张表多大、多少个分区、数据新不新" → `doris_get_table_size` / `doris_get_table_partitions`
- "这条 SQL 为什么慢" → `doris_explain_query(format="graph")`，需要细节时 `doris_get_query_profile`
- "现在有谁在跑查询" → `doris_list_active_tasks`
- "集群几台机器、磁盘够不够" → `doris_get_cluster_overview`
- "某个 SQL 语法能不能用" → `doris_get_runtime_capabilities`

## 只读保证

写语句（`ALTER` / `INSERT` / `UPDATE` / `DELETE` / `DROP` / `TRUNCATE` / `SET GLOBAL` /
`INTO OUTFILE` / `KILL` …）在**碰数据库之前**就被沙箱拒绝，返回 `invalid_argument`；
`doris_explain_query` 也只接受只读 SQL。

只读是硬编码策略，没有放开写的环境变量或开关。改表结构、改注释请用别的客户端在 Doris 侧执行。

## 配置

优先级：CLI 参数 > 环境变量 > `.env` > 默认值。

| 变量 | 默认 | 说明 |
|---|---|---|
| `DORIS_HOST` / `DORIS_PORT` | `127.0.0.1` / `9030` | FE 的 MySQL 协议地址 |
| `DORIS_USER` / `DORIS_PASSWORD` | `root` / 空 | **建议用只有 `SELECT_PRIV` 的账号** |
| `DORIS_DATABASE` | 空 | 默认库 |
| `DORIS_MAX_ROWS` | `200` | 单次返回行数上限（硬上限） |
| `DORIS_MAX_RESULT_BYTES` | `1000000` | 单次返回字节上限 |
| `DORIS_CONNECT_TIMEOUT` | `10` | 建连超时（秒） |
| `DORIS_QUERY_TIMEOUT` | `60` | 查询超时（秒） |
| `DORIS_POOL_SIZE` | `4` | 连接池大小 |
| `DORIS_RETRIES` | `2` | 传输层重试次数（2013/10054），0 表示不重试 |
| `MCP_TRANSPORT` | `stdio` | `stdio` 或 `streamable-http` |
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | `127.0.0.1` / `3000` / `/mcp` | HTTP 模式 |
| `LOG_LEVEL` | `INFO` | 日志只写 stderr |

## 接入 MCP 宿主

**推荐**：把 CLI 装到 PATH，配置里就不用写任何路径。在项目目录执行一次：

```bash
uv tool install .          # 安装 doris-mcp 命令（依赖隔离，与项目 .venv 无关）
doris-mcp --version        # 能打印版本即可
```

stdio（Claude Desktop / Cherry Studio / Cline 等）：

```json
{
  "mcpServers": {
    "doris": {
      "command": "doris-mcp",
      "args": ["--transport", "stdio"],
      "env": {
        "DORIS_HOST": "10.0.0.10",
        "DORIS_PORT": "9030",
        "DORIS_USER": "readonly_user",
        "DORIS_PASSWORD": "***",
        "DORIS_DATABASE": "your_db"
      }
    }
  }
}
```

如果不想安装，也可以让宿主用 `uv` 拉起，把 `./doris-mcp` 换成你放置本项目的相对或绝对路径：

```json
"command": "uv",
"args": ["--directory", "./doris-mcp", "run", "doris-mcp", "--transport", "stdio"]
```

> 宿主的启动工作目录通常不是你的 shell 目录，相对路径可能解析不到；这种情况下
> 用上面的 `uv tool install` 方式更稳。

HTTP：

```bash
uv run doris-mcp --transport http --port 3000
# 端点：http://127.0.0.1:3000/mcp
```

## 返回格式

所有工具返回紧凑 JSON，**从不抛异常**（MCP 层会把异常包成 `UnexpectedToolError`，真实原因会丢）。

```json
{"ok":true,"sql":"SHOW DATABASES","columns":[{"name":"Database","type":"VAR_STRING"}],
 "rows":[["cloud"]],"row_count":1,"truncated":false,"elapsed_ms":3}
```

失败时 `ok:false`，`error` 里带 `code` 与 `hint`：

```json
{"ok":false,"sql":"SHOW BACKENDS","target":"root@h:9030/cloud",
 "error":{"type":"sql_error","code":1064,"message":"...",
          "hint":"当前连接 Doris version doris-1.2.4-1-Unknown。Doris 1.2 常见 2.0+ 语法不兼容：..."}}
```

## 1.2 使用注意

以下差异在 `doris-1.2.4-1-Unknown` 上实测，写 SQL 时容易踩：

- 表大小用 `SHOW DATA`，**必须先 `USE db`**；`SHOW DATA FROM db` 报 unknown table
- 分区用 `SHOW PARTITIONS FROM db.tbl`（17 列，含 `DataSize`/`VisibleVersionTime`）
- 节点信息：`SHOW FRONTENDS`、`information_schema.backends`（列名是 `IP`）
- UDF 用 `SHOW FUNCTIONS`；`SHOW GLOBAL FUNCTIONS` 是语法错误
- profile 是 `SHOW QUERY PROFILE "/id"`，path 必须带 `/` 前缀
- 当前查询用 `SHOW PROC '/current_queries'`；没有 `/slow_queries`，也没有审计表
- 不支持 `mv_infos()` / `jobs()` / `SHOW WORKLOAD GROUPS` / `COUNT(DISTINCT x)`
- 并行度变量是 `parallel_fragment_exec_instance_num`

不确定某条语法能不能用，先查 `doris_get_runtime_capabilities`（内置 19 项支持 / 6 项不支持）。

## 已知限制

- `doris_execute_query` 超出 `max_rows` 时**不读取剩余结果**，直接丢弃该连接（避免 `SELECT *` 把百万行拉进内存），
  代价是大结果查询多一次建连。
- 查询超时只中断本进程的等待，Doris 侧语句可能仍在跑；要彻底终止请在 Doris 侧 `KILL`。
- 只读守卫是"够用级"而非完备级：**生产环境请给 MCP 配一个只有 `SELECT_PRIV` 的 Doris 账号**。
- 连接层有三层兜底：复用前 `COM_PING`、传输层重试、诊断查询 15s 短超时。
  若出现大面积 `2013/10054`，先跑 `scripts/diagnose_connection.py` 判断是网络还是 Doris。

## 开发

```bash
uv sync                                                    # 含 dev 依赖（pytest / ruff）
uv run pytest                                              # 85 个用例，不需要数据库
uv run python scripts/probe.py                             # 探测实例方言，输出 OK/UNSUPPORTED
uv run python scripts/smoke.py --db <db> --table <tbl>      # MCP 客户端实测 17 个工具
uv run python scripts/diagnose_connection.py               # 连接稳定性诊断
```

`probe.py` 支持 `PROBE_DB` / `PROBE_TABLE` 追加表级检查。

## 说明

本项目是面向 Doris 1.2 的社区实现，与 Apache Doris 项目及官方 `doris-mcp-server` 无隶属关系。
只在 `doris-1.2.4-1-Unknown` 上做过完整验证，其他版本的行为未经测试。

## 许可证

[MIT](LICENSE)
