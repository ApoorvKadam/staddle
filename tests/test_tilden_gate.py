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
