"""phost power and PocketHost SFTP exercised only through a synthetic runner."""
import json
from pathlib import Path
import socket

import pytest

from collector import offline as O, pbwrite as W
from test_offline import schema, rows, TODAY, BASE

USER = 'fixture'
HOST = USER + '@ftp.pockethost.io'
REMOTE = 'selfhosted/pb_data'


class Runner:
  """Stands in for both the `phost` power CLI and the `sftp` file CLI."""
  def __init__(self):
    self.data = {'data.db': b'original', 'auxiliary.db': b'untouched'}
    self.on, self.calls, self.batches = True, [], []
    self.before = self.after = None

  @staticmethod
  def result(output=''):
    return type('Result', (), {'stdout': output, 'stderr': ''})()

  def status(self):
    record = {'id': '2sb1xkspblek1f5', 'subdomain': 'selfhosted', 'power': self.on}
    return json.dumps(record) + '\nlive health: HTTP 200\n'

  def run(self, argv, **kwargs):
    self.calls.append(argv)
    if argv[0] == 'sftp':
      self.batches.append(kwargs['input'].splitlines())
    if self.before:
      override = self.before(argv)
      if override is not None:
        return override
    if argv[0] == 'phost':
      assert kwargs['check'] and kwargs['capture_output'] and kwargs['timeout'] == 180
      assert argv[2:] == ['selfhosted'] or argv[2:4] == ['selfhosted', '--yes']
      if argv[1] == 'status':
        output = [self.status()]
      else:
        self.on = argv[1] == 'on'
        output = ''
    else:
      assert argv[:1] == ['sftp']
      assert kwargs['check'] and kwargs['capture_output'] and kwargs['timeout'] == 300
      assert argv[argv.index('-P') + 1] == '2222'
      assert argv[argv.index('-i') + 1].endswith('.ssh/pockethost_ed25519')
      assert argv[argv.index('-b') + 1] == '-'
      assert argv[-1] == HOST
      assert not self.on
      lines = self.batches[-1]
      output = []
      for line in lines:
        verb, _, rest = line.partition(' ')
        rest = rest[3:] if rest.startswith('-1 ') else rest
        if verb == 'ls':
          assert rest.strip('"') == REMOTE + '/'
          output += ['sftp> ' + line] + [REMOTE + '/' + n for n in sorted(self.data)]
        elif verb == 'put':
          local, remote = [p.strip('"') for p in rest.split(' ')]
          name = remote.split('/')[-1]
          if name in self.data:
            raise O.subprocess.CalledProcessError(1, argv, output='', stderr='exists')
          self.data[name] = Path(local).read_bytes()
        elif verb == 'get':
          remote, local = [p.strip('"') for p in rest.split(' ')]
          name = remote.split('/')[-1]
          if name not in self.data:
            raise O.subprocess.CalledProcessError(1, argv, output='', stderr='not found')
          Path(local).write_bytes(self.data[name])
        elif verb == 'rename':
          path, _, target = rest.partition(' ')
          old, new = path.strip('"').split('/')[-1], target.strip('"').split('/')[-1]
          if old not in self.data or new in self.data:
            raise O.subprocess.CalledProcessError(1, argv, output='', stderr='rename failed')
          self.data[new] = self.data.pop(old)
        else:
          raise AssertionError(line)
    if self.after:
      self.after(argv)
    return self.result('\n'.join(output) + '\n')


@pytest.fixture
def cli(monkeypatch, tmp_path):
  def blocked(*args, **kwargs):
    raise AssertionError('unexpected network or record API')
  monkeypatch.setattr(socket.socket, 'connect', blocked)
  monkeypatch.setattr(socket, 'create_connection', blocked)
  monkeypatch.setattr(O.urllib.request, 'urlopen', blocked)
  monkeypatch.setattr(W.PB, '__init__', blocked)
  monkeypatch.setenv('POCKETHOST_SFTP_USER', USER)
  monkeypatch.delenv('PH_EMAIL', raising=False)
  monkeypatch.delenv('ICLOUD_EMAIL', raising=False)
  runner = Runner()
  monkeypatch.setattr(O.subprocess, 'run', runner.run)
  monkeypatch.setattr(O.SFTPTransport, 'health', lambda self: runner.on)
  transport = O.SFTPTransport()
  transport.configure(tmp_path / 'transport-state.json')
  return runner, transport


def staging(suffix='upload', base='data.db'):
  return base + '.offline-' + 'a' * 32 + '.' + suffix


def verbs(runner):
  return [line.split(' ')[0] for batch in runner.batches for line in batch]


def test_roundtrip_uses_phost_power_and_native_sftp_rename(cli, tmp_path):
  runner, transport = cli
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
  assert runner.data['data.db'] == b'original'
  assert runner.data[staging('original')] == b'original'
  assert runner.data['auxiliary.db'] == b'untouched'
  transport.start()
  assert transport.is_on()
  # A move is one server-side rename, never a download/upload/remove sequence.
  assert 'rename' in verbs(runner) and 'rm' not in verbs(runner)


def test_constructor_requires_an_sftp_user(monkeypatch):
  for name in ('POCKETHOST_SFTP_USER', 'PH_EMAIL', 'ICLOUD_EMAIL'):
    monkeypatch.delenv(name, raising=False)
  with pytest.raises(ValueError, match='SFTP destination'):
    O.SFTPTransport()


@pytest.mark.parametrize('user', ['fixture', 'a.b+c'])
def test_constructor_accepts_configured_user(monkeypatch, user):
  monkeypatch.setenv('POCKETHOST_SFTP_USER', user)
  transport = O.SFTPTransport()
  assert transport.host == user + '@ftp.pockethost.io'
  assert transport.remote_dir == REMOTE


def test_constructor_falls_back_to_ph_email(monkeypatch):
  monkeypatch.delenv('POCKETHOST_SFTP_USER', raising=False)
  monkeypatch.setenv('PH_EMAIL', 'owner')
  assert O.SFTPTransport().host == 'owner@ftp.pockethost.io'


def test_constructor_rejects_other_remote_dirs(monkeypatch):
  monkeypatch.setenv('POCKETHOST_SFTP_USER', USER)
  with pytest.raises(ValueError, match='only selfhosted/pb_data'):
    O.SFTPTransport(remote_dir='bible/pb_data')


def test_mutation_marker_precedes_request_and_blocks_fresh_process(cli):
  runner, transport = cli
  marker = transport._marker
  def terminated(argv):
    if argv[:2] == ['phost', 'off']:
      assert json.loads(marker.read_text())['pending'] is True
      raise SystemExit('simulated process death')
  runner.before = terminated
  with pytest.raises(SystemExit):
    transport.stop()
  before = len(runner.calls)
  fresh = O.SFTPTransport()
  with pytest.raises(O.MutationUncertain):
    fresh.configure(Path(str(marker).removesuffix('.sftp-mutation.json')))
  assert len(runner.calls) == before
  assert HOST not in marker.read_text()


def test_successful_mutation_clears_durable_marker(cli):
  runner, transport = cli
  transport.stop()
  assert json.loads(transport._marker.read_text())['pending'] is False
  fresh = O.SFTPTransport()
  fresh.configure(Path(str(transport._marker).removesuffix('.sftp-mutation.json')))
  assert not fresh.uncertain


def test_listing_only_reports_requested_names(cli):
  runner, transport = cli
  transport.stop()
  assert transport.files(('data.db-wal',)) == set()
  assert transport.files(('data.db', 'data.db-wal')) == {'data.db'}


def test_download_requires_a_stopped_instance(cli, tmp_path):
  runner, transport = cli
  with pytest.raises(RuntimeError, match='stopped'):
    transport.download('data.db', tmp_path / 'copy')
  assert not any(argv[0] == 'sftp' for argv in runner.calls)


def test_missing_source_blocks_rename(cli):
  runner, transport = cli
  transport.stop()
  with pytest.raises(RuntimeError, match='source is missing'):
    transport.rename('data.db-wal', staging('original', 'data.db-wal'))
  assert 'rename' not in verbs(runner)


@pytest.mark.parametrize('name', ['../data.db', 'data.db/other', 'pb_hooks/test',
  'auxiliary.db', 'data.db.bak', 'data.db.offline-bogus.upload', 'data.db\n',
  'data.db"onmouseover', 'data.db*'])
def test_forged_paths_never_reach_sftp(cli, tmp_path, name):
  runner, transport = cli
  transport.stop()
  with pytest.raises(ValueError):
    transport.files((name,))
  with pytest.raises(ValueError):
    transport.download(name, tmp_path / 'file')
  assert not any(argv[0] == 'sftp' for argv in runner.calls)


def test_controls_and_retained_source_protection(cli, tmp_path):
  runner, transport = cli
  with pytest.raises(RuntimeError, match='stopped'):
    transport.files()
  transport.stop()
  for suffix in ('original', 'rejected'):
    with pytest.raises(ValueError, match='retained'):
      transport.rename(staging(suffix), 'data.db')
  with pytest.raises(ValueError):
    transport.upload(tmp_path, staging())
  with pytest.raises(ValueError):
    transport.upload(tmp_path, 'data.db')
  assert 'put' not in verbs(runner)


@pytest.mark.parametrize('output', ['', '{}\n',
  '{"id":"wrong","subdomain":"selfhosted","power":false}\n',
  '{"id":"2sb1xkspblek1f5","subdomain":"other","power":false}\n',
  '{"id":"2sb1xkspblek1f5","subdomain":"selfhosted","power":0}\n'])
def test_status_must_confirm_exact_instance_and_boolean_power(cli, output):
  runner, transport = cli
  runner.before = lambda argv: Runner.result(output) if argv[:2] == ['phost', 'status'] else None
  with pytest.raises(RuntimeError, match='power state'):
    transport.stop()
  assert not any(argv[0] == 'sftp' for argv in runner.calls)


def prepare_run(cli, tmp_path):
  runner, transport = cli
  db, state = tmp_path / 'fixture.db', tmp_path / 'writes.json'
  schema(db)
  runner.data['data.db'] = db.read_bytes()
  return runner, transport, state


def publish(transport, state):
  return O.run(transport, state, lambda scope: rows(scope), O.DAILY, TODAY, BASE)


def test_overall_run(cli, tmp_path):
  runner, transport, state = prepare_run(cli, tmp_path)
  assert publish(transport, state)['success'] and runner.on
  receipt = json.loads(O._receipt_path(state).read_text())
  assert receipt['phase'] == 'complete'
  assert receipt['transport'] == transport.identity
  assert set(verbs(runner)) <= {'ls', 'get', 'put', 'rename'}


@pytest.mark.parametrize('point', ['put', 'rename', 'off', 'on'])
@pytest.mark.parametrize('failure', ['timeout', 'process-error'])
def test_mutation_failure_persists_uncertain_and_never_recovers_automatically(
    cli, tmp_path, point, failure):
  runner, transport, state = prepare_run(cli, tmp_path)
  thrown = (lambda argv: O.subprocess.TimeoutExpired(argv, 300) if failure == 'timeout'
            else O.subprocess.CalledProcessError(1, argv, output='', stderr='connection lost'))
  fired = []
  def before(argv, _kwargs=None):
    hit = (argv[:2] == ['phost', point]) if argv[0] == 'phost' else bool(
      runner.batches and runner.batches[-1][0].split(' ')[0] == point)
    if hit:
      fired.append(argv)
      raise thrown(argv)
  runner.before = before
  with pytest.raises(Exception):
    publish(transport, state)
  assert fired, point
  # A mutation whose outcome is unknown never reports success and never clears.
  receipt = json.loads(O._receipt_path(state).read_text())
  assert receipt['phase'] == 'uncertain'
  fresh = O.SFTPTransport()
  with pytest.raises(O.MutationUncertain):
    O._recover(fresh, state, receipt, BASE)


def test_interrupt_during_mutation_is_uncertain(cli, tmp_path):
  runner, transport, state = prepare_run(cli, tmp_path)
  def interrupt(argv):
    if argv[0] == 'sftp' and 'put' in [line.split(' ')[0] for line in runner.batches[-1]]:
      raise O.Interrupted('termination requested')
  runner.after = interrupt
  with pytest.raises(O.MutationUncertain):
    publish(transport, state)
  assert not runner.on
  assert json.loads(O._receipt_path(state).read_text())['phase'] == 'uncertain'


def test_old_receipt_target_mismatch_but_completed_receipt_is_accepted(cli, tmp_path):
  runner, transport = cli
  receipt = {'phase': 'installing', 'transport': {'host': 'legacy'}, 'target': BASE}
  with pytest.raises(RuntimeError, match='target mismatch'):
    O._recover(transport, tmp_path / 'writes.json', receipt, BASE)
  receipt['phase'] = 'complete'
  O._recover(transport, tmp_path / 'writes.json', receipt, BASE)
  assert runner.calls == []


def test_main_dry_run_needs_neither_sftp_nor_token(cli, tmp_path, monkeypatch, capsys):
  runner, _ = cli
  db, collected = tmp_path / 'fixture.db', tmp_path / 'collected.json'
  schema(db)
  collected.write_text(json.dumps({'date': TODAY, 'requested_sources': list(O.DAILY),
                                  'rows': [vars(row) for row in rows(O.DAILY)]}))
  monkeypatch.delenv('POCKETHOST_TOKEN', raising=False)
  monkeypatch.delenv('POCKETHOST_SFTP_USER', raising=False)
  result = O.main(['--dry-run', '--db', str(db), '--from-collected', str(collected),
                   '--today', TODAY, '--state-file', str(tmp_path / 'writes.json')])
  assert result == 0
  result = json.loads(capsys.readouterr().out)
  assert result['dry_run'] and not result['success'] and not result['installed']
  assert runner.calls == []
