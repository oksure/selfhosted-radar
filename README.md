# Self-Hosted Radar

A unified directory of self-hosted applications, built from
[awesome-selfhosted](https://github.com/awesome-selfhosted/awesome-selfhosted-data)
and [selfh.st/apps](https://selfh.st/apps), with daily snapshots and a change feed.

Live: <https://selfhosted.pockethost.io/>

## What it does

- **Merge.** Both directories are normalised to one project per repository. The
  canonical key is `github:<owner>/<repo>`, `git:<host>/<path>`, or
  `web:<host>|<slug>` when an app has no source repo. 2,093 projects, 551 of
  them listed in both directories.
- **Snapshot.** One `repo_snapshots` row per project per day, so star counts and
  releases become time series.
- **Watch.** `surveillance_events` records first appearances, new releases,
  star surges and stale or archived projects. Published as
  `/radar.xml`.
- **Browse.** Search, facets, sorting, a table view, per-app detail pages and a
  change feed. Sign-in unlocks a private curation list.

## Layout

```
collector/     source adapters, merge, API writer, offline SQLite stager
pb_migrations/ collections, indexes and API rules
pb_hooks/      summary endpoint, RSS feed, /apps/<slug> route
pb_public/     the whole frontend (no build step)
tests/         merge, delta and adapter tests
```

## Loading data

Two paths, and the choice is not a preference — PocketHost limits requests per
IP per hour, so a full load through the HTTP API cannot finish.

**API update (the daily path).** The writer saves the complete plan locally,
then confirms at most 200 serial record operations per invocation. It never
drops a collection because it contains more than 600 rows.

```bash
source ~/dotfiles/claude/hooks/agent-env.sh
python3 -m collector.sync --dry-run     # fetch and merge, write nothing
python3 -m collector.sync               # fetch, merge, write
python3 -m collector.sync --resume      # finish saved work without fetching
```

A systemd user timer runs this nightly at 04:20 KST
(`selfhosted-radar-collect.timer`).

The normal command also resumes pending work before fetching anything new.
An invocation that resumes a plan processes that plan only, retaining its original
date and event details. Run normally again after it completes to collect fresh
input. A normal run exits **0** only if its plan is complete, its date matches the
requested date, and its estimated daily baseline fits one invocation. It exits
**1** for failed or pending writes, a stale plan, insufficient daily capacity, or
unknown capacity. `complete` refers only to journal completion; `success` and
`failure_reasons` describe the normal daily result. Explicit `--resume` exits **0**
when the saved plan completes, even when its date is stale or its daily capacity
is insufficient. A successful dry run exits **0** and reports the estimate.
Invalid arguments or missing credentials exit **2**. A request-budget stop is
incomplete even with zero errors.

`freshness` is `current` only for a completed plan dated for the requested day,
`stale` when its date differs, and `incomplete` for an unfinished plan dated for
that day. `--today` sets the requested date; without it, the collector uses its
existing local-date default. Finishing yesterday's journal is not a successful
daily collection, and does not fetch today's input during that invocation.
Collecting again later creates that later day's snapshot; it does not reconstruct
snapshots for days missed while the older journal was pending.

`daily_capacity` compares a baseline with the operation cap, assuming one run per
day and an unchanged input set: one snapshot and one project update per project,
plus one update per distinct source entry. Source and project updates recur
because their last-seen and last-synced dates change. Events and other additional
work are excluded, so `baseline_within_cap` is not a guarantee of headroom.
For example, 2,400 projects with one source each need at least 7,200 baseline
operations per day under those assumptions, versus the default 200 operations
per run. The estimate concerns record operations, not PocketHost HTTP quotas;
reads, retries, and writes consume requests too. A saved journal from before this
estimate has `daily_capacity.assessment: unknown`; finish it with `--resume` and
run normally to create a fresh estimate.

The JSON result reports confirmed operations in `projects_created`,
`projects_updated`, `entries`, `snapshots`, and `events`; replay confirmations
are included even if the original request committed before its response was lost.
`attempted` counts this invocation's record operations, `planned` counts the
whole journal, `confirmed_total` is its checkpointed prefix, and `pending` plus
`pending_<collection>` account for every remaining operation. `errors` counts
failed operations in this invocation; the writer stops at the first failure.
`failure` identifies the failed collection/record and exception type, with the
HTTP status when available, without logging submitted data or server messages.
Errors before a plan can be inspected report `pending: null`, not zero.

`--max-writes N` changes the record-operation cap, not an HTTP-request allowance.
A new record normally needs a PATCH/GET and a POST; an icon validation fallback
can add one request. Reads retry transient HTTP errors at most twice, with
Retry-After capped at 30 seconds; each HTTP request has a 30-second timeout.
Mutations are never blindly retried. Run `--resume` manually in a later budget
window; no automatic loop or HTTP quota estimate is provided. Large daily plans
may need multiple invocations, and new collection waits until the saved plan finishes.

The production batch API is disabled and its configured `maxRequests` is 50;
the estimate does not assume batch requests. Multiple 200-operation runs would
need a separately verified request budget and schedule, including reads and
retries, and may still fall behind. Alternatives to assess separately are the
existing offline SQLite staging path (including its downtime, correctness, and
coordination with any pending API journal), or an explicit reduction in source
scope or collection frequency with the resulting loss of daily coverage. No
option is implemented or scheduled by this collector change.

The default journal is
`${XDG_STATE_HOME:-~/.local/state}/selfhosted-radar/writes.json`, configurable
with `--state-file /absolute/path/writes.json`. Its `.progress` sidecar records
the confirmed prefix, and its `.lock` prevents simultaneous use of that journal.
Intent is atomically replaced and fsynced before any mutation; each successful
operation gets an atomic, fsynced progress checkpoint. Both data files use mode
0600 and contain upstream evidence, never credentials. Keep the journal and
progress together, on durable local storage, with the same path for every run
against this instance. Completed intent remains until the next fresh plan replaces
it; copy both files first if a permanent audit copy is needed.

Do not delete or edit pending intent to unblock a failed row: repair the cause
and resume. Corrupt, foreign-target, and legacy unversioned stash files are
rejected without overwriting them. Legacy stashes require manual inspection and
migration; the new `flush()` consumes only the versioned journal. Journal locking
does not coordinate a separate host, a different journal path, or offline staging;
serialize those writers too. Do not run the offline loader while an API plan is
pending. The collector does not change schedules or deployment configuration.

**Bulk load (thousands of records).** Stage the rows into PocketBase's own
SQLite file while the instance is powered down.

```bash
phost off selfhosted --yes
sftp ... # download selfhosted/pb_data/data.db
python3 -m collector.sqlstage data.db --dump merged.json
sftp ... # upload data.db back, instance still off
phost on selfhosted
```

`pb_data` is reachable only while the instance is powered down, and it must be
written while down. Daily small updates may keep using the API.

## Rules the code relies on

- `projects`, `source_entries`, `repo_snapshots` and `surveillance_events` are
  public read, superuser write. `curation` needs a signed-in account.
- The API writer resolves existing records by the schema's unique natural keys:
  project canonical key; source plus external id; project plus snapshot date;
  project plus event type plus event date. It retains legacy record IDs, slugs
  and first-seen dates. New records use a fixed 15-character id derived from the
  collection and natural key. Replays use that same id, including after an
  ambiguous response or interruption between server commit and local checkpoint.
  Only a structured HTTP 404 permits POST after a failed PATCH; auth, validation,
  rate-limit, transport and server failures remain pending.
- Snapshots and events are immutable first observations for their unique key,
  including on same-day reruns. Event details and `is_read` are preserved. The
  schema permits only one event per type/project/day, so multiple releases that
  day share the first event. Source rows refresh their raw payload and last-seen
  date without deletion; restricted-source collection retains known source
  membership. Project updates include release/archive metadata and sync dates,
  and event intent is recorded before those changes can erase the old state.
- An HTTP 400 whose only field error is an invalid optional `icon_url`
  (`validation_is_url` or `validation_invalid_url`) retries once with that field
  omitted. PATCH preserves the previous icon; creation uses the empty default.
  Original icon input stays in the journal and source raw payloads are unchanged.
  Other validation errors fail the operation and cannot trigger creation.
- Upstream descriptions are untrusted text: the frontend renders only through
  `textContent` or escaped markup, and URLs go through an http(s) check.
- No health ping or uptime monitor: the instance is left to idle on its own.

## Stage 2 (not implemented here)

GitHub Trending and Popular collection. The `sources` column and
`collector/sources.py` are already source-agnostic, and
`surveillance_events.source` accepts `gh_trending` and `gh_popular`.

## Tests

```bash
netless python3 -m pytest tests -q
```

Writer tests use an in-memory transport with network access denied. They cover
snapshot persistence, rerun idempotency, more than 600 rows, failure accounting
and CLI exits, bounded retries, journal locking, and ambiguous/interrupted writes.
