CREATE TABLE analytics.projection_settings (
  singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
  start_day DATE NOT NULL
);
CREATE TABLE analytics.projected_files (
  file_key TEXT PRIMARY KEY REFERENCES analytics.ingest_ledger(file_key),
  sha256 TEXT NOT NULL,
  rows_loaded INTEGER NOT NULL,
  projected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE analytics.event_registry (
  event_id UUID PRIMARY KEY,
  fingerprint TEXT NOT NULL,
  event_name TEXT NOT NULL,
  visitor_id TEXT,
  server_time TIMESTAMPTZ NOT NULL,
  business_day DATE NOT NULL
);
CREATE INDEX event_registry_visitor_idx ON analytics.event_registry(visitor_id);
CREATE TABLE analytics.visitor_history (
  visitor_id TEXT PRIMARY KEY,
  first_seen TIMESTAMPTZ NOT NULL,
  last_seen TIMESTAMPTZ NOT NULL,
  first_business_day DATE NOT NULL,
  event_count BIGINT NOT NULL
);
CREATE OR REPLACE VIEW analytics.dim_visitor AS
SELECT visitor_id, first_seen, last_seen, first_business_day, event_count
FROM analytics.visitor_history;
COMMENT ON VIEW analytics.dim_visitor IS
  'Persistent browser history; new visitors use earliest server receipt in UTC. Refresh with run_report.py.';
CREATE TABLE analytics.visitor_activity (
  business_day DATE NOT NULL,
  visitor_id TEXT NOT NULL,
  PRIMARY KEY (business_day, visitor_id)
);
CREATE TABLE analytics.feed_requests (
  request_id TEXT PRIMARY KEY,
  visitor_id TEXT NOT NULL,
  surface TEXT NOT NULL,
  exp_bucket TEXT NOT NULL,
  rank_version TEXT NOT NULL
);
CREATE TABLE analytics.impressions (
  request_id TEXT NOT NULL REFERENCES analytics.feed_requests(request_id),
  character_id TEXT NOT NULL,
  position INTEGER NOT NULL CHECK (position >= 0),
  occurred_at TIMESTAMPTZ NOT NULL,
  business_day DATE NOT NULL,
  dwell_ms BIGINT NOT NULL,
  PRIMARY KEY (request_id, character_id, position)
);
CREATE TABLE analytics.clicks (
  event_id UUID PRIMARY KEY REFERENCES analytics.event_registry(event_id),
  request_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  position INTEGER NOT NULL CHECK (position >= 0),
  visitor_id TEXT NOT NULL,
  surface TEXT NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  business_day DATE NOT NULL
);
CREATE INDEX clicks_attribution_idx ON analytics.clicks(request_id, character_id, position);
CREATE TABLE analytics.daily_visitors (
  business_day DATE PRIMARY KEY,
  visitors BIGINT NOT NULL,
  new_visitors BIGINT NOT NULL,
  profile_views BIGINT NOT NULL
);
CREATE TABLE analytics.daily_feed (
  business_day DATE NOT NULL,
  character_id TEXT NOT NULL,
  surface TEXT NOT NULL,
  exp_bucket TEXT NOT NULL,
  rank_version TEXT NOT NULL,
  impressions BIGINT NOT NULL,
  clicked_impressions BIGINT NOT NULL,
  PRIMARY KEY (business_day, character_id, surface, exp_bucket, rank_version)
);
CREATE TABLE analytics.daily_clicks (
  business_day DATE NOT NULL,
  character_id TEXT NOT NULL,
  surface TEXT NOT NULL,
  clicks BIGINT NOT NULL,
  unmatched_clicks BIGINT NOT NULL,
  PRIMARY KEY (business_day, character_id, surface)
);
CREATE TABLE analytics.model_state (
  singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
  refreshed_at TIMESTAMPTZ NOT NULL,
  ingest_watermark TIMESTAMPTZ,
  event_watermark TIMESTAMPTZ,
  coverage_start DATE,
  attribution_seconds INTEGER NOT NULL CHECK (attribution_seconds > 0),
  projected_files BIGINT NOT NULL
);
CREATE VIEW analytics.report_visitors AS SELECT * FROM analytics.daily_visitors;
CREATE VIEW analytics.report_feed AS SELECT * FROM analytics.daily_feed;
CREATE VIEW analytics.report_clicks AS SELECT * FROM analytics.daily_clicks;
CREATE VIEW analytics.report_status AS SELECT * FROM analytics.model_state;
