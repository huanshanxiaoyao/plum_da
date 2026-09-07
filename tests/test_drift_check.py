"""字典漂移检查。它自己坏掉时的表现是「永远报无漂移」——必须有测试盯着。"""

from __future__ import annotations

import copy

import pytest

from contracts.drift_check import _events, diff

BASE = {
    "version": 1,
    "events": [
        {
            "name": "feed_served",
            "status": "active",
            "props": {
                "view": {"type": "enum", "required": True, "values": ["for_you", "trending"]},
                "latency_ms": {"type": "int", "required": True},
            },
        }
    ],
}


def test_events_are_indexed_from_a_list_not_a_mapping():
    """真源的 events 是列表。按映射读会拿到空字典，让检查静默失效。"""

    assert set(_events(BASE)) == {"feed_served"}
    with pytest.raises(ValueError):
        _events({"events": {"feed_served": {}}})


def test_identical_dicts_have_no_drift():
    assert diff(BASE, copy.deepcopy(BASE)) == []


def test_detects_breaking_changes():
    upstream = copy.deepcopy(BASE)
    upstream["version"] = 2
    upstream["events"][0]["status"] = "deprecated"
    upstream["events"][0]["props"]["view"]["values"].remove("trending")
    upstream["events"][0]["props"]["latency_ms"]["required"] = False
    del upstream["events"][0]["props"]["latency_ms"]["type"]

    notes = "\n".join(diff(BASE, upstream))
    assert "version: 钉住 1 → 上游 2" in notes
    assert "status active → deprecated" in notes
    assert "删除 enum 取值 trending" in notes
    assert "required True → False" in notes


def test_detects_removed_event():
    upstream = copy.deepcopy(BASE)
    upstream["events"] = []
    assert any("已删除事件" in note for note in diff(BASE, upstream))
