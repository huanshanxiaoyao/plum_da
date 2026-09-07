"""装载链路的集成测试。没有 PLUM_DA_TEST_DSN 时整体跳过。

这里只验三件**装载层自己的不变量**，不验业务口径（那是 W4 的事）：
  1. 迁移可重复执行；
  2. 同一个文件装两次不会变成两份数据；
  3. 内容与 manifest 不符时**拒绝装载**，而不是装进去再说。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ingest.filespec import iter_sealed_slices
from ingest.load_ods import IntegrityError, load_pending, load_slice
from migrations import SCHEMA, apply_migrations
from warehouse.partitions import ensure_partitions

DSN = os.environ.get("PLUM_DA_TEST_DSN", "").strip()
pytestmark = pytest.mark.skipif(not DSN, reason="需要 PLUM_DA_TEST_DSN 指向可写的测试库")


def _envelope(index: int, day: str = "2026-09-07") -> dict:
    return {
        "event_id": f"00000000-0000-4000-8000-{index:012d}",
        "event_name": "feed_served",
        "dict_version": "1",
        "client_time": 1757206800000 + index,
        "skew0_ms": 0,
        "session_id": "sess-1",
        "props": {"rank_version": "v3", "request_id": f"req-{index}"},
        "server_time": f"{day}T01:00:0{index % 10}+00:00",
        "clock_skew_ms": 0,
        "event_time_utc": f"{day}T01:00:00+00:00",
        "business_day": day,
        "time_fallback": None,
        "subject_kind": "visitor",
        "visitor_id": "v-1",
        "platform_user_id": None,
    }


def _write_slice(root: Path, index: int, count: int, *, corrupt_sha=False, wrong_lines=False):
    directory = root / "business_day=2026-09-07"
    directory.mkdir(parents=True, exist_ok=True)
    name = f"plum-2026-09-07T01-bbbbbb-{index:04d}.ndjson.gz"
    path = directory / name
    body = "".join(json.dumps(_envelope(i)) + "\n" for i in range(count)).encode("utf-8")
    with gzip.open(path, "wb") as handle:
        handle.write(body)
    (directory / f"{name}.manifest").write_text(
        json.dumps(
            {
                "line_count": count + (1 if wrong_lines else 0),
                "sha256": "0" * 64
                if corrupt_sha
                else hashlib.sha256(path.read_bytes()).hexdigest(),
                "first_ts": None,
                "last_ts": None,
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def conn():
    import psycopg

    connection = psycopg.connect(DSN)
    with connection.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    connection.commit()
    apply_migrations(connection)
    ensure_partitions(connection, date(2026, 9, 7))
    yield connection
    connection.close()


def _count(connection) -> int:
    with connection.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {SCHEMA}.product_events")
        return cur.fetchone()[0]


def test_migrations_are_idempotent(conn):
    assert apply_migrations(conn) == []


def test_loading_the_same_file_twice_does_not_duplicate(conn, tmp_path):
    _write_slice(tmp_path, 0, 5)
    slices = list(iter_sealed_slices(tmp_path))

    first = load_pending(conn, slices)
    assert first["loaded_files"] == 1 and _count(conn) == 5

    second = load_pending(conn, slices)
    assert second["skipped"] == 1 and second["loaded_files"] == 0
    assert _count(conn) == 5


def test_sha_mismatch_is_refused(conn, tmp_path):
    """传输损坏或文件被手改时必须拒装：装进去之后没人能发现它是坏的。"""

    _write_slice(tmp_path, 1, 3, corrupt_sha=True)
    slice_file = list(iter_sealed_slices(tmp_path))[0]
    with pytest.raises(IntegrityError):
        load_slice(conn, slice_file)
    conn.rollback()
    assert _count(conn) == 0


def test_line_count_mismatch_is_refused(conn, tmp_path):
    _write_slice(tmp_path, 2, 3, wrong_lines=True)
    slice_file = list(iter_sealed_slices(tmp_path))[0]
    with pytest.raises(IntegrityError):
        load_slice(conn, slice_file)
    conn.rollback()
    assert _count(conn) == 0


def test_bad_file_does_not_block_the_rest(conn, tmp_path):
    """一个坏文件不该挡住后面几十个好文件。"""

    _write_slice(tmp_path, 3, 2, corrupt_sha=True)
    _write_slice(tmp_path, 4, 4)
    summary = load_pending(conn, list(iter_sealed_slices(tmp_path)))
    assert summary["loaded_files"] == 1
    assert len(summary["failed"]) == 1
    assert _count(conn) == 4
