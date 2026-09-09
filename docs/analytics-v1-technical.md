# Analytics V1 technical plan

2026-09-09: migrations 007 and 008, persistent models, report and Watchdog are deployed;
the core business data flow passed production acceptance and the user closed phase 1.
See the [acceptance record](analytics-v1-acceptance.md) for evidence; four unverified
operational checks are the starting work of [phase 2](analytics-phase2-data-quality.md).
This plan describes implementation, not an instruction to redeploy.

## Boundaries

All warehouse/report changes live in plum_da. The only application change is a
client-side profile-view event in plum_chat; no business API or event dictionary
change is required. UTC is explicit in SQL and rendered dates. Reports are static
artifacts generated from aggregate tables, never a public query API.

## Storage and processing

Migrations 007 and 008 are append-only; never edit deployed migrations 001-006. Persist an event-ID
registry, lifetime visitors, daily browser activity, qualified impressions, clicks,
and a source-file projection ledger. Preserve the old dim_visitor query interface
with a view over the persistent visitor table. Per the user's internal-beta scope
decision, initial projection fixes `projection_settings.start_day` to today UTC.
No historical backfill or migration is performed. Only source-file days at or after
that date are projected. In-scope row counts must match the ingest ledger before
a file is marked complete. A missing source blocks projection; it cannot become a zero day.

Migration 008 persists deduplicated message events and conversation-to-character
mappings. It catches up only already projected, in-scope sources still present in
ODS; it does not broaden the fixed coverage start. Daily message totals include
every message event, while per-character totals include messages with a known stable
conversation mapping. Conflicting mappings fail the projection transaction.

Projection is serialized with a PostgreSQL advisory transaction lock. Each run is
atomic: new event keys, visitor history, feed facts and projection ledger either all
commit or all roll back. Conflicting event payloads or request dimensions fail with
a diagnostic; no arbitrary winner is published. Repeated event IDs are ignored,
repeated impression keys use the earliest occurrence and maximum observed dwell.
CTR keys are (request_id, character_id, position), with matching visitor and surface.
The attribution interval is measured using corrected occurrence time, not arrival.

Report refresh recomputes daily aggregate tables from persistent facts in one
transaction, so late events update earlier exposures as well. Store numerator and
denominator, not averaged ratios. Daily UV remains a daily metric; date-range
summaries show average daily UV rather than the sum masquerading as distinct UV.
Static output is a single atomically replaced HTML document. Build failures leave
the previous complete document in place. The generated document includes its build
time, ingest watermark, model watermark, coverage start and attribution settings.

ODS expiry checks that every expiring in-scope source file has been projected.
Earlier source files expire normally. Missing projection blocks DROP. Cleanup runs
after raw loading, so a delayed model does not prevent new events from entering B;
the cleanup failure still returns nonzero and does not refresh the success heartbeat.
This is a B-only retention guard; A never depends on model success. The initial increment retains compact fact/identity
metadata without automatic pruning; 90-day fact pruning requires a separately
validated rollup/archive path, and must never erase lifetime visitor state.

## Privacy and access

Projection keeps only analysis fields, not message text, prompts, raw queries or
full props of unrelated events. Static output contains only per-day/per-character
aggregates and quality counters; no visitor/account identifiers. Report reads use
dedicated aggregate views, and deployment grants the report reader access only to
those views. nginx protects the complete artifact with basic auth over TLS. Reuse
the existing deployment boundary; do not publish the dashboard to a third party.

The deployed beta uses an IP TLS/basic-auth `/plum-report` location, with the host
root reserved for sub2api. A dedicated subdomain remains a template option, not the
current deployment. Host addresses and credentials stay in controlled operations
records. B uses `/opt/workspace/plum_da`; unit templates' `/opt/plum_da` paths were
adapted during deployment. Coverage is fixed at 2026-09-09 UTC and attribution at
604800 seconds; documentation closeout must not reactivate or reset the model.

## Verification and rollback

See [analytics-v1-acceptance.md](analytics-v1-acceptance.md). For future code tests,
use an isolated local PostgreSQL 16 instance.
Test duplicate IDs across files, repeated exposure keys, late/missing exposure,
cross-day attribution, conflicting dimensions, empty data, raw expiry, rollback and
static export privacy. UI checks cover desktop/mobile, dates, sorting and empty
results. Local Watchdog tests use fake responses. Production delivery is separately
supported by user-confirmed receipt of a controlled ping and isolated missing-heartbeat
alert; the first natural daily ping and long-term stale drill remain pending.

Rollback disables the new report timer and restores the previous HTML artifact.
Keep migrations 007 and 008 and persistent tables: deleting them loses visitor history.
The ingest entrypoint remains compatible; the retention guard is intentionally kept
until pending sources are projected or an explicit data-retention decision is made.

## Internal-beta operating limits

Daily aggregates are rebuilt from compact facts. This is intentionally simple for
the current volume; incremental partitions and archival are deferred until measured
runtime requires them. Feed impression timestamps are batch occurrence times from
the existing contract, not individual card timestamps. Missing/invalid dimensions
or conflicting request identities stop the refresh with an event-level diagnostic.
The previously published artifact remains readable and shows its actual timestamp.
No test-user or bot exclusion exists without a trusted source-contract flag.
The user confirmed a seven-day attribution window for internal beta on 2026-09-09.
`run_report.py` defaults to 604800 seconds; deployment also records this explicitly
in `PLUM_DA_ATTRIBUTION_SECONDS`. Invalid nonpositive values fail before connection.
