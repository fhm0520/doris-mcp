"""Doris 1.2 能力矩阵。

每条都在 `doris-1.2.4-1-Unknown` 上实测过（`scripts/probe.py` 可复现）。
矩阵是静态的，`probe=True` 时额外跑几条廉价语句做实时抽查。

注意区分两类"不可用"：
- 语法/对象不存在 → 明确不支持；
- 语句挂死（如 SHOW FRONTENDS）→ 实测 100s 无响应，也不能用。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    area: str
    feature: str
    supported: bool
    syntax: str
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "area": self.area,
            "feature": self.feature,
            "supported": self.supported,
            "syntax": self.syntax,
        }
        if self.note:
            payload["note"] = self.note
        return payload


CAPABILITIES: tuple[Capability, ...] = (
    # --- 元数据 ---
    Capability("元数据", "列注释", True, "SHOW FULL COLUMNS FROM db.t", "Type 里含聚合类型，Extra 里也有"),
    Capability("元数据", "表注释", True, "information_schema.tables.TABLE_COMMENT", "或 SHOW TABLE STATUS"),
    Capability("元数据", "分区", True, "SHOW PARTITIONS FROM db.t", "17 列，含 VisibleVersionTime / DataSize"),
    Capability("元数据", "分区（系统表）", True, "information_schema.partitions", "存在，但缺 PARTITION_KEY/RANGE/BUCKETS/STATE"),
    Capability("元数据", "表大小", True, "SHOW DATA", "必须先选库；SHOW DATA FROM db 报 unknown table"),
    Capability("元数据", "catalog", True, "SHOW CATALOGS", "4 列"),
    Capability("元数据", "跨 catalog 浏览", True, "SHOW DATABASES FROM c / SHOW TABLES FROM c.db", ""),
    Capability("元数据", "UDF 清单", True, "SHOW FUNCTIONS", "SHOW GLOBAL FUNCTIONS 是语法错误"),
    # --- 查询 ---
    Capability("查询", "只读 SQL", True, "SELECT / SHOW / DESC / EXPLAIN / WITH", "沙箱常开"),
    Capability("查询", "执行计划", True, "EXPLAIN / EXPLAIN VERBOSE / EXPLAIN GRAPH", "GRAPH 输出 ASCII 图"),
    Capability("查询", "Profile", True, 'SHOW QUERY PROFILE "/query_id"', "需要真实 query id"),
    Capability("查询", "当前查询", True, "SHOW PROC '/current_queries'", "7 列"),
    Capability("查询", "慢查询", False, "—", "SHOW PROC '/slow_queries' 不存在；无审计表"),
    Capability("查询", "审计日志", False, "—", "doris_audit_db__.doris_audit_tbl__ 与 __internal_schema.audit_log 都不存在"),
    Capability("查询", "ADBC / Arrow Flight", False, "—", "2.1+"),
    # --- 集群 ---
    Capability("集群", "BE 清单", True, "information_schema.backends", "23 列，列名是 IP；SHOW BACKENDS 25 列、SHOW PROC '/backends' 26 列"),
    Capability("集群", "FE 清单", True, "SHOW FRONTENDS", "17 列；SHOW PROC '/frontends' 慢（十几秒），优先用 SHOW FRONTENDS"),
    Capability("集群", "库表统计", True, "SHOW PROC '/statistic'", "7 列"),
    Capability("集群", "库容量", True, "SHOW PROC '/dbs'", "Size/Quota 是带单位字符串"),
    Capability("集群", "compaction（间接）", True, "information_schema.rowsets", "13 列；CREATION_TIME 是 unix 秒"),
    Capability("集群", "内存 / file cache / workload group / compute group", False, "—", "2.0+"),
    # --- 导入 ---
    Capability("导入", "导入任务", True, "SHOW LOAD / SHOW ROUTINE LOAD", "17 / 19 列"),
    Capability("导入", "数据新鲜度", True, "SHOW PARTITIONS 的 VisibleVersionTime", ""),
    Capability("导入", "异步物化视图", False, "—", "mv_infos() / jobs() 函数不存在"),
    # --- 检索 ---
    Capability("检索", "倒排索引 / ANN / BM25 / Variant", False, "—", "2.0+ / 4.x"),
)

_LIVE_CHECKS: tuple[tuple[str, str], ...] = (
    ("catalogs", "SHOW CATALOGS"),
    ("functions", "SHOW FUNCTIONS"),
    ("load", "SHOW LOAD"),
    ("backends", "SELECT BackendId, IP, Alive FROM information_schema.backends LIMIT 1"),
)

# 已知会挂死的语句：早期实测过 SHOW FRONTENDS / SHOW PROC '/frontends' 挂 100s，
# 复测发现是当时网络/IP 冲突导致的，环境恢复后二者都能用（SHOW FRONTENDS 0.3s）。
# 这里保留清单，仅用于提醒"别放进 live probe"（慢语句会拖长探测）。
BLOCKED_STATEMENTS = ("SHOW PROC '/frontends'",)


def profile_for(version: str | None) -> str:
    """只区分 1.2 / 其他，够用即可。"""
    if version and "1.2" in version:
        return "1.2"
    return "unknown"


def capabilities_for(version: str | None) -> list[dict[str, object]]:
    profile = profile_for(version)
    if profile == "1.2":
        return [c.to_dict() for c in CAPABILITIES]
    # 非 1.2 实例：不谎报，返回空矩阵并让调用方看 version
    return []


def live_checks() -> tuple[tuple[str, str], ...]:
    return _LIVE_CHECKS
