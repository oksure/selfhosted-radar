"""Synthetic transport only: no PocketBase service or upstream fetches."""
import copy
import http.client
import stat
import io
import json
import urllib.error
from pathlib import Path

import pytest

from collector import pbwrite as W
from collector import sync
from collector.merge import merge
from collector.sources import RawApp

TODAY = '2026-11-15'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
  def blocked(*args, **kwargs):
    raise AssertionError('unexpected network request')
  monkeypatch.setattr(W.urllib.request, 'urlopen', blocked)


class MemoryPB(W.PB):
  def __init__(self, path):
    self.base = 'https://pb.example.invalid'
    self.stash_path = path
    self.icon_fallbacks = 0
    self.rows = {c: {} for c in W.KEYS}
    self.calls = []
    self.fail = None
    self.after = None

  def _req(self, method, path, body=None, params=None, auth=True):
    parts = path.split('/')
    coll = parts[3]
    rid = parts[5] if len(parts) > 5 else None
    body = copy.deepcopy(body)
    self.calls.append((method, coll, rid, body))
    if self.fail:
      self.fail(method, coll, rid, body)
    records = self.rows[coll]
    if method == 'GET' and rid is None:
      start = (params['page'] - 1) * params['perPage']
      return {'items': copy.deepcopy(sorted(records.values(), key=lambda r: r['id'])[
        start:start + params['perPage']])}
    if method in ('GET', 'PATCH') and rid not in records:
      raise W.PBError(404)
    if method == 'POST':
      if body['id'] in records or any(W._key(coll, r) == W._key(coll, body) for r in records.values()):
        raise W.PBError(400)
      if body.get('project') and body['project'] not in self.rows['projects']:
        raise W.PBError(400)
      rid = body['id']
      records[rid] = body
    elif method == 'PATCH':
      records[rid].update(body)
    if self.after:
      self.after(method, coll, rid, body)
    return copy.deepcopy(records[rid])


@pytest.fixture
def db(tmp_path):
  return MemoryPB(tmp_path / 'writes.json')


def groups(n=1, **changes):
  rows = []
  for i in range(n):
    values = dict(source='awesome', external_id=f'app-{i}', name=f'App {i}',
                  website_url=f'https://example.invalid/app-{i}', stars=100,
                  latest_release_tag='v1', raw_payload={'original': i})
    values.update(changes)
    rows.append(RawApp(**values))
  return merge(rows)


def saved(db):
  return W._load(Path(db.stash_path), db)


@pytest.mark.parametrize('status', [400, 401, 403, 409, 429, 500, 502, 503, 504])
def test_non_404_patch_never_creates(db, status):
  def fail(method, coll, rid, body):
    raise W.PBError(status, {'message': '404 appears only in untrusted text'})
  db.fail = fail
  with pytest.raises(W.PBError) as err:
    db.upsert('projects', {'canonical_key': 'test'})
  assert err.value.status == status
  assert [c[0] for c in db.calls] == ['PATCH']


def test_plain_exception_with_404_is_not_not_found(db):
  def fail(*args):
    raise RuntimeError('upstream 404 in unrelated text')
  db.fail = fail
  with pytest.raises(RuntimeError):
    db.upsert('projects', {'canonical_key': 'test'})
  assert len(db.calls) == 1


def test_structured_404_creates_with_same_fixed_id(db):
  row = db.upsert('projects', {'canonical_key': 'test'})
  assert [c[0] for c in db.calls] == ['PATCH', 'POST']
  assert db.calls[0][2] == row['id'] == db.calls[1][3]['id']
  db.upsert('projects', {'canonical_key': 'test'})
  assert len(db.rows['projects']) == 1


def test_snapshots_persist_and_same_day_is_idempotent(db):
  data = groups()
  original = copy.deepcopy(data)
  first = W.run(db, data, today=TODAY)
  assert first['complete'] and first['errors'] == 0
  assert [first[k] for k in W.COUNTERS] == [1, 0, 1, 1, 1]
  assert len(db.rows['repo_snapshots']) == 1
  event = next(iter(db.rows['surveillance_events'].values()))
  event['is_read'] = True
  second = W.run(db, data, today=TODAY)
  assert second['complete'] and second['attempted'] == 0
  assert len(db.rows['repo_snapshots']) == len(db.rows['surveillance_events']) == 1
  assert event['is_read'] is True
  assert data == original
  assert Path(db.stash_path).stat().st_mode & 0o777 == 0o600


def test_more_than_600_rows_are_all_accounted_and_resumable(db):
  data = groups(601)
  stats = W.run(db, data, today=TODAY, max_writes=200)
  assert stats['planned'] == 2404
  assert stats['attempted'] == stats['confirmed_total'] == 200
  assert stats['pending'] == 2204 and not stats['complete']
  assert sum(stats['pending_' + c] for c in W.KEYS) == stats['pending']
  # Explicit bounded invocations, no watcher or network polling.
  for _ in range(12):
    stats = W.flush(db, max_writes=200)
    assert stats['attempted'] <= 200 and stats['errors'] == 0
  assert stats['complete'] and stats['confirmed_total'] == 2404
  assert {c: len(r) for c, r in db.rows.items()} == dict.fromkeys(W.KEYS, 601)
  # Replanning must page past the first 500 existing records in every collection.
  assert W.run(db, data, today=TODAY)['attempted'] == 0


@pytest.mark.parametrize('failed_coll', list(W.KEYS))
def test_failure_counters_retain_failed_and_unattempted_work(db, failed_coll):
  def fail(method, coll, rid, body):
    if method != 'GET' and coll == failed_coll:
      raise W.PBError(403)
  db.fail = fail
  stats = W.run(db, groups(), today=TODAY)
  assert stats['errors'] == 1 and not stats['complete']
  assert stats['failure']['collection'] == failed_coll
  assert stats['failure']['http_status'] == 403
  assert stats['pending'] == stats['planned'] - stats['confirmed_total'] > 0
  assert sum(stats[k] for k in W.COUNTERS) == stats['confirmed_total']
  assert saved(db)['operations'][saved(db)['next']]['collection'] == failed_coll
  db.fail = None
  assert W.flush(db)['complete']
  assert all(len(r) == 1 for r in db.rows.values())


@pytest.mark.parametrize('coll', list(W.KEYS))
def test_committed_post_with_lost_response_replays_without_duplicates(db, coll):
  def lose(method, collection, rid, body):
    if method == 'POST' and collection == coll:
      raise urllib.error.URLError('response lost after commit')
  db.after = lose
  stats = W.run(db, groups(), today=TODAY)
  assert stats['errors'] == 1 and stats['pending'] > 0
  assert len(db.rows[coll]) == 1
  db.after = None
  stats = W.flush(db)
  assert stats['complete'] and all(len(r) == 1 for r in db.rows.values())
  assert sum(c[0] == 'POST' and c[1] == coll for c in db.calls) == 1


def test_interrupted_checkpoint_replays_confirmed_id(db, monkeypatch):
  real_save = W._save
  def interrupted(path, payload):
    if str(path).endswith('.progress'):
      raise KeyboardInterrupt()
    return real_save(path, payload)
  monkeypatch.setattr(W, '_save', interrupted)
  with pytest.raises(KeyboardInterrupt):
    W.run(db, groups(), today=TODAY)
  assert len(db.rows['projects']) == 1 and saved(db)['next'] == 0
  monkeypatch.setattr(W, '_save', real_save)
  assert W.flush(db)['complete']
  assert all(len(r) == 1 for r in db.rows.values())


def test_checkpoint_io_error_is_incomplete(db, monkeypatch):
  real_save = W._save
  def full(path, payload):
    if str(path).endswith('.progress'):
      raise OSError('disk full')
    return real_save(path, payload)
  monkeypatch.setattr(W, '_save', full)
  stats = W.run(db, groups(), today=TODAY)
  assert stats['errors'] == 1 and stats['pending'] == 4
  assert stats['projects_created'] == 0
  monkeypatch.setattr(W, '_save', real_save)
  assert W.flush(db)['complete']


def test_journal_failure_precedes_any_mutation(db, monkeypatch):
  def full(*args):
    raise OSError('disk full')
  monkeypatch.setattr(W, '_save', full)
  with pytest.raises(OSError):
    W.run(db, groups(), today=TODAY)
  assert not any(c[0] != 'GET' for c in db.calls)


def test_pending_intent_wins_over_new_input_and_keeps_event_detail(db):
  W.run(db, groups(), today=TODAY)
  data = groups(latest_release_tag='v2', archived=True, raw_payload={'release': 'v2'})
  stats = W.run(db, data, today='2026-11-16', max_writes=1)
  assert stats['pending'] > 0
  plan = saved(db)
  release = next(op for op in plan['operations'] if op['body'].get('event_type') == 'new_release')
  assert release['body']['detail'] == {'previous': 'v1', 'current': 'v2'}
  resumed = W.run(db, groups(latest_release_tag='v3'), today='2026-11-17')
  assert resumed['resumed'] and resumed['date'] == '2026-11-16' and resumed['complete']
  assert next(iter(db.rows['projects'].values()))['latest_release_tag'] == 'v2'
  assert any(r.get('detail') == release['body']['detail'] for r in db.rows['surveillance_events'].values())
  assert any(r['raw_payload'] == {'release': 'v2'} for r in db.rows['source_entries'].values())


def test_restricted_sources_preserve_existing_presence_and_ids(db):
  W.run(db, groups(), today=TODAY)
  project = next(iter(db.rows['projects'].values()))
  old_id, old_slug = project['id'], project['slug']
  stats = W.run(db, groups(source='selfhst', latest_release_tag=''), today='2026-11-16')
  assert stats['complete']
  assert project['sources'] == ['awesome', 'selfhst']
  assert (project['id'], project['slug'], project['latest_release_tag']) == (old_id, old_slug, 'v1')
  assert len(db.rows['source_entries']) == 2
  assert any(r['event_type'] == 'new_in_selfhst' for r in db.rows['surveillance_events'].values())


@pytest.mark.parametrize('method', ['PATCH', 'POST'])
def test_invalid_optional_icon_is_omitted_but_original_evidence_survives(db, method):
  if method == 'PATCH':
    W.run(db, groups(icon_url='https://example.invalid/old.png'), today=TODAY)
  def reject(verb, coll, rid, body):
    if verb == method and coll == 'projects' and body.get('icon_url') == 'bad-icon':
      raise W.PBError(400, {'data': {'icon_url': {'code': 'validation_is_url'}}})
  db.fail = reject
  stats = W.run(db, groups(icon_url='bad-icon', raw_payload={'icon': 'bad-icon'}), today='2026-11-16')
  assert stats['complete'] and stats['icon_fallbacks'] == 1
  p = next(iter(db.rows['projects'].values()))
  assert p.get('icon_url', '') == ('https://example.invalid/old.png' if method == 'PATCH' else '')
  assert next(iter(db.rows['source_entries'].values()))['raw_payload'] == {'icon': 'bad-icon'}
  assert any(op['body'].get('icon_url') == 'bad-icon' for op in saved(db)['operations'])


@pytest.mark.parametrize('fields', [
  {'icon_url': {'code': 'validation_required'}},
  {'icon_url': {'code': 'validation_is_url'}, 'name': {'code': 'validation_required'}},
  {'homepage_url': {'code': 'validation_is_url'}}, None,
])
def test_other_validation_errors_are_not_sanitised(db, fields):
  def reject(*args):
    raise W.PBError(400, {'data': fields})
  db.fail = reject
  with pytest.raises(W.PBError):
    db.upsert('projects', {'canonical_key': 'test', 'icon_url': 'bad'})
  assert len(db.calls) == 1


def test_legacy_random_ids_resolved_by_natural_keys(db):
  W.run(db, groups(), today=TODAY)
  for coll in ('source_entries', 'repo_snapshots', 'surveillance_events'):
    row = next(iter(db.rows[coll].values()))
    db.rows[coll].clear()
    row['id'] = 'legacy000000001'
    db.rows[coll][row['id']] = row
  stats = W.run(db, groups(), today=TODAY)
  assert stats['complete'] and stats['attempted'] == 0
  assert all(len(r) == 1 for r in db.rows.values())


def test_lock_and_target_mismatch_fail_closed(db):
  with W._lock(Path(db.stash_path)):
    with pytest.raises(RuntimeError, match='locked'):
      W.run(db, groups(), today=TODAY)
  W.run(db, groups(), today=TODAY, max_writes=1)
  db.base = 'https://other.example.invalid'
  db.calls.clear()
  with pytest.raises(ValueError, match='target mismatch'):
    W.flush(db)
  assert db.calls == []


@pytest.mark.parametrize('content', ['{broken', '[]', '{"version":0}'])
def test_corrupt_or_legacy_journal_is_not_overwritten(db, content):
  Path(db.stash_path).write_text(content)
  with pytest.raises(ValueError):
    W.run(db, groups(), today=TODAY)
  assert Path(db.stash_path).read_text() == content and db.calls == []


def test_corrupt_progress_is_not_ignored(db):
  W.run(db, groups(), today=TODAY, max_writes=1)
  progress = Path(str(db.stash_path) + '.progress')
  progress.write_text('{broken')
  db.calls.clear()
  with pytest.raises(ValueError):
    W.flush(db)
  assert db.calls == [] and progress.read_text() == '{broken'


@pytest.mark.parametrize('coll', ['repo_snapshots', 'surveillance_events'])
def test_immutable_read_failure_never_creates(db, coll):
  def denied(*args):
    raise W.PBError(403)
  db.fail = denied
  row = {'project': 'test', 'captured_date': TODAY, 'event_type': 'new_release', 'occurred_at': TODAY}
  with pytest.raises(W.PBError):
    db.upsert(coll, row)
  assert [c[0] for c in db.calls] == ['GET']


def test_committed_patch_with_lost_response_retains_release_event(db):
  W.run(db, groups(), today=TODAY)
  def lose(method, coll, rid, body):
    if method == 'PATCH' and coll == 'projects':
      raise urllib.error.URLError('lost project update response')
  db.after = lose
  stats = W.run(db, groups(latest_release_tag='v2'), today='2026-11-16')
  assert stats['errors'] == 1 and stats['pending'] == 1
  release = next(r for r in db.rows['surveillance_events'].values() if r['event_type'] == 'new_release')
  release['is_read'] = True
  db.after = None
  assert W.flush(db)['complete']
  assert release['detail'] == {'previous': 'v1', 'current': 'v2'} and release['is_read']
  assert len([r for r in db.rows['surveillance_events'].values() if r['event_type'] == 'new_release']) == 1


def test_low_level_import_does_not_suppress_failure(db):
  def fail(*args):
    raise W.PBError(403)
  db.fail = fail
  with pytest.raises(W.PBError):
    db.import_records('projects', [{'canonical_key': 'a'}, {'canonical_key': 'b'}])
  assert len(db.calls) == 1


@pytest.mark.parametrize('method', ['POST', 'PATCH'])
def test_transport_does_not_blindly_retry_mutations(monkeypatch, method):
  calls = []
  def fail(req, timeout):
    calls.append(req.method)
    assert timeout == 30
    raise urllib.error.HTTPError(req.full_url, 502, 'bad gateway', {}, io.BytesIO(b'{}'))
  monkeypatch.setattr(W.urllib.request, 'urlopen', fail)
  client = object.__new__(W.PB)
  client.base, client.token = 'https://example.invalid', None
  with pytest.raises(W.PBError) as err:
    client._req(method, '/records', {'id': 'test'})
  assert err.value.status == 502 and calls == [method]


def test_get_retries_and_retry_after_are_bounded(monkeypatch, fake_clock):
  calls = []
  def fail(req, timeout):
    calls.append(req.method)
    raise urllib.error.HTTPError(req.full_url, 429, 'limit', {'Retry-After': '99999'}, io.BytesIO(b'{}'))
  monkeypatch.setattr(W.urllib.request, 'urlopen', fail)
  client = object.__new__(W.PB)
  client.base, client.token = 'https://example.invalid', None
  with pytest.raises(W.PBError):
    client._req('GET', '/records')
  assert calls == ['GET'] * 3 and fake_clock.waits == [30, 30]
  assert client.budget.http_attempts == 3


def test_unconfirmed_empty_write_response_stays_pending(db, monkeypatch):
  real = db._req
  def empty(method, *args, **kwargs):
    result = real(method, *args, **kwargs)
    return None if method == 'POST' else result
  monkeypatch.setattr(db, '_req', empty)
  stats = W.run(db, groups(), today=TODAY)
  assert stats['errors'] == 1 and stats['projects_created'] == 0
  monkeypatch.setattr(db, '_req', real)
  assert W.flush(db)['complete']


def cli_setup(db, monkeypatch):
  monkeypatch.setenv('SELFHOSTED_PB_EMAIL', 'synthetic@example.invalid')
  monkeypatch.setenv('SELFHOSTED_PB_PASS', 'synthetic-test-only')
  monkeypatch.setattr(W, 'PB', lambda *args: db)
  monkeypatch.setattr(sync, 'collect', lambda names: [RawApp(
    source='awesome', external_id='one', name='One', website_url='https://example.invalid')])


def test_cli_incomplete_and_resume_exit_codes(db, monkeypatch, capsys):
  cli_setup(db, monkeypatch)
  assert sync.main(['--max-writes', '1', '--today', TODAY]) == 1
  stats = json.loads(capsys.readouterr().out)
  assert stats['errors'] == 0 and stats['pending'] == 3 and not stats['complete']
  real_collect = sync.collect
  collections = []
  def collected_after_drain(names):
    assert not W.pending(db)
    collections.append(names)
    return real_collect(names)
  monkeypatch.setattr(sync, 'collect', collected_after_drain)
  # Normal invocation first drains, then collects within the remaining cap.
  assert sync.main(['--today', TODAY]) == 0
  result = json.loads(capsys.readouterr().out)
  assert result['resumed'] and result['freshness'] == 'current'
  assert result['collected_after_resume'] and len(collections) == 1
  assert sync.main(['--resume', '--today', TODAY]) == 0


def test_cli_failed_write_is_nonzero_with_truthful_counters(db, monkeypatch, capsys):
  cli_setup(db, monkeypatch)
  def fail(method, *args):
    if method != 'GET':
      raise W.PBError(429)
  db.fail = fail
  assert sync.main(['--today', TODAY]) == 1
  stats = json.loads(capsys.readouterr().out)
  assert stats['errors'] == 1 and stats['projects_created'] == 0 and stats['pending'] == 4


def test_cli_auth_failure_and_missing_resume_fail(db, monkeypatch, capsys):
  cli_setup(db, monkeypatch)
  assert sync.main(['--resume']) == 1
  assert json.loads(capsys.readouterr().out)['complete'] is False
  def no_auth(*args):
    raise W.PBError(401)
  monkeypatch.setattr(W, 'PB', no_auth)
  assert sync.main([]) == 1
  assert json.loads(capsys.readouterr().out)['errors'] == 1


def test_cli_dry_run_needs_no_credentials_or_pb(db, monkeypatch, capsys):
  cli_setup(db, monkeypatch)
  monkeypatch.delenv('SELFHOSTED_PB_EMAIL')
  monkeypatch.delenv('SELFHOSTED_PB_PASS')
  assert sync.main(['--dry-run']) == 0
  assert json.loads(capsys.readouterr().out)['dry_run']
  assert db.calls == [] and not Path(db.stash_path).exists()


def test_daily_workload_counts_distinct_sources_and_projects():
  data = groups(2)
  data[0]['sources'].append(RawApp(source='selfhst', external_id='second', name='Second'))
  data[1]['sources'].append(copy.deepcopy(data[0]['sources'][0]))
  assert W.daily_workload(data) == {'projects': 2, 'source_entries': 3, 'baseline_operations': 7}


def test_capacity_assessment_for_2400_projects_and_unknown_legacy_workload():
  workload = {'projects': 2400, 'source_entries': 2400, 'baseline_operations': 7200}
  result = sync.capacity(workload, 200)
  assert result == {'assumption': 'one_invocation_per_day', 'operations_per_run': 200,
                    'baseline_operations_per_day': 7200, 'shortfall_operations_per_day': 7000,
                    'assessment': 'insufficient'}
  assert sync.capacity(None, 200)['assessment'] == 'unknown'
  assert sync.capacity(dict(workload, baseline_operations=0), 200)['assessment'] == 'unknown'
  exact = {'projects': 60, 'source_entries': 80, 'baseline_operations': 200}
  assert sync.capacity(exact, 200)['assessment'] == 'baseline_within_cap'


def test_normal_fails_on_capacity_even_when_current_plan_completes(db, monkeypatch, capsys):
  cli_setup(db, monkeypatch)
  monkeypatch.setattr(sync, 'collect', lambda names: [r for g in groups(70) for r in g['sources']])
  # Explicitly finish the initial plan using bounded invocations.
  assert sync.main(['--today', TODAY]) == 1
  first = json.loads(capsys.readouterr().out)
  assert first['planned'] == 280 and first['pending'] == 80
  assert 'capacity_insufficient' in first['failure_reasons']
  assert sync.main(['--resume', '--today', TODAY]) == 0
  resumed = json.loads(capsys.readouterr().out)
  assert resumed['complete'] and resumed['daily_capacity']['assessment'] == 'insufficient'
  # The next day's 210-operation baseline is incomplete after the same cap.
  assert sync.main(['--today', '2026-11-16']) == 1
  tomorrow = json.loads(capsys.readouterr().out)
  assert tomorrow['planned'] == 210 and tomorrow['pending'] == 10
  assert tomorrow['daily_capacity']['baseline_operations_per_day'] == 210
  assert sync.main(['--today', '2026-11-16']) == 1
  finished = json.loads(capsys.readouterr().out)
  assert finished['complete'] and finished['freshness'] == 'current'
  assert finished['failure_reasons'] == ['capacity_insufficient']


def test_consecutive_daily_runs_report_backlog_and_stale_completion(db, monkeypatch, capsys):
  cli_setup(db, monkeypatch)
  collections = []
  def collect(names):
    collections.append(True)
    return [r for g in groups(70) for r in g['sources']]
  monkeypatch.setattr(sync, 'collect', collect)
  assert sync.main(['--today', TODAY]) == 1
  first = json.loads(capsys.readouterr().out)
  assert first['pending'] == 80 and first['daily_capacity']['assessment'] == 'insufficient'
  assert sync.main(['--today', '2026-11-16']) == 1
  second = json.loads(capsys.readouterr().out)
  assert not second['complete'] and second['resumed'] and second['date'] == '2026-11-16'
  assert second['drained_plan']['date'] == TODAY and second['attempted'] == 200
  assert second['freshness'] == 'incomplete' and len(collections) == 2
  assert sync.main(['--today', '2026-11-17']) == 1
  third = json.loads(capsys.readouterr().out)
  assert not third['complete'] and third['pending'] > 0 and third['attempted'] == 200
  assert third['daily_capacity']['baseline_operations_per_day'] == 210
  assert len(collections) == 3
  assert sync.main(['--today', '2026-11-18']) == 1
  fourth = json.loads(capsys.readouterr().out)
  assert not fourth['complete'] and fourth['freshness'] == 'incomplete'
  assert fourth['attempted'] == 200
  assert set(fourth['failure_reasons']) == {'pending_writes', 'capacity_insufficient'}
  assert len(collections) == 4


def test_legacy_journal_capacity_is_unknown_not_success(db, monkeypatch, capsys):
  cli_setup(db, monkeypatch)
  W.run(db, groups(), today=TODAY, max_writes=1)
  path = Path(db.stash_path)
  journal = json.loads(path.read_text())
  del journal['daily_workload']
  path.write_text(json.dumps(journal))
  assert sync.main(['--today', TODAY, '--max-writes', '3']) == 1
  stats = json.loads(capsys.readouterr().out)
  assert stats['complete'] and stats['freshness'] == 'current'
  assert stats['daily_capacity']['assessment'] == 'unknown'
  assert stats['failure_reasons'] == ['capacity_unknown', 'scope_unknown']


def test_explicit_resume_can_complete_stale_plan(db, monkeypatch, capsys):
  cli_setup(db, monkeypatch)
  W.run(db, groups(), today=TODAY, max_writes=1)
  assert sync.main(['--resume', '--today', '2026-11-16']) == 0
  result = json.loads(capsys.readouterr().out)
  assert result['complete'] and result['success'] and result['freshness'] == 'stale'
  assert not result['failure_reasons']


def test_dry_run_exposes_insufficient_capacity_without_writes(db, monkeypatch, capsys):
  cli_setup(db, monkeypatch)
  monkeypatch.setattr(sync, 'collect', lambda names: [r for g in groups(70) for r in g['sources']])
  assert sync.main(['--dry-run']) == 0
  stats = json.loads(capsys.readouterr().out)
  assert stats['daily_capacity']['assessment'] == 'insufficient'
  assert stats['daily_capacity']['shortfall_operations_per_day'] == 10
  assert db.calls == [] and not Path(db.stash_path).exists()


class TransactionPB(MemoryPB):
  """PocketBase batch contract: ordered results and whole-request rollback."""
  def __init__(self, path):
    super().__init__(path)
    self.batches = []
    self.before_batch = None
    self.after_commit = None
    self.result = lambda rows: rows
    self.lookups = []
    self.response_status_field = 'code'
    self.batch_limit = 5 * 1024 * 1024
    self.batch_bytes = []

  def _req(self, method, path, body=None, params=None, auth=True):
    if path != '/api/batch':
      if method == 'GET' and params and params.get('filter'):
        coll = path.split('/')[3]
        self.calls.append((method, coll, None, None))
        self.lookups.append((coll, copy.deepcopy(params)))
        if self.fail:
          self.fail(method, coll, None, None)
        ids = [json.loads(part.split('=', 1)[1]) for part in params['filter'].split(' || ')]
        assert params['perPage'] == 50 and params['skipTotal'] == 1 and len(ids) <= 50
        fields = params['fields'].split(',')
        return {'items': [{k: copy.deepcopy(row[k]) for k in fields} for rid, row in
                          self.rows[coll].items() if rid in ids]}
      return super()._req(method, path, body, params, auth)
    assert method == 'POST' and set(body) == {'requests'}
    size = len(W._encode_body(body))
    self.batch_bytes.append(size)
    if size > self.batch_limit:
      raise W.PBError(413)
    requests = copy.deepcopy(body['requests'])
    assert 1 <= len(requests) <= 50
    self.batches.append(requests)
    if self.before_batch:
      self.before_batch()
    original = copy.deepcopy(self.rows)
    results = []
    for i, request in enumerate(requests):
      verb, url, payload = (request[k] for k in ('method', 'url', 'body'))
      coll = url.split('/')[3]
      assert url == f'/api/collections/{coll}/records'
      assert verb == ('POST' if coll in W.IMMUTABLE else 'PUT')
      if verb == 'PUT':
        verb = 'PATCH' if payload['id'] in self.rows[coll] else 'POST'
        if verb == 'PATCH':
          url += '/' + payload['id']
      try:
        row = super()._req(verb, url, payload)
      except W.PBError as e:
        self.rows = original
        raise W.PBError(400, {'data': {'requests': {str(i): {
          'code': 'batch_request_failed',
          'response': {self.response_status_field: e.status, 'data': e.data.get('data', {})},
        }}}}) from None
      results.append({'status': 200, 'body': row})
    if self.after_commit:
      self.after_commit()
    return self.result(results)


@pytest.fixture
def batch_db(tmp_path):
  return TransactionPB(tmp_path / 'writes.json')


def seed_plan(db, count=51):
  journal = W._plan(db, groups((count + 3) // 4), TODAY)
  journal['operations'] = journal['operations'][:count]
  W._save(Path(db.stash_path), journal)
  return journal


def test_batch_50_plus_1_ordered_and_checkpointed_once_per_slice(batch_db, monkeypatch):
  journal = seed_plan(batch_db)
  checkpoints = []
  real_save = W._save
  def checkpoint(path, payload):
    checkpoints.append(copy.deepcopy(payload))
    real_save(path, payload)
  monkeypatch.setattr(W, '_save', checkpoint)
  stats = W.flush(batch_db, batch_size=50)
  assert stats['complete'] and stats['confirmed_total'] == stats['attempted'] == 51
  assert [len(b) for b in batch_db.batches] == [50, 1]
  assert [r['body'] for b in batch_db.batches for r in b] == [
    op['body'] for op in journal['operations']]
  assert checkpoints == [{'generation': journal['generation'], 'next': n} for n in (50, 51)]
  assert sum(stats[k] for k in W.COUNTERS) == 51


@pytest.mark.parametrize('cap', [1, 49, 50, 53])
def test_batch_record_cap_and_resume(batch_db, cap):
  stats = W.run(batch_db, groups(20), today=TODAY, batch_size=50, max_writes=cap)
  assert stats['attempted'] == stats['confirmed_total'] == cap
  assert stats['pending'] == 80 - cap and not stats['complete']
  assert [len(b) for b in batch_db.batches] == ([50, 3] if cap == 53 else [cap])
  assert W.flush(batch_db, batch_size=50)['complete']
  assert all(len(rows) == 20 for rows in batch_db.rows.values())


def test_batch_lost_after_commit_replay_preserves_immutable_detail_and_read(batch_db):
  def lost():
    raise urllib.error.URLError('lost after commit')
  batch_db.after_commit = lost
  stats = W.run(batch_db, groups(), today=TODAY, batch_size=50)
  assert stats['errors'] == 1 and stats['pending'] == 4 and saved(batch_db)['next'] == 0
  assert len(batch_db.batches) == 1 and all(len(r) == 1 for r in batch_db.rows.values())
  event = next(iter(batch_db.rows['surveillance_events'].values()))
  event['is_read'] = True
  event['detail'] = {'preserve': 'committed evidence'}
  snapshot = next(iter(batch_db.rows['repo_snapshots'].values()))
  snapshot['stars'] = 999
  batch_db.after_commit = None
  stats = W.flush(batch_db, batch_size=50)
  assert stats['complete'] and stats['confirmed_total'] == 4
  assert event['is_read'] and event['detail'] == {'preserve': 'committed evidence'}
  assert snapshot['stars'] == 999
  assert [r['method'] for r in batch_db.batches[1]] == ['PUT', 'PUT']
  assert all(len(r) == 1 for r in batch_db.rows.values())


def test_batch_middle_failure_rolls_back_and_never_retries(batch_db):
  def reject(method, coll, rid, body):
    if method == 'POST' and coll == 'repo_snapshots':
      raise W.PBError(403)
  batch_db.fail = reject
  stats = W.run(batch_db, groups(), today=TODAY, batch_size=50)
  assert stats['errors'] == 1 and stats['pending'] == 4
  assert stats['confirmed_total'] == sum(stats[k] for k in W.COUNTERS) == 0
  assert all(not rows for rows in batch_db.rows.values())
  assert len(batch_db.batches) == 1 and saved(batch_db)['next'] == 0
  batch_db.fail = None
  assert W.flush(batch_db, batch_size=50)['complete']


def test_batch_immutable_insert_race_is_create_only_and_rolls_back(batch_db):
  journal = seed_plan(batch_db, 4)
  op = journal['operations'][2]
  existing = dict(op['body'], stars=1234)
  def race():
    batch_db.rows['repo_snapshots'][existing['id']] = copy.deepcopy(existing)
  batch_db.before_batch = race
  stats = W.flush(batch_db, batch_size=50)
  assert stats['errors'] == 1 and stats['pending'] == 4
  assert batch_db.rows['repo_snapshots'][existing['id']] == existing
  assert not batch_db.rows['projects'] and not batch_db.rows['source_entries']
  assert len(batch_db.batches) == 1
  batch_db.before_batch = None
  assert W.flush(batch_db, batch_size=50)['complete']
  assert batch_db.rows['repo_snapshots'][existing['id']]['stars'] == 1234


@pytest.mark.parametrize('mode', ['none', 'dict', 'short', 'extra', 'reversed', 'status',
                                 'missing_body', 'wrong_id', 'wrong_key', 'missing_key'])
def test_batch_malformed_results_retain_whole_slice(batch_db, mode):
  def corrupt(rows):
    if mode == 'none':
      return None
    if mode == 'dict':
      return {'results': rows}
    if mode == 'short':
      return rows[:-1]
    if mode == 'extra':
      return rows + rows[:1]
    if mode == 'reversed':
      return rows[::-1]
    if mode == 'status':
      rows[1]['status'] = 201
    elif mode == 'missing_body':
      del rows[1]['body']
    elif mode == 'wrong_id':
      rows[1]['body']['id'] = 'wrong'
    elif mode == 'wrong_key':
      rows[1]['body']['external_id'] = 'wrong'
    elif mode == 'missing_key':
      del rows[1]['body']['external_id']
    return rows
  batch_db.result = corrupt
  stats = W.run(batch_db, groups(), today=TODAY, batch_size=50)
  assert stats['errors'] == 1 and stats['pending'] == 4
  assert stats['confirmed_total'] == saved(batch_db)['next'] == 0
  assert len(batch_db.batches) == 1
  batch_db.result = lambda rows: rows
  assert W.flush(batch_db, batch_size=50)['complete']


def test_batch_checkpoint_failure_retains_whole_slice(batch_db, monkeypatch):
  real_save = W._save
  def full(path, payload):
    if str(path).endswith('.progress'):
      raise OSError('disk full')
    real_save(path, payload)
  monkeypatch.setattr(W, '_save', full)
  stats = W.run(batch_db, groups(), today=TODAY, batch_size=50)
  assert stats['errors'] == 1 and stats['failure']['error_type'] == 'CheckpointError'
  assert stats['pending'] == 4 and stats['confirmed_total'] == saved(batch_db)['next'] == 0
  assert all(len(rows) == 1 for rows in batch_db.rows.values())
  monkeypatch.setattr(W, '_save', real_save)
  assert W.flush(batch_db, batch_size=50)['complete']


@pytest.mark.parametrize('cursor', [-1, 5, True, '4', None])
def test_batch_malformed_checkpoint_precedes_requests(batch_db, cursor):
  journal = seed_plan(batch_db, 4)
  W._save(Path(str(batch_db.stash_path) + '.progress'),
          {'generation': journal['generation'], 'next': cursor})
  batch_db.calls.clear()
  with pytest.raises(ValueError):
    W.flush(batch_db, batch_size=50)
  assert batch_db.calls == batch_db.batches == []


@pytest.mark.parametrize('coll', W.IMMUTABLE)
@pytest.mark.parametrize('status', [403, 429, 500])
def test_batch_immutable_get_failure_prevents_any_mutation(batch_db, coll, status):
  seed_plan(batch_db, 4)
  def reject(method, collection, rid, body):
    if method == 'GET' and collection == coll:
      raise W.PBError(status)
  batch_db.fail = reject
  stats = W.flush(batch_db, batch_size=50)
  assert stats['errors'] == 1 and stats['pending'] == 4
  assert not batch_db.batches and all(not rows for rows in batch_db.rows.values())


def test_batch_immutable_get_identity_mismatch_prevents_mutation(batch_db):
  journal = seed_plan(batch_db, 4)
  op = journal['operations'][2]
  batch_db.rows[op['collection']][op['body']['id']] = dict(op['body'], project='wrong')
  stats = W.flush(batch_db, batch_size=50)
  assert stats['errors'] == 1 and stats['pending'] == 4 and not batch_db.batches


def test_batch_existing_immutable_only_slice_confirms_without_post(batch_db):
  journal = seed_plan(batch_db, 4)
  journal['operations'] = [op for op in journal['operations'] if op['collection'] in W.IMMUTABLE]
  for op in journal['operations']:
    batch_db.rows[op['collection']][op['body']['id']] = dict(op['body'], is_read=True)
  W._save(Path(batch_db.stash_path), journal)
  stats = W.flush(batch_db, batch_size=50)
  assert stats['complete'] and stats['snapshots'] == stats['events'] == 1
  assert stats['attempted'] == saved(batch_db)['next'] == 2 and not batch_db.batches


def test_batch_bad_tail_retains_previously_checkpointed_prefix(batch_db):
  seed_plan(batch_db)
  batch_db.result = lambda rows: rows if len(rows) == 50 else []
  stats = W.flush(batch_db, batch_size=50)
  assert stats['errors'] == 1 and stats['pending'] == 1
  assert stats['confirmed_total'] == saved(batch_db)['next'] == 50
  assert sum(stats[k] for k in W.COUNTERS) == 50
  assert [len(b) for b in batch_db.batches] == [50, 1]
  batch_db.result = lambda rows: rows
  assert W.flush(batch_db, batch_size=50)['complete']


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('status_field', ['status', 'code'])
def test_batch_indexed_icon_fallback_preserves_journal_and_previous_icon(batch_db, existing, status_field):
  batch_db.response_status_field = status_field
  if existing:
    assert W.run(batch_db, groups(icon_url='https://example.invalid/old.png'),
                 today=TODAY, batch_size=50)['complete']
  batch_db.batches.clear()
  def reject(method, coll, rid, body):
    if method != 'GET' and coll == 'projects' and body.get('icon_url') == 'bad':
      raise W.PBError(400, {'data': {'icon_url': {'code': 'validation_is_url'}}})
  batch_db.fail = reject
  stats = W.run(batch_db, groups(icon_url='bad', raw_payload={'icon': 'bad'}),
                today='2026-11-16', batch_size=50)
  assert stats['complete'] and stats['icon_fallbacks'] == 1
  assert len(batch_db.batches) == 2
  first, second = batch_db.batches
  i = next(i for i, r in enumerate(first) if r['body'].get('icon_url') == 'bad')
  assert i == (len(first) - 1 if existing else 0)
  assert 'icon_url' not in second[i]['body']
  assert all(a == b for j, (a, b) in enumerate(zip(first, second)) if j != i)
  project = next(iter(batch_db.rows['projects'].values()))
  assert project.get('icon_url', '') == ('https://example.invalid/old.png' if existing else '')
  assert next(iter(batch_db.rows['source_entries'].values()))['raw_payload'] == {'icon': 'bad'}
  assert any(op['body'].get('icon_url') == 'bad' for op in saved(batch_db)['operations'])


@pytest.mark.parametrize('mode', ['plain', 'wrong_index', 'leading_zero', 'multiple',
                                 'other_field', 'other_code', 'unstructured', 'server_error'])
def test_batch_icon_fallback_rejects_unproven_rollback(batch_db, monkeypatch, mode):
  attempts = []
  real_req = batch_db._req
  def reject(method, path, body=None, **kwargs):
    if path != '/api/batch':
      return real_req(method, path, body, **kwargs)
    attempts.append(copy.deepcopy(body))
    fields = {'icon_url': {'code': 'validation_is_url'}}
    error = {'code': 'batch_request_failed', 'response': {'code': 400, 'data': fields}}
    errors = {'0': error}
    if mode == 'wrong_index':
      errors = {'4': error}
    elif mode == 'leading_zero':
      errors = {'00': error}
    elif mode == 'multiple':
      errors['1'] = error
    elif mode == 'other_field':
      fields['name'] = {'code': 'validation_required'}
    elif mode == 'other_code':
      fields['icon_url']['code'] = 'validation_required'
    elif mode == 'unstructured':
      error['code'] = 'not_a_rollback'
    data = {'data': fields} if mode == 'plain' else {'data': {'requests': errors}}
    raise W.PBError(500 if mode == 'server_error' else 400, data)
  monkeypatch.setattr(batch_db, '_req', reject)
  stats = W.run(batch_db, groups(icon_url='bad'), today=TODAY, batch_size=50)
  assert stats['errors'] == 1 and stats['pending'] == 4 and stats['icon_fallbacks'] == 0
  assert len(attempts) == 1


def test_batch_icon_fallback_is_only_one_retry(batch_db):
  def reject(method, coll, rid, body):
    if method != 'GET' and coll == 'projects':
      raise W.PBError(400, {'data': {'icon_url': {'code': 'validation_is_url'}}})
  batch_db.fail = reject
  stats = W.run(batch_db, groups(icon_url='bad'), today=TODAY, batch_size=50)
  assert stats['errors'] == 1 and stats['pending'] == 4
  assert len(batch_db.batches) == 2 and all(not rows for rows in batch_db.rows.values())


@pytest.mark.parametrize('size', [0, 51, -1, True, 1.5, '50'])
def test_batch_library_size_validation_precedes_requests(batch_db, size):
  with pytest.raises(ValueError):
    W.run(batch_db, groups(), today=TODAY, batch_size=size)
  with pytest.raises(ValueError):
    W.flush(batch_db, batch_size=size)
  assert batch_db.calls == batch_db.batches == []


@pytest.mark.parametrize('size', ['0', '51', '-1', 'bad'])
def test_batch_cli_size_validation_precedes_requests(batch_db, size):
  with pytest.raises(SystemExit) as err:
    sync.main(['--batch-size', size])
  assert err.value.code == 2 and batch_db.calls == []


def test_batch_cli_run_and_resume_propagate_record_cap(batch_db, monkeypatch, capsys):
  cli_setup(batch_db, monkeypatch)
  assert sync.main(['--today', TODAY, '--batch-size', '50', '--max-writes', '1']) == 1
  assert json.loads(capsys.readouterr().out)['pending'] == 3
  assert sync.main(['--resume', '--today', TODAY, '--batch-size', '50',
                    '--max-writes', '20000']) == 0
  stats = json.loads(capsys.readouterr().out)
  assert [len(b) for b in batch_db.batches] == [1, 3]
  assert stats['complete'] and stats['daily_capacity']['operations_per_run'] == 20000
  assert stats['daily_capacity']['assessment'] == 'baseline_within_cap'


def test_daily_capacity_20000_covers_record_baseline_not_http_budget():
  workload = {'projects': 2400, 'source_entries': 4800, 'baseline_operations': 9600}
  result = sync.capacity(workload, 20000)
  assert result['assessment'] == 'baseline_within_cap'
  assert result['shortfall_operations_per_day'] == 0
  assert result['baseline_operations_per_day'] == 9600
  assert set(result) == {'assumption', 'operations_per_run', 'baseline_operations_per_day',
                         'shortfall_operations_per_day', 'assessment'}


@pytest.mark.parametrize('status_field', ['status', 'code'])
def test_batch_two_rejected_icons_complete_with_bounded_omissions(batch_db, status_field):
  batch_db.response_status_field = status_field
  def reject(method, coll, rid, body):
    if method != 'GET' and coll == 'projects' and body.get('icon_url') == 'bad':
      raise W.PBError(400, {'data': {'icon_url': {'code': 'validation_is_url'}}})
  batch_db.fail = reject
  stats = W.run(batch_db, groups(2, icon_url='bad'), today=TODAY, batch_size=50)
  assert stats['complete'] and stats['icon_fallbacks'] == 2
  assert len(batch_db.batches) == 3
  assert [sum(r['body'].get('icon_url') == 'bad' for r in b) for b in batch_db.batches] == [2, 1, 0]
  assert sum(op['body'].get('icon_url') == 'bad' for op in saved(batch_db)['operations']) == 2


@pytest.mark.parametrize('nested', [{'status': 400, 'code': 403}, {'status': '400'},
                                   {'code': True}, {'status': 400, 'code': 500}])
def test_batch_icon_fallback_rejects_conflicting_or_noninteger_status(batch_db, monkeypatch, nested):
  real_req = batch_db._req
  attempts = []
  def reject(method, path, body=None, **kwargs):
    if path != '/api/batch':
      return real_req(method, path, body, **kwargs)
    attempts.append(body)
    raise W.PBError(400, {'data': {'requests': {'0': {
      'code': 'batch_request_failed', 'response': dict(nested, data={
        'icon_url': {'code': 'validation_is_url'}}),
    }}}})
  monkeypatch.setattr(batch_db, '_req', reject)
  stats = W.run(batch_db, groups(icon_url='bad'), today=TODAY, batch_size=50)
  assert stats['errors'] == 1 and stats['pending'] == 4 and len(attempts) == 1


def test_batch_immutable_list_reads_are_bounded_per_collection_per_slice(batch_db):
  journal = seed_plan(batch_db, 51)
  stats = W.flush(batch_db, batch_size=50)
  assert stats['complete']
  # First 50 operations include both immutable collections; the tail is a snapshot.
  assert [c for c, p in batch_db.lookups] == ['repo_snapshots', 'surveillance_events', 'repo_snapshots']
  for coll, params in batch_db.lookups:
    assert set(params) == {'filter', 'fields', 'page', 'perPage', 'skipTotal'}
    assert params['fields'] == ','.join(('id',) + W.KEYS[coll])
    ids = [json.loads(part.split('=', 1)[1]) for part in params['filter'].split(' || ')]
    assert len(ids) <= 50 and set(ids) <= {
      op['body']['id'] for op in journal['operations'] if op['collection'] == coll}
  assert not any(method == 'GET' and rid for method, coll, rid, body in batch_db.calls)


@pytest.mark.parametrize('mode', ['none', 'missing_items', 'not_list', 'duplicate',
                                 'foreign_id', 'wrong_key', 'missing_key'])
def test_batch_immutable_list_malformed_result_never_means_absent(batch_db, monkeypatch, mode):
  journal = seed_plan(batch_db, 4)
  op = journal['operations'][2]
  real_req = batch_db._req
  def malformed(method, path, body=None, params=None, **kwargs):
    if method != 'GET' or not params or not params.get('filter'):
      return real_req(method, path, body, params=params, **kwargs)
    row = copy.deepcopy(op['body'])
    if mode == 'none':
      return None
    if mode == 'missing_items':
      return {}
    if mode == 'not_list':
      return {'items': {}}
    if mode == 'duplicate':
      return {'items': [row, row]}
    if mode == 'foreign_id':
      row['id'] = 'foreign'
    elif mode == 'wrong_key':
      row['project'] = 'wrong'
    elif mode == 'missing_key':
      del row['captured_date']
    return {'items': [row]}
  monkeypatch.setattr(batch_db, '_req', malformed)
  stats = W.flush(batch_db, batch_size=50)
  assert stats['errors'] == 1 and stats['pending'] == 4
  assert saved(batch_db)['next'] == 0 and not batch_db.batches


WEEKLY = ['awesome', 'selfhst', 'gh_trending', 'gh_popular']


@pytest.mark.parametrize('cap', [5, 30])
def test_weekly_drains_daily_then_collects_required_scope_with_one_cap(batch_db, monkeypatch, capsys, cap):
  W.run(batch_db, groups(), today=TODAY, max_writes=1, requested_sources=['awesome', 'selfhst'])
  old_generation = saved(batch_db)['generation']
  cli_setup(batch_db, monkeypatch)
  calls = []
  def collect(names):
    assert not W.pending(batch_db)
    assert saved(batch_db)['generation'] == old_generation
    calls.append(names)
    return [RawApp(source=n, external_id=n, name='App 0',
                   website_url='https://example.invalid/app-0') for n in names]
  monkeypatch.setattr(sync, 'collect', collect)
  status = sync.main(['--today', TODAY, '--sources', ','.join(WEEKLY),
                      '--max-writes', str(cap), '--batch-size', '50'])
  stats = json.loads(capsys.readouterr().out)
  assert calls == [WEEKLY] and stats['collected_after_resume']
  assert stats['drained_plan']['attempted'] == 3
  assert stats['attempted'] <= cap and stats['scope_assessment'] == 'sufficient'
  assert saved(batch_db)['requested_sources'] == sorted(WEEKLY)
  assert saved(batch_db)['generation'] != old_generation
  if cap == 5:
    assert status == 1 and stats['attempted'] == 5 and stats['pending'] > 0
  else:
    assert status == 0 and stats['complete']
    assert any(r['source'] == 'gh_popular' for r in batch_db.rows['source_entries'].values())


@pytest.mark.parametrize('cap', [1, 3])
@pytest.mark.parametrize('legacy', [False, True])
def test_weekly_never_fetches_before_pending_complete_or_after_cap(batch_db, monkeypatch, capsys, cap, legacy):
  W.run(batch_db, groups(), today=TODAY, max_writes=1, requested_sources=['awesome', 'selfhst'])
  if legacy:
    path = Path(batch_db.stash_path)
    journal = json.loads(path.read_text())
    del journal['requested_sources']
    W._save(path, journal)
  cli_setup(batch_db, monkeypatch)
  def forbidden(names):
    raise AssertionError('must not fetch with pending work or no remaining record budget')
  monkeypatch.setattr(sync, 'collect', forbidden)
  assert sync.main(['--today', TODAY, '--sources', ','.join(WEEKLY),
                    '--max-writes', str(cap), '--batch-size', '50']) == 1
  stats = json.loads(capsys.readouterr().out)
  assert stats['attempted'] == cap and stats['pending'] == 3 - cap
  assert stats['scope_assessment'] == ('unknown' if legacy else 'insufficient')
  assert ('scope_unknown' if legacy else 'scope_insufficient') in stats['failure_reasons']
  assert stats['success'] is False


@pytest.mark.parametrize('batch_size', [1, 50])
@pytest.mark.parametrize('error', [http.client.IncompleteRead(b'private-response'),
                                 http.client.HTTPException('private-response')])
def test_http_read_failure_reports_json_and_preserves_pending(db, monkeypatch, capsys, batch_size, error):
  transport = W.PB._req
  real_req = db._req
  db.token = None
  attempts = []
  class BrokenResponse:
    def __enter__(self):
      return self
    def __exit__(self, *args):
      return False
    def read(self):
      raise error
  def broken(req, timeout):
    attempts.append(req.method)
    return BrokenResponse()
  def request(method, path, body=None, params=None, auth=True):
    if method == 'GET':
      return real_req(method, path, body, params, auth)
    return transport(db, method, path, body, params, auth)
  monkeypatch.setattr(db, '_req', request)
  monkeypatch.setattr(W.urllib.request, 'urlopen', broken)
  cli_setup(db, monkeypatch)
  assert sync.main(['--today', TODAY, '--batch-size', str(batch_size)]) == 1
  output = capsys.readouterr()
  stats = json.loads(output.out)
  assert stats['errors'] == 1 and stats['pending'] == 4 and stats['confirmed_total'] == 0
  assert stats['failure']['error_type'] == 'RuntimeError'
  assert len(attempts) == 1 and saved(db)['next'] == 0
  assert 'private-response' not in output.out + output.err


def test_incomplete_http_error_body_is_sanitized_without_retry(monkeypatch):
  class BrokenBody:
    def read(self, *args):
      raise http.client.IncompleteRead(b'private-error-response')
    def close(self):
      pass
  calls = []
  def broken(req, timeout):
    calls.append(req.method)
    raise urllib.error.HTTPError(req.full_url, 400, 'bad', {}, BrokenBody())
  monkeypatch.setattr(W.urllib.request, 'urlopen', broken)
  client = object.__new__(W.PB)
  client.base, client.token = 'https://example.invalid', None
  with pytest.raises(W.PBError) as err:
    client._req('POST', '/api/batch', {'requests': []})
  assert err.value.status == 400 and err.value.data == {} and calls == ['POST']


def large_entry_plan(db, payload, count=50):
  pid = 'parentrecord001'
  db.rows['projects'][pid] = {'id': pid, 'canonical_key': 'parent', 'slug': 'parent'}
  journal = W._plan(db, [], TODAY, requested_sources=['selfhst'])
  for i in range(count):
    body = {'project': pid, 'source': 'selfhst', 'external_id': str(i),
            'raw_payload': {'original': payload}}
    body['id'] = W._rid('source_entries', body)
    journal['operations'].append({'collection': 'source_entries', 'body': body, 'counter': 'entries'})
  W._save(Path(db.stash_path), journal)
  return journal


@pytest.mark.parametrize('payload', ['x' * 110000, '\ud55c' * 20000], ids=['110k-ascii', 'escaped-unicode'])
def test_batch_encoded_byte_limit_splits_under_fake_5mib_cap(batch_db, monkeypatch, payload):
  journal = large_entry_plan(batch_db, payload)
  full_body = {'requests': [W._batch_request(op) for op in journal['operations']]}
  assert len(W._encode_body(full_body)) > batch_db.batch_limit
  checkpoints = []
  real_save = W._save
  def checkpoint(path, body):
    if str(path).endswith('.progress'):
      checkpoints.append(body['next'])
    real_save(path, body)
  monkeypatch.setattr(W, '_save', checkpoint)
  stats = W.flush(batch_db, batch_size=50, max_writes=50)
  assert stats['complete'] and stats['attempted'] == stats['entries'] == 50
  assert len(batch_db.batches) == len(checkpoints) == 2
  assert all(size <= W.BATCH_MAX_BYTES for size in batch_db.batch_bytes)
  assert [r['body'] for batch in batch_db.batches for r in batch] == [
    op['body'] for op in journal['operations']]
  assert len(batch_db.rows['source_entries']) == 50 and saved(batch_db)['next'] == 50


def test_batch_single_oversize_record_stays_pending_without_send(batch_db):
  journal = large_entry_plan(batch_db, 'x' * W.BATCH_MAX_BYTES, count=1)
  stats = W.flush(batch_db, batch_size=50)
  assert stats['errors'] == 1 and stats['pending'] == 1 and stats['confirmed_total'] == 0
  assert stats['failure']['error_type'] == 'BatchSizeError'
  assert stats['failure']['reason'] == 'record_exceeds_batch_byte_limit'
  assert saved(batch_db)['next'] == 0 and saved(batch_db)['operations'] == journal['operations']
  assert batch_db.batches == batch_db.batch_bytes == []
  assert not batch_db.rows['source_entries']


@pytest.mark.parametrize('response', [{'items': {}}, {'items': ''}, {'items': None}, {}, None])
def test_malformed_inventory_never_replaces_existing_journal_or_project(db, monkeypatch, response):
  assert W.run(db, groups(icon_url='https://example.invalid/icon.png'), today=TODAY)['complete']
  project = copy.deepcopy(next(iter(db.rows['projects'].values())))
  path = Path(db.stash_path)
  progress = Path(str(path) + '.progress')
  prior = path.read_bytes(), progress.read_bytes()
  real_req = db._req
  def malformed(method, url, body=None, params=None, auth=True):
    if method == 'GET' and url == '/api/collections/projects/records':
      return copy.deepcopy(response)
    return real_req(method, url, body, params, auth)
  monkeypatch.setattr(db, '_req', malformed)
  db.calls.clear()
  with pytest.raises(ValueError, match='inventory'):
    W.run(db, groups(latest_release_tag='v2'), today='2026-11-16')
  assert (path.read_bytes(), progress.read_bytes()) == prior
  assert next(iter(db.rows['projects'].values())) == project
  assert not any(method != 'GET' for method, coll, rid, body in db.calls)


@pytest.mark.parametrize('failure', [OSError, http.client.IncompleteRead, EOFError, RuntimeError])
def test_completed_drain_then_collect_failure_retains_confirmed_counters(db, monkeypatch, capsys, failure):
  W.run(db, groups(), today=TODAY, max_writes=1, requested_sources=['awesome'])
  cli_setup(db, monkeypatch)
  def failed_collection(names):
    assert not W.pending(db)
    raise failure(b'private upstream text')
  monkeypatch.setattr(sync, 'collect', failed_collection)
  assert sync.main(['--today', TODAY, '--sources', 'awesome', '--max-writes', '20']) == 1
  output = capsys.readouterr()
  stats = json.loads(output.out)
  assert stats['errors'] == 1 and stats['success'] is False and stats['complete'] is False
  assert stats['phase'] == 'collect' and stats['error_type'] == failure.__name__
  assert stats['pending'] is None and stats['fresh_pending'] is None and stats['planned'] is None
  assert stats['attempted'] == stats['drained_plan']['attempted'] == 3
  assert stats['confirmed_total'] == stats['drained_plan']['confirmed_total'] == 4
  assert stats['drained_plan']['complete'] and stats['drained_plan']['pending'] == 0
  assert [stats[k] for k in W.COUNTERS] == [0, 0, 1, 1, 1]
  assert all(stats[k] == stats['drained_plan'][k] for k in W.COUNTERS)
  assert saved(db)['next'] == 4 and 'private upstream text' not in output.out + output.err
  assert 'rerun the original command' in output.err and 'retry --resume' not in output.err


def test_weekly_rejects_input_after_journal_published_between_pending_and_run(db, monkeypatch, capsys):
  cli_setup(db, monkeypatch)
  monkeypatch.setattr(W, 'pending', lambda *a, **k: False)
  real_run = W.run
  calls = []
  def raced(database, merged, **kwargs):
    calls.append(kwargs['max_writes'])
    if len(calls) == 1:
      real_run(database, groups(latest_release_tag='v2', raw_payload={'version': 2}),
               today=TODAY, max_writes=3, requested_sources=['awesome'])
    return real_run(database, merged, **kwargs)
  monkeypatch.setattr(W, 'run', raced)
  monkeypatch.setattr(sync, 'collect', lambda names: [RawApp(source='awesome',
    external_id='app-0', name='App 0', website_url='https://example.invalid/app-0',
    latest_release_tag='v1', raw_payload={'version': 1})])
  args = ['--today', TODAY, '--sources', 'awesome,selfhst,gh_trending,gh_popular',
          '--max-writes', '60000']
  assert sync.main(args) == 1
  result = json.loads(capsys.readouterr().out)
  assert calls == [60000] and not result['success']
  assert next(iter(db.rows['projects'].values()))['latest_release_tag'] == 'v2'
  assert next(iter(db.rows['source_entries'].values()))['raw_payload'] == {'version': 2}
  assert saved(db)['next'] == 3


def test_newer_completed_generation_cannot_be_overwritten_by_cached_input(db):
  expected = W.generation(db)
  assert W.run(db, groups(latest_release_tag='v2', raw_payload={'version': 2}),
               today=TODAY)['complete']
  before = copy.deepcopy(db.rows)
  with pytest.raises(RuntimeError, match='superseded'):
    W.run(db, groups(latest_release_tag='v1', raw_payload={'version': 1}), today=TODAY,
          expected_generation=expected)
  assert db.rows == before


def test_atomic_initial_state_drains_published_work_before_upstream_failure(db, monkeypatch, capsys):
  cli_setup(db, monkeypatch)
  real_state = W.journal_state
  calls = []
  def published(database, path=None):
    calls.append(1)
    if len(calls) == 1:
      W.run(database, groups(latest_release_tag='v2'), today=TODAY, max_writes=3,
            requested_sources=['awesome'])
    return real_state(database, path)
  monkeypatch.setattr(W, 'journal_state', published)
  def offline(names):
    assert not W.pending(db)
    raise http.client.IncompleteRead(b'private upstream data')
  monkeypatch.setattr(sync, 'collect', offline)
  assert sync.main(['--today', TODAY, '--sources', 'awesome', '--max-writes', '20']) == 1
  result = json.loads(capsys.readouterr().out)
  assert calls == [1] and result['phase'] == 'collect'
  assert result['attempted'] == 1 and result['drained_plan']['complete']
  assert result['drained_plan']['generation'] == saved(db)['generation']
  assert saved(db)['next'] == 4


def test_missing_credentials_has_safe_json_and_exit_two(monkeypatch, capsys):
  monkeypatch.delenv('SELFHOSTED_PB_EMAIL', raising=False)
  monkeypatch.delenv('SELFHOSTED_PB_PASS', raising=False)
  assert sync.main(['--sources', 'awesome']) == 2
  result = json.loads(capsys.readouterr().out)
  assert result['error_type'] == 'MissingCredentials'
  assert result['pending'] is None and not result['success']


def test_resume_second_slice_eof_retains_known_counts(db, monkeypatch, capsys):
  W.run(db, groups(), today=TODAY, max_writes=2, requested_sources=['awesome'])
  cli_setup(db, monkeypatch)
  real = db.upsert
  calls = []
  def interrupted(*args, **kwargs):
    calls.append(1)
    if len(calls) == 2:
      raise EOFError('private transport detail')
    return real(*args, **kwargs)
  monkeypatch.setattr(db, 'upsert', interrupted)
  assert sync.main(['--resume', '--today', TODAY, '--sources', 'awesome']) == 1
  output = capsys.readouterr()
  result = json.loads(output.out)
  assert result['attempted'] == 2 and result['snapshots'] == 1
  assert result['pending'] == 1 and result['errors'] == 1
  assert result['confirmed_total'] == saved(db)['next'] == 3
  assert 'private transport detail' not in output.out + output.err


def test_directory_fsync_failure_reports_visible_but_uncertain_checkpoint(db, monkeypatch, capsys):
  W.run(db, groups(), today=TODAY, max_writes=3, requested_sources=['awesome'])
  real = W.os.fsync
  failures = []
  def interrupted(fd):
    if stat.S_ISDIR(W.os.fstat(fd).st_mode) and not failures:
      failures.append(1)
      raise OSError('directory fault')
    return real(fd)
  monkeypatch.setattr(W.os, 'fsync', interrupted)
  cli_setup(db, monkeypatch)
  assert sync.main(['--resume', '--today', TODAY, '--sources', 'awesome']) == 1
  output = capsys.readouterr()
  result = json.loads(output.out)
  assert result['errors'] == 1 and not result['complete']
  assert result['checkpoint_durability'] == 'unknown'
  assert result['confirmed_total'] == saved(db)['next'] == 4
  assert result['pending'] == 0 and result['events'] == 1
  assert 'pending_writes' not in result['failure_reasons']
  assert 'retained for replay' not in output.err
  resumed = W.flush(db)
  assert resumed['complete'] and resumed['events'] == 0


class FakeClock:
  def __init__(self):
    self.now = 100.0
    self.wall = 1800000000.0
    self.waits = []

  def advance(self, seconds):
    self.now += seconds
    self.wall += seconds

  def sleep(self, seconds):
    assert 0 < seconds <= 60
    self.waits.append(seconds)
    self.advance(seconds)


@pytest.fixture
def fake_clock(monkeypatch):
  clock = FakeClock()
  monkeypatch.setattr(W.time, 'monotonic', lambda: clock.now)
  monkeypatch.setattr(W.time, 'time', lambda: clock.wall)
  monkeypatch.setattr(W.time, 'sleep', clock.sleep)
  return clock


def quota_headers(clock, remaining=0, seconds=120, scope='ip'):
  prefix = f'X-PocketHost-RateLimit-{scope}-Hourly-'
  return {prefix+'Limit': str(W.QUOTA_LIMITS[scope]), prefix+'Remaining': str(remaining),
          prefix+'Reset': str(int(clock.wall + seconds))}


class HTTPResponse(io.BytesIO):
  def __init__(self, body=None, headers=None):
    super().__init__(json.dumps(body if body is not None else {}).encode())
    self.headers = headers or {}


def transport_client(seconds=14400):
  client = object.__new__(W.PB)
  client.base, client.token = 'https://example.invalid', None
  client.budget = W.RuntimeBudget(seconds)
  return client


@pytest.mark.parametrize('scope', ['ip', 'instance'])
@pytest.mark.parametrize('status', [200, 429])
def test_health_prime_waits_before_single_login(monkeypatch, fake_clock, scope, status):
  calls = []
  def respond(req, timeout):
    calls.append((req.method, req.full_url, fake_clock.now, timeout))
    if req.method == 'GET':
      assert req.get_header('Authorization') is None
      headers = quota_headers(fake_clock, remaining=10, scope=scope)
      if status == 429:
        raise urllib.error.HTTPError(req.full_url, 429, 'private', headers, io.BytesIO(b'{}'))
      return HTTPResponse(headers=headers)
    return HTTPResponse({'token': 'synthetic'})
  monkeypatch.setattr(W.urllib.request, 'urlopen', respond)
  client = W.PB('https://test.pockethost.io', 'synthetic', 'synthetic')
  assert [(c[0], c[2]) for c in calls] == [('GET', 100), ('POST', 222)]
  assert calls[0][1].endswith('/api/health') and calls[1][1].endswith('/auth-with-password')
  assert fake_clock.waits == [60, 60, 2]
  assert client.budget.report()['http_attempts'] == 2
  assert client.budget.report()['quota_waits'] == 1
  assert client.budget.report()['quota_wait_seconds'] == 122


def test_non_pockethost_login_has_no_health_prime(monkeypatch, fake_clock):
  calls = []
  def respond(req, timeout):
    calls.append(req.method)
    return HTTPResponse({'token': 'synthetic'})
  monkeypatch.setattr(W.urllib.request, 'urlopen', respond)
  client = W.PB('https://pockethost.io.example.invalid', 'synthetic', 'synthetic')
  assert calls == ['POST'] and client.budget.http_attempts == 1


def test_multiple_windows_debit_headerless_attempts_and_wait_for_both_scopes(monkeypatch, fake_clock):
  calls = []
  client = transport_client()
  def respond(req, timeout):
    calls.append((req.method, fake_clock.now))
    if len(calls) == 1:
      return HTTPResponse(headers=quota_headers(fake_clock, remaining=11, seconds=100))
    if len(calls) == 3:
      return HTTPResponse(headers={**quota_headers(fake_clock, seconds=120),
        **quota_headers(fake_clock, seconds=180, scope='instance')})
    return HTTPResponse()
  monkeypatch.setattr(W.urllib.request, 'urlopen', respond)
  for method in ('GET', 'POST', 'GET', 'PATCH'):
    client._req(method, '/records')
  assert calls == [('GET', 100), ('POST', 100), ('GET', 202), ('PATCH', 384)]
  assert client.budget.http_attempts == 4 and client.budget.quota_waits == 2
  assert sum(fake_clock.waits) == client.budget.quota_wait_seconds == 284
  assert max(fake_clock.waits) <= 60


@pytest.mark.parametrize('remaining,reset', [('-1', 'valid'), ('1001', 'valid'),
  ('1.5', 'valid'), ('NaN', 'valid'), ('0', 'missing'), ('0', 'past'),
  ('0', 'distant'), ('0', 'NaN'), ('0', 'infinity'), ('0', '99999999999999999999'),
  ('0', '+1800000120'), ('0', '1800000120.0'), ('0', ' 1800000120'), ('０', 'valid')])
def test_malformed_quota_headers_never_cause_wait(monkeypatch, fake_clock, remaining, reset):
  client = transport_client()
  values = {'valid': str(int(fake_clock.wall + 120)), 'past': str(int(fake_clock.wall - 1)),
            'distant': str(int(fake_clock.wall + 3601))}
  headers = {'X-PocketHost-RateLimit-IP-Hourly-Limit': '1000',
             'X-PocketHost-RateLimit-IP-Hourly-Remaining': remaining,
             'X-PocketHost-RateLimit-IP-Hourly-Reset': values.get(reset, reset)}
  if reset == 'missing':
    del headers['X-PocketHost-RateLimit-IP-Hourly-Reset']
  monkeypatch.setattr(W.urllib.request, 'urlopen', lambda *args, **kwargs: HTTPResponse(headers=headers))
  client._req('GET', '/records')
  client._req('POST', '/records')
  assert fake_clock.waits == [] and client.budget.http_attempts == 2


def test_case_insensitive_scoped_headers_and_maximum_horizon(monkeypatch, fake_clock):
  client = transport_client()
  client.budget.observe({'x-pockethost-ratelimit-instance-hourly-limit': '10000',
                         'X-POCKETHOST-RATELIMIT-INSTANCE-HOURLY-REMAINING': '0',
                         'X-POCKETHOST-RATELIMIT-INSTANCE-HOURLY-RESET': str(int(fake_clock.wall + 3600))})
  monkeypatch.setattr(W.urllib.request, 'urlopen', lambda *args, **kwargs: HTTPResponse())
  client._req('GET', '/records')
  assert client.budget.http_attempts == 1
  assert len(fake_clock.waits) == 61 and sum(fake_clock.waits) == 3602


def test_quota_wait_uses_monotonic_time_across_wall_clock_jump(monkeypatch, fake_clock):
  client = transport_client()
  client.budget.observe(quota_headers(fake_clock, seconds=120))
  fake_clock.wall += 100000
  monkeypatch.setattr(W.urllib.request, 'urlopen', lambda *args, **kwargs: HTTPResponse())
  client._req('GET', '/records')
  assert fake_clock.now == 222 and sum(fake_clock.waits) == 122


def test_insufficient_deadline_after_health_never_sends_login(monkeypatch, fake_clock):
  budget = W.RuntimeBudget(120)
  calls = []
  def respond(req, timeout):
    calls.append(req.method)
    return HTTPResponse(headers=quota_headers(fake_clock, seconds=120))
  monkeypatch.setattr(W.urllib.request, 'urlopen', respond)
  with pytest.raises(W.RuntimeDeadlineExceeded):
    W.PB('https://test.pockethost.io', 'synthetic', 'synthetic', budget=budget)
  assert calls == ['GET'] and fake_clock.waits == []
  assert budget.http_attempts == 1 and budget.report()['runtime_exhausted']


def test_valid_get_429_retries_across_only_two_windows(monkeypatch, fake_clock):
  client = transport_client()
  calls = []
  def respond(req, timeout):
    calls.append(fake_clock.now)
    raise urllib.error.HTTPError(req.full_url, 429, 'private',
      quota_headers(fake_clock, seconds=120), io.BytesIO(b'{}'))
  monkeypatch.setattr(W.urllib.request, 'urlopen', respond)
  with pytest.raises(W.PBError):
    client._req('GET', '/records')
  assert calls == [100, 222, 344] and client.budget.http_attempts == 3
  assert client.budget.quota_waits == 2 and sum(fake_clock.waits) == 244


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
@pytest.mark.parametrize('quota', [False, True])
def test_mutation_429_never_waits_or_retries(monkeypatch, fake_clock, method, quota):
  client = transport_client()
  calls = []
  def respond(req, timeout):
    calls.append(req.method)
    raise urllib.error.HTTPError(req.full_url, 429, 'private',
      quota_headers(fake_clock) if quota else {'Retry-After': '120'}, io.BytesIO(b'{}'))
  monkeypatch.setattr(W.urllib.request, 'urlopen', respond)
  with pytest.raises(W.PBError) as error:
    client._req(method, '/records', {'private': 'payload'})
  assert error.value.status == 429 and calls == [method]
  assert client.budget.http_attempts == 1 and fake_clock.waits == []


@pytest.mark.parametrize('batch_size', [1, 50])
@pytest.mark.parametrize('failure', ['429', 'timeout'])
def test_transport_failures_preserve_journal_without_extra_write(db, monkeypatch, fake_clock, batch_size, failure):
  journal = seed_plan(db, count=3)
  original = Path(db.stash_path).read_bytes()
  db.token, db.budget = None, W.RuntimeBudget(7)
  real = db._req
  calls, timeouts = [], []
  def respond(req, timeout):
    calls.append(req.method)
    timeouts.append(timeout)
    if req.method == 'GET':
      return HTTPResponse({'items': []})
    if failure == 'timeout':
      fake_clock.advance(timeout)
      raise TimeoutError('private timeout')
    raise urllib.error.HTTPError(req.full_url, 429, 'private response',
      quota_headers(fake_clock), io.BytesIO(b'{}'))
  monkeypatch.setattr(W.urllib.request, 'urlopen', respond)
  monkeypatch.setattr(db, '_req', W.PB._req.__get__(db))
  stats = W.flush(db, batch_size=batch_size)
  assert stats['errors'] == 1 and stats['confirmed_total'] == 0 and stats['pending'] == 3
  assert calls == (['PATCH'] if batch_size == 1 else ['GET', 'POST'])
  assert timeouts == [7] * len(calls) and fake_clock.waits == []
  assert stats['http_attempts'] == len(calls)
  assert Path(db.stash_path).read_bytes() == original and saved(db)['generation'] == journal['generation']
  assert not Path(str(db.stash_path) + '.progress').exists()
  # A later, explicit invocation reuses the same IDs and retains normal idempotence.
  monkeypatch.setattr(db, '_req', real)
  db.budget = W.RuntimeBudget()
  assert W.flush(db)['complete']
  assert W.flush(db)['attempted'] == 0 and len(db.rows['projects']) == 1


def test_deadline_between_operations_retains_confirmed_prefix(db, monkeypatch, fake_clock):
  seed_plan(db, count=3)
  db.calls.clear()
  db.budget = W.RuntimeBudget(5)
  real = db.upsert
  def slow(*args, **kwargs):
    result = real(*args, **kwargs)
    fake_clock.advance(5)
    return result
  monkeypatch.setattr(db, 'upsert', slow)
  result = W.flush(db)
  assert result['projects_created'] == result['confirmed_total'] == 1
  assert result['pending'] == 2 and result['errors'] == 1 and saved(db)['next'] == 1
  assert result['failure']['error_type'] == 'RuntimeDeadlineExceeded'
  assert result['runtime_exhausted'] and len(db.calls) == 2


@pytest.mark.parametrize('phase', ['collect', 'merge', 'plan_and_write'])
def test_cli_deadline_checks_phases_and_retains_completed_drain(db, monkeypatch, fake_clock, capsys, phase):
  W.run(db, groups(), today=TODAY, max_writes=1, requested_sources=['awesome'])
  cli_setup(db, monkeypatch)
  original = Path(db.stash_path).read_bytes()
  if phase == 'collect':
    real = W.flush
    def slow(*args, **kwargs):
      result = real(*args, **kwargs)
      fake_clock.advance(5)
      return result
    monkeypatch.setattr(W, 'flush', slow)
    monkeypatch.setattr(sync, 'collect', lambda names: pytest.fail('collect after deadline'))
  elif phase == 'merge':
    real = sync.collect
    def slow(names):
      rows = real(names)
      fake_clock.advance(5)
      return rows
    monkeypatch.setattr(sync, 'collect', slow)
    monkeypatch.setattr(sync, 'merge', lambda rows: pytest.fail('merge after deadline'))
  else:
    real = sync.merge
    def slow(rows):
      rows = real(rows)
      fake_clock.advance(5)
      return rows
    monkeypatch.setattr(sync, 'merge', slow)
    monkeypatch.setattr(W, 'run', lambda *args, **kwargs: pytest.fail('plan after deadline'))
  assert sync.main(['--today', TODAY, '--sources', 'awesome', '--max-runtime-seconds', '5']) == 1
  output = capsys.readouterr()
  result = json.loads(output.out)
  assert result['phase'] == phase and result['error_type'] == 'RuntimeDeadlineExceeded'
  assert result['runtime_exhausted'] and result['attempted'] == 3
  assert result['confirmed_total'] == result['drained_plan']['confirmed_total'] == 4
  assert [result[k] for k in W.COUNTERS] == [0, 0, 1, 1, 1]
  assert result['pending'] is None and Path(db.stash_path).read_bytes() == original


@pytest.mark.parametrize('seconds', ['0', '-1', 'NaN', 'inf'])
def test_cli_rejects_invalid_runtime_before_requests(db, monkeypatch, seconds):
  cli_setup(db, monkeypatch)
  with pytest.raises(SystemExit) as error:
    sync.main(['--max-runtime-seconds', seconds])
  assert error.value.code == 2 and db.calls == []


def test_real_transport_drain_spans_windows_counts_404s_and_remains_idempotent(db, monkeypatch, fake_clock):
  real = db._req
  calls = []
  db.token, db.budget = None, W.RuntimeBudget()
  def respond(req, timeout):
    calls.append(req.method)
    url = W.urllib.parse.urlsplit(req.full_url)
    params = {k: v[0] for k, v in W.urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
    for key in ('page', 'perPage'):
      if key in params:
        params[key] = int(params[key])
    body = json.loads(req.data) if req.data else None
    headers = quota_headers(fake_clock, remaining=11)
    try:
      result = real(req.method, url.path, body, params)
    except W.PBError as error:
      raise urllib.error.HTTPError(req.full_url, error.status, 'synthetic', headers, io.BytesIO(b'{}'))
    return HTTPResponse(result, headers)
  monkeypatch.setattr(W.urllib.request, 'urlopen', respond)
  monkeypatch.setattr(db, '_req', W.PB._req.__get__(db))
  result = W.run(db, groups(), today=TODAY)
  assert result['complete'] and result['confirmed_total'] == 4
  assert result['http_attempts'] == len(calls) == 12
  assert result['quota_waits'] == 5 and sum(fake_clock.waits) == 610
  assert calls.count('GET') == 6 and calls.count('PATCH') == 2 and calls.count('POST') == 4
  writes = [m for m in calls if m != 'GET']
  assert W.run(db, groups(), today=TODAY)['complete']
  assert [m for m in calls if m != 'GET'] == writes
  assert all(len(rows) == 1 for rows in db.rows.values())


def test_planning_deadline_never_replaces_original_journal(db, monkeypatch, fake_clock):
  assert W.run(db, groups(), today=TODAY)['complete']
  original = Path(db.stash_path).read_bytes()
  db.budget = W.RuntimeBudget(5)
  real = W._plan
  def slow(*args, **kwargs):
    result = real(*args, **kwargs)
    fake_clock.advance(5)
    return result
  monkeypatch.setattr(W, '_plan', slow)
  with pytest.raises(W.RuntimeDeadlineExceeded):
    W.run(db, groups(2), today=TODAY)
  assert Path(db.stash_path).read_bytes() == original and saved(db)['next'] == 4
