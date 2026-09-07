"""分析库连接。凭据只从环境变量来，仓库里不留任何连接串。"""

from __future__ import annotations

import os
from typing import Any

DSN_ENV = "PLUM_DA_DATABASE_URL"


def dsn() -> str:
    """读取分析库 DSN；缺失时直接失败，不猜默认值。

    猜一个 localhost 默认值的代价是：配错环境时脚本会「成功」地写进一个空库，
    而不是当场报错。
    """

    value = os.environ.get(DSN_ENV, "").strip()
    if not value:
        raise RuntimeError(f"{DSN_ENV} 未设置：分析库连接串必须由环境提供")
    return value


def connect(dsn_override: str | None = None) -> Any:
    """打开一个分析库连接（psycopg3）。调用方负责关闭。"""

    import psycopg  # 延迟导入：纯逻辑测试不需要驱动在场

    return psycopg.connect(dsn_override or dsn())
