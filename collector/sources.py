"""Source adapters. Stage 1 ships the two directory sources.

Each adapter yields one ``RawApp`` per upstream entry. ``sources`` lists the
adapter names it contributes; Stage 2 adds ``gh_trending`` / ``gh_popular``
here without touching the merge or the schema.
"""
import html
import io
import json
import os
import re
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta

import yaml

AWESOME_TARBALL = ('https://github.com/awesome-selfhosted/awesome-selfhosted-data'
                   '/archive/refs/heads/master.tar.gz')
SELFHST_SOFTWARE = 'https://r2.selfh.st/directory/software.json'
SELFHST_METRICS = 'https://r2.selfh.st/directory/metrics.json'

UA = {'User-Agent': 'selfhosted-radar/1.0 (+https://selfhosted.pockethost.io)'}


@dataclass
class RawApp:
  source: str                      # awesome | selfhst
  external_id: str
  name: str = ''
  website_url: str = ''
  repo_url: str = ''
  demo_url: str = ''
  icon_url: str = ''
  description: str = ''
  license: str = ''
  language: str = ''
  platforms: list = field(default_factory=list)
  raw_tags: list = field(default_factory=list)     # canonical tag labels
  alternatives_refs: list = field(default_factory=list)
  stars: int = 0
  forks: int = 0
  selfhst_trending: int = 0
  last_commit_at: str = ''
  latest_release_tag: str = ''
  latest_release_at: str = ''
  archived: bool = False
  source_url: str = ''
  raw_payload: dict = field(default_factory=dict)


def fetch(url, timeout=120):
  req = urllib.request.Request(url, headers=UA)
  with urllib.request.urlopen(req, timeout=timeout) as r:
    return r.read()


def _selfhst_icon(e):
  """selfh.st keeps its icon set in the selfhst/icons repo, keyed by reference."""
  override = e.get('icon_override')
  if isinstance(override, str) and override.strip():
    ov = override.strip()
    if ov.startswith('http'):
      return ov
    if '/' not in ov and ':' not in ov:
      return f'https://cdn.jsdelivr.net/gh/selfhst/icons/png/{ov}.png'
  ref = (e.get('reference') or '').strip()
  source = e.get('icon_source') or ''
  if not ref or source == 'None':
    return ''
  if source == 'Apps':
    return ''  # an app-store icon, not ours to mirror
  return f'https://cdn.jsdelivr.net/gh/selfhst/icons/png/{ref}.png'


def _normalize_icon_url(raw):
  v = (raw or '').strip()
  if not v:
    return ''
  if v.startswith(('http://', 'https://')):
    return v
  if '/' not in v and ':' not in v:
    return f'https://cdn.jsdelivr.net/gh/selfhst/icons/png/{v}.png'
  return ''


def _as_list(v):
  if v is None:
    return []
  return v if isinstance(v, list) else [v]


class AwesomeSelfhosted:
  """``awesome-selfhosted-data``: one tarball, then ``software/*.yml`` + ``tags/*.yml``."""

  name = 'awesome'
  branch = 'master'

  def __init__(self, tarball_url=AWESOME_TARBALL):
    self.tarball_url = tarball_url

  def collect(self):
    blob = fetch(self.tarball_url)
    tag_labels = {}
    software = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as tf:
      for m in tf.getmembers():
        if not m.isfile():
          continue
        parts = m.name.split('/', 2)
        if len(parts) < 3:
          continue
        kind, rel = parts[1], parts[2]
        if kind == 'tags' and rel.endswith('.yml'):
          doc = yaml.safe_load(tf.extractfile(m)) or {}
          label = doc.get('name') or rel[:-4]
          desc = doc.get('description') or ''
          for alias in [rel[:-4], *(doc.get('aliases') or []), doc.get('name')]:
            if alias:
              tag_labels[str(alias).lower()] = (label, desc)
        elif kind == 'software' and rel.endswith('.yml'):
          software[rel[:-4]] = yaml.safe_load(tf.extractfile(m)) or {}
    for ext_id, doc in software.items():
      tags = []
      for t in _as_list(doc.get('tags')):
        label, _ = tag_labels.get(str(t).lower(), (t, ''))
        if label not in tags:
          tags.append(label)
      lic = ', '.join(_as_list(doc.get('licenses'))) or ''
      rel = doc.get('current_release') or {}
      yield RawApp(
        source=self.name,
        external_id=ext_id,
        name=doc.get('name') or ext_id,
        website_url=doc.get('website_url') or '',
        repo_url=doc.get('source_code_url') or '',
        demo_url=doc.get('demo_url') or '',
        icon_url=_normalize_icon_url(doc.get('icon_url')),
        description=doc.get('description') or '',
        license=lic,
        language=doc.get('language') or '',
        platforms=_as_list(doc.get('platforms')),
        raw_tags=tags,
        stars=int(doc.get('stargazers_count') or 0),
        last_commit_at=str(doc.get('updated_at') or ''),
        latest_release_tag=str(rel.get('tag') or '') if isinstance(rel, dict) else '',
        latest_release_at=str(rel.get('published_at') or '') if isinstance(rel, dict) else '',
        archived=bool(doc.get('archived')),
        source_url=f'https://awesome-selfhosted.net/tags/',
        raw_payload=doc,
      )


class SelfhSt:
  """``selfh.st/apps``: two public R2 JSON documents joined on ``id``."""

  name = 'selfhst'

  def __init__(self, software_url=SELFHST_SOFTWARE, metrics_url=SELFHST_METRICS):
    self.software_url = software_url
    self.metrics_url = metrics_url

  def collect(self):
    sw = json.loads(fetch(self.software_url))
    metrics = json.loads(fetch(self.metrics_url))
    tags = sw.get('tags') or {}
    alts = sw.get('alternatives') or {}
    per = {str(a['id']): a for a in (metrics.get('apps') or [])}
    built = str(metrics.get('built') or '')
    for e in sw.get('entries') or []:
      ext_id = str(e['id'])
      mm = per.get(ext_id, {})
      yield RawApp(
        source=self.name,
        external_id=ext_id,
        name=e.get('name') or '',
        website_url=e.get('website') or '',
        repo_url=e.get('repository') or '',
        icon_url=_selfhst_icon(e),
        description=e.get('description') or '',
        license=e.get('license') or '',
        language=e.get('language') or '',
        platforms=_as_list(e.get('platform')),
        raw_tags=[tags.get(str(t), str(t)) for t in _as_list(e.get('tags'))],
        alternatives_refs=_as_list(e.get('alternatives')),
        stars=int(mm.get('stars') or 0),
        forks=int(mm.get('forks') or 0),
        selfhst_trending=int(mm.get('trending') or 0),
        last_commit_at=str(mm.get('commit') or ''),
        source_url=f'https://selfh.st/apps/?ref={e.get("reference") or ""}',
        raw_payload={'entry': e, 'metrics': mm, 'built': built},
      )


def _gh_token():
  return os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_PERSONAL_ACCESS_TOKEN') or ''


def _gh_json(url, timeout=30):
  headers = {
    'User-Agent': UA['User-Agent'],
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
  }
  tok = _gh_token()
  if tok:
    headers['Authorization'] = f'Bearer {tok}'
  req = urllib.request.Request(url, headers=headers)
  with urllib.request.urlopen(req, timeout=timeout) as r:
    return json.loads(r.read())


def _gh_search_rest(q, page=1, per_page=100, timeout=30):
  """Call GitHub Search REST API with rate-limit-aware pacing and retry."""
  qs = urllib.parse.urlencode({'q': q, 'sort': 'stars', 'order': 'desc', 'per_page': per_page, 'page': page})
  url = f'https://api.github.com/search/repositories?{qs}'
  headers = {
    'User-Agent': UA['User-Agent'],
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
  }
  tok = _gh_token()
  if tok:
    headers['Authorization'] = f'Bearer {tok}'
  for attempt in range(4):
    req = urllib.request.Request(url, headers=headers)
    try:
      with urllib.request.urlopen(req, timeout=timeout) as r:
        rem = r.headers.get('X-RateLimit-Remaining')
        rst = r.headers.get('X-RateLimit-Reset')
        data = json.loads(r.read())
      if rem is not None and int(rem) <= 2 and rst:
        wait = max(1, int(rst) - int(time.time()) + 2)
        time.sleep(min(wait, 65))
      return data
    except urllib.error.HTTPError as e:
      if e.code in (403, 429) and attempt < 3:
        rst = e.headers.get('X-RateLimit-Reset') if e.headers else None
        wait = max(5, int(rst) - int(time.time()) + 2) if rst else 62
        time.sleep(min(wait, 65))
        continue
      if e.code in (500, 502, 503, 504) and attempt < 3:
        time.sleep(2 * (attempt + 1))
        continue
      raise
    except (urllib.error.URLError, TimeoutError, OSError):
      if attempt == 3:
        raise
      time.sleep(2 * (attempt + 1))
  raise RuntimeError(f'GitHub search failed for {q} page={page}')


def _clean_url(raw):
  v = (raw or '').strip()
  if not v:
    return ''
  if v.startswith(('http://', 'https://')):
    return v[:500]
  if '.' in v and ' ' not in v and ':' not in v:
    return f'https://{v}'[:500]
  return ''


def _gh_raw_from_repo(repo_obj, source, ext_id, source_url, extra_tags=None, extra_payload=None):
  full = repo_obj.get('full_name') or ext_id
  owner = (repo_obj.get('owner') or {}).get('login') or (full.split('/')[0] if '/' in full else '')
  avatar = (repo_obj.get('owner') or {}).get('avatar_url') or (f'https://github.com/{owner}.png?size=64' if owner else '')
  lic = ((repo_obj.get('license') or {}).get('spdx_id') or '')
  if lic == 'NOASSERTION':
    lic = ''
  tags = [t for t in (repo_obj.get('topics') or [])[:6] if t]
  for t in (extra_tags or []):
    if t and t not in tags:
      tags.append(t)
  payload = {
    'full_name': full,
    'stargazers_count': repo_obj.get('stargazers_count') or 0,
    'forks_count': repo_obj.get('forks_count') or 0,
    'pushed_at': repo_obj.get('pushed_at') or '',
    'topics': repo_obj.get('topics') or [],
  }
  if extra_payload:
    payload.update(extra_payload)
  return RawApp(
    source=source,
    external_id=full,
    name=(repo_obj.get('name') or full.split('/')[-1])[:200],
    website_url=_clean_url(repo_obj.get('homepage')),
    repo_url=repo_obj.get('html_url') or f'https://github.com/{full}',
    icon_url=avatar,
    description=(repo_obj.get('description') or '').strip()[:2000],
    license=lic,
    language=repo_obj.get('language') or '',
    raw_tags=tags,
    stars=int(repo_obj.get('stargazers_count') or 0),
    forks=int(repo_obj.get('forks_count') or 0),
    last_commit_at=str(repo_obj.get('pushed_at') or ''),
    archived=bool(repo_obj.get('archived')),
    source_url=source_url,
    raw_payload=payload,
  )


def _strip_tags(raw):
  return html.unescape(re.sub(r'<[^>]+>', ' ', raw or '')).strip()


def _parse_int(s):
  digits = re.sub(r'[^\d]', '', s or '')
  return int(digits) if digits else 0


def _parse_trending_html(page_html, since='daily'):
  """Parse ``github.com/trending`` HTML rows into normalized dicts."""
  rows = []
  for block in re.findall(r'<article[^>]*class="[^"]*Box-row[^"]*"[^>]*>(.*?)</article>', page_html, re.S):
    m = re.search(r'<h2[^>]*>.*?<a[^>]*href="/([^"/]+/[^"/#?]+)"', block, re.S)
    if not m:
      continue
    full = m.group(1).strip()
    desc_m = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
    desc = re.sub(r'\s+', ' ', _strip_tags(desc_m.group(1))) if desc_m else ''
    lang_m = re.search(r'itemprop="programmingLanguage"[^>]*>(.*?)<', block, re.S)
    lang = _strip_tags(lang_m.group(1)) if lang_m else ''
    stars_m = re.search(r'href="/[^"]+/stargazers"[^>]*>(.*?)</a>', block, re.S)
    forks_m = re.search(r'href="/[^"]+/forks"[^>]*>(.*?)</a>', block, re.S)
    period_m = re.search(r'([\d,]+)\s+stars\s+(?:today|this week|this month)', block, re.I)
    rows.append({
      'full_name': full,
      'description': desc,
      'language': lang,
      'stars': _parse_int(stars_m.group(1)) if stars_m else 0,
      'forks': _parse_int(forks_m.group(1)) if forks_m else 0,
      'stars_period': _parse_int(period_m.group(1)) if period_m else 0,
      'since': since,
    })
  return rows


class GitHubTrending:
  """``github.com/trending`` (daily + weekly), enriched via GitHub REST when a token exists."""

  name = 'gh_trending'
  BASE = 'https://github.com/trending'

  def __init__(self, sinces=('daily', 'weekly'), enrich=True):
    self.sinces = sinces
    self.enrich = enrich

  def collect(self):
    seen = {}
    for since in self.sinces:
      url = f'{self.BASE}?since={urllib.parse.quote(since)}'
      raw_html = fetch(url, timeout=30).decode('utf-8', errors='replace')
      for row in _parse_trending_html(raw_html, since=since):
        fn = row['full_name']
        if fn not in seen:
          seen[fn] = row
        else:
          seen[fn].setdefault('also_since', []).append(since)

    meta = {}
    if self.enrich and _gh_token() and seen:
      def _fetch_one(fn):
        try:
          return fn, _gh_json(f'https://api.github.com/repos/{fn}', timeout=15)
        except Exception:
          return fn, None

      with ThreadPoolExecutor(max_workers=4) as pool:
        for fn, obj in pool.map(_fetch_one, seen.keys()):
          if obj:
            meta[fn] = obj

    for fn, row in seen.items():
      extra = {'since': row['since'], 'stars_period': row['stars_period']}
      if fn in meta:
        yield _gh_raw_from_repo(
          meta[fn], self.name, fn, f'https://github.com/trending?since={row["since"]}',
          extra_tags=['github-trending'], extra_payload=extra,
        )
      else:
        owner, repo = fn.split('/', 1)
        yield RawApp(
          source=self.name,
          external_id=fn,
          name=repo,
          repo_url=f'https://github.com/{fn}',
          icon_url=f'https://github.com/{owner}.png?size=64',
          description=row['description'],
          language=row['language'],
          raw_tags=['github-trending'],
          stars=row['stars'],
          forks=row['forks'],
          source_url=f'https://github.com/trending?since={row["since"]}',
          raw_payload=row,
        )


class GitHubPopular:
  """GitHub Search REST (self-hosted topics + breakout + top 10,000 by stars)."""

  name = 'gh_popular'
  SEARCH_API = 'https://api.github.com/search/repositories'

  def __init__(self, queries=None, top_limit=10000):
    self.queries = queries
    self.top_limit = top_limit

  def _default_queries(self):
    recent = (date.today() - timedelta(days=90)).isoformat()
    return [
      ('topic:self-hosted stars:>=500', 1),
      ('topic:self-hosted stars:>=500', 2),
      ('topic:selfhosted stars:>=300', 1),
      (f'created:>={recent} stars:>=1000', 1),
    ]

  def _collect_top_rest(self, seen):
    """Fetch top ``self.top_limit`` repos by stars via Search REST star-range buckets."""
    stack = [(5000, None)]
    while stack and len(seen) < self.top_limit:
      lo, hi = stack.pop()
      q = f'stars:>={lo}' if hi is None else f'stars:{lo}..{hi}'
      data = _gh_search_rest(q, page=1, per_page=100)
      total = int(data.get('total_count') or 0)
      if total > 950 and (hi is None or lo < hi):
        mid = max(lo + 1, 50000) if hi is None else (lo + hi) // 2
        if hi is None or mid > lo:
          stack.append((lo, mid))
          stack.append((mid + 1, hi))
          continue
      pages = min(10, (total + 99) // 100) if total else 1
      print(f'  gh_popular {q}: {total} repos ({pages} pages), collected={len(seen)}', file=sys.stderr, flush=True)
      for page in range(1, pages + 1):
        if page > 1:
          data = _gh_search_rest(q, page=page, per_page=100)
        items = data.get('items') or []
        if not items:
          break
        for item in items:
          fn = item.get('full_name')
          if fn and fn not in seen:
            seen[fn] = _gh_raw_from_repo(
              item, self.name, fn,
              f'https://github.com/search?q={urllib.parse.quote(q)}&type=repositories',
              extra_tags=['github-popular'], extra_payload={'query': q},
            )
            if len(seen) >= self.top_limit:
              return

  def collect(self):
    seen = {}
    for q, page in (self.queries or self._default_queries()):
      qs = urllib.parse.urlencode({'q': q, 'sort': 'stars', 'order': 'desc', 'per_page': 100, 'page': page})
      data = _gh_json(f'{self.SEARCH_API}?{qs}', timeout=30)
      for item in data.get('items') or []:
        fn = item.get('full_name')
        if fn and fn not in seen:
          seen[fn] = _gh_raw_from_repo(
            item, self.name, fn,
            f'https://github.com/search?q={urllib.parse.quote(q)}&type=repositories',
            extra_tags=['github-popular'], extra_payload={'query': q},
          )
    if self.queries is None and self.top_limit > 0 and _gh_token():
      self._collect_top_rest(seen)
    yield from seen.values()


ADAPTERS = {
  AwesomeSelfhosted.name: AwesomeSelfhosted,
  SelfhSt.name: SelfhSt,
  GitHubTrending.name: GitHubTrending,
  GitHubPopular.name: GitHubPopular,
}

PLANNED_SOURCES = ()


def get(name):
  if name not in ADAPTERS:
    raise SystemExit(f'unknown source: {name} (have {", ".join(ADAPTERS)})')
  return ADAPTERS[name]()
