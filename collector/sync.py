"""Daily collection entry point.

  python3 -m collector.sync                    fetch both directories, update the instance
  python3 -m collector.sync --dry-run          fetch, merge, report counts, write nothing
  python3 -m collector.sync --dump f.json      also keep the merged dataset for auditing
  python3 -m collector.sync --sources awesome  restrict the sources

Writes are bounded and journaled (serial by default). An incomplete run exits nonzero and can be
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


def assess(stats, today, max_writes, explicit_resume, required_sources=None):
  stats['requested_date'] = today
  stats['freshness'] = ('stale' if stats['date'] != today else
                        'current' if stats['complete'] else 'incomplete')
  stats['daily_capacity'] = capacity(stats.get('daily_workload'), max_writes)
  stats['required_sources'] = sorted(set(required_sources)) if required_sources else None
  recorded = stats.get('requested_sources')
  stats['scope_assessment'] = ('unknown' if recorded is None or not required_sources else
                                'sufficient' if set(required_sources) <= set(recorded) else 'insufficient')
  reasons = []
  if stats['errors']:
    reasons.append('write_failure')
  if stats['pending'] > 0:
    reasons.append('pending_writes')
  if not explicit_resume:
    if stats['freshness'] == 'stale':
      reasons.append('stale_data')
    assessment = stats['daily_capacity']['assessment']
    if assessment != 'baseline_within_cap':
      reasons.append('capacity_' + assessment)
    if stats['scope_assessment'] != 'sufficient':
      reasons.append('scope_' + stats['scope_assessment'])
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
                  help='maximum record operations per invocation (default: 200)')
  ap.add_argument('--batch-size', type=int, default=1,
                  help='transactional records per batch, 1..50 (default: 1, serial)')
  ap.add_argument('--max-runtime-seconds', type=int, default=14400,
                  help='invocation runtime budget, including quota waits (default: 14400)')
  a = ap.parse_args(argv)
  if a.max_writes < 1:
    ap.error('--max-writes must be positive')
  if not 1 <= a.batch_size <= 50:
    ap.error('--batch-size must be in 1..50')
  if a.max_runtime_seconds < 1:
    ap.error('--max-runtime-seconds must be positive')
  if a.resume and a.dry_run:
    ap.error('--resume and --dry-run cannot be combined')
  names = [s.strip() for s in a.sources.split(',') if s.strip()]
  if not names or set(names) - set(sources.ADAPTERS):
    ap.error('--sources must name one or more known adapters')
  today = a.today or date.today().isoformat()

  email, pwd = os.environ.get('SELFHOSTED_PB_EMAIL'), os.environ.get('SELFHOSTED_PB_PASS')
  if not a.dry_run and not (email and pwd):
    print('SELFHOSTED_PB_EMAIL / SELFHOSTED_PB_PASS missing (source agent-env.sh)', file=sys.stderr)
    print(json.dumps({'complete': False, 'success': False, 'errors': 1, 'pending': None,
                      'phase': 'credentials', 'error_type': 'MissingCredentials',
                      'failure_reasons': ['credentials_failure']}))
    return 2
  from .pbwrite import COUNTERS, PB, RuntimeBudget, daily_workload, flush, journal_state, run
  budget = RuntimeBudget(a.max_runtime_seconds)
  stats, phase = None, 'authentication'
  try:
    date.fromisoformat(today)
    budget.check()
    db = None if a.dry_run else PB(BASE, email, pwd, False, budget)
    if db is not None:
      db.budget = budget
    phase = 'drain'
    state = journal_state(db, a.state_file) if db else {'generation': None, 'pending': False}
    expected_generation = state['generation']
    if db and (a.resume or state['pending']):
      stats = flush(db, max_writes=a.max_writes, journal_path=a.state_file, batch_size=a.batch_size)
      expected_generation = stats['generation']
    remaining = a.max_writes - (stats['attempted'] if stats else 0)
    if not a.resume and (stats is None or (stats['complete'] and remaining > 0)):
      phase = 'collect'
      budget.check()
      rows = collect(names)
      phase = 'merge'
      budget.check()
      merged = merge(rows)
      both = sum(1 for g in merged if len(g['sources']) > 1)
      print(f'merged: {len(merged)} projects, {both} present in multiple sources', file=sys.stderr)
      if a.dump:
        phase = 'dump'
        budget.check()
        with open(a.dump, 'w', encoding='utf-8') as ff:
          json.dump([{'project': g['project'], 'sources': [s.source for s in g['sources']]} for g in merged],
                    ff, ensure_ascii=False)
      if a.dry_run:
        budget.check()
        workload = daily_workload(merged)
        print(json.dumps({'dry_run': True, 'projects': len(merged), 'in_both': both,
                          'daily_workload': workload,
                          'daily_capacity': capacity(workload, a.max_writes), **budget.report()}))
        return 0
      phase = 'plan_and_write'
      budget.check()
      fresh = run(db, merged, today=today, max_writes=remaining, journal_path=a.state_file,
                  batch_size=a.batch_size, requested_sources=names,
                  expected_generation=expected_generation)
      # A competing writer can publish intent after pending() but before run()
      # locks it. Drain that intent first, then reuse the already collected input.
      # Each iteration spends the remaining record budget; no network refetch loop.
      while fresh['resumed'] and fresh['complete'] and fresh['attempted'] < remaining:
        remaining -= fresh['attempted']
        if stats is None:
          stats = fresh
        else:
          prior = stats
          stats = dict(fresh, drained_plan=prior)
          for key in (*COUNTERS, 'attempted', 'errors', 'icon_fallbacks'):
            stats[key] += prior[key]
        budget.check()
        fresh = run(db, merged, today=today, max_writes=remaining, journal_path=a.state_file,
                    batch_size=a.batch_size, requested_sources=names,
                    expected_generation=expected_generation)
      if not fresh['resumed']:
        fresh.update({'projects': len(merged), 'in_both': both})
      if stats is not None:
        fresh['drained_plan'] = stats
        for key in (*COUNTERS, 'attempted', 'errors', 'icon_fallbacks'):
          fresh[key] += stats[key]
        fresh['resumed'] = True
        fresh['collected_after_resume'] = True
      stats = fresh
    assess(stats, today, a.max_writes, a.resume, names)
    stats.update(budget.report())
    if stats['runtime_exhausted']:
      stats['success'] = False
      stats['failure_reasons'].append('runtime_exhausted')
    print(json.dumps(stats))
    return 0 if stats['success'] else 1
  except Exception as e:
    # Do not print server bodies, source text, credentials or raw payloads.
    print(f'collector failed: {type(e).__name__}; preserve the journal and rerun the original command', file=sys.stderr)
    failed = dict(stats) if stats is not None else {}
    if stats is not None:
      failed['drained_plan'] = stats
      for coll in ('projects', 'source_entries', 'repo_snapshots', 'surveillance_events'):
        failed['pending_' + coll] = None
      failed['fresh_pending'] = None
      failed['planned'] = None
    failed.update(complete=False, success=False, errors=failed.get('errors', 0) + 1,
                  pending=None, phase=phase, error_type=type(e).__name__,
                  failure_reasons=[phase + '_failure'])
    failed.update(budget.report())
    print(json.dumps(failed))
    return 1


if __name__ == '__main__':
  sys.exit(main())
