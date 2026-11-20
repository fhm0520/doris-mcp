"""连接稳定性诊断：区分"连接参数问题"与"建连/网络问题"。

用法：
  uv run python scripts/diagnose_connection.py                 # 读 .env / 环境变量
  uv run python scripts/diagnose_connection.py --host 10.0.0.5 --port 9030

判断依据（在 doris-1.2.4 上实测过）：
- 如果**复用连接连发多条查询**很稳，只有**新建连接**失败 → 不是连接参数问题，
  而是建连阶段被拒（FE accept 路径或网络中间设备 RST）。
- 如果在**本机 127.0.0.1** 上全绿、在**远端**上大量失败 → 问题在网络路径（防火墙/代理/负载均衡）。
- 如果连 `SELECT 1` 都慢（>1s）或 `SHOW PROC '/frontends'` 挂住 → FE 自身状态问题。

脚本只做 SELECT 1 与 COM_PING，不写任何数据。
"""

from __future__ import annotations

import argparse
import socket
import time

import pymysql

from doris_mcp.config import Settings


def connect(host: str, port: int, user: str, password: str, read_timeout: int = 10):
    return pymysql.connect(
        host=host, port=port, user=user, password=password, charset="utf8mb4",
        connect_timeout=5, read_timeout=read_timeout, autocommit=True,
    )


def phase_handshake(host, port, n=20, gap=0.1) -> tuple[int, int, list[str]]:
    ok = 0
    errors: list[str] = []
    for _ in range(n):
        s = socket.socket()
        s.settimeout(4)
        try:
            s.connect((host, port))
            if s.recv(64):
                ok += 1
            else:
                errors.append("EOF(握手空包)")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:40]}")
        finally:
            s.close()
        time.sleep(gap)
    return ok, n, errors


def phase_fresh(host, port, user, password, n=20) -> tuple[int, int, list[str]]:
    ok = 0
    errors: list[str] = []
    for _ in range(n):
        try:
            c = connect(host, port, user, password)
            cur = c.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            c.close()
            ok += 1
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:40]}")
    return ok, n, errors


def phase_reuse(host, port, user, password, n=30) -> tuple[int, int, list[str], float]:
    """单连接连发 n 条，测"已建立的连接是否稳定"。"""
    ok = 0
    errors: list[str] = []
    started = time.perf_counter()
    try:
        c = connect(host, port, user, password)
        cur = c.cursor()
        for _ in range(n):
            cur.execute("SELECT 1")
            cur.fetchone()
            ok += 1
        c.close()
    except Exception as exc:
        errors.append(f"第 {ok + 1} 条断: {type(exc).__name__}: {str(exc)[:40]}")
    return ok, n, errors, round(time.perf_counter() - started, 2)


def phase_idle(host, port, user, password, idle=0.5, rounds=8) -> tuple[int, int, list[str]]:
    """建连 → 空闲 idle 秒 → 查询，测空闲连接是否被关。"""
    ok = 0
    errors: list[str] = []
    for _ in range(rounds):
        try:
            c = connect(host, port, user, password)
            time.sleep(idle)
            cur = c.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            c.close()
            ok += 1
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:40]}")
    return ok, rounds, errors


def show(label: str, ok: int, total: int, errors: list[str], extra: str = "") -> None:
    rate = f"{ok}/{total}"
    mark = "OK" if ok == total else ("PARTIAL" if ok else "FAIL")
    print(f"  {label:30} {rate:>7}  {mark:8} {extra}")
    for err in errors[:2]:
        print(f"      · {err}")


def main() -> int:
    parser = argparse.ArgumentParser()
    settings = Settings()
    parser.add_argument("--host", default=settings.doris_host)
    parser.add_argument("--port", type=int, default=settings.doris_port)
    parser.add_argument("--user", default=settings.doris_user)
    parser.add_argument("--password", default=settings.doris_password)
    args = parser.parse_args()

    print(f"目标 {args.user}@{args.host}:{args.port}\n")

    ok, total, errs = phase_handshake(args.host, args.port)
    show("TCP 握手（间隔 0.1s）", ok, total, errs)

    ok2, total2, errs2 = phase_fresh(args.host, args.port, args.user, args.password)
    show("新建连接 + SELECT 1", ok2, total2, errs2)

    ok3, total3, errs3, elapsed = phase_reuse(args.host, args.port, args.user, args.password)
    show("同一连接连发 30 条", ok3, total3, errs3, f"{elapsed}s")

    ok4, total4, errs4 = phase_idle(args.host, args.port, args.user, args.password)
    show("建连后空闲 0.5s 再查", ok4, total4, errs4)

    print("\n结论：")
    if ok3 == total3 and ok2 < total2:
        print("  · 复用稳定、新建失败 → 不是连接参数问题，是**建连阶段**被拒。")
        print("    请在 FE 本机对 127.0.0.1 再跑一次本脚本：")
        print("    - 本机全绿、远端失败 → 网络路径（防火墙 / 代理 / 负载均衡 / 安全软件）")
        print("    - 本机也失败     → FE 的 MySQL accept 路径或进程状态，查 fe.log")
    elif ok2 == total2 and ok4 < total4:
        print("  · 新建稳定、空闲后失效 → 中间设备或 FE 关闭了空闲连接，缩短池的复用间隔即可。")
    elif ok2 == total2 and ok4 == total4:
        print("  · 各项正常，连接层没有明显问题。")
    else:
        print("  · 连接普遍不稳，先看 FE 进程与网络（fe.log、netstat -s 的 resets）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
