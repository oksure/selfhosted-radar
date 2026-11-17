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

Daily and weekly bulk runs use `collector.offline`. Sources are captured while
the site is online. Under the API writer's existing journal lock, it stops only
`selfhosted` with the official `pockethost` CLI, freshly downloads the stopped `data.db` and any WAL/SHM,
and preserves them locally. SQLite's backup API builds a separate staging DB,
including WAL content. Pending intent is replayed with its exact saved IDs, then
fresh requested input is planned through `SQLiteInventory` and applied through
`apply_journal`. Neither SQL replay nor staging advances journal progress.

```bash
# Managed POCKETHOST_TOKEN; official CLI 4.3.0 on Node 24+ in PATH.
source ~/dotfiles/claude/hooks/agent-env.sh
python3 -m collector.offline --sources awesome,selfhst,gh_trending
bash collector/run_daily.sh
bash collector/run_weekly_sqlstage.sh  # adds gh_popular
```

Both runners share `${XDG_STATE_HOME:-~/.local/state}/selfhosted-radar/writes.json`
by default, also configurable with `--state-file`. `--max-writes` defaults to
100,000 offline operations across pending and fresh plans; it is independent of
the API's 200-operation default. Insufficient capacity fails without installing
partial fresh scope. Requested adapters that return no entries remain recorded
in the fresh scope. Only fresh scope plus installed DB plus successful restart
and health check produces `success: true` and exit 0.

Both runners source the existing `agent-env.sh`. The default transport requires
the managed `POCKETHOST_TOKEN` environment variable and resolves
`pockethost` before stopping anything. One shared platform token has read/write
access to all instances, including future instances. Instance-specific platform
tokens are not required. The Radar writer still targets `selfhosted` explicitly.
It copies the environment for the CLI child. Credentials never enter arguments,
receipts or output. The child's `POCKETHOST_MOTHERSHIP_URL` is pinned to
`https://mothership.pockethost.io`, overriding inherited current or legacy URL
settings. There is no SFTP transport or fallback. A fresh run confirms ON
power before and after collection and refuses to turn on an intentionally OFF
instance. Recovery may restore only its recorded original ON state. The source
and tests never read key contents. Incomplete receipts from the legacy transport
fail with a target mismatch; completed receipts remain accepted.

The staged DB passes SQLite integrity checks, uses DELETE journal mode, and is
uploaded to a unique temporary name. A downloaded copy must match its SHA-256
before installation. The original main DB set is renamed to retained remote
backups; old WAL/SHM cannot attach to the new DB. Because the CLI has no rename,
each logical move requires a stopped instance and an absent destination, pulls
the source into a fresh private directory, hashes and fsyncs it, uploads that
single file, then freshly pulls and verifies both destination and unchanged
source before removing only the source. An exact missing-file probe confirms
removal. The official server writes a `.ph-upload` temporary file and atomically
publishes the completed PUT, so a partially uploaded DB is never published.
Retained `.original` and `.rejected` copies cannot be move sources or deleted.
Only canonical DB files and unique `.upload`/`.restore` staging files can be
removed by a verified move. All listings probe exact canonical or receipt-derived
filenames through the official HTTPS file metadata endpoint. CLI 4.3 mislabels
missing dotted filenames as instance/auth failures. The adapter therefore reads
the original HTTP status and exact missing-path message, and rejects truncated
metadata. Authorization stays on the initial request only during redirects.
Directory uploads, recursive operations and deployment are never used.
Another download/hash check verifies installation. Only then are
the old generation checkpointed, fresh intent published and fresh progress
checkpointed, using the API writer's durable primitives. `auxiliary.db` and all
other files are untouched. This path never constructs a record API client or
authenticates with PocketBase superuser credentials. A scoped platform token
authorizes file metadata and CLI operations. `/api/health` needs no credential.

`writes.json.offline.json` records the exact old/fresh plans and installation
state for crash reconciliation. Per-run captures, original DB sets, staged DBs,
hash-check copies and receipts remain under the journal's `offline/<run-id>/`
directory (or `--backup-dir`). Receipt and capture JSON files use mode 0600 and
run directories use 0700. No backup is deleted. Keep these artifacts private and
retain them until the operator explicitly chooses a retention policy.

Read, hash or checkpoint failures trigger restoration of the original DB set and
journal cursor, then restart/health verification. Failed mutating CLI commands
record `uncertain` and stop automatic work: the server may still be writing, so
no conflicting rollback or restart is attempted. A tiny credential-free
`writes.json.cli-mutation.json` marker is fsynced before every mutation and cleared
only after successful acknowledgment. A process killed during a command therefore
also requires operator reconciliation, including in a fresh process. Lost
responses leave the retained original copies available for that reconciliation.
Other SIGTERM interruptions enter the cleanup path. An uncatchable kill outside
a pending mutation is reconciled from the saved receipt on the next invocation:
verify the installed hash before publishing progress, or
restore a partial installation. After a checkpointed installation, startup
failure retains that installation and its progress; the next invocation retries
startup/health before collecting fresh input. A changed installed hash or failed
rollback fails closed for operator inspection. Network outages can prevent
cleanup; the receipt and original copies are the recovery evidence.
Rollback persists a restart-pending boundary only after restoring the DB and
journal. Recovery after that boundary retries startup without replacing files,
preserving user changes made after a successful restart. A failed journal
restoration leaves the instance stopped.
During clean shutdown SQLite may remove WAL/SHM after the first directory
listing. Read-only capture retries up to three times with fresh listings and
hash checks, retaining every attempt. No upload, rename or checkpoint occurs
until a stable complete original set has been captured.

Local dry runs require a synthetic DB and a captured JSON object containing
`date`, `requested_sources`, and `rows` (RawApp dictionaries). They never fetch,
require a CLI or token, stop the instance, install a DB or publish journal progress.
Successful validation exits 0 with `dry_run: true` and `success: false`.

```bash
netless python3 -m collector.offline --dry-run --db /path/to/fixture/data.db \
  --from-collected /path/to/captured.json --state-file /path/to/test-writes.json
```

The orchestration transport exposes bounded file probes/download/upload/logical
moves plus stop/start/health. API-key provisioning, runtime PATH/environment,
timer enablement and live installation are separate operator actions. The daily service should allow
1800 seconds to start and 300 seconds for termination cleanup; source collection
happens before the downtime window. Serialize any other host or writer using a
different journal path.

## Optional API collector

The manual API writer saves the complete plan locally,
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

The normal API command resumes pending work before fetching anything new.
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
In default serial mode a new record normally needs a PATCH/GET and a POST; an icon
validation fallback can add one request. [PocketHost's limits](https://pockethost.io/docs/limits)
are **1,000 requests per IP per hour** and **10,000 per instance per hour**.
The client makes one unauthenticated `/api/health` GET on PocketHost before login
to acquire quota headers; there are no recurring health probes. Valid scoped
`X-PocketHost-RateLimit-Ip-Hourly-Limit/Remaining/Reset` and
`X-PocketHost-RateLimit-Instance-Hourly-Limit/Remaining/Reset` triples
govern subsequent requests, including login.
Only nonnegative integers within each scope's limit, with a Unix reset from now
through one hour ahead, are trusted. Missing or malformed headers never authorize
an unbounded wait. Retained valid quota state is debited for every HTTP attempt,
including failed requests and retries; a new valid response can tighten it.

When either validated remaining quota is **10 or less**, the next request waits
until all depleted scopes reset, plus a two-second margin. Waits use monotonic
targets computed when the headers arrive, in finite chunks of at most 60 seconds.
GETs retry transient HTTP errors at most twice. A GET 429 with validated quota
can wait for reset; other transient GET retries use Retry-After capped at 30 seconds
or a short backoff. A mutation 429 stops immediately, with its original journal
intent pending. There is no proven pre-dispatch signature allowing safe automatic
mutation replay; timeouts, lost responses, and other unknown write outcomes also
stop without another write. Explicit later resume retains the existing fixed IDs
and immutable-record checks.

`--max-runtime-seconds` defaults to **14400** for the whole invocation, including
authentication and quota waits. Each PocketBase request timeout is the smaller
of 30 seconds and the remaining budget. The collector checks the deadline before
collection, merge, planning, and subsequent record slices. Collection already in
progress is not preempted; after it returns, an expired deadline prevents the next
phase. A reset wait that cannot fit the remaining deadline fails immediately.
Confirmed counters and checkpoints survive exhaustion, and errors remain sanitized.
`http_attempts` counts every dispatched PocketBase HTTP attempt, including the
prime, login, reads, retries, and writes (not upstream source requests).
`quota_waits`, `quota_wait_seconds`, `runtime_seconds`, `max_runtime_seconds`, and
`runtime_exhausted` report the invocation's budget use. These counters do not
estimate other clients' traffic. Large plans may span several hourly windows or
invocations; new collection still waits until saved intent finishes.

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
does not coordinate a separate host or a different journal path; serialize those
writers too. `collector.offline` shares this lock and consumes pending API intent
before fresh planning. Do not use the legacy `collector.sqlstage` CLI to replace
the live DB independently of this orchestration. No schedules or deployment
configuration are changed by these modules.

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
- No scheduled health ping or uptime monitor; a PocketHost writer invocation uses
  one health GET to acquire quota before login.

## Transport failures

Requirements for the PocketHost CLI transport in `collector/offline.py`. They are not implemented yet.

1. A failed CLI command keeps its kind (timeout, non-zero exit with the code, or signal) and the last 20 lines of stderr in the run receipt. Secret values are masked before they are written.
2. The command timeout scales with the file size: 180 seconds base plus 60 seconds per 25 MB, upper limit 900 seconds. The timeout used is recorded in the receipt.
3. A `reconcile` subcommand reads the receipt and the remote state without writing. It prints one of `applied`, `not-applied` or `unknown`. It clears the pending marker only when an explicit flag is given.
4. After every mutating run the collector calls the instance health address and records the result. A failed post-check ends the run with a non-zero exit.
5. The job has its own Healthchecks slug.
6. Each rule above has a test that runs without network access (`netless`).

## Additional sources

The collector also has GitHub Trending and Popular adapters (`gh_trending` and
`gh_popular`). The weekly offline runner includes both alongside the two directories;
the `sources` column and `surveillance_events.source` retain those source names.

## Tests

```bash
netless python3 -m pytest tests -q
```

Offline orchestration tests use migration-derived SQLite fixtures and an in-memory
file transport. CLI transport tests substitute a synthetic command runner and
deny sockets and record API construction. They cover exact absence versus auth
failures, fresh private pulls, credential isolation, malformed paths/listings,
verified moves, lost responses and mutation timeouts. Orchestration tests deny
sockets, subprocesses and record API construction, and
cover online capture, old/fresh replay, scopes, WAL inclusion/removal, operation
caps, transfer/hash/rename failures, checkpoint rollback, crash receipts, startup
recovery, SIGTERM cleanup and local-only dry runs.

Writer tests use an in-memory transport with network access denied. They cover
snapshot persistence, rerun idempotency, more than 600 rows, failure accounting
and CLI exits, bounded retries, journal locking, and ambiguous/interrupted writes.
Transactional batch tests cover 50+1 slicing, record caps, atomic rollback,
immutable insert races and replay/read-state preservation, malformed results and
checkpoints, failed immutable reads, and narrowly indexed icon fallback.
Fake-clock quota tests cover startup before login, multiple quota windows, scoped
header validation, bounded waits, request accounting, deadline stops, mutation
429s and timeouts, and preserved journal intent and confirmed progress.

Local PocketBase record writes resolve the selected selfhosted automation identity through `~/dotfiles/python/pb_credentials.py` when neither explicit email nor password is supplied. Separate deployments may continue supplying a complete `SELFHOSTED_PB_EMAIL`/`SELFHOSTED_PB_PASS` pair. Partial credentials fail before authentication. This does not change the shared `POCKETHOST_TOKEN` used for platform file transfers.
