"""Bounded PocketBase writes backed by a durable, replayable local journal."""
import fcntl
import hashlib
import importlib
import http.client
import json
import math
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
_UNSPECIFIED = object()
QUOTA_LIMITS = {'ip': 1000, 'instance': 10000}
QUOTA_RESERVE = 10
QUOTA_MARGIN = 2
QUOTA_HORIZON = 3600


class RuntimeDeadlineExceeded(RuntimeError):
  """The invocation cannot safely start another request or quota wait."""


class RuntimeBudget:
  def __init__(self, max_runtime_seconds=14400):
    if not math.isfinite(max_runtime_seconds) or max_runtime_seconds <= 0:
      raise ValueError('max runtime must be finite and positive')
    self.started = time.monotonic()
    self.deadline = self.started + max_runtime_seconds
    self.max_runtime_seconds = max_runtime_seconds
    self.http_attempts = 0
    self.quota_waits = 0
    self.quota_wait_seconds = 0
    self.exhausted = False
    self.quotas = {}

  def check(self):
    if self.exhausted or time.monotonic() >= self.deadline:
      self.exhausted = True
      raise RuntimeDeadlineExceeded('invocation runtime budget exhausted')

  def wait_until(self, target, quota=False):
    self.check()
    now = time.monotonic()
    if target >= self.deadline:
      self.exhausted = True
      raise RuntimeDeadlineExceeded('quota or retry wait exceeds invocation deadline')
    if target <= now:
      return
    if quota:
      self.quota_waits += 1
    # Finite foreground wait, with checks between chunks; never poll the server.
    for _ in range(math.ceil((target - now) / 60) + 1):
      self.check()
      delay = min(60, target - time.monotonic())
      if delay <= 0:
        return
      before = time.monotonic()
      time.sleep(delay)
      if quota:
        self.quota_wait_seconds += max(0, time.monotonic() - before)
    self.check()
    if time.monotonic() < target:
      raise RuntimeError('quota wait did not reach reset')

  def observe(self, headers):
    """Read the actual PocketHost hourly Limit/Remaining/Reset contract."""
    headers = {k.lower(): str(v) for k, v in (headers or {}).items()}
    wall, now = time.time(), time.monotonic()
    resets = []
    for scope in QUOTA_LIMITS:
      prefix = f'x-pockethost-ratelimit-{scope}-hourly-'
      values = [headers.get(prefix + field, '') for field in ('limit', 'remaining', 'reset')]
      if not all(v and len(v) <= 12 and all('0' <= c <= '9' for c in v) for v in values):
        continue
      limit, remaining, reset = map(int, values)
      delay = reset - wall
      if not 0 < limit or not 0 <= remaining <= limit or not 0 <= delay <= QUOTA_HORIZON:
        continue
      target = now + delay + QUOTA_MARGIN
      prior = self.quotas.get(scope)
      self.quotas[scope] = ((min(prior[0], remaining), max(prior[1], target))
                            if prior and prior[1] > now else (remaining, target))
      resets.append(target)
    return resets

  def blocked_until(self):
    now = time.monotonic()
    self.quotas = {k: v for k, v in self.quotas.items() if v[1] > now}
    return max((reset for remaining, reset in self.quotas.values()
                if remaining <= QUOTA_RESERVE), default=now)

  def before_request(self):
    self.wait_until(self.blocked_until(), quota=True)
    self.check()
    timeout = min(30, self.deadline - time.monotonic())
    if timeout <= 0:
      self.check()
    self.http_attempts += 1
    self.quotas = {k: (max(0, remaining - 1), reset)
                   for k, (remaining, reset) in self.quotas.items()}
    return timeout

  def report(self):
    return {'http_attempts': self.http_attempts, 'quota_waits': self.quota_waits,
            'quota_wait_seconds': self.quota_wait_seconds,
            'runtime_seconds': max(0, time.monotonic() - self.started),
            'runtime_exhausted': self.exhausted or time.monotonic() >= self.deadline,
            'max_runtime_seconds': self.max_runtime_seconds}


def _check_deadline(db):
  budget = getattr(db, 'budget', None)
  if budget is not None:
    budget.check()


class CheckpointDurabilityError(OSError):
  """Replacement is visible, but directory durability was not confirmed."""
IMMUTABLE = ('repo_snapshots', 'surveillance_events')
BATCH_MAX_BYTES = 4 * 1024 * 1024


class BatchSizeError(ValueError):
  """A single intact record cannot fit the conservative batch byte limit."""


def _encode_body(body):
  return json.dumps(body).encode()


def _batch_request(op):
  coll = op['collection']
  return {'method': 'POST' if coll in IMMUTABLE else 'PUT',
          'url': f'/api/collections/{coll}/records', 'body': dict(op['body'])}


def _batch_slice(operations):
  # Match json.dumps' actual default ASCII escaping and comma-space separators,
  # including the requests envelope, without re-encoding each growing prefix.
  size = len(_encode_body({'requests': []}))
  selected = []
  for op in operations:
    extra = len(_encode_body(_batch_request(op))) + (2 if selected else 0)
    if size + extra > BATCH_MAX_BYTES:
      if not selected:
        raise BatchSizeError('single record exceeds 4 MiB encoded batch limit')
      break
    size += extra
    selected.append(op)
  return selected


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


def _confirmed(coll, body, row):
  if (not isinstance(row, dict) or row.get('id') != body['id']
      or _key(coll, row) != _key(coll, body)):
    raise ValueError('write response did not confirm record identity')
  return row


def _icon_only(fields):
  return (isinstance(fields, dict) and set(fields) == {'icon_url'}
          and isinstance(fields['icon_url'], dict)
          and fields['icon_url'].get('code') in ('validation_invalid_url', 'validation_is_url'))


def _source_scope(names):
  if names is None:
    return None
  if (not isinstance(names, (list, tuple)) or not names
      or any(not isinstance(n, str) or not n for n in names)):
    raise ValueError('invalid requested source scope')
  return sorted(set(names))


def credentials(base, email=None, password=None):
  """Keep explicit pairs; resolve only the canonical local selfhosted target."""
  if email is not None or password is not None:
    if not email or not password:
      raise ValueError('SELFHOSTED_PB_EMAIL / SELFHOSTED_PB_PASS must be a complete pair')
    return email, password
  if base.rstrip('/') != 'https://selfhosted.pockethost.io':
    raise ValueError('custom PocketBase URLs require explicit credentials')
  helper = Path.home() / 'dotfiles/python'
  if not (helper / 'pb_credentials.py').is_file():
    raise RuntimeError('local PocketBase credential helper unavailable')
  sys.path.insert(0, str(helper))
  try:
    selected = importlib.import_module('pb_credentials').resolve('selfhosted')
    if (selected.url.rstrip('/') != base.rstrip('/')
        or not selected.email or not selected.password):
      raise ValueError('invalid selected credentials')
    return selected.email, selected.password
  except Exception:
    raise RuntimeError('local PocketBase credentials unavailable') from None
  finally:
    sys.path.pop(0)


class PB:
  def __init__(self, base, email=None, password=None, quiet=False, budget=None):
    email, password = credentials(base, email, password)
    self.base = base.rstrip('/')
    self.quiet = quiet
    self.token = None
    self.stash_path = None
    self.icon_fallbacks = 0
    self.budget = budget if budget is not None else RuntimeBudget()
    host = urllib.parse.urlsplit(self.base).hostname or ''
    if host.endswith('.pockethost.io'):
      try:
        self._req('GET', '/api/health', auth=False, attempts=1)
      except PBError as e:
        # A single denied health prime may still supply a validated empty window.
        if e.status != 429 or self.budget.blocked_until() <= time.monotonic():
          raise
    self._login(email, password)

  def _req(self, method, path, body=None, params=None, auth=True, attempts=3):
    url = self.base + path
    if params:
      url += '?' + urllib.parse.urlencode(params)
    data = _encode_body(body) if body is not None else None
    if path == '/api/batch' and data is not None and len(data) > BATCH_MAX_BYTES:
      raise BatchSizeError('batch exceeds 4 MiB encoded body limit')
    headers = dict(UA, **{'Content-Type': 'application/json'})
    if auth and self.token:
      headers['Authorization'] = self.token
    if not hasattr(self, 'budget'):
      self.budget = RuntimeBudget()
    for attempt in range(attempts):
      req = urllib.request.Request(url, data=data, headers=headers, method=method)
      timeout = self.budget.before_request()
      try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
          self.budget.observe(getattr(r, 'headers', None))
          raw = r.read()
          return json.loads(raw) if raw.strip() else None
      except urllib.error.HTTPError as e:
        try:
          resets = self.budget.observe(e.headers)
          # Mutation 429s have no proven pre-dispatch signature: never retry them.
          if method == 'GET' and e.code in (429, 502, 503, 504) and attempt < attempts - 1:
            delay = e.headers.get('Retry-After', '') if e.headers else ''
            delay = min(30, int(delay)) if delay.isascii() and delay.isdigit() and len(delay) < 12 else 2 ** attempt
            target = max(resets) if e.code == 429 and resets else time.monotonic() + delay
            e.close()
            self.budget.wait_until(target, quota=bool(e.code == 429 and resets))
            continue
          try:
            detail = json.loads(e.read())
          except (ValueError, OSError, http.client.HTTPException):
            detail = {}
        finally:
          e.close()
        raise PBError(e.code, detail) from None
      except (http.client.HTTPException, OSError):
        raise RuntimeError('PocketBase transport response unconfirmed') from None

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
      if not isinstance(r, dict) or not isinstance(r.get('items'), list):
        raise ValueError('collection lookup did not confirm record inventory')
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
      if not (coll == 'projects' and e.status == 400 and _icon_only(fields)
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
      return _confirmed(coll, payload, row)

    if coll in IMMUTABLE:
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

  def write_batch(self, operations):
    """Confirm one contiguous slice; immutable rows are never upserted."""
    existing = set()
    for coll in IMMUTABLE:
      wanted = {op['body']['id']: op['body'] for op in operations if op['collection'] == coll}
      if not wanted:
        continue
      result = self._req('GET', f'/api/collections/{coll}/records', params={
        'filter': ' || '.join('id=' + json.dumps(rid) for rid in wanted),
        'page': 1, 'perPage': 50, 'skipTotal': 1,
        'fields': ','.join(('id',) + KEYS[coll]),
      })
      if not isinstance(result, dict) or not isinstance(result.get('items'), list):
        raise ValueError('immutable lookup did not confirm record absence')
      found = set()
      for row in result['items']:
        rid = row.get('id') if isinstance(row, dict) else None
        if not isinstance(rid, str) or rid not in wanted or rid in found:
          raise ValueError('immutable lookup returned unexpected record identity')
        _confirmed(coll, wanted[rid], row)
        found.add(rid)
      existing.update((coll, rid) for rid in found)
    requests, submitted = [], []
    for op in operations:
      coll, body = op['collection'], op['body']
      if (coll, body['id']) in existing:
        continue  # Preserve detail/is_read, including after a lost response.
      requests.append(_batch_request(op))
      submitted.append(op)
    if not requests:
      return
    omitted = set()
    for attempt in range(len(requests) + 1):
      try:
        results = self._req('POST', '/api/batch', {'requests': requests})
        break
      except PBError as e:
        # Only PocketBase's indexed transactional rollback envelope proves this
        # retry safe. A timeout, malformed result, or ordinary 400 is ambiguous.
        data = e.data.get('data')
        errors = data.get('requests') if isinstance(data, dict) else None
        if not (e.status == 400 and isinstance(data, dict) and set(data) == {'requests'}
                and isinstance(errors, dict) and len(errors) == 1):
          raise
        index, error = next(iter(errors.items()))
        if not (isinstance(index, str) and index.isascii() and index.isdigit()
                and str(int(index)) == index and int(index) < len(requests)
                and isinstance(error, dict) and error.get('code') == 'batch_request_failed'):
          raise
        i = int(index)
        response = error.get('response')
        statuses = ([response[k] for k in ('status', 'code') if k in response]
                    if isinstance(response, dict) else [])
        identity = (submitted[i]['collection'], submitted[i]['body']['id'])
        if not (statuses and all(type(s) is int and s == 400 for s in statuses)
                and _icon_only(response.get('data')) and identity not in omitted
                and submitted[i]['collection'] == 'projects'
                and requests[i]['body'].get('icon_url') and attempt < len(requests)):
          raise
        requests[i]['body'].pop('icon_url')  # Outgoing copy only; preserve intent.
        omitted.add(identity)
    if not isinstance(results, list) or len(results) != len(submitted):
      raise ValueError('batch response did not confirm every record')
    for op, result in zip(submitted, results):
      if not (isinstance(result, dict) and type(result.get('status')) is int
              and result['status'] == 200):
        raise ValueError('batch response did not confirm record status')
      _confirmed(op['collection'], op['body'], result.get('body'))
    if omitted:
      self.icon_fallbacks += len(omitted)
      print('  projects: omitted rejected icon_url (input retained in journal)', file=sys.stderr)


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
    try:
      fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
      try:
        os.fsync(fd)
      finally:
        os.close(fd)
    except Exception:
      raise CheckpointDurabilityError('checkpoint replacement durability unknown') from None
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
  scope = journal.get('requested_sources')
  if scope is not None and _source_scope(scope) != scope:
    raise ValueError('invalid journal source scope')
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


def _plan(db, merged, today, requested_sources=None):
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
          'requested_sources': _source_scope(requested_sources),
          'daily_workload': daily_workload(merged), 'operations': operations, 'next': 0}


def _event(pid, etype, src, title, detail, today):
  return {'project': pid, 'event_type': etype, 'source': src or '', 'title': title,
          'detail': detail, 'occurred_at': today, 'is_read': False}


def _drain(db, journal, path, max_writes, batch_size=1):
  stats = dict.fromkeys(COUNTERS, 0)
  stats.update(errors=0, attempted=0, icon_fallbacks=0, date=journal['date'])
  before = getattr(db, 'icon_fallbacks', 0)
  start = journal['next']
  stop = min(len(journal['operations']), start + max_writes)
  pos = start
  while pos < stop:
    op = journal['operations'][pos]
    stats['attempted'] += 1
    try:
      _check_deadline(db)
      operations = journal['operations'][pos:min(pos + batch_size, stop)]
      if batch_size > 1:
        operations = _batch_slice(operations)
      stats['attempted'] += len(operations) - 1
      if batch_size == 1:
        db.upsert(op['collection'], op['body'], rid=op['body']['id'])
      else:
        db.write_batch(operations)
    except Exception as e:
      stats['errors'] += 1
      stats['failure'] = {'collection': op['collection'], 'id': op['body']['id'],
                          'error_type': type(e).__name__,
                          'http_status': e.status if isinstance(e, PBError) else None}
      if isinstance(e, BatchSizeError):
        stats['failure'].update(reason='record_exceeds_batch_byte_limit',
                                max_batch_bytes=BATCH_MAX_BYTES)
      print(f'  {op["collection"]}/{op["body"]["id"]}: {type(e).__name__}; '
            'write unconfirmed, retained for replay', file=sys.stderr)
      break
    cursor = pos + len(operations)
    # Failed checkpoints leave the durable position behind; replay uses the
    # same id and never advances past unconfirmed work.
    try:
      _save(Path(str(path) + '.progress'),
            {'generation': journal['generation'], 'next': cursor})
    except Exception as e:
      stats['errors'] += 1
      stats['failure'] = {'collection': op['collection'], 'id': op['body']['id'],
                          'error_type': 'CheckpointError', 'http_status': None}
      if isinstance(e, CheckpointDurabilityError):
        journal['next'] = cursor
        for confirmed in operations:
          stats[confirmed['counter']] += 1
        stats['checkpoint_durability'] = 'unknown'
        stats['checkpointed_before_failure'] = pos
        stats['failure']['error_type'] = 'CheckpointDurabilityError'
        print('  checkpoint replacement is visible but durability is unknown; '
              'reload the journal before deciding replay', file=sys.stderr)
      else:
        print('  journal checkpoint failed before replacement; write retained for replay', file=sys.stderr)
      break
    journal['next'] = cursor
    pos = cursor
    for op in operations:
      stats[op['counter']] += 1
  stats['icon_fallbacks'] = getattr(db, 'icon_fallbacks', 0) - before
  stats['planned'] = len(journal['operations'])
  stats['generation'] = journal['generation']
  stats['daily_workload'] = journal.get('daily_workload')
  stats['requested_sources'] = journal.get('requested_sources')
  stats['confirmed_total'] = journal['next']
  stats['pending'] = stats['planned'] - stats['confirmed_total']
  for coll in KEYS:
    stats['pending_' + coll] = sum(op['collection'] == coll
                                  for op in journal['operations'][journal['next']:])
  stats['complete'] = stats['pending'] == 0 and stats['errors'] == 0
  if getattr(db, 'budget', None) is not None:
    stats.update(db.budget.report())
  return stats


def journal_state(db, journal_path=None):
  """Read pending status and intent version in the same lock window."""
  path = state_path(db, journal_path)
  with _lock(path):
    journal = _load(path, db)
    return {'generation': journal['generation'] if journal else None,
            'pending': bool(journal and journal['next'] < len(journal['operations']))}


def generation(db, journal_path=None):
  return journal_state(db, journal_path)['generation']


def run(db, merged, today=None, dry_run=False, workers=1, max_writes=200, journal_path=None,
        batch_size=1, requested_sources=None, expected_generation=_UNSPECIFIED):
  """Persist intent, then confirm at most max_writes record operations.

  An unfinished journal takes precedence over newly collected data.
  workers is retained for callers, but parallel writes are rejected.
  """
  if workers != 1 or max_writes < 1 or type(batch_size) is not int or not 1 <= batch_size <= 50:
    raise ValueError('workers must be 1, max_writes positive, and batch_size an integer in 1..50')
  today = today or date.today().isoformat()
  date.fromisoformat(today)
  requested_sources = _source_scope(requested_sources)
  if dry_run:
    return {'dry_run': True, 'projects': len(merged), 'complete': False}
  path = state_path(db, journal_path)
  with _lock(path):
    journal = _load(path, db)
    actual_generation = journal['generation'] if journal else None
    if expected_generation is not _UNSPECIFIED and actual_generation != expected_generation:
      raise RuntimeError('collector input superseded by another journal generation')
    resumed = bool(journal and journal['next'] < len(journal['operations']))
    if not resumed:
      _check_deadline(db)
      journal = _plan(db, merged, today, requested_sources)
      _check_deadline(db)
      _save(path, journal)
    stats = _drain(db, journal, path, max_writes, batch_size)
    stats['resumed'] = resumed
    return stats


def flush(db, workers=1, max_writes=200, journal_path=None, batch_size=1):
  """Resume saved intent without fetching upstream or regenerating events."""
  if workers != 1 or max_writes < 1 or type(batch_size) is not int or not 1 <= batch_size <= 50:
    raise ValueError('workers must be 1, max_writes positive, and batch_size an integer in 1..50')
  path = state_path(db, journal_path)
  with _lock(path):
    journal = _load(path, db)
    if journal is None:
      raise ValueError('no write journal to resume')
    stats = _drain(db, journal, path, max_writes, batch_size)
    stats['resumed'] = True
    return stats
