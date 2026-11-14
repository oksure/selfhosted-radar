"""Merge upstream entries into unified project rows."""
from datetime import date, datetime, timedelta, timezone

from .canonical import canonical_key, repo_host, slug

STAR_SURGE_ABS = 200
STAR_SURGE_PCT = 0.20
SLOW_DAYS = 90     # no commit for a quarter -> slow
STALE_DAYS = 365   # a year without a commit -> stale


def _pick(*vals):
  for v in vals:
    if v:
      return v
  return ''


def _today(now=None):
  return (now or datetime.now(timezone.utc)).date()


def merge(entries):
  """Group ``RawApp`` rows by canonical key; return unified project dicts.

  Each project keeps every contributing source row so the merge can be audited
  and re-derived, and picks field values from the higher-confidence source
  (awesome-selfhosted descriptions are curated; selfh.st carries metrics).
  """
  groups = {}
  seen = set()
  for e in entries:
    # upstream ships the odd repeated (source, id) row; keep the first
    if (e.source, e.external_id) in seen:
      continue
    seen.add((e.source, e.external_id))
    key = canonical_key(e.repo_url, e.website_url, e.name)
    if not key:
      key = f'name:{(e.name or e.external_id).strip().lower()}'
    groups.setdefault(key, []).append(e)

  taken = set()
  out = []
  for key in sorted(groups):
    rows = groups[key]
    aw = next((r for r in rows if r.source == 'awesome'), None)
    sh = next((r for r in rows if r.source == 'selfhst'), None)
    first = rows[0]
    name = _pick(aw and aw.name, sh and sh.name, first.name)
    repo_url = _pick(aw and aw.repo_url, sh and sh.repo_url, first.repo_url)
    tags = []
    for r in sorted(rows, key=lambda r: 0 if r.source == 'awesome' else 1):
      for t in r.raw_tags:
        if t and t not in tags:
          tags.append(t)
    platforms = []
    for r in rows:
      for p in r.platforms:
        if p and p not in platforms:
          platforms.append(p)
    today = _today()
    first_seen = today.isoformat()
    stars = max(int(r.stars or 0) for r in rows)
    forks = max(int(r.forks or 0) for r in rows)
    trending = max(int(r.selfhst_trending or 0) for r in rows)
    archived = all(bool(r.archived) for r in rows) if len(rows) > 1 else bool(first.archived)
    p = {
      'canonical_key': key,
      'slug': slug(key, taken),
      'name': name,
      'repo_full_name': _repo_full(repo_url),
      'repo_host': repo_host(repo_url),
      'homepage_url': _pick(aw and aw.website_url, sh and sh.website_url),
      'repo_url': repo_url,
      'demo_url': _pick(aw and aw.demo_url),
      'icon_url': _pick(aw and aw.icon_url, sh and sh.icon_url),
      'description': _pick(aw and aw.description, sh and sh.description),
      'license': _pick(sh and sh.license, aw and aw.license),
      'language': _pick(sh and sh.language, aw and aw.language),
      'platforms': platforms,
      'unified_tags': tags,
      'sources': sorted({r.source for r in rows}),
      'stars': stars,
      'forks': forks,
      'stars_1d_delta': 0,
      'stars_7d_delta': 0,
      'stars_30d_delta': 0,
      'selfhst_trending': trending,
      'last_commit_at': _pick(sh and sh.last_commit_at, aw and aw.last_commit_at),
      'latest_release_tag': _pick(aw and aw.latest_release_tag),
      'latest_release_at': _pick(aw and aw.latest_release_at),
      'archived': archived,
      'activity_status': 'archived' if archived else 'active',
      'first_seen_at': first_seen,
      'last_synced_at': first_seen,
    }
    taken.add(p['slug'])
    out.append({'project': p, 'sources': rows})
  return out


def _repo_full(url):
  from .canonical import repo_key
  k = repo_key(url)
  return k.split(':', 1)[1] if k.startswith('github:') else ''


def activity_status(last_commit_at, archived, today):
  """active (<=90d) | slow (<=365d) | stale (>365d or unknown) | archived."""
  if archived:
    return 'archived'
  if not last_commit_at:
    return 'stale'
  try:
    d = date.fromisoformat(str(last_commit_at)[:10])
  except ValueError:
    return 'stale'
  age = (today - d).days
  if age <= SLOW_DAYS:
    return 'active'
  if age <= STALE_DAYS:
    return 'slow'
  return 'stale'


def delta(snaps, today):
  """1d / 7d / 30d star deltas from a list of (date, stars) pairs, newest last."""
  by = {d: s for d, s in snaps}
  cur = by.get(today)
  if cur is None:
    return (0, 0, 0)
  out = []
  for back in (1, 7, 30):
    ref = None
    for k in range(back, back + 8):  # tolerate a missed day
      cand = today - timedelta(days=k)
      if cand in by:
        ref = by[cand]
        break
    out.append(cur - ref if ref is not None else 0)
  return tuple(out)


def is_star_surge(d7):
  return d7 >= STAR_SURGE_ABS
