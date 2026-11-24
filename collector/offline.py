"""Offline bulk publication with exact journal replay and a crash receipt.

A full PocketBase backup crosses the transport seam; only ``data.db`` is changed
for publication. No PocketBase record client is used.
"""
import argparse
import copy
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
import zipfile
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


class DBContentMismatch(RuntimeError):
  """A backup confirms that the remote database differs from the expected state."""


class ReceiptTargetMismatch(RuntimeError):
  pass


class SFTPTransport:
  """Power through phost, stopped pb_data files through PocketHost SFTP.

  PocketHost retired ph_pat_ tokens on 2026-11-21 for pbt_ tokens, and the
  npm pockethost 4.3 CLI cannot spend them, so its show/power/pull/upload/rm
  surface is gone. The v5 CLI (5.0.0-alpha.9) deploys code only and has no
  power control, so this seam splits in two: `phost` (mothership user auth)
  owns power, and SFTP owns the main database set. SFTP renames natively, so a
  move is one server-side operation instead of download/upload/remove.

  No remove operation is exposed. Only the operator runs the live transport.
  """
  identity = {'transport': 'phost-sftp', 'instance': 'selfhosted', 'remote_dir': 'selfhosted/pb_data'}

  def __init__(self, host=None, remote_dir='selfhosted/pb_data'):
    # PocketHost SFTP authenticates with the full account address as the user,
    # e.g. "name@example.com@ftp.pockethost.io". The session is chrooted at the
    # instances root and reports it as "/", so paths below stay relative.
    user = (os.environ.get('POCKETHOST_SFTP_USER') or os.environ.get('PH_EMAIL')
            or os.environ.get('ICLOUD_EMAIL') or '').strip()
    host = host or (user + '@ftp.pockethost.io')
    if (not re.fullmatch(r'[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+@ftp\.pockethost\.io', host)
        or host.startswith('-')):
      raise ValueError('invalid SFTP destination; set POCKETHOST_SFTP_USER or PH_EMAIL')
    if remote_dir.rstrip('/') not in ('selfhosted/pb_data', '/selfhosted/pb_data'):
      raise ValueError('only selfhosted/pb_data may be managed')
    self.host, self.remote_dir = host, remote_dir.rstrip('/')
    self.uncertain = False
    self._marker = None

  def configure(self, path):
    marker = Path(str(path) + '.sftp-mutation.json')
    if marker.exists():
      record = json.loads(marker.read_text())
      if (record.get('version') != 1 or record.get('transport') != self.identity
          or type(record.get('pending')) is not bool):
        raise RuntimeError('invalid SFTP mutation marker')
      if record['pending']:
        self.uncertain = True
    if self.uncertain:
      raise MutationUncertain('unfinished SFTP mutation requires operator reconciliation')
    self._marker = marker

  def _remote(self, name):
    if not isinstance(name, str) or not re.fullmatch(
        r'data\.db(?:-wal|-shm)?(?:\.offline-[0-9a-f]{32}\.(?:original|rejected|upload|restore))?', name):
      raise ValueError('only known main database filenames may be managed')
    return self.remote_dir + '/' + name

  @staticmethod
  def _quote(value):
    value = str(value)
    if any(c in value for c in ('\n', '\r', '\x00', '*', '?', '[', ']', '"', '\\')):
      raise ValueError('invalid SFTP path')
    return '"' + value + '"'

  def _sftp(self, commands, mutation=False):
    """Run one batch. A mutation is journaled first; its outcome may be unknown."""
    if self.uncertain:
      raise MutationUncertain('SFTP mutation outcome uncertain; operator reconciliation required')
    record = {'version': 1, 'transport': self.identity, 'pending': True,
              'command': commands[0].split()[0] if commands else ''}
    if mutation:
      if self._marker is None:
        raise RuntimeError('SFTP mutation requires a recorded journal context')
      W._save(self._marker, record)
      self.uncertain = True
    try:
      result = subprocess.run(
        ['sftp', '-P', '2222', '-i', str(Path.home() / '.ssh/pockethost_ed25519'),
         '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'BatchMode=yes',
         '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=20',
         '-b', '-', self.host],
        input='\n'.join(commands) + '\n', text=True, capture_output=True,
        timeout=300, check=True)
      if mutation:
        W._save(self._marker, dict(record, pending=False))
        self.uncertain = False
      return result.stdout
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, Interrupted, KeyboardInterrupt):
      if mutation:
        self.uncertain = True
        raise MutationUncertain('SFTP mutation outcome uncertain; operator reconciliation required') from None
      raise

  def _power(self, args, mutation=False):
    if self.uncertain:
      raise MutationUncertain('SFTP mutation outcome uncertain; operator reconciliation required')
    record = {'version': 1, 'transport': self.identity, 'pending': True, 'command': args[0]}
    if mutation:
      if self._marker is None:
        raise RuntimeError('power mutation requires a recorded journal context')
      W._save(self._marker, record)
      self.uncertain = True
    try:
      result = subprocess.run(['phost', *args], text=True, capture_output=True,
                              timeout=180, check=True)
      if mutation:
        W._save(self._marker, dict(record, pending=False))
        self.uncertain = False
      return result.stdout
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, Interrupted, KeyboardInterrupt):
      if mutation:
        self.uncertain = True
        raise MutationUncertain('power mutation outcome uncertain; operator reconciliation required') from None
      raise

  def is_on(self):
    result = self._power(['status', 'selfhosted'])
    try:
      record, _ = json.JSONDecoder().raw_decode(result.lstrip())
    except ValueError:
      raise RuntimeError('instance power state was not confirmed') from None
    if (not isinstance(record, dict) or record.get('subdomain') != 'selfhosted'
        or record.get('id') != '2sb1xkspblek1f5' or type(record.get('power')) is not bool):
      raise RuntimeError('instance power state was not confirmed')
    return record['power']

  def _stopped(self):
    if self.is_on():
      raise RuntimeError('main DB access requires a stopped instance')

  def files(self, names=DBSET):
    names = tuple(names)
    for name in names:
      self._remote(name)
    self._stopped()
    output = self._sftp(['ls -1 ' + self._quote(self.remote_dir + '/')])
    present = {PurePosixPath(line.strip()).name for line in output.splitlines()
               if not line.startswith('sftp>') and line.strip()}
    return present & set(names)

  def download(self, name, path):
    remote = self._remote(name)
    self._stopped()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    self._sftp(['get ' + self._quote(remote) + ' ' + self._quote(path)])
    if path.is_symlink() or not path.is_file():
      raise RuntimeError('SFTP get did not produce the exact regular file')
    with path.open('rb') as ff:
      os.fsync(ff.fileno())

  def upload(self, path, name):
    remote = self._remote(name)
    if not name.endswith(('.upload', '.restore')) or not Path(path).is_file():
      raise ValueError('uploads require a single file and a unique staging name')
    if name in self.files((name,)):
      raise RuntimeError('upload staging destination already exists')
    self._sftp(['put ' + self._quote(path) + ' ' + self._quote(remote)],
               mutation=True)

  def rename(self, old, new):
    source, destination = self._remote(old), self._remote(new)
    canonical = old if old in DBSET else new
    if canonical not in DBSET or not (
        (old == canonical and new.startswith(canonical + '.offline-')
         and new.endswith(('.original', '.rejected'))) or
        (new == canonical and old.startswith(canonical + '.offline-')
         and old.endswith(('.upload', '.restore')))):
      raise ValueError('retained files cannot be move sources')
    files = self.files((old, new))
    if old not in files:
      raise RuntimeError('move source is missing')
    if new in files:
      raise RuntimeError('move destination already exists')
    # SFTP rename is server-side and atomic; a failed batch leaves the source.
    self._sftp(['rename ' + self._quote(source) + ' '
                + self._quote(destination)], mutation=True)

  def stop(self):
    self._power(['off', 'selfhosted', '--yes'], mutation=True)
    self._stopped()

  def start(self):
    self._power(['on', 'selfhosted'], mutation=True)

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


def _run_sftp_legacy(transport, state_file, collect_rows, names=DAILY, today=None, base=BASE,
                     max_writes=100000, backup_dir=None):
  """Retired SFTP implementation retained only for historical receipt parsing."""
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


def _backup_db(archive, destination):
  """Extract exactly one safe root data.db from a PocketBase backup ZIP."""
  destination = Path(destination)
  destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
  with zipfile.ZipFile(archive) as src:
    infos = src.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)) or src.testzip() is not None:
      raise RuntimeError('backup ZIP has duplicate or corrupt members')
    for info in infos:
      parts = PurePosixPath(info.filename).parts
      if (info.filename.startswith('/') or '\\' in info.filename or '..' in parts
          or (info.external_attr >> 16) & 0o170000 == 0o120000):
        raise RuntimeError('backup ZIP contains an unsafe member')
    matches = [info for info in infos if info.filename == 'data.db']
    if len(matches) != 1:
      raise RuntimeError('backup ZIP must contain exactly one root data.db')
    with src.open(matches[0]) as data, destination.open('xb') as out:
      shutil.copyfileobj(data, out, 1 << 20)
      out.flush()
      os.fsync(out.fileno())
  with sqlite3.connect(destination.resolve().as_uri() + '?mode=ro&immutable=1', uri=True) as db:
    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
      raise RuntimeError('backup database integrity check failed')
  return destination


def _replace_backup_db(archive, staged_db, destination):
  """Prepare a fresh same-instance backup with only data.db replaced."""
  with zipfile.ZipFile(archive) as src, zipfile.ZipFile(destination, 'x') as dst:
    dst.comment = src.comment
    infos = src.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)) or 'data.db' not in names or src.testzip() is not None:
      raise RuntimeError('source backup ZIP is invalid or lacks data.db')
    replaced = 0
    for info in infos:
      parts = PurePosixPath(info.filename).parts
      if (info.filename.startswith('/') or '\\' in info.filename or '..' in parts
          or (info.external_attr >> 16) & 0o170000 == 0o120000):
        raise RuntimeError('source backup ZIP contains an unsafe member')
      clone = copy.copy(info)
      with dst.open(clone, 'w') as out:
        if info.filename == 'data.db':
          with Path(staged_db).open('rb') as data:
            shutil.copyfileobj(data, out, 1 << 20)
          replaced += 1
        else:
          with src.open(info) as data:
            shutil.copyfileobj(data, out, 1 << 20)
    if replaced != 1:
      raise RuntimeError('source backup ZIP data.db is ambiguous')
  if not zipfile.is_zipfile(destination):
    raise RuntimeError('prepared backup ZIP failed validation')
  with zipfile.ZipFile(destination) as archive_file:
    if archive_file.testzip() is not None:
      raise RuntimeError('prepared backup ZIP has a CRC error')


def _logical_db_digest(path):
  """Hash a deterministic SQLite dump so PocketBase startup page rewrites do not false-fail."""
  uri = Path(path).resolve().as_uri() + '?mode=ro&immutable=1'
  digestor = hashlib.sha256()
  with sqlite3.connect(uri, uri=True) as db:
    for line in db.iterdump():
      digestor.update(line.encode('utf-8') + b'\n')
  return digestor.hexdigest()


def _fresh_snapshot(transport, directory, name):
  key = transport.create_backup(name)
  local = transport.download(key, directory / (name + '.zip'))
  repeated = transport.download(key, directory / (name + '.repeat.zip'))
  expected = digest(local)
  if digest(repeated) != expected:
    raise RuntimeError('backup download hash mismatch')
  return key, local, expected


def _verify_current_db(transport, directory, name, expected):
  _, snapshot, _ = _fresh_snapshot(transport, directory, name)
  data_db = _backup_db(snapshot, directory / (name + '-data.db'))
  if _logical_db_digest(data_db) != expected:
    raise DBContentMismatch('restored database contents do not match the expected state')


def _rollback_backup(transport, directory, receipt):
  original_key = receipt.get('original_backup_key')
  if not original_key:
    raise RuntimeError('original remote backup key is missing')
  attempts = receipt.get('rollback_restore_attempts', 0)
  while True:
    if attempts >= 2:
      raise RuntimeError('original backup restore retry limit reached')
    attempts += 1
    receipt['rollback_restore_attempts'] = attempts
    _record(Path(receipt['state_file']), receipt, 'rolling_back')
    try:
      transport.restore(original_key)
    except W.PBError as exc:
      receipt['rollback_restore_status'] = exc.status
      _record(Path(receipt['state_file']), receipt, 'rolling_back')
      if exc.status != 400 or attempts >= 2 or not transport.wait_healthy():
        raise
      # PocketBase can reject a restore while the preceding restore is restarting.
      continue
    receipt['rollback_restore_confirmed'] = True
    receipt.pop('rollback_restore_status', None)
    _record(Path(receipt['state_file']), receipt, 'rolling_back')
    break
  if not transport.wait_healthy():
    raise RuntimeError('original backup restore did not become healthy')
  _verify_current_db(transport, directory, 'radar-' + receipt['id'] + '-rollback-check.zip',
                     receipt['original_db_content_sha256'])
  _record(Path(receipt['state_file']), receipt, 'rolled_back')
  _restore_intent(Path(receipt['state_file']), receipt)
  _record(Path(receipt['state_file']), receipt, 'aborted')


def _recover_backup_api(transport, path, receipt, base):
  phase = receipt.get('phase')
  if phase in ('complete', 'aborted'):
    return
  if receipt.get('target') != base or receipt.get('transport') != transport.identity:
    raise ReceiptTargetMismatch('offline receipt target mismatch')
  if phase == 'uncertain' or transport.uncertain:
    raise MutationUncertain('PocketBase mutation outcome uncertain; operator reconciliation required')
  if phase in ('capturing', 'backed_up', 'staged'):
    _restore_intent(path, receipt)
    _record(path, receipt, 'aborted')
    return
  if phase == 'rolling_back':
    attempts = receipt.get('rollback_restore_attempts', 0)
    if type(attempts) is not int or attempts < 0:
      raise RuntimeError('invalid rollback restore attempt count')
    retry = (not receipt.get('rollback_restore_confirmed') and attempts < 2
             and getattr(transport, 'rollback_retryable', lambda key: False)(
               receipt.get('original_backup_key')))
    if not transport.wait_healthy():
      raise RuntimeError('original backup rollback is awaiting health recovery')
    try:
      _verify_current_db(transport, Path(receipt['directory']),
                         'radar-' + receipt['id'] + '-rollback-recovery-check.zip',
                         receipt['original_db_content_sha256'])
    except MutationUncertain:
      raise
    except DBContentMismatch:
      if retry:
        _rollback_backup(transport, Path(receipt['directory']), receipt)
        return
      raise RuntimeError('original backup rollback does not match the retained DB; manual reconciliation required') from None
    except Exception:
      raise RuntimeError('original backup rollback verification did not complete') from None
    _record(path, receipt, 'rolled_back')
    phase = 'rolled_back'
  if phase == 'rolled_back':
    _restore_intent(path, receipt)
    _record(path, receipt, 'aborted')
    return
  if phase not in ('installing', 'installed', 'checkpointed'):
    raise RuntimeError('offline receipt phase requires manual reconciliation')
  try:
    if not transport.wait_healthy():
      raise RuntimeError('instance health is not confirmed')
    _verify_current_db(transport, Path(receipt['directory']),
                       'radar-' + receipt['id'] + '-recovery-check.zip',
                       receipt['installed_content_sha256'])
  except Exception:
    if transport.uncertain:
      raise MutationUncertain('recovery mutation outcome uncertain; operator reconciliation required') from None
    _rollback_backup(transport, Path(receipt['directory']), receipt)
    return
  if phase != 'checkpointed':
    _record(path, receipt, 'installed')
    _checkpoint(path, receipt, base)
  _record(path, receipt, 'complete')


def run(transport, state_file, collect_rows, names=DAILY, today=None, base=BASE,
        max_writes=100000, backup_dir=None):
  """Stage from a retained API backup and restore it without powering off."""
  path = W.state_path(SimpleNamespace(base=base), state_file)
  today = today or date.today().isoformat()
  names = sorted(set(names))
  date.fromisoformat(today)
  if not names or set(names) - set(sources.ADAPTERS) or max_writes <= 0:
    raise ValueError('invalid offline scope or cap')
  with W._lock(path):
    transport.configure(path)
    rp = _receipt_path(path)
    if rp.exists():
      _recover_backup_api(transport, path, json.loads(rp.read_text()), base)
      if getattr(transport, 'r2_active', False):
        transport.clear_r2()
    old = W._load(path, SimpleNamespace(base=base))
    if not transport.is_on():
      raise RuntimeError('instance was intentionally off; refusing scheduled publication')
    rows = list(collect_rows(names))
    if any(row.source not in names for row in rows):
      raise ValueError('captured source outside requested scope')
    if not transport.is_on():
      raise RuntimeError('instance powered off during collection; refusing publication')
    run_id = uuid.uuid4().hex
    directory = Path(backup_dir or path.parent / 'offline').resolve() / run_id
    directory.mkdir(parents=True, mode=0o700)
    W._save(directory / 'collected.json', {'requested_sources': names, 'date': today,
            'rows': [vars(row) for row in rows]})
    receipt = {'version': 2, 'id': run_id, 'target': base, 'transport': transport.identity,
               'directory': str(directory), 'state_file': str(path), 'old_journal': old,
               'fresh_journal': None, 'original_power': True, 'requested_sources': names,
               'date': today}
    _record(path, receipt, 'capturing')
    try:
      original_key, original_zip, original_zip_sha = _fresh_snapshot(
        transport, directory, 'radar-' + run_id + '-original.zip')
      original_db = _backup_db(original_zip, directory / 'original-data.db')
      receipt.update(original_backup_key=original_key, original_zip_sha256=original_zip_sha,
                     original_db_sha256=digest(original_db),
                     original_db_content_sha256=_logical_db_digest(original_db))
      _record(path, receipt, 'backed_up')
      stage = directory / 'staged.db'
      _copy_db(original_db, stage)
      fresh, applied = _prepare(stage, old, rows, names, today, base, max_writes)
      staged_zip = directory / 'staged-backup.zip'
      _replace_backup_db(original_zip, stage, staged_zip)
      receipt.update(fresh_journal=fresh, applied=applied, installed_sha256=digest(stage),
                     installed_content_sha256=_logical_db_digest(stage),
                     staged_zip_sha256=digest(staged_zip))
      _record(path, receipt, 'staged')
      if staged_zip.stat().st_size > transport.upload_cap:
        transport.enable_r2(original_key, original_zip, directory)
      staged_key = transport.upload_backup(staged_zip, 'radar-' + run_id + '-staged.zip')
      upload_check = transport.download(staged_key, directory / 'upload-check.zip')
      if digest(upload_check) != receipt['staged_zip_sha256']:
        raise RuntimeError('uploaded backup hash mismatch')
      receipt['staged_backup_key'] = staged_key
      _record(path, receipt, 'installing')
      transport.restore(staged_key)
      if not transport.wait_healthy():
        raise RuntimeError('restored instance health check failed')
      if not transport.is_on():
        raise RuntimeError('instance power state changed during backup restore')
      _verify_current_db(transport, directory, 'radar-' + run_id + '-installed-check.zip',
                         receipt['installed_content_sha256'])
      if getattr(transport, 'r2_active', False):
        transport.clear_r2()
      _record(path, receipt, 'installed')
      _checkpoint(path, receipt, base)
      _record(path, receipt, 'complete')
      return {'success': True, 'installed': True, 'healthy': True, 'fresh_scope': names,
              'generation': fresh['generation'], 'date': today, 'applied': applied,
              'receipt': str(rp), 'backup': str(directory), 'original_backup': original_key}
    except BaseException:
      if transport.uncertain:
        _record(path, receipt, 'uncertain')
      elif receipt.get('phase') in ('installing', 'installed', 'checkpointed'):
        try:
          _rollback_backup(transport, directory, receipt)
          if getattr(transport, 'r2_active', False):
            transport.clear_r2()
        except BaseException:
          if transport.uncertain:
            _record(path, receipt, 'uncertain')
            raise MutationUncertain('rollback or R2 cleanup outcome uncertain; inspect retained backups') from None
          if receipt.get('phase') in ('rolling_back', 'rolled_back'):
            raise RuntimeError('original backup rollback is awaiting health or journal recovery') from None
          if receipt.get('phase') == 'aborted' and getattr(transport, 'r2_active', False):
            raise RuntimeError('rollback completed; temporary R2 settings still need cleanup') from None
          _record(path, receipt, 'uncertain')
          raise MutationUncertain('rollback reconciliation failed; inspect retained backups') from None
      else:
        if getattr(transport, 'r2_active', False):
          try:
            transport.clear_r2()
          except BaseException:
            _record(path, receipt, 'uncertain')
            raise MutationUncertain('temporary R2 credentials could not be cleared') from None
        _restore_intent(path, receipt)
        _record(path, receipt, 'aborted')
      raise


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
      from .pockethost_backup import BackupAPITransport
      result = run(BackupAPITransport(), args.state_file, collect_rows,
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
