from datetime import timedelta

from core import renting, walk
from core.store import Event
from core.tilden import authority, cases
from tests.test_core_walk import AT, FRONT, tiny

STREAM = 'visitor:a'


def setup_case():
    store = tiny()
    with store.write():
        source = store.append(STREAM, [Event('test.source', AT, {}, 'source')])[0]
    case = cases.open_case(store, STREAM, source, 'A102', AT)
    return store, case


def allow(store, action):
    authority.grant(store, STREAM, action, {'unit': 'A102'}, AT + timedelta(days=1), 'owner', AT)


def test_commands_require_live_grant_and_keep_actor_and_case():
    store, case = setup_case()
    act = walk.Act('no', 'report_work_order', 'agent:tilden', unit='A102', text='Door broken', case_id=case.id)
    denied = walk.run(store, STREAM, act, AT)
    assert not denied.allowed and 'grant' in denied.check
    allow(store, 'open_work_order')
    act.id = 'yes'
    assert walk.run(store, STREAM, act, AT).allowed
    event = next(e for e in store.events(STREAM) if e.command == 'yes' and e.kind == 'work_order.reported')
    assert event.source == event.payload['by'] == 'agent:tilden'
    assert event.payload['case_id'] == case.id
    assert walk.run(store, STREAM, walk.Act('staff', 'report_work_order', FRONT, unit='A102', text='Staff report'), AT).allowed


def test_registry_reads_and_actions():
    from core.tilden import tools
    store, case = setup_case()
    for key in ('open_work_order', 'link_reports', 'hold_unit', 'release_unit', 'request_inspection'):
        allow(store, key)
    for name in ('lease', 'walk_notes', 'work_orders', 'readiness', 'availability'):
        assert tools.read(name, store, STREAM, case, AT) is not None
    first = tools.act('open_work_order', store, STREAM, case, AT, text='Door broken')
    second = tools.act('open_work_order', store, STREAM, case, AT, text='Door sticks')
    assert first.allowed and second.allowed
    assert tools.act('link_reports', store, STREAM, case, AT, order=second.made, into=first.made).allowed
    assert tools.act('hold_unit', store, STREAM, case, AT, reason='Repair needed').allowed
    inspection = tools.act('request_inspection', store, STREAM, case, AT)
    assert inspection.allowed
    assert walk.load(store, STREAM)[1].orders[inspection.made].kind == 'check'
    assert tools.act('release_unit', store, STREAM, case, AT).allowed
    events = [e for e in store.events(STREAM) if e.source == 'agent:tilden' and e.kind != 'gate.decided']
    assert events and all(e.payload['case_id'] == case.id for e in events)
    handed = tools.act('hand_to_staff', store, STREAM, case, AT, question='Conflicting reports', record_ids=['O1'], role='front_desk', wake_evidence='staff note')
    assert handed.state == 'needs staff'
    assert handed.steps[-1]['handoff'] == {'question': 'Conflicting reports', 'record_ids': ['O1'], 'role': 'front_desk', 'wake_evidence': 'staff note'}


def test_renting_agent_cannot_bypass_grants():
    store, case = setup_case()
    act = renting.Act('hold-no', 'block_unit', 'agent:tilden', 'A102', args={'reason': 'Repair'}, case_id=case.id)
    assert not renting.run(store, STREAM, act, AT).allowed
    allow(store, 'hold_unit')
    act.id = 'hold-yes'
    assert renting.run(store, STREAM, act, AT).allowed
    assert not renting.run(store, STREAM, renting.Act('release-no', 'unblock_unit', 'agent:tilden', 'A102', case_id=case.id), AT).allowed


def test_grants_cannot_be_used_for_another_unit_or_after_expiry():
    store, case = setup_case()
    allow(store, 'open_work_order')
    wrong = walk.Act('wrong-unit', 'report_work_order', 'agent:tilden', unit='A101', text='Door broken', case_id=case.id)
    assert not walk.run(store, STREAM, wrong, AT).allowed
    expired = walk.Act('expired', 'report_work_order', 'agent:tilden', unit=case.unit, text='Door broken', case_id=case.id)
    assert not walk.run(store, STREAM, expired, AT + timedelta(days=1)).allowed
    early = walk.Act('early', 'report_work_order', 'agent:tilden', unit=case.unit, text='Door broken', case_id=case.id)
    assert not walk.run(store, STREAM, early, AT - timedelta(seconds=1)).allowed
    forbidden = walk.Act('forbidden', 'start_walk', 'agent:tilden', route=[case.unit], case_id=case.id)
    assert not walk.run(store, STREAM, forbidden, AT).allowed


def test_inspection_kind_uses_action_instead_of_order_title():
    store, case = setup_case()
    allow(store, 'request_inspection')
    action = walk.Act('inspection-title', 'report_work_order', 'agent:tilden', unit=case.unit,
                      text='Safety check after work', case_id=case.id, authority_action='request_inspection')
    result = walk.run(store, STREAM, action, AT)
    assert result.allowed
    assert walk.load(store, STREAM)[1].orders[result.made].kind == 'check'


def test_agent_cannot_join_inspections_or_staff_separated_orders():
    from core.tilden import tools
    store, case = setup_case()
    for action in ('open_work_order', 'link_reports', 'request_inspection'):
        allow(store, action)
    a = tools.act('open_work_order', store, STREAM, case, AT, text='Broken door').made
    b = tools.act('open_work_order', store, STREAM, case, AT, text='Broken latch').made
    check = tools.act('request_inspection', store, STREAM, case, AT).made
    assert not tools.act('link_reports', store, STREAM, case, AT, order=check, into=a).allowed
    assert not tools.act('link_reports', store, STREAM, case, AT, order=a, into=check).allowed
    assert walk.run(store, STREAM, walk.Act('apart', 'keep_work_orders_apart', FRONT, about=a, into=b), AT).allowed
    assert not tools.act('link_reports', store, STREAM, case, AT, order=b, into=a).allowed


def test_agent_cannot_join_orders_staff_kept_apart():
    from core.tilden import tools
    store, case = setup_case()
    allow(store, 'open_work_order')
    allow(store, 'link_reports')
    a = tools.act('open_work_order', store, STREAM, case, AT, text='Broken door').made
    b = tools.act('open_work_order', store, STREAM, case, AT, text='Broken latch').made
    assert walk.run(store, STREAM, walk.Act('apart-only', 'keep_work_orders_apart', FRONT, about=a, into=b), AT).allowed
    assert not tools.act('link_reports', store, STREAM, case, AT, order=b, into=a).allowed


def test_no_work_closure_requires_grant_and_case_unit():
    store, case = setup_case()
    a = walk.run(store, STREAM, walk.Act('a', 'report_work_order', FRONT, unit=case.unit, text='Water mark'), AT).made
    b = walk.run(store, STREAM, walk.Act('b', 'report_work_order', FRONT, unit='A101', text='Water mark'), AT).made
    def close(key, order):
        return walk.run(store, STREAM, walk.Act(key, 'close_report_no_work', 'agent:tilden', about=order, text='Staff record I1: dry', case_id=case.id), AT)
    assert not close('no-grant', a).allowed
    allow(store, 'close_report_no_work')
    assert not close('wrong-unit', b).allowed
    assert close('yes-close', a).allowed


def test_no_work_refuses_inspection_and_staff_separated_orders():
    from core.tilden import tools
    store, case = setup_case()
    for action in ('open_work_order', 'request_inspection', 'close_report_no_work'):
        allow(store, action)
    a = tools.act('open_work_order', store, STREAM, case, AT, text='Wall mark').made
    b = tools.act('open_work_order', store, STREAM, case, AT, text='Door stuck').made
    inspection = tools.act('request_inspection', store, STREAM, case, AT).made
    assert walk.run(store, STREAM, walk.Act('apart-no-work', 'keep_work_orders_apart', FRONT, about=a, into=b), AT).allowed
    assert not tools.act('close_report_no_work', store, STREAM, case, AT, order=a, text='Staff record O1: dry').allowed
    assert not tools.act('close_report_no_work', store, STREAM, case, AT, order=inspection, text='Staff record O1: dry').allowed
