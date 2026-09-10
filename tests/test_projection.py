import os
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from migrations import apply_migrations
from warehouse.partitions import drop_expired_partitions, ensure_partitions
from warehouse.project import ProjectionError, project

DSN = os.environ.get("PLUM_DA_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="requires isolated PLUM_DA_TEST_DSN")
DAY = date(2026, 9, 7)
TIME = datetime(2026, 9, 7, 23, 0, tzinfo=timezone.utc)
WINDOW = 7 * 86400


@pytest.fixture
def conn():
    import psycopg
    with psycopg.connect(DSN) as connection:
        connection.execute("DROP SCHEMA IF EXISTS analytics CASCADE")
        connection.commit()
        apply_migrations(connection)
        ensure_partitions(connection, DAY)
        yield connection


def event(name="feed_impressed", *, props=None, seconds=0, visitor="browser-private", event_id=None):
    if props is None:
        props = {"request_id": "req", "surface": "for_you"}
        if name == "feed_impressed":
            props.update(rank_version="baseline-v1", exp_bucket="control", items=[
                {"character_id": "character-a", "position": 0, "dwell_ms": 600}])
        else:
            props.update(character_id="character-a", position=0)
    return (event_id or uuid4(), name, visitor, TIME + timedelta(seconds=seconds), props)


def source(conn, *events, file_day=DAY):
    key = str(uuid4())
    with conn.cursor() as cur:
        for event_id, name, visitor, when, props in events:
            cur.execute("""INSERT INTO analytics.product_events
              (event_id, event_name, visitor_id, server_time, event_time_utc, business_day,
               props, dict_version, source_file, client_time)
              VALUES (%s, %s, %s, %s, %s, %s, %s, '1', %s, %s)""",
              (event_id, name, visitor, when, when, when.date(), Jsonb(props), key,
               int(when.timestamp() * 1000)))
        cur.execute("""INSERT INTO analytics.ingest_ledger
          (file_key, lane, business_day, writer_id, slice_index, sha256, line_count, rows_loaded)
          VALUES (%s, 'data', %s, 'test', 0, %s, %s, %s)""",
          (key, file_day, key, len(events), len(events)))
    conn.commit()
    return key


def scalar(conn, sql):
    value = conn.execute(sql).fetchone()[0]
    conn.commit()
    return value


def refresh(conn):
    return project(conn, WINDOW, start_day=DAY)


def test_replay_and_repeated_impressions_and_clicks(conn):
    impression = event()
    click = event("character_card_clicked", seconds=10)
    source(conn, impression, click)
    source(conn, impression, click, event(seconds=5), event("character_card_clicked", seconds=20))
    refresh(conn)
    refresh(conn)
    assert scalar(conn, "SELECT count(*) FROM analytics.event_registry") == 4
    assert scalar(conn, "SELECT sum(impressions) FROM analytics.daily_feed") == 1
    assert scalar(conn, "SELECT sum(clicked_impressions) FROM analytics.daily_feed") == 1
    assert scalar(conn, "SELECT sum(clicks) FROM analytics.daily_clicks") == 2
    assert scalar(conn, "SELECT sum(new_visitors) FROM analytics.daily_visitors") == 1


def test_click_before_impression_arrives_and_cross_day_attribution(conn):
    source(conn, event("character_card_clicked", seconds=7200))
    refresh(conn)
    assert scalar(conn, "SELECT sum(unmatched_clicks) FROM analytics.daily_clicks") == 1
    source(conn, event())
    refresh(conn)
    assert scalar(conn, "SELECT sum(unmatched_clicks) FROM analytics.daily_clicks") == 0
    assert scalar(conn, "SELECT business_day FROM analytics.daily_feed") == DAY
    assert scalar(conn, "SELECT business_day FROM analytics.daily_clicks") == DAY + timedelta(days=1)


def test_attribution_boundaries_and_wrong_position(conn):
    source(conn, event(), event("character_card_clicked", seconds=-1),
           event("character_card_clicked", seconds=WINDOW),
           event("character_card_clicked", seconds=WINDOW + 1),
           event("character_card_clicked", props={"request_id": "req", "surface": "for_you",
                 "character_id": "character-a", "position": 1}, seconds=2))
    refresh(conn)
    assert scalar(conn, "SELECT sum(unmatched_clicks) FROM analytics.daily_clicks") == 3
    assert scalar(conn, "SELECT sum(clicked_impressions) FROM analytics.daily_feed") == 1


def test_expiry_keeps_first_seen_and_aggregates_and_blocks_pending(conn):
    source(conn, event())
    with pytest.raises(RuntimeError, match="unprojected"):
        drop_expired_partitions(conn, DAY + timedelta(days=31))
    refresh(conn)
    drop_expired_partitions(conn, DAY + timedelta(days=31))
    assert scalar(conn, "SELECT count(*) FROM analytics.product_events") == 0
    refresh(conn)
    assert scalar(conn, "SELECT first_seen FROM analytics.dim_visitor") == TIME
    assert scalar(conn, "SELECT sum(impressions) FROM analytics.daily_feed") == 1


@pytest.mark.parametrize("kind", ["payload", "request", "click", "invalid"])
def test_conflict_rolls_back_everything(conn, kind):
    original = event()
    source(conn, original)
    refresh(conn)
    if kind == "payload":
        bad = event(visitor="other", event_id=original[0])
    elif kind == "request":
        bad = event(visitor="other")
    elif kind == "click":
        bad = event("character_card_clicked", visitor="other")
    else:
        bad = event(props={"request_id": "bad", "surface": "for_you"})
    source(conn, bad)
    with pytest.raises(ProjectionError):
        refresh(conn)
    assert scalar(conn, "SELECT count(*) FROM analytics.event_registry") == 1
    assert scalar(conn, "SELECT count(*) FROM analytics.projected_files") == 1
    assert scalar(conn, "SELECT sum(impressions) FROM analytics.daily_feed") == 1


def test_missing_source_is_not_marked_successful(conn):
    source(conn, event())
    conn.execute("DELETE FROM analytics.product_events")
    conn.commit()
    with pytest.raises(ProjectionError, match="backfill"):
        refresh(conn)
    assert scalar(conn, "SELECT count(*) FROM analytics.projected_files") == 0


def test_internal_beta_starts_today_without_historical_backfill(conn):
    source(conn, event(), file_day=DAY - timedelta(days=1))
    refresh(conn)
    assert scalar(conn, "SELECT count(*) FROM analytics.event_registry") == 0
    assert scalar(conn, "SELECT coverage_start FROM analytics.model_state") == DAY
    drop_expired_partitions(conn, DAY + timedelta(days=31))
    with pytest.raises(ProjectionError, match="fixed"):
        project(conn, WINDOW, start_day=DAY - timedelta(days=1))


def test_new_visitor_day_is_server_receipt_utc_not_client_day(conn):
    source(conn, event())
    conn.execute("UPDATE analytics.product_events SET business_day = '2026-09-06'")
    conn.commit()
    refresh(conn)
    assert scalar(conn, "SELECT first_business_day FROM analytics.dim_visitor") == DAY
    assert scalar(conn, "SELECT business_day FROM analytics.visitor_activity") == DAY - timedelta(days=1)


def test_server_origin_first_seen_is_rejected_before_polluting_visitors(conn):
    source(conn, event("visitor_first_seen", props={"entry_path": "/"}))

    with pytest.raises(ProjectionError, match="visitor_first_seen missing client session"):
        refresh(conn)

    assert scalar(conn, "SELECT count(*) FROM analytics.event_registry") == 0
    assert scalar(conn, "SELECT count(*) FROM analytics.projected_files") == 0


def test_report_entrypoint_can_repeat_after_all_migrations_are_applied(conn, monkeypatch):
    import run_report
    source(conn, event())
    monkeypatch.setenv("PLUM_DA_DATABASE_URL", DSN)
    args = ["--only", "project", "--start-day", str(DAY), "--attribution-seconds", str(WINDOW)]
    assert run_report.main(args) == 0
    assert run_report.main(args) == 0
    assert scalar(conn, "SELECT sum(impressions) FROM analytics.daily_feed") == 1


def test_messages_are_deduplicated_and_attributed_to_character(conn):
    conversation = event("conversation_started", props={
        "conversation_id": "conversation-a", "character_id": "character-a"})
    message = event("message_sent", props={"conversation_id": "conversation-a"}, seconds=1)
    source(conn, conversation, message)
    source(conn, message)
    refresh(conn)
    refresh(conn)
    assert scalar(conn, "SELECT count(*) FROM analytics.message_events") == 1
    assert scalar(conn, "SELECT sum(messages) FROM analytics.daily_visitors") == 1
    assert scalar(conn, "SELECT character_id FROM analytics.daily_messages") == "character-a"
    assert scalar(conn, "SELECT sum(messages) FROM analytics.daily_messages") == 1


def test_message_gets_role_when_mapping_arrives_later(conn):
    source(conn, event("message_sent", props={"conversation_id": "conversation-a"}))
    refresh(conn)
    assert scalar(conn, "SELECT sum(messages) FROM analytics.daily_visitors") == 1
    assert scalar(conn, "SELECT count(*) FROM analytics.daily_messages") == 0
    source(conn, event("conversation_resumed", props={
        "conversation_id": "conversation-a", "character_id": "character-a"}, seconds=1))
    refresh(conn)
    assert scalar(conn, "SELECT sum(messages) FROM analytics.daily_messages") == 1


def test_conflicting_conversation_character_rolls_back(conn):
    source(conn, event("conversation_started", props={
        "conversation_id": "conversation-a", "character_id": "character-a"}))
    refresh(conn)
    source(conn, event("conversation_resumed", props={
        "conversation_id": "conversation-a", "character_id": "character-b"}, seconds=1))
    with pytest.raises(ProjectionError, match="multiple characters"):
        refresh(conn)
    assert scalar(conn, "SELECT character_id FROM analytics.conversation_characters") == "character-a"
    assert scalar(conn, "SELECT count(*) FROM analytics.projected_files") == 1
