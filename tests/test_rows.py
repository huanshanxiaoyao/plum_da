"""信封 → ODS 行的映射。列错位不会报错，只会把值写进相邻的列。"""

from __future__ import annotations

import json

import pytest

from ingest.rows import DEAD_COLUMNS, EVENT_COLUMNS, RowError, dead_row, event_row

ENVELOPE = {
    "event_id": "38379ba5-a891-409f-8ddf-dbcdd8e39ee4",
    "event_name": "feed_served",
    "dict_version": "1",
    "client_time": 1757206800000,
    "skew0_ms": 12,
    "session_id": "sess-1",
    "props": {"rank_version": "v3", "item_count": 20},
    "server_time": "2026-09-07T01:00:00+00:00",
    "clock_skew_ms": -5,
    "event_time_utc": "2026-09-07T01:00:00+00:00",
    "business_day": "2026-09-07",
    "time_fallback": None,
    "subject_kind": "visitor",
    "visitor_id": "v-1",
    "platform_user_id": None,
}


def test_column_count_matches_row_length():
    assert len(event_row(ENVELOPE, "k")) == len(EVENT_COLUMNS)


def test_values_land_on_the_right_columns():
    row = dict(zip(EVENT_COLUMNS, event_row(ENVELOPE, "data/2026-09-07/f.ndjson.gz")))
    assert row["event_name"] == "feed_served"
    assert row["visitor_id"] == "v-1"
    assert row["platform_user_id"] is None
    assert row["business_day"] == "2026-09-07"
    assert row["source_file"] == "data/2026-09-07/f.ndjson.gz"
    # client_time 是 epoch 毫秒，原样保留不转成时间戳：转型会把「客户端说的时间」
    # 和「我们相信的时间」混成一列，之后再也分不开。
    assert row["client_time"] == 1757206800000
    assert json.loads(row["props"])["rank_version"] == "v3"


@pytest.mark.parametrize("missing", ["event_id", "event_name", "server_time", "business_day"])
def test_missing_anchor_fields_are_rejected(missing):
    """只有分区键和回溯键较真：没有它们这行既无处安放也无从追查。"""

    payload = dict(ENVELOPE)
    payload.pop(missing)
    with pytest.raises(RowError):
        event_row(payload, "k")


def test_unknown_props_pass_through_untouched():
    """ODS 是原始落地层：字典里没有的字段照收，不在这里做业务校验。"""

    payload = dict(ENVELOPE, props={"brand_new_field": 1})
    row = dict(zip(EVENT_COLUMNS, event_row(payload, "k")))
    assert json.loads(row["props"]) == {"brand_new_field": 1}


def test_non_dict_props_degrade_to_empty_object():
    row = dict(zip(EVENT_COLUMNS, event_row(dict(ENVELOPE, props="oops"), "k")))
    assert row["props"] == "{}"


def test_dead_row_truncates_detail_and_keeps_no_payload():
    """死信是唯一没脱敏的路径：只留哈希与字段路径，绝不整包透传。"""

    record = {
        "reason": "unknown_event",
        "server_time": "2026-09-07T01:00:00+00:00",
        "business_day": "2026-09-07",
        "payload_sha256": "abc",
        "detail": ["props.x:type_error"] * 80 + ["y" * 500],
    }
    row = dict(zip(DEAD_COLUMNS, dead_row(record, "k")))
    assert len(row["detail"]) == 50
    assert all(len(item) <= 200 for item in row["detail"])
    assert "payload" not in row
