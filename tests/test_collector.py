import io
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from collector import merge as M
from collector.canonical import canonical_key, repo_host, repo_key, slug, web_key
from collector.sources import AwesomeSelfhosted, SelfhSt, RawApp



# --- canonical keys -------------------------------------------------------

@pytest.mark.parametrize('url,want', [
  ('https://github.com/n8n-io/n8n', 'github:n8n-io/n8n'),
  ('https://github.com/n8n-io/n8n.git', 'github:n8n-io/n8n'),
  ('https://github.com/N8N-IO/N8N/', 'github:n8n-io/n8n'),
  ('https://github.com/n8n-io/n8n/tree/master/packages', 'github:n8n-io/n8n'),
  ('http://www.github.com/bubka/2fauth', 'github:bubka/2fauth'),
  ('https://gitlab.com/foo/bar', 'git:gitlab.com/foo/bar'),
  ('https://codeberg.org/forgejo/forgejo', 'git:codeberg.org/forgejo/forgejo'),
  ('https://example.com/download', ''),
  ('', ''),
])
def test_repo_key(url, want):
  assert repo_key(url) == want


def test_repo_host():
  assert repo_host('https://github.com/a/b') == 'github'
  assert repo_host('https://gitlab.com/a/b') == 'gitlab'
  assert repo_host('https://codeberg.org/a/b') == 'codeberg'
  assert repo_host('https://git.example.org/a/b') == 'other'
  assert repo_host('') == ''


def test_web_key_and_fallback():
  assert web_key('https://ente.com', 'Ente Auth') == 'web:ente.com|ente-com-ente-auth'
  assert web_key('https://www.ente.com/photos') == 'web:ente.com|photos'
  assert canonical_key('', 'https://a.example.com', 'Foo') == 'web:a.example.com|a-example-com-foo'
  # two products on one host stay apart
  assert canonical_key('', 'https://a.example.com', 'Foo') != canonical_key('', 'https://a.example.com', 'Bar')
  assert canonical_key('https://github.com/a/b', 'https://a.example.com') == 'github:a/b'


def test_slug_unique():
  taken = set()
  a = slug('github:a/b', taken)
  taken.add(a)
  assert a == 'a-b' and slug('github:a/b', taken) == 'a-b-2'


# --- merge ----------------------------------------------------------------

def _row(**kw):
  base = dict(source='awesome', external_id='x', name='X')
  base.update(kw)
  return RawApp(**base)


def test_merge_same_repo_from_both_sources():
  rows = [
    _row(source='awesome', external_id='n8n.yml', name='n8n',
         repo_url='https://github.com/n8n-io/n8n', raw_tags=['Automation'], stars=100),
    _row(source='selfhst', external_id='308', name='n8n',
         repo_url='https://github.com/n8n-io/n8n/', raw_tags=['Workflow'], stars=120, forks=7),
  ]
  out = M.merge(rows)
  assert len(out) == 1
  g = out[0]
  assert g['project']['canonical_key'] == 'github:n8n-io/n8n'
  assert g['project']['sources'] == ['awesome', 'selfhst']
  assert g['project']['unified_tags'] == ['in-use', 'Automation', 'Workflow']
  assert g['project']['stars'] == 120 and g['project']['forks'] == 7
  assert len(g['sources']) == 2


def test_merge_dedupes_repeated_upstream_row():
  rows = [
    _row(source='selfhst', external_id='1601', name='Soulbeet', repo_url='https://github.com/t/soulbeet'),
    _row(source='selfhst', external_id='1601', name='Soulbeet', repo_url='https://github.com/t/soulbeet'),
  ]
  out = M.merge(rows)
  assert len(out) == 1 and out[0]['project']['sources'] == ['selfhst']


def test_merge_monorepo_products_collapse_but_keep_both_entries():
  rows = [
    _row(source='selfhst', external_id='526', name='Ente Auth', repo_url='https://github.com/ente/ente'),
    _row(source='selfhst', external_id='474', name='Ente Photos', repo_url='https://github.com/ente/ente'),
  ]
  out = M.merge(rows)
  assert len(out) == 1
  assert len(out[0]['sources']) == 2


def test_merge_website_only_and_unique_slugs():
  rows = [_row(source='awesome', external_id='a', name='A', website_url='https://same.example.com'),
          _row(source='selfhst', external_id='b', name='B', website_url='https://same.example.com/other')]
  out = M.merge(rows)
  assert len(out) == 2
  assert len({g['project']['slug'] for g in out}) == 2


def test_merge_archived_only_when_all_sources_agree():
  rows = [_row(source='awesome', external_id='a', repo_url='https://github.com/a/b', archived=True),
          _row(source='selfhst', external_id='b', repo_url='https://github.com/a/b', archived=False)]
  assert M.merge(rows)[0]['project']['archived'] is False


# --- deltas and events ----------------------------------------------------

def test_delta_prefers_exact_day_then_tolerates_gap():
  today = date(2026, 11, 14)
  snaps = [(today - timedelta(days=1), 100), (today - timedelta(days=7), 50),
           (today - timedelta(days=30), 10), (today, 150)]
  assert M.delta(snaps, today) == (50, 100, 140)


def test_delta_tolerates_missed_day():
  today = date(2026, 11, 14)
  snaps = [(today - timedelta(days=2), 100), (today, 150)]
  d1, d7, d30 = M.delta(snaps, today)
  assert d1 == 50 and d30 == 0


def test_delta_without_history_is_zero():
  today = date(2026, 11, 14)
  assert M.delta([(today, 150)], today) == (0, 0, 0)


def test_activity_status():
  today = date(2026, 11, 14)
  assert M.activity_status('2026-11-01', False, today) == 'active'
  assert M.activity_status('2026-06-01', False, today) == 'slow'
  assert M.activity_status('2026-01-01', False, today) == 'slow'
  assert M.activity_status('2024-01-01', False, today) == 'stale'
  assert M.activity_status('', False, today) == 'stale'
  assert M.activity_status('2026-11-01', True, today) == 'archived'


def test_star_surge_threshold():
  assert M.is_star_surge(200) and not M.is_star_surge(199)


# --- source parsing -------------------------------------------------------

def test_awesome_parses_tarball_and_maps_tag_labels(monkeypatch):
  import tarfile
  buf = io.BytesIO()
  with tarfile.open(fileobj=buf, mode='w:gz') as tf:
    def add(name, doc):
      b = yaml.safe_dump(doc).encode()
      ti = tarfile.TarInfo(name)
      ti.size = len(b)
      tf.addfile(ti, io.BytesIO(b))
    add('awesome-selfhosted-data-master/tags/automation.yml', {'name': 'Automation', 'description': 'd'})
    add('awesome-selfhosted-data-master/software/n8n.yml', {
      'name': 'n8n', 'website_url': 'https://n8n.io/', 'source_code_url': 'https://github.com/n8n-io/n8n',
      'description': 'Workflow', 'licenses': ['Apache-2.0'], 'platforms': ['Nodejs'],
      'tags': ['automation'], 'stargazers_count': 5, 'updated_at': '2026-01-01',
      'current_release': {'tag': 'v1', 'published_at': '2026-01-02'}, 'archived': False})
  from collector import sources as S
  monkeypatch.setattr(S, 'fetch', lambda url, timeout=120: buf.getvalue())
  rows = list(AwesomeSelfhosted().collect())
  assert len(rows) == 1
  r = rows[0]
  assert r.raw_tags == ['Automation'] and r.latest_release_tag == 'v1' and r.repo_url.endswith('/n8n')


def test_selfhst_joins_software_and_metrics(monkeypatch):
  software = {'entries': [{'id': 1, 'name': 'App', 'website': 'https://app.example',
                           'repository': 'https://github.com/o/app', 'tags': [74],
                           'alternatives': [52]}], 'tags': {'74': 'Automation'},
              'alternatives': {'52': 'Other'}}
  metrics = {'built': '2026-11-13T00:00:00Z',
             'apps': [{'id': 1, 'stars': 42, 'forks': 3, 'commit': '2026-11-01', 'trending': 9}]}
  payloads = [json.dumps(software).encode(), json.dumps(metrics).encode()]
  it = iter(payloads)
  from collector import sources as S
  monkeypatch.setattr(S, 'fetch', lambda url, timeout=120: next(it))
  rows = list(SelfhSt().collect())
  assert len(rows) == 1
  r = rows[0]
  assert (r.stars, r.forks, r.selfhst_trending) == (42, 3, 9)
  assert r.raw_tags == ['Automation'] and r.alternatives_refs == [52]
