"""Export aggregate-only snapshots and atomically publish a self-contained report."""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from psycopg.rows import dict_row

VIEWS = {"visitors": "report_visitors", "feed": "report_feed", "clicks": "report_clicks",
         "messages": "report_messages"}


def snapshot(conn):
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cur.execute("SET LOCAL TIME ZONE 'UTC'")
            cur.execute("SELECT * FROM analytics.report_status")
            status = cur.fetchone()
            if not status:
                raise ValueError("analytics model has not been refreshed")
            result = {"status": status, "generated_at": datetime.now(timezone.utc).isoformat()}
            for key, view in VIEWS.items():
                cur.execute(f"SELECT * FROM analytics.{view} ORDER BY business_day")
                result[key] = cur.fetchall()
    return result


def render(data):
    # JSON is script data, but HTML parsing still recognizes a literal closing script tag.
    payload = json.dumps(data, ensure_ascii=True, default=str, allow_nan=False).replace("<", "\\u003c")
    return Path(__file__).with_name("dashboard.html").read_text().replace("__REPORT_DATA__", payload)


def publish(data, destination: Path):
    content = render(data)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=".report-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def demo():
    """Synthetic, explicitly labelled browser-review fixture; never a production fallback."""
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    now = datetime.now(timezone.utc).isoformat()
    data = {"demo": True, "generated_at": now, "status": {"refreshed_at": now,
        "ingest_watermark": now, "event_watermark": now, "coverage_start": str(today - timedelta(days=13)),
        "attribution_seconds": 604800, "projected_files": 42}, "visitors": [], "feed": [],
        "clicks": [], "messages": []}
    for n in range(14):
        day = str(today - timedelta(days=13 - n))
        data["visitors"].append(dict(business_day=day, visitors=40 + n * 7,
            new_visitors=15 + n * 2, profile_views=25 + n * 4, messages=80 + n * 6))
        for k, name in enumerate(["Mira", "Rowan", "Luna"]):
            data["feed"].append(dict(business_day=day, character_id=name, surface="for_you",
                rank_version="baseline-v1", exp_bucket="control", impressions=100 + n * 17 + k * 8,
                clicked_impressions=9 + n + k))
            data["clicks"].append(dict(business_day=day, character_id=name, surface="for_you",
                clicks=14 + n + k, unmatched_clicks=2))
            data["messages"].append(dict(business_day=day, character_id=name,
                messages=20 + n + k))
    return data
