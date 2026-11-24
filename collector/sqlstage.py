"""Apply a merged dataset to a local SQLite staging database.

This module contains the SQL planning and apply logic used by the offline bulk
publisher. The Backups API transport, retained snapshots and restore safeguards
live in ``collector.offline`` and ``collector.pockethost_backup``; this module
does not manage instance power or transfer ``pb_data`` files.

Standard record writes stay available for small, interactive updates; this SQL
path is only for the bulk publisher.
"""
import json
import math
import random
import re
import sqlite3
import string
from datetime import date, datetime
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from . import sources
from .merge import activity_status, delta, is_star_surge, merge


# Application fields from pb_migrations/1763100000..1763100200. Do not infer
# writable columns from SQLite: downloaded files also contain private tables.
_SOURCES = ('awesome', 'selfhst', 'gh_trending', 'gh_popular')
_KEYS = {
  'projects': ('canonical_key',),
  'source_entries': ('source', 'external_id'),
  'repo_snapshots': ('project', 'captured_date'),
  'surveillance_events': ('project', 'event_type', 'occurred_at'),
}
_TEXT = {
  'projects': dict(canonical_key=300, slug=200, name=200, repo_full_name=200,
                   homepage_url=0, repo_url=0, demo_url=0, icon_url=0,
                   description=2000, license=200, language=100, last_commit_at=40,
                   latest_release_tag=200, latest_release_at=40,
                   first_seen_at=40, last_synced_at=40),
  'source_entries': dict(project=15, external_id=200, source_url=500, last_seen_at=40),
  'repo_snapshots': dict(project=15, captured_date=10, release_tag=200),
  'surveillance_events': dict(project=15, title=300, occurred_at=10),
}
_JSON = {
  'projects': dict(platforms=20000, unified_tags=20000, sources=2000),
  'source_entries': dict(raw_tags=5000, alternatives_refs=5000, raw_payload=200000),
  'repo_snapshots': {},
  'surveillance_events': dict(detail=20000),
}
_NUMBER = {
  'projects': ('stars', 'forks', 'stars_1d_delta', 'stars_7d_delta',
               'stars_30d_delta', 'selfhst_trending'),
  'source_entries': (),
  'repo_snapshots': ('stars', 'forks', 'selfhst_trending'),
  'surveillance_events': (),
}
_BOOL = {'projects': ('archived',), 'source_entries': (), 'repo_snapshots': (),
         'surveillance_events': ('is_read',)}
_SELECT = {
  'projects': dict(repo_host=('github', 'gitlab', 'codeberg', 'other'),
                   activity_status=('active', 'slow', 'stale', 'archived')),
  'source_entries': dict(source=_SOURCES),
  'repo_snapshots': {},
  'surveillance_events': dict(source=_SOURCES, event_type=(
    'new_in_awesome', 'new_in_selfhst', 'new_in_gh_trending', 'new_in_gh_popular',
    'new_release', 'star_surge', 'archived_or_stale')),
}
_REQUIRED = {
  'projects': ('canonical_key', 'slug', 'name'),
  'source_entries': ('project', 'source', 'external_id'),
  'repo_snapshots': ('project', 'captured_date'),
  'surveillance_events': ('project', 'event_type', 'title', 'occurred_at'),
}
_DATES = {'captured_date', 'occurred_at', 'last_commit_at', 'latest_release_at',
          'first_seen_at', 'last_synced_at', 'last_seen_at'}
_COUNTERS = {'projects': ('projects_created', 'projects_updated'),
             'source_entries': ('entries',), 'repo_snapshots': ('snapshots',),
             'surveillance_events': ('events',)}
_FIELDS = {c: {'id'} | set(_TEXT[c]) | set(_JSON[c]) | set(_NUMBER[c]) |
           set(_BOOL[c]) | set(_SELECT[c]) for c in _KEYS}


def _journal_date(value, day_only=False):
  if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value[:10]):
    raise ValueError('invalid journal date')
  date.fromisoformat(value[:10])
  if len(value) == 10:
    return
  if day_only or not re.fullmatch(
      r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})?', value):
    raise ValueError('invalid journal date')
  if re.search(r'[+-]\d{2}:\d{2}$', value) and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
    raise ValueError('invalid journal timezone')
  datetime.fromisoformat(value.replace('Z', '+00:00'))


def _json_value(value):
  # json.dumps otherwise accepts non-JSON dict keys and tuples silently.
  if value is None or type(value) in (str, bool, int):
    return
  if type(value) is float and math.isfinite(value):
    return
  if type(value) is list:
    for item in value:
      _json_value(item)
    return
  if type(value) is dict and all(type(k) is str for k in value):
    for item in value.values():
      _json_value(item)
    return
  raise ValueError('invalid journal JSON value')


def _valid_url(value):
  if not value:
    return True
  if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or '\\' in value:
    return False
  try:
    parts = urlsplit(value)
    if (parts.scheme not in ('http', 'https') or not parts.hostname
        or parts.username or parts.password or parts.netloc.endswith(':')
        or (parts.port is not None and not 0 < parts.port <= 65535)):
      return False
    host = parts.hostname.encode('idna').decode('ascii').rstrip('.')
    if ':' in host or re.fullmatch(r'[\d.]+', host):
      ip_address(host)
      return True
    return len(host) <= 253 and all(re.fullmatch(
      r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', label) for label in host.split('.'))
  except ValueError:
    return False


def _record(coll, body, stored=False):
  if type(body) is not dict or not body or set(body) - _FIELDS[coll]:
    raise ValueError(f'invalid or unknown {coll} columns')
  out = dict(body)
  for key, value in body.items():
    if key in ('id', 'project'):
      if not isinstance(value, str) or not re.fullmatch('[a-z0-9]{15}', value):
        raise ValueError(f'invalid {coll}.{key}')
    elif key in _JSON[coll]:
      if stored and value is not None:
        if isinstance(value, str):
          value = json.loads(value)
        elif type(value) not in (int, float):
          raise ValueError(f'invalid stored {coll}.{key} JSON')
      _json_value(value)
      encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
      if len(encoded.encode('utf-8')) > _JSON[coll][key]:
        raise ValueError(f'{coll}.{key} JSON too large')
      out[key] = value
    elif key in _BOOL[coll]:
      if stored and type(value) is int and value in (0, 1):
        out[key] = bool(value)
      elif type(value) is not bool:
        raise ValueError(f'invalid {coll}.{key} boolean')
    elif key in _NUMBER[coll]:
      if (type(value) not in (int, float)
          or (type(value) is int and not -(2 ** 63) <= value < 2 ** 63)
          or not math.isfinite(value)):
        raise ValueError(f'invalid {coll}.{key} number')
    else:
      if not isinstance(value, str):
        raise ValueError(f'invalid {coll}.{key} text')
      limit = _TEXT[coll].get(key, 0)
      if limit and len(value) > limit:
        raise ValueError(f'{coll}.{key} text too long')
      if key in _SELECT[coll] and value and value not in _SELECT[coll][key]:
        raise ValueError(f'invalid {coll}.{key} selection')
      if key in _DATES and value:
        _journal_date(value, day_only=key in ('captured_date', 'occurred_at'))
      if coll == 'projects' and key in ('homepage_url', 'repo_url', 'demo_url') and not _valid_url(value):
        raise ValueError(f'invalid {coll}.{key} URL')
  return out


def _required(coll, body):
  if any(not body.get(k) for k in ('id',) + _REQUIRED[coll]):
    raise ValueError(f'missing required {coll} fields')


def _schema(con):
  for coll, fields in _FIELDS.items():
    columns = {r[1] for r in con.execute(f'PRAGMA table_info("{coll}")')}
    if not fields <= columns:
      raise ValueError(f'{coll} schema does not match migrations')


def _verify(con):
  if [tuple(r) for r in con.execute('PRAGMA integrity_check')] != [('ok',)]:
    raise ValueError('SQLite integrity check failed')
  if con.execute('PRAGMA foreign_key_check').fetchone() is not None:
    raise ValueError('SQLite foreign key check failed')
  for coll in _KEYS:
    keys = [('id',), _KEYS[coll]]
    if coll == 'projects':
      keys.append(('slug',))
    for fields in keys:
      columns = ', '.join(f'"{k}"' for k in fields)
      if con.execute(f'SELECT 1 FROM "{coll}" GROUP BY {columns} HAVING count(*) > 1 LIMIT 1').fetchone():
        raise ValueError(f'conflicting {coll} identities')
    if coll != 'projects' and con.execute(
        f'SELECT 1 FROM "{coll}" c LEFT JOIN projects p ON c.project = p.id '
        'WHERE p.id IS NULL LIMIT 1').fetchone():
      raise ValueError(f'{coll} has a missing parent')


def _read_rows(con, coll):
  columns = ', '.join(f'"{k}"' for k in sorted(_FIELDS[coll]))
  return [_record(coll, dict(r), stored=True)
          for r in con.execute(f'SELECT {columns} FROM "{coll}" ORDER BY id')]


def _open_sqlite(db_path, mode):
  # mode=rw/ro must never create a missing file or omit its WAL via immutable=1.
  return sqlite3.connect(Path(db_path).resolve().as_uri() + f'?mode={mode}', uri=True)


class SQLiteInventory:
  """Read-only, WAL-aware snapshot implementing pbwrite._plan's inventory API.

  Only the four public collection inventories are exposed. SQLite's backup API
  captures committed WAL pages without changing the source or its journal.
  """
  def __init__(self, db_path, base):
    if not isinstance(base, str) or not base.rstrip('/'):
      raise ValueError('inventory requires a target base')
    self.base = base.rstrip('/')
    src = _open_sqlite(db_path, 'ro')
    con = sqlite3.connect(':memory:')
    try:
      src.backup(con)
      con.row_factory = sqlite3.Row
      con.execute('PRAGMA foreign_keys = ON')
      _schema(con)
      _verify(con)
      self._rows = {c: _read_rows(con, c) for c in _KEYS}
      for coll, rows in self._rows.items():
        for row in rows:
          _required(coll, row)
    finally:
      con.close()
      src.close()

  def all(self, coll):
    if not isinstance(coll, str) or coll not in _KEYS:
      raise ValueError('invalid inventory collection')
    from copy import deepcopy
    return deepcopy(self._rows[coll])


def apply_journal(db_path, journal, start):
  """Replay a journal suffix into an offline DB and return a commit receipt.

  Never changes journal/progress files. The caller owns transfer, instance
  downtime and production progress; this function only commits the local DB.
  Receipt counts confirm suffix operations, including already-existing rows;
  they are not counts of newly inserted or changed rows.
  """
  if (type(journal) is not dict or type(journal.get('version')) is not int
      or journal['version'] != 1 or not isinstance(journal.get('generation'), str)
      or not journal['generation'] or not isinstance(journal.get('target'), str)
      or not journal['target'] or type(journal.get('operations')) is not list
      or type(start) is not int or not 0 <= start <= len(journal['operations'])
      or type(journal.get('next')) is not int
      or not 0 <= journal['next'] <= len(journal['operations'])):
    raise ValueError('invalid journal envelope or replay cursor')
  _journal_date(journal.get('date'), day_only=True)
  operations = []
  icon_fallbacks = 0
  for op in journal['operations']:
    if (type(op) is not dict or set(op) != {'collection', 'counter', 'body'}
        or not isinstance(op.get('collection'), str) or op['collection'] not in _KEYS):
      raise ValueError('invalid journal operation collection')
    coll = op['collection']
    if op['counter'] not in _COUNTERS[coll]:
      raise ValueError('invalid journal operation counter')
    body = _record(coll, op['body'])
    if any(not body.get(k) for k in ('id',) + _KEYS[coll]):
      raise ValueError('journal operation has no fixed identity')
    if coll == 'projects' and 'icon_url' in body and not _valid_url(body['icon_url']):
      del body['icon_url']
      if len(operations) >= start:
        icon_fallbacks += 1
    operations.append((coll, body, op['counter']))
  counts = dict.fromkeys((k for values in _COUNTERS.values() for k in values), 0)
  con = _open_sqlite(db_path, 'rw')
  con.row_factory = sqlite3.Row
  try:
    con.execute('PRAGMA foreign_keys = ON')
    con.execute('BEGIN IMMEDIATE')
    _schema(con)
    _verify(con)
    for coll, body, counter in operations[start:]:
      fields = _KEYS[coll]
      where = ' AND '.join(f'"{k}" = ?' for k in fields)
      old = con.execute(f'SELECT * FROM "{coll}" WHERE id = ?', (body['id'],)).fetchone()
      match = con.execute(f'SELECT id FROM "{coll}" WHERE {where}',
                          tuple(body[k] for k in fields)).fetchone()
      if ((old is not None and any(old[k] != body[k] for k in fields))
          or (match is not None and match['id'] != body['id'])):
        raise ValueError(f'conflicting {coll} ID or natural key')
      if body.get('project') and con.execute(
          'SELECT 1 FROM projects WHERE id = ?', (body['project'],)).fetchone() is None:
        raise ValueError(f'{coll} has a missing parent')
      mutable = coll not in ('repo_snapshots', 'surveillance_events')
      if old is None or mutable:
        current = {k: old[k] for k in _FIELDS[coll]} if old is not None else {}
        current.update(body)
        _required(coll, current)
        encoded = {k: json.dumps(v, ensure_ascii=False, allow_nan=False) if k in _JSON[coll]
                   else int(v) if k in _BOOL[coll] else v for k, v in body.items()}
        columns = list(encoded)
        if old is None:
          names = ', '.join(f'"{k}"' for k in columns)
          marks = ', '.join('?' for k in columns)
          con.execute(f'INSERT INTO "{coll}" ({names}) VALUES ({marks})', tuple(encoded.values()))
        else:
          columns.remove('id')
          updates = ', '.join(f'"{k}" = ?' for k in columns)
          con.execute(f'UPDATE "{coll}" SET {updates} WHERE id = ?',
                      tuple(encoded[k] for k in columns) + (body['id'],))
      columns = ', '.join(f'"{k}"' for k in sorted(_FIELDS[coll]))
      confirmed = con.execute(f'SELECT {columns} FROM "{coll}" WHERE {where}',
                              tuple(body[k] for k in fields)).fetchone()
      if confirmed is None or confirmed['id'] != body['id']:
        raise ValueError('SQLite replay did not confirm record identity')
      record = _record(coll, dict(confirmed), stored=True)
      _required(coll, record)
      if (old is None or mutable) and any(record[k] != v for k, v in body.items()):
        raise ValueError('SQLite replay did not confirm supplied fields')
      counts[counter] += 1
    _verify(con)
    con.commit()
  except Exception:
    con.rollback()
    raise
  finally:
    con.close()
  return {'generation': journal['generation'], 'start': start,
          'end': len(operations), 'counts': counts, 'icon_fallbacks': icon_fallbacks}


_ALPHABET = string.ascii_lowercase + string.digits


def _rid():
  # PocketBase ids are 'r' + 14 lowercase alphanumeric characters.
  return 'r' + ''.join(random.choices(_ALPHABET, k=14))


def _d(v):
  return v if v not in ('', None) else ''


def stage(db_path, merged, today, base_id=0, verbose=True):
  # Every date column is TEXT; keep `today` a string so set membership matches.
  today = today.isoformat() if hasattr(today, 'isoformat') else str(today)
  td = date.fromisoformat(today)
  con = sqlite3.connect(db_path)
  con.execute('PRAGMA foreign_keys = OFF')
  cur = con.cursor()

  have = {k: i for i, k in cur.execute('SELECT id, canonical_key FROM projects')}
  seen_entries = {(s, e) for s, e in cur.execute('SELECT source, external_id FROM source_entries')}
  seen_snaps = {(p, d) for p, d in cur.execute('SELECT project, captured_date FROM repo_snapshots')}
  seen_events = {(p, t, o) for p, t, o in cur.execute('SELECT project, event_type, occurred_at FROM surveillance_events')}
  prev_stars = {}
  for p, d, s in cur.execute('SELECT project, captured_date, stars FROM repo_snapshots'):
    prev_stars.setdefault(p, []).append((date.fromisoformat(d), s))

  n = {'projects_new': 0, 'projects_upd': 0, 'entries': 0, 'snapshots': 0, 'events': 0}
  seq = base_id

  for g in merged:
    p = g['project']
    p['activity_status'] = activity_status(p['last_commit_at'], p['archived'], td)
    pid = have.get(p['canonical_key'])
    fresh = pid is None
    if fresh:
      seq += 1
      pid = _rid()
      have[p['canonical_key']] = pid
      cur.execute(
        '''INSERT INTO projects (id, canonical_key, slug, name, repo_full_name, repo_host,
             homepage_url, repo_url, demo_url, icon_url, description, license, language,
             platforms, unified_tags, sources, stars, forks, stars_1d_delta, stars_7d_delta,
             stars_30d_delta, selfhst_trending, last_commit_at, latest_release_tag,
             latest_release_at, archived, activity_status, first_seen_at, last_synced_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (pid, p['canonical_key'], p['slug'], p['name'], p['repo_full_name'], p['repo_host'],
         p['homepage_url'], p['repo_url'], p['demo_url'], p['icon_url'], p['description'],
         p['license'], p['language'], json.dumps(p['platforms'], ensure_ascii=False),
         json.dumps(p['unified_tags'], ensure_ascii=False),
         json.dumps(p['sources'], ensure_ascii=False), p['stars'], p['forks'],
         0, 0, 0, p['selfhst_trending'], p['last_commit_at'], p['latest_release_tag'],
         p['latest_release_at'], 1 if p['archived'] else 0, p['activity_status'],
         p['first_seen_at'], today))
      n['projects_new'] += 1
    else:
      n['projects_upd'] += 1

    snaps = sorted(prev_stars.get(pid, []) + [(td, p['stars'])])
    d1, d7, d30 = delta(snaps, td)
    cur.execute('''UPDATE projects SET sources=?, unified_tags=?, stars=?, forks=?, selfhst_trending=?, last_commit_at=?,
        latest_release_tag=?, latest_release_at=?, archived=?, activity_status=?, last_synced_at=?,
        stars_1d_delta=?, stars_7d_delta=?, stars_30d_delta=?
        WHERE id=?''',
                (json.dumps(p['sources'], ensure_ascii=False),
                 json.dumps(p['unified_tags'], ensure_ascii=False),
                 p['stars'], p['forks'], p['selfhst_trending'], p['last_commit_at'],
                 p['latest_release_tag'], p['latest_release_at'], 1 if p['archived'] else 0,
                 p['activity_status'], today, d1, d7, d30, pid))

    if (pid, today) not in seen_snaps:
      cur.execute('''INSERT INTO repo_snapshots (id, project, captured_date, stars, forks,
                       selfhst_trending, release_tag) VALUES (?,?,?,?,?,?,?)''',
                  (_rid(), pid, today, p['stars'], p['forks'], p['selfhst_trending'],
                   p['latest_release_tag']))
      seen_snaps.add((pid, today))
      n['snapshots'] += 1

    for e in g['sources']:
      if (e.source, e.external_id) in seen_entries:
        continue
      cur.execute('''INSERT INTO source_entries (id, project, source, external_id, source_url,
                       raw_tags, alternatives_refs, raw_payload, last_seen_at)
                     VALUES (?,?,?,?,?,?,?,?,?)''',
                  (_rid(), pid, e.source, e.external_id, e.source_url,
                   json.dumps(e.raw_tags, ensure_ascii=False),
                   json.dumps([str(a) for a in e.alternatives_refs]),
                   json.dumps(e.raw_payload, ensure_ascii=False), today))
      seen_entries.add((e.source, e.external_id))
      n['entries'] += 1

    evs = []
    if fresh:
      for src in p['sources']:
        etype = f'new_in_{src}' if src in ('awesome', 'selfhst', 'gh_trending', 'gh_popular') else 'new_in_awesome'
        evs.append((etype, src, f'{p["name"]} added to {src}', {'source': src}))
    else:
      old = cur.execute('SELECT latest_release_tag, archived, sources FROM projects WHERE id=?', (pid,)).fetchone()
      if old:
        try:
          old_srcs = set(json.loads(old[2] or '[]'))
        except ValueError:
          old_srcs = set()
        for src in ('gh_trending', 'gh_popular'):
          if src in p['sources'] and src not in old_srcs:
            evs.append((f'new_in_{src}', src, f'{p["name"]} added to {src}', {'source': src}))
      if p['latest_release_tag'] and old and old[0] != p['latest_release_tag']:
        evs.append(('new_release', 'awesome', f'{p["name"]} released {p["latest_release_tag"]}',
                    {'previous': old[0], 'current': p['latest_release_tag']}))
    if is_star_surge(d7):
      evs.append(('star_surge', 'selfhst' if 'selfhst' in p['sources'] else p['sources'][0],
                  f'{p["name"]} +{d7} stars in 7 days', {'delta_7d': d7, 'stars': p['stars']}))
    for etype, src, title, detail in evs:
      if (pid, etype, today) in seen_events:
        continue
      cur.execute('''INSERT INTO surveillance_events (id, project, event_type, source, title,
                       detail, occurred_at, is_read) VALUES (?,?,?,?,?,?,?,0)''',
                  (_rid(), pid, etype, src, title, json.dumps(detail, ensure_ascii=False), today))
      seen_events.add((pid, etype, today))
      n['events'] += 1

  con.commit()
  if verbose:
    print(json.dumps(n), flush=True)
  con.close()
  return n


def main(argv=None):
  import argparse
  from dataclasses import asdict
  ap = argparse.ArgumentParser(prog='collector.sqlstage')
  ap.add_argument('db_path', nargs='?', default='')
  ap.add_argument('--today', default=date.today().isoformat())
  ap.add_argument('--dump', default='', help='also keep the merged JSON for auditing')
  ap.add_argument('--collect-only', default='', help='collect all sources to JSON without touching a DB')
  ap.add_argument('--from-collected', default='', help='load pre-collected RawApp JSON instead of fetching')
  a = ap.parse_args(argv)
  if a.from_collected:
    rows = [sources.RawApp(**r) for r in json.load(open(a.from_collected, encoding='utf-8'))]
  else:
    rows = []
    for name in sources.ADAPTERS:
      got = list(sources.get(name).collect())
      print(f'{name}: {len(got)}', flush=True)
      rows += got
  if a.collect_only:
    with open(a.collect_only, 'w', encoding='utf-8') as ff:
      json.dump([asdict(r) for r in rows], ff, ensure_ascii=False)
    print(f'collected: {len(rows)} -> {a.collect_only}', flush=True)
    return 0
  if not a.db_path:
    ap.error('db_path is required unless --collect-only is set')
  merged = merge(rows)
  if a.dump:
    with open(a.dump, 'w') as ff:
      json.dump([{'project': g['project'], 'sources': [s.source for s in g['sources']]} for g in merged], ff)
  stage(a.db_path, merged, date.fromisoformat(a.today))
  return 0


if __name__ == '__main__':
  raise SystemExit(main())


def backfill_icons(db_path, verbose=True):
  """Fill projects.icon_url from source_entries.raw_payload (offline, bulk path)."""
  from .sources import _selfhst_icon
  con = sqlite3.connect(db_path)
  cur = con.cursor()
  rows = cur.execute("SELECT project, raw_payload FROM source_entries WHERE source = 'selfhst'").fetchall()
  n = 0
  for pid, payload in rows:
    try:
      icon = _selfhst_icon(json.loads(payload).get('entry') or {})
    except (ValueError, AttributeError):
      continue
    if not icon:
      continue
    cur.execute("UPDATE projects SET icon_url = ? WHERE id = ? AND (icon_url IS NULL OR icon_url = '')", (icon, pid))
    n += cur.rowcount
  con.commit()
  if verbose:
    print(json.dumps({'icon_url_set': n}), flush=True)
  con.close()
  return n
