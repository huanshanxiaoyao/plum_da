"""文件台账（设计文档 §8.3 L1）：回答「这个文件装过没有」。

去重不靠 ODS 上的唯一约束——分区表的唯一约束必须包含分区键，`event_id` 单列做不到。
所以幂等性放在**文件粒度**：台账登记与数据装载在同一个事务里，
要么这个文件整个进去了，要么一行都没进去。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set

from migrations import SCHEMA

from .filespec import Manifest, SliceFile


def loaded_keys(conn: Any) -> Set[str]:
    """已装载文件的 key 集合。文件数量在千级，一次读全比逐个查快得多。"""

    with conn.cursor() as cur:
        cur.execute(f"SELECT file_key FROM {SCHEMA}.ingest_ledger")
        return {row[0] for row in cur.fetchall()}


def lookup(conn: Any, file_key: str) -> Optional[Dict[str, Any]]:
    """按 key 取一条台账，没有则 None。"""

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT file_key, sha256, line_count, rows_loaded
            FROM {SCHEMA}.ingest_ledger WHERE file_key = %s
            """,
            (file_key,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"file_key": row[0], "sha256": row[1], "line_count": row[2], "rows_loaded": row[3]}


def record(conn: Any, slice_file: SliceFile, manifest: Manifest, rows_loaded: int) -> None:
    """登记一次装载。**不提交**——由调用方与数据装载放在同一事务里提交。

    `ON CONFLICT DO UPDATE` 而不是 DO NOTHING：走到这里说明调用方已经决定重放这个文件
    （先按 source_file 删了旧行），台账要跟着更新，否则 sha256 会停在旧值上。
    """

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {SCHEMA}.ingest_ledger
              (file_key, lane, business_day, writer_id, slice_index,
               sha256, line_count, first_ts, last_ts, closed_at, rows_loaded)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (file_key) DO UPDATE SET
              sha256 = EXCLUDED.sha256,
              line_count = EXCLUDED.line_count,
              rows_loaded = EXCLUDED.rows_loaded,
              loaded_at = now()
            """,
            (
                slice_file.key,
                slice_file.lane,
                slice_file.business_day.isoformat(),
                slice_file.writer_id,
                slice_file.index,
                manifest.sha256,
                manifest.line_count,
                manifest.first_ts,
                manifest.last_ts,
                manifest.closed_at,
                rows_loaded,
            ),
        )
