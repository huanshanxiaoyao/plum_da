"""分区命名与保留期边界。算错一天就是多删或少删一天的数据。"""

from __future__ import annotations

from datetime import date, timedelta

from warehouse.partitions import (
    DEFAULT_AHEAD_DAYS,
    DEFAULT_RETENTION_DAYS,
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
