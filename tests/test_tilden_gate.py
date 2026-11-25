from types import SimpleNamespace

from core.store import Event
from tests.test_tilden_tools import AT, STREAM, allow, setup_case


def test_release_without_recent_look_is_refused():
    from core.tilden import gate

    store, case = setup_case()
    allow(store, 'release_unit')
    content, result, error = gate.act(store, STREAM, case.id, 'release_unit', {}, AT)
    assert error and 'Release refused' in content
    assert any('Release refused' in step.get('refused', '') for step in result.steps)
    assert not any(event.kind == 'unit.unblocked' for event in store.events(STREAM))


def test_work_order_removing_tenant_items_is_refused():
    from core.tilden import gate

    store, case = setup_case()
    allow(store, 'open_work_order')
    with store.write():
        store.append(STREAM, [
            Event('walk.started', AT, {'walk': 'W1', 'route': [case.unit], 'by': 'staff:manager'}, 'start'),
            Event('walk.observed', AT, {'walk': 'W1', 'observation': 'O1', 'unit': case.unit, 'lock': 'absent',
                  'whose': 'unknown', 'tag': 'absent', 'door': 'ok', 'note': 'Tenant left boxes',
                  'by': 'staff:manager', 'supersedes': None}, 'note'),
        ])
    readers = {'read_note': lambda *a: SimpleNamespace(refusal=None, fields={'contents': 'seen'})}
    content, result, error = gate.act(store, STREAM, case.id, 'open_work_order',
                                      {'text': 'Remove the boxes', 'answers_record': 'O1'}, AT, readers=readers)
    assert error and 'former tenant' in content
    assert any('former tenant' in step.get('refused', '') for step in result.steps)
    assert not any(event.kind == 'work_order.reported' for event in store.events(STREAM))


def test_accepted_failed_inspection_blocks_until_later_inspection():
    from core import walk, work_forms
    from core.tilden import gate
    from tests.test_core_walk import FRONT

    store, case = setup_case()
    with store.write():
        store.append(STREAM, [
            Event('lease.signed', AT, {'lease': 'L9', 'tenant': 'T9', 'unit': case.unit, 'rent_cents': 100, 'start': '2026-01-01', 'schedule': []}, 'sign'),
            Event('lease.ended', AT, {'lease': 'L9', 'unit': case.unit, 'on': '2026-11-13'}, 'move'),
            Event('work_order.reported', AT, {'order': 'I1', 'unit': case.unit, 'problem': 'Inspect', 'kind': 'check', 'by': FRONT}, 'inspection'),
        ])
    def inspect(ready, cid, order=None):
        wk = walk.load(store, STREAM)[1]
        steps = work_forms.inspect_unit(wk, case.unit, order=order, ready=ready, observations='Door checked', scope='Inspect door', person='Dana', source=FRONT, cid=cid)
        assert work_forms.commit(store, STREAM, steps, AT).allowed
    inspect(False, 'failed', 'I1')
    observation = next(e for e in store.events(STREAM) if e.kind == 'walk.observed')
    assert observation.payload.get('verdict') == 'not ready'
    wk = walk.load(store, STREAM)[1]
    assert work_forms.commit(store, STREAM, work_forms.check_inspection(wk, 'I1', accept=True, words='Record checked', source=FRONT, cid='accept'), AT).allowed
    records = gate._records(store, STREAM, case, AT)
    assert not gate._ready_now(store, STREAM, case, records)
    inspect(True, 'passed')
    records = gate._records(store, STREAM, case, AT)
    assert gate._ready_now(store, STREAM, case, records)
