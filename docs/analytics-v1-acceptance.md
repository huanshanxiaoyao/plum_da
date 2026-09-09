# Analytics V1 acceptance

Status, 2026-09-09: **phase-1 core business data flow accepted in production**,
based on machine B's execution report and the user's browser/notification confirmation.
This documentation closeout did not recheck or redeploy production. Four operational
acceptance items remain below; service startup and committed code do not close them.
Coverage starts on 2026-09-09 UTC, with seven-day attribution (604800 seconds).
Historical users/data are excluded, not migrated or backfilled.

| Requirement | Evidence | Status |
| --- | --- | --- |
| A1 Duplicate delivery and repeat processing | test_projection.py: duplicate files/IDs/keys and repeat CLI execution | Passed locally |
| A2 Durable visitor history and expiry guard | test_projection.py and test_ingest_retention.py: history survives expiry; cleanup refusal permits new ingestion | Passed locally |
| A3 Late/cross-day click attribution | Late exposure, cross-day clicks, exact window boundary, wrong position and repeated clicks | Passed locally; 7-day window confirmed and deployed |
| A4 Dimension conflicts and missing raw sources | Atomic rollback on payload/request/identity/dimension conflicts or missing in-scope rows | Passed locally |
| A5 Date aggregation and CTR arithmetic | Browser: 1/99 and 1/1 combine to 2/100 = 2.00%, not average CTR; 4 visitors across 2 days yields daily average 2 | Passed locally; production daily aggregate separately recorded below |
| A6 Empty/stale/failed states | Local empty/stale/date-error checks; production failed export retained the last successful HTML | Local checks and production export failure passed; 26-hour stale/recovery drill pending |
| A7 Responsive and private static output | Local 1440/390/320 px checks and script escaping; production aggregate-only reader permissions and HTTPS authentication verified | Permissions passed in production; real desktop/mobile dashboard checks pending |
| A8 Character view lifecycle | Local lifecycle regression tests; two real browser visits each yielded one exposure, click and successful profile view with matching context | Core production flow passed; no claim of separate live coverage for every auth/error branch |
| A9 Watchdog delivery support | Local heartbeat/ledger/cooldown/response tests; user confirmed receipt of manual healthy ping and isolated heartbeat_missing alert | Controlled delivery passed; first natural daily ping pending |

## Local evidence

- Isolated PostgreSQL 16.14, port 55439, dedicated `plum_da_test` database.
  Full warehouse suite: **75 passed**. Final CLI repeat/reader-export regression
  run after the last adjustment: **15 passed**, including one additional test.
  No production database or webhook was accessed.
- Frontend: **241 tests passed**, `npm run typecheck` passed. Warehouse Ruff and
  all three repositories' `git diff --check` passed.
- After the user's seven-day decision: **3 configuration tests passed**, covering
  the 604800-second default, explicit override and invalid configuration rejection.
- Playwright: date filtering, invalid-date correction, CTR arithmetic, sorting,
  25-row pagination, empty and stale states, native keyboard activation passed.
  Viewport width equals document width at 1440/390/320; chart pixel checks found
  18274/5117/4167 painted pixels respectively. Screenshots were visually checked.
- Local artifacts: `/tmp/plum-analytics-preview/index.html` (synthetic demo only),
  `desktop.png`, `mobile.png`, and `mobile-320.png`. The report opens as a standalone
  file; the temporary HTTP service is only for browser verification.
- The user explicitly waived local real-chain integration. The production evidence
  below comes from the subsequent machine B report and user confirmation, not from
  the local browser screenshots or this documentation session.

## Production evidence

- Frontend: `ec2018c03d54ef078600256f2044d6e41b9fd46f`,
  [Deploy EC201 / 34308761021](https://github.com/huanshanxiaoyao/plum_chat/actions/runs/34308761021), success.
  Warehouse: `7fe4f00a0ca656513bf5adeb8fd599ce231bc34c`.
  Upstream handoff baseline: `69a8da951e4f976e4c9f083d3cced42c8bec25e4` on
  `docs/analytics-v1-release-handoff`; never merge this branch into backend main
  merely to synchronize documentation, as main deploys the business backend.
- B checkout `/opt/workspace/plum_da`, service user `plum_da`, PostgreSQL 16/main
  on loopback port 5432, database `plum_da`. Migration `007_persistent_analytics`
  applied; coverage and attribution match the values above. Existing A/B collection
  remains running; no historical migration/backfill or coverage-start change.
- Ingest, report, watchdog and watchdog-ping timers are enabled/active. This is
  installation evidence, not evidence of each timer's first natural execution.
- Dashboard uses the protected IP HTTPS `/plum-report` location; the actual host
  address stays in controlled operations records. HTTPS returns 401 without auth
  and 200 with auth; the same host's root continues to serve sub2api. Both HTTP
  and HTTPS allow 256m request bodies; HTTP uses 308. The sub2api 413 issue is fixed.
  Let's Encrypt shortlived IP TLS renewal dry-run passed; the deploy hook tests
  nginx configuration before reloading. The report reader cannot read identity/raw
  tables or write; a failed export leaves the previous successful HTML unchanged.
- The user confirmed receipt of both the healthy ping and heartbeat_missing alert.
  The alert used an isolated state directory; the real ingest heartbeat and real
  notification cooldown state were not modified.
- One browser completed two Feed-to-profile visits with a return to Feed between
  them. Each independent request had exactly one `feed_impressed`, one
  `character_card_clicked` and one `character_profile_viewed`; both dwell times
  exceeded one second. Character, position, surface, experiment bucket and ranking
  version matched. Both clicks were attributed within seven days; unmatched clicks: 0.
- A sealed the files naturally; B's natural ingest at **08:12 UTC** succeeded.
  Manual report refresh at **08:14 UTC** succeeded. The targeted requests contain
  **2 impressions and 2 clicked impressions**. The target character's full-day
  aggregate is **39 impressions, 2 clicked impressions, CTR 5.13%**. The sample's
  2/2 is not the dashboard's daily CTR and must not be reported as 100% for the day.
- Rollback materials: `/var/backups/plum_da/analytics-v1-20260909/`, outside the
  web root. No passwords, DSNs, webhooks, production visitor/request/character IDs,
  or individual event timestamps are included here.

Cross-repository record:
[release handoff](https://github.com/huanshanxiaoyao/ai4all_bridge/blob/docs/analytics-v1-release-handoff/docs/ops/products/plum/analytics_v1_release_handoff.md).

## Operational acceptance still required

1. `plum-da-watchdog-ping.timer`: first natural run on 2026-09-09 at **10:05 UTC**.
2. `plum-da-report.timer`: first natural run on 2026-09-10 at **00:30 UTC**.
3. Real desktop and mobile dashboard checks: dates, visitor trend, character table,
   data freshness and empty dates. Local responsive checks do not substitute for these.
4. **26-hour stale warning and recovery drill**. Do not alter the real ingest
   heartbeat to manufacture the condition.

Keep these pending until actual evidence is reported, even after the scheduled time.

## Documentation closeout checks

2026-09-09, documentation-only changes:

- `tests/test_report_config.py`, `tests/test_watchdog.py`, `tests/test_report.py`:
  **26 passed, 1 skipped**. The aggregate-reader PostgreSQL integration test was
  skipped because no isolated test DSN was configured; no database was started or
  production database accessed. Prior local and reported production permission
  evidence above remains distinct from this run.
- `ruff check . --no-cache` passed; `contracts/drift_check.py` found no drift from
  the backend event dictionary.
- Frontend analytics regression: **26 passed**. Backend dictionary, envelope and
  identity-token regression: **53 passed**; dictionary G1/G2/G3 checks passed.
- Across all three repositories, added relative links, Markdown code fences and
  added sensitive-value patterns were checked; `git diff --check` passed.
  This closeout does not deploy, restart, change A, or alter B's coverage start.

## Rollback

Stop the report timer; retain ODS and persistent tables. Restore the previous static
artifact. Do not reverse the persistent visitor migration by dropping history.
