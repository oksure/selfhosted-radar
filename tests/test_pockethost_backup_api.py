import io
import json
import shutil
import sqlite3
import sys
import types
import zipfile
from pathlib import Path

import pytest

from collector import offline as O
from collector import pockethost_backup as P
from test_offline import BASE, TODAY, rows, schema


class Response(io.BytesIO):
  status = 200
  def __enter__(self): return self
  def __exit__(self, *args): self.close()


def make_db(path, value):
  with sqlite3.connect(path) as db:
    db.execute('CREATE TABLE payload (value TEXT)')
    db.execute('INSERT INTO payload VALUES (?)', (value,))


def make_zip(path, data, extra=b'auxiliary'):
  with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
    zf.writestr('data.db', Path(data).read_bytes())
    zf.writestr('auxiliary.db', extra)
    zf.writestr('types.d.ts', 'kept')
  return Path(path)


class FakeAPI:
  def __init__(self):
    self.archives = {}
    self.calls = []
    self.token = 'fixture-bearer'
    self.settings = {'backups': {'s3': {'enabled': False, 'endpoint': '', 'region': '',
                                       'bucket': '', 'accessKey': '', 'secret': '',
                                       'forcePathStyle': False}}}
    self.transport = None
    self.fail = None
    self.create_400_count = 0
    self.create_400_after_write_count = 0
    self.retain_secret = False
    self.omit_secret_on_get = False
    self.omit_enabled_on_get = False
  def _req(self, method, path, body=None, params=None):
    self.calls.append((method, path))
    if method == 'POST':
      assert self.transport is not None
      marker = json.loads(self.transport._marker.read_text())
      assert marker['pending'] is True
    if path == '/api/settings' and method == 'GET':
      if self.omit_secret_on_get or self.omit_enabled_on_get:
        settings = json.loads(json.dumps(self.settings))
        if self.omit_secret_on_get:
          settings['backups']['s3'].pop('secret', None)
        if self.omit_enabled_on_get:
          settings['backups']['s3'].pop('enabled', None)
        return settings
      return self.settings
    if path == '/api/settings' and method == 'PATCH':
      s3 = dict(body['backups']['s3'])
      if self.retain_secret and not s3.get('secret'):
        s3['secret'] = self.settings['backups']['s3'].get('secret')
      self.settings['backups']['s3'] = s3
      return None
    if path == '/api/backups' and method == 'GET':
      return [{'key': key} for key in self.archives]
    if path == '/api/backups' and method == 'POST':
      if self.create_400_count:
        self.create_400_count -= 1
        raise P.W.PBError(400, {})
      key = body['name']
      if self.create_400_after_write_count:
        self.create_400_after_write_count -= 1
        self.archives[key] = self.seed
        raise P.W.PBError(400, {})
      self.archives[key] = self.seed
      return None
    if path == '/api/files/token':
      return {'token': 'fixture-file-token'}
    if path.startswith('/api/backups/') and path.endswith('/restore'):
      if self.fail == 'restore-400':
        raise P.W.PBError(400)
      return None
    raise AssertionError((method, path))


class FakeOpener:
  def __init__(self, api):
    self.api = api
    self.fail = None
  def open(self, request, timeout):
    if self.fail == 'network':
      raise OSError('fixture transport failure')
    if request.method == 'GET':
      from urllib.parse import urlsplit, unquote
      key = unquote(urlsplit(request.full_url).path.rsplit('/', 1)[-1])
      return Response(self.api.archives[key])
    assert request.method == 'POST' and '/api/backups/upload' in request.full_url
    body = request.data
    header, content = body.split(b'\r\n\r\n', 1)
    filename = header.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
    boundary = request.get_header('Content-type').split('boundary=', 1)[1]
    trailer = ('\r\n--' + boundary + '--\r\n').encode()
    archive = content[:-len(trailer)]
    self.api.archives[filename] = archive
    return Response(json.dumps({'key': filename}).encode())


def make_transport(monkeypatch, tmp_path, archive):
  api = FakeAPI()
  api.seed = archive
  opener = FakeOpener(api)
  selected = types.SimpleNamespace(url='https://selfhosted.pockethost.io',
                                  email='automation@example.invalid', password='fixture')
  monkeypatch.setitem(sys.modules, 'pb_credentials', types.SimpleNamespace(resolve=lambda _: selected))
  monkeypatch.setattr(P.W, 'PB', lambda *args, **kwargs: api)
  transport = P.BackupAPITransport(opener=opener)
  transport.configure(tmp_path / 'state.json')
  api.transport = transport
  return transport, api, opener


def test_transport_hashes_download_upload_and_journals_every_mutation(monkeypatch, tmp_path):
  db = tmp_path / 'seed.db'
  make_db(db, 'stable')
  archive = make_zip(tmp_path / 'seed.zip', db)
  transport, api, opener = make_transport(monkeypatch, tmp_path, archive.read_bytes())
  key = transport.create_backup('radar-fixture-original.zip')
  local = transport.download(key, tmp_path / 'original.zip')
  assert local.read_bytes() == archive.read_bytes()
  staged = tmp_path / 'staged.zip'
  shutil.copyfile(archive, staged)
  uploaded = transport.upload_backup(staged, 'radar-fixture-staged.zip')
  assert uploaded == 'radar-fixture-staged.zip'
  readback = transport.download(uploaded, tmp_path / 'readback.zip')
  assert O.digest(readback) == O.digest(staged)
  transport.restore(uploaded)
  assert not transport.uncertain
  assert json.loads(transport._marker.read_text())['pending'] is False
  assert not transport.rollback_retryable(uploaded)
  assert [path for method, path in api.calls if method == 'POST'] == [
      '/api/backups', '/api/files/token', '/api/files/token',
      '/api/backups/radar-fixture-staged.zip/restore']


def test_rollback_retryability_tracks_confirmed_restore_rejection(monkeypatch, tmp_path):
  db = tmp_path / 'seed.db'
  make_db(db, 'stable')
  archive = make_zip(tmp_path / 'seed.zip', db)
  transport, api, _ = make_transport(monkeypatch, tmp_path, archive.read_bytes())
  key = 'radar-fixture-original.zip'
  api.fail = 'restore-400'
  with pytest.raises(P.W.PBError):
    transport.restore(key)
  assert transport.rollback_retryable(key)

  api.fail = None
  transport.restore(key)
  assert not transport.rollback_retryable(key)


def test_create_backup_retries_confirmed_400_once(monkeypatch, tmp_path):
  db = tmp_path / 'seed.db'
  make_db(db, 'stable')
  archive = make_zip(tmp_path / 'seed.zip', db)
  transport, api, _ = make_transport(monkeypatch, tmp_path, archive.read_bytes())
  api.create_400_count = 1
  delays = []
  monkeypatch.setattr(P.time, 'sleep', delays.append)
  key = 'radar-fixture-rollback-check.zip'

  assert transport.create_backup(key) == key
  assert key in api.archives
  assert delays == [P.BACKUP_CREATE_RETRY_DELAY]
  assert len([1 for method, path in api.calls if method == 'POST' and path == '/api/backups']) == 2
  marker = json.loads(transport._marker.read_text())
  assert marker['pending'] is False and 'response_status' not in marker


def test_create_backup_resolves_retry_400_when_key_appears(monkeypatch, tmp_path):
  db = tmp_path / 'seed.db'
  make_db(db, 'stable')
  archive = make_zip(tmp_path / 'seed.zip', db)
  transport, api, _ = make_transport(monkeypatch, tmp_path, archive.read_bytes())
  api.create_400_count = 1
  api.create_400_after_write_count = 1
  delays = []
  monkeypatch.setattr(P.time, 'sleep', delays.append)
  key = 'radar-fixture-rollback-check.zip'

  assert transport.create_backup(key) == key
  assert key in api.archives
  assert delays == [P.BACKUP_CREATE_RETRY_DELAY]
  assert len([1 for method, path in api.calls if method == 'POST' and path == '/api/backups']) == 2
  marker = json.loads(transport._marker.read_text())
  assert marker['pending'] is False and marker['response_status'] == 400


def test_create_backup_stops_after_second_confirmed_400(monkeypatch, tmp_path):
  db = tmp_path / 'seed.db'
  make_db(db, 'stable')
  archive = make_zip(tmp_path / 'seed.zip', db)
  transport, api, _ = make_transport(monkeypatch, tmp_path, archive.read_bytes())
  api.create_400_count = 2
  delays = []
  monkeypatch.setattr(P.time, 'sleep', delays.append)
  key = 'radar-fixture-rollback-check.zip'

  with pytest.raises(P.W.PBError):
    transport.create_backup(key)
  assert key not in api.archives
  assert delays == [P.BACKUP_CREATE_RETRY_DELAY]
  marker = json.loads(transport._marker.read_text())
  assert marker['pending'] is False and marker['response_status'] == 400


def test_mutation_marker_blocks_after_unconfirmed_upload(monkeypatch, tmp_path):
  db = tmp_path / 'seed.db'
  make_db(db, 'stable')
  archive = make_zip(tmp_path / 'seed.zip', db)
  transport, _, opener = make_transport(monkeypatch, tmp_path, archive.read_bytes())
  opener.fail = 'network'
  with pytest.raises(O.MutationUncertain):
    transport.upload_backup(archive, 'radar-fixture-staged.zip')
  assert transport.uncertain
  assert json.loads(transport._marker.read_text())['pending'] is True
  fresh = P.BackupAPITransport.__new__(P.BackupAPITransport)
  fresh.identity = transport.identity
  fresh.uncertain = False
  fresh._marker = None
  with pytest.raises(O.MutationUncertain):
    fresh.configure(tmp_path / 'state.json')


def test_upload_limit_fails_before_network_or_mutation(monkeypatch, tmp_path):
  db = tmp_path / 'seed.db'
  make_db(db, 'stable')
  archive = make_zip(tmp_path / 'seed.zip', db)
  transport, _, opener = make_transport(monkeypatch, tmp_path, archive.read_bytes())
  large = tmp_path / 'oversize.zip'
  large.write_bytes(archive.read_bytes())
  with large.open('ab') as stream:
    stream.truncate(P.MAX_UPLOAD + 1)
  with pytest.raises(P.APIError, match='HTTPS limit'):
    transport.upload_backup(large, 'radar-fixture-large.zip')
  assert not transport.uncertain
  assert not transport._marker.exists()


def test_backup_replacement_preserves_auxiliary_and_checks_integrity(tmp_path):
  original_db = tmp_path / 'original.db'
  stage_db = tmp_path / 'stage.db'
  make_db(original_db, 'original')
  shutil.copyfile(original_db, stage_db)
  with sqlite3.connect(stage_db) as db:
    db.execute('UPDATE payload SET value=?', ('staged',))
  original_zip = make_zip(tmp_path / 'original.zip', original_db, b'auxiliary-original')
  staged_zip = tmp_path / 'staged.zip'
  O._replace_backup_db(original_zip, stage_db, staged_zip)
  extracted = O._backup_db(staged_zip, tmp_path / 'extracted.db')
  with sqlite3.connect(extracted) as db:
    assert db.execute('SELECT value FROM payload').fetchone()[0] == 'staged'
  with zipfile.ZipFile(staged_zip) as zf:
    assert zf.read('auxiliary.db') == b'auxiliary-original'
    assert zf.read('types.d.ts') == b'kept'
  with pytest.raises(RuntimeError, match='unsafe'):
    bad = tmp_path / 'bad.zip'
    with zipfile.ZipFile(bad, 'w') as zf:
      zf.writestr('../outside', 'bad')
      zf.writestr('data.db', original_db.read_bytes())
    O._backup_db(bad, tmp_path / 'bad.db')


class MemoryTransport:
  upload_cap = P.MAX_UPLOAD
  identity = {'transport': 'memory-backups', 'instance': 'selfhosted'}
  def __init__(self, backup):
    self.backups = {}
    self.current = backup
    self.on = True
    self.uncertain = False
    self.health_results = []
    self.restore_calls = []
    self.restore_400_once = None
  def configure(self, path): self.marker=Path(str(path)+'.backup-mutation.json')
  def is_on(self): return self.on
  def create_backup(self, key):
    assert key not in self.backups
    self.backups[key] = self.current
    return key
  def download(self, key, path):
    p = Path(path); p.write_bytes(self.backups[key]); return p
  def upload_backup(self, path, name):
    self.backups[name] = Path(path).read_bytes(); return name
  def restore(self, key):
    operation = 'restore-backup:' + key
    record = {'version': 1, 'transport': self.identity, 'operation': operation}
    self.marker.write_text(json.dumps(dict(record, pending=True)))
    self.restore_calls.append(key)
    if self.restore_400_once == len(self.restore_calls):
      self.restore_400_once = None
      self.marker.write_text(json.dumps(dict(record, pending=False, response_status=400)))
      raise O.W.PBError(400, {})
    self.current = self.backups[key]
    self.marker.write_text(json.dumps(dict(record, pending=False)))
  def rollback_retryable(self, key):
    record = json.loads(self.marker.read_text())
    if record['pending']:
      raise O.MutationUncertain('unfinished fixture restore')
    operation = 'restore-backup:' + key
    return record['operation'] != operation or record.get('response_status') == 400
  def wait_healthy(self):
    return self.health_results.pop(0) if self.health_results else True


def test_full_offline_run_restores_backup_without_power_off(tmp_path):
  db = tmp_path / 'remote.db'
  schema(db)
  archive = make_zip(tmp_path / 'remote.zip', db, b'auxiliary-unchanged').read_bytes()
  transport = MemoryTransport(archive)
  state = tmp_path / 'writes.json'
  result = O.run(transport, state, lambda scope: rows(scope), O.DAILY, TODAY, BASE)
  assert result['success'] and result['healthy'] and transport.on
  receipt = json.loads(O._receipt_path(state).read_text())
  assert receipt['phase'] == 'complete'
  assert any(key.endswith('-original.zip') for key in transport.backups)
  installed = tmp_path / 'installed.zip'
  installed.write_bytes(transport.current)
  data_db = O._backup_db(installed, tmp_path / 'installed.db')
  with sqlite3.connect(data_db) as conn:
    assert conn.execute('SELECT COUNT(*) FROM source_entries').fetchone()[0] > 0
  with zipfile.ZipFile(installed) as zf:
    assert zf.read('auxiliary.db') == b'auxiliary-unchanged'
  assert O._logical_db_digest(data_db) == O._logical_db_digest(Path(receipt['directory']) / 'staged.db')


def test_rollback_health_timeout_recovers_on_later_invocation(tmp_path):
  db = tmp_path / 'remote.db'
  schema(db)
  archive = make_zip(tmp_path / 'remote.zip', db).read_bytes()
  transport = MemoryTransport(archive)
  transport.health_results = [False, False, True]
  state = tmp_path / 'writes.json'
  with pytest.raises(RuntimeError, match='rollback is awaiting'):
    O.run(transport, state, lambda scope: rows(scope), O.DAILY, TODAY, BASE)
  receipt = json.loads(O._receipt_path(state).read_text())
  assert receipt['phase'] == 'rolling_back'
  assert transport.current == archive

  result = O.run(transport, state, lambda scope: rows(scope), O.DAILY, TODAY, BASE)
  assert result['success'] and result['healthy']
  assert json.loads(O._receipt_path(state).read_text())['phase'] == 'complete'


def test_rollback_retries_confirmed_400_after_health_returns(tmp_path):
  db = tmp_path / 'remote.db'
  schema(db)
  archive = make_zip(tmp_path / 'remote.zip', db).read_bytes()
  transport = MemoryTransport(archive)
  transport.health_results = [False, True, True]
  transport.restore_400_once = 2
  state = tmp_path / 'writes.json'
  with pytest.raises(RuntimeError, match='health check failed'):
    O.run(transport, state, lambda scope: rows(scope), O.DAILY, TODAY, BASE)
  receipt = json.loads(O._receipt_path(state).read_text())
  assert receipt['phase'] == 'aborted'
  assert transport.current == archive
  assert len(transport.restore_calls) == 3
  assert transport.restore_calls[1] == transport.restore_calls[2]


def test_confirmed_400_rollback_recovers_after_health_returns_later(tmp_path):
  db = tmp_path / 'remote.db'
  schema(db)
  archive = make_zip(tmp_path / 'remote.zip', db).read_bytes()
  transport = MemoryTransport(archive)
  transport.health_results = [False, False, True]
  transport.restore_400_once = 2
  state = tmp_path / 'writes.json'
  with pytest.raises(RuntimeError, match='rollback is awaiting'):
    O.run(transport, state, lambda scope: rows(scope), O.DAILY, TODAY, BASE)
  receipt = json.loads(O._receipt_path(state).read_text())
  assert receipt['phase'] == 'rolling_back'
  assert receipt['rollback_restore_status'] == 400

  result = O.run(transport, state, lambda scope: rows(scope), O.DAILY, TODAY, BASE)
  assert result['success'] and result['healthy']
  assert len(transport.restore_calls) >= 4
  assert transport.restore_calls[1] == transport.restore_calls[2]
  assert json.loads(O._receipt_path(state).read_text())['phase'] == 'complete'


def test_configure_rejects_secret_left_in_disabled_r2_settings(monkeypatch, tmp_path):
  db = tmp_path / 'seed.db'
  make_db(db, 'stable')
  archive = make_zip(tmp_path / 'seed.zip', db)
  transport, api, _ = make_transport(monkeypatch, tmp_path, archive.read_bytes())
  api.settings['backups']['s3']['secret'] = 'fixture-secret'
  with pytest.raises(RuntimeError, match='inactive backup S3 settings are nonempty'):
    transport.configure(tmp_path / 'state.json')
  assert not transport.r2_active


def test_configure_rejects_unreadable_secret_in_disabled_r2_settings(monkeypatch, tmp_path):
  db = tmp_path / 'seed.db'
  make_db(db, 'stable')
  archive = make_zip(tmp_path / 'seed.zip', db)
  transport, api, _ = make_transport(monkeypatch, tmp_path, archive.read_bytes())
  api.omit_secret_on_get = True

  with pytest.raises(RuntimeError, match='unverifiable'):
    transport.configure(tmp_path / 'state.json')
  assert not transport.r2_active


@pytest.mark.parametrize('settings', [
  None, {}, {'backups': {}}, {'backups': {'s3': None}},
  {'backups': {'s3': {'endpoint': '', 'region': '', 'bucket': '', 'accessKey': '',
                      'secret': '', 'forcePathStyle': False}}},
  {'backups': {'s3': {'enabled': None, 'endpoint': '', 'region': '', 'bucket': '',
                      'accessKey': '', 'secret': '', 'forcePathStyle': False}}},
])
def test_configure_rejects_unreadable_s3_settings(monkeypatch, tmp_path, settings):
  db = tmp_path / 'seed.db'
  make_db(db, 'stable')
  archive = make_zip(tmp_path / 'seed.zip', db)
  transport, api, _ = make_transport(monkeypatch, tmp_path, archive.read_bytes())
  api.settings = settings

  with pytest.raises(RuntimeError, match='could not be inspected'):
    transport.configure(tmp_path / 'state.json')
  assert not transport.r2_active


def test_r2_clear_rejects_missing_enabled_state(monkeypatch, tmp_path):
  db = tmp_path / 'seed.db'
  make_db(db, 'stable')
  archive = make_zip(tmp_path / 'seed.zip', db)
  transport, api, _ = make_transport(monkeypatch, tmp_path, archive.read_bytes())
  monkeypatch.setattr(transport, '_aws_env', lambda: {
    'AWS_ACCESS_KEY_ID': 'fixture-access', 'AWS_SECRET_ACCESS_KEY': 'fixture-secret'})
  transport.configure_r2()
  api.omit_enabled_on_get = True

  with pytest.raises(P.APIError, match='could not be verified cleared'):
    transport.clear_r2()
  assert transport.r2_active


def test_temporary_r2_settings_are_journaled_and_cleared(monkeypatch, tmp_path):
  db = tmp_path / 'seed.db'
  make_db(db, 'stable')
  archive = make_zip(tmp_path / 'seed.zip', db)
  transport, api, _ = make_transport(monkeypatch, tmp_path, archive.read_bytes())
  monkeypatch.setattr(transport, '_aws_env', lambda: {
    'AWS_ACCESS_KEY_ID': 'fixture-access', 'AWS_SECRET_ACCESS_KEY': 'fixture-secret'})
  transport.configure_r2()
  assert api.settings['backups']['s3']['enabled'] is True
  marker = transport._marker.read_text()
  assert 'fixture-access' not in marker and 'fixture-secret' not in marker
  transport.clear_r2()
  assert api.settings['backups']['s3']['enabled'] is False
  assert not api.settings['backups']['s3']['accessKey']
  assert not api.settings['backups']['s3']['secret']
  assert json.loads(transport._marker.read_text())['pending'] is False


def test_r2_clear_fails_if_the_remote_secret_remains(monkeypatch, tmp_path):
  db = tmp_path / 'seed.db'
  make_db(db, 'stable')
  archive = make_zip(tmp_path / 'seed.zip', db)
  transport, api, _ = make_transport(monkeypatch, tmp_path, archive.read_bytes())
  monkeypatch.setattr(transport, '_aws_env', lambda: {
    'AWS_ACCESS_KEY_ID': 'fixture-access', 'AWS_SECRET_ACCESS_KEY': 'fixture-secret'})
  transport.configure_r2()
  api.retain_secret = True
  with pytest.raises(P.APIError, match='could not be verified cleared'):
    transport.clear_r2()
  assert transport.r2_active
  assert api.settings['backups']['s3']['secret'] == 'fixture-secret'
  assert json.loads(transport._marker.read_text())['pending'] is False


def test_r2_clear_fails_if_remote_secret_is_not_readable(monkeypatch, tmp_path):
  db = tmp_path / 'seed.db'
  make_db(db, 'stable')
  archive = make_zip(tmp_path / 'seed.zip', db)
  transport, api, _ = make_transport(monkeypatch, tmp_path, archive.read_bytes())
  monkeypatch.setattr(transport, '_aws_env', lambda: {
    'AWS_ACCESS_KEY_ID': 'fixture-access', 'AWS_SECRET_ACCESS_KEY': 'fixture-secret'})
  transport.configure_r2()
  api.omit_secret_on_get = True
  with pytest.raises(P.APIError, match='could not be verified cleared'):
    transport.clear_r2()
  assert transport.r2_active
  assert json.loads(transport._marker.read_text())['pending'] is False


def test_large_backup_uses_journaled_r2_upload_and_hash_readback(monkeypatch, tmp_path):
  db=tmp_path/'seed.db'
  make_db(db,'stable')
  archive=make_zip(tmp_path/'large.zip',db)
  transport,_,_=make_transport(monkeypatch,tmp_path,archive.read_bytes())
  transport.r2_active=True
  monkeypatch.setattr(P,'MAX_UPLOAD',1)
  monkeypatch.setattr(P.shutil,'which',lambda _: '/fake/aws')
  objects={}
  def aws(args,**kwargs):
    marker=json.loads(transport._marker.read_text())
    src,dst=args[4],args[5]
    assert marker['pending'] is (not str(src).startswith('s3://'))
    assert 'fixture-secret' not in args
    assert kwargs['env']['AWS_SECRET_ACCESS_KEY']=='fixture-secret'
    assert 'fixture-secret' not in transport._marker.read_text()
    if str(src).startswith('s3://'):
      Path(dst).write_bytes(objects[str(src)])
    else:
      objects[str(dst)]=Path(src).read_bytes()
    return types.SimpleNamespace(returncode=0)
  monkeypatch.setattr(P.subprocess,'run',aws)
  monkeypatch.setattr(transport,'_aws_env',lambda:{'AWS_ACCESS_KEY_ID':'fixture-access',
                                                   'AWS_SECRET_ACCESS_KEY':'fixture-secret'})
  key=transport.upload_backup(archive,'radar-r2-fixture.zip')
  assert key=='radar-r2-fixture.zip'
  assert O.digest(archive)==O.digest(Path(archive.with_name(archive.name+'.r2-check')))
  assert json.loads(transport._marker.read_text())['pending'] is False
