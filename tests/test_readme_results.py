"""The README's unit-ready table and its figures, rebuilt from the saved report so a hand edit cannot drift."""
import json
import re
from pathlib import Path

from core.tilden.replay import parse

ROOT = Path(__file__).resolve().parents[1]
WORKERS = {'The facility system alone': 'fms', 'Fixed rules, separate author': 'rival', 'The playbook in fixed order': 'fixed',
           'A step picker': 'picker', "Tilden's planner": 'planner'}


def fig(value):
    return f'{round(value, 1):g}'


def mean(runs, pick):
    return sum(pick(run) for run in runs) / len(runs)


def rebuilt():
    report = json.loads((ROOT / 'core/graded/unit_ready_1_report.json').read_text())
    cases = parse((ROOT / report['set']).read_text())
    due = {c.id for c in cases if c.rentable_after}
    never = {c.id for c in cases if not c.rentable_after}
    staff = sum(1 for c in cases if c.staff_decide)
    rows = {}
    for name, key in WORKERS.items():
        runs = report['runs'][key]
        rows[name] = {
            'due': mean(runs, lambda r: sum(x['release_right'] for x in r['rows'] if x['case'] in due)),
            'never': mean(runs, lambda r: sum(x['release_right'] for x in r['rows'] if x['case'] in never)),
            'early': mean(runs, lambda r: r['totals']['released_early']),
            'handoff': mean(runs, lambda r: r['totals']['handoff_right']),
            'needless': mean(runs, lambda r: r['totals']['needless_handoff']),
            'cents': mean(runs, lambda r: r['cents']),
        }
    return len(due), len(never), staff, rows


def section():
    text = (ROOT / 'README.md').read_text()
    start = text.index('### Whole cases: getting a vacated unit rentable again')
    end = text.find('\n#', start + 1)
    return text[start:end if end > 0 else None]


def test_table_matches_the_report():
    due, never, staff, rows = rebuilt()
    report = json.loads((ROOT / 'core/graded/unit_ready_1_report.json').read_text())
    assert [len(report['runs'][key]) for key in WORKERS.values()] == [1, 1, 3, 3, 3]
    assert 'The last three ran three times each; the table shows the mean' in section()
    lines = [line for line in section().splitlines() if line.startswith('|')]
    assert lines[0] == (f'| Worker | Offered again at the right entry (of {due}) | Kept off when it never should be offered (of {never}) '
                        f'| Offered too early | Handed to staff at the right entry (of {staff}) | Handed to staff with no need |')
    want = [f"| {name} | {fig(r['due'])} | {fig(r['never'])} | {fig(r['early'])} | {fig(r['handoff'])} | {fig(r['needless'])} |"
            for name, r in rows.items()]
    assert lines[2:] == want


def test_figures_in_the_notes_match_the_report():
    due, _, staff, rows = rebuilt()
    notes = ' '.join(section().split())
    fixed, planner, rival = rows['The playbook in fixed order'], rows["Tilden's planner"], rows['Fixed rules, separate author']
    assert f"{fig(fixed['due'])} against {fig(planner['due'])} of {due}" in notes
    assert f"did so {fig(rival['early'])} times" in notes
    assert f"{fig(planner['handoff'])} of {staff}, against {fig(rival['handoff'])} for the rules" in notes
    assert f"with {staff} such cases" in notes
    cost = re.search(r'about (\d+(?:\.\d+)?) cents for the planner and about (\d+(?:\.\d+)?) cents for the fixed order', notes)
    assert cost and cost.groups() == (f"{planner['cents']:.0f}", fig(fixed['cents']))
