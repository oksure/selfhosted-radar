"""Push the merged dataset into a PocketBase instance.

Reads go through the public API; writes use a superuser token that never
reaches argv, logs or the repository.
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

UA = {'User-Agent': 'selfhosted-radar/1.0'}


class PB:
  def __init__(self, base, email, password, quiet=False):
    self.base = base.rstrip('/')
    self.quiet = quiet
    self.token = None
    self.stash_path = None
    self._login(email, password)

  def _req(self, method, path, body=None, params=None, auth=True):
    url = self.base + path
    if params:
      url += '?' + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    h = dict(UA)
    h['Content-Type'] = 'application/json'
    if auth and self.token:
      h['Authorization'] = self.token
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
      with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
        return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
      detail = e.read().decode(errors='replace')[:400]
      raise RuntimeError(f'{method} {path} -> {e.code}: {detail}') from None

  def _login(self, email, password):
    for attempt in range(4):
      try:
        r = self._req('POST', '/api/collections/_superusers/auth-with-password',
                      {'identity': email, 'password': password}, auth=False)
        self.token = r['token']
        return
      except (RuntimeError, urllib.error.URLError, OSError) as e:
        if attempt == 3:
          raise
        if not self.quiet:
          print(f'  login retry {attempt + 1}: {e}', file=sys.stderr)
        time.sleep(3 * (attempt + 1))

  def all(self, coll, flt='', fields='', per=500):
    page, out = 1, []
    while True:
      r = self._req('GET', f'/api/collections/{coll}/records',
                    params={'page': page, 'perPage': per, 'filter': flt,
                            'fields': fields, 'skipTotal': 1})
      items = r.get('items', [])
      out.extend(items)
      if len(items) < per:
        return out
      page += 1

  def count(self, coll, flt=''):
    r = self._req('GET', f'/api/collections/{coll}/records',
                  params={'perPage': 1, 'filter': flt})
    return r.get('totalItems', 0)

  def import_records(self, coll, rows):
    """Create or update records via upsert (PocketBase >=0.23 removed /import)."""
    if not rows:
      return 0
    from concurrent.futures import ThreadPoolExecutor

    def _one(r):
      try:
        self.upsert(coll, r, rid=r.get('id'))
        return 1
      except RuntimeError as e:
        if '-> 429' in str(e):
          raise
        print(f'  upsert {coll} failed: {e}', file=sys.stderr, flush=True)
        return 0

    out = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
      for n in pool.map(_one, rows):
        out += n
    return out

  def upsert(self, coll, body, rid=None):
    if rid:
      try:
        return self._req('PATCH', f'/api/collections/{coll}/records/{rid}', body)
      except RuntimeError as e:
        if '404' not in str(e):
          raise
    return self._req('POST', f'/api/collections/{coll}/records', body)


def run(db, merged, today=None, dry_run=False, workers=1):
  """Write the merged dataset within the instance's hourly request budget.

  PocketHost caps requests per IP per hour, so volume is the constraint, not
  order: ``projects`` goes in through one bulk import (one request), every
  other collection resolves its relation with a single filtered read.
  """
  today = today or date.today().isoformat()
  stats = {'projects_created': 0, 'projects_updated': 0, 'entries': 0,
           'snapshots': 0, 'events': 0, 'errors': 0}
  if dry_run:
    for g in merged:
      stats['projects_created'] += 1
      stats['entries'] += len(g['sources'])
      stats['snapshots'] += 1
    return stats

  from .merge import activity_status, delta, is_star_surge

  existing = {r['canonical_key']: r
              for r in db.all('projects', fields='id,canonical_key,slug,latest_release_tag,archived,first_seen_at,sources,unified_tags,stars,activity_status')}
  entry_keys = {(r['source'], r['external_id']) for r in db.all('source_entries', fields='source,external_id')}
  snap_keys = {(r['project'], r['captured_date'])
               for r in db.all('repo_snapshots', fields='project,captured_date')}
  prev_stars = {}
  for r in db.all('repo_snapshots', fields='project,captured_date,stars'):
    prev_stars.setdefault(r['project'], []).append((date.fromisoformat(r['captured_date']), r['stars']))

  new_projects, upd = [], []
  for g in merged:
    p = g['project']
    old = existing.get(p['canonical_key'])
    p['activity_status'] = activity_status(p['last_commit_at'], p['archived'], date.fromisoformat(today))
    if old:
      p['slug'] = old['slug']
      p['first_seen_at'] = old.get('first_seen_at') or p['first_seen_at']
      p['last_synced_at'] = today
      upd.append((p['canonical_key'], p))
    else:
      new_projects.append({k: (v if v != '' else None) for k, v in p.items()})
      stats['projects_created'] += 1

  if new_projects:
    db.import_records('projects', new_projects)

  # Resolve every key to its record id once.
  ids = {r['canonical_key']: r['id'] for r in db.all('projects', fields='id,canonical_key')}
  keys = list(ids)
  for k in upd:
    if k[0] not in ids:
      stats['errors'] += 1

  rows_e, rows_s, rows_ev, rows_upd = [], [], [], []
  for g in merged:
    p = g['project']
    pid = ids.get(p['canonical_key'])
    if not pid:
      continue
    old = existing.get(p['canonical_key'])
    if old:
      stats['projects_updated'] += 1
      new_tag = p['latest_release_tag']
      old_tag = old.get('latest_release_tag') or ''
      if new_tag and new_tag != old_tag:
        rows_ev.append(_event(pid, 'new_release', 'awesome', f'{p["name"]} released {new_tag}',
                              {'previous': old_tag, 'current': new_tag}, today))
      if p['archived'] and not old.get('archived'):
        rows_ev.append(_event(pid, 'archived_or_stale', 'awesome', f'{p["name"]} archived', {}, today))
      d1, d7, d30 = delta(sorted(prev_stars.get(pid, []) + [(date.fromisoformat(today), p['stars'])]),
                          date.fromisoformat(today))
      old_srcs = set(old.get('sources') or [])
      for src in ('gh_trending', 'gh_popular'):
        if src in p['sources'] and src not in old_srcs:
          rows_ev.append(_event(pid, f'new_in_{src}', src, f'{p["name"]} added to {src}', {'source': src}, today))
      old_stars = int(old.get('stars') or 0)
      star_moved = abs(p['stars'] - old_stars) >= max(25, int(old_stars * 0.01))
      changed = (
        list(old.get('sources') or []) != p['sources']
        or list(old.get('unified_tags') or []) != p['unified_tags']
        or star_moved
        or (old.get('activity_status') or '') != p['activity_status']
      )
      if changed:
        rows_upd.append({
          'id': pid,
          'sources': p['sources'], 'unified_tags': p['unified_tags'],
          'stars': p['stars'], 'forks': p['forks'], 'activity_status': p['activity_status'],
          'last_commit_at': p['last_commit_at'] or None,
          'stars_1d_delta': d1, 'stars_7d_delta': d7,
          'stars_30d_delta': d30, 'last_synced_at': today,
        })
      if is_star_surge(d7):
        rows_ev.append(_event(pid, 'star_surge', 'selfhst' if 'selfhst' in p['sources'] else p['sources'][0],
                              f'{p["name"]} +{d7} stars in 7 days', {'delta_7d': d7, 'stars': p['stars']}, today))
    else:
      for src in p['sources']:
        etype = f'new_in_{src}' if src in ('awesome', 'selfhst', 'gh_trending', 'gh_popular') else 'new_in_awesome'
        rows_ev.append(_event(pid, etype, src, f'{p["name"]} added to {src}', {'source': src}, today))
    for e in g['sources']:
      if (e.source, e.external_id) in entry_keys:
        continue
      rows_e.append({
        'project': pid, 'source': e.source, 'external_id': e.external_id,
        'source_url': e.source_url or None, 'raw_tags': e.raw_tags,
        'alternatives_refs': [str(a) for a in e.alternatives_refs],
        'raw_payload': e.raw_payload, 'last_seen_at': today,
      })
    if (pid, today) not in snap_keys:
      rows_s.append({'project': pid, 'captured_date': today, 'stars': p['stars'], 'forks': p['forks'],
                     'selfhst_trending': p['selfhst_trending'], 'release_tag': p['latest_release_tag'] or None})
    stats['entries'] += sum(1 for e in g['sources'] if (e.source, e.external_id) not in entry_keys)
    stats['snapshots'] += 1

  stats['updates'] = len(rows_upd)
  stats['pending_entries'] = len(rows_e)
  stats['pending_snapshots'] = len(rows_s)
  stats['pending_events'] = len(rows_ev)
  stats['pending_updates'] = len(rows_upd)
  if getattr(db, 'stash_path', None):
    _stash(db, {'projects': rows_upd, 'source_entries': rows_e,
                'repo_snapshots': rows_s, 'surveillance_events': rows_ev})
  else:
    if rows_upd:
      db.import_records('projects', rows_upd)
    if rows_e and len(rows_e) <= 600:
      db.import_records('source_entries', rows_e)
    if rows_ev and len(rows_ev) <= 600:
      db.import_records('surveillance_events', rows_ev)
  return stats


def _event(pid, etype, src, title, detail, today):
  return {'project': pid, 'event_type': etype, 'source': src or None, 'title': title,
          'detail': detail, 'occurred_at': today, 'is_read': False}


def _stash(db, payload):
  """Keep the relation rows for the second, resumable phase."""
  path = getattr(db, 'stash_path', None)
  if not path:
    return
  import json
  with open(path, 'w') as ff:
    json.dump(payload, ff)


def flush(db, workers=4):
  """Second phase: relay the stashed relation rows through standard writes."""
  from concurrent.futures import ThreadPoolExecutor
  import json

  if not db.stash_path:
    return {}
  try:
    payload = json.load(open(db.stash_path))
  except FileNotFoundError:
    return {}
  allow = {'source_entries': set(), 'repo_snapshots': set(), 'surveillance_events': set()}
  for r in db.all('source_entries', fields='source,external_id'):
    allow['source_entries'].add((r['source'], r['external_id']))
  for r in db.all('repo_snapshots', fields='project,captured_date'):
    allow['repo_snapshots'].add((r['project'], r['captured_date']))
  for r in db.all('surveillance_events', fields='project,event_type,occurred_at'):
    allow['surveillance_events'].add((r['project'], r['event_type'], r['occurred_at']))

  def key(coll, r):
    if coll == 'source_entries':
      return (r['source'], r['external_id'])
    if coll == 'repo_snapshots':
      return (r['project'], r['captured_date'])
    return (r['project'], r['event_type'], r['occurred_at'])

  counts = {}
  for coll, rows in payload.items():
    if not rows:
      counts[coll] = 0
      continue
    if coll == 'projects':
      db.import_records('projects', rows)
      counts[coll] = len(rows)
      continue
    todo = [r for r in rows if key(coll, r) not in allow[coll]]

    def one(r, coll=coll):
      try:
        if coll == 'source_entries':
          db.upsert('source_entries', r)
        elif coll == 'repo_snapshots':
          db.upsert('repo_snapshots', r)
        else:
          db.upsert('surveillance_events', r)
        return 1
      except RuntimeError as e:
        if '-> 429' in str(e) or '429' in str(e):
          raise
        return 0

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
      for n in pool.map(one, todo):
        done += n
    counts[coll] = done
  return counts
