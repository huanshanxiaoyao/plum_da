# Analytics V1 requirements

Date: 2026-09-09. Status: phase-1 core business data flow accepted in production;
four operational checks remain pending. Evidence and exact scope:
[acceptance record](analytics-v1-acceptance.md).

Scope correction from the user, 2026-09-09: this is a new product in internal beta.
Do not build historical-user migration, historical backfill or legacy compatibility.
The first successful model run fixes the start day to today UTC; only source files
from that day onward enter the model. Previously stored raw data is left to normal
retention. First seen means first observed within this explicitly displayed scope.

## Goal and baseline

Give Plum operators a read-only daily view of visitors, qualified feed impressions,
clicks, CTR and character performance. The browser -> A files -> B PostgreSQL
pipeline is already deployed and accepted according to the deployment handoff.
The persistent models, dashboard and Watchdog are deployed; controlled notification
receipt and real browser attribution are confirmed. Natural daily scheduling and
long-term stale/recovery acceptance remain open.
Do not equate a merged commit with a production deployment.

The original development baseline was plum_chat bfd5fee, ai4all_bridge 074d5636 and plum_da
70cd582. Existing collection and file transport contracts remain the source contracts.
The implemented 30-day ODS retention and file-key ledger supersede the old P4 draft
examples of 14-day retention and hash-keyed ledger states.

## Scope and decisions

- Persist browser visitor first/last seen independently of ODS retention. This is
  a browser identifier count, not a count of people or signed-in accounts.
- First seen uses the earliest server receipt; its UTC date defines new visitors.
  Daily activity uses the existing event business_day (UTC). Repeated events do not
  inflate either metric. Clearing cookies can create another visitor identifier.
- Store deduplicated impression and click facts and daily aggregates; preserve
  surface, experiment bucket and ranking version. Do not mix experiments silently.
- CTR is clicked qualified impressions / qualified impressions. Multiple clicks on
  one impression contribute at most one numerator. Unmatched clicks are separate.
- Attribution window: **7 days (604800 seconds)**, confirmed by the user on
  2026-09-09 for internal beta; this is the report command's default.
- The user waived local browser-to-A-to-B integration for this release. Automated
  tests and the subsequent real browser production acceptance are recorded separately.
- Generate a daily static, authenticated dashboard, following existing D10. Include
  date filters, daily browser visitors/new visitors, impressions/clicks/CTR, character
  performance, data freshness and processing/coverage status. No person-level data
  or database credentials may be embedded in the static output.
- Emit character_profile_viewed only once a character page actually loads, with
  available feed entry context. Failed/blocked character loads must not count.
- Provide local tests and deployment/rollback instructions for reporting and
  Watchdog. Production installation and controlled notification receipt are now
  confirmed; the remaining operational checks are tracked in the acceptance record.

## Acceptance

| ID | Observable result |
| --- | --- |
| A1 | Reprocessing a file/event does not increase visitor, impression or clicked-impression counts. |
| A2 | Removing old ODS partitions does not move a visitor's first seen or erase existing aggregate history. |
| A3 | A late click updates its matching earlier exposure; unmatched clicks remain visible separately. |
| A4 | Invalid/conflicting attribution dimensions are surfaced, never silently assigned to a bucket. |
| A5 | An arbitrary date range recomputes CTR from summed counts; daily UV is never summed and labelled range UV. |
| A6 | Empty days, incomplete data, stale ingestion and failed report builds are distinguishable. |
| A7 | The dashboard works on desktop/mobile and keyboard, and exports no visitor/account identifiers. |
| A8 | A successful character load emits one view with the correct entry context; failed loads and effect reruns do not duplicate it. |
| A9 | Watchdog tests cover healthy/late/missing/failed signals; real notification delivery is a separate production acceptance. |

## Deferred

Historical users/data, person identity merging, account deletion propagation (C2), revenue/cost exports
(C3), recommendation scoring/feedback (C4), real-time reporting and probabilistic
cross-device identity are outside this increment. Existing C2-before-account-deletion
release requirements still apply. Historical data predating collection cannot be
reconstructed or backfilled; a missing in-scope source blocks the current model
refresh rather than being treated as a zero-activity day.
