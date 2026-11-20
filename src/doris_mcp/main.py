"""CLI 入口：stdio（默认）或 streamable-http。"""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import Settings
from .server import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doris-mcp",
        description="Apache Doris 1.2.x 只读 MCP Server（查询 SQL / 库表元数据）",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--transport", choices=["stdio", "http"], default=None, help="默认 stdio")
    parser.add_argument("--host", default=None, help="HTTP 监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="HTTP 监听端口，默认 3000")

    parser.add_argument("--doris-host", default=None, help="Doris FE 地址")
    parser.add_argument("--doris-port", type=int, default=None, help="Doris FE MySQL 端口，默认 9030")
    parser.add_argument("--doris-user", default=None)
    parser.add_argument("--doris-password", default=None)
    parser.add_argument("--doris-database", default=None, help="默认库")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=None)
    return parser


def apply_args(settings: Settings, args: argparse.Namespace) -> Settings:
    """CLI 覆盖环境变量；未提供的参数不动。"""
    if args.transport:
        settings.mcp_transport = "streamable-http" if args.transport == "http" else args.transport
    if args.host:
        settings.mcp_host = args.host
    if args.port is not None:
        settings.mcp_port = args.port
    if args.doris_host:
        settings.doris_host = args.doris_host
    if args.doris_port is not None:
        settings.doris_port = args.doris_port
    if args.doris_user:
        settings.doris_user = args.doris_user
    if args.doris_password is not None:
        settings.doris_password = args.doris_password
    if args.doris_database is not None:
        settings.doris_database = args.doris_database
    if args.log_level:
        settings.log_level = args.log_level
    return settings


def main_sync() -> None:
    args = build_parser().parse_args()
    settings = apply_args(Settings(), args)

    # stdio 模式下 stdout 只允许 MCP 协议帧，日志必须走 stderr
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("doris_mcp")
    logger.info("doris-mcp %s -> %s (transport=%s)", __version__, settings.endpoint, settings.mcp_transport)

    mcp, pool = create_app(settings)
    try:
        if settings.mcp_transport == "stdio":
            mcp.run(transport="stdio")
        else:
            mcp.run(
                transport="streamable-http",
                host=settings.mcp_host,
                port=settings.mcp_port,
                streamable_http_path=settings.mcp_path,
            )
    except KeyboardInterrupt:
        logger.info("interrupted")
    finally:
        pool.close_all()


if __name__ == "__main__":
    main_sync()
