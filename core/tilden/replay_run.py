"""One graded run of the unit-ready job: every worker on the same frozen cases, scored by core/tilden/replay.py.

python -m core.tilden.replay_run --set <cases file> --arms fms,rival,fixed,planner,hybrid --out <result json>
Prints only the totals; the cases' words stay in the files."""
import argparse
import hashlib
import json
import os
import subprocess
import time
from dataclasses import asdict

from core.tilden import cases, replay, runtime


def arm_for(name, client=None):
    if name == 'fms':
        return lambda store, stream, now: None
    if name == 'rival':
        from core.tilden import rival
        return rival.arm
    if name in ('planner', 'fixed', 'hybrid'):
        return lambda store, stream, now: runtime.sweep(store, stream, now, chooser=name, client=client)
    raise ValueError(f'unknown worker {name}')


def run_arm(name, parsed, client=None):
    out = []
    for case in parsed:
        seen = {}
        arm = arm_for(name, client)

        def wrapped(store, stream, now):
            seen['s'] = (store, stream)
            arm(store, stream, now)
        started = time.monotonic()
        error = None
        try:
            trace = replay.replay(case, wrapped)
        except Exception as exc:
            error = type(exc).__name__
        events, steps = [], []
        if 's' in seen:
            store, stream = seen['s']
            events = store.stream_events(stream)
            steps = [s for c in cases.load(store, stream).values() for s in c.steps if isinstance(s, dict)]
            if any(s.get('reason', '').startswith('The case stopped on an error') for s in steps):
                error = error or 'CaseError'
        row = {'case': case.id, 'error': error} if error else asdict(replay.score(case, trace))
        if not error:
            row['trace'] = [[s.entry, s.rentable, s.needs_staff, s.paused, s.actions] for s in trace]
        row.update({
            'cents': round(sum(e.payload.get('cost_cents', 0) for e in events if e.kind in ('tilden.planner.call', 'tilden.decision'))
                           + sum(s.get('reader_cents', 0) for s in steps), 4),
            'refused': sum(1 for s in steps if s.get('allowed') is False or s.get('refused')),
            'second_opinions': [[s['second_opinion'], s['p']] for s in steps if s.get('second_opinion')],
            'seconds': round(time.monotonic() - started, 1)})
        out.append(row)
    return out


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--set', required=True)
    p.add_argument('--arms', default='fms,rival,fixed,planner,hybrid')
    p.add_argument('--out', required=True)
    a = p.parse_args(argv)
    raw = open(a.set, 'rb').read()
    parsed = replay.parse(raw.decode())
    client = None
    names = a.arms.split(',')
    if any(n in ('planner', 'fixed', 'hybrid') for n in names):
        import anthropic
        client = anthropic.Anthropic()
    try:
        commit = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip() or None
    except FileNotFoundError:     # the test image has no git; the run's code is pinned by the freeze record instead
        commit = None
    report = {'set_sha256': hashlib.sha256(raw).hexdigest(), 'cases': len(parsed), 'commit': commit,
              'planner_model': os.getenv('TILDEN_PLANNER_MODEL', 'claude-haiku-5-5'), 'planner_effort': os.getenv('TILDEN_PLANNER_EFFORT', 'medium'),
              'cutoffs': {'ready': runtime.READY_CUTOFF, 'same_fault': runtime.SAME_FAULT_CUTOFF, 'tenant_items': runtime.BELONGINGS_CUTOFF},
              'arms': {}}
    for name in names:
        rows = run_arm(name, parsed, client)
        scored = [r for r in rows if 'error' not in r]
        report['arms'][name] = {'totals': replay.totals([replay.Result(**{k: r[k] for k in replay.Result.__dataclass_fields__}) for r in scored]),
                                'errors': len(rows) - len(scored), 'cents': round(sum(r.get('cents', 0) for r in rows), 3),
                                'refused': sum(r.get('refused', 0) for r in scored), 'rows': rows}
        with open(a.out, 'w') as f:
            json.dump(report, f, indent=1, default=str)
        print(name, json.dumps({k: v for k, v in report['arms'][name].items() if k != 'rows'}), flush=True)


if __name__ == '__main__':
    main()
