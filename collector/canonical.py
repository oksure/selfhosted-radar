"""Canonical identity for an app across the two source directories.

Order of preference:
  1. ``github:<owner>/<repo>``     a GitHub source-code URL
  2. ``git:<host>/<path>``         GitLab, Codeberg or any other git host
  3. ``web:<host>|<slug>``         a website only, no source code
"""
import re
from urllib.parse import urlparse

GIT_HOSTS = ('github.com', 'gitlab.com', 'codeberg.org', 'gitea.com', 'git.sr.ht', 'bitbucket.org')


def _clean_url(u):
  if not u:
    return ''
  u = u.strip()
  if not u.startswith(('http://', 'https://')):
    u = 'https://' + u.lstrip('/')
  return u


def repo_key(url):
  """``github:owner/repo`` / ``git:host/path`` from a source-code URL, else ''."""
  u = _clean_url(url)
  if not u:
    return ''
  p = urlparse(u)
  host = (p.hostname or '').lower()
  if host.startswith('www.'):
    host = host[4:]
  if not host or host not in GIT_HOSTS:
    return ''
  path = p.path.strip('/')
  # strip /tree/<ref>, /blob/<ref>, /releases, /issues... — keep owner/repo
  path = re.split(r'/(?:tree|blob|releases|issues|pulls|wiki|commits)(?:/|$)', path)[0]
  path = path.strip('/')
  if path.endswith('.git'):
    path = path[:-4]
  if not path:
    return ''
  parts = [s for s in path.split('/') if s]
  if host == 'github.com':
    if len(parts) < 2:
      return ''
    return f'github:{parts[0].lower()}/{parts[1].lower()}'
  return f'git:{host}/' + '/'.join(parts).lower()


def web_key(url, name=''):
  """``web:<host>|<slug>`` for a website-only app.

  A bare host would otherwise collapse every product on a multi-product site
  into one project, so the app name is kept on the slug when the path is empty.
  """
  u = _clean_url(url)
  if not u:
    return ''
  p = urlparse(u)
  host = (p.hostname or '').lower()
  if host.startswith('www.'):
    host = host[4:]
  if not host:
    return ''
  path = (p.path or '').strip('/')
  raw = path or f'{host}/{name}'
  slug = re.sub(r'[^a-z0-9]+', '-', raw.lower()).strip('-') or 'root'
  return f'web:{host}|{slug}'


def canonical_key(repo_url='', website_url='', name=''):
  return repo_key(repo_url) or web_key(website_url, name)


def repo_host(repo_url):
  """github | gitlab | codeberg | other, or '' when there is no repo."""
  u = _clean_url(repo_url)
  if not u:
    return ''
  host = (urlparse(u).hostname or '').lower()
  if host.startswith('www.'):
    host = host[4:]
  if host == 'github.com':
    return 'github'
  if host == 'gitlab.com':
    return 'gitlab'
  if host == 'codeberg.org':
    return 'codeberg'
  return 'other' if host else ''


def slug(key, taken):
  """A URL-safe slug derived from the canonical key, unique against ``taken``."""
  base = key.split(':', 1)[-1]
  base = base.replace('|', '-').replace('/', '-')
  base = re.sub(r'[^a-z0-9]+', '-', base.lower()).strip('-') or 'app'
  if base not in taken:
    return base
  n = 2
  while f'{base}-{n}' in taken:
    n += 1
  return f'{base}-{n}'
