# CI code deployment

CI uses Node 24 and the official `pockethost@4.3.0` CLI. Configure the repository's
`POCKETHOST_TOKEN` Actions secret with the existing common all-instances read/write
token. No new token or SSH credentials are needed. Instances deploy serially;
the per-instance concurrency group queues deployments without cancelling them.

Run locally (this publishes changes):
`python3 .github/deploy/deploy-code.py INSTANCE SOURCE_ROOT`.
The source must contain real directories named `pb_public`, `pb_hooks`, and
`pb_migrations`. CI supplies the appropriate source and instance.

The helper copies the three folders to a private snapshot and anchors both CLI
working directories with a private `.pockethostconfig`, explicit
`POCKETHOST_PROJECT_DIR` and `INIT_CWD`. It never writes a config to the repository.
It stages the official CLI's exact local deployment set with
`--out --no-node-modules`. It rejects symlinks and files outside the
three folders, including files added by the CLI's broader default includes.
Provider-owned `_ph_*` hooks and `*_pockethost_*` migrations are reference-only
and excluded from uploads and deletions.
Empty code sets (including folders containing only ignored `.gitkeep` files)
remain authoritative, subject to the same remote deletion guard.
It hashes the staged files and submits a read-only deploy check. A successful
response must contain both a `missing` hash list and an explicit `toDelete`
list. Older servers omitting that list fail closed. Obsolete files within the
three folders may be deleted; any other remote deletion blocks publication.
Resolve remote extras with a separate reviewed operation; there is no fallback.

Only after this guard does the CLI deploy the same staged files with `--yes`.
Installed packages remain unmanaged through `--no-node-modules`. The helper
never includes database files. Original power is read through `show --json`
and restored after an acknowledged publish result. An unknown CLI outcome stops
without an automatic power change. Health checks are
bounded and run only when the instance was originally powered on. A CLI startup
failure message fails deployment even when its exit status is zero.

The production endpoint is pinned in the child environment. Credentials are
passed only through the environment or an HTTPS authorization header, and CLI
output is captured rather than forwarded. The read-only scope check and the
CLI's own check/commit are separate requests: PocketHost exposes no conditional
commit here. Keep other remote writers quiescent during deployment; the workflow
concurrency group serializes only this repository's workflows. GitHub concurrency
also permits replacement of a pending run by a newer pending run.

Offline checks:
`netless python3 -m unittest discover -s .github/deploy -p 'test_*.py' -v`.
These use synthetic CLI/HTTP responses and exercise no real instance.
Actual token access, each target's response contract, and post-deployment live
acceptance must be checked by the operator before enabling the migration.
