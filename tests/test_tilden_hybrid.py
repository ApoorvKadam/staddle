from datetime import timedelta

from core import walk
from core.store import Event
from core.tilden import cases, gate, planner, runtime
from tests.test_core_walk import FRONT
from tests.test_tilden_tools import AT, STREAM, allow, setup_case
from tests.test_tilden_planner import Client, block, check, note, reader, response


def permissions(store):
    with store.write():
        store.append(STREAM, [Event('lease.signed', AT - timedelta(days=2), {'lease': 'L9', 'tenant': 'T9', 'unit': 'A102', 'rent_cents': 100, 'start': '2026-01-01', 'schedule': []}, 'sign-hybrid'),
            Event('lease.ended', AT - timedelta(days=1), {'lease': 'L9', 'unit': 'A102', 'on': '2026-11-12'}, 'end-hybrid')])
    for action in ('hold_unit', 'release_unit', 'open_work_order', 'request_inspection', 'link_reports'):
        allow(store, action)


def hybrid(store, case, client, now=AT):
    return runtime.run_case(store, STREAM, case.id, now, chooser='hybrid', client=client,
                            readers=reader(), check=checks)


def checks(*args):
    question = args[4]
    # these cases hand over matters that do not stop a rental, so the blocking check answers low
    return check(0.01 if 'remove or dispose' in question or 'settled before the unit is offered' in question else 0.99)(*args)


def test_clean_unit_releases_without_a_model_call():
    store, case = setup_case()
    permissions(store)
    with store.write():
        store.append(STREAM, [Event('work_order.reported', AT, {'order': 'I1', 'unit': case.unit, 'problem': 'Look', 'kind': 'check', 'by': 'agent:tilden'}, 'look'),
            Event('work_order.closed', AT + timedelta(minutes=1), {'order': 'I1', 'by': 'staff:manager', 'text': 'Clean'}, 'look-done')])
    # The look was already judged on an earlier wake.
    close = next(e for e in store.events(STREAM) if e.kind == 'work_order.closed')
    cases.record(store, STREAM, case.id, 'updated', {'steps': [{'judged_event': close.id}]}, AT)
    client = Client()
    assert hybrid(store, case, client, AT + timedelta(minutes=1)).state == 'done'
    assert not client.calls


def test_repair_then_inspection_and_release():
    store, case = setup_case()
    permissions(store)
    with store.write():
        store.append(STREAM, [Event('walk.started', AT, {'walk': 'W1', 'route': [case.unit], 'by': 'staff:manager'}, 'walk-start'),
            Event('walk.observed', AT, {'walk': 'W1', 'observation': 'O1', 'unit': case.unit, 'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'unknown', 'note': 'The door latch is broken.', 'by': 'staff:manager', 'supersedes': None}, 'walk-note')])
    client = Client(response(block('open_work_order', 'a', text='Fix the door latch', answers_record='O1')),
                    response(stop='end_turn'))
    assert hybrid(store, case, client).state == 'waiting'
    order = next(e.payload['order'] for e in store.events(STREAM) if e.kind == 'work_order.reported')
    later = AT + timedelta(minutes=1)
    with store.write():
        store.append(STREAM, [Event('work_order.closed', later, {'order': order, 'by': 'staff:manager', 'text': 'Latch repaired.'}, 'closed-repair')])
    client = Client(response(stop='end_turn'))
    runtime.sweep(store, STREAM, later, chooser='hybrid', client=client, readers=reader(), check=checks)
    inspection = next(e.payload['order'] for e in store.events(STREAM) if e.kind == 'work_order.reported' and e.payload.get('kind') == 'check')
    later += timedelta(minutes=1)
    with store.write():
        store.append(STREAM, [Event('work_order.closed', later, {'order': inspection, 'by': 'staff:manager', 'text': 'Latch works, unit ready.'}, 'closed-look')])
    client = Client(response(stop='end_turn'))
    runtime.sweep(store, STREAM, later, chooser='hybrid', client=client, readers=reader(), check=checks)
    assert cases.load(store, STREAM)[case.id].state == 'done'
    assert len(client.calls) == 1
    names = {t['name'] for t in client.calls[0]['tools']}
    assert not names.intersection({'hold_unit', 'release_unit', 'link_reports', 'request_inspection'})


def test_handoff_resumes_with_new_staff_note():
    store, case = setup_case()
    permissions(store)
    note(store, case, 'Former tenant left a chair.')
    client = Client(response(block('hand_to_staff', 'a', question='What happens to the chair?', record_ids=['O1'], role='front_desk', wake_evidence='staff note')))
    assert hybrid(store, case, client).state == 'needs staff'
    assert not any(e.kind == 'unit.unblocked' for e in store.events(STREAM))
    later = AT + timedelta(minutes=1)
    with store.write():
        store.append(STREAM, [Event('walk.observed', later, {'walk': 'W1', 'observation': 'O2', 'unit': case.unit, 'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'unknown', 'note': 'Chair collected, unit clear and ready.', 'by': 'staff:manager', 'supersedes': None}, 'settled')])
    client = Client(response(stop='end_turn'))
    runtime.sweep(store, STREAM, later, chooser='hybrid', client=client, readers=reader(), check=checks)
    assert len(client.calls) == 1       # the new staff record is judged once by the model; code then releases on the ready test
    assert cases.load(store, STREAM)[case.id].state == 'done'


def test_one_model_turn_per_staff_record_with_no_shared_budget():
    store, case = setup_case()
    permissions(store)
    note(store, case, 'A latch needs attention.')
    client = Client(*(response(block('lease', str(i))) for i in range(3)))
    result = hybrid(store, case, client)
    assert result.state == 'waiting' and len(client.calls) == 1
    assert not any(s.get('handoff') for s in result.steps)


def test_a_wake_judges_every_record_without_a_shared_decision_budget():
    store, case = setup_case()
    permissions(store)
    with store.write():
        store.append(STREAM, [Event('walk.started', AT, {'walk': 'W', 'route': [case.unit], 'by': 'staff:manager'}, 'many-walk')])
        for i in range(5):
            store.append(STREAM, [Event('walk.observed', AT, {'walk': 'W', 'observation': f'O{i}', 'unit': case.unit, 'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'unknown', 'supersedes': None, 'note': f'Note {i}', 'by': 'staff:manager'}, f'many-{i}')])
    client = Client(*(response(stop='end_turn') for _ in range(5)))
    result = hybrid(store, case, client)
    assert len(client.calls) == 5 and result.state != 'paused'
    assert len([s for s in result.steps if s.get('judged_event')]) == 5


def test_a_record_gets_at_most_two_tool_calls():
    store, case = setup_case()
    permissions(store)
    note(store, case, 'A latch needs attention.')
    ran = []

    def execute(action, args):
        ran.append(action)
        return 'ok', cases.load(store, STREAM)[case.id], False
    client = Client(response(*(block('lease', str(i)) for i in range(9))))
    planner.run(store, STREAM, case, AT, client, {}, execute, lambda c, stop=False, reserve=0: False,
                record={'id': 'O1', 'event': 1, 'kind': 'walk.observed', 'words': 'A latch needs attention.'})
    assert ran.count('lease') == 2 and len(client.calls) == 1
    # through the case: one turn, nothing handed over, and nothing released by the model
    store, case = setup_case()
    permissions(store)
    note(store, case, 'A latch needs attention.')
    result = hybrid(store, case, Client(response(*(block('release_unit', str(i)) for i in range(9)))))
    assert result.state != 'paused' and not any(s.get('handoff') for s in result.steps)
    assert not any(e.kind == 'unit.unblocked' for e in store.events(STREAM))


def test_code_links_two_matching_open_reports():
    store, case = setup_case()
    permissions(store)
    with store.write():
        store.append(STREAM, [Event('work_order.reported', AT, {'order': 'R1', 'unit': case.unit, 'problem': 'Latch broken', 'by': 'staff:manager'}, 'report-1'),
            Event('work_order.reported', AT, {'order': 'R2', 'unit': case.unit, 'problem': 'Broken latch', 'by': 'staff:manager'}, 'report-2')])
    client = Client(response(stop='end_turn'), response(stop='end_turn'))
    assert hybrid(store, case, client).state == 'waiting'
    assert any(e.kind == 'work_order.joined' for e in store.events(STREAM))


def test_corrected_note_settles_staff_note_handoff():
    store, case = setup_case()
    permissions(store)
    note(store, case, 'Former tenant left a chair.')
    hybrid(store, case, Client(response(block('hand_to_staff', 'a', question='What happens to the chair?', record_ids=['O1'], role='front_desk', wake_evidence='staff note'))))
    later = AT + timedelta(minutes=1)
    # staff file a correction the way the walk screen does: a new observation that supersedes the old one
    fix = walk.run(store, STREAM, walk.Act('fix-chair', 'correct_observation', FRONT, about='O1',
                                          form={'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'unknown'},
                                          text='Chair collected, unit clear and ready.'), later)
    assert fix.allowed
    correction = next(e for e in store.events(STREAM) if e.command == 'fix-chair')
    assert correction.kind == 'walk.observed' and correction.payload['supersedes'] == 'O1'
    assert walk.load(store, STREAM)[1].observations['O1'].superseded_by == correction.payload['observation']
    asked = []

    def recording(*args):
        asked.append((args[4], args[5]))
        return checks(*args)
    runtime.sweep(store, STREAM, later, chooser='hybrid', client=Client(response(stop='end_turn')), readers=reader(), check=recording)
    assert any('settle the question' in question and 'Chair collected, unit clear and ready.' in payload for question, payload in asked)
    assert any(s.get('settled_question') == 'What happens to the chair?' for s in cases.load(store, STREAM)[case.id].steps)


def test_handoff_settlement_survives_daily_budget_pause():
    store, case = setup_case()
    permissions(store)
    note(store, case, 'Former tenant left a chair.')
    hybrid(store, case, Client(response(block('hand_to_staff', 'a', question='What happens to the chair?', record_ids=['O1'], role='front_desk', wake_evidence='staff note'))))
    later = AT + timedelta(minutes=1)
    with store.write():
        store.append(STREAM, [Event('walk.observed', later, {'walk': 'W1', 'observation': 'O2', 'unit': case.unit, 'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'unknown', 'note': 'Chair collected.', 'by': 'staff:manager', 'supersedes': None}, 'settled-budget'),
            Event('tilden.decision', later, {'case': 'other', 'cost_cents': runtime.DAILY_CENTS}, 'daily-budget')])
    runtime.sweep(store, STREAM, later, chooser='hybrid', client=Client(), readers=reader(), check=checks)
    assert cases.load(store, STREAM)[case.id].state == 'paused'
    runtime.sweep(store, STREAM, AT + timedelta(days=1, minutes=1), chooser='hybrid', client=Client(response(stop='end_turn')), readers=reader(), check=checks)
    assert any(s.get('settled_question') == 'What happens to the chair?' for s in cases.load(store, STREAM)[case.id].steps)


def test_hybrid_ignores_evidence_before_case_source():
    from tests.test_core_walk import tiny
    store = tiny()
    with store.write():
        store.append(STREAM, [Event('walk.started', AT - timedelta(days=2), {'walk': 'W', 'route': ['A102'], 'by': 'staff:manager'}, 'old-walk')])
        for i in range(3):
            store.append(STREAM, [Event('walk.observed', AT - timedelta(days=2), {'walk': 'W', 'observation': f'old{i}', 'unit': 'A102', 'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'unknown', 'supersedes': None, 'note': 'Old note', 'by': 'staff:manager'}, f'old{i}')])
        source = store.append(STREAM, [Event('test.source', AT, {}, 'new-source')])[0]
    case = cases.open_case(store, STREAM, source, 'A102', AT)
    permissions(store)
    note(store, case, 'Current note')
    client = Client(response(stop='end_turn'))
    result = hybrid(store, case, client)
    assert result.state != 'paused'
    assert len(client.calls) == 1
    assert 'Current note' in str(client.calls[0]['messages'])


def test_three_matching_reports_are_linked():
    store, case = setup_case()
    permissions(store)
    with store.write():
        for i in range(3):
            store.append(STREAM, [Event('work_order.reported', AT, {'order': f'R{i}', 'unit': case.unit, 'problem': 'Latch broken', 'by': 'staff:manager'}, f'report-{i}')])
    hybrid(store, case, Client(*(response(stop='end_turn') for _ in range(3))))
    assert len([e for e in store.events(STREAM) if e.kind == 'work_order.joined']) == 2


def test_unjudged_notes_resume_after_spending_pause():
    store, case = setup_case()
    permissions(store)
    with store.write():
        store.append(STREAM, [Event('walk.started', AT, {'walk': 'W', 'route': [case.unit], 'by': 'staff:manager'}, 'notes-walk')])
        for i in range(4):
            store.append(STREAM, [Event('walk.observed', AT, {'walk': 'W', 'observation': f'O{i}', 'unit': case.unit, 'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'unknown', 'supersedes': None, 'note': f'Note {i}', 'by': 'staff:manager'}, f'note-{i}')])
        store.append(STREAM, [Event('tilden.decision', AT, {'case': 'other', 'cost_cents': runtime.DAILY_CENTS}, 'notes-spend')])
    first = hybrid(store, case, Client())
    assert first.state == 'paused'
    assert not any(e.kind == 'unit.unblocked' for e in store.events(STREAM))
    client = Client(*(response(stop='end_turn') for _ in range(4)))
    runtime.sweep(store, STREAM, AT + timedelta(days=1), chooser='hybrid', client=client, readers=reader(), check=checks)
    assert len(client.calls) == 4
    assert 'Note 3' in str(client.calls[3]['messages'])


def test_paused_case_keeps_its_pending_evidence_while_the_budget_is_still_spent():
    store, case = setup_case()
    permissions(store)
    with store.write():
        store.append(STREAM, [Event('walk.started', AT, {'walk': 'W', 'route': [case.unit], 'by': 'staff:manager'}, 'keep-walk')])
        for i in range(2):
            store.append(STREAM, [Event('walk.observed', AT, {'walk': 'W', 'observation': f'O{i}', 'unit': case.unit, 'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'unknown', 'supersedes': None, 'note': f'Note {i}', 'by': 'staff:manager'}, f'keep-{i}')])
        store.append(STREAM, [Event('tilden.decision', AT, {'case': 'other', 'cost_cents': runtime.DAILY_CENTS}, 'keep-spend')])
    first = hybrid(store, case, Client())
    assert first.state == 'paused' and first.steps[-1]['pending_evidence']
    pending, entries, history = first.steps[-1]['pending_evidence'], len(first.steps), len(first.history)
    # the same day, with the budget still spent, a sweep must not reopen the saved wake or write another pause entry
    for minutes in (1, 2):
        runtime.sweep(store, STREAM, AT + timedelta(minutes=minutes), chooser='hybrid', client=Client(), readers=reader(), check=checks)
    kept = cases.load(store, STREAM)[case.id]
    assert kept.state == 'paused' and len(kept.steps) == entries and len(kept.history) == history
    assert kept.steps[-1]['pending_evidence'] == pending
    # the first sweep with budget uses the evidence it kept
    client = Client(*(response(stop='end_turn') for _ in range(2)))
    runtime.sweep(store, STREAM, AT + timedelta(hours=10), chooser='hybrid', client=client, readers=reader(), check=checks)
    assert len(client.calls) == 2 and 'Note 1' in str(client.calls[1]['messages'])


def test_source_observation_is_judged():
    from tests.test_core_walk import tiny
    store = tiny()
    with store.write():
        store.append(STREAM, [Event('walk.started', AT, {'walk': 'W', 'route': ['A102'], 'by': 'staff:manager'}, 'source-walk')])
        source = store.append(STREAM, [Event('walk.observed', AT, {'walk': 'W', 'observation': 'source-note', 'unit': 'A102', 'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'unknown', 'supersedes': None, 'note': 'Source note', 'by': 'staff:manager'}, 'source-observation')])[0]
    case = cases.open_case(store, STREAM, source, 'A102', AT)
    permissions(store)
    client = Client(response(stop='end_turn'))
    hybrid(store, case, client)
    assert len(client.calls) == 1
    assert any(s.get('judged_event') == source.id for s in cases.load(store, STREAM)[case.id].steps)


def test_nonmatching_reports_are_not_linked():
    store, case = setup_case()
    permissions(store)
    with store.write():
        for i in range(3):
            store.append(STREAM, [Event('work_order.reported', AT, {'order': f'R{i}', 'unit': case.unit, 'problem': f'Fault {i}', 'by': 'staff:manager'}, f'report-{i}')])
    runtime.run_case(store, STREAM, case.id, AT, chooser='hybrid', client=Client(*(response(stop='end_turn') for _ in range(3))), readers=reader(), check=check(0.01))
    assert not any(e.kind == 'work_order.joined' for e in store.events(STREAM))


def test_cleanup_handoff_settles_before_work_continues_and_releases():
    store, case = setup_case()
    permissions(store)
    with store.write():
        store.append(STREAM, [Event('walk.started', AT, {'walk': 'W1', 'route': [case.unit], 'by': 'staff:manager'}, 'cleanup-walk'),
            Event('walk.observed', AT, {'walk': 'W1', 'observation': 'O1', 'unit': case.unit, 'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'unknown', 'note': 'Cardboard in the back corner and dust along the wall. Door works, no damage; needs cleanup.', 'by': 'staff:manager', 'supersedes': None}, 'cleanup-note')])
    question = 'Could the cardboard be belongings left by the former tenant?'
    first = Client(response(block('hand_to_staff', 'a', question=question, record_ids=['O1'], role='front_desk', wake_evidence='work order change')))
    # the second check does not call the cleanup note a pass, so the ready test holds the unit while the handoff is open
    def not_yet(*args):
        # nothing staff wrote yet settles the cardboard question, and the cleanup note is not a pass
        unsettled_now = 'settle it' in args[4] and 'Picked up' not in args[5]
        return check(0.1 if unsettled_now or ('fine to rent again' in args[4] and 'needs cleanup' in args[5]) else 0.99)(*args)
    assert runtime.run_case(store, STREAM, case.id, AT, chooser='hybrid', client=first, readers=reader(), check=not_yet).state == 'needs staff'
    later = AT + timedelta(minutes=1)
    with store.write():
        store.append(STREAM, [Event('work_order.reported', later, {'order': 'R1', 'unit': case.unit, 'problem': 'Clean unit', 'by': 'staff:manager'}, 'cleanup'),
            Event('work_order.closed', later, {'order': 'R1', 'by': 'staff:manager', 'text': 'Picked up all the cardboard, swept the edges and mopped the floor.'}, 'cleanup-done')])
    client = Client(response(stop='end_turn'), response(stop='end_turn'))
    runtime.sweep(store, STREAM, later, chooser='hybrid', client=client, readers=reader(), check=checks)
    resumed = cases.load(store, STREAM)[case.id]
    settled = next(s for s in resumed.steps if s.get('settled_question') == question)
    assert settled['p'] == 0.99 and len(settled['evidence_ids']) == 2
    assert all(question in str(call['messages'][0]) and 'settled_question' in str(call['messages'][0]) for call in client.calls)
    assert resumed.state == 'waiting' and len([s for s in resumed.steps if s.get('handoff')]) == 1
    inspection = next(e.payload['order'] for e in store.events(STREAM) if e.kind == 'work_order.reported' and e.payload.get('kind') == 'check')
    later += timedelta(minutes=1)
    with store.write():
        store.append(STREAM, [Event('work_order.closed', later, {'order': inspection, 'by': 'staff:manager', 'text': 'Floor is dry, nothing left behind, no smells or damage, door locks fine. Ready.'}, 'final-look')])
    runtime.sweep(store, STREAM, later, chooser='hybrid', client=Client(response(stop='end_turn')), readers=reader(), check=checks)
    assert cases.load(store, STREAM)[case.id].state == 'done'


def test_code_releases_after_the_model_finishes_a_record_by_waiting():
    store, case = setup_case()
    permissions(store)
    with store.write():
        store.append(STREAM, [Event('work_order.reported', AT, {'order': 'I1', 'unit': case.unit, 'problem': 'Look', 'kind': 'check', 'by': 'agent:tilden'}, 'look'),
            Event('work_order.closed', AT + timedelta(minutes=1), {'order': 'I1', 'by': 'staff:manager', 'text': 'Empty, clean, door fine.'}, 'look-done')])
    # the model reads the new look and simply waits; code must still release the ready unit
    client = Client(response(block('wait_for', 'a', record='work_orders', what='nothing more')))
    result = hybrid(store, case, client, AT + timedelta(minutes=1))
    assert result.state == 'done' and client.calls


def test_later_inspection_closes_report_without_work_and_releases():
    store, case = setup_case()
    permissions(store)
    allow(store, 'close_report_no_work')
    later = AT + timedelta(minutes=1)
    with store.write():
        store.append(STREAM, [Event('work_order.reported', AT, {'order': 'R1', 'unit': case.unit, 'problem': 'Water mark, perhaps still leaking', 'by': 'staff:manager'}, 'water-report'),
            Event('work_order.reported', later, {'order': 'I1', 'unit': case.unit, 'problem': 'Water test', 'kind': 'check', 'by': 'staff:manager'}, 'water-test'),
            Event('work_order.closed', later, {'order': 'I1', 'by': 'staff:manager', 'text': 'No water coming in, wall solid and dry. Old discoloration only. Unit empty and clean.'}, 'water-look')])
    result = hybrid(store, case, Client(response(stop='end_turn'), response(stop='end_turn'), response(stop='end_turn')), later)
    closed = [e for e in store.events(STREAM) if e.kind == 'work_order.closed' and e.payload['order'] == 'R1']
    assert result.state == 'done' and len(closed) == 1, result.steps
    assert 'I1' in closed[0].payload['text'] and 'Old discoloration' in closed[0].payload['text']


def test_low_no_work_check_leaves_report_open():
    store, case = setup_case()
    permissions(store)
    allow(store, 'close_report_no_work')
    later = AT + timedelta(minutes=1)
    with store.write():
        store.append(STREAM, [Event('work_order.reported', AT, {'order': 'R1', 'unit': case.unit, 'problem': 'Water mark', 'by': 'staff:manager'}, 'water-report'),
            Event('work_order.reported', later, {'order': 'I1', 'unit': case.unit, 'problem': 'Look', 'kind': 'check', 'by': 'staff:manager'}, 'look'),
            Event('work_order.closed', later, {'order': 'I1', 'by': 'staff:manager', 'text': 'Perhaps dry'}, 'look-done')])
    runtime.run_case(store, STREAM, case.id, later, chooser='hybrid', client=Client(response(stop='end_turn'), response(stop='end_turn'), response(stop='end_turn')), readers=reader(), check=check(0.1))
    assert not any(e.kind == 'work_order.closed' and e.payload['order'] == 'R1' for e in store.events(STREAM))


def test_fixed_closes_report_from_later_staff_note():
    store, case = setup_case()
    permissions(store)
    allow(store, 'close_report_no_work')
    later = AT + timedelta(minutes=1)
    with store.write():
        store.append(STREAM, [Event('work_order.reported', AT, {'order': 'R1', 'unit': case.unit, 'problem': 'Possible leak', 'by': 'staff:manager'}, 'report-fixed'),
            Event('walk.started', later, {'walk': 'W1', 'route': [case.unit], 'by': 'staff:manager'}, 'start-fixed'),
            Event('walk.observed', later, {'walk': 'W1', 'observation': 'O1', 'unit': case.unit, 'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'closed', 'note': 'Dry wall, old mark only, unit clean.', 'by': 'staff:manager', 'supersedes': None}, 'note-fixed')])
    result = runtime.run_case(store, STREAM, case.id, later, chooser='fixed', readers=reader(), check=checks)
    assert result.state == 'done'
    assert any(e.kind == 'work_order.closed' and e.payload['order'] == 'R1' and 'O1' in e.payload['text'] for e in store.events(STREAM))


def test_unavailable_no_work_check_leaves_report_open():
    store, case = setup_case()
    permissions(store)
    allow(store, 'close_report_no_work')
    later = AT + timedelta(minutes=1)
    with store.write():
        store.append(STREAM, [Event('work_order.reported', AT, {'order': 'R1', 'unit': case.unit, 'problem': 'Possible leak', 'by': 'staff:manager'}, 'report-failed'),
            Event('work_order.reported', later, {'order': 'I1', 'unit': case.unit, 'problem': 'Look', 'kind': 'check', 'by': 'staff:manager'}, 'check-failed'),
            Event('work_order.closed', later, {'order': 'I1', 'by': 'staff:manager', 'text': 'Dry'}, 'look-failed')])
    def unavailable(*args):
        raise RuntimeError('unavailable')
    runtime.run_case(store, STREAM, case.id, later, chooser='fixed', readers=reader(), check=unavailable)
    assert not any(e.kind == 'work_order.closed' and e.payload['order'] == 'R1' for e in store.events(STREAM))


def test_planner_can_close_report_using_later_inspection():
    store, case = setup_case()
    permissions(store)
    allow(store, 'close_report_no_work')
    later = AT + timedelta(minutes=1)
    with store.write():
        store.append(STREAM, [Event('work_order.reported', AT, {'order': 'R1', 'unit': case.unit, 'problem': 'Possible leak', 'by': 'staff:manager'}, 'report-planner'),
            Event('work_order.reported', later, {'order': 'I1', 'unit': case.unit, 'problem': 'Look', 'kind': 'check', 'by': 'staff:manager'}, 'check-planner'),
            Event('work_order.closed', later, {'order': 'I1', 'by': 'staff:manager', 'text': 'Dry, old mark only.'}, 'look-planner')])
    client = Client(response(block('close_report_no_work', 'a', order='R1')), response(stop='end_turn'))
    runtime.run_case(store, STREAM, case.id, later, chooser='planner', client=client, readers=reader(), check=checks)
    assert 'close_report_no_work' in {tool['name'] for tool in client.calls[0]['tools']}
    assert any(e.kind == 'work_order.closed' and e.payload['order'] == 'R1' for e in store.events(STREAM))


def test_an_open_belongings_handoff_waits_for_settlement_after_a_passing_look():
    store, case = setup_case()
    permissions(store)
    note(store, case, 'Former tenant left a chair.')
    client = Client(response(block('hand_to_staff', 'a', question='Who handles the chair?', record_ids=['O1'], role='front_desk', wake_evidence='staff note')))
    assert hybrid(store, case, client).state == 'needs staff'
    later = AT + timedelta(minutes=1)
    with store.write():
        store.append(STREAM, [Event('walk.observed', later, {'walk': 'W1', 'observation': 'O2', 'unit': case.unit, 'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'unknown', 'note': 'Empty and ready.', 'by': 'staff:manager', 'supersedes': None}, 'passing-look')])
    def unsettled(*args):
        return check(0.1 if 'settle the question' in args[4] or 'settle it' in args[4] else 0.99)(*args)
    runtime.sweep(store, STREAM, later, chooser='hybrid', client=Client(response(stop='end_turn')), readers=reader(), check=unsettled)
    result = cases.load(store, STREAM)[case.id]
    # a former tenant's things must be settled by staff before the unit is offered, even when a later look reads well
    assert result.state != 'done'
    assert any('must be settled before the unit is offered' in str(s) for s in result.steps)


def test_spending_cap_blocks_release_but_routine_closes_and_links():
    store, case = setup_case()
    permissions(store)
    allow(store, 'close_report_no_work')
    later = AT + timedelta(minutes=1)
    with store.write():
        store.append(STREAM, [Event('work_order.reported', AT, {'order': 'R1', 'unit': case.unit, 'problem': 'Old mark', 'by': 'staff:manager'}, 'cap-r1'),
            Event('work_order.reported', AT, {'order': 'R2', 'unit': case.unit, 'problem': 'Old mark', 'by': 'staff:manager'}, 'cap-r2'),
            Event('work_order.reported', later, {'order': 'I1', 'unit': case.unit, 'problem': 'Look', 'kind': 'check', 'by': 'agent:tilden'}, 'cap-look'),
            Event('work_order.closed', later, {'order': 'I1', 'by': 'staff:manager', 'text': 'Dry and ready'}, 'cap-done'),
            Event('tilden.decision', later, {'case': 'other', 'cost_cents': runtime.DAILY_CENTS}, 'cap-spend')])
    result = hybrid(store, case, Client(), later)
    assert result.state == 'paused'
    assert any(e.kind == 'work_order.joined' for e in store.events(STREAM))
    assert any(e.kind == 'work_order.closed' and e.payload.get('no_work') for e in store.events(STREAM))
    assert not any(e.kind == 'unit.unblocked' for e in store.events(STREAM))


def test_inspection_report_and_close_with_same_words_are_judged_once():
    store, case = setup_case()
    permissions(store)
    with store.write():
        store.append(STREAM, [Event('work_order.reported', AT, {'order': 'I1', 'unit': case.unit, 'problem': 'Clean and ready', 'kind': 'check', 'by': 'staff:manager'}, 'same-report'),
            Event('work_order.closed', AT, {'order': 'I1', 'by': 'staff:manager', 'text': 'Clean and ready'}, 'same-close')])
    client = Client(response(stop='end_turn'))
    result = hybrid(store, case, client)
    assert result.state == 'done' and len(client.calls) == 1


def _ready_unit_with_a_staff_look(store, case):
    with store.write():
        store.append(STREAM, [Event('work_order.reported', AT, {'order': 'I1', 'unit': case.unit, 'problem': 'Look', 'kind': 'check', 'by': 'agent:tilden'}, 'planner-look'),
            Event('work_order.closed', AT + timedelta(minutes=1), {'order': 'I1', 'by': 'staff:manager', 'text': 'Empty, clean, door fine.'}, 'planner-look-done')])


def test_planner_arm_releases_only_after_the_model_has_judged_the_staff_record():
    store, case = setup_case()
    permissions(store)
    _ready_unit_with_a_staff_look(store, case)
    client = Client(response(stop='end_turn'))
    result = runtime.run_case(store, STREAM, case.id, AT + timedelta(minutes=1), chooser='planner', client=client, readers=reader(), check=checks)
    assert len(client.calls) == 1 and result.state == 'done'


def test_planner_arm_never_releases_while_a_staff_record_is_unjudged():
    store, case = setup_case()
    permissions(store)
    _ready_unit_with_a_staff_look(store, case)
    with store.write():
        store.append(STREAM, [Event('tilden.decision', AT, {'case': 'other', 'cost_cents': runtime.DAILY_CENTS}, 'planner-spend')])
    result = runtime.run_case(store, STREAM, case.id, AT + timedelta(minutes=1), chooser='planner', client=Client(), readers=reader(), check=checks)
    assert result.state == 'paused' and result.steps[-1]['pending_evidence']
    assert not any(e.kind == 'unit.unblocked' for e in store.events(STREAM))


def test_planner_handoff_does_not_park_the_release_path():
    store, case = setup_case()
    permissions(store)
    _ready_unit_with_a_staff_look(store, case)
    client = Client(response(block('hand_to_staff', 'a', question='Who checks the door?', record_ids=['I1'], role='front_desk', wake_evidence='staff note')))
    result = runtime.run_case(store, STREAM, case.id, AT + timedelta(minutes=1), chooser='planner', client=client, readers=reader(), check=checks)
    # the handoff is recorded, and the ready test and its checks still decided the release on the same wake
    assert len([s for s in result.steps if s.get('handoff')]) == 1
    assert any(s.get('second_opinion') == 'ready' for s in result.steps) and result.state == 'done'


def test_an_open_handoff_that_must_be_settled_first_stops_the_release():
    store, case = setup_case()
    permissions(store)
    with store.write():
        store.append(STREAM, [Event('work_order.reported', AT, {'order': 'I1', 'unit': case.unit, 'problem': 'Look', 'kind': 'check', 'by': 'agent:tilden'}, 'look'),
            Event('work_order.closed', AT + timedelta(minutes=1), {'order': 'I1', 'by': 'staff:manager', 'text': 'Empty, clean, door fine.'}, 'look-done')])
    close = next(e for e in store.events(STREAM) if e.kind == 'work_order.closed')
    cases.record(store, STREAM, case.id, 'updated', {'steps': [{'judged_event': close.id},
                 {'handoff': {'question': 'The former tenant left a sofa; what happens to it?', 'record_ids': ['I1'], 'role': 'manager', 'wake_evidence': 'staff note'}}]}, AT)
    blocking = lambda *a: check(0.95 if 'settled before the unit is offered' in a[4] else 0.1 if 'settle it' in a[4] else 0.99)(*a)
    result = runtime.run_case(store, STREAM, case.id, AT + timedelta(minutes=1), chooser='hybrid', client=Client(), readers=reader(), check=blocking)
    assert result.state != 'done'
    assert any('must be settled before the unit is offered' in str(s) for s in result.steps)


def test_release_settlement_reads_every_staff_record_since_the_handoff():
    store, case = setup_case()
    permissions(store)
    note(store, case, 'Former tenant left a chair.')
    client = Client(response(block('hand_to_staff', 'a', question='Who handles the chair?', record_ids=['O1'], role='front_desk', wake_evidence='staff note')))
    assert hybrid(store, case, client).state == 'needs staff'
    later = AT + timedelta(minutes=1)
    # the ruling comes first and nine routine records follow it, so a window of the newest few records would miss it
    words = ['Chair collected by the former tenant.'] + [f'Swept aisle {n}.' for n in range(9)] + ['Empty and ready.']
    with store.write():
        store.append(STREAM, [Event('walk.observed', later + timedelta(seconds=i), {'walk': 'W1', 'observation': f'S{i}', 'unit': case.unit, 'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'unknown', 'note': w, 'by': 'staff:manager', 'supersedes': None}, f'staff-{i}')
                              for i, w in enumerate(words)])
    asked = []

    def judge(*args):
        if 'settle it' in args[4]:
            asked.append(args[5])
            return check(0.99 if 'Chair collected' in args[5] else 0.1)(*args)
        return check(0.99)(*args)
    result = gate.act(store, STREAM, case.id, 'release_unit', {}, later + timedelta(minutes=1), check=judge, readers=reader())
    assert asked and 'Chair collected' in asked[0], result
    assert any(s.get('settled_question') == 'Who handles the chair?' for s in cases.load(store, STREAM)[case.id].steps)


def test_wake_settlement_keeps_a_ruling_from_before_an_unsettled_check():
    store, case = setup_case()
    permissions(store)
    note(store, case, 'Former tenant left a chair.')
    client = Client(response(block('hand_to_staff', 'a', question='Who handles the chair?', record_ids=['O1'], role='front_desk', wake_evidence='staff note')))
    assert hybrid(store, case, client).state == 'needs staff'

    def staff_note(at, key, words):
        with store.write():
            store.append(STREAM, [Event('walk.observed', at, {'walk': 'W1', 'observation': key, 'unit': case.unit, 'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'unknown', 'note': words, 'by': 'staff:manager', 'supersedes': None}, key)])
    later = AT + timedelta(minutes=1)
    staff_note(later, 'S1', 'Chair collected by the former tenant.')
    # the first check misreads the ruling; the step it records must not move the start of the evidence past the ruling
    runtime.sweep(store, STREAM, later, chooser='hybrid', client=Client(response(stop='end_turn')), readers=reader(),
                  check=lambda *args: check(0.6 if 'settle the question' in args[4] else 0.01)(*args))
    later += timedelta(minutes=1)
    staff_note(later, 'S2', 'Swept the aisle.')
    seen = []

    def judge(*args):
        if 'settle the question' in args[4]:
            seen.append(args[5])
            return check(0.99 if 'Chair collected' in args[5] else 0.1)(*args)
        return check(0.01)(*args)
    runtime.sweep(store, STREAM, later, chooser='hybrid', client=Client(response(stop='end_turn')), readers=reader(), check=judge)
    assert seen and 'Chair collected' in seen[-1]
