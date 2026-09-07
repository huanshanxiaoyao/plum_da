"""信封 JSON → ODS 行的纯函数映射。不碰数据库，可离线测试。

列顺序在这里定义一次，COPY 和 INSERT 都引用它，避免两处手抄错位——
那种错位不会报错，只会把 visitor_id 写进 platform_user_id。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

# 与 migrations 001 的列定义严格同序。
EVENT_COLUMNS: Tuple[str, ...] = (
    "event_id",
    "event_name",
    "dict_version",
    "client_time",
    "skew0_ms",
    "session_id",
    "props",
    "server_time",
    "clock_skew_ms",
    "event_time_utc",
    "business_day",
    "time_fallback",
    "subject_kind",
    "visitor_id",
    "platform_user_id",
    "source_file",
)

DEAD_COLUMNS: Tuple[str, ...] = (
    "reason",
    "event_id",
    "event_name",
    "dict_version",
    "session_id",
    "client_time",
    "server_time",
    "business_day",
    "payload_sha256",
    "detail",
    "source_file",
)


class RowError(ValueError):
    """一行无法映射成 ODS 行（缺必需字段/类型不对）。"""


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def event_row(envelope: Dict[str, Any], source_file: str) -> List[Any]:
    """把一条行为事件信封映射成 ODS 行。

    只对**分区键和主键性质的字段**较真：`server_time` / `business_day` / `event_id`
    缺失或不可解析时抛错（没有它们这行既无处安放也无从回溯）；其余字段一律尽力而为，
    ODS 是原始落地层，不在这里做业务校验。
    """

    for field in ("event_id", "event_name", "server_time", "business_day"):
        if not _text(envelope.get(field)):
            raise RowError(f"缺少必需字段 {field}")

    props = envelope.get("props")
    if not isinstance(props, dict):
        props = {}

    return [
        _text(envelope.get("event_id")),
        _text(envelope.get("event_name")),
        _text(envelope.get("dict_version")) or "",
        _int(envelope.get("client_time")),
        _int(envelope.get("skew0_ms")),
        _text(envelope.get("session_id")),
        json.dumps(props, ensure_ascii=False, separators=(",", ":")),
        _text(envelope.get("server_time")),
        _int(envelope.get("clock_skew_ms")),
        _text(envelope.get("event_time_utc")),
        _text(envelope.get("business_day")),
        _text(envelope.get("time_fallback")),
        _text(envelope.get("subject_kind")),
        _text(envelope.get("visitor_id")),
        _text(envelope.get("platform_user_id")),
        source_file,
    ]


def dead_row(record: Dict[str, Any], source_file: str) -> List[Any]:
    """把一条死信记录映射成死信行。

    死信本身就是「没通过校验的东西」，这里比主通道更宽松：只要求 `reason` 和
    `server_time` 在——前者是死信存在的理由，后者是它落在哪天。
    """

    for field in ("reason", "server_time", "business_day"):
        if not _text(record.get(field)):
            raise RowError(f"死信缺少必需字段 {field}")

    detail = record.get("detail")
    if not isinstance(detail, list):
        detail = []
    # detail 里只该有「字段路径:错误类型」，不该有值。这里再截一次长度，
    # 防止上游哪天不小心把值拼进去，直接把原始内容带进仓。
    detail_text = [str(item)[:200] for item in detail][:50]

    return [
        _text(record.get("reason")),
        _text(record.get("event_id")),
        _text(record.get("event_name")),
        _text(record.get("dict_version")),
        _text(record.get("session_id")),
        _int(record.get("client_time")),
        _text(record.get("server_time")),
        _text(record.get("business_day")),
        _text(record.get("payload_sha256")),
        detail_text,
        source_file,
    ]
