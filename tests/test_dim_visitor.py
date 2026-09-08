"""访客维表：新访客口径由 ODS 推导，而不是数 `visitor_first_seen` 事件。

没有 PLUM_DA_TEST_DSN 时整体跳过（与 test_load_pg.py 一致）。

这里守的是**口径**本身，不是 SQL 语法：客户端铸 visitor_id 消灭了服务端的并发竞态，
但消灭不了重复铸造（两个 tab 同时冷启、cookie 被拦截后每次访问重铸）。数事件会被这些
重复直接污染，`min(server_time) group by visitor_id` 不会。
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest

from migrations import SCHEMA, apply_migrations
from warehouse.partitions import ensure_partitions

DSN = os.environ.get("PLUM_DA_TEST_DSN", "").strip()
pytestmark = pytest.mark.skipif(not DSN, reason="需要 PLUM_DA_TEST_DSN 指向可写的测试库")

_DAY = date(2026, 9, 7)
_BASE = datetime(2026, 9, 7, 1, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn():
    import psycopg

    connection = psycopg.connect(DSN)
    with connection.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    connection.commit()
    apply_migrations(connection)
    ensure_partitions(connection, _DAY)
    yield connection
    connection.close()


def _insert(connection, *rows) -> None:
    """按 (visitor_id, event_name, 相对基准的秒数) 插几条最小 ODS 记录。"""

    with connection.cursor() as cur:
        for index, (visitor_id, event_name, offset_seconds) in enumerate(rows):
            server_time = _BASE + timedelta(seconds=offset_seconds)
            cur.execute(
                f"""
                INSERT INTO {SCHEMA}.product_events
                  (event_id, event_name, dict_version, server_time, business_day,
                   subject_kind, visitor_id, source_file)
                VALUES (%s, %s, '1', %s, %s, %s, %s, 'test')
                """,
                (
                    f"00000000-0000-4000-8000-{index:012d}",
                    event_name,
                    server_time,
                    _DAY,
                    "visitor" if visitor_id else "member",
                    visitor_id,
                ),
            )
    connection.commit()


def _dim(connection) -> dict:
    with connection.cursor() as cur:
        cur.execute(
            f"SELECT visitor_id, first_seen, last_seen, first_business_day, event_count "
            f"FROM {SCHEMA}.dim_visitor ORDER BY visitor_id"
        )
        return {row[0]: row[1:] for row in cur.fetchall()}


def test_first_seen_comes_from_the_earliest_event_not_from_visitor_first_seen(conn):
    """首见时刻取该 visitor 的最早一条事件，与 `visitor_first_seen` 何时到达无关。

    真实链路里 `visitor_first_seen` 是页面初始化时异步发的，很可能**晚于**同一次首屏
    的 feed_served / age_gate_shown 落库。拿它当首见会系统性地把时刻推后。
    """

    _insert(
        conn,
        ("v-1", "feed_served", 0),
        ("v-1", "visitor_first_seen", 30),
        ("v-1", "message_sent", 90),
    )

    row = _dim(conn)["v-1"]
    assert row[0] == _BASE
    assert row[1] == _BASE + timedelta(seconds=90)
    assert row[2] == _DAY
    assert row[3] == 3


def test_duplicate_first_seen_events_do_not_inflate_the_visitor(conn):
    """同一个 visitor 收到两条 `visitor_first_seen` 也只算一个访客。

    这是整个视图存在的理由：事件计数会说"2 个新访客"，维表说"1 个"。
    """

    _insert(
        conn,
        ("v-dup", "visitor_first_seen", 0),
        ("v-dup", "visitor_first_seen", 5),
    )

    assert list(_dim(conn)) == ["v-dup"]


def test_events_without_a_visitor_are_excluded(conn):
    """纯会员事件（visitor_id 为空）不该凭空变成一个 NULL 访客。"""

    _insert(conn, (None, "message_sent", 0), ("v-2", "message_sent", 1))

    assert list(_dim(conn)) == ["v-2"]
