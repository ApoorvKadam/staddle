import json
from pathlib import Path

import pytest

from core.tilden import replay


def test_parse_rejects_unknown_date():
    text = '''U1.
UNIT: A101 5x10
E1: someday, MOVE-OUT, Pat: "Left"
RENTABLE AFTER: E1
STAFF DECIDE: none
MUST NOT: none
WHY: Empty.
'''
    with pytest.raises(ValueError, match='date'):
        replay.parse(text)

from core import walk
from core.tilden import cases, tools


def idle(store, stream, now):
    pass


def toy(store, stream, now):
    events = store.stream_events(stream)
    source = next(e for e in events if e.kind == 'lease.ended')
    unit = next(iter(store.books(stream).units))
    case = cases.open_case(store, stream, source, unit, now)
    notes = [e for e in events if e.kind == 'walk.observed']
    if any('disputes' in e.payload['note'] for e in notes) and case.state != 'needs staff':
        tools.act('hand_to_staff', store, stream, case, now, question='Review dispute', record_ids=[], role='front_desk', wake_evidence='staff note')


def practice():
    return replay.parse(Path('tests/fixtures/unit_ready_practice.txt').read_text())


def test_rows_record_worker_actions_for_each_entry(monkeypatch):
    from core.tilden import replay_run

    case = replay.parse('''U1.
UNIT: A101 5x10
E1: Mar 3, 9:00 AM, MOVE-OUT, Pat: "Left."
E2: Mar 3, 10:00 AM, NOTE, Pat: "Door sticks."
RENTABLE AFTER: never in this record
STAFF DECIDE: E2: Review the door.
MUST NOT: Ignore the door.
WHY: It needs attention.
''')[0]

    def worker(store, stream, now):
        events = store.stream_events(stream)
        notes = [e for e in events if e.kind == 'walk.observed']
        if not notes:
            return
        source = next(e for e in events if e.kind == 'lease.ended')
        current = cases.open_case(store, stream, source, case.unit, now)
        record = notes[-1].payload['observation']
        tools.act('open_work_order', store, stream, current, now, text='Fix the door', answers_record=record)
        tools.act('hand_to_staff', store, stream, current, now, question='Who should fix it?',
                  record_ids=[record], role='front_desk', wake_evidence='staff note')

    monkeypatch.setattr(replay_run, 'arm_for', lambda *args: worker)
    row = replay_run.run_arm('fake', [case])[0]
    assert row['trace'][0][4] == []
    order, handoff = row['trace'][1][4]
    assert order == {'action': 'open_work_order', 'order': 'WO0001', 'text': 'Fix the door', 'answers_record': 'O0001'}
    assert handoff == {'action': 'hand_to_staff', 'question': 'Who should fix it?', 'record_ids': ['O0001'],
                       'role': 'front_desk', 'wake_evidence': 'staff note'}


def test_action_trace_keeps_the_details_of_other_worker_actions():
    from core.store import Event

    def event(kind, payload, source='agent:tilden'):
        return Event(kind, None, payload, 'key', source=source)

    events = [event('work_order.reported', {'order': 'I1', 'problem': 'Inspect', 'kind': 'check', 'answers_record': 'W1'}),
              event('work_order.closed', {'order': 'W1', 'text': 'Already clean', 'no_work': True}),
              event('work_order.joined', {'order': 'W2', 'into': 'W1'}),
              event('unit.blocked', {'block': 'B1', 'reason': 'Await repair'}),
              event('unit.unblocked', {'block': 'B1'}),
              event('work_order.closed', {'order': 'W3', 'text': 'Staff closed', 'no_work': True}, 'staff:manager')]
    assert replay._actions(events) == [
        {'action': 'request_inspection', 'order': 'I1', 'text': 'Inspect', 'answers_record': 'W1'},
        {'action': 'close_report_no_work', 'order': 'W1', 'text': 'Already clean'},
        {'action': 'link_reports', 'order': 'W2', 'into': 'W1'},
        {'action': 'hold_unit', 'block': 'B1', 'reason': 'Await repair'},
        {'action': 'release_unit', 'block': 'B1'},
    ]


def test_practice_cases_replay_and_score_both_arms():
    rows = practice()
    assert len(rows) == 8
    results = [replay.score(case, replay.replay(case, idle, {})) for case in rows]
    assert [r.released_at for r in results] == ['E1'] * 8
    assert [r.release_right for r in results] == [False] * 8
    assert [r.released_early for r in results] == [True] * 8
    assert [r.missed_handoff for r in results] == [False, False, True, False, True, True, True, False]
    assert replay.totals(results)['released_early'] == 8
    toy_results = [replay.score(case, replay.replay(case, toy, {})) for case in rows]
    assert toy_results[5].handed_at == 'E2'
    assert toy_results[5].handoff_right
    assert toy_results[5].must_not == ['Charge the former tenant.']


def test_note_work_and_inspection_use_existing_orders_and_make_missing_ones():
    text = '''U9.
UNIT: X1 5x10
E1: Mar 3, 9:00 AM, MOVE-OUT, Pat: "Left."
E2: Mar 3, 10:00 AM, NOTE, Pat: "Clean floor."
E3: Mar 3, 11:00 AM, WORK DONE, Lee, answers E2: "Swept."
E4: Mar 3, 12:00 PM, NOTE, Pat: "Repair latch."
E5: Mar 3, 1:00 PM, WORK DONE, Lee, answers E4: "Fixed."
E6: Mar 3, 2:00 PM, INSPECTION, Pat: "Works."
E7: Mar 3, 3:00 PM, INSPECTION, Pat: "Still works."
RENTABLE AFTER: E1
STAFF DECIDE: none
MUST NOT: none
WHY: Practice.
'''
    case = replay.parse(text)[0]
    snapshots = []
    def worker(store, stream, now):
        source = next(e for e in store.stream_events(stream) if e.kind == 'lease.ended')
        current = cases.open_case(store, stream, source, 'X1', now)
        hour = now.hour
        if hour == 12:
            tools.act('open_work_order', store, stream, current, now, text='Repair latch', answers_record=next(e.payload['observation'] for e in reversed(store.stream_events(stream)) if e.kind == 'walk.observed'))
        if hour == 13:
            tools.act('request_inspection', store, stream, current, now)
        snapshots.append([(o.problem, o.kind, o.state, [c.text for c in o.changes])
                          for o in walk.load(store, stream)[1].orders.values()])
    replay.replay(case, worker, {})
    assert snapshots[2][0][0:3] == ('Clean floor.', None, 'closed')
    assert snapshots[4][1][0:3] == ('Repair latch', None, 'closed')
    assert snapshots[5][2][1:3] == ('check', 'closed')
    assert 'Works.' in snapshots[5][2][3]
    assert snapshots[6][3][0:3] == ('Still works.', 'check', 'closed')


def test_score_distinguishes_late_and_needless_handoff():
    case = practice()[0]
    late = replay.score(case, [replay.Step('E1', False, False), replay.Step('E2', False, True)])
    assert late.released_late_or_missed and late.needless_handoff
    assert not late.release_right and not late.released_early


def test_joined_reports_finish_the_surviving_order():
    case = practice()[3]
    case.entries[3].answers = 'E3'
    captured = []
    def joiner(store, stream, now):
        orders = list(walk.load(store, stream)[1].orders.values())
        if len(orders) == 2 and orders[1].state != 'closed':
            source = next(e for e in store.stream_events(stream) if e.kind == 'lease.ended')
            current = cases.open_case(store, stream, source, case.unit, now)
            assert tools.act('link_reports', store, stream, current, now, order=orders[1].id, into=orders[0].id).allowed
        captured.append(walk.load(store, stream)[1])
    replay.replay(case, joiner, {})
    orders = list(captured[3].orders.values())
    assert orders[0].state == 'closed' and orders[1].joined_into == orders[0].id


def test_wrapped_words_and_entry_authors_are_preserved():
    case = replay.parse('''U10.
UNIT: Z1 5x10
E1: Mar 3, 9:00 AM, MOVE-OUT, Casey: "Lease
ended."
E2: Mar 3, 10:00 AM, NOTE, Robin: "Floor needs
cleaning."
RENTABLE AFTER: E1
STAFF DECIDE: none
MUST NOT: Do not charge.
  Do not discard belongings.
WHY: A practice
case.
''')[0]
    observed = []
    def reader(store, stream, now):
        observed.extend(e.payload for e in store.stream_events(stream) if e.kind == 'replay.entry' and e.payload['entry'] == 'E2')
    replay.replay(case, reader, {})
    assert observed[0]['who'] == 'Robin' and observed[0]['text'] == 'Floor needs cleaning.'
    assert case.must_not == ['Do not charge.', 'Do not discard belongings.']
    assert case.why == 'A practice case.'


def test_handing_every_unit_to_staff_at_move_out_is_premature_not_right():
    from core.tilden.replay import Step, parse, score
    cases = [c for c in parse(open("tests/fixtures/unit_ready_practice.txt").read()) if c.staff_decide and c.staff_decide != c.entries[0].id]
    assert cases
    for case in cases:
        trace = [Step(e.id, False, True) for e in case.entries]
        result = score(case, trace)
        assert result.premature_handoff and not result.handoff_right and not result.missed_handoff, case.id


def test_the_graded_runner_scores_every_worker_and_keeps_crashes(tmp_path, monkeypatch):
    from core.tilden import replay_run
    calls = []
    def broken(store, stream, now):
        raise RuntimeError('boom')
    monkeypatch.setattr(replay_run, 'arm_for', lambda name, client=None: broken if name == 'broken' else (lambda s, st, n: calls.append(n)))
    out = tmp_path / 'r.json'
    replay_run.main(['--set', 'tests/fixtures/unit_ready_practice.txt', '--arms', 'fms,broken', '--out', str(out)])
    report = json.loads(out.read_text())
    assert report['cases'] == 8 and len(report['set_sha256']) == 64
    assert report['arms']['fms']['errors'] == 0 and report['arms']['fms']['totals']['released_early'] == 8
    assert report['arms']['broken']['errors'] == 8 and calls


def test_practice_first_unit_needs_a_look():
    assert practice()[0].rentable_after == 'E2'


def test_replay_finishes_only_work_answering_named_note():
    case = replay.parse('''U9.
UNIT: X1 5x10
E1: Mar 3, 9:00 AM, MOVE-OUT, Pat: "Left."
E2: Mar 3, 10:00 AM, NOTE, Pat: "Broken latch."
E3: Mar 3, 11:00 AM, REPORT, Pat: "Roof leak."
E4: Mar 3, 12:00 PM, WORK DONE, Lee, answers E2: "Fixed latch."
RENTABLE AFTER: E4
STAFF DECIDE: none
MUST NOT: none
WHY: Practice.
''')[0]
    snapshots = []
    replay.replay(case, lambda s, st, n: snapshots.append(list(walk.load(s, st)[1].orders.values())))
    assert [(o.problem, o.state) for o in snapshots[-1]] == [('Roof leak.', 'reported'), ('Broken latch.', 'closed')]


def test_replay_failure_keeps_costs_and_is_not_scored(monkeypatch):
    from core.tilden import replay_run, runtime
    from core.store import Event
    def broken(store, stream, now):
        with store.write():
            store.append(stream, [Event('tilden.planner.call', now, {'cost_cents': 7}, 'cost')])
        raise RuntimeError('boom')
    monkeypatch.setattr(replay_run, 'arm_for', lambda *a: broken)
    row = replay_run.run_arm('broken', practice()[:1])[0]
    assert row['error'] == 'RuntimeError' and row['cents'] == 7
    def swallowed(store, stream, now):
        source = next(e for e in store.stream_events(stream) if e.kind == 'lease.ended')
        case = cases.open_case(store, stream, source, 'A101', now)
        runtime._staff(store, stream, case, now, 'The case stopped on an error (RuntimeError); staff review it.')
    monkeypatch.setattr(replay_run, 'arm_for', lambda *a: swallowed)
    row = replay_run.run_arm('broken', practice()[:1])[0]
    assert 'error' in row and 'handoff_right' not in row


def test_inspection_does_not_close_unrelated_check():
    case = replay.parse('''U9.
UNIT: X1 5x10
E1: Mar 3, 9:00 AM, MOVE-OUT, Pat: "Left."
E2: Mar 3, 10:00 AM, REPORT, Pat: "Roof leak."
E3: Mar 3, 11:00 AM, REPORT, Pat: "Door broken."
E4: Mar 3, 12:00 PM, WORK DONE, Lee, answers E3: "Door fixed."
E5: Mar 3, 1:00 PM, INSPECTION, Pat, answers E4: "Door works."
RENTABLE AFTER: E5
STAFF DECIDE: none
MUST NOT: none
WHY: Practice.
''')[0]
    snapshots = []
    def worker(store, stream, now):
        source = next(e for e in store.stream_events(stream) if e.kind == 'lease.ended')
        current = cases.open_case(store, stream, source, 'X1', now)
        if now == case.entries[1].at:
            tools.act('request_inspection', store, stream, current, now)
        snapshots.append(list(walk.load(store, stream)[1].orders.values()))
    replay.replay(case, worker)
    checks = [o for o in snapshots[-1] if o.kind == 'check']
    # the worker's plain request asked for a look at the unit; staff's look answers it, and the open roof order still blocks the unit
    assert [o.state for o in checks] == ['closed']
    assert any(o.kind != 'check' and o.state != 'closed' and 'roof' in o.problem.lower() for o in snapshots[-1])


def test_inspection_closes_check_with_matching_evidence():
    case = replay.parse('''U9.
UNIT: X1 5x10
E1: Mar 3, 9:00 AM, MOVE-OUT, Pat: "Left."
E2: Mar 3, 10:00 AM, REPORT, Pat: "Roof leak."
E3: Mar 3, 11:00 AM, REPORT, Pat: "Door broken."
E4: Mar 3, 12:00 PM, WORK DONE, Lee, answers E3: "Door fixed."
E5: Mar 3, 1:00 PM, INSPECTION, Pat, answers E4: "Door works."
RENTABLE AFTER: E5
STAFF DECIDE: none
MUST NOT: none
WHY: Practice.
''')[0]
    snapshots = []
    def worker(store, stream, now):
        source = next(e for e in store.stream_events(stream) if e.kind == 'lease.ended')
        current = cases.open_case(store, stream, source, 'X1', now)
        orders = list(walk.load(store, stream)[1].orders.values())
        if now == case.entries[2].at:
            for order in orders:
                tools.act('request_inspection', store, stream, current, now, answers_record=order.id)
        snapshots.append(list(walk.load(store, stream)[1].orders.values()))
    replay.replay(case, worker)
    assert [o.state for o in snapshots[-1] if o.kind == 'check'] == ['reported', 'closed']


def test_staff_fallback_from_exception_is_an_error(monkeypatch):
    from core.tilden import replay_run, runtime
    def swallowed(store, stream, now):
        source = next(e for e in store.stream_events(stream) if e.kind == 'lease.ended')
        case = cases.open_case(store, stream, source, 'A101', now)
        runtime._staff(store, stream, case, now, 'The case stopped on an error (RuntimeError); staff review it.')
    monkeypatch.setattr(replay_run, 'arm_for', lambda *a: swallowed)
    row = replay_run.run_arm('broken', practice()[:1])[0]
    assert 'error' in row and 'handoff_right' not in row


MINI = """U1.
UNIT: A101 10x10
E1: 2026-03-03 09:00, MOVE-OUT, Pat: "Lease ended."
E2: 2026-03-03 10:00, NOTE, Pat: "Swept, empty, door fine."
E3: 2026-03-04 09:00, INSPECTION, Lee: "Light fixture is broken."
E4: 2026-03-04 12:00, WORK DONE, Sam, answers E3: "Replaced the light fixture."
RENTABLE AFTER: never in this record
STAFF DECIDE: none
MUST NOT: none
WHY: practice.
"""


def _asking_arm():
    from core.store import Event
    from core.tilden import cases, tools

    def arm(store, stream, now):
        if not cases.load(store, stream):
            with store.write():
                src = store.append(stream, [Event('test.trigger', now, {}, 'trigger')])[0]
            case = cases.open_case(store, stream, src, 'A101', now)
            tools.act('request_inspection', store, stream, case, now)
    return arm


def test_a_note_closes_a_plain_inspection_request_and_work_can_answer_an_inspection():
    from core import walk
    from core.tilden.replay import parse, replay
    case = parse(MINI)[0]
    seen = {}
    arm = _asking_arm()

    def wrapped(store, stream, now):
        seen['s'] = (store, stream)
        arm(store, stream, now)
    replay(case, wrapped)
    store, stream = seen['s']
    orders = walk.load(store, stream)[1].orders.values()
    checks = [o for o in orders if o.kind == 'check']
    assert checks and all(o.state == 'closed' for o in checks), [(o.id, o.state) for o in checks]
    repairs = [o for o in orders if o.kind != 'check']
    assert len(repairs) == 1 and repairs[0].state == 'closed' and 'light' in repairs[0].problem.lower()


def test_an_intro_line_is_skipped_and_a_repeated_case_keeps_the_last_version():
    from core.tilden.replay import parse
    first = MINI.replace('Swept, empty, door fine.', 'First draft.')
    text = 'Here are the cases.\n\n' + first + '\n' + MINI
    cases = parse(text)
    assert len(cases) == 1 and cases[0].entries[1].text == 'Swept, empty, door fine.'


def test_paused_cases_are_counted_apart_from_handoffs():
    case = practice()[0]
    result = replay.score(case, [replay.Step('E1', False, False, True)])
    assert result.paused and result.handed_at is None and not result.needless_handoff
    assert replay.totals([result])['paused'] == 1


def test_runner_defaults_include_hybrid(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from core.tilden import replay_run

    names = []
    monkeypatch.setitem(sys.modules, 'anthropic', SimpleNamespace(Anthropic=lambda: object()))
    monkeypatch.setattr(replay_run, 'run_arm', lambda name, parsed, client: names.append(name) or [])
    replay_run.main(['--set', 'tests/fixtures/unit_ready_practice.txt', '--out', str(tmp_path / 'report.json')])
    assert names == ['fms', 'rival', 'fixed', 'planner', 'hybrid']
    called = []
    monkeypatch.setattr(replay_run.runtime, 'sweep', lambda *args, **kwargs: called.append(kwargs))
    replay_run.arm_for('hybrid', client='fake')('store', 'stream', 'now')
    assert called == [{'chooser': 'hybrid', 'client': 'fake'}]


def test_split_release_scores_and_post_handoff_recovery():
    text = '''U1.
UNIT: A101 10x10
E1: 2026-01-01 09:00, MOVE-OUT, staff: "Moved out"
E2: 2026-01-01 10:00, NOTE, staff: "Staff decide"
E3: 2026-01-01 11:00, NOTE, staff: "Ready"
RENTABLE AFTER: E3
STAFF DECIDE: E2: Decide
MUST NOT: none
WHY: Ready after staff decide
'''
    case = replay.parse(text)[0]
    recovered = replay.score(case, [replay.Step('E1', False, False), replay.Step('E2', False, True), replay.Step('E3', True, False)])
    never = replay.parse(text.replace('RENTABLE AFTER: E3', 'RENTABLE AFTER: never in this record').replace('STAFF DECIDE: E2: Decide', 'STAFF DECIDE: none'))[0]
    held = replay.score(never, [replay.Step('E1', False, False)])
    counts = replay.totals([recovered, held])
    assert counts['released_on_due_entry'] == {'right': 1, 'of': 1}
    assert counts['kept_off_when_never_offered'] == {'right': 1, 'of': 1}
    assert counts['post_handoff_recovery'] == {'right': 1, 'of': 1}


def test_release_without_a_handoff_is_not_post_handoff_recovery():
    text = '''U1.
UNIT: A101 10x10
E1: 2026-01-01 09:00, MOVE-OUT, staff: "Moved out"
E2: 2026-01-01 10:00, NOTE, staff: "Staff decide"
E3: 2026-01-01 11:00, NOTE, staff: "Ready"
RENTABLE AFTER: E3
STAFF DECIDE: E2: Decide
MUST NOT: none
WHY: Ready after staff decide
'''
    case = replay.parse(text)[0]
    never_handed = replay.score(case, [replay.Step('E1', False, False), replay.Step('E2', False, False), replay.Step('E3', True, False)])
    assert never_handed.released_on_due_entry is True and never_handed.post_handoff_recovery is False
    assert replay.totals([never_handed])['post_handoff_recovery'] == {'right': 0, 'of': 1}
    # a handoff at the deciding entry and then the release is recovery, and so is one that came a little early
    right = replay.score(case, [replay.Step('E1', False, False), replay.Step('E2', False, True), replay.Step('E3', True, False)])
    early = replay.score(case, [replay.Step('E1', False, True), replay.Step('E2', False, False), replay.Step('E3', True, False)])
    assert right.post_handoff_recovery is True and early.post_handoff_recovery is True
    # a handoff that comes only with or after the release is not a recovery from it
    after = replay.score(case, [replay.Step('E1', False, False), replay.Step('E2', False, False), replay.Step('E3', True, True)])
    assert after.post_handoff_recovery is False
