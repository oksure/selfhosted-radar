"""Serial PocketBase writes backed by a durable, replayable local journal."""
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path

UA = {'User-Agent': 'selfhosted-radar/1.0'}
KEYS = {
  'projects': ('canonical_key',),
  'source_entries': ('source', 'external_id'),
  'repo_snapshots': ('project', 'captured_date'),
  'surveillance_events': ('project', 'event_type', 'occurred_at'),
}
COUNTERS = ('projects_created', 'projects_updated', 'entries', 'snapshots', 'events')


class PBError(RuntimeError):
  def __init__(self, status, data=None):
    self.status = status
    self.data = data if isinstance(data, dict) else {}
    # Server messages can echo submitted data; never put them in logs.
    super().__init__(f'PocketBase HTTP {status}')


def _key(coll, row):
  return tuple(row[k] for k in KEYS[coll])


def _rid(coll, row):
  raw = json.dumps([coll, _key(coll, row)], ensure_ascii=False).encode()
  return hashlib.sha256(raw).hexdigest()[:15]


class PB:
  def __init__(self, base, email, password, quiet=False):
    self.base = base.rstrip('/')
    self.quiet = quiet
    self.token = None
    self.stash_path = None
    self.icon_fallbacks = 0
    self._login(email, password)

  def _req(self, method, path, body=None, params=None, auth=True):
    url = self.base + path
    if params:
      url += '?' + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(UA, **{'Content-Type': 'application/json'})
    if auth and self.token:
      headers['Authorization'] = self.token
    for attempt in range(3):
      req = urllib.request.Request(url, data=data, headers=headers, method=method)
      try:
        with urllib.request.urlopen(req, timeout=30) as r:
          raw = r.read()
          return json.loads(raw) if raw.strip() else None
      except urllib.error.HTTPError as e:
        # Writes may commit despite a failed response; replay by fixed id.
        if method == 'GET' and e.code in (429, 502, 503, 504) and attempt < 2:
          delay = e.headers.get('Retry-After', '') if e.headers else ''
          time.sleep(min(30, int(delay)) if delay.isdigit() else 2 ** attempt)
          continue
        try:
          detail = json.loads(e.read())
        except (ValueError, OSError):
          detail = {}
        raise PBError(e.code, detail) from None

  def _login(self, email, password):
    r = self._req('POST', '/api/collections/_superusers/auth-with-password',
                  {'identity': email, 'password': password}, auth=False)
    self.token = r['token']

  def all(self, coll, flt='', fields='', per=500):
    page, out = 1, []
    while True:
      r = self._req('GET', f'/api/collections/{coll}/records',
                    params={'page': page, 'perPage': per, 'filter': flt,
                            'fields': fields, 'skipTotal': 1, 'sort': 'id'})
      items = r['items']
      out.extend(items)
      if len(items) < per:
        return out
      page += 1

  def count(self, coll, flt=''):
    r = self._req('GET', f'/api/collections/{coll}/records',
                  params={'perPage': 1, 'filter': flt})
    return r['totalItems']

  def import_records(self, coll, rows):
    """Low-level serial helper; failures propagate."""
    for r in rows:
      self.upsert(coll, r, rid=r.get('id'))
    return len(rows)

  def _write(self, method, path, body, coll):
    try:
      return self._req(method, path, body)
    except PBError as e:
      fields = e.data.get('data', {})
      icon = fields.get('icon_url', {}) if isinstance(fields, dict) else {}
      if not (coll == 'projects' and e.status == 400 and isinstance(fields, dict)
              and set(fields) == {'icon_url'} and isinstance(icon, dict)
              and icon.get('code') in ('validation_invalid_url', 'validation_is_url')
              and body.get('icon_url')):
        raise
      # PATCH preserves the previous icon; POST uses the default. Original input
      # stays in the journal and source raw_payload is never sanitised.
      clean = {k: v for k, v in body.items() if k != 'icon_url'}
      result = self._req(method, path, clean)
      self.icon_fallbacks += 1
      print('  projects: omitted rejected icon_url (input retained in journal)', file=sys.stderr)
      return result

  def upsert(self, coll, body, rid=None):
    rid = rid or body.get('id') or _rid(coll, body)
    payload = dict(body, id=rid)
    path = f'/api/collections/{coll}/records'

    def confirmed(row):
      if not isinstance(row, dict) or row.get('id') != rid or _key(coll, row) != _key(coll, body):
        raise ValueError('write response did not confirm record identity')
      return row

    if coll in ('repo_snapshots', 'surveillance_events'):
      try:
        old = self._req('GET', f'{path}/{rid}')
      except PBError as e:
        if e.status != 404:
          raise
      else:
        return confirmed(old)  # Preserve immutable detail and is_read.
    else:
      try:
        return confirmed(self._write('PATCH', f'{path}/{rid}', payload, coll))
      except PBError as e:
        if e.status != 404:
          raise
    return confirmed(self._write('POST', path, payload, coll))


def state_path(db, path=None):
  return Path(path or getattr(db, 'stash_path', None) or
              Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) /
              'selfhosted-radar/writes.json').expanduser()


@contextmanager
def _lock(path):
  path.parent.mkdir(parents=True, exist_ok=True)
  fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
  with os.fdopen(fd, 'w') as ff:
    try:
      fcntl.flock(ff, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      raise RuntimeError('write journal is locked by another collector') from None
    yield


def _save(path, payload):
  fd, tmp = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
  try:
    with os.fdopen(fd, 'w', encoding='utf-8') as ff:
      json.dump(payload, ff, ensure_ascii=False)
      ff.flush()
      os.fsync(ff.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
      os.fsync(fd)
    finally:
      os.close(fd)
  finally:
    if os.path.exists(tmp):
      os.unlink(tmp)


def _load(path, db):
  try:
    with path.open(encoding='utf-8') as ff:
      journal = json.load(ff)
  except FileNotFoundError:
    return None
  if (not isinstance(journal, dict)
      or journal.get('version') != 1 or journal.get('target') != db.base.rstrip('/')
      or not isinstance(journal.get('operations'), list)
      or type(journal.get('next')) is not int
      or not 0 <= journal['next'] <= len(journal['operations'])):
    raise ValueError('invalid journal or target mismatch; preserve it for inspection')
  date.fromisoformat(journal['date'])
  if not isinstance(journal.get('generation'), str) or not journal['generation']:
    raise ValueError('journal has no generation')
  try:
    with Path(str(path) + '.progress').open(encoding='utf-8') as ff:
      progress = json.load(ff)
  except FileNotFoundError:
    progress = {}
  if not isinstance(progress, dict):
    raise ValueError('invalid journal progress')
  if progress.get('generation') == journal['generation']:
    cursor = progress.get('next')
    if type(cursor) is not int or not 0 <= cursor <= len(journal['operations']):
      raise ValueError('invalid journal progress cursor')
    journal['next'] = cursor
  for op in journal['operations']:
    if op['collection'] not in KEYS or op['counter'] not in COUNTERS:
      raise ValueError('invalid journal operation')
    _key(op['collection'], op['body'])
    if not op['body'].get('id'):
      raise ValueError('journal operation has no fixed record id')
  return journal


def pending(db, path=None):
  """Validate saved state before deciding whether upstream collection is needed."""
  journal = _load(state_path(db, path), db)
  return bool(journal and journal['next'] < len(journal['operations']))


def daily_workload(merged):
  """Estimate recurring operations if this set of projects and sources persists."""
  projects = {g['project']['canonical_key'] for g in merged}
  entries = {(e.source, e.external_id) for g in merged for e in g['sources']}
  # Each new day changes last_synced_at and last_seen_at and needs a snapshot.
  return {'projects': len(projects), 'source_entries': len(entries),
          'baseline_operations': 2 * len(projects) + len(entries)}


def _plan(db, merged, today):
  from .canonical import slug
  from .merge import activity_status, delta, is_star_surge

  have = {c: {_key(c, r): r for r in db.all(c)} for c in KEYS}
  taken = {r['slug'] for r in have['projects'].values()}
  history = {}
  for r in have['repo_snapshots'].values():
    if r['captured_date'] != today:
      history.setdefault(r['project'], []).append((date.fromisoformat(r['captured_date']), r['stars']))
  operations = []

  def add(coll, body, counter, immutable=False):
    key = _key(coll, body)
    old = have[coll].get(key)
    if immutable and old:
      return
    body = dict(body, id=old['id'] if old else _rid(coll, body))
    if old and all(old.get(k) == v for k, v in body.items()):
      return
    operations.append({'collection': coll, 'body': body, 'counter': counter})
    have[coll][key] = body

  td = date.fromisoformat(today)
  for g in merged:
    p = dict(g['project'])
    old = have['projects'].get(_key('projects', p))
    pid = old['id'] if old else _rid('projects', p)
    p['slug'] = old['slug'] if old else slug(p['canonical_key'], taken)
    taken.add(p['slug'])
    p['first_seen_at'] = (old or {}).get('first_seen_at') or today
    p['last_synced_at'] = today
    p['activity_status'] = activity_status(p['last_commit_at'], p['archived'], td)
    # A restricted-source run cannot establish that an earlier source vanished.
    p['sources'] = sorted(set(p['sources']) | set((old or {}).get('sources') or []))
    if old:
      for k in ('latest_release_tag', 'latest_release_at', 'icon_url'):
        p[k] = p.get(k) or old.get(k) or ''
    d1, d7, d30 = delta(sorted(history.get(pid, []) + [(td, p['stars'])]), td)
    p.update(stars_1d_delta=d1, stars_7d_delta=d7, stars_30d_delta=d30)
    if not old:
      add('projects', p, 'projects_created')
    for e in g['sources']:
      add('source_entries', {
        'project': pid, 'source': e.source, 'external_id': e.external_id,
        'source_url': e.source_url, 'raw_tags': e.raw_tags,
        'alternatives_refs': [str(a) for a in e.alternatives_refs],
        'raw_payload': e.raw_payload, 'last_seen_at': today,
      }, 'entries')
    add('repo_snapshots', {
      'project': pid, 'captured_date': today, 'stars': p['stars'], 'forks': p['forks'],
      'selfhst_trending': p['selfhst_trending'], 'release_tag': p['latest_release_tag'],
    }, 'snapshots', immutable=True)
    events = []
    for src in sorted(set(p['sources']) - set((old or {}).get('sources') or [])):
      events.append(_event(pid, f'new_in_{src}', src, f'{p["name"]} added to {src}', {'source': src}, today))
    if old:
      tag = p['latest_release_tag']
      if tag and tag != old.get('latest_release_tag'):
        events.append(_event(pid, 'new_release', 'awesome', f'{p["name"]} released {tag}',
                             {'previous': old.get('latest_release_tag') or '', 'current': tag}, today))
      if p['archived'] and not old.get('archived'):
        events.append(_event(pid, 'archived_or_stale', 'awesome', f'{p["name"]} archived', {}, today))
    if is_star_surge(d7):
      src = 'selfhst' if 'selfhst' in p['sources'] else next(iter(p['sources']), '')
      events.append(_event(pid, 'star_surge', src, f'{p["name"]} +{d7} stars in 7 days',
                           {'delta_7d': d7, 'stars': p['stars']}, today))
    for r in events:
      add('surveillance_events', r, 'events', immutable=True)
    if old:
      add('projects', p, 'projects_updated')
  return {'version': 1, 'generation': uuid.uuid4().hex,
          'target': db.base.rstrip('/'), 'date': today,
          'daily_workload': daily_workload(merged), 'operations': operations, 'next': 0}


def _event(pid, etype, src, title, detail, today):
  return {'project': pid, 'event_type': etype, 'source': src or '', 'title': title,
          'detail': detail, 'occurred_at': today, 'is_read': False}


def _drain(db, journal, path, max_writes):
  stats = dict.fromkeys(COUNTERS, 0)
  stats.update(errors=0, attempted=0, icon_fallbacks=0, date=journal['date'])
  before = getattr(db, 'icon_fallbacks', 0)
  start = journal['next']
  for op in journal['operations'][start:start + max_writes]:
    stats['attempted'] += 1
    try:
      db.upsert(op['collection'], op['body'], rid=op['body']['id'])
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as e:
      stats['errors'] += 1
      stats['failure'] = {'collection': op['collection'], 'id': op['body']['id'],
                          'error_type': type(e).__name__,
                          'http_status': e.status if isinstance(e, PBError) else None}
      print(f'  {op["collection"]}/{op["body"]["id"]}: {type(e).__name__}; '
            'write unconfirmed, retained for replay', file=sys.stderr)
      break
    journal['next'] += 1
    # Failed checkpoints leave the durable position behind; replay uses the
    # same id and never advances past unconfirmed work.
    try:
      _save(Path(str(path) + '.progress'),
            {'generation': journal['generation'], 'next': journal['next']})
    except OSError:
      journal['next'] -= 1
      stats['errors'] += 1
      stats['failure'] = {'collection': op['collection'], 'id': op['body']['id'],
                          'error_type': 'CheckpointError', 'http_status': None}
      print('  journal checkpoint failed; write retained for replay', file=sys.stderr)
      break
    stats[op['counter']] += 1
  stats['icon_fallbacks'] = getattr(db, 'icon_fallbacks', 0) - before
  stats['planned'] = len(journal['operations'])
  stats['daily_workload'] = journal.get('daily_workload')
  stats['confirmed_total'] = journal['next']
  stats['pending'] = stats['planned'] - stats['confirmed_total']
  for coll in KEYS:
    stats['pending_' + coll] = sum(op['collection'] == coll
                                  for op in journal['operations'][journal['next']:])
  stats['complete'] = stats['pending'] == 0 and stats['errors'] == 0
  return stats


def run(db, merged, today=None, dry_run=False, workers=1, max_writes=200, journal_path=None):
  """Persist intent, then confirm at most max_writes serial record operations.

  An unfinished journal takes precedence over newly collected data.
  workers is retained for callers, but parallel writes are rejected.
  """
  if workers != 1 or max_writes < 1:
    raise ValueError('workers must be 1 and max_writes must be positive')
  today = today or date.today().isoformat()
  date.fromisoformat(today)
  if dry_run:
    return {'dry_run': True, 'projects': len(merged), 'complete': False}
  path = state_path(db, journal_path)
  with _lock(path):
    journal = _load(path, db)
    resumed = bool(journal and journal['next'] < len(journal['operations']))
    if not resumed:
      journal = _plan(db, merged, today)
      _save(path, journal)
    stats = _drain(db, journal, path, max_writes)
    stats['resumed'] = resumed
    return stats


def flush(db, workers=1, max_writes=200, journal_path=None):
  """Resume saved intent without fetching upstream or regenerating events."""
  if workers != 1 or max_writes < 1:
    raise ValueError('workers must be 1 and max_writes must be positive')
  path = state_path(db, journal_path)
  with _lock(path):
    journal = _load(path, db)
    if journal is None:
      raise ValueError('no write journal to resume')
    stats = _drain(db, journal, path, max_writes)
    stats['resumed'] = True
    return stats
