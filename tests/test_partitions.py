"""分区命名与保留期边界。算错一天就是多删或少删一天的数据。"""

from __future__ import annotations

from datetime import date, timedelta

from warehouse.partitions import (
    DEFAULT_AHEAD_DAYS,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_RETENTION_DAYS,
    ensure_partitions,
    partition_name,
)


def test_partition_name_is_zero_padded():
    assert partition_name(date(2026, 9, 7)) == "product_events_20260907"


def test_retention_window_is_longer_than_lookahead():
    """提前建的天数必须远小于保留期，否则会出现「刚建就够格被删」。"""

    assert DEFAULT_AHEAD_DAYS < DEFAULT_RETENTION_DAYS


def test_cutoff_keeps_exactly_retention_days():
    today = date(2026, 9, 7)
    cutoff = today - timedelta(days=DEFAULT_RETENTION_DAYS)
    # 边界：正好等于 cutoff 的那天要留下（`day >= cutoff` 才跳过删除）。
    assert cutoff == date(2026, 8, 8)


def test_backfill_covers_the_whole_retention_window():
    """回补天数必须盖满保留期。

    只建「今天及以后」时，首次部署拉到的第一批文件（接收日在昨天及更早）会直接撞上
    `no partition found`。边界取到 cutoff 当天为止：再往前的分区无论如何都会被
    `drop_expired_partitions` 删掉。
    """

    assert DEFAULT_BACKFILL_DAYS == DEFAULT_RETENTION_DAYS


def test_ensure_partitions_spans_past_and_future():
    today = date(2026, 9, 7)
    conn = _RecordingConn()

    created = ensure_partitions(conn, today, ahead_days=2, backfill_days=3)

    assert created[0] == partition_name(today - timedelta(days=3))
    assert created[-1] == partition_name(today + timedelta(days=2))
    assert partition_name(today - timedelta(days=1)) in created
    assert len(created) == 6


class _RecordingConn:
    """只记录 SQL 的假连接：分区窗口是纯算术，不必为它起一个真库。"""

    def __init__(self):
        self.statements = []

    def cursor(self):
        return _RecordingCursor(self.statements)

    def commit(self):
        return None


class _RecordingCursor:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._sink.append(sql)
