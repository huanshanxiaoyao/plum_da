# Analytics V1 acceptance

Status: local implementation verified, 2026-09-09. Seven-day attribution confirmed
by the user; production deployment acceptance remains pending. Internal-beta scope starts on
activation day; historical users/data are excluded, not migrated or backfilled.

| Requirement | Evidence | Status |
| --- | --- | --- |
| A1 Duplicate delivery and repeat processing | test_projection.py: duplicate files/IDs/keys and repeat CLI execution | Passed |
| A2 Durable visitor history and expiry guard | test_projection.py and test_ingest_retention.py: history survives expiry; cleanup refusal permits new ingestion | Passed |
| A3 Late/cross-day click attribution | Late exposure, cross-day clicks, exact window boundary, wrong position and repeated clicks | Passed; 7-day window confirmed |
| A4 Dimension conflicts and missing raw sources | Atomic rollback on payload/request/identity/dimension conflicts or missing in-scope rows | Passed |
| A5 Date aggregation and CTR arithmetic | Browser: 1/99 and 1/1 combine to 2/100 = 2.00%, not average CTR; 4 visitors across 2 days yields daily average 2 | Passed |
| A6 Empty/stale/failed states | Browser empty/stale/date-error recovery; test_report.py failed atomic replace preserves last HTML | Passed |
| A7 Responsive and private static output | 1440/390/320 px browser checks; actual reader-login export; forbidden raw/identity reads and writes; script escaping | Passed locally |
| A8 Character view lifecycle | analytics-profile-visit.test.mjs: successful commit, effect cancellation, retries, query replacement, stale/departed loads and storage | Passed locally; real collector receipt remains release acceptance |
| A9 Watchdog delivery support | test_watchdog.py: existing heartbeat/ledger/cooldown checks, malformed notification response rejection and failed ping exit | Passed with fake delivery; real notification pending |

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
- The frontend lifecycle is unit tested and type checked, not tested against a
  live business backend in this session. The user explicitly waived local real-chain
  integration and will operate the online acceptance after deployment. Production TLS/systemd and notification
  delivery are intentionally not reported as verified.

## Deployment acceptance still required

- Record deployed commits and migration IDs independently of repository state.
- Verify the report timer's natural execution and avoid the existing backup window.
- Verify TLS and 401 without credentials for the entire dashboard location.
- Send a controlled Watchdog notification and verify receipt; test missing/stale
  heartbeat, recovery and daily ping separately. Do not infer delivery from HTTP 200.
- Restore the previous report after a controlled failed build; confirm the last
  complete report remains readable and stale status is visible.

## Rollback

Stop the report timer; retain ODS and persistent tables. Restore the previous static
artifact. Do not reverse the persistent visitor migration by dropping history.
