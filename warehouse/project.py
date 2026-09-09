"""Atomic, replayable projection from sealed ODS sources into durable facts."""

from pathlib import Path
from datetime import date, datetime, timezone

LOCK_ID = 5852007


class ProjectionError(ValueError):
    pass


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ProjectionError(f"missing/invalid {field}")
    return value


def _position(value):
    if type(value) is not int or not 0 <= value <= 2147483647:
        raise ProjectionError("invalid position")
    return value


def project(conn, attribution_seconds: int, *, start_day: date | None = None):
    if type(attribution_seconds) is not int or not 0 < attribution_seconds <= 2147483647:
        raise ValueError("attribution_seconds must be an explicit positive integer")
    # Own the transaction so failed validation cannot leave partial history behind.
    if conn.info.transaction_status != 0:
        raise ValueError("projection requires an idle connection")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
            cur.execute("SET LOCAL TIME ZONE 'UTC'")
            cur.execute("SELECT start_day FROM analytics.projection_settings WHERE singleton")
            settings = cur.fetchone()
            if settings and start_day is not None and settings[0] != start_day:
                raise ProjectionError("start_day is fixed after activation")
            cur.execute("""INSERT INTO analytics.projection_settings VALUES (true, %s)
                ON CONFLICT DO NOTHING""", (start_day or datetime.now(timezone.utc).date(),))
            cur.execute("SELECT set_config('plum_da.attribution_seconds', %s, true)",
                        (str(attribution_seconds),))
            cur.execute("""
                SELECT e.source_file FROM analytics.product_events e
                LEFT JOIN analytics.ingest_ledger l ON l.file_key = e.source_file AND l.lane = 'data'
                WHERE l.file_key IS NULL AND e.server_time >=
                  (SELECT start_day FROM analytics.projection_settings) LIMIT 1
            """)
            if cur.fetchone():
                raise ProjectionError("ODS source has no data ingest ledger entry")
            cur.execute("""
                SELECT l.file_key FROM analytics.ingest_ledger l
                JOIN analytics.projected_files p USING (file_key)
                WHERE l.sha256 <> p.sha256 OR l.rows_loaded <> p.rows_loaded LIMIT 1
            """)
            if cur.fetchone():
                raise ProjectionError("a projected source was replaced; restore original source")
            cur.execute("""
                CREATE TEMP TABLE pending_sources ON COMMIT DROP AS
                SELECT l.* FROM analytics.ingest_ledger l
                LEFT JOIN analytics.projected_files p USING (file_key)
                WHERE l.lane = 'data' AND p.file_key IS NULL
                  AND l.business_day >= (SELECT start_day FROM analytics.projection_settings)
            """)
            cur.execute("""
                SELECT p.file_key FROM pending_sources p
                LEFT JOIN analytics.product_events e ON e.source_file = p.file_key
                GROUP BY p.file_key, p.rows_loaded
                HAVING count(e.event_id) <> p.rows_loaded LIMIT 1
            """)
            if cur.fetchone():
                raise ProjectionError("pending source rows do not match ingest ledger; backfill required")
            cur.execute("""
                CREATE TEMP TABLE candidates ON COMMIT DROP AS
                SELECT e.*, encode(sha256(convert_to(jsonb_build_array(
                  e.event_name, e.dict_version, e.client_time, e.session_id,
                  e.visitor_id, e.props)::text, 'UTF8')), 'hex') AS fingerprint
                FROM analytics.product_events e
                JOIN pending_sources p ON p.file_key = e.source_file
            """)
            cur.execute("""
                SELECT event_id FROM candidates GROUP BY event_id
                HAVING count(DISTINCT fingerprint) > 1
                UNION ALL
                SELECT c.event_id FROM candidates c JOIN analytics.event_registry e USING(event_id)
                WHERE c.fingerprint <> e.fingerprint LIMIT 1
            """)
            if cur.fetchone():
                raise ProjectionError("conflicting payloads for the same event_id")
            cur.execute("""
                CREATE TEMP TABLE fresh_events ON COMMIT DROP AS
                SELECT DISTINCT ON (c.event_id) c.* FROM candidates c
                LEFT JOIN analytics.event_registry e USING(event_id)
                WHERE e.event_id IS NULL ORDER BY c.event_id, c.server_time
            """)
            cur.execute("""
                INSERT INTO analytics.event_registry
                SELECT event_id, fingerprint, event_name, visitor_id, server_time, business_day
                FROM fresh_events
            """)
            cur.execute("""
                UPDATE analytics.event_registry e SET server_time = c.first_receipt
                FROM (SELECT event_id, min(server_time) first_receipt FROM candidates
                      GROUP BY event_id) c
                WHERE e.event_id = c.event_id AND c.first_receipt < e.server_time
            """)
            cur.execute("""
                CREATE TEMP TABLE new_impressions (LIKE analytics.impressions INCLUDING DEFAULTS)
                ON COMMIT DROP;
                CREATE TEMP TABLE new_requests (LIKE analytics.feed_requests) ON COMMIT DROP;
                CREATE TEMP TABLE new_clicks (LIKE analytics.clicks) ON COMMIT DROP
            """)
        # Stream bounded chunks; COPY keeps database roundtrips independent of event volume.
        with conn.cursor(name="feed_events") as source:
            source.execute("""
                SELECT event_id, event_name, visitor_id, coalesce(event_time_utc, server_time),
                       business_day, props FROM fresh_events
                WHERE event_name IN ('feed_impressed', 'character_card_clicked')
            """)
            while rows := source.fetchmany(1000):
                requests, impressions, clicks = [], [], []
                for event_id, name, visitor, when, day, props in rows:
                    try:
                        visitor = _text(visitor, "visitor_id")
                        request = _text(props.get("request_id"), "request_id")
                        surface = props.get("surface")
                        if surface not in {"home", "for_you", "search", "tag"}:
                            raise ProjectionError("invalid surface")
                        if name == "character_card_clicked":
                            clicks.append((event_id, request, _text(props.get("character_id"),
                                "character_id"), _position(props.get("position")), visitor,
                                surface, when, day))
                            continue
                        requests.append((request, visitor, surface,
                            _text(props.get("exp_bucket"), "exp_bucket"),
                            _text(props.get("rank_version"), "rank_version")))
                        items = props.get("items")
                        if not isinstance(items, list) or not items:
                            raise ProjectionError("missing impression items")
                        for item in items:
                            dwell = item.get("dwell_ms", 0)
                            if type(dwell) is not int or not 0 <= dwell <= 9223372036854775807:
                                raise ProjectionError("invalid dwell_ms")
                            impressions.append((request, _text(item.get("character_id"),
                                "character_id"), _position(item.get("position")), when, day, dwell))
                    except (AttributeError, ProjectionError) as exc:
                        raise ProjectionError(f"event {event_id}: {exc}") from exc
                with conn.cursor() as cur:
                    for table, batch in (("new_requests", requests),
                                         ("new_impressions", impressions), ("new_clicks", clicks)):
                        with cur.copy(f"COPY {table} FROM STDIN") as copy:
                            for row in batch:
                                copy.write_row(row)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT event_id FROM fresh_events
                WHERE event_name IN ('conversation_started', 'conversation_resumed')
                  AND (nullif(btrim(props ->> 'conversation_id'), '') IS NULL
                    OR nullif(btrim(props ->> 'character_id'), '') IS NULL)
                LIMIT 1
            """)
            invalid_conversation = cur.fetchone()
            if invalid_conversation:
                raise ProjectionError(
                    f"event {invalid_conversation[0]}: missing/invalid conversation mapping"
                )
            cur.execute("""
                WITH mappings AS (
                  SELECT props ->> 'conversation_id' conversation_id,
                         props ->> 'character_id' character_id
                  FROM fresh_events
                  WHERE event_name IN ('conversation_started', 'conversation_resumed')
                  UNION ALL
                  SELECT conversation_id, character_id
                  FROM analytics.conversation_characters
                )
                SELECT conversation_id FROM mappings GROUP BY conversation_id
                HAVING count(DISTINCT character_id) > 1 LIMIT 1
            """)
            conflict = cur.fetchone()
            if conflict:
                raise ProjectionError(
                    f"conversation {conflict[0]} maps to multiple characters"
                )
            cur.execute("""
                SELECT event_id FROM fresh_events
                WHERE event_name = 'message_sent'
                  AND nullif(btrim(props ->> 'conversation_id'), '') IS NULL
                LIMIT 1
            """)
            invalid_message = cur.fetchone()
            if invalid_message:
                raise ProjectionError(
                    f"event {invalid_message[0]}: missing/invalid conversation_id"
                )
            cur.execute("""
                SELECT request_id FROM (
                    SELECT * FROM new_requests UNION ALL SELECT * FROM analytics.feed_requests
                ) r GROUP BY request_id
                HAVING count(DISTINCT (visitor_id, surface, exp_bucket, rank_version)) > 1 LIMIT 1
            """)
            if cur.fetchone():
                raise ProjectionError("conflicting dimensions for a feed request")
            cur.execute("""
                INSERT INTO analytics.feed_requests SELECT DISTINCT * FROM new_requests
                ON CONFLICT DO NOTHING;
                INSERT INTO analytics.impressions
                SELECT request_id, character_id, position, min(occurred_at),
                       (array_agg(business_day ORDER BY occurred_at))[1], max(dwell_ms)
                FROM new_impressions GROUP BY request_id, character_id, position
                ON CONFLICT (request_id, character_id, position) DO UPDATE SET
                  business_day = CASE WHEN excluded.occurred_at < impressions.occurred_at
                    THEN excluded.business_day ELSE impressions.business_day END,
                  occurred_at = least(impressions.occurred_at, excluded.occurred_at),
                  dwell_ms = greatest(impressions.dwell_ms, excluded.dwell_ms);
                INSERT INTO analytics.clicks SELECT * FROM new_clicks;
                INSERT INTO analytics.conversation_characters
                SELECT DISTINCT props ->> 'conversation_id', props ->> 'character_id'
                FROM fresh_events
                WHERE event_name IN ('conversation_started', 'conversation_resumed')
                ON CONFLICT DO NOTHING;
                INSERT INTO analytics.message_events
                SELECT event_id, props ->> 'conversation_id', business_day
                FROM fresh_events WHERE event_name = 'message_sent';
                INSERT INTO analytics.projected_files (file_key, sha256, rows_loaded)
                SELECT file_key, sha256, rows_loaded FROM pending_sources
            """)
            cur.execute("""
                SELECT c.event_id FROM analytics.clicks c
                JOIN analytics.feed_requests r USING(request_id)
                WHERE c.visitor_id <> r.visitor_id OR c.surface <> r.surface LIMIT 1
            """)
            if cur.fetchone():
                raise ProjectionError("click identity/surface conflicts with feed request")
            cur.execute(Path(__file__).with_name("refresh.sql").read_text())
    return {"attribution_seconds": attribution_seconds}
