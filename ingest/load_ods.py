"""把一个已封口的切片文件装进 ODS。

幂等性在**文件粒度**上保证（见 ledger）：删旧行 + 装新行 + 登记台账在同一个事务里，
中途崩溃整体回滚，重跑就是重来一次，不会留下半个文件的数据。
"""

from __future__ import annotations

import gzip
import json
import logging
from typing import Any, Dict, List, Tuple

from migrations import SCHEMA

from . import ledger
from .filespec import LANE_DEAD, ContractError, SliceFile, read_manifest, sha256_of
from .rows import DEAD_COLUMNS, EVENT_COLUMNS, RowError, dead_row, event_row

logger = logging.getLogger("plum_da.load")


class IntegrityError(RuntimeError):
    """文件内容与 manifest 不符：传输损坏，或上游文件被改过。"""


def _parse_lines(slice_file: SliceFile) -> Tuple[List[List[Any]], List[str], int]:
    """解析整个切片，返回 (行, 坏行原因, 总行数)。

    坏行**不阻断**整个文件：一行 JSON 解析失败就丢掉整个切片，等于让一个字节的损坏
    带走一小时的数据。计数上报出来，人来判断要不要追。
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

    table = "product_events_dead" if slice_file.lane == LANE_DEAD else "product_events"
    columns = DEAD_COLUMNS if slice_file.lane == LANE_DEAD else EVENT_COLUMNS

    with conn.cursor() as cur:
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

    if bad:
        logger.warning("%s: %d 行无法解析，已跳过：%s", slice_file.key, len(bad), bad[:5])

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
        except (IntegrityError, ContractError, OSError) as exc:
            conn.rollback()
            logger.error("装载失败 %s: %s", slice_file.key, exc)
            summary["failed"].append({"file_key": slice_file.key, "error": str(exc)})
            continue
        summary["loaded_files"] += 1
        summary["loaded_rows"] += result["loaded"]
    return summary
