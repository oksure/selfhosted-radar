"""Stage the merged dataset directly into PocketBase's SQLite file.

Bulk loads never go through the HTTP API: PocketHost caps requests per IP per
hour, and a full run needs tens of thousands of writes. The flow is

  phost off <inst> --yes
  sftp download pb_data/data.db
  python3 -m collector.sqlstage data.db        (this module)
  sftp upload pb_data/data.db     (instance still off)
  phost on <inst>

Standard record writes stay available for small, interactive updates; this is
only for the bulk path.
"""
import json
import random
import sqlite3
import string
from datetime import date

from . import sources
from .merge import activity_status, delta, is_star_surge, merge


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
    cur.execute('''UPDATE projects SET stars=?, forks=?, selfhst_trending=?, last_commit_at=?,
        latest_release_tag=?, latest_release_at=?, archived=?, activity_status=?, last_synced_at=?,
        stars_1d_delta=?, stars_7d_delta=?, stars_30d_delta=?
        WHERE id=?''',
                (p['stars'], p['forks'], p['selfhst_trending'], p['last_commit_at'],
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
        evs.append(('new_in_awesome' if src == 'awesome' else 'new_in_selfhst', src,
                    f'{p["name"]} added to {src}', {'source': src}))
    else:
      old = cur.execute('SELECT latest_release_tag, archived FROM projects WHERE id=?', (pid,)).fetchone()
      if p['latest_release_tag'] and old and old[0] != p['latest_release_tag']:
        evs.append(('new_release', 'awesome', f'{p["name"]} released {p["latest_release_tag"]}',
                    {'previous': old[0], 'current': p['latest_release_tag']}))
    if is_star_surge(d7):
      evs.append(('star_surge', 'selfhst' if 'selfhst' in p['sources'] else 'awesome',
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
  ap = argparse.ArgumentParser(prog='collector.sqlstage')
  ap.add_argument('db_path')
  ap.add_argument('--today', default=date.today().isoformat())
  ap.add_argument('--dump', default='', help='also keep the merged JSON for auditing')
  a = ap.parse_args(argv)
  rows = []
  for name in sources.ADAPTERS:
    got = list(sources.get(name).collect())
    print(f'{name}: {len(got)}', flush=True)
    rows += got
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
