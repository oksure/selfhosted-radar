"""Offline replay against migration-derived SQLite fixtures; no live DBs."""
import copy
import json
import re
import socket
import sqlite3
from pathlib import Path

import pytest

from collector import pbwrite as W
from collector import sqlstage as S
from collector.merge import merge
from collector.sources import RawApp


TODAY = '2026-11-15'
BASE = 'https://radar.example.invalid'
PID = 'legacyproject01'
EID = 'legacyentry0001'
SID = 'legacysnap00001'
VID = 'legacyevent0001'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
  def blocked(*args, **kwargs):
    raise AssertionError('offline replay attempted network access')
  monkeypatch.setattr(socket, 'create_connection', blocked)
  monkeypatch.setattr(socket.socket, 'connect', blocked)


def create_schema(con):
  # The actual four collection layouts, real unique indexes and no SQL FKs.
  root = Path(__file__).resolve().parents[1] / 'pb_migrations'
  found = set()
  for path in sorted(root.glob('*.js')):
    for block in re.findall(r'new Collection\(\{(.*?)\n  \}\)', path.read_text(), re.S):
      coll = re.search(r"name: '([^']+)'", block)[1]
      if coll not in W.KEYS:
        continue
      columns = ['id TEXT PRIMARY KEY NOT NULL']
      fields = block.split('fields: [', 1)[1].split('indexes:', 1)[0]
      for field in re.findall(r'\{(.*?)\}', fields, re.S):
        name = re.search(r"name: '([^']+)'", field)[1]
        kind = re.search(r"type: '([^']+)'", field)[1]
        sql = {'number': 'NUMERIC NOT NULL DEFAULT 0',
               'bool': 'BOOLEAN NOT NULL DEFAULT FALSE', 'json': 'JSON DEFAULT NULL'}.get(
                 kind, "TEXT NOT NULL DEFAULT ''")
        columns.append(f'"{name}" {sql}')
      con.execute(f'CREATE TABLE "{coll}" ({", ".join(columns)})')
      for index in re.findall(r"'(CREATE (?:UNIQUE )?INDEX [^']+)'", block):
        con.execute(index)
      found.add(coll)
  assert found == set(W.KEYS)
  con.commit()


@pytest.fixture
def db(tmp_path):
  path = tmp_path / 'offline.db'
  with sqlite3.connect(path) as con:
    create_schema(con)
  return path


def project(**changes):
  return dict(dict(id=PID, canonical_key='github:owner/app', slug='app', name='App',
                   stars=100, icon_url='https://example.invalid/original.png',
                   platforms=['Linux'], sources=['awesome'], unified_tags=['private'],
                   archived=False, first_seen_at='2025-01-01', last_synced_at=TODAY), **changes)


def entry(**changes):
  return dict(dict(id=EID, project=PID, source='awesome', external_id='app',
                   raw_tags=['storage'], alternatives_refs=[], raw_payload={'original': '☃'},
                   last_seen_at=TODAY), **changes)


def snapshot(**changes):
  return dict(dict(id=SID, project=PID, captured_date=TODAY,
                   stars=100, forks=2, selfhst_trending=3, release_tag='v1'), **changes)


def event(**changes):
  return dict(dict(id=VID, project=PID, event_type='new_release', occurred_at=TODAY,
                   source='awesome', title='Original', detail={'original': True}, is_read=True), **changes)


def op(coll, body, counter=None):
  return {'collection': coll, 'body': body, 'counter': counter or {
    'projects': 'projects_created', 'source_entries': 'entries',
    'repo_snapshots': 'snapshots', 'surveillance_events': 'events'}[coll]}


def journal(*operations):
  return dict(version=1, generation='fixture-generation', target=BASE, date=TODAY,
              next=0, operations=list(operations))


def inventory(db):
  inv = S.SQLiteInventory(db, BASE + '/')
  return {c: inv.all(c) for c in W.KEYS}


def seed(db):
  intent = journal(op('projects', project()), op('source_entries', entry()),
                   op('repo_snapshots', snapshot()), op('surveillance_events', event()))
  S.apply_journal(db, intent, 0)
  return intent


def test_four_collection_schema_and_exact_legacy_ids(db):
  intent = seed(db)
  rows = inventory(db)
  assert [rows[c][0]['id'] for c in W.KEYS] == [PID, EID, SID, VID]
  assert rows['projects'][0]['archived'] is False
  assert rows['surveillance_events'][0]['is_read'] is True
  assert rows['source_entries'][0]['raw_payload'] == {'original': '☃'}
  assert rows['projects'][0]['platforms'] == ['Linux']
  before = copy.deepcopy(rows)
  receipt = S.apply_journal(db, intent, 0)
  assert receipt == dict(generation='fixture-generation', start=0, end=4,
                        counts=dict(projects_created=1, projects_updated=0, entries=1,
                                    snapshots=1, events=1), icon_fallbacks=0)
  assert inventory(db) == before
  with sqlite3.connect(db) as con:
    assert con.execute('PRAGMA foreign_key_list(source_entries)').fetchall() == []
    assert con.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
    assert 'created' not in {r[1] for r in con.execute('PRAGMA table_info(projects)')}


def test_partial_mutable_updates_and_immutable_preservation(db):
  seed(db)
  before = inventory(db)
  intent = journal(op('projects', dict(id=PID, canonical_key='github:owner/app', stars=120), 'projects_updated'),
                   op('source_entries', dict(id=EID, source='awesome', external_id='app', last_seen_at='2026-11-16')),
                   op('repo_snapshots', snapshot(stars=999, release_tag='changed')),
                   op('surveillance_events', event(detail={'changed': True}, is_read=False, title='Changed')))
  original = copy.deepcopy(intent)
  S.apply_journal(db, intent, 0)
  expected = copy.deepcopy(before)
  expected['projects'][0]['stars'] = 120
  expected['source_entries'][0]['last_seen_at'] = '2026-11-16'
  assert inventory(db) == expected
  assert intent == original
  S.apply_journal(db, intent, 0)
  assert inventory(db) == expected


@pytest.mark.parametrize('coll,body', [
  ('projects', project(id='anotherproject1')),
  ('projects', project(canonical_key='github:owner/conflict')),
  ('projects', project(id='anotherproject1', canonical_key='different')),
  ('source_entries', entry(id='anotherentry001')),
  ('source_entries', entry(external_id='different')),
  ('repo_snapshots', snapshot(id='anothersnap0001')),
  ('repo_snapshots', snapshot(captured_date='2026-11-16')),
  ('surveillance_events', event(id='anotherevent001')),
  ('surveillance_events', event(event_type='star_surge')),
])
def test_identity_conflicts_roll_back_entire_transaction(db, coll, body):
  seed(db)
  before = inventory(db)
  intent = journal(op('projects', project(stars=123), 'projects_updated'), op(coll, body))
  with pytest.raises((ValueError, sqlite3.IntegrityError)):
    S.apply_journal(db, intent, 0)
  assert inventory(db) == before


@pytest.mark.parametrize('changes', [
  {'unknown_column': 'oops'}, {'name': None}, {'stars': True}, {'stars': '12'},
  {'stars': float('nan')}, {'stars': float('inf')}, {'stars': 2 ** 64},
  {'archived': 1}, {'repo_host': 'unknown'}, {'sources': {'x': float('nan')}},
  {'sources': {1: 'not a JSON key'}}, {'platforms': {'a'}},
  {'sources': ['x' * 2001]}, {'last_synced_at': '2026-02-30'},
  {'latest_release_at': '2026-11-15T99:00:00Z'}, {'last_commit_at': '20261115'},
  {'latest_release_at': '2026-11-15T00:00:00+00:99'},
  {'homepage_url': 'javascript:alert(1)'}, {'name': 'x' * 201}, {'id': '../unsafe'},
])
def test_reject_invalid_columns_values_and_types(db, changes):
  with pytest.raises(ValueError):
    S.apply_journal(db, journal(op('projects', project(**changes))), 0)
  assert all(not rows for rows in inventory(db).values())


@pytest.mark.parametrize('coll,body', [
  ('source_entries', entry()), ('repo_snapshots', snapshot()),
  ('surveillance_events', event()),
])
def test_missing_parent_is_rejected_without_sql_foreign_keys(db, coll, body):
  with pytest.raises(ValueError, match='missing parent'):
    S.apply_journal(db, journal(op(coll, body)), 0)
  assert all(not rows for rows in inventory(db).values())


def test_preexisting_orphans_and_invalid_json_rejected(db):
  with sqlite3.connect(db) as con:
    con.execute('INSERT INTO source_entries (id, project, source, external_id) VALUES (?, ?, ?, ?)',
                (EID, PID, 'awesome', 'app'))
  with pytest.raises(ValueError, match='missing parent'):
    S.apply_journal(db, journal(op('projects', project())), 0)
  with sqlite3.connect(db) as con:
    assert con.execute('SELECT count(*) FROM projects').fetchone()[0] == 0
    con.execute('DELETE FROM source_entries')
  seed(db)
  with sqlite3.connect(db) as con:
    con.execute("UPDATE source_entries SET raw_payload = '{broken'")
  with pytest.raises(ValueError):
    S.SQLiteInventory(db, BASE)


def test_wal_inventory_backup_sees_committed_pages_and_is_detached(db):
  seed(db)
  con = sqlite3.connect(db)
  try:
    assert con.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    con.execute('PRAGMA wal_autocheckpoint=0')
    con.execute('UPDATE projects SET name = ?', ('In WAL',))
    con.commit()
    assert Path(str(db) + '-wal').stat().st_size > 0
    inv = S.SQLiteInventory(db, BASE)
    assert inv.base == BASE
    assert inv.all('projects')[0]['id'] == PID
    con.execute('UPDATE projects SET name = ?', ('Later',))
    con.commit()
    assert inv.all('projects')[0]['name'] == 'In WAL'
    rows = inv.all('projects')
    rows[0]['sources'].append('changed')
    assert inv.all('projects')[0]['sources'] == ['awesome']
  finally:
    con.close()


def test_existing_legacy_ids_reused_by_planner_and_event_order(db):
  seed(db)
  data = merge([RawApp(source='awesome', external_id='app', name='App',
                       repo_url='https://github.com/owner/app', latest_release_tag='v2',
                       stars=110, raw_payload={'new': True})])
  intent = W._plan(S.SQLiteInventory(db, BASE), data, '2026-11-16')
  assert all(o['body']['id'] == PID for o in intent['operations'] if o['collection'] == 'projects')
  assert all(o['body']['project'] == PID for o in intent['operations'] if o['collection'] != 'projects')
  assert next(o['body']['id'] for o in intent['operations'] if o['collection'] == 'source_entries') == EID
  release = next(o for o in intent['operations'] if o['collection'] == 'surveillance_events')
  assert release['body']['detail']['current'] == 'v2'
  assert intent['operations'][-1]['collection'] == 'projects'
  S.apply_journal(db, intent, 0)
  rows = inventory(db)
  assert rows['projects'][0]['latest_release_tag'] == 'v2'
  assert len(rows['repo_snapshots']) == len(rows['surveillance_events']) == 2
  assert W._plan(S.SQLiteInventory(db, BASE), data, '2026-11-16')['operations'] == []


def test_suffix_receipt_does_not_change_production_progress_or_journal(db, tmp_path):
  intent = seed(db)
  intent['next'] = 1
  path = tmp_path / 'journal.json'
  path.write_text(json.dumps(intent))
  progress = tmp_path / 'journal.json.progress'
  progress.write_text('{"generation":"fixture-generation","next":1}')
  before = {p: p.read_bytes() for p in (path, progress)}
  original = copy.deepcopy(intent)
  receipt = S.apply_journal(db, intent, 1)
  assert (receipt['start'], receipt['end']) == (1, 4)
  assert sum(receipt['counts'].values()) == 3
  assert intent == original
  assert all(p.read_bytes() == value for p, value in before.items())
  assert S.apply_journal(db, intent, 4)['counts'] == dict.fromkeys(W.COUNTERS, 0)


@pytest.mark.parametrize('coll', ['curation', '_superusers', 'projects; DROP TABLE projects;--'])
def test_collection_allowlist(db, coll):
  with pytest.raises(ValueError):
    S.SQLiteInventory(db, BASE).all(coll)
  with pytest.raises(ValueError):
    S.apply_journal(db, journal(dict(collection=coll, body=project(), counter='projects_created')), 0)


@pytest.mark.parametrize('start', [-1, 1, True, 0.0, None])
def test_invalid_start(db, start):
  with pytest.raises(ValueError):
    S.apply_journal(db, journal(), start)


@pytest.mark.parametrize('icon', ['not a URL', 'javascript:bad', 'https://', 'https://exa mple.invalid/a'])
def test_only_optional_bad_icon_falls_back_without_mutating_journal(db, icon):
  seed(db)
  intent = journal(op('projects', project(icon_url=icon, stars=200), 'projects_updated'))
  original = copy.deepcopy(intent)
  result = S.apply_journal(db, intent, 0)
  row = inventory(db)['projects'][0]
  assert result['icon_fallbacks'] == 1
  assert row['stars'] == 200
  assert row['icon_url'] == 'https://example.invalid/original.png'
  assert intent == original


def test_bad_icon_on_insert_preserves_default_and_strict_other_urls(db):
  receipt = S.apply_journal(db, journal(op('projects', project(icon_url='bad'))), 0)
  assert receipt['icon_fallbacks'] == 1
  assert inventory(db)['projects'][0]['icon_url'] == ''
  with pytest.raises(ValueError):
    S.apply_journal(db, journal(op('projects', project(icon_url='bad', repo_url='bad'))), 0)


def test_trigger_changes_are_verified_and_rolled_back(db):
  with sqlite3.connect(db) as con:
    con.execute("CREATE TRIGGER change_name AFTER INSERT ON projects BEGIN "
                "UPDATE projects SET name = 'changed' WHERE id = NEW.id; END")
  with pytest.raises(ValueError, match='supplied fields'):
    S.apply_journal(db, journal(op('projects', project())), 0)
  assert inventory(db)['projects'] == []


def test_foreign_keys_enabled_and_post_write_orphan_check_rolls_back(db):
  with sqlite3.connect(db) as con:
    con.execute("CREATE TRIGGER require_fk BEFORE INSERT ON projects "
                "WHEN (SELECT foreign_keys FROM pragma_foreign_keys) != 1 "
                "BEGIN SELECT RAISE(ABORT, 'foreign keys disabled'); END")
  S.apply_journal(db, journal(op('projects', project())), 0)
  before = inventory(db)
  with sqlite3.connect(db) as con:
    con.execute("CREATE TRIGGER add_orphan AFTER UPDATE ON projects BEGIN "
                "INSERT INTO source_entries (id, project, source, external_id) "
                "VALUES ('orphanentry0001', 'missingparent01', 'awesome', 'orphan'); END")
  with pytest.raises(ValueError, match='missing parent'):
    S.apply_journal(db, journal(op('projects', project(stars=200))), 0)
  assert inventory(db) == before


@pytest.mark.parametrize('value', [None, 1, 2.5, True, 'string', [], {}, {'a': [False, None]}])
def test_json_values_normalized_with_real_json_column_affinity(db, value):
  S.apply_journal(db, journal(op('projects', project(platforms=value))), 0)
  assert inventory(db)['projects'][0]['platforms'] == value


def test_nonexistent_database_not_created(tmp_path):
  path = tmp_path / 'missing.db'
  with pytest.raises(sqlite3.OperationalError):
    S.SQLiteInventory(path, BASE)
  with pytest.raises(sqlite3.OperationalError):
    S.apply_journal(path, journal(), 0)
  assert not path.exists()
