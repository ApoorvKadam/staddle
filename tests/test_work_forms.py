"""Compact submissions preserve the ordinary staff records and transaction boundary."""
import pytest
from core import walk, work_forms
from tests.test_core_walk import AT, FRONT, tiny, put

STREAM = 'visitor:a'


def setup_order():
    store = tiny()
    decision = walk.run(store, STREAM, walk.Act('report', 'report_work_order', FRONT, unit='A102', text='Door catches'), AT)
    return store, decision.made


def test_compact_repair_matches_the_ordinary_moves_and_check():
    compact, oid = setup_order()
    ordinary, oid2 = setup_order()
    wk = walk.load(compact, STREAM)[1]
    steps = work_forms.record_repair(wk, oid, work='Fixed track', scope='Repair track', person='Dana', source=FRONT, cid='form')
    assert work_forms.commit(compact, STREAM, steps, AT).allowed
    for action, kw in [('triage_work_order', {'text': 'Repair track'}), ('assign_work_order', {'to': 'person:Dana'}), ('record_order_work', {'text': 'Fixed track'})]:
        assert walk.run(ordinary, STREAM, walk.Act(action, action, FRONT, about=oid2, **kw), AT).allowed
    wk = walk.load(compact, STREAM)[1]
    assert [(x.state, x.text, x.to) for x in wk.orders[oid].changes] == [(x.state, x.text, x.to) for x in walk.load(ordinary, STREAM)[1].orders[oid2].changes]
    result = work_forms.commit(compact, STREAM, work_forms.check_repair(wk, oid, accept=False, words='Still catches', source=FRONT, cid='reject'), AT)
    assert result.allowed and walk.load(compact, STREAM)[1].orders[oid].state == 'assigned'


def test_compound_refusal_and_interruption_leave_no_partial_records(monkeypatch):
    store, oid = setup_order()
    wk = walk.load(store, STREAM)[1]
    steps = work_forms.record_repair(wk, oid, work='Fixed track', scope='Repair track', person='Dana', source='renter', cid='bad')
    before = len(store.stream_events(STREAM))
    assert not work_forms.commit(store, STREAM, steps, AT).allowed
    assert len(store.stream_events(STREAM)) == before
    steps = work_forms.record_repair(wk, oid, work='Fixed track', scope='Repair track', person='Dana', source=FRONT, cid='interrupted')
    original = walk.command
    count = 0
    def interrupted(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError('interrupted')
        return original(*args, **kwargs)
    monkeypatch.setattr(walk, 'command', interrupted)
    with pytest.raises(RuntimeError):
        work_forms.commit(store, STREAM, steps, AT)
    assert len(store.stream_events(STREAM)) == before
    assert walk.load(store, STREAM)[1].orders[oid].state == 'reported'


def test_failed_inspection_is_preserved_when_its_record_is_accepted():
    store = tiny()
    put(store, STREAM, 1, 'work_order.reported', {'order': 'WO0001', 'unit': 'A102', 'problem': 'Inspect unit', 'by': FRONT, 'kind': 'check'})
    wk = walk.load(store, STREAM)[1]
    steps = work_forms.inspect_unit(wk, 'A102', order='WO0001', ready=False, observations='Door still catches', scope='Inspect door', person='Dana', source=FRONT, cid='inspect')
    outcome = work_forms.commit(store, STREAM, steps, AT)
    assert outcome.allowed and outcome.woke
    assert work_forms.commit(store, STREAM, steps, AT).repeated
    wk = walk.load(store, STREAM)[1]
    assert 'Not ready to rent' in next(iter(wk.observations.values())).note
    returned = work_forms.commit(store, STREAM, work_forms.check_inspection(wk, 'WO0001', accept=False, words='Add a detail', source=FRONT, cid='return'), AT)
    assert returned.allowed and walk.load(store, STREAM)[1].orders['WO0001'].state == 'assigned'
    wk = walk.load(store, STREAM)[1]
    correction = work_forms.inspect_unit(wk, 'A102', order='WO0001', ready=False, observations='Door still catches', scope='Inspect door', person='person:Dana', source=FRONT, cid='correct')
    assert work_forms.commit(store, STREAM, correction, AT).allowed
    wk = walk.load(store, STREAM)[1]
    result = work_forms.commit(store, STREAM, work_forms.check_inspection(wk, 'WO0001', accept=True, words='Inspection recorded correctly', source=FRONT, cid='check'), AT)
    assert result.allowed and result.woke
    wk = walk.load(store, STREAM)[1]
    assert wk.orders['WO0001'].state == 'closed'
    assert 'Door still catches' in next(iter(wk.observations.values())).note


def test_failure_during_append_rolls_back_the_first_written_event(monkeypatch):
    store, oid = setup_order()
    wk = walk.load(store, STREAM)[1]
    steps = work_forms.record_repair(wk, oid, work='Fixed track', scope='Repair track', person='Dana', source=FRONT, cid='broken-write')
    before = len(store.stream_events(STREAM))
    append = store.append
    def interrupted(stream, events, **kwargs):
        append(stream, events[:1], **kwargs)
        raise RuntimeError('interrupted during write')
    monkeypatch.setattr(store, 'append', interrupted)
    with pytest.raises(RuntimeError):
        work_forms.commit(store, STREAM, steps, AT)
    assert len(store.stream_events(STREAM)) == before
    assert walk.load(store, STREAM)[1].orders[oid].state == 'reported'


def test_compact_inspection_matches_the_ordinary_walk_and_order_commands():
    compact, ordinary = tiny(), tiny()
    for store in (compact, ordinary):
        put(store, STREAM, 1, 'work_order.reported', {'order': 'WO0001', 'unit': 'A102', 'problem': 'Inspect unit', 'by': FRONT, 'kind': 'check'})
    wk = walk.load(compact, STREAM)[1]
    steps = work_forms.inspect_unit(wk, 'A102', order='WO0001', ready=False, observations='Door still catches',
                                    scope='Inspect door', person='Dana', source=FRONT, cid='compact-inspection')
    assert work_forms.commit(compact, STREAM, steps, AT).allowed
    wk = walk.load(compact, STREAM)[1]
    assert work_forms.commit(compact, STREAM, work_forms.check_inspection(wk, 'WO0001', accept=True, words='Checked the record',
                                                                        source=FRONT, cid='compact-check'), AT).allowed
    def take(command, action, **kwargs):
        result = walk.run(ordinary, STREAM, walk.Act(command, action, FRONT, **kwargs), AT)
        assert result.allowed, result.check
        return result.made
    take('triage', 'triage_work_order', about='WO0001', text='Inspect door')
    take('assign', 'assign_work_order', about='WO0001', to='person:Dana')
    walk_id = take('walk', 'start_walk', route=['A102'])
    text = 'Not ready to rent. Door still catches'
    take('observe', 'observe_unit', about=walk_id, unit='A102', form=dict(work_forms.UNKNOWN), text=text)
    take('finish', 'finish_walk', about=walk_id)
    take('record', 'record_order_work', about='WO0001', text=text)
    take('check', 'check_order_work', about='WO0001', text='Checked the record')
    take('close', 'close_work_order', about='WO0001')
    a, b = walk.load(compact, STREAM)[1], walk.load(ordinary, STREAM)[1]
    assert a.orders['WO0001'].changes == b.orders['WO0001'].changes
    assert list(a.observations.values()) == list(b.observations.values())


def test_a_refusal_on_a_later_step_rolls_back_the_whole_submission():
    store, oid = setup_order()
    wk = walk.load(store, STREAM)[1]
    steps = work_forms.record_repair(wk, oid, work='Fixed track', scope='Repair track', person='Dana', source=FRONT, cid='later-refusal')
    steps[1] = ('assign_work_order', lambda made: walk.Act('later-refusal:bad', 'assign_work_order', 'renter', about=oid, to='person:Dana'))
    before = len(store.stream_events(STREAM))
    assert not work_forms.commit(store, STREAM, steps, AT).allowed
    assert len(store.stream_events(STREAM)) == before
    assert walk.load(store, STREAM)[1].orders[oid].state == 'reported'


def test_staff_transaction_is_not_reported_as_a_running_shift(monkeypatch):
    from core.tilden import shift
    store, stream = object(), 'visitor:a'
    with shift._lock(store, stream):
        assert not shift.running(store, stream)
    def step(*args):
        assert shift.running(store, stream)
        return 'finished'
    monkeypatch.setattr(shift, '_step', step)
    assert shift.start_shift(store, stream, AT, allowance=None) == 'finished'
    assert not shift.running(store, stream)


def test_staff_forms_do_not_invent_an_inspection_when_no_order_exists():
    from desk.tilden_screen import staff_actions
    assert staff_actions(walk.load(tiny(), STREAM)[1], 'A102') == []


@pytest.mark.parametrize('assignee, submitted', [('person:Dana', 'Dana'), ('front_desk', 'front_desk')])
def test_returned_repair_accepts_the_existing_assignee(assignee, submitted):
    store, oid = setup_order()
    for action, kwargs in [('triage_work_order', {'text': 'Repair track'}), ('assign_work_order', {'to': assignee}), ('record_order_work', {'text': 'Fixed track'})]:
        assert walk.run(store, STREAM, walk.Act(action, action, FRONT, about=oid, **kwargs), AT).allowed
    wk = walk.load(store, STREAM)[1]
    assert work_forms.commit(store, STREAM, work_forms.check_repair(wk, oid, accept=False, words='Still catches', source=FRONT, cid='returned'), AT).allowed
    wk = walk.load(store, STREAM)[1]
    steps = work_forms.record_repair(wk, oid, work='Adjusted track', scope='Repair track', person=submitted, source=FRONT, cid='retry')
    assert work_forms.commit(store, STREAM, steps, AT).allowed
    assert walk.load(store, STREAM)[1].orders[oid].state == 'work recorded'
