-- Run as DBA in plum_da, after migration 007. Set a password separately with \password.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'plum_report') THEN
    CREATE ROLE plum_report LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO plum_report', current_database());
END $$;
GRANT USAGE ON SCHEMA analytics TO plum_report;
GRANT SELECT ON analytics.report_visitors, analytics.report_feed,
  analytics.report_clicks, analytics.report_messages, analytics.report_status TO plum_report;
ALTER ROLE plum_report SET default_transaction_read_only = on;
