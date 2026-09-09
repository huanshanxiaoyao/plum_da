"""把一个已封口的切片文件装进 ODS。

幂等性在**文件粒度**上保证（见 ledger）：删旧行 + 装新行 + 登记台账在同一个事务里，
中途崩溃整体回滚，重跑就是重来一次，不会留下半个文件的数据。
"""

from __future__ import annotations

import gzip
import json
import logging
from typing import Any, Dict, List, Tuple

import psycopg

from migrations import SCHEMA

from . import ledger
from .filespec import LANE_DEAD, ContractError, SliceFile, read_manifest, sha256_of
from .rows import DEAD_COLUMNS, EVENT_COLUMNS, RowError, dead_row, event_row

logger = logging.getLogger("plum_da.load")


class IntegrityError(RuntimeError):
    """文件内容与 manifest 不符：传输损坏，或上游文件被改过。"""


def _parse_lines(slice_file: SliceFile) -> Tuple[List[List[Any]], List[str], int]:
    """解析整个切片，返回 (行, 坏行原因, 总行数)。

    收集坏行原因，由调用方拒绝整个文件，保留后续修复重试的机会。
    """

    rows: List[List[Any]] = []
    bad: List[str] = []
    total = 0
    builder = dead_row if slice_file.lane == LANE_DEAD else event_row

    with gzip.open(slice_file.path, "rt", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            total += 1
            try:
                payload: Dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError as exc:
                bad.append(f"json:{exc.msg}")
                continue
            if not isinstance(payload, dict):
                bad.append("json:not-an-object")
                continue
            try:
                rows.append(builder(payload, slice_file.key))
            except RowError as exc:
                bad.append(str(exc))
    return rows, bad, total


def load_slice(conn: Any, slice_file: SliceFile, *, verify_sha: bool = True) -> Dict[str, Any]:
    """校验并装载一个切片。返回本次装载摘要。

    `verify_sha` 只在明确知道文件刚被 sha 校验过时才关闭——它是发现「传输损坏」和
    「文件被手改」的唯一手段，默认必须开。
    """

    manifest = read_manifest(slice_file.manifest_path)

    if verify_sha:
        actual = sha256_of(slice_file.path)
        if actual != manifest.sha256:
            raise IntegrityError(
                f"{slice_file.key}: sha256 不符 manifest={manifest.sha256} actual={actual}"
            )

    rows, bad, total = _parse_lines(slice_file)
    if total != manifest.line_count:
        raise IntegrityError(
            f"{slice_file.key}: 行数不符 manifest={manifest.line_count} actual={total}"
        )

    if bad:
        raise IntegrityError(f"{slice_file.key}: {len(bad)} 行无法解析，拒绝登记台账：{bad[:5]}")

    table = "product_events_dead" if slice_file.lane == LANE_DEAD else "product_events"
    columns = DEAD_COLUMNS if slice_file.lane == LANE_DEAD else EVENT_COLUMNS

    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (5852007,))
        cur.execute(f"SELECT sha256 FROM {SCHEMA}.projected_files WHERE file_key = %s",
                    (slice_file.key,))
        projected = cur.fetchone()
        if projected and projected[0] != manifest.sha256:
            raise IntegrityError(f"{slice_file.key}: cannot replace an already projected source")
        # 重放路径：先按 source_file 清掉旧行。正常首装这里删 0 行。
        cur.execute(f"DELETE FROM {SCHEMA}.{table} WHERE source_file = %s", (slice_file.key,))
        if rows:
            if slice_file.lane == LANE_DEAD:
                # 死信量级是主通道的千分之一，用 executemany 就够，
                # 还省掉 TEXT[] 在 COPY 文本格式下的数组转义坑。
                placeholders = ", ".join(["%s"] * len(columns))
                cur.executemany(
                    f"INSERT INTO {SCHEMA}.{table} ({', '.join(columns)}) VALUES ({placeholders})",
                    rows,
                )
            else:
                with cur.copy(f"COPY {SCHEMA}.{table} ({', '.join(columns)}) FROM STDIN") as copy:
                    for row in rows:
                        copy.write_row(row)
        ledger.record(conn, slice_file, manifest, rows_loaded=len(rows))
    conn.commit()

    return {
        "file_key": slice_file.key,
        "lane": slice_file.lane,
        "lines": total,
        "loaded": len(rows),
        "bad": len(bad),
    }


def load_pending(conn: Any, slices: List[SliceFile]) -> Dict[str, Any]:
    """装载所有尚未登记的切片。已装过的直接跳过。

    单个文件失败不终止整批：一个坏文件不该挡住后面 23 小时的数据。
    """

    done = ledger.loaded_keys(conn)
    summary = {"skipped": 0, "loaded_files": 0, "loaded_rows": 0, "failed": []}

    for slice_file in slices:
        if slice_file.key in done:
            summary["skipped"] += 1
            continue
        try:
            result = load_slice(conn, slice_file)
        except (IntegrityError, ContractError, OSError, psycopg.Error) as exc:
            # `psycopg.Error` 必须在列内：数据库侧的拒绝（类型放不下、值不合法、
            # 缺分区）都是 `DataError` / `UndefinedTable` 这一支，**不是**
            # `IntegrityError`。漏掉它，异常就会穿出本函数，上面那句「单个文件失败
            # 不终止整批」名存实亡——一个坏文件不仅自己装不进去，还会带走排在它
            # 后面的所有正常文件，而且连接停在失败事务里。
            conn.rollback()
            logger.error("装载失败 %s: %s", slice_file.key, exc)
            summary["failed"].append({"file_key": slice_file.key, "error": str(exc)})
            continue
        summary["loaded_files"] += 1
        summary["loaded_rows"] += result["loaded"]
    return summary
