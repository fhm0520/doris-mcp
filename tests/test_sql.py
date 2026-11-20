import pytest

from doris_mcp.sql import (
    SqlSafetyError,
    check_read_only,
    qualify,
    quote_ident,
    quote_str,
    skeleton,
)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select * from t",
        "SELECT * FROM t WHERE a = 'x;y'",
        "SELECT 1;",
        "SHOW PROC '/backends'",
        "SHOW FULL COLUMNS FROM db.tbl",
        "DESC db.tbl",
        "EXPLAIN SELECT 1",
        "WITH a AS (SELECT 1) SELECT * FROM a",
        "/* hint */ SELECT 1",
        "SELECT 1 -- 注释里的 ; 不算",
        "SELECT 1 # 井号注释",
        "SELECT `weird;name` FROM t",
    ],
)
def test_read_only_accepts(sql):
    assert check_read_only(sql) is None


@pytest.mark.parametrize(
    "sql,keyword",
    [
        ("ALTER TABLE t SET ('comment'='x')", "ALTER"),
        ("UPDATE t SET a = 1", "UPDATE"),
        ("DELETE FROM t", "DELETE"),
        ("DROP TABLE t", "DROP"),
        ("SET GLOBAL query_timeout = 1", "SET"),
        ("INSERT INTO t VALUES (1)", "INSERT"),
        ("TRUNCATE TABLE t", "TRUNCATE"),
        ("", "空"),
        ("   ", "空"),
    ],
)
def test_read_only_rejects_statements(sql, keyword):
    reason = check_read_only(sql)
    assert reason is not None
    assert keyword in reason or keyword == "空"


def test_read_only_rejects_multi_statement():
    reason = check_read_only("SELECT 1; DROP TABLE t")
    assert reason is not None
    assert "多条语句" in reason


def test_read_only_rejects_outfile():
    reason = check_read_only("SELECT * FROM t INTO OUTFILE '/tmp/x'")
    assert reason is not None
    assert "OUTFILE" in reason


def test_read_only_ignores_string_content():
    assert check_read_only("SELECT 'DROP TABLE t'") is None
    assert check_read_only("SELECT 'a;b'") is None


def test_skeleton_strips_literals_and_comments():
    assert skeleton("SELECT 'a;b' -- x") .strip() == "SELECT ''"
    assert "DROP" not in skeleton("SELECT 1 /* DROP TABLE t */")


def test_quote_ident_escapes_backtick():
    assert quote_ident("a`b") == "`a``b`"
    with pytest.raises(SqlSafetyError):
        quote_ident("")


def test_quote_str_escapes():
    assert quote_str("O'Reilly") == "'O\\'Reilly'"
    assert quote_str("a\\b") == "'a\\\\b'"
    assert quote_str("新注释") == "'新注释'"


def test_qualify():
    assert qualify("cloud", "orders") == "`cloud`.`orders`"


def test_rejection_message_has_no_write_tool_escape_hatch():
    # 只读是硬策略：错误提示里不能引导模型去找某个写工具或开关
    reason = check_read_only("ALTER TABLE t MODIFY COMMENT 'x'")
    assert reason is not None
    assert "set_table_comment" not in reason
    assert "DORIS_" not in reason
