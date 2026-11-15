"""Daily collection entry point.

  python3 -m collector.sync                    fetch both directories, update the instance
  python3 -m collector.sync --dry-run          fetch, merge, report counts, write nothing
  python3 -m collector.sync --dump f.json      also keep the merged dataset for auditing
  python3 -m collector.sync --sources awesome  restrict the sources

Writes are serial and journaled. An incomplete run exits nonzero and can be
resumed without fetching upstream. Bulk loads still use collector.sqlstage.

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


def capacity(workload, max_writes):
  """Assess the steady daily baseline against one invocation per day."""
  result = {'assumption': 'one_invocation_per_day', 'operations_per_run': max_writes,
            'baseline_operations_per_day': None, 'shortfall_operations_per_day': None,
            'assessment': 'unknown'}
  if isinstance(workload, dict):
    projects, entries, baseline = (workload.get(k) for k in
                                   ('projects', 'source_entries', 'baseline_operations'))
    if (all(type(n) is int and n >= 0 for n in (projects, entries, baseline))
        and baseline == 2 * projects + entries):
      result['baseline_operations_per_day'] = baseline
      result['shortfall_operations_per_day'] = max(0, baseline - max_writes)
      result['assessment'] = 'baseline_within_cap' if baseline <= max_writes else 'insufficient'
  return result


def assess(stats, today, max_writes, explicit_resume):
  stats['requested_date'] = today
  stats['freshness'] = ('stale' if stats['date'] != today else
                        'current' if stats['complete'] else 'incomplete')
  stats['daily_capacity'] = capacity(stats.get('daily_workload'), max_writes)
  reasons = []
  if stats['errors']:
    reasons.append('write_failure')
  if not stats['complete']:
    reasons.append('pending_writes')
  if not explicit_resume:
    if stats['freshness'] == 'stale':
      reasons.append('stale_data')
    assessment = stats['daily_capacity']['assessment']
    if assessment != 'baseline_within_cap':
      reasons.append('capacity_' + assessment)
  stats['failure_reasons'] = reasons
  stats['success'] = not reasons
  return stats


def main(argv=None):
  ap = argparse.ArgumentParser(prog='collector.sync')
  ap.add_argument('--dry-run', action='store_true')
  ap.add_argument('--sources', default=','.join(sources.ADAPTERS))
  ap.add_argument('--dump', default='', help='write the merged dataset here as JSON')
  ap.add_argument('--today', default='')
  ap.add_argument('--resume', action='store_true', help='replay the saved journal without collecting')
  ap.add_argument('--state-file', default='', help='local write journal (default: XDG state directory)')
  ap.add_argument('--max-writes', type=int, default=200,
                  help='maximum serial record operations per invocation (default: 200)')
  a = ap.parse_args(argv)
  if a.max_writes < 1:
    ap.error('--max-writes must be positive')
  if a.resume and a.dry_run:
    ap.error('--resume and --dry-run cannot be combined')
  names = [s.strip() for s in a.sources.split(',') if s.strip()]
  today = a.today or date.today().isoformat()

  email, pwd = os.environ.get('SELFHOSTED_PB_EMAIL'), os.environ.get('SELFHOSTED_PB_PASS')
  if not a.dry_run and not (email and pwd):
    print('SELFHOSTED_PB_EMAIL / SELFHOSTED_PB_PASS missing (source agent-env.sh)', file=sys.stderr)
    return 2
  from .pbwrite import PB, daily_workload, flush, pending, run
  try:
    date.fromisoformat(today)
    db = None if a.dry_run else PB(BASE, email, pwd)
    if db and (a.resume or pending(db, a.state_file)):
      stats = flush(db, max_writes=a.max_writes, journal_path=a.state_file)
    else:
      rows = collect(names)
      merged = merge(rows)
      both = sum(1 for g in merged if len(g['sources']) > 1)
      print(f'merged: {len(merged)} projects, {both} present in multiple sources', file=sys.stderr)
      if a.dump:
        with open(a.dump, 'w', encoding='utf-8') as ff:
          json.dump([{'project': g['project'], 'sources': [s.source for s in g['sources']]} for g in merged],
                    ff, ensure_ascii=False)
      if a.dry_run:
        workload = daily_workload(merged)
        print(json.dumps({'dry_run': True, 'projects': len(merged), 'in_both': both,
                          'daily_workload': workload,
                          'daily_capacity': capacity(workload, a.max_writes)}))
        return 0
      stats = run(db, merged, today=today, max_writes=a.max_writes, journal_path=a.state_file)
      if not stats['resumed']:
        stats.update({'projects': len(merged), 'in_both': both})
    assess(stats, today, a.max_writes, a.resume)
    print(json.dumps(stats))
    return 0 if stats['success'] else 1
  except (RuntimeError, OSError, ValueError, KeyError, TypeError) as e:
    # Do not print server bodies, source text, credentials or raw payloads.
    print(f'collector failed: {type(e).__name__}; preserve the journal and retry --resume', file=sys.stderr)
    print(json.dumps({'complete': False, 'errors': 1, 'pending': None,
                      'error_type': type(e).__name__}))
    return 1


if __name__ == '__main__':
  sys.exit(main())
