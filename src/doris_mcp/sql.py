"""SQL 工具：只读守卫、标识符/字面量转义。

不引入 sqlparse —— 这里只需要"判断首关键字 + 数分号"，自己扫一遍更可控。

本服务与官方 doris-mcp-server 一致：**纯只读**，不提供任何写语句通道，
也没有放开写的开关。守卫是常开的。
"""

from __future__ import annotations

import re

READ_ONLY_KEYWORDS = frozenset({"SELECT", "WITH", "SHOW", "DESC", "DESCRIBE", "EXPLAIN", "HELP"})

# 出现在"结构骨架"里即拒绝的片段（注释与字符串已被剥离，不会误伤字面量）
BLOCKED_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bINTO\s+OUTFILE\b", "禁止 INTO OUTFILE（会写 FE/BE 本地文件）"),
    (r"\bINTO\s+DUMPFILE\b", "禁止 INTO DUMPFILE"),
    (r"\bLOAD\s+DATA\b", "禁止 LOAD DATA"),
    (r"\bKILL\b", "禁止 KILL，请到 Doris 侧处理"),
    (r"\bSET\s+GLOBAL\b", "禁止 SET GLOBAL"),
    (r"^\s*ADMIN\b", "禁止 ADMIN 语句"),
)

_ESCAPES = {
    "\\": "\\\\",
    "'": "\\'",
    '"': '\\"',
    "\0": "\\0",
    "\n": "\\n",
    "\r": "\\r",
    "\x1a": "\\Z",
}


class SqlSafetyError(ValueError):
    """输入不合法，调用方应转成错误信封而不是抛给 MCP。"""


def skeleton(sql: str) -> str:
    """剥离注释与字符串内容，只保留 SQL 的结构骨架。

    'a;b'  -> '';  -- x  -> 空;  1 /* y */ 2 -> 1  2
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"`":
            quote = ch
            out.append(quote * 2)
            i += 1
            while i < n:
                cur = sql[i]
                if cur == "\\" and quote != "`":
                    i += 2
                    continue
                if cur == quote:
                    if i + 1 < n and sql[i + 1] == quote:  # 双写转义
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if sql.startswith("--", i) or ch == "#":
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            out.append(" ")
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def check_read_only(sql: str) -> str | None:
    """返回 None 表示放行，否则返回给模型看的中文原因。"""
    if not sql or not sql.strip():
        return "SQL 为空"

    body = skeleton(sql).strip()
    if body.endswith(";"):
        body = body[:-1].rstrip()
    if ";" in body:
        return "不支持一次执行多条语句（检测到中间分号），请拆分后分别调用"

    match = re.match(r"[A-Za-z_]+", body)
    keyword = match.group(0).upper() if match else ""
    if keyword not in READ_ONLY_KEYWORDS:
        return (
            f"只读模式只允许 {'/'.join(sorted(READ_ONLY_KEYWORDS))}，收到 `{keyword or '空'}`。"
            "本服务不提供任何写语句通道，改表结构/注释请在 Doris 侧操作"
        )

    flat = " ".join(body.split()).upper()
    for pattern, message in BLOCKED_PATTERNS:
        if re.search(pattern, flat):
            return message
    return None


def quote_ident(name: str) -> str:
    """反引号标识符，反引号本身双写转义。"""
    if not name or "\0" in name:
        raise SqlSafetyError("标识符为空或包含非法字符")
    return "`" + name.replace("`", "``") + "`"


def quote_str(value: str) -> str:
    """MySQL 风格字符串字面量。"""
    return "'" + "".join(_ESCAPES.get(ch, ch) for ch in value) + "'"


def qualify(database: str, table: str) -> str:
    return f"{quote_ident(database)}.{quote_ident(table)}"
