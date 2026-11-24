from datetime import timedelta

from core.seed import NOW, build
from core.store import Event, Store
from core.tilden import authority, cases


def test_demo_grants_are_visible_to_cases_in_a_copy():
    store = Store()
    with store.write():
        store.append('base', build(standing=True).led.pending)
        source = store.append('visit', [Event('test.source', NOW, {}, 'source')])[0]
    case = cases.open_case(store, 'visit', source, 'A101', NOW)
    for action in ('open_work_order', 'link_reports', 'hold_unit', 'release_unit', 'request_inspection'):
        assert authority.live_grant(store, 'visit', case.id, action, case.unit, NOW) == (action != 'release_unit')
        assert authority.check(store, 'visit', case.id, action, {'objective': case.objective}, {}, NOW).level == 'alone'
        assert not authority.live_grant(store, 'visit', case.id, action, case.unit, NOW + timedelta(days=366))


def test_other_facilities_start_without_grants():
    store = Store()
    with store.write():
        source = store.append('visit', [Event('test.source', NOW, {}, 'source')])[0]
    case = cases.open_case(store, 'visit', source, 'A101', NOW)
    assert not authority.live_grant(store, 'visit', case.id, 'open_work_order', case.unit, NOW)
