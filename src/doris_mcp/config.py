"""配置：环境变量 + .env，CLI 参数在 main.py 里覆盖。"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """字段名小写后与同名大写环境变量对应，例如 doris_host <- DORIS_HOST。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- 连接 ---
    doris_host: str = "127.0.0.1"
    doris_port: int = 9030
    doris_user: str = "root"
    doris_password: str = ""
    doris_database: str = ""

    doris_connect_timeout: int = Field(10, ge=1, le=120)
    doris_query_timeout: int = Field(60, ge=1, le=3600)
    doris_pool_size: int = Field(4, ge=1, le=32)
    # 传输层抖动重试次数（0 = 不重试）
    doris_retries: int = Field(2, ge=0, le=5)

    # --- 结果边界 ---
    doris_max_rows: int = Field(200, ge=1, le=10000)
    doris_max_result_bytes: int = Field(1_000_000, ge=10_000)

    # 安全策略固定为只读，与官方 doris-mcp-server 一致（55 个子工具全只读），
    # 不提供任何放开写能力的开关：DORIS_READ_ONLY / DORIS_ALLOW_COMMENT_EDIT 都已移除。

    # --- MCP 传输 ---
    mcp_transport: str = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 3000
    mcp_path: str = "/mcp"

    log_level: str = "INFO"

    @property
    def endpoint(self) -> str:
        """用于日志与错误提示，不含密码。"""
        db = f"/{self.doris_database}" if self.doris_database else ""
        return f"{self.doris_user}@{self.doris_host}:{self.doris_port}{db}"
