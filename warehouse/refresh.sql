INSERT INTO analytics.visitor_history
SELECT visitor_id, min(server_time), max(server_time),
       (min(server_time) AT TIME ZONE 'UTC')::date, count(*)
FROM analytics.event_registry WHERE visitor_id IS NOT NULL GROUP BY visitor_id
ON CONFLICT (visitor_id) DO UPDATE SET
  first_seen = excluded.first_seen, last_seen = excluded.last_seen,
  first_business_day = excluded.first_business_day, event_count = excluded.event_count;
INSERT INTO analytics.visitor_activity
SELECT DISTINCT business_day, visitor_id FROM analytics.event_registry WHERE visitor_id IS NOT NULL
ON CONFLICT DO NOTHING;

DELETE FROM analytics.daily_visitors;
INSERT INTO analytics.daily_visitors
WITH active AS (
  SELECT business_day, count(*) visitors FROM analytics.visitor_activity GROUP BY business_day
), new AS (
  SELECT first_business_day business_day, count(*) new_visitors
  FROM analytics.visitor_history GROUP BY first_business_day
), profiles AS (
  SELECT business_day, count(*) views FROM analytics.event_registry
  WHERE event_name = 'character_profile_viewed' GROUP BY business_day
)
SELECT business_day, coalesce(visitors, 0), coalesce(new_visitors, 0), coalesce(views, 0)
FROM active FULL JOIN new USING(business_day) FULL JOIN profiles USING(business_day);

CREATE TEMP TABLE matched_clicks ON COMMIT DROP AS
SELECT c.event_id, i.request_id, i.character_id, i.position
FROM analytics.clicks c JOIN analytics.impressions i USING(request_id, character_id, position)
JOIN analytics.feed_requests r USING(request_id)
WHERE c.visitor_id = r.visitor_id AND c.surface = r.surface
  AND c.occurred_at >= i.occurred_at
  AND c.occurred_at <= i.occurred_at + make_interval(
    secs => current_setting('plum_da.attribution_seconds')::integer);
CREATE INDEX ON matched_clicks(event_id);
CREATE INDEX ON matched_clicks(request_id, character_id, position);
DELETE FROM analytics.daily_feed;
INSERT INTO analytics.daily_feed
SELECT i.business_day, i.character_id, r.surface, r.exp_bucket, r.rank_version,
       count(*), count(*) FILTER (WHERE EXISTS (
         SELECT 1 FROM matched_clicks m WHERE m.request_id = i.request_id
         AND m.character_id = i.character_id AND m.position = i.position))
FROM analytics.impressions i JOIN analytics.feed_requests r USING(request_id)
GROUP BY i.business_day, i.character_id, r.surface, r.exp_bucket, r.rank_version;
DELETE FROM analytics.daily_clicks;
INSERT INTO analytics.daily_clicks
SELECT c.business_day, c.character_id, c.surface, count(*),
       count(*) FILTER (WHERE m.event_id IS NULL)
FROM analytics.clicks c LEFT JOIN matched_clicks m USING(event_id)
GROUP BY c.business_day, c.character_id, c.surface;
INSERT INTO analytics.model_state
SELECT true, now(),
  (SELECT max(loaded_at) FROM analytics.ingest_ledger WHERE lane = 'data'),
  (SELECT max(server_time) FROM analytics.event_registry),
  (SELECT start_day FROM analytics.projection_settings),
  current_setting('plum_da.attribution_seconds')::integer,
  (SELECT count(*) FROM analytics.projected_files)
ON CONFLICT (singleton) DO UPDATE SET
  refreshed_at = excluded.refreshed_at, ingest_watermark = excluded.ingest_watermark,
  event_watermark = excluded.event_watermark, coverage_start = excluded.coverage_start,
  attribution_seconds = excluded.attribution_seconds, projected_files = excluded.projected_files;
