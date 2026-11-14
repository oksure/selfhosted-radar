"""Source adapters. Stage 1 ships the two directory sources.

Each adapter yields one ``RawApp`` per upstream entry. ``sources`` lists the
adapter names it contributes; Stage 2 adds ``gh_trending`` / ``gh_popular``
here without touching the merge or the schema.
"""
import io
import json
import tarfile
import urllib.request
from dataclasses import dataclass, field

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
  if isinstance(override, str) and override.startswith('http'):
    return override
  ref = (e.get('reference') or '').strip()
  source = e.get('icon_source') or ''
  if not ref or source == 'None':
    return ''
  if source == 'Apps':
    return ''  # an app-store icon, not ours to mirror
  return f'https://cdn.jsdelivr.net/gh/selfhst/icons/png/{ref}.png'


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
        icon_url=doc.get('icon_url') or '',
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


ADAPTERS = {AwesomeSelfhosted.name: AwesomeSelfhosted, SelfhSt.name: SelfhSt}

# Stage 2 placeholders — declared so the schema and merge stay source-agnostic.
PLANNED_SOURCES = ('gh_trending', 'gh_popular')


def get(name):
  if name not in ADAPTERS:
    raise SystemExit(f'unknown source: {name} (have {", ".join(ADAPTERS)})')
  return ADAPTERS[name]()
