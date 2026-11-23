"""Offline orchestration uses synthetic DBs and an in-memory file transport."""
import json
from pathlib import Path
import re
import signal
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from collector import offline as O, pbwrite as W, sqlstage as S
from collector.merge import merge
from collector.sources import RawApp

TODAY = '2026-11-15'
BASE = 'https://radar.example.invalid'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
  def blocked(*args, **kwargs):
    raise AssertionError('unexpected network or subprocess')
  monkeypatch.setattr(socket.socket, 'connect', blocked)
  monkeypatch.setattr(socket, 'create_connection', blocked)
  monkeypatch.setattr(O.urllib.request, 'urlopen', blocked)
  monkeypatch.setattr(O.subprocess, 'run', blocked)
  monkeypatch.setattr(W.PB, '__init__', blocked)
  monkeypatch.setattr(W.PB, '_req', blocked)


def schema(path):
  with sqlite3.connect(path) as db:
    root = Path(__file__).resolve().parents[1] / 'pb_migrations'
    for source in sorted(root.glob('*.js')):
      for block in re.findall(r'new Collection\(\{(.*?)\n  \}\)', source.read_text(), re.S):
        coll = re.search(r"name: '([^']+)'", block)[1]
        if coll not in W.KEYS:
          continue
        columns = ['id TEXT PRIMARY KEY NOT NULL']
        fields = block.split('fields: [', 1)[1].split('indexes:', 1)[0]
        for field in re.findall(r'\{(.*?)\}', fields, re.S):
          name = re.search(r"name: '([^']+)'", field)[1]
          kind = re.search(r"type: '([^']+)'", field)[1]
          sql = {'number': 'NUMERIC NOT NULL DEFAULT 0', 'bool': 'BOOLEAN NOT NULL DEFAULT FALSE',
                 'json': 'JSON DEFAULT NULL'}.get(kind, "TEXT NOT NULL DEFAULT ''")
          columns.append(f'"{name}" {sql}')
        db.execute(f'CREATE TABLE "{coll}" ({", ".join(columns)})')
        for index in re.findall(r"'(CREATE (?:UNIQUE )?INDEX [^']+)'", block):
          db.execute(index)


def rows(names, n=1):
  return [RawApp(source=name, external_id=f'app-{i}', name=f'App {i}',
                 repo_url=f'https://github.com/owner/app-{i}', stars=100 + i)
          for i in range(n) for name in names]


class Files:
  identity = {'fixture': 'selfhosted'}

  def __init__(self, path):
    self.data = {'data.db': path.read_bytes(), 'auxiliary.db': b'NEVER TOUCH'}
    self.on, self.calls, self.failure = True, [], None

  def call(self, kind, *args):
    self.calls.append((kind, *args))
    if self.failure:
      self.failure(kind, *args)

  def stop(self):
    self.call('stop')
    self.on = False

  def is_on(self):
    self.call('power')
    return self.on

  def start(self):
    self.call('start')
    self.on = True

  def health(self):
    self.call('health')
    return self.on

  def files(self, names=O.DBSET):
    assert not self.on
    self.call('files')
    return set(self.data) & set(names)

  def download(self, name, path):
    assert not self.on and name.startswith('data.db')
    self.call('download', name)
    Path(path).write_bytes(self.data[name])

  def upload(self, path, name):
    assert not self.on and name.startswith('data.db')
    self.call('upload', name)
    self.data[name] = Path(path).read_bytes()

  def rename(self, old, new):
    assert not self.on and new not in self.data
    self.call('rename', old, new)
    self.data[new] = self.data.pop(old)


@pytest.fixture
def setup(tmp_path):
  db = tmp_path / 'data.db'
  schema(db)
  return db, tmp_path / 'writes.json', Files(db)


def run(setup, names=O.DAILY, n=1, **kwargs):
  db, state, remote = setup
  def collect(scope):
    assert remote.on  # Capture must precede downtime, including pending replay.
    remote.call('collect', tuple(scope))
    return rows(scope, n)
  return O.run(remote, state, collect, names, TODAY, BASE, **kwargs)


def saved(state):
  return W._load(state, SimpleNamespace(base=BASE))


def receipt(state):
  return json.loads(O._receipt_path(state).read_text())


def test_online_capture_fresh_install_complete_and_no_record_api(setup, tmp_path):
  db, state, remote = setup
  original = remote.data['data.db']
  result = run(setup)
  assert result['success'] and result['healthy'] and result['installed']
  assert result['fresh_scope'] == sorted(O.DAILY)
  assert remote.on and remote.data['auxiliary.db'] == b'NEVER TOUCH'
  assert [c[0] for c in remote.calls[:3]] == ['power', 'collect', 'power']
  assert remote.calls[-2:] == [('start',), ('health',)]
  assert saved(state)['next'] == len(saved(state)['operations'])
  assert receipt(state)['phase'] == 'complete'
  assert any(value == original for name, value in remote.data.items() if name.endswith('.original'))
  installed = tmp_path / 'installed.db'
  installed.write_bytes(remote.data['data.db'])
  inv = S.SQLiteInventory(installed, BASE)
  assert len(inv.all('projects')) == 1
  assert {r['source'] for r in inv.all('source_entries')} == set(O.DAILY)


def test_pending_old_exact_ids_then_fresh_weekly(setup, tmp_path):
  db, state, remote = setup
  old = W._plan(S.SQLiteInventory(db, BASE), merge(rows(['awesome'])), '2026-11-14', ['awesome'])
  old['operations'][0]['body']['id'] = 'legacyproject01'
  previous = W._rid('projects', old['operations'][0]['body'])
  for op in old['operations'][1:]:
    if op['body'].get('project') == previous:
      op['body']['project'] = 'legacyproject01'
  W._save(state, old)
  result = run(setup, names=(*O.DAILY, 'gh_popular'))
  assert result['applied'][0]['generation'] == old['generation']
  assert result['applied'][0]['start'] == 0
  installed = tmp_path / 'installed.db'
  installed.write_bytes(remote.data['data.db'])
  inv = S.SQLiteInventory(installed, BASE)
  assert inv.all('projects')[0]['id'] == 'legacyproject01'
  assert {r['captured_date'] for r in inv.all('repo_snapshots')} == {'2026-11-14', TODAY}
  assert saved(state)['requested_sources'] == sorted((*O.DAILY, 'gh_popular'))


@pytest.mark.parametrize('failure', ['upload', 'rename-original', 'rename-install', 'upload-hash', 'installed-hash'])
def test_transfer_failures_restore_original_no_checkpoint(setup, failure):
  db, state, remote = setup
  original = remote.data['data.db']
  fired = []
  def fail(kind, *args):
    if fired:
      return
    trigger = ((failure == 'upload' and kind == 'upload') or
      (failure == 'rename-original' and kind == 'rename' and args[0] == 'data.db') or
      (failure == 'rename-install' and kind == 'rename' and args[1] == 'data.db'))
    if trigger:
      fired.append(True)
      raise OSError('synthetic transport failure')
    if kind == 'download' and ((failure == 'upload-hash' and args[0].endswith('.upload')) or
       (failure == 'installed-hash' and args[0] == 'data.db' and any(
         c[0] == 'rename' and c[2] == 'data.db' for c in remote.calls))):
      fired.append(True)
      remote.data[args[0]] = b'corrupt'
  remote.failure = fail
  with pytest.raises((OSError, RuntimeError)):
    run(setup)
  assert fired and remote.on and remote.data['data.db'] == original
  assert remote.data['auxiliary.db'] == b'NEVER TOUCH'
  journal = saved(state)
  assert journal is None or journal['next'] == 0
  assert receipt(state)['phase'] == 'aborted'


@pytest.mark.parametrize('failure', ['health', 'interrupted', 'terminal-receipt'])
def test_rollback_restart_recovery_preserves_new_user_writes(setup, monkeypatch, failure):
  db, state, remote = setup
  original_record = O._record
  def checkpoint(*args):
    raise OSError('checkpoint failed')
  monkeypatch.setattr(O, '_checkpoint', checkpoint)
  def fail(kind, *args):
    if kind == 'health' and failure in ('health', 'interrupted'):
      raise (O.Interrupted if failure == 'interrupted' else OSError)('health unavailable')
  remote.failure = fail
  def record(path, payload, phase):
    if phase == 'aborted' and failure == 'terminal-receipt':
      raise OSError('terminal receipt unavailable')
    return original_record(path, payload, phase)
  monkeypatch.setattr(O, '_record', record)
  with pytest.raises((OSError, O.Interrupted)):
    run(setup)
  assert remote.on and receipt(state)['phase'] == 'restart_pending'
  remote.data['data.db'] += b'new user writes after rollback restart'
  newer = remote.data['data.db']
  remote.calls.clear()
  remote.failure = None
  monkeypatch.setattr(O, '_record', original_record)
  O._recover(remote, state, receipt(state), BASE)
  assert remote.data['data.db'] == newer
  assert remote.calls == [('start',), ('health',)]
  assert receipt(state)['phase'] == 'aborted'


def test_failed_intent_restore_keeps_instance_stopped(setup, monkeypatch):
  db, state, remote = setup
  def failed(*args):
    raise OSError('durable write failed')
  monkeypatch.setattr(O, '_checkpoint', failed)
  monkeypatch.setattr(O, '_restore_intent', failed)
  with pytest.raises(OSError):
    run(setup)
  assert not remote.on and receipt(state)['phase'] == 'rolled_back'
  assert ('start',) not in remote.calls


def test_shutdown_sidecar_disappears_capture_restarts_without_mutations(setup):
  db, state, remote = setup
  remote.data['data.db-shm'] = b'transient shared memory'
  fired = []
  def fail(kind, *args):
    if kind == 'download' and args[0] == 'data.db-shm' and not fired:
      fired.append(True)
      remote.data.pop('data.db-shm')
      raise O.subprocess.CalledProcessError(1, ['sftp'])
  remote.failure = fail
  result = run(setup)
  assert result['success'] and fired and remote.on
  rr = receipt(state)
  assert set(rr['original']) == {'data.db'}
  assert (Path(rr['directory']) / 'capture-0/data.db').exists()
  assert (Path(rr['directory']) / 'capture-1/data.db').exists()
  assert remote.calls.count(('stop',)) == 1


def test_unstable_capture_stops_after_three_attempts_without_installing(setup):
  db, state, remote = setup
  original = remote.data['data.db']
  def fail(kind, *args):
    if kind == 'download':
      raise O.subprocess.CalledProcessError(1, ['sftp'])
  remote.failure = fail
  with pytest.raises(O.subprocess.CalledProcessError):
    run(setup)
  assert len([c for c in remote.calls if c[0] == 'download']) == 3
  assert not any(c[0] in ('upload', 'rename') for c in remote.calls)
  assert remote.on and remote.data['data.db'] == original and saved(state) is None


def test_sigterm_during_capture_does_not_retry_or_publish(setup, monkeypatch, capsys):
  db, state, remote = setup
  original = remote.data['data.db']
  monkeypatch.setattr(O, 'SFTPTransport', lambda: remote)
  from collector import sync
  monkeypatch.setattr(sync, 'collect', lambda names: rows(names))
  def terminate(kind, *args):
    if kind == 'download':
      signal.raise_signal(signal.SIGTERM)
  remote.failure = terminate
  assert O.main(['--state-file', str(state)]) == 1
  assert json.loads(capsys.readouterr().out)['error_type'] == 'Interrupted'
  assert len([c for c in remote.calls if c[0] == 'download']) == 1
  assert not any(c[0] in ('upload', 'rename') for c in remote.calls)
  assert remote.on and remote.data['data.db'] == original and saved(state) is None


@pytest.mark.parametrize('point', ['old-progress', 'fresh-publish', 'fresh-progress', 'after-progress-replace'])
def test_checkpoint_failures_rollback_and_restore_pending_intent(setup, monkeypatch, point):
  db, state, remote = setup
  old = W._plan(S.SQLiteInventory(db, BASE), merge(rows(['awesome'])), '2026-11-14', ['awesome'])
  W._save(state, old)
  original = remote.data['data.db']
  real_save, fired = W._save, []
  def fail(path, payload):
    progress = path == Path(str(state) + '.progress')
    old_gen = payload.get('generation') == old['generation']
    trigger = ((point == 'old-progress' and progress and old_gen) or
      (point == 'fresh-publish' and path == state and not old_gen) or
      (point in ('fresh-progress', 'after-progress-replace') and progress and not old_gen))
    if trigger and not fired:
      # All checkpoint attempts happen only after verified remote installation.
      assert remote.data['data.db'] != original and not remote.on
      fired.append(True)
      if point == 'after-progress-replace':
        real_save(path, payload)
      raise OSError('synthetic checkpoint failure')
    return real_save(path, payload)
  monkeypatch.setattr(W, '_save', fail)
  with pytest.raises(OSError):
    run(setup)
  assert fired and remote.on and remote.data['data.db'] == original
  assert saved(state)['generation'] == old['generation'] and saved(state)['next'] == old['next']
  assert receipt(state)['phase'] == 'aborted'
  assert run(setup)['success']


@pytest.mark.parametrize('failure', ['start', 'health'])
def test_startup_failure_keeps_installed_checkpoint_and_retry_recovers(setup, failure):
  db, state, remote = setup
  original = remote.data['data.db']
  def fail(kind, *args):
    if kind == failure:
      raise OSError('synthetic startup failure')
  remote.failure = fail
  with pytest.raises(OSError):
    run(setup)
  assert remote.data['data.db'] != original
  assert saved(state)['next'] == len(saved(state)['operations'])
  assert receipt(state)['phase'] == 'checkpointed'
  remote.failure = None
  assert run(setup)['success'] and remote.on


def test_crash_after_install_before_checkpoint_reconciles_exact_receipt(setup, monkeypatch):
  db, state, remote = setup
  real_checkpoint = O._checkpoint
  class PowerLoss(BaseException):
    pass
  def die(*args):
    raise PowerLoss()
  # Bypass normal cleanup to model SIGKILL / machine loss, not an exception.
  monkeypatch.setattr(O, '_checkpoint', die)
  monkeypatch.setattr(O, '_abort', die)
  with pytest.raises(PowerLoss):
    run(setup)
  installed = receipt(state)
  assert installed['phase'] == 'installed' and not remote.on
  assert saved(state) is None
  monkeypatch.setattr(O, '_checkpoint', real_checkpoint)
  O._recover(remote, state, installed, BASE)
  assert remote.on and saved(state)['generation'] == installed['fresh_journal']['generation']
  assert saved(state)['next'] == len(saved(state)['operations'])


def test_wal_download_backup_and_old_sidecars_never_attach(setup, tmp_path):
  db, state, remote = setup
  connection = sqlite3.connect(db)
  connection.execute('PRAGMA journal_mode=WAL')
  connection.execute('PRAGMA wal_autocheckpoint=0')
  connection.execute('CREATE TABLE wal_marker (value TEXT)')
  connection.execute("INSERT INTO wal_marker VALUES ('preserved')")
  connection.commit()
  remote.data.update({name: (tmp_path / name).read_bytes() for name in O.DBSET})
  try:
    assert run(setup)['success']
    assert 'data.db-wal' not in remote.data and 'data.db-shm' not in remote.data
    assert all(any(name.startswith(original + '.offline-') and name.endswith('.original')
                   for name in remote.data) for original in O.DBSET)
    installed = tmp_path / 'installed.db'
    installed.write_bytes(remote.data['data.db'])
    with sqlite3.connect(installed) as staged:
      assert staged.execute('SELECT * FROM wal_marker').fetchall() == [('preserved',)]
  finally:
    connection.close()


def test_offline_cap_exceeds_api_200_and_insufficient_cap_fails_safely(setup):
  assert run(setup, n=75)['success']
  assert len(saved(setup[1])['operations']) > 200
  before = setup[2].data['data.db']
  with pytest.raises(RuntimeError):
    run(setup, n=76, max_writes=1)
  assert setup[2].on and setup[2].data['data.db'] == before


def test_empty_requested_adapter_still_records_fresh_scope(setup):
  db, state, remote = setup
  result = O.run(remote, state, lambda names: [], (*O.DAILY, 'gh_popular'), TODAY, BASE)
  assert result['success'] and saved(state)['requested_sources'] == sorted((*O.DAILY, 'gh_popular'))


def test_dry_run_is_local_only_and_preserves_journal(setup, tmp_path):
  db, state, remote = setup
  old = W._plan(S.SQLiteInventory(db, BASE), [], TODAY, ['awesome'])
  W._save(state, old)
  before = state.read_bytes()
  result = O.dry_run(db, state, rows(O.DAILY), list(O.DAILY), TODAY, tmp_path, base=BASE)
  assert result['dry_run'] and not result['installed'] and not result['success']
  assert result['planned'] > 0 and before == state.read_bytes() and remote.calls == []


def test_journal_lock_excludes_capture_and_downtime(setup):
  with W._lock(setup[1]):
    with pytest.raises(RuntimeError):
      run(setup)
  assert setup[2].calls == []


def test_sigterm_during_install_rolls_back_restarts_and_exits_failure(setup, monkeypatch, capsys):
  db, state, remote = setup
  original = remote.data['data.db']
  monkeypatch.setattr(O, 'SFTPTransport', lambda: remote)
  from collector import sync
  monkeypatch.setattr(sync, 'collect', lambda names: rows(names))
  sent = []
  def terminate(kind, *args):
    if kind == 'rename' and args[1] == 'data.db' and not sent:
      sent.append(True)
      signal.raise_signal(signal.SIGTERM)
  remote.failure = terminate
  status = O.main(['--state-file', str(state)])
  assert status == 1 and json.loads(capsys.readouterr().out)['success'] is False
  assert remote.on and remote.data['data.db'] == original
  assert receipt(state)['phase'] == 'aborted'


def test_runners_preserve_daily_weekly_scope():
  root = Path(__file__).resolve().parents[1] / 'collector'
  daily = (root / 'run_daily.sh').read_text()
  weekly = (root / 'run_weekly_sqlstage.sh').read_text()
  assert 'collector.offline --sources awesome,selfhst,gh_trending "$@"' in daily
  assert 'collector.offline --sources awesome,selfhst,gh_trending,gh_popular "$@"' in weekly
  assert 'agent-env.sh' in daily and 'agent-env.sh' in weekly
  assert 'collector.sync' not in daily + weekly


def test_sigterm_at_startup_retries_start_without_rolling_back(setup, monkeypatch, capsys):
  db, state, remote = setup
  original = remote.data['data.db']
  monkeypatch.setattr(O, 'SFTPTransport', lambda: remote)
  from collector import sync
  monkeypatch.setattr(sync, 'collect', lambda names: rows(names))
  sent = []
  def terminate(kind, *args):
    if kind == 'start' and not sent:
      sent.append(True)
      signal.raise_signal(signal.SIGTERM)
  remote.failure = terminate
  assert O.main(['--state-file', str(state)]) == 1
  assert json.loads(capsys.readouterr().out)['error_type'] == 'Interrupted'
  assert remote.on and remote.data['data.db'] != original
  assert receipt(state)['phase'] == 'checkpointed'
  journal = W._load(state, SimpleNamespace(base=O.BASE))
  assert journal['next'] == len(journal['operations'])




def test_health_uses_existing_pockethost_user_agent(monkeypatch):
  import io
  class Response(io.BytesIO):
    status = 200
  def urlopen(request, timeout):
    assert request.full_url == O.BASE + '/api/health'
    assert request.get_header('User-agent') == W.UA['User-Agent']
    assert timeout == 20
    return Response(b'{"code":200}')
  monkeypatch.setattr(O.urllib.request, 'urlopen', urlopen)
  monkeypatch.setenv('POCKETHOST_SFTP_USER', 'fixture')
  assert O.SFTPTransport().health()


def test_default_destination_uses_the_phost_sftp_transport(setup, monkeypatch, capsys):
  db, state, remote = setup
  destinations = []
  def transport():
    destinations.append('phost-sftp')
    return remote
  monkeypatch.setenv('POCKETHOST_SFTP_USER', 'fixture')
  monkeypatch.setattr(O, 'SFTPTransport', transport)
  from collector import sync
  monkeypatch.setattr(sync, 'collect', lambda names: rows(names))
  assert O.main(['--state-file', str(state)]) == 0
  assert destinations == ['phost-sftp']
  assert json.loads(capsys.readouterr().out)['success']


def test_intentionally_off_instance_is_never_started_or_mutated(setup):
  db, state, remote = setup
  remote.on = False
  before = dict(remote.data)
  with pytest.raises(RuntimeError, match='intentionally off'):
    run(setup)
  assert remote.calls == [('power',)] and not remote.on and remote.data == before
  assert not state.exists() and not O._receipt_path(state).exists()
