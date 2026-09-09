# Analytics V1 deployment and acceptance

2026-09-09: production installation and core business data flow acceptance completed,
according to machine B's report and user confirmation. The user has closed phase 1;
the four unverified operational observations now start
[phase 2: data quality](../docs/analytics-phase2-data-quality.md). Historical evidence
remains in [the acceptance record](../docs/analytics-v1-acceptance.md).
The procedures below are deployment/rollback reference, not a request to reinstall
the running system during documentation closeout.
The message dashboard increment (migration 008) was deployed at 14:55 UTC; the
report service and authenticated HTTPS checks passed.
Existing A/B collection and transport remain running. No A business API changes.
Internal beta scope: **no historical user migration or event backfill**. First
successful projection fixes the start date to today UTC; subsequent runs reuse it.
Events in earlier source-file days are excluded and expire normally. Only newly
scoped sources must be projected before ODS expiry. Do not change the start date
or delete existing data to get past an error.
Actual B checkout is `/opt/workspace/plum_da`; the deployed units adapt the template
path `/opt/plum_da`. PostgreSQL is 16/main on loopback port 5432. Migrations
`007_persistent_analytics` and `008_messages` are applied; coverage is
**2026-09-09 UTC**, and attribution is **604800 seconds**.
Ingest/report/watchdog/watchdog-ping timers are enabled/active.
The dashboard uses protected IP HTTPS at `/plum-report`; the host root serves
sub2api. Actual host addresses and credentials remain in controlled operations records.

## Activate on B (reference procedure; already completed)

1. Update the B checkout/dependencies in the existing deployment location.
   Confirm the report service's PostgreSQL unit name matches the host.
2. Set `PLUM_DA_ATTRIBUTION_SECONDS=604800`. The user confirmed **7 days** for
   internal beta on 2026-09-09; this is also the command default. Record it in the
   deployment environment so the operating configuration is explicit.
3. With the existing owner DSN, run `.venv/bin/python run_report.py --only project`.
   This applies migrations 007-008 and activates the model from today UTC. There is
   no historical conversion requirement for this internal beta.
4. As DBA, run `deploy/report_reader.sql` in `plum_da`, then set the dedicated
   role's password using psql `\password plum_report`. Do not pass secrets in shell
   history. Set `PLUM_DA_REPORT_DATABASE_URL` to that role in the environment.
5. Create `/srv/plum_report`, writable by `plum_da`, readable by nginx. Run
   `.venv/bin/python run_report.py --only export --output /srv/plum_report/index.html`.
   Query/export errors return nonzero and retain the last complete HTML.
6. Install the report service/timer. `/etc/plum_da_report.env` holds the attribution
   setting and reader URL, mode 0600 root. The service also uses `/etc/plum_da.env`
   for projection. Schedule is daily at 00:30 UTC, after ingestion at :12.
7. Protect the dashboard with TLS/basic auth. `report-nginx.conf.example` shows
   a dedicated-subdomain option; the deployed beta instead uses IP TLS at
   `/plum-report`, preserving sub2api at `/`. Keep temporary/backup artifacts outside
   the web root. The plum_chat profile-view event is already deployed.

Readiness checklist for future releases (current results are in the acceptance record):

- Manual project/export succeeds; a natural timer execution then succeeds.
- `SELECT * FROM analytics.report_status` shows the intended start day, window,
  model time and ingest/event watermarks. A late click revises its earlier exposure
  day on the next daily run. Today and the open attribution window are provisional.
- Reader can query the five `report_*` views and cannot SELECT `product_events`,
  `event_registry`, `dim_visitor`, or any `visitor_id` table; INSERT must fail.
- Unauthenticated HTTPS request is 401, authenticated request is 200, HTTP redirects
  to HTTPS. Check mobile date filtering and the latest data timestamps.
- A failed export leaves the prior HTML unchanged. A stopped report timer results
  in the page's stale-snapshot warning after 26h; report failures remain in journal.

No bot/internal-user exclusion is claimed: the current event contract provides no
trusted exclusion flag. Role labels are IDs because no character-name export exists.
The dashboard publishes aggregates only; browser IDs and account IDs stay in B.

## Watchdog acceptance

Use the existing four watchdog service/timer files in `deploy/README.md` section 8.
This release also stops treating unknown HTTP-200 response bodies as delivered and
makes failed/missing-config health pings return nonzero.

1. `run_watchdog.py --dry-run` reads real signals without sending or writing state.
2. Verify 6h process heartbeat, 26h ledger freshness, 6h notification cooldown and
   daily positive ping. The absence of a positive ping still requires a human to
   notice; this is not external host monitoring.
3. In the authorized deployment session, send one controlled ping and one controlled
   alert to the approved destination, and record receipt. Do not modify the real
   ingest heartbeat merely to simulate failure; use a separate test state directory.
4. Observe a natural timer run and confirm failed delivery does not enter cooldown.
   Controlled production ping and isolated alert receipt are now confirmed by the
   user. The first natural daily ping at 10:05 UTC remains pending; fake-response
   tests and a manual send cannot substitute for that observation.

## Rollback

Disable the report timer and restore the last good HTML from an operator backup.
Keep migrations 007 and 008 and persistent state. Removing the tables loses tracked history
and breaks the new ODS-expiry guard. Frontend rollback removes only the new view
event. New frontend telemetry requires no backend deployment. Existing raw pipeline
remains running. B code/model installation is complete; this documentation closeout
does not deploy or restart services. Existing rollback materials are under
`/var/backups/plum_da/analytics-v1-20260909/`.
