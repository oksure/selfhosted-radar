"""Daily collection entry point.

  python3 -m collector.sync                    fetch both directories, update the instance
  python3 -m collector.sync --dry-run          fetch, merge, report counts, write nothing
  python3 -m collector.sync --dump f.json      also keep the merged dataset for auditing
  python3 -m collector.sync --sources awesome  restrict the sources

Updates go through the HTTP API, which suits the small daily delta but not a
bulk load: PocketHost caps requests per IP per hour. To load a large volume,
power the instance off and use the offline path (collector.sqlstage).

Credentials come from the environment (``SELFHOSTED_PB_EMAIL`` /
``SELFHOSTED_PB_PASS``), never from the repository.
"""
import argparse
import json
import os
import sys
from datetime import date

from . import sources
from .merge import merge

BASE = os.environ.get('SELFHOSTED_PB_URL', 'https://selfhosted.pockethost.io')


def collect(names):
  rows = []
  for n in names:
    got = list(sources.get(n).collect())
    print(f'{n}: {len(got)} entries', file=sys.stderr)
    rows.extend(got)
  return rows


def main(argv=None):
  ap = argparse.ArgumentParser(prog='collector.sync')
  ap.add_argument('--dry-run', action='store_true')
  ap.add_argument('--sources', default=','.join(sources.ADAPTERS))
  ap.add_argument('--dump', default='', help='write the merged dataset here as JSON')
  ap.add_argument('--today', default='')
  a = ap.parse_args(argv)
  names = [s.strip() for s in a.sources.split(',') if s.strip()]
  today = a.today or date.today().isoformat()

  rows = collect(names)
  merged = merge(rows)
  both = sum(1 for g in merged if len(g['sources']) > 1)
  print(f'merged: {len(merged)} projects, {both} present in both sources', file=sys.stderr)
  if a.dump:
    with open(a.dump, 'w') as ff:
      json.dump([{'project': g['project'], 'sources': [s.source for s in g['sources']]} for g in merged],
                ff, ensure_ascii=False)
    print(f'wrote {a.dump}', file=sys.stderr)

  if a.dry_run:
    print(json.dumps({'dry_run': True, 'projects': len(merged), 'in_both': both}))
    return 0

  email, pwd = os.environ.get('SELFHOSTED_PB_EMAIL'), os.environ.get('SELFHOSTED_PB_PASS')
  if not (email and pwd):
    print('SELFHOSTED_PB_EMAIL / SELFHOSTED_PB_PASS missing (source agent-env.sh)', file=sys.stderr)
    return 2
  from .pbwrite import PB, run
  db = PB(BASE, email, pwd)
  stats = run(db, merged, today=today)
  stats.update({'projects': len(merged), 'in_both': both, 'date': today})
  print(json.dumps(stats))
  return 0


if __name__ == '__main__':
  sys.exit(main())
