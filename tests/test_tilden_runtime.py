from datetime import timedelta
from types import SimpleNamespace

from core import renting
from core.store import Event
from core.tilden import authority, cases, runtime, tools
from tests.test_tilden_tools import AT, STREAM, allow, setup_case


def choose(action):
    return lambda *args: SimpleNamespace(choice=action)


def test_approval_wakes_and_is_single_use():
    store, case = setup_case()
    waiting = runtime.run_case(store, STREAM, case.id, AT, choose('hold_unit'))
    approval = waiting.wake[0]['approval']
    authority.approve(store, STREAM, approval, 'staff:office_manager', AT)
    runtime.sweep(store, STREAM, AT, choose('wait'))
    assert any(e.kind == 'tilden.approval.used' and e.payload['id'] == approval for e in store.events(STREAM))
    assert not authority.live_grant(store, STREAM, case.id, 'hold_unit', case.unit, AT, approval)


def test_changed_records_make_approval_stale():
    store, case = setup_case()
    waiting = runtime.run_case(store, STREAM, case.id, AT, choose('hold_unit'))
    authority.approve(store, STREAM, waiting.wake[0]['approval'], 'staff:office_manager', AT)
    renting.run(store, STREAM, renting.Act('staff-block', 'block_unit', 'staff:manager', case.unit, args={'reason': 'Staff work'}), AT)
    result = runtime.run_case(store, STREAM, case.id, AT, choose('wait'))
    assert result.state == 'waiting'
    assert result.wake[0]['approval'] != waiting.wake[0]['approval']
    assert any('records changed' in s.get('reason', '') for s in result.steps)


def test_own_hold_is_ignored_but_staff_hold_is_not_released():
    store, case = setup_case()
    allow(store, 'hold_unit')
    allow(store, 'release_unit')
    assert tools.act('hold_unit', store, STREAM, case, AT, reason='Repair').allowed
    assert 'the unit is blocked' not in tools.read('readiness', store, STREAM, case, AT)['reasons']
    assert tools.act('release_unit', store, STREAM, case, AT).allowed
    renting.run(store, STREAM, renting.Act('staff-block', 'block_unit', 'staff:manager', case.unit, args={'reason': 'Staff work'}), AT)
    assert 'the unit is blocked' in tools.read('readiness', store, STREAM, case, AT)['reasons']
    assert not tools.act('release_unit', store, STREAM, case, AT).allowed


def test_early_release_waits_and_budget_stops():
    store, case = setup_case()
    allow(store, 'hold_unit')
    result = runtime.run_case(store, STREAM, case.id, AT, choose('release_unit'))
    assert result.state == 'waiting' and 'Release refused' in result.steps[-1]['reason']
    case = cases.record(store, STREAM, case.id, 'updated', {'state': 'open', 'wake': [], 'steps': [{'decisions': runtime.MAX_DECISIONS}]}, AT)
    result = runtime.run_case(store, STREAM, case.id, AT, choose('wait'))
    assert result.state == 'needs staff' and 'limit' in result.steps[-1]['reason']


def test_sweep_only_opens_new_triggers_and_is_idempotent():
    store, _ = setup_case()
    with store.write():
        store.append(STREAM, [Event('lease.signed', AT - timedelta(days=2), {'lease': 'L1', 'tenant': 'T1', 'unit': 'A101', 'rent_cents': 100, 'start': '2026-01-01', 'schedule': []}, 'signed1'),
                              Event('lease.signed', AT - timedelta(days=2), {'lease': 'L2', 'tenant': 'T2', 'unit': 'A103', 'rent_cents': 100, 'start': '2026-01-01', 'schedule': []}, 'signed2'),
                              Event('lease.ended', AT - timedelta(days=1), {'lease': 'L1', 'on': '2026-11-12'}, 'old'),
                              Event('lease.ended', AT, {'lease': 'L2', 'on': '2026-11-13'}, 'new')])
    allow(store, 'hold_unit')
    runtime.sweep(store, STREAM, AT, choose('wait'))
    found = cases.load(store, STREAM)
    assert {c.unit for c in found.values()} == {'A102', 'A103'}
    before = len(store.events(STREAM))
    runtime.sweep(store, STREAM, AT, choose('wait'))
    assert len(store.events(STREAM)) == before


def test_finished_repair_and_later_staff_inspection_allow_release():
    store, case = setup_case()
    allow(store, 'hold_unit')
    allow(store, 'release_unit')
    assert tools.act('hold_unit', store, STREAM, case, AT, reason='Repair').allowed
    later = AT + timedelta(minutes=1)
    with store.write():
        store.append(STREAM, [
            Event('work_order.reported', AT, {'order': 'W1', 'unit': case.unit, 'problem': 'Door', 'by': 'staff:manager'}, 'r1'),
            Event('work_order.closed', AT, {'order': 'W1', 'by': 'staff:manager', 'text': 'Done'}, 'r2'),
            Event('work_order.reported', later, {'order': 'W2', 'unit': case.unit, 'problem': 'Inspection', 'kind': 'check', 'by': 'staff:manager'}, 'i1'),
            Event('work_order.closed', later, {'order': 'W2', 'by': 'staff:manager', 'text': 'Checked'}, 'i2'),
        ])
    result = runtime.run_case(store, STREAM, case.id, later, choose('release_unit'), check=lambda *a: SimpleNamespace(probabilities={'yes': 0.99}, reason=None))
    assert result.state == 'done'


def test_staff_block_is_handed_to_staff():
    store, case = setup_case()
    renting.run(store, STREAM, renting.Act('staff-block', 'block_unit', 'staff:manager', case.unit, args={'reason': 'Staff work'}), AT)
    result = runtime.run_case(store, STREAM, case.id, AT, choose('release_unit'))
    assert result.state == 'needs staff' and 'Staff placed' in result.steps[-1]['reason']


def test_note_reader_supplies_grounded_work():
    store, case = setup_case()
    allow(store, 'hold_unit')
    allow(store, 'open_work_order')
    with store.write():
        store.append(STREAM, [
            Event('walk.started', AT, {'walk': 'W1', 'route': [case.unit], 'by': 'staff:manager'}, 'walk-start'),
            Event('walk.observed', AT, {'walk': 'W1', 'observation': 'O1', 'unit': case.unit, 'lock': 'absent',
                                       'whose': 'unknown', 'tag': 'absent', 'door': 'damaged', 'note': 'Door is broken',
                                       'by': 'staff:manager', 'supersedes': None}, 'walk-note'),
        ])
    calls = []
    def read_note(note, route, unit):
        calls.append(note)
        return SimpleNamespace(refusal=None, work=[{'kind': 'repair', 'words': 'Door is broken'}])
    choices = iter(['open_work_order', 'wait'])
    result = runtime.run_case(store, STREAM, case.id, AT, lambda *args: SimpleNamespace(choice=next(choices)),
                              {'read_note': read_note}, check=lambda *a: SimpleNamespace(probabilities={'yes': 0.01}, reason=None))
    assert result.state == 'waiting' and calls == ['Door is broken', 'Door is broken']
    assert any(e.kind == 'work_order.reported' and e.payload['problem'] == 'Door is broken' for e in store.events(STREAM))


def test_daily_spending_limit_counts_other_cases():
    store, case = setup_case()
    with store.write():
        store.append(STREAM, [Event('tilden.decision', AT, {'case': 'another', 'cost_cents': runtime.DAILY_CENTS}, 'daily-cost')])
    result = runtime.run_case(store, STREAM, case.id, AT, choose('wait'))
    assert result.state == 'needs staff' and 'spending limit' in result.steps[-1]['reason']


def test_initial_hold_approval_is_preserved_before_chooser():
    store, case = setup_case()
    result = runtime.run_case(store, STREAM, case.id, AT, choose('wait'))
    assert result.state == 'waiting' and result.wake[0]['action'] == 'hold_unit'


def test_look_uses_move_out_and_event_order_for_ties():
    store, case = setup_case()
    with store.write():
        store.append(STREAM, [
            Event('work_order.reported', AT - timedelta(days=3), {'order': 'W1', 'unit': case.unit, 'problem': 'Door', 'by': 'staff:manager'}, 'old-report'),
            Event('work_order.closed', AT - timedelta(days=2), {'order': 'W1', 'by': 'staff:manager', 'text': 'Fixed'}, 'old-close'),
            Event('work_order.reported', AT - timedelta(days=1), {'order': 'I1', 'unit': case.unit, 'problem': 'Look', 'kind': 'check', 'by': 'staff:manager'}, 'old-look'),
            Event('work_order.closed', AT - timedelta(days=1), {'order': 'I1', 'by': 'staff:manager', 'text': 'Good'}, 'old-look-close'),
            Event('lease.signed', AT - timedelta(days=4), {'lease': 'L9', 'tenant': 'T9', 'unit': case.unit, 'rent_cents': 100, 'start': '2026-01-01', 'schedule': []}, 'sign'),
            Event('lease.ended', AT, {'lease': 'L9', 'unit': case.unit, 'on': '2026-11-13'}, 'move'),
        ])
    records = runtime._records(store, STREAM, case, AT)
    assert runtime._latest_look(store, STREAM, case, records) is None
    with store.write():
        store.append(STREAM, [
            Event('work_order.reported', AT, {'order': 'I0', 'unit': case.unit, 'problem': 'Look', 'kind': 'check', 'by': 'staff:manager'}, 'prior-look'),
            Event('work_order.closed', AT, {'order': 'I0', 'by': 'staff:manager', 'text': 'Good'}, 'prior-look-close'),
            Event('work_order.reported', AT, {'order': 'W2', 'unit': case.unit, 'problem': 'Latch', 'by': 'staff:manager'}, 'new-report'),
            Event('work_order.closed', AT, {'order': 'W2', 'by': 'staff:manager', 'text': 'Fixed'}, 'new-close'),
        ])
    records = runtime._records(store, STREAM, case, AT)
    assert runtime._latest_look(store, STREAM, case, records) is None
    with store.write():
        store.append(STREAM, [
            Event('work_order.reported', AT, {'order': 'I2', 'unit': case.unit, 'problem': 'Look', 'kind': 'check', 'by': 'staff:manager'}, 'new-look'),
            Event('work_order.closed', AT, {'order': 'I2', 'by': 'staff:manager', 'text': 'Good'}, 'new-look-close'),
        ])
    records = runtime._records(store, STREAM, case, AT)
    assert runtime._latest_look(store, STREAM, case, records)[1] == 'I2'


def test_daily_limit_counts_reader_charges_from_other_cases():
    store, case = setup_case()
    with store.write():
        source = store.append(STREAM, [Event('test.source', AT, {}, 'other-source')])[0]
    other = cases.open_case(store, STREAM, source, 'A101', AT)
    cases.record(store, STREAM, other.id, 'updated', {'steps': [{'reader_cents': 100}]}, AT)
    result = runtime.run_case(store, STREAM, case.id, AT, choose('wait'))
    assert result.state == 'needs staff' and 'spending limit' in result.steps[-1]['reason']


def test_fixed_reconsiders_new_note_on_wake():
    store, case = setup_case()
    for action in ('hold_unit', 'open_work_order', 'request_inspection'):
        allow(store, action)
    read = lambda *a: SimpleNamespace(refusal=None, work=[{'words': 'Door is broken'}])
    check = lambda *a: SimpleNamespace(probabilities={'yes': 0.01}, reason=None)
    runtime.run_case(store, STREAM, case.id, AT, readers={'read_note': read}, chooser='fixed', check=check)
    with store.write():
        store.append(STREAM, [Event('walk.started', AT, {'walk': 'W1', 'route': [case.unit], 'by': 'staff:manager'}, 'start-later'),
            Event('walk.observed', AT, {'walk': 'W1', 'observation': 'O1', 'unit': case.unit, 'lock': 'unknown', 'whose': 'unknown', 'tag': 'unknown', 'door': 'unknown', 'note': 'Door is broken', 'by': 'staff:manager', 'supersedes': None}, 'note-later')])
    runtime.sweep(store, STREAM, AT, readers={'read_note': read}, chooser='fixed', check=check)
    assert any(e.kind == 'work_order.reported' and e.payload['problem'] == 'Door is broken' for e in store.events(STREAM))


def test_fixed_does_not_reopen_work_for_an_answered_note():
    store, case = setup_case()
    for action in ('hold_unit', 'open_work_order', 'request_inspection'):
        allow(store, action)
    with store.write():
        store.append(STREAM, [Event('walk.started', AT, {'walk': 'W1', 'route': [case.unit], 'by': 'staff:manager'}, 'start-once'),
            Event('walk.observed', AT, {'walk': 'W1', 'observation': 'O1', 'unit': case.unit, 'lock': 'unknown', 'whose': 'unknown', 'tag': 'unknown', 'door': 'unknown', 'note': 'Door is broken', 'by': 'staff:manager', 'supersedes': None}, 'note-once')])
    read = lambda *a: SimpleNamespace(refusal=None, work=[{'words': 'Door is broken'}])
    check = lambda *a: SimpleNamespace(probabilities={'yes': 0.01}, reason=None)
    runtime.run_case(store, STREAM, case.id, AT, readers={'read_note': read}, chooser='fixed', check=check)
    runtime.run_case(store, STREAM, case.id, AT, readers={'read_note': read}, chooser='fixed', check=check)
    assert len([e for e in store.events(STREAM) if e.kind == 'work_order.reported' and e.payload.get('answers_record') == 'O1']) == 1


def test_inspection_after_repair_at_same_minute_counts():
    store, case = setup_case()
    with store.write():
        store.append(STREAM, [
            Event('work_order.reported', AT, {'order': 'W1', 'unit': case.unit, 'problem': 'Door', 'by': 'staff:manager'}, 'tie-report'),
            Event('work_order.closed', AT, {'order': 'W1', 'by': 'staff:manager', 'text': 'Fixed'}, 'tie-close'),
            Event('work_order.reported', AT, {'order': 'I1', 'unit': case.unit, 'problem': 'Look', 'kind': 'check', 'by': 'staff:manager'}, 'tie-look'),
            Event('work_order.closed', AT, {'order': 'I1', 'by': 'staff:manager', 'text': 'Good'}, 'tie-look-close'),
        ])
    records = runtime._records(store, STREAM, case, AT)
    assert runtime._latest_look(store, STREAM, case, records)[1] == 'I1'
