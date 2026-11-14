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

**Small update (the daily path).** The API writer handles a few hundred writes.

```bash
source ~/dotfiles/claude/hooks/agent-env.sh
python3 -m collector.sync --dry-run     # fetch and merge, write nothing
python3 -m collector.sync               # fetch, merge, write
```

A systemd user timer runs this nightly at 04:20 KST
(`selfhosted-radar-collect.timer`).

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
- Upstream descriptions are untrusted text: the frontend renders only through
  `textContent` or escaped markup, and URLs go through an http(s) check.
- No health ping or uptime monitor: the instance is left to idle on its own.

## Stage 2 (not implemented here)

GitHub Trending and Popular collection. The `sources` column and
`collector/sources.py` are already source-agnostic, and
`surveillance_events.source` accepts `gh_trending` and `gh_popular`.

## Tests

```bash
python3 -m pytest tests -q
```
