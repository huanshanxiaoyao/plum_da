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
from warehouse.project import LOCK_ID

logger = logging.getLogger("plum_da.partitions")

PARENT = f"{SCHEMA}.product_events"

# 提前建 7 天：留出足够的失败重试窗口。缺分区意味着**装载直接失败**
# （PG 对无匹配分区的插入报错），所以宁可多建，空分区几乎不占空间。
DEFAULT_AHEAD_DAYS = 7
# 同时向**过去**建满整个保留期。只建「今天及以后」是错的：装载的是机器 A 已经落好的
# 文件，接收日天然落在过去——首次部署拉到的第一批就是昨天及更早的，会直接报
# `no partition found for relation`；停机时间超过预建窗口后再启动也是同一个死法。
# 建到保留期边界为止，与 `drop_expired_partitions` 的 cutoff 对齐：早于它的数据
# 无论如何都会被删，没有必要为它留分区。
DEFAULT_BACKFILL_DAYS = DEFAULT_RETENTION_DAYS = 30
# 保留 30 天原始事件（`DEFAULT_RETENTION_DAYS`，定义在上面与回补天数同一行）。
# ODS 是可重建的落地层，长期口径靠 DWD/DWS，但重建的前提是机器 A 的文件还在——
# 保留期不得长于机器 A 的文件保留期。

_PARTITION_PREFIX = "product_events_"


def partition_name(day: date) -> str:
    """某天分区的表名（不含 schema）。"""

    return f"{_PARTITION_PREFIX}{day:%Y%m%d}"


def ensure_partitions(
    conn: Any,
    today: date,
    ahead_days: int = DEFAULT_AHEAD_DAYS,
    backfill_days: int = DEFAULT_BACKFILL_DAYS,
) -> List[str]:
    """幂等建出 [today-backfill_days, today+ahead_days] 的日分区，返回涉及的表名。

    往回也要建：装载的是机器 A 已落盘的文件，接收日必然在过去（见 `DEFAULT_BACKFILL_DAYS`）。
    """

    created: List[str] = []
    for offset in range(-backfill_days, ahead_days + 1):
        day = today + timedelta(days=offset)
        name = partition_name(day)
        # 分区边界属于 DDL 语法，PG 不接受参数绑定（`could not determine data type
        # of parameter`），只能内联字面量。这里的值全部来自内部构造的 `date`，
        # 经 isoformat 后必然是 YYYY-MM-DD，不存在外部输入路径。
        lower = day.isoformat() + " 00:00:00+00"
        upper = (day + timedelta(days=1)).isoformat() + " 00:00:00+00"
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
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
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
            cur.execute(f"""
                SELECT e.source_file FROM {SCHEMA}.{name} e
                LEFT JOIN {SCHEMA}.projected_files p ON p.file_key = e.source_file
                LEFT JOIN {SCHEMA}.ingest_ledger l ON l.file_key = e.source_file
                WHERE (p.file_key IS NULL OR l.file_key IS NULL
                   OR p.sha256 <> l.sha256 OR p.rows_loaded <> l.rows_loaded)
                  AND (l.business_day IS NULL OR l.business_day >= coalesce(
                    (SELECT start_day FROM analytics.projection_settings), '-infinity'::date))
                LIMIT 1
            """)
            if cur.fetchone():
                conn.rollback()
                raise RuntimeError(f"refusing to expire unprojected source data in {name}")
            cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.{name}")
        dropped.append(name)
        logger.info("dropped expired partition %s", name)
    conn.commit()
    return dropped
