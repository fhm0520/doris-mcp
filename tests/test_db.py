import json
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar

import pymysql

from doris_mcp.config import Settings
from doris_mcp.db import (
    DorisPool,
    _to_jsonable,
    _trim_rows,
    dumps,
    error_envelope,
    ok_envelope,
)


def test_dumps_is_compact():
    text = dumps({"ok": True, "rows": [[1, 2]]})
    assert text == '{"ok":true,"rows":[[1,2]]}'
    assert json.loads(text)["rows"] == [[1, 2]]


def test_to_jsonable_keeps_decimal_as_string():
    assert _to_jsonable(Decimal("12.30")) == "12.30"
    assert _to_jsonable(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02 03:04:05"
    assert _to_jsonable(date(2026, 1, 2)) == "2026-01-02"
    assert _to_jsonable(b"abc") == "abc"
    assert _to_jsonable(None) is None
    assert _to_jsonable(True) is True


def test_to_jsonable_truncates_long_cell():
    out = _to_jsonable("x" * 9000)
    assert len(out) < 9000
    assert "chars)" in out


def test_ok_envelope_shape():
    payload = ok_envelope(
        sql="SHOW DATABASES",
        columns=[{"name": "Database", "type": "VAR_STRING"}],
        rows=[["cloud"]],
        elapsed_ms=3,
    )
    assert payload["ok"] is True
    assert payload["row_count"] == 1
    assert payload["truncated"] is False
    assert "database" not in payload


def test_error_envelope_shape():
    payload = error_envelope("SELECT x", type_="sql_error", code=1064, message="syntax", hint="h")
    assert payload["ok"] is False
    assert payload["error"]["code"] == 1064
    assert payload["error"]["hint"] == "h"


def test_trim_rows_stops_at_budget():
    rows, truncated = _trim_rows([["a" * 100], ["b" * 100]], budget=150)
    assert truncated is True
    assert len(rows) == 1


def test_map_db_error_syntax_hint_mentions_version():
    pool = DorisPool(Settings())
    pool._version = "Doris version doris-1.2.4-1-Unknown"
    exc = pymysql.err.ProgrammingError(1064, "Syntax error near 'BACKENDS'")
    payload = pool._map_db_error(exc, "SHOW BACKENDS", None)
    assert payload["ok"] is False
    assert payload["error"]["code"] == 1064
    assert "1.2.4" in payload["error"]["hint"]
    assert "information_schema.backends" in payload["error"]["hint"]


def test_transient_error_is_marked_retryable():
    pool = DorisPool(Settings())
    exc = pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query")
    payload = pool._map_db_error(exc, "SELECT 1", None)
    assert payload["error"]["retryable"] is True


def test_syntax_error_is_not_retryable():
    pool = DorisPool(Settings())
    exc = pymysql.err.ProgrammingError(1064, "Syntax error")
    payload = pool._map_db_error(exc, "SELECT", None)
    assert "retryable" not in payload["error"]


def test_map_db_error_access_denied():
    pool = DorisPool(Settings())
    exc = pymysql.err.OperationalError(1045, "Access denied")
    payload = pool._map_db_error(exc, "SELECT 1", None)
    assert "DORIS_USER" in payload["error"]["hint"]


def test_settings_endpoint_hides_password():
    s = Settings(doris_host="h", doris_port=9030, doris_user="u", doris_password="secret", doris_database="db")
    assert s.endpoint == "u@h:9030/db"
    assert "secret" not in s.endpoint


class FakeCursor:
    description: ClassVar[list[tuple[str, int]]] = [("a", 3), ("b", 253)]

    def __init__(self, rows):
        self._rows = rows
        self.closed = False
        self.rowcount = 0

    def execute(self, sql):
        self.sql = sql

    def fetchmany(self, size):
        return self._rows[:size]

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self, rows, ping_ok=True):
        self.rows = rows
        self.closed = False
        self.selected = ""
        self.ping_ok = ping_ok
        self.pinged = 0
        self.cursor_obj = FakeCursor(rows)

    def select_db(self, db):
        self.selected = db

    def cursor(self):
        return self.cursor_obj

    def ping(self, reconnect=False):
        self.pinged += 1
        if not self.ping_ok:
            raise pymysql.err.OperationalError(2006, "MySQL server has gone away")
        return True

    def close(self):
        self.closed = True


def _pool_with(conn):
    pool = DorisPool(Settings(doris_database="cloud"))
    pool._idle.put_nowait((conn, 1e18, ""))
    return pool


def test_execute_switches_database_once():
    conn = FakeConn([[1, "x"]])
    pool = _pool_with(conn)
    payload = pool._execute_sync("SELECT 1", "other", 10)
    assert payload["ok"] is True
    assert conn.selected == "other"
    assert conn.closed is False


def test_execute_truncated_discards_connection():
    conn = FakeConn([[1, "x"], [2, "y"], [3, "z"]])
    pool = _pool_with(conn)
    payload = pool._execute_sync("SELECT * FROM t", None, 2)
    assert payload["truncated"] is True
    assert payload["row_count"] == 2
    # 没读完的结果集直接丢连接，避免把整表拉进内存
    assert conn.closed is True
    assert conn.cursor_obj.closed is False


def test_execute_keeps_connection_when_not_truncated():
    conn = FakeConn([[1, "x"]])
    pool = _pool_with(conn)
    payload = pool._execute_sync("SELECT 1", None, 10)
    assert payload["truncated"] is False
    assert conn.closed is False
    assert conn.cursor_obj.closed is True
    assert pool._idle.qsize() == 1


def test_reused_connection_is_pinged_before_use():
    conn = FakeConn([[1, "x"]])
    pool = _pool_with(conn)
    pool._execute_sync("SELECT 1", None, 10)
    # 复用前必须做一次 COM_PING：这台实例的空闲连接可能随时被对端关掉
    assert conn.pinged == 1


def test_dead_connection_is_replaced(monkeypatch):
    dead = FakeConn([[1, "x"]], ping_ok=False)
    fresh = FakeConn([[2, "y"]])
    pool = _pool_with(dead)
    monkeypatch.setattr(pool, "_connect", lambda: fresh)

    payload = pool._execute_sync("SELECT 1", None, 10)

    assert payload["ok"] is True
    assert dead.pinged == 1
    assert dead.closed is True          # 死连接被关掉
    assert payload["rows"] == [[2, "y"]]  # 用的是新连接
    assert pool._idle.qsize() == 1


async def test_execute_clamps_rows_unless_internal(monkeypatch):
    pool = DorisPool(Settings(doris_max_rows=10))
    captured: list[int] = []

    def fake_sync(sql, database, max_rows):
        captured.append(max_rows)
        return {"ok": True, "sql": sql, "columns": [], "rows": [], "row_count": 0,
                "truncated": False, "elapsed_ms": 0}

    monkeypatch.setattr(pool, "_execute_sync", fake_sync)

    await pool.execute("SELECT 1", max_rows=1000)
    await pool.execute("SELECT 1", max_rows=1000, clamp=False)

    assert captured == [10, 1000]


async def test_internal_timeout_is_retryable_and_retried(monkeypatch):
    """诊断查询用短超时：超时视为可重试（本实例的超时多是连接假死）。"""
    import time as _time

    pool = DorisPool(Settings(doris_retries=1))
    attempts: list[int] = []

    def slow_sync(sql, database, max_rows):
        attempts.append(1)
        _time.sleep(0.15)
        return {"ok": True, "sql": sql, "columns": [], "rows": [], "row_count": 0,
                "truncated": False, "elapsed_ms": 150}

    monkeypatch.setattr(pool, "_execute_sync", slow_sync)

    payload = await pool.execute("SELECT 1", timeout=0.02)
    assert payload["ok"] is False
    assert payload["error"]["type"] == "timeout"
    assert payload["error"]["retryable"] is True
    # 1 次 + 重试 1 次
    assert len(attempts) == 2


async def test_user_query_timeout_is_not_retryable(monkeypatch):
    """用户查询保持原行为：超时不重试，避免把等待时间翻倍。"""
    pool = DorisPool(Settings(doris_retries=2, doris_query_timeout=1))
    calls: list[int] = []

    def slow_sync(sql, database, max_rows):
        calls.append(1)
        return {"ok": True, "sql": sql, "columns": [], "rows": [], "row_count": 0,
                "truncated": False, "elapsed_ms": 1}

    def block(sql, database, max_rows):
        calls.append(1)
        import time as _time
        _time.sleep(1.5)
        return slow_sync(sql, database, max_rows)

    monkeypatch.setattr(pool, "_execute_sync", block)

    payload = await pool.execute("SELECT 1")
    assert payload["error"]["type"] == "timeout"
    assert "retryable" not in payload["error"]
    assert len(calls) == 1
