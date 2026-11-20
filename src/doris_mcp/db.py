"""连接池、执行、结果封装。

驱动用 PyMySQL + asyncio.to_thread：Doris 1.2 只跑 MySQL 文本协议，
纯同步栈最稳，也最容易排查空闲断连问题。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import queue
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from typing import Any

import pymysql
from pymysql.constants import FIELD_TYPE
from pymysql.cursors import SSCursor

from .config import Settings

logger = logging.getLogger(__name__)

MAX_CELL_CHARS = 8192

_FIELD_NAMES = {v: k for k, v in vars(FIELD_TYPE).items() if isinstance(v, int) and not k.startswith("_")}

_FATAL_CODES = {0, 1040, 1053, 2003, 2006, 2013, 2055}

# 实测这台 1.2.4 实例会随机掐断连接（SELECT 1 也有约 10% 概率收到 2013/10054），
# 所以 2013/2006/0 这类传输层错误必须重试，否则工具会随机失败。
_RETRYABLE_CODES = {0, 2006, 2013}

_HINTS: dict[int, str] = {
    1045: "账号或密码错误，检查 DORIS_USER / DORIS_PASSWORD",
    1049: "数据库不存在，检查 database 参数或 DORIS_DATABASE",
    1054: "字段不存在，先用 doris_describe_table 确认列名",
    1105: "Doris 内部错误，请结合 message 原文判断（常见于分区、副本、内存、权限）",
    1146: "表不存在，先用 doris_list_tables 确认表名",
    1203: "连接数超过 Doris 上限，调小 DORIS_POOL_SIZE 或清理空闲连接",
    2003: "连不上 Doris FE，检查 DORIS_HOST / DORIS_PORT 与网络",
    2013: "连接中断（可能超时），该连接已丢弃并重建",
}

_DORIS_12_HINT = (
    "Doris 1.2 常见 2.0+ 语法不兼容：SHOW BACKENDS / SHOW FRONTENDS 会挂住，BE 信息请用 "
    "SELECT * FROM information_schema.backends；没有 information_schema.active_queries、"
    "__internal_schema.audit_log、mv_infos()、SHOW WORKLOAD GROUPS。"
)


def dumps(payload: dict[str, Any]) -> str:
    """紧凑 JSON，省 token。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _type_name(code: Any) -> str:
    return _FIELD_NAMES.get(code, str(code))


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value) > MAX_CELL_CHARS:
            return value[:MAX_CELL_CHARS] + f"…(+{len(value) - MAX_CELL_CHARS} chars)"
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # NaN / ±Inf 不是合法 JSON，降级成字符串，避免序列化炸掉整条结果
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, dt_time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(value).hex()
    return str(value)


def ok_envelope(
    *,
    sql: str,
    columns: list[dict[str, str]],
    rows: list[list[Any]],
    elapsed_ms: int,
    truncated: bool = False,
    database: str | None = None,
    affected_rows: int | None = None,
    notices: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True, "sql": sql}
    if database:
        payload["database"] = database
    payload["columns"] = columns
    payload["rows"] = rows
    payload["row_count"] = len(rows)
    payload["truncated"] = truncated
    payload["elapsed_ms"] = elapsed_ms
    if affected_rows is not None:
        payload["affected_rows"] = affected_rows
    if notices:
        payload["notices"] = notices
    return payload


def error_envelope(
    sql: str,
    *,
    type_: str,
    message: str,
    code: int | None = None,
    hint: str | None = None,
    database: str | None = None,
    target: str | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    error: dict[str, Any] = {"type": type_}
    if code is not None:
        error["code"] = code
    error["message"] = message
    if hint:
        error["hint"] = hint
    if retryable:
        error["retryable"] = True
    payload: dict[str, Any] = {"ok": False, "sql": sql}
    if database:
        payload["database"] = database
    if target:
        payload["target"] = target
    payload["error"] = error
    return payload


def _trim_rows(raw_rows: list[Any], budget: int) -> tuple[list[list[Any]], bool]:
    rows: list[list[Any]] = []
    used = 0
    for raw in raw_rows:
        row = [_to_jsonable(value) for value in raw]
        used += sum(len(v) for v in row if isinstance(v, str)) + 8 * len(row)
        if used > budget:
            return rows, True
        rows.append(row)
    return rows, False


def _is_fatal(exc: pymysql.MySQLError) -> bool:
    code = exc.args[0] if exc.args else None
    return code in _FATAL_CODES


class DorisPool:
    """懒创建的小连接池。所有 DB 调用都在线程里跑，事件循环不阻塞。"""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._idle: queue.LifoQueue = queue.LifoQueue(maxsize=settings.doris_pool_size)
        self._version: str | None = None
        self._version_checked = False
        self._version_lock = threading.Lock()

    @property
    def server_version(self) -> str | None:
        return self._version

    # --- 连接管理 ---
    def _connect(self) -> pymysql.connections.Connection:
        return pymysql.connect(
            host=self._s.doris_host,
            port=self._s.doris_port,
            user=self._s.doris_user,
            password=self._s.doris_password,
            database=self._s.doris_database or None,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=self._s.doris_connect_timeout,
            read_timeout=self._s.doris_query_timeout + 5,
            write_timeout=self._s.doris_query_timeout + 5,
            cursorclass=SSCursor,
        )

    def _acquire(self) -> tuple[pymysql.connections.Connection, str]:
        try:
            conn, _last_used, current_db = self._idle.get_nowait()
        except queue.Empty:
            return self._connect(), self._s.doris_database

        # 每次复用前都做一次 COM_PING。实测这台实例的空闲连接可能在几百毫秒内
        # 就被对端关掉，只靠"空闲超过 N 秒才 ping"会拿到死连接。
        try:
            conn.ping(reconnect=False)
        except Exception:
            # ping 任何异常都说明连接已不可用，直接换一条
            self._close_quietly(conn)
            return self._connect(), self._s.doris_database
        return conn, current_db

    def _release(self, conn: pymysql.connections.Connection, current_db: str, *, broken: bool) -> None:
        if broken:
            self._close_quietly(conn)
            return
        try:
            self._idle.put_nowait((conn, time.time(), current_db))
        except queue.Full:
            self._close_quietly(conn)

    @staticmethod
    def _close_quietly(conn: pymysql.connections.Connection) -> None:
        with contextlib.suppress(Exception):
            conn.close()

    def close_all(self) -> None:
        while True:
            try:
                conn, _, _ = self._idle.get_nowait()
            except queue.Empty:
                return
            self._close_quietly(conn)

    # --- 对外执行 ---
    async def execute(
        self,
        sql: str,
        *,
        database: str | None = None,
        max_rows: int | None = None,
        clamp: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """clamp=False 用于内部元数据查询（表结构可能远超用户可见行数上限）。

        timeout 显式传入时（内部诊断查询）走短超时，且超时视为可重试：
        本实例的"超时"多数是连接假死而非真慢查询。

        传输层抖动会自动重试：只有 error.retryable 为真时才重试，语法/权限错误立即返回。
        """
        requested = max_rows or self._s.doris_max_rows
        rows_cap = min(requested, self._s.doris_max_rows) if clamp else requested
        attempts = self._s.doris_retries + 1

        payload: dict[str, Any] = {}
        for attempt in range(attempts):
            payload = await self._execute_once(sql, database, rows_cap, timeout)
            if payload.get("ok"):
                return payload
            if not payload.get("error", {}).get("retryable"):
                return payload
            if attempt + 1 < attempts:
                logger.info("retry %s/%s after transient error: %s", attempt + 1, attempts - 1, sql[:80])
                await asyncio.sleep(0.15 * (attempt + 1))
        return payload

    async def _execute_once(
        self, sql: str, database: str | None, rows_cap: int, timeout: float | None = None
    ) -> dict[str, Any]:
        effective = timeout if timeout is not None else self._s.doris_query_timeout
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._execute_sync, sql, database, rows_cap),
                timeout=effective,
            )
        except TimeoutError:
            logger.warning("query timeout after %ss: %s", effective, sql[:200])
            return error_envelope(
                sql,
                type_="timeout",
                message=f"查询超过 {effective}s 未返回",
                hint="调大 DORIS_QUERY_TIMEOUT，或缩小查询范围 / 加上更严格的过滤条件",
                database=database,
                target=self._s.endpoint,
                # 内部短超时多因连接假死，可重试；用户查询保持原样，避免重复等待
                retryable=timeout is not None,
            )

    async def server_version_text(self) -> str | None:
        if self._version_checked:
            return self._version
        payload = await self.execute("SELECT @@version_comment", max_rows=1)
        if payload.get("ok") and payload.get("rows"):
            with self._version_lock:
                self._version = str(payload["rows"][0][0])
                self._version_checked = True
        return self._version

    # --- 线程内实现 ---
    def _execute_sync(self, sql: str, database: str | None, max_rows: int) -> dict[str, Any]:
        conn = None
        cursor = None
        current_db = ""
        broken = False
        started = time.perf_counter()
        try:
            conn, current_db = self._acquire()
            target_db = database or self._s.doris_database
            if target_db and target_db != current_db:
                conn.select_db(target_db)
                current_db = target_db
            cursor = conn.cursor()
            cursor.execute(sql)
            elapsed = int((time.perf_counter() - started) * 1000)

            if cursor.description:
                columns = [{"name": d[0], "type": _type_name(d[1])} for d in cursor.description]
                fetched = cursor.fetchmany(max_rows + 1)
                truncated = len(fetched) > max_rows
                if truncated:
                    # 没读完的结果集不消费（SSCursor 会一路读到 EOF），直接丢连接
                    fetched = fetched[:max_rows]
                    broken = True
                else:
                    cursor.close()
                    cursor = None
                rows, cut_by_bytes = _trim_rows(fetched, self._s.doris_max_result_bytes)
                return ok_envelope(
                    sql=sql,
                    columns=columns,
                    rows=rows,
                    elapsed_ms=elapsed,
                    truncated=truncated or cut_by_bytes,
                    database=database,
                )

            affected = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else None
            return ok_envelope(
                sql=sql,
                columns=[],
                rows=[],
                elapsed_ms=elapsed,
                database=database,
                affected_rows=affected,
            )
        except pymysql.MySQLError as exc:
            broken = _is_fatal(exc)
            return self._map_db_error(exc, sql, database)
        except Exception as exc:
            broken = True
            logger.exception("unexpected db error")
            return error_envelope(
                sql,
                type_="internal",
                message=f"{type(exc).__name__}: {exc}",
                database=database,
                target=self._s.endpoint,
            )
        finally:
            if cursor is not None and not broken:
                with contextlib.suppress(Exception):
                    cursor.close()
            if conn is not None:
                self._release(conn, current_db, broken=broken)

    def _map_db_error(
        self, exc: pymysql.MySQLError, sql: str, database: str | None
    ) -> dict[str, Any]:
        code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
        message = str(exc.args[1]) if len(exc.args) > 1 else str(exc)

        if code == 1064:
            hint = _DORIS_12_HINT
            if self._version:
                hint = f"当前连接 {self._version}。" + hint
        elif code is not None:
            hint = _HINTS.get(code)
            if hint is None and self._version:
                hint = f"当前连接 {self._version}"
        else:
            hint = None

        logger.warning("doris error code=%s sql=%s msg=%s", code, sql[:200], message)
        return error_envelope(
            sql,
            type_="sql_error",
            code=code,
            message=message,
            hint=hint,
            database=database,
            target=self._s.endpoint,
            retryable=code in _RETRYABLE_CODES,
        )
