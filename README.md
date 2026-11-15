# Self-Hosted Radar

The CLI captures the journal generation before fetching sources. Publication
checks that generation again under the writer lock. Concurrent publication
rejects the fetched input without writing it; rerun the original command to
fetch fresh data. This prevents older collected input from overwriting newer
project releases and source payloads.

A checkpoint failure before file replacement leaves the prior cursor for replay.
If replacement succeeds but the directory sync fails, the visible cursor and
confirmed server writes are reported together with `checkpoint_durability:
unknown`. The run fails; reload the journal before deciding what to replay.

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

Two loading paths exist: a durable API collector and a legacy offline SQLite
loader. Record capacity and HTTP quotas are separate concerns; neither a large
record cap nor batching alone proves that a run fits the available request budget.

**API update (the daily path).** The writer saves the complete plan locally,
then confirms at most 200 record operations per invocation, serial by default. It never
drops a collection because it contains more than 600 rows.

```bash
source ~/dotfiles/claude/hooks/agent-env.sh
python3 -m collector.sync --dry-run     # fetch and merge, write nothing
python3 -m collector.sync               # fetch, merge, write
python3 -m collector.sync --resume      # finish saved work without fetching
# Opt-in throughput, after enabling/verifying the server's transactional batch API:
python3 -m collector.sync --max-writes 20000 --batch-size 50
```

A systemd user timer runs this daily at 04:20 on the UTC host, **13:20 KST**
(`selfhosted-radar-collect.timer`). The planned service runtime is
`--max-writes 20000 --batch-size 50`; the CLI's conservative defaults remain
200 records and serial writes.

The weekly runner is planned to retain its existing filename for compatibility
but use the same API pipeline, **without powering down the database**:

```bash
python3 -m collector.sync --sources awesome,selfhst,gh_trending,gh_popular --max-writes 60000 --batch-size 50
```

Daily and weekly runners must use the same durable `--state-file` (or the same
default state directory), so the shared journal lock excludes concurrent drains.
These runner changes are integrated separately from the collector. Pending intent
still wins over new input: no upstream source is fetched until that plan completes.
A normal weekly invocation then collects its requested source set in the same
invocation if record budget remains, using only the remainder of its single cap.

The normal command also resumes pending work before fetching anything new.
Resumed intent retains its original date and event details. A normal invocation
that finishes it collects and plans fresh requested input with the remaining
record budget; it never replans while pending writes remain or after exhausting
the cap. Explicit `--resume` only drains saved intent and never collects.
A normal run exits **0** only if its final plan is complete, its date matches the
requested date, its recorded source scope covers the requested scope, and its
estimated daily baseline fits one invocation. It exits
**1** for failed or pending writes, a stale plan, insufficient daily capacity, or
unknown capacity. `complete` refers only to journal completion; `success` and
`failure_reasons` describe the normal daily result. Journals persist
`requested_sources`, including requested adapters that returned no rows;
`required_sources` and `scope_assessment` report coverage of the invocation's
request. Old journals without source metadata have unknown scope and cannot
establish normal same-scope success without a fresh collection. Explicit `--resume` exits **0**
when the saved plan completes, even when its date is stale or its daily capacity
is insufficient. A successful dry run exits **0** and reports the estimate.
Invalid arguments or missing credentials exit **2**. A record-cap stop is
incomplete even with zero errors.

`freshness` is `current` only for a completed plan dated for the requested day,
`stale` when its date differs, and `incomplete` for an unfinished plan dated for
that day. It describes date/completion only; source coverage is separately
reported by `scope_assessment`. `--today` sets the requested date; without it,
the collector uses its existing local-date default. Merely finishing yesterday's
journal is not a successful daily collection; normal mode must also complete
the fresh plan, while explicit resume only confirms the old plan.
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
`attempted` counts this invocation's selected record operations (the whole slice
in batch mode, including immutable rows confirmed by pre-read), `planned` counts the
whole journal, `confirmed_total` is its visible checkpoint prefix (durability is
unconfirmed when `checkpoint_durability` is `unknown`), and `pending` plus
`pending_<collection>` account for every remaining operation. `errors` counts
failed operations or slices in this invocation; the writer stops at the first failure.
`failure` identifies the failed operation (the first operation in a failed batch
slice) and exception type, with the
HTTP status when available, without logging submitted data or server messages.
Errors before a plan can be inspected report `pending: null`, not zero.
When normal mode collects after a drain, `drained_plan` reports the completed
older generation and `collected_after_resume` is true. Top-level attempt, error,
fallback, and record counters cover the whole invocation; `planned`,
`confirmed_total`, and `pending` refer to the final generation. The combined
`attempted` count never exceeds the single `--max-writes` cap.
If fresh collection or planning fails after a successful drain, the failure JSON
retains those confirmed counters and `drained_plan`, identifies the failing
`phase`, and reports fresh pending work as unknown (`pending`/`fresh_pending` null)
rather than discarding completed work or claiming success. Planning inventories
must be object responses with an `items` list; malformed inventories cannot be
treated as empty collections or replace the saved journal.

`--max-writes N` changes the record-operation cap, not an HTTP-request allowance.
In default serial mode a new record normally needs a PATCH/GET and a POST; an icon validation fallback
can add one request. Reads retry transient HTTP errors at most twice, with
Retry-After capped at 30 seconds; each HTTP request has a 30-second timeout.
Mutations are never blindly retried. Run `--resume` manually in a later budget
window; no automatic loop or HTTP quota estimate is provided. Large daily plans
may need multiple invocations, and new collection waits until the saved plan finishes.

`--batch-size` accepts integers from **1 to 50**, default **1** (the existing serial
path). Values above 1 opt into transactional `POST /api/batch`. Each contiguous
journal slice contains at most that many records and never exceeds the remaining
`--max-writes` allowance; even a one-record tail uses the batch API. Slices also
stay within **4 MiB encoded request bodies**, leaving headroom below the hosted
5 MiB cap. The size includes the envelope, JSON separators and default ASCII
escaping, using the same encoder as the actual request. Large slices split into
smaller contiguous slices before sending; records are never truncated. A single
oversize record fails with `BatchSizeError` and stays pending without cursor
advance or a batch request. Mutable
projects and source entries use batch `PUT` with the journal's fixed ID in the
body. Before submission, immutable IDs are read with one bounded filtered list
GET per immutable collection per slice (`perPage=50`, `skipTotal`, and only ID
plus natural-key fields). The response must contain an items list with unique,
requested IDs and matching keys before omitted IDs can mean absent records.
Existing immutable rows are skipped without changing their detail or `is_read`;
absent rows use create-only batch `POST`. Immutable records **never use PUT**:
a competing insert between read and
create fails the transaction instead of overwriting another writer's record.

Every submitted result must be in order, have status 200, and return the expected
ID and natural key before any progress is checkpointed. A failed transaction,
lost response, malformed/partial result, failed immutable GET, or checkpoint
failure before replacement leaves the whole slice pending and stops the invocation.
A post-replacement directory-sync failure reports visible progress with unknown
durability instead of claiming the slice remains pending. Replay rechecks immutable
IDs, so an event marked read after a committed-but-lost response stays read.
There are no blind mutation retries. Indexed, rolled-back validation failures
naming only a project's invalid optional `icon_url` permit omitting that field
from the outgoing copy, never from the journal or source evidence. Each uniquely
identified bad row can trigger this at most once, bounded by the slice size;
multiple rejected icons can therefore complete without an endless replay stall.
Nested `status: 400` and version-specific `code: 400` are accepted; conflicting
or noninteger values are rejected. All other validation failures stop without
sanitising input. Truncated HTTP response reads become sanitized runtime failures
and retain pending intent, with no automatic mutation retry.

Batching requires a separately enabled server batch API with `maxRequests` at
least the selected batch size. The parent has enabled the hosted batch API with
`maxRequests=50`, timeout 3 seconds, and `maxBodySize=5242880` (5 MiB).
Collection write rules remain locked to superusers; rate limiting remains
disabled. Collector cutover is held until the final verification pass. This
implementation does not change those server settings, and disabled rate limiting
does not establish an HTTP budget. This code does not change runtime
configuration, schedules, or deployment. The intended daily runtime setting is
`--max-writes 20000 --batch-size 50`; the CLI default remains 200/1. At 2,400
projects and one source each, 20,000 covers the 7,200-operation daily baseline;
at two sources each, the baseline is 9,600. Events still need headroom. This is
**record capacity only**, not a verified HTTP quota, time budget, or throughput
guarantee: authentication, paginated planning reads, immutable pre-reads, retries,
and batches all consume requests. Offline SQLite staging remains a separate path
requiring downtime and coordination with pending API intent.

The default journal is
`${XDG_STATE_HOME:-~/.local/state}/selfhosted-radar/writes.json`, configurable
with `--state-file /absolute/path/writes.json`. Its `.progress` sidecar records
the confirmed prefix, and its `.lock` prevents simultaneous use of that journal.
Intent is atomically replaced and fsynced before any mutation; each successful
serial operation or fully confirmed batch slice gets one atomic, fsynced,
generation-bound progress checkpoint. Both data files use mode
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

**Legacy offline bulk load (manual only).** The existing loader stages rows into
PocketBase's own SQLite file while the instance is powered down. It is unsafe to
run concurrently with a durable API journal and does not participate in that
journal's lock. It must not remain the scheduled weekly path alongside the API
collector. The replacement weekly API pipeline above does not power-cycle the DB.

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

## Additional sources

The collector also has GitHub Trending and Popular adapters (`gh_trending` and
`gh_popular`). The weekly API command includes both alongside the two directories;
the `sources` column and `surveillance_events.source` retain those source names.

## Tests

```bash
netless python3 -m pytest tests -q
```

Writer tests use an in-memory transport with network access denied. They cover
snapshot persistence, rerun idempotency, more than 600 rows, failure accounting
and CLI exits, bounded retries, journal locking, and ambiguous/interrupted writes.
Transactional batch tests cover 50+1 slicing, record caps, atomic rollback,
immutable insert races and replay/read-state preservation, malformed results and
checkpoints, failed immutable reads, and narrowly indexed icon fallback.
