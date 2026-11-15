"""Offline bulk publication with exact journal replay and a crash receipt.

Only main-DB files cross the transport seam; no PocketBase record client is used.
"""
import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import urllib.request
import uuid

from . import pbwrite as W
from . import sources
from .merge import merge

BASE = 'https://selfhosted.pockethost.io'
DAILY = ('awesome', 'selfhst', 'gh_trending')
DBSET = ('data.db', 'data.db-wal', 'data.db-shm')


class Interrupted(RuntimeError):
  pass


class MutationUncertain(RuntimeError):
  """A server mutation may still be running; automatic recovery is unsafe."""


class ReceiptTargetMismatch(RuntimeError):
  pass


class CLITransport:
  """Official single-file CLI, scoped to the stopped Radar main DB set."""
  identity = {'transport': 'pockethost-cli', 'instance': 'selfhosted', 'remote_dir': 'pb_data'}

  def __init__(self):
    token = os.environ.get('SELFHOSTED_POCKETHOST_TOKEN')
    executable = shutil.which('pockethost')
    if not token or not executable:
      raise RuntimeError('managed SELFHOSTED_POCKETHOST_TOKEN and pockethost CLI required')
    self._env = dict(os.environ, POCKETHOST_TOKEN=token,
                     POCKETHOST_MOTHERSHIP_URL='https://mothership.pockethost.io')
    self._executable = executable
    self.uncertain = False
    self._marker = None

  def configure(self, path):
    marker = Path(str(path) + '.cli-mutation.json')
    if marker.exists():
      record = json.loads(marker.read_text())
      if (record.get('version') != 1 or record.get('transport') != self.identity
          or type(record.get('pending')) is not bool):
        raise RuntimeError('invalid CLI mutation marker')
      if record['pending']:
        self.uncertain = True
    if self.uncertain:
      raise MutationUncertain('unfinished CLI mutation requires operator reconciliation')
    self._marker = marker

  @staticmethod
  def _remote(name):
    if not isinstance(name, str) or not re.fullmatch(
        r'data\.db(?:-wal|-shm)?(?:\.offline-[0-9a-f]{32}\.(?:original|rejected|upload|restore))?', name):
      raise ValueError('only known main database filenames may be managed')
    return 'pb_data/' + name

  def _command(self, *args, mutation=False):
    if self.uncertain:
      raise MutationUncertain('CLI mutation outcome uncertain; operator reconciliation required')
    record = {'version': 1, 'transport': self.identity, 'pending': True, 'command': args[0]}
    if mutation:
      if self._marker is None:
        raise RuntimeError('CLI mutation requires a recorded journal context')
      W._save(self._marker, record)
      self.uncertain = True
    try:
      result = subprocess.run([self._executable, *args], env=self._env, text=True,
                              capture_output=True, timeout=180, check=True)
      if mutation:
        W._save(self._marker, dict(record, pending=False))
        self.uncertain = False
      return result.stdout
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, Interrupted, KeyboardInterrupt):
      if mutation:
        self.uncertain = True
        raise MutationUncertain('CLI mutation outcome uncertain; operator reconciliation required') from None
      raise

  def is_on(self):
    record = json.loads(self._command('show', 'selfhosted', '--json'))
    if (not isinstance(record, dict) or record.get('subdomain') != 'selfhosted'
        or record.get('id') != '2sb1xkspblek1f5' or type(record.get('power')) is not bool):
      raise RuntimeError('instance power state was not confirmed')
    return record['power']

  def _stopped(self):
    if self.is_on():
      raise RuntimeError('main DB access requires a stopped instance')

  def files(self, names=DBSET):
    names = tuple(names)
    paths = [(name, self._remote(name)) for name in names]
    self._stopped()
    present = set()
    for name, path in paths:
      try:
        output = self._command('files', path, '--json', '-I', 'selfhosted')
      except subprocess.CalledProcessError as exc:
        if exc.returncode == 1 and exc.stderr == f'Error: {path} not found\n' and not exc.stdout:
          continue
        raise RuntimeError('exact CLI file probe failed') from None
      records = json.loads(output)
      if not isinstance(records, list) or len(records) != 1:
        raise RuntimeError('invalid exact CLI file response')
      record = records[0]
      if (not isinstance(record, dict) or record.get('path') != path
          or record.get('isDirectory') is not False
          or type(record.get('size')) is not int or record['size'] < 0
          or type(record.get('mtime')) not in (str, int, float) or record['mtime'] == ''):
        raise RuntimeError('invalid exact CLI file response')
      present.add(name)
    return present

  def download(self, name, path):
    remote = self._remote(name)
    if name not in self.files((name,)):
      raise RuntimeError('exact CLI download source is missing')
    # A fresh private destination defeats the CLI's mtime/size skip optimization.
    with tempfile.TemporaryDirectory(prefix='radar-pull-') as directory:
      self._command('pull', remote, '--data', '-d', directory, '-I', 'selfhosted')
      source = Path(directory) / remote
      if source.is_symlink() or not source.is_file() or source.parent.is_symlink():
        raise RuntimeError('CLI pull did not produce the exact regular file')
      shutil.copyfile(source, path)
      with Path(path).open('rb') as ff:
        os.fsync(ff.fileno())

  def upload(self, path, name):
    remote = self._remote(name)
    if not name.endswith(('.upload', '.restore')) or not Path(path).is_file():
      raise ValueError('uploads require a single file and a unique staging name')
    if name in self.files((name,)):
      raise RuntimeError('upload staging destination already exists')
    self._command('upload', str(path), remote, '-I', 'selfhosted', mutation=True)

  def rename(self, old, new):
    source, destination = self._remote(old), self._remote(new)
    canonical = old if old in DBSET else new
    if canonical not in DBSET or not (
        (old == canonical and new.startswith(canonical + '.offline-')
         and new.endswith(('.original', '.rejected'))) or
        (new == canonical and old.startswith(canonical + '.offline-')
         and old.endswith(('.upload', '.restore')))):
      raise ValueError('retained files cannot be move sources')
    if new in self.files((new,)):
      raise RuntimeError('move destination already exists')
    with tempfile.TemporaryDirectory(prefix='radar-move-') as directory:
      local = Path(directory) / 'source'
      self.download(old, local)
      expected = digest(local)
      # The official server atomically publishes a completed single-file PUT.
      self._stopped()
      self._command('upload', str(local), destination, '-I', 'selfhosted', mutation=True)
      _checked_download(self, new, Path(directory) / 'destination-check', expected)
      _checked_download(self, old, Path(directory) / 'source-check', expected)
      self._stopped()
      self._command('rm', source, '-I', 'selfhosted', mutation=True)
      if old in self.files((old,)):
        raise RuntimeError('move source removal was not confirmed')

  def stop(self):
    self._command('power', 'off', 'selfhosted', '--wait', mutation=True)
    self._stopped()

  def start(self):
    self._command('power', 'on', 'selfhosted', mutation=True)

  def health(self):
    request = urllib.request.Request(BASE + '/api/health', headers=W.UA)
    with urllib.request.urlopen(request, timeout=20) as response:
      return response.status == 200 and json.load(response).get('code') == 200


class SFTPTransport:
  """Replaceable file/control seam; OpenSSH configuration owns authentication.

  No remove operation is exposed. Only the operator runs the live transport.
  """
  def __init__(self, host, remote_dir='selfhosted/pb_data'):
    if not re.fullmatch(r'[A-Za-z0-9_@.+%:-]+', host) or host.startswith('-'):
      raise ValueError('invalid SFTP destination')
    if remote_dir.rstrip('/') not in ('selfhosted/pb_data', '/selfhosted/pb_data'):
      raise ValueError('only selfhosted/pb_data may be managed')
    self.host, self.remote_dir = host, remote_dir.rstrip('/')
    self.identity = {'host': host, 'remote_dir': self.remote_dir}

  @staticmethod
  def _quote(value):
    value = str(value)
    if any(c in value for c in ('\n', '\r', '\x00', '*', '?', '[', ']')):
      raise ValueError('invalid SFTP path')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'

  def _remote(self, name):
    if PurePosixPath(name).name != name or not name.startswith('data.db'):
      raise ValueError('only the main database set may be transferred')
    return self.remote_dir + '/' + name

  def _batch(self, command):
    result = subprocess.run(['sftp', '-P', '2222', '-i', str(Path.home() / '.ssh/pockethost_ed25519'),
                             '-oIdentitiesOnly=yes', '-oIdentityAgent=none',
                             '-oBatchMode=yes', '-oConnectTimeout=20',
                             '-b', '-', self.host], input=command + '\n',
                            text=True, capture_output=True, timeout=60, check=True)
    return result.stdout

  def files(self, names=DBSET):
    output = self._batch('ls -1 ' + self._quote(self.remote_dir + '/'))
    return {PurePosixPath(line.strip()).name for line in output.splitlines()
            if not line.startswith('sftp>') and line.strip()} & set(names)

  def download(self, name, path):
    self._batch('get ' + self._quote(self._remote(name)) + ' ' + self._quote(path))

  def upload(self, path, name):
    self._batch('put ' + self._quote(path) + ' ' + self._quote(self._remote(name)))

  def rename(self, old, new):
    self._batch('rename ' + self._quote(self._remote(old)) + ' ' + self._quote(self._remote(new)))

  def stop(self):
    subprocess.run(['phost', 'off', 'selfhosted', '--yes'], check=True, timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

  def is_on(self):
    result = subprocess.run(['phost', 'status', 'selfhosted'], check=True, timeout=60,
                            text=True, capture_output=True)
    record, _ = json.JSONDecoder().raw_decode(result.stdout.lstrip())
    if record.get('subdomain') != 'selfhosted' or type(record.get('power')) is not bool:
      raise RuntimeError('instance power state was not confirmed')
    return record['power']

  def start(self):
    subprocess.run(['phost', 'on', 'selfhosted'], check=True, timeout=180,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

  def health(self):
    request = urllib.request.Request(BASE + '/api/health', headers=W.UA)
    with urllib.request.urlopen(request, timeout=20) as response:
      return response.status == 200 and json.load(response).get('code') == 200


def digest(path):
  with Path(path).open('rb') as ff:
    return hashlib.file_digest(ff, 'sha256').hexdigest()


def _copy_db(db_path, stage):
  """SQLite backup incorporates stopped WAL without opening preserved originals."""
  seed = stage.parent / ('sqlite-input-' + uuid.uuid4().hex)
  seed.mkdir(mode=0o700)
  for suffix in ('', '-wal', '-shm'):
    source = Path(str(db_path) + suffix)
    if source.exists():
      shutil.copyfile(source, seed / ('data.db' + suffix))
  with sqlite3.connect((seed / 'data.db').as_uri() + '?mode=ro', uri=True) as src:
    with sqlite3.connect(stage) as dst:
      src.backup(dst)


def _seal(stage):
  with sqlite3.connect(stage) as db:
    if db.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
      raise RuntimeError('staging database still uses WAL')
    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
      raise RuntimeError('staging integrity check failed')
  with stage.open('rb') as ff:
    os.fsync(ff.fileno())


def _checked_download(transport, name, path, expected):
  transport.download(name, path)
  if digest(path) != expected:
    raise RuntimeError('remote database hash mismatch')
  with Path(path).open('rb') as ff:
    os.fsync(ff.fileno())


def _capture_original(transport, directory):
  """Retry read-only capture if SQLite removes sidecars during clean shutdown."""
  for attempt in range(3):
    capture = directory / f'capture-{attempt}'
    capture.mkdir(mode=0o700)
    hashes = {}
    try:
      present = set(transport.files(DBSET))
      if 'data.db' not in present:
        raise RuntimeError('stopped instance has no main DB')
      for name in sorted(present):
        transport.download(name, capture / name)
        hashes[name] = digest(capture / name)
        _checked_download(transport, name, directory / f'{name}.capture-{attempt}-check',
                          hashes[name])
      if set(transport.files(DBSET)) != present:
        raise RuntimeError('DB set changed while stopping')
    except Interrupted:
      raise
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError):
      if attempt == 2:
        raise
      continue
    # All failed attempts remain untouched. Publish only a stable original set.
    original = directory / 'original'
    shutil.copytree(capture, original)
    for name in hashes:
      with (original / name).open('rb') as ff:
        os.fsync(ff.fileno())
    return present, hashes


def _receipt_path(path):
  return Path(str(path) + '.offline.json')


def _record(path, receipt, phase):
  if phase == 'uncertain' and receipt.get('phase') != 'uncertain':
    receipt['uncertain_from'] = receipt['phase']
  updated = dict(receipt, phase=phase)
  W._save(Path(receipt['directory']) / 'receipt.json', updated)
  W._save(_receipt_path(path), updated)
  receipt['phase'] = phase


def _apply(stage, journal, start):
  from .sqlstage import apply_journal
  result = apply_journal(stage, journal, start)
  if (result.get('generation') != journal['generation'] or result.get('start') != start
      or result.get('end') != len(journal['operations'])):
    raise RuntimeError('SQL receipt does not confirm the complete journal range')
  return result


def _prepare(stage, old, rows, names, today, base, max_writes):
  from .sqlstage import SQLiteInventory
  applied = []
  pending = len(old['operations']) - old['next'] if old else 0
  if pending > max_writes:
    raise RuntimeError('offline cap cannot finish pending intent')
  if pending:
    applied.append(_apply(stage, old, old['next']))
  fresh = W._plan(SQLiteInventory(stage, base), merge(rows), today, requested_sources=names)
  if pending + len(fresh['operations']) > max_writes:
    raise RuntimeError('offline cap cannot finish fresh scope')
  applied.append(_apply(stage, fresh, 0))
  _seal(stage)
  return fresh, applied


def _verify_install(transport, receipt):
  if set(transport.files()) & {'data.db-wal', 'data.db-shm'}:
    raise RuntimeError('old SQLite sidecar could attach to installed DB')
  _checked_download(transport, 'data.db', Path(receipt['directory']) / 'installed-check.db',
                    receipt['installed_sha256'])


def _checkpoint(path, receipt, base):
  """Publish generations only after stopped remote install has been verified."""
  old, fresh = receipt['old_journal'], receipt['fresh_journal']
  current = W._load(path, SimpleNamespace(base=base))
  generation = current['generation'] if current else None
  if generation == fresh['generation']:
    if current['operations'] != fresh['operations']:
      raise RuntimeError('fresh journal changed after installation')
  else:
    if generation != (old['generation'] if old else None) or (old and (
        current['operations'] != old['operations'] or current['next'] < old['next'])):
      raise RuntimeError('journal changed since offline installation')
    if old:
      W._save(Path(str(path) + '.progress'),
              {'generation': old['generation'], 'next': len(old['operations'])})
    W._save(path, fresh)
  W._save(Path(str(path) + '.progress'),
          {'generation': fresh['generation'], 'next': len(fresh['operations'])})
  _record(path, receipt, 'checkpointed')


def _restore_intent(path, receipt):
  journal = receipt['old_journal'] or receipt['fresh_journal']
  if journal is not None:
    W._save(path, journal)
    W._save(Path(str(path) + '.progress'),
            {'generation': journal['generation'], 'next': journal['next']})


def _rollback(transport, receipt):
  """Restore the entire original DB set; never delete retained backups."""
  directory = Path(receipt['directory'])
  for name, expected in receipt['original'].items():
    backup = name + '.offline-' + receipt['id'] + '.original'
    files = transport.files((*DBSET, backup))
    if backup in files:
      _checked_download(transport, backup, directory / (name + '.rollback-check'), expected)
      if name in files:
        transport.rename(name, name + '.offline-' + uuid.uuid4().hex + '.rejected')
      # Upload/verify a new rollback temp, retaining the remote original forever.
      temporary = name + '.offline-' + uuid.uuid4().hex + '.restore'
      transport.upload(directory / 'original' / name, temporary)
      _checked_download(transport, temporary, directory / (name + '.restore-check'), expected)
      transport.rename(temporary, name)
    else:
      _checked_download(transport, name, directory / (name + '.rollback-check'), expected)
  for name in set(DBSET) - set(receipt['original']):
    if name in transport.files((name,)):
      transport.rename(name, name + '.offline-' + uuid.uuid4().hex + '.rejected')
  for name, expected in receipt['original'].items():
    _checked_download(transport, name, directory / (name + '.restored-check'), expected)


def _restart(transport):
  transport.start()
  if not transport.health():
    raise RuntimeError('instance failed health check')


def _abort(transport, path, receipt):
  if getattr(transport, 'uncertain', False):
    _record(path, receipt, 'uncertain')
    raise MutationUncertain('CLI mutation outcome uncertain; operator reconciliation required')
  try:
    _abort_known(transport, path, receipt)
  except MutationUncertain:
    _record(path, receipt, 'uncertain')
    raise


def _abort_known(transport, path, receipt):
  if receipt['phase'] in ('installing', 'installed', 'rolling_back', 'rolled_back'):
    _record(path, receipt, 'rolling_back')
    _rollback(transport, receipt)
    _record(path, receipt, 'rolled_back')
    _restore_intent(path, receipt)
  # Persist the no-more-file-restoration boundary before reopening user writes.
  # If restoring intent fails, leave the instance stopped for reconciliation.
  _record(path, receipt, 'restart_pending')
  _restart(transport)
  _record(path, receipt, 'aborted')


def _recover(transport, path, receipt, base):
  try:
    if hasattr(transport, 'configure'):
      transport.configure(path)
    _recover_known(transport, path, receipt, base)
  except MutationUncertain:
    _record(path, receipt, 'uncertain')
    raise


def _recover_known(transport, path, receipt, base):
  phase = receipt['phase']
  if phase in ('complete', 'aborted'):
    return
  if receipt['target'] != base or receipt['transport'] != transport.identity:
    raise ReceiptTargetMismatch('offline receipt target mismatch')
  if phase == 'uncertain':
    raise MutationUncertain('CLI mutation outcome uncertain; operator reconciliation required')
  if receipt.get('original_power') is not True:
    raise RuntimeError('receipt does not own an originally powered-on instance')
  if phase in ('checkpointed', 'restart_pending'):
    try:
      _restart(transport)
    except Interrupted:
      _restart(transport)
      raise
    _record(path, receipt, 'complete' if phase == 'checkpointed' else 'aborted')
    return
  transport.stop()
  if phase in ('installing', 'installed'):
    try:
      _verify_install(transport, receipt)
    except Exception:
      if phase == 'installed':
        raise RuntimeError('installed hash changed; manual reconciliation required') from None
      _abort(transport, path, receipt)
    else:
      _record(path, receipt, 'installed')
      try:
        _checkpoint(path, receipt, base)
      except BaseException:
        _abort(transport, path, receipt)
        raise
      _restart(transport)
      _record(path, receipt, 'complete')
  else:
    _abort(transport, path, receipt)


def run(transport, state_file, collect_rows, names=DAILY, today=None, base=BASE,
        max_writes=100000, backup_dir=None):
  """Recover, collect online, then install pending + freshly planned operations."""
  path = W.state_path(SimpleNamespace(base=base), state_file)
  today = today or date.today().isoformat()
  names = sorted(set(names))
  date.fromisoformat(today)
  if not names or set(names) - set(sources.ADAPTERS) or max_writes <= 0:
    raise ValueError('invalid offline scope or cap')
  with W._lock(path):
    if hasattr(transport, 'configure'):
      transport.configure(path)
    rp = _receipt_path(path)
    if rp.exists():
      _recover(transport, path, json.loads(rp.read_text()), base)
    old = W._load(path, SimpleNamespace(base=base))
    if not transport.is_on():
      raise RuntimeError('instance was intentionally off; refusing scheduled publication')
    rows = list(collect_rows(names))  # Still online, even with pending API intent.
    if any(row.source not in names for row in rows):
      raise ValueError('captured source outside requested scope')
    if not transport.is_on():
      raise RuntimeError('instance powered off during collection; refusing publication')
    run_id = uuid.uuid4().hex
    directory = Path(backup_dir or path.parent / 'offline').resolve() / run_id
    directory.mkdir(parents=True, mode=0o700)
    W._save(directory / 'collected.json', {'requested_sources': names, 'date': today,
            'rows': [vars(row) for row in rows]})
    receipt = {'version': 1, 'id': run_id, 'target': base, 'transport': transport.identity,
               'directory': str(directory), 'old_journal': old, 'fresh_journal': None,
               'original': {}, 'original_power': True, 'requested_sources': names, 'date': today}
    _record(path, receipt, 'stopping')
    try:
      transport.stop()
      original = directory / 'original'
      present, receipt['original'] = _capture_original(transport, directory)
      _record(path, receipt, 'backed_up')
      stage = directory / 'staged.db'
      _copy_db(original / 'data.db', stage)
      fresh, applied = _prepare(stage, old, rows, names, today, base, max_writes)
      receipt.update(fresh_journal=fresh, applied=applied, installed_sha256=digest(stage))
      _record(path, receipt, 'staged')
      temporary = 'data.db.offline-' + run_id + '.upload'
      transport.upload(stage, temporary)
      _checked_download(transport, temporary, directory / 'upload-check.db', receipt['installed_sha256'])
      _record(path, receipt, 'installing')
      for name in sorted(present):
        transport.rename(name, name + '.offline-' + run_id + '.original')
      transport.rename(temporary, 'data.db')
      _verify_install(transport, receipt)
      _record(path, receipt, 'installed')
      _checkpoint(path, receipt, base)
    except BaseException:
      # The CLI ignores further SIGTERM once cleanup begins. This also catches
      # interruption at any rename/checkpoint boundary. Never checkpoint rollback.
      _abort(transport, path, receipt)
      raise
    # Startup can itself write SQLite. Keep verified/checkpointed content on any
    # startup failure; the next invocation retries health before fresh collection.
    try:
      _restart(transport)
    except MutationUncertain:
      _record(path, receipt, 'uncertain')
      raise
    except Interrupted:
      _restart(transport)
      raise
    _record(path, receipt, 'complete')
    return {'success': True, 'installed': True, 'healthy': True, 'fresh_scope': names,
            'generation': fresh['generation'], 'date': today, 'applied': applied,
            'receipt': str(rp), 'backup': str(directory)}


def dry_run(db_path, state_file, rows, names, today, directory, max_writes=100000, base=BASE):
  """Local fixtures only; no remote transport or journal publication."""
  path = W.state_path(SimpleNamespace(base=base), state_file)
  with W._lock(path):
    if _receipt_path(path).exists():
      receipt = json.loads(_receipt_path(path).read_text())
      if receipt['phase'] not in ('complete', 'aborted'):
        raise RuntimeError('reconcile the outstanding receipt first')
    old = W._load(path, SimpleNamespace(base=base))
    stage = Path(directory).resolve() / 'dry-run.db'
    _copy_db(Path(db_path).resolve(), stage)
    fresh, applied = _prepare(stage, old, rows, names, today, base, max_writes)
    return {'success': False, 'dry_run': True, 'installed': False, 'healthy': False,
            'requested_sources': names, 'planned': len(fresh['operations']), 'applied': applied}


def main(argv=None):
  ap = argparse.ArgumentParser(prog='collector.offline')
  ap.add_argument('--sources', default=','.join(DAILY))
  ap.add_argument('--state-file', default='')
  ap.add_argument('--today', default=date.today().isoformat())
  ap.add_argument('--max-writes', type=int, default=100000)
  ap.add_argument('--backup-dir', default='')
  ap.add_argument('--from-collected', default='')
  ap.add_argument('--dry-run', action='store_true')
  ap.add_argument('--db', default='', help='local data.db fixture for dry run')
  args = ap.parse_args(argv)
  names = sorted(set(n.strip() for n in args.sources.split(',') if n.strip()))
  if not names or set(names) - set(sources.ADAPTERS) or args.max_writes <= 0:
    ap.error('known nonempty --sources and positive --max-writes required')
  try:
    date.fromisoformat(args.today)
  except ValueError:
    ap.error('--today must be an ISO date')
  if args.dry_run and not (args.db and args.from_collected):
    ap.error('--dry-run requires --db and --from-collected; it never fetches')

  def collect_rows(scope):
    if args.from_collected:
      payload = json.loads(Path(args.from_collected).read_text())
      if sorted(payload['requested_sources']) != scope or payload['date'] != args.today:
        raise ValueError('captured input date/scope does not match request')
      return [sources.RawApp(**row) for row in payload['rows']]
    from .sync import collect
    return collect(scope)

  def terminated(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise Interrupted('termination requested')

  previous = signal.signal(signal.SIGTERM, terminated)
  try:
    if args.dry_run:
      with tempfile.TemporaryDirectory(prefix='radar-offline-') as directory:
        result = dry_run(args.db, args.state_file, collect_rows(names), names, args.today,
                         directory, args.max_writes)
    else:
      result = run(CLITransport(), args.state_file, collect_rows,
                   names, args.today, max_writes=args.max_writes, backup_dir=args.backup_dir)
    print(json.dumps(result))
    return 0
  except Exception as exc:
    print(json.dumps({'success': False, 'error_type': type(exc).__name__,
                      'receipt': str(_receipt_path(W.state_path(None, args.state_file)))}))
    return 1
  finally:
    signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
  sys.exit(main())
