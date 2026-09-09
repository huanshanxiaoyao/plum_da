-- Persistent message facts and role attribution.
-- message_sent carries conversation_id; conversation_started/resumed carry the
-- stable conversation_id -> character_id mapping. Keep both beyond ODS expiry.
CREATE TABLE analytics.conversation_characters (
  conversation_id TEXT PRIMARY KEY,
  character_id TEXT NOT NULL
);
CREATE TABLE analytics.message_events (
  event_id UUID PRIMARY KEY REFERENCES analytics.event_registry(event_id),
  conversation_id TEXT NOT NULL,
  business_day DATE NOT NULL
);
CREATE INDEX message_events_conversation_idx
  ON analytics.message_events (conversation_id, business_day);

CREATE TABLE analytics.daily_messages (
  business_day DATE NOT NULL,
  character_id TEXT NOT NULL,
  messages BIGINT NOT NULL,
  PRIMARY KEY (business_day, character_id)
);

ALTER TABLE analytics.daily_visitors
  ADD COLUMN messages BIGINT NOT NULL DEFAULT 0;

CREATE OR REPLACE VIEW analytics.report_visitors AS
SELECT business_day, visitors, new_visitors, profile_views, messages
FROM analytics.daily_visitors;
CREATE VIEW analytics.report_messages AS SELECT * FROM analytics.daily_messages;

-- One-time catch-up for files projected before this migration. Joining the
-- persistent registry limits this to events accepted by the model. DISTINCT
-- intentionally makes an inconsistent conversation mapping fail on the PK.
INSERT INTO analytics.conversation_characters (conversation_id, character_id)
SELECT DISTINCT e.props ->> 'conversation_id', e.props ->> 'character_id'
FROM analytics.product_events e
JOIN analytics.projected_files p ON p.file_key = e.source_file
JOIN analytics.event_registry r ON r.event_id = e.event_id
WHERE e.event_name IN ('conversation_started', 'conversation_resumed');

INSERT INTO analytics.message_events (event_id, conversation_id, business_day)
SELECT DISTINCT ON (e.event_id)
  e.event_id, e.props ->> 'conversation_id', r.business_day
FROM analytics.product_events e
JOIN analytics.projected_files p ON p.file_key = e.source_file
JOIN analytics.event_registry r ON r.event_id = e.event_id
WHERE e.event_name = 'message_sent'
ORDER BY e.event_id, e.server_time;

COMMENT ON TABLE analytics.message_events IS
  'Persistent deduplicated message_sent facts; contains no message text.';
COMMENT ON TABLE analytics.daily_messages IS
  'Daily message_sent count attributed to character through conversation_id.';
