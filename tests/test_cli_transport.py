"""Official CLI transport exercised only through a synthetic command runner."""
import json
import io
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from collector import offline as O, pbwrite as W
from test_offline import schema, rows, TODAY, BASE


class CLI:
  def __init__(self):
    self.data = {'pb_data/data.db': b'original', 'pb_data/auxiliary.db': b'untouched'}
    self.on, self.calls, self.pulls = True, [], []
    self.before = self.after = None

  def run(self, argv, **kwargs):
    args = argv[1:]
    self.calls.append((args, kwargs))
    assert kwargs['env']['POCKETHOST_TOKEN'] == 'synthetic-token'
    assert kwargs['env']['POCKETHOST_MOTHERSHIP_URL'] == 'https://mothership.pockethost.io'
    assert 'synthetic-token' not in repr(argv)
    assert kwargs['check'] and kwargs['capture_output'] and kwargs['timeout'] == 180
    if self.before:
      override = self.before(args)
      if override is not None:
        return SimpleNamespace(stdout=override)
    kind = args[0]
    if kind == 'show':
      assert args == ['show', 'selfhosted', '--json']
      output = json.dumps({'id': '2sb1xkspblek1f5', 'subdomain': 'selfhosted', 'power': self.on})
    elif kind == 'power':
      assert args in (['power', 'off', 'selfhosted', '--wait'], ['power', 'on', 'selfhosted'])
      self.on = args[1] == 'on'
      output = ''
    else:
      assert not self.on
      assert args[-2:] == ['-I', 'selfhosted']
      if kind == 'files':
        assert args[2] == '--json'
        if args[1] not in self.data:
          raise O.subprocess.CalledProcessError(1, argv, output='',
                                               stderr=f'Error: {args[1]} not found\n')
        output = json.dumps([{'path': args[1], 'isDirectory': False,
                              'size': len(self.data[args[1]]), 'mtime': '2026-11-15'}])
      elif kind == 'pull':
        assert args[2:4] == ['--data', '-d']
        directory = Path(args[4])
        assert directory.stat().st_mode & 0o777 == 0o700
        assert not list(directory.iterdir())
        assert str(directory) not in self.pulls
        self.pulls.append(str(directory))
        path = directory / args[1]
        path.parent.mkdir()
        path.write_bytes(self.data[args[1]])
        output = ''
      elif kind == 'upload':
        assert Path(args[1]).is_file()
        self.data[args[2]] = Path(args[1]).read_bytes()  # atomic completed PUT
        output = ''
      elif kind == 'rm':
        del self.data[args[1]]
        output = ''
      else:
        raise AssertionError(args)
    if self.after:
      self.after(args)
    return SimpleNamespace(stdout=output)


@pytest.fixture
def cli(monkeypatch, tmp_path):
  def blocked(*args, **kwargs):
    raise AssertionError('unexpected network or record API')
  monkeypatch.setattr(socket.socket, 'connect', blocked)
  monkeypatch.setattr(socket, 'create_connection', blocked)
  monkeypatch.setattr(O.urllib.request, 'urlopen', blocked)
  monkeypatch.setattr(W.PB, '__init__', blocked)
  monkeypatch.setenv('SELFHOSTED_POCKETHOST_TOKEN', 'obsolete-instance-token')
  monkeypatch.setenv('POCKETHOST_TOKEN', 'synthetic-token')
  monkeypatch.setenv('POCKETHOST_MOTHERSHIP_URL', 'https://untrusted.example.invalid')
  monkeypatch.setenv('PHIO_MOTHERSHIP_URL', 'https://legacy.example.invalid')
  monkeypatch.setattr(O.shutil, 'which', lambda name: '/synthetic/pockethost')
  runner = CLI()
  def metadata(request, timeout):
    parsed = O.urllib.parse.urlparse(request.full_url)
    assert parsed.scheme == 'https' and parsed.netloc == 'selfhosted.pockethost.io'
    assert parsed.path == '/_ph/deploy/v1/files' and timeout == 20
    assert request.get_header('Authorization') == 'Bearer synthetic-token'
    assert 'Authorization' not in request.headers
    remote = O.urllib.parse.parse_qs(parsed.query)['path'][0]
    try:
      result = runner.run(['/synthetic/pockethost', 'files', remote, '--json', '-I', 'selfhosted'],
                          env={'POCKETHOST_TOKEN': 'synthetic-token',
                               'POCKETHOST_MOTHERSHIP_URL': 'https://mothership.pockethost.io'},
                          check=True, capture_output=True, timeout=180)
    except O.subprocess.CalledProcessError as exc:
      missing = exc.returncode == 1 and exc.stderr == f'Error: {remote} not found\n' and not exc.stdout
      code = 404 if missing else 403
      raise O.urllib.error.HTTPError(request.full_url, code, 'probe', {},
        io.BytesIO(json.dumps({'message': f'{remote} not found' if missing else 'unauthorized'}).encode()))
    return io.BytesIO(json.dumps({'entries': json.loads(result.stdout), 'truncated': False}).encode())
  monkeypatch.setattr(O.urllib.request, 'urlopen', metadata)
  monkeypatch.setattr(O.subprocess, 'run', runner.run)
  monkeypatch.setattr(O.CLITransport, 'health', lambda self: runner.on)
  transport = O.CLITransport()
  transport.configure(tmp_path / 'transport-state.json')
  return runner, transport


def staging(suffix='upload', base='data.db'):
  return base + '.offline-' + 'a' * 32 + '.' + suffix


def test_roundtrip_fresh_pulls_and_child_credential_environment(cli, tmp_path):
  runner, transport = cli
  assert O.os.environ['POCKETHOST_TOKEN'] == 'synthetic-token'
  assert O.os.environ['POCKETHOST_MOTHERSHIP_URL'] == 'https://untrusted.example.invalid'
  assert transport.is_on()
  transport.stop()
  assert transport.files() == {'data.db'}
  path = tmp_path / 'copy'
  transport.download('data.db', path)
  path.write_bytes(b'stale local')
  transport.download('data.db', path)
  assert path.read_bytes() == b'original'
  transport.upload(path, staging())
  transport.rename('data.db', staging('original'))
  transport.rename(staging(), 'data.db')
  assert runner.data['pb_data/data.db'] == b'original'
  assert runner.data['pb_data/' + staging('original')] == b'original'
  assert runner.data['pb_data/auxiliary.db'] == b'untouched'
  assert all(not Path(p).exists() for p in runner.pulls)
  assert 'synthetic-token' not in json.dumps(transport.identity)
  transport.start()
  assert transport.is_on()


@pytest.mark.parametrize('token,executable', [(None, '/synthetic/pockethost'), ('synthetic', None)])
def test_constructor_preflight(cli, monkeypatch, token, executable):
  runner, _ = cli
  if token is None:
    monkeypatch.delenv('POCKETHOST_TOKEN')
  monkeypatch.setattr(O.shutil, 'which', lambda name: executable)
  with pytest.raises(RuntimeError, match='required'):
    O.CLITransport()
  assert runner.calls == []


@pytest.mark.parametrize('value', [None, ''])
def test_obsolete_instance_token_never_substitutes_for_common_token(cli, monkeypatch, value):
  runner, _ = cli
  if value is None:
    monkeypatch.delenv('POCKETHOST_TOKEN')
  else:
    monkeypatch.setenv('POCKETHOST_TOKEN', value)
  assert O.os.environ['SELFHOSTED_POCKETHOST_TOKEN'] == 'obsolete-instance-token'
  with pytest.raises(RuntimeError, match='POCKETHOST_TOKEN'):
    O.CLITransport()
  assert runner.calls == []


def test_mutation_marker_precedes_request_and_blocks_fresh_process(cli, tmp_path):
  runner, transport = cli
  marker = transport._marker
  def terminated(args):
    if args[0] == 'power':
      assert json.loads(marker.read_text())['pending'] is True
      raise SystemExit('simulated process death')
  runner.before = terminated
  with pytest.raises(SystemExit):
    transport.stop()
  before = len(runner.calls)
  fresh = O.CLITransport()
  with pytest.raises(O.MutationUncertain):
    fresh.configure(Path(str(marker).removesuffix('.cli-mutation.json')))
  assert len(runner.calls) == before
  assert 'synthetic-token' not in marker.read_text()


def test_successful_mutation_clears_durable_marker(cli):
  runner, transport = cli
  transport.stop()
  assert json.loads(transport._marker.read_text())['pending'] is False
  fresh = O.CLITransport()
  fresh.configure(Path(str(transport._marker).removesuffix('.cli-mutation.json')))
  assert not fresh.uncertain


def test_raw_metadata_rejects_truncation_and_unrelated_not_found(cli, monkeypatch):
  runner, transport = cli
  transport.stop()
  monkeypatch.setattr(O.urllib.request, 'urlopen', lambda *a, **k:
    io.BytesIO(b'{"entries":[],"truncated":true}'))
  with pytest.raises(RuntimeError, match='metadata'):
    transport.files()
  def wrong_missing(request, **kwargs):
    raise O.urllib.error.HTTPError(request.full_url, 404, 'missing', {},
      io.BytesIO(b'{"message":"instance not found"}'))
  monkeypatch.setattr(O.urllib.request, 'urlopen', wrong_missing)
  with pytest.raises(RuntimeError, match='probe failed'):
    transport.files()


def test_metadata_authorization_does_not_follow_redirects(cli):
  request = O.urllib.request.Request('https://selfhosted.pockethost.io/_ph/deploy/v1/files')
  request.add_unredirected_header('Authorization', 'Bearer synthetic-token')
  redirected = O.urllib.request.HTTPRedirectHandler().redirect_request(
    request, None, 302, 'redirect', {}, 'https://example.invalid/metadata')
  assert redirected.get_header('Authorization') is None


@pytest.mark.parametrize('stderr,code', [('Error: unauthorized\n', 1),
  ('Error: pb_data/data.db-wal not found\nextra', 1),
  ('Error: pb_data/other not found\n', 1), ('Error: pb_data/data.db-wal not found\n', 2)])
def test_only_exact_absence_is_missing(cli, stderr, code):
  runner, transport = cli
  transport.stop()
  def fail(args):
    if args[0] == 'files':
      raise O.subprocess.CalledProcessError(code, args, output='', stderr=stderr)
  runner.before = fail
  with pytest.raises(RuntimeError, match='probe failed'):
    transport.files(('data.db-wal',))


@pytest.mark.parametrize('record', [None, {}, [], [{'path': 'pb_data/data.db'}],
  [{'path': 'pb_data/data.db', 'isDirectory': True, 'size': 3, 'mtime': 'now'}],
  [{'path': 'pb_data/other', 'isDirectory': False, 'size': 3, 'mtime': 'now'}],
  [{'path': 'pb_data/data.db', 'isDirectory': False, 'size': True, 'mtime': 'now'}],
  [{'path': 'pb_data/data.db', 'isDirectory': False, 'size': 3, 'mtime': 'now'}] * 2])
def test_malformed_exact_files_fail_closed(cli, record):
  runner, transport = cli
  transport.stop()
  runner.before = lambda args: json.dumps(record) if args[0] == 'files' else None
  with pytest.raises(RuntimeError):
    transport.files(('data.db',))


@pytest.mark.parametrize('name', ['../data.db', 'data.db/other', 'pb_hooks/test',
  'auxiliary.db', 'data.db.bak', 'data.db.offline-bogus.upload', 'data.db\n'])
def test_forged_paths_never_reach_cli(cli, tmp_path, name):
  runner, transport = cli
  with pytest.raises(ValueError):
    transport.files((name,))
  with pytest.raises(ValueError):
    transport.download(name, tmp_path / 'file')
  assert runner.calls == []


def test_controls_and_retained_source_protection(cli, tmp_path):
  runner, transport = cli
  with pytest.raises(RuntimeError, match='stopped'):
    transport.files()
  transport.stop()
  for suffix in ('original', 'rejected'):
    with pytest.raises(ValueError, match='retained'):
      transport.rename(staging(suffix), 'data.db')
  with pytest.raises(RuntimeError, match='exists'):
    transport.rename(staging(), 'data.db')
  with pytest.raises(ValueError):
    transport.upload(tmp_path, staging())
  assert not any(a[0] in ('upload', 'rm') for a, _ in runner.calls)


@pytest.mark.parametrize('record', [[], {}, {'id': 'wrong', 'subdomain': 'selfhosted', 'power': False},
  {'id': '2sb1xkspblek1f5', 'subdomain': 'other', 'power': False},
  {'id': '2sb1xkspblek1f5', 'subdomain': 'selfhosted', 'power': 0}])
def test_show_must_confirm_exact_instance_and_boolean_power(cli, record):
  runner, transport = cli
  runner.before = lambda args: json.dumps(record) if args[0] == 'show' else None
  with pytest.raises(RuntimeError, match='power state'):
    transport.stop()
  assert not any(a[0] in ('upload', 'rm', 'pull') for a, _ in runner.calls)


@pytest.mark.parametrize('changed', ['source', 'destination'])
def test_move_rechecks_both_hashes_before_removing_anything(cli, changed):
  runner, transport = cli
  transport.stop()
  def corrupt(args):
    if args[0] == 'upload':
      path = 'pb_data/data.db' if changed == 'source' else args[2]
      runner.data[path] = b'changed during move'
  runner.after = corrupt
  with pytest.raises(RuntimeError, match='hash mismatch'):
    transport.rename('data.db', staging('original'))
  assert 'pb_data/data.db' in runner.data
  assert not any(a[0] == 'rm' for a, _ in runner.calls)


def prepare_run(cli, tmp_path):
  runner, transport = cli
  db, state = tmp_path / 'fixture.db', tmp_path / 'writes.json'
  schema(db)
  runner.data['pb_data/data.db'] = db.read_bytes()
  return runner, transport, state


def publish(transport, state):
  return O.run(transport, state, lambda scope: rows(scope), O.DAILY, TODAY, BASE)


def test_overall_run(cli, tmp_path):
  runner, transport, state = prepare_run(cli, tmp_path)
  assert publish(transport, state)['success'] and runner.on
  receipt = json.loads(O._receipt_path(state).read_text())
  assert receipt['phase'] == 'complete'
  assert receipt['transport'] == transport.identity
  assert 'synthetic-token' not in O._receipt_path(state).read_text()
  assert all(a[0] in ('show', 'files', 'pull', 'upload', 'rm', 'power') for a, _ in runner.calls)


@pytest.mark.parametrize('point', ['backup-copy', 'backup-remove', 'install-copy', 'install-remove'])
def test_lost_responses_retain_backups_without_conflicting_mutations(cli, tmp_path, point):
  runner, transport, state = prepare_run(cli, tmp_path)
  original = runner.data['pb_data/data.db']
  fired = []
  def lost(args):
    trigger = ((point == 'backup-copy' and args[0] == 'upload' and args[2].endswith('.original')) or
      (point == 'backup-remove' and args[:2] == ['rm', 'pb_data/data.db']) or
      (point == 'install-copy' and args[0] == 'upload' and args[2] == 'pb_data/data.db') or
      (point == 'install-remove' and args[0] == 'rm' and args[1].endswith('.upload')))
    if trigger and not fired:
      fired.append(len(runner.calls))
      raise O.subprocess.CalledProcessError(1, args, output='', stderr='lost completed response')
  runner.after = lost
  with pytest.raises(O.MutationUncertain):
    publish(transport, state)
  assert fired and not runner.on and len(runner.calls) == fired[0]
  assert any(p.endswith('.original') and v == original for p, v in runner.data.items())
  assert json.loads(O._receipt_path(state).read_text())['phase'] == 'uncertain'


@pytest.mark.parametrize('point', ['reject-copy', 'reject-remove', 'restore-copy', 'restore-remove'])
def test_rollback_move_lost_response_retains_originals_and_stays_stopped(cli, tmp_path, monkeypatch, point):
  runner, transport, state = prepare_run(cli, tmp_path)
  original = runner.data['pb_data/data.db']
  def failed_checkpoint(*args):
    raise OSError('synthetic checkpoint failure')
  monkeypatch.setattr(O, '_checkpoint', failed_checkpoint)
  rolling, fired = [], []
  real_record = O._record
  def record(path, receipt, phase):
    if phase == 'rolling_back':
      rolling.append(True)
    return real_record(path, receipt, phase)
  monkeypatch.setattr(O, '_record', record)
  def lost(args):
    trigger = ((point == 'reject-copy' and args[0] == 'upload' and args[2].endswith('.rejected')) or
      (point == 'reject-remove' and args[:2] == ['rm', 'pb_data/data.db']) or
      (point == 'restore-copy' and args[0] == 'upload' and args[2] == 'pb_data/data.db') or
      (point == 'restore-remove' and args[0] == 'rm' and args[1].endswith('.restore')))
    if rolling and trigger and not fired:
      fired.append(len(runner.calls))
      raise O.subprocess.CalledProcessError(1, args, output='', stderr='lost completed response')
  runner.after = lost
  with pytest.raises(O.MutationUncertain):
    publish(transport, state)
  receipt = json.loads(O._receipt_path(state).read_text())
  assert fired and not runner.on and receipt['phase'] == 'uncertain'
  retained = {p: value for p, value in runner.data.items() if p.endswith(('.original', '.rejected'))}
  assert any(p.endswith('.original') and v == original for p, v in retained.items())
  with pytest.raises(O.MutationUncertain):
    O._recover(O.CLITransport(), state, receipt, BASE)
  assert len(runner.calls) == fired[0]
  assert all(runner.data[p] == value for p, value in retained.items())


def test_timeout_during_rollback_stops_without_restart(cli, tmp_path, monkeypatch):
  runner, transport, state = prepare_run(cli, tmp_path)
  def failed_checkpoint(*args):
    raise OSError('synthetic checkpoint failure')
  monkeypatch.setattr(O, '_checkpoint', failed_checkpoint)
  fired = []
  def timeout(args):
    if args[0] == 'upload' and args[2].endswith('.restore'):
      fired.append(len(runner.calls))
      raise O.subprocess.TimeoutExpired(args, 180)
  runner.after = timeout
  with pytest.raises(O.MutationUncertain):
    publish(transport, state)
  assert fired and not runner.on and len(runner.calls) == fired[0]
  assert json.loads(O._receipt_path(state).read_text())['phase'] == 'uncertain'


def test_interrupt_during_mutation_is_uncertain(cli, tmp_path):
  runner, transport, state = prepare_run(cli, tmp_path)
  fired = []
  def interrupt(args):
    if args[0] == 'upload':
      fired.append(len(runner.calls))
      raise O.Interrupted('termination requested')
  runner.after = interrupt
  with pytest.raises(O.MutationUncertain):
    publish(transport, state)
  assert not runner.on and len(runner.calls) == fired[0]
  assert json.loads(O._receipt_path(state).read_text())['phase'] == 'uncertain'


@pytest.mark.parametrize('point', ['upload', 'rm', 'off', 'on'])
@pytest.mark.parametrize('failure', ['timeout', 'process-error'])
def test_mutation_failure_persists_uncertain_and_never_recovers_automatically(cli, tmp_path, point, failure):
  runner, transport, state = prepare_run(cli, tmp_path)
  fired = []
  def timeout(args):
    if not fired and (args[0] == point or args[:2] == ['power', point]):
      fired.append(len(runner.calls))
      if failure == 'timeout':
        raise O.subprocess.TimeoutExpired(args, 180)
      raise O.subprocess.CalledProcessError(1, args, output='', stderr='connection lost')
  runner.after = timeout
  with pytest.raises(O.MutationUncertain):
    publish(transport, state)
  assert fired and len(runner.calls) == fired[0]
  receipt = json.loads(O._receipt_path(state).read_text())
  assert receipt['phase'] == 'uncertain'
  fresh = O.CLITransport()
  with pytest.raises(O.MutationUncertain):
    O._recover(fresh, state, receipt, BASE)
  assert len(runner.calls) == fired[0]


def test_old_receipt_target_mismatch_but_completed_receipt_is_accepted(cli, tmp_path):
  runner, transport = cli
  receipt = {'phase': 'installing', 'transport': {'host': 'legacy'}, 'target': BASE}
  with pytest.raises(RuntimeError, match='target mismatch'):
    O._recover(transport, tmp_path / 'writes.json', receipt, BASE)
  receipt['phase'] = 'complete'
  O._recover(transport, tmp_path / 'writes.json', receipt, BASE)
  assert runner.calls == []


def test_main_dry_run_needs_neither_cli_nor_token(cli, tmp_path, monkeypatch, capsys):
  runner, _ = cli
  db, collected = tmp_path / 'fixture.db', tmp_path / 'collected.json'
  schema(db)
  collected.write_text(json.dumps({'date': TODAY, 'requested_sources': list(O.DAILY),
                                  'rows': [vars(row) for row in rows(O.DAILY)]}))
  monkeypatch.delenv('POCKETHOST_TOKEN')
  monkeypatch.setattr(O.shutil, 'which', lambda name: None)
  assert O.main(['--dry-run', '--db', str(db), '--from-collected', str(collected),
                 '--today', TODAY, '--state-file', str(tmp_path / 'writes.json')]) == 0
  result = json.loads(capsys.readouterr().out)
  assert result['dry_run'] and not result['success'] and not result['installed']
  assert runner.calls == []
