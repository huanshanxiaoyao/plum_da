"""ODS 按天分区的维护：提前建、过期删。

为什么分区键是 `server_time`（接收日）而不是 `business_day`（业务日）：
分区要**单调**才能安全 DROP。business_day 由客户端时间推导，迟到事件会往回落，
用它当分区键意味着「昨天的分区」永远可能再长出新行，DROP 就不再安全。
接收日只增不减，DROP 才是可判定的。

代价是：整日重算的过滤条件（business_day）和分区键不重合，扫不到分区裁剪。
用 `product_events_business_day_event_name_idx` 补回来（见 migrations 002）。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, List

from migrations import SCHEMA

logger = logging.getLogger("plum_da.partitions")

PARENT = f"{SCHEMA}.product_events"

# 提前建 7 天：留出足够的失败重试窗口。缺分区意味着**装载直接失败**
# （PG 对无匹配分区的插入报错），所以宁可多建，空分区几乎不占空间。
DEFAULT_AHEAD_DAYS = 7
# 保留 30 天原始事件。ODS 是可重建的落地层，长期口径靠 DWD/DWS，
# 但重建的前提是机器 A 的文件还在——保留期不得长于机器 A 的文件保留期。
DEFAULT_RETENTION_DAYS = 30

_PARTITION_PREFIX = "product_events_"


def partition_name(day: date) -> str:
    """某天分区的表名（不含 schema）。"""

    return f"{_PARTITION_PREFIX}{day:%Y%m%d}"


def ensure_partitions(conn: Any, today: date, ahead_days: int = DEFAULT_AHEAD_DAYS) -> List[str]:
    """幂等建出 [today, today+ahead_days] 的日分区，返回本次新建的表名。"""

    created: List[str] = []
    for offset in range(ahead_days + 1):
        day = today + timedelta(days=offset)
        name = partition_name(day)
        # 分区边界属于 DDL 语法，PG 不接受参数绑定（`could not determine data type
        # of parameter`），只能内联字面量。这里的值全部来自内部构造的 `date`，
        # 经 isoformat 后必然是 YYYY-MM-DD，不存在外部输入路径。
        lower = day.isoformat()
        upper = (day + timedelta(days=1)).isoformat()
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {SCHEMA}.{name} "
                f"PARTITION OF {PARENT} "
                f"FOR VALUES FROM ('{lower}') TO ('{upper}')"
            )
        created.append(name)
    conn.commit()
    return created


def drop_expired_partitions(
    conn: Any, today: date, retention_days: int = DEFAULT_RETENTION_DAYS
) -> List[str]:
    """DROP 掉早于保留期的分区，返回被删的表名。

    只删**确实是本表分区**的表：用 pg_inherits 反查而不是按表名前缀猜，
    免得哪天有人建了个同前缀的临时表被顺手删掉。
    """

    cutoff = today - timedelta(days=retention_days)
    dropped: List[str] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            JOIN pg_namespace n ON n.oid = p.relnamespace
            WHERE n.nspname = %s AND p.relname = 'product_events'
            ORDER BY c.relname
            """,
            (SCHEMA,),
        )
        names = [row[0] for row in cur.fetchall()]

    for name in names:
        suffix = name[len(_PARTITION_PREFIX) :]
        if not name.startswith(_PARTITION_PREFIX) or len(suffix) != 8 or not suffix.isdigit():
            continue
        try:
            day = date(int(suffix[0:4]), int(suffix[4:6]), int(suffix[6:8]))
        except ValueError:
            continue
        if day >= cutoff:
            continue
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.{name}")
        dropped.append(name)
        logger.info("dropped expired partition %s", name)
    conn.commit()
    return dropped
