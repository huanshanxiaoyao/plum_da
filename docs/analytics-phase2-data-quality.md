# Analytics phase 2: operational acceptance and data quality

Date: 2026-09-09. Status: plan agreed in direction; execution evidence pending.
The user closed phase 1 after its core business data flow passed production
acceptance. The four unverified operational observations below transfer to phase 2;
closing phase 1 does not mark them passed. Phase 2 starts with those observations,
then concentrates on the reliability of the data used for product decisions.

## Baseline and boundaries

- Phase-1 evidence remains in [the acceptance record](analytics-v1-acceptance.md).
  Keep the deployed A/B pipeline, migration 007 and persistent model running.
- Coverage stays at **2026-09-09 UTC**, attribution at **7 days / 604800 seconds**.
  No historical-user migration, pre-coverage backfill or legacy compatibility work.
- Phase 2 serves internal operators who need to distinguish valid zero activity,
  missing data, provisional attribution and stale output before interpreting CTR.
- This change publishes the plan and documentation PRs only. It does not execute
  production observations, send notifications, deploy services or change thresholds.
- C2/C3/C4, identity merging, cold-backup architecture, recommendation scoring and
  Thompson sampling remain separate future work. C2 is still required before
  account deletion is released. The older architecture's "phase 2" recommendation
  ideas are not the scope of this phase-2 data-quality iteration.

## Start with the four transferred observations

All four are **pending evidence**, not failed. Scheduled times are original planned
times, not proof of execution. Verify retained records first; if the first run can
no longer be established, record that gap and observe a subsequent natural run
without calling it the first. A manual run cannot close a natural-scheduling check.

| ID | Observation | Required evidence | Status |
| --- | --- | --- | --- |
| O1 | watchdog-ping.timer, originally 2026-09-09 10:05 UTC | Natural systemd execution, exit result and confirmed notification receipt, distinguished from the already accepted manual ping | Pending |
| O2 | report.timer, originally 2026-09-10 00:30 UTC | Natural execution and completion, refreshed HTML generation/model/ingest timestamps and preserved coverage/window | Pending |
| O3 | Real desktop and mobile dashboard | Protected access, date filtering, visitor trend, character table, data freshness and empty dates; anonymized conclusions | Pending |
| O4 | 26-hour stale warning and recovery | Warning for the correct stale signal, last successful HTML remains usable, successful refresh clears the warning and advances timestamps | Pending |

For O4, distinguish stale HTML generation time, stale ingestion/ledger watermark
and Watchdog heartbeat age; one passing case does not prove all three. Use an
isolated state directory and a protected test artifact for fault injection. Never
edit the real ingest heartbeat/cooldown or stop A/B ingestion to manufacture a
condition. Label accelerated clock-based checks as simulations; retain the actual
26-hour observation as pending until its duration and recovery have evidence.
Record any required production-affecting drill separately before execution.

## Data-quality work sequence

After recording O1-O4 results, prioritize observed defects, then deliver the checks
below in small changes. If an observation fails, record the symptom and evidence,
fix the cause, and rerun that check. Do not mark the whole phase complete from a
single clean sample. Existing SQL, ledgers, CLI and static report are the defaults;
new tables or a new monitoring service require a concrete gap first.

| ID | Work | Observable acceptance |
| --- | --- | --- |
| Q1 | File and model completeness | Reconcile sealed manifests, ingest ledger rows, bad rows and in-scope projection coverage. Show eligible/projected/missing counts separately; pre-coverage files and unsealed files are not missing model inputs. |
| Q2 | Event and attribution integrity | Quantify duplicate delivery, collapsed exposure keys, missing context, dimension conflicts, unmatched clicks and late arrivals. Reprocessing the same sealed inputs does not increase unique facts or aggregates. |
| Q3 | Trustworthy metric output | Reconcile request samples with daily aggregates using explicit UTC date, surface, bucket and ranking version. CTR is summed clicked impressions / summed qualified impressions; never average ratios or label summed daily UV as unique visitors across days. |
| Q4 | Freshness and recovery | Distinguish ingest success heartbeat, ledger data freshness, model projection and HTML generation times. On missing/conflicting in-scope data or export failure, expose the failure and retain the last successful artifact; correction and retry recover without resetting coverage. |

For Q2/Q3, check `0 <= clicked_impressions <= impressions` at the same aggregation
grain; raw click events may exceed impressions. Zero denominators must remain
distinguishable from measured 0% CTR. Display the counts behind rates and quality
ratios. The open seven-day attribution window and today's data remain provisional;
late delivery can revise earlier results. An unmatched click is a quality signal,
not automatically an implementation bug, because source events are best effort.
Do not require exposure/click/profile counts to be equal across all organic traffic.

## Ownership and validation

- `plum_da` owns this plan, O1-O4 evidence, quality queries/checks, report quality
  output and Watchdog verification. B operational records remain the production
  evidence source; document schema, permissions and rollback separately if changed.
- `plum_chat` owns browser observations and source-event defects discovered by the
  quality checks. Fix confirmed source issues with focused lifecycle tests; do not
  add events solely because an old broad roadmap lists them.
- `ai4all_bridge` owns the source dictionary/collector contracts and cross-repo
  handoff. Backend implementation changes require a demonstrated source defect.
- QA fixtures cover valid empty data, duplicates, late/cross-day delivery, wrong
  dimensions, missing source files, failed builds, permission denial and recovery.
  Scale new tests to changed behavior; keep production evidence separate from tests.
- Initial quality reporting uses counts and observed rates. Preserve current
  Watchdog thresholds (6h heartbeat, 26h ledger, 6h cooldown); new alert thresholds
  need measured beta traffic and an explicit recorded decision.

## Evidence and completion

For each O/Q item, append the observation day, environment, natural/manual/simulated
execution type, aggregate counts, expected versus actual result, status and follow-up.
Keep exact visitor/request/character IDs, individual event timestamps, production
host addresses, passwords, DSNs and webhooks out of repository evidence. Detailed
investigation values stay in controlled operational records; synthetic fixture
identifiers must be clearly labeled.

Phase 2 completes when O1-O4 have documented outcomes and required fixes/rechecks,
Q1-Q4 have repeatable checks and recovery evidence, and remaining limitations are
explicitly accepted. Until then this is a plan, not a deployment or acceptance claim.
