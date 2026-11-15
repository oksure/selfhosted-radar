"""Synthetic transport only: no PocketBase service or upstream fetches."""
import copy
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


def test_get_retries_and_retry_after_are_bounded(monkeypatch):
  calls, waits = [], []
  def fail(req, timeout):
    calls.append(req.method)
    raise urllib.error.HTTPError(req.full_url, 429, 'limit', {'Retry-After': '99999'}, io.BytesIO(b'{}'))
  monkeypatch.setattr(W.urllib.request, 'urlopen', fail)
  monkeypatch.setattr(W.time, 'sleep', waits.append)
  client = object.__new__(W.PB)
  client.base, client.token = 'https://example.invalid', None
  with pytest.raises(W.PBError):
    client._req('GET', '/records')
  assert calls == ['GET'] * 3 and waits == [30, 30]


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
  def no_collect(*args):
    raise AssertionError('resume must not collect')
  monkeypatch.setattr(sync, 'collect', no_collect)
  # Normal invocation also prioritises pending work without collecting.
  assert sync.main(['--today', TODAY]) == 0
  result = json.loads(capsys.readouterr().out)
  assert result['resumed'] and result['freshness'] == 'current'
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
  assert second['complete'] and second['resumed'] and second['date'] == TODAY
  assert second['freshness'] == 'stale' and 'stale_data' in second['failure_reasons']
  assert len(collections) == 1
  assert sync.main(['--today', '2026-11-17']) == 1
  third = json.loads(capsys.readouterr().out)
  assert not third['complete'] and third['pending'] == 10
  assert third['daily_capacity']['baseline_operations_per_day'] == 210
  assert len(collections) == 2
  assert sync.main(['--today', '2026-11-18']) == 1
  fourth = json.loads(capsys.readouterr().out)
  assert fourth['complete'] and fourth['freshness'] == 'stale'
  assert set(fourth['failure_reasons']) == {'stale_data', 'capacity_insufficient'}
  assert len(collections) == 2


def test_legacy_journal_capacity_is_unknown_not_success(db, monkeypatch, capsys):
  cli_setup(db, monkeypatch)
  W.run(db, groups(), today=TODAY, max_writes=1)
  path = Path(db.stash_path)
  journal = json.loads(path.read_text())
  del journal['daily_workload']
  path.write_text(json.dumps(journal))
  assert sync.main(['--today', TODAY]) == 1
  stats = json.loads(capsys.readouterr().out)
  assert stats['complete'] and stats['freshness'] == 'current'
  assert stats['daily_capacity']['assessment'] == 'unknown'
  assert stats['failure_reasons'] == ['capacity_unknown']


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
