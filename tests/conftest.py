"""测试隔离：不读取开发者本机的 .env 与 DORIS_* 环境变量。

配置类 `Settings` 默认会读当前目录的 .env，本机跑过 smoke 后很容易带着
DORIS_DATABASE 之类的值，导致测试结果依赖环境。这里统一清掉并切到临时目录。
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.lower().startswith(("doris_", "mcp_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
