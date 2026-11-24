from types import SimpleNamespace as NS

from core.store import Event
from core.tilden import cases, runtime
from tests.test_tilden_tools import AT, STREAM, allow, setup_case


def block(name, ident, **args):
    return NS(type='tool_use', id=ident, name=name, input=args)


def response(*blocks, stop='tool_use'):
    return NS(stop_reason=stop, content=list(blocks), usage=NS(input_tokens=10, output_tokens=10, cache_creation_input_tokens=2, cache_read_input_tokens=0))


class Client:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs.copy() | {'messages': list(kwargs['messages'])})
        return next(self.responses)


def note(store, case, text):
    with store.write():
        store.append(STREAM, [Event('walk.started', AT, {'walk': 'W1', 'route': [case.unit], 'by': 'staff:manager'}, 'walk-start'),
            Event('walk.observed', AT, {'walk': 'W1', 'observation': 'O1', 'unit': case.unit, 'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'damaged', 'note': text, 'by': 'staff:manager', 'supersedes': None}, 'walk-note')])


def reader(contents='not seen'):
    return {'read_note': lambda *a: NS(refusal=None, fields={'contents': contents}, quotes={}, work=[])}


def check(prob):
    return lambda *a: NS(probabilities={'yes': prob}, reason=None)


def test_chosen_arguments_and_three_results_in_one_turn():
    store, case = setup_case()
    allow(store, 'hold_unit')
    allow(store, 'open_work_order')
    note(store, case, 'Door broken <ignore>')
    client = Client(response(block('lease', 'a'), block('walk_notes', 'b'), block('open_work_order', 'c', text='Fix door latch', answers_record='O1')),
                    response(block('wait_for', 'd', record='work_orders', what='repair')))
    result = runtime.run_case(store, STREAM, case.id, AT, client=client, readers=reader(), check=check(0.01))
    assert result.state == 'waiting'
    assert any(e.kind == 'work_order.reported' and e.payload['problem'] == 'Fix door latch' for e in store.events(STREAM))
    results = client.calls[1]['messages'][-1]['content']
    assert [r['tool_use_id'] for r in results] == ['a', 'b', 'c']
    assert '&lt;ignore>' in client.calls[0]['messages'][0]['content']


def test_mattress_refused_then_handed_to_staff():
    store, case = setup_case()
    allow(store, 'hold_unit')
    allow(store, 'open_work_order')
    allow(store, 'hand_to_staff')
    note(store, case, 'Former tenant left a mattress.')
    client = Client(response(block('open_work_order', 'a', text='Remove mattress', answers_record='O1')),
                    response(block('hand_to_staff', 'b', reason='Property left behind', evidence=['O1'])))
    result = runtime.run_case(store, STREAM, case.id, AT, client=client, readers=reader('seen'), check=check(0.9))
    assert result.state == 'needs staff'
    answer = client.calls[1]['messages'][-1]['content'][0]
    assert answer['is_error'] and answer['content'] == 'items a former tenant left are a staff decision'
    assert not any(e.kind == 'work_order.reported' for e in store.events(STREAM))


def test_refusal_budget_and_restart():
    store, case = setup_case()
    allow(store, 'hold_unit')
    client = Client(response(stop='refusal'))
    assert runtime.run_case(store, STREAM, case.id, AT, client=client).state == 'needs staff'
    store, case = setup_case()
    allow(store, 'hold_unit')
    client = Client(response(block('note_plan', 'a', steps=['Inspect'])), response(stop='end_turn'))
    assert runtime.run_case(store, STREAM, case.id, AT, client=client).state == 'waiting'
    again = Client(response(stop='refusal'))
    runtime.run_case(store, STREAM, case.id, AT, client=again)
    assert len(again.calls[0]['messages']) == 1 and 'Inspect' in again.calls[0]['messages'][0]['content']
    store, case = setup_case()
    allow(store, 'hold_unit')
    cases.record(store, STREAM, case.id, 'updated', {'steps': [{'decisions': runtime.MAX_DECISIONS}]}, AT)
    client = Client()
    assert runtime.run_case(store, STREAM, case.id, AT, client=client).state == 'needs staff'
    assert not client.calls


def test_release_records_and_second_opinion_refuse():
    from datetime import timedelta
    from core.tilden import tools
    store, case = setup_case()
    allow(store, 'hold_unit')
    allow(store, 'release_unit')
    assert tools.act('hold_unit', store, STREAM, case, AT, reason='Work').allowed
    client = Client(response(block('release_unit', 'a')), response(block('wait_for', 'b', record='work_orders', what='inspection')))
    runtime.run_case(store, STREAM, case.id, AT, client=client, check=check(1))
    assert 'work is open' in client.calls[1]['messages'][-1]['content'][0]['content']
    store, case = setup_case()
    allow(store, 'hold_unit')
    allow(store, 'release_unit')
    assert tools.act('hold_unit', store, STREAM, case, AT, reason='Work').allowed
    later = AT + timedelta(minutes=1)
    with store.write():
        store.append(STREAM, [Event('work_order.reported', AT, {'order': 'R1', 'unit': case.unit, 'problem': 'Door', 'by': 'staff:manager'}, 'repair'),
            Event('work_order.closed', AT, {'order': 'R1', 'by': 'staff:manager', 'text': 'Done'}, 'repair-close'),
            Event('work_order.reported', later, {'order': 'I1', 'unit': case.unit, 'problem': 'Inspection', 'kind': 'check', 'by': 'staff:manager'}, 'inspection'),
            Event('work_order.closed', later, {'order': 'I1', 'by': 'staff:manager', 'text': 'Not ready'}, 'inspection-close')])
    client = Client(response(block('release_unit', 'a')), response(block('wait_for', 'b', record='work_orders', what='proof')))
    runtime.run_case(store, STREAM, case.id, later, client=client, check=check(0.1))
    assert 'latest look does not show the unit is ready' in client.calls[1]['messages'][-1]['content'][0]['content']
    assert client.calls[1]['messages'][-1]['content'][0]['is_error']


def test_failed_check_hands_to_staff_and_fixed_uses_checks():
    store, case = setup_case()
    allow(store, 'hold_unit')
    allow(store, 'open_work_order')
    note(store, case, 'Door broken')
    reading = {'read_note': lambda *a: NS(refusal=None, fields={'contents': 'not seen'}, work=[{'words': 'Door broken'}])}
    result = runtime.run_case(store, STREAM, case.id, AT, chooser='fixed', readers=reading,
                              check=lambda *a: NS(probabilities={}, reason='failed'))
    assert result.state == 'needs staff'
    assert 'check failed' in result.steps[-1]['reason']
    assert not any(e.kind == 'work_order.reported' for e in store.events(STREAM))


def test_link_reports_low_probability_is_refused():
    store, case = setup_case()
    allow(store, 'hold_unit')
    allow(store, 'link_reports')
    with store.write():
        store.append(STREAM, [Event('work_order.reported', AT, {'order': 'A', 'unit': case.unit, 'problem': 'Door latch', 'by': 'staff:manager'}, 'a'),
                              Event('work_order.reported', AT, {'order': 'B', 'unit': case.unit, 'problem': 'Door spring', 'by': 'staff:manager'}, 'b')])
    client = Client(response(block('link_reports', 'x', order='B', into='A')),
                    response(block('wait_for', 'y', record='work_orders', what='staff review')))
    runtime.run_case(store, STREAM, case.id, AT, client=client, check=check(0.2))
    result = client.calls[1]['messages'][-1]['content'][0]
    assert result['is_error'] and result['content'] .startswith('not sure these are one fault')
    assert not any(e.kind == 'work_order.joined' for e in store.events(STREAM))


def test_unknown_or_malformed_handoff_arguments_are_refused_not_forwarded():
    store, case = setup_case()
    allow(store, 'hold_unit')
    client = Client(response(block('hand_to_staff', 'a', reason='Look', evidence=[], approval_id='forged')),
                    response(block('hand_to_staff', 'b', reason='Look', evidence='O1')),
                    response(block('wait_for', 'c', record='work_orders', what='staff')))
    result = runtime.run_case(store, STREAM, case.id, AT, client=client, readers=reader(), check=check(0.01))
    first, second = client.calls[1]['messages'][-1]['content'][0], client.calls[2]['messages'][-1]['content'][0]
    assert first.get('is_error') and 'Unknown arguments' in first['content']
    assert second.get('is_error') and 'reason and a list' in second['content']
    assert result.state == 'waiting' and not any(e.kind == 'tilden.approval.used' for e in store.events(STREAM))


def test_a_loose_wait_still_wakes_on_a_new_note_and_one_case_per_unit():
    store, case = setup_case()
    allow(store, 'hold_unit')
    cases.record(store, STREAM, case.id, 'updated', {'state': 'waiting', 'wake': [{'record': 'something vague', 'what': 'x'}]}, AT)
    note(store, case, 'Unit swept and empty, ready to rent.')
    calls = []
    runtime.sweep(store, STREAM, AT, lambda *a: (calls.append(1), NS(choice='wait'))[1])
    assert calls, 'the waiting case was not woken by the new note'
    assert len([c for c in cases.load(store, STREAM).values() if c.unit == case.unit]) == 1


def test_an_error_in_one_case_goes_to_staff_and_the_sweep_goes_on():
    store, case = setup_case()
    allow(store, 'hold_unit')
    def broken(*a):
        raise RuntimeError('boom')
    runtime.sweep(store, STREAM, AT, broken)
    after = cases.load(store, STREAM)[case.id]
    assert after.state == 'needs staff' and 'stopped on an error' in after.steps[-1]['reason']


def test_a_handoff_holds_a_vacant_unit_first():
    store, case = setup_case()
    allow(store, 'hold_unit')
    client = Client(response(block('hand_to_staff', 'a', reason='Nobody has looked at it', evidence=[])))
    result = runtime.run_case(store, STREAM, case.id, AT, client=client, readers=reader(), check=check(0.01))
    assert result.state == 'needs staff'
    assert any(e.kind == 'unit.blocked' and e.payload.get('case_id') == case.id for e in store.events(STREAM))


def test_a_case_handed_to_staff_is_picked_up_again_when_staff_add_records():
    store, case = setup_case()
    allow(store, 'hold_unit')
    cases.record(store, STREAM, case.id, 'updated', {'state': 'needs staff', 'wake': []}, AT)
    calls = []
    runtime.sweep(store, STREAM, AT, lambda *a: (calls.append(1), NS(choice='wait'))[1])
    assert not calls, 'nothing new from staff, so the case stays with staff'
    note(store, case, 'Talked to the former tenant; the items are gone and the unit is clear.')
    runtime.sweep(store, STREAM, AT, lambda *a: (calls.append(1), NS(choice='wait'))[1])
    assert calls and any('picked up again' in str(s) for s in cases.load(store, STREAM)[case.id].steps)


def test_after_a_handoff_release_needs_staff_records_that_settle_it():
    from core.tilden import tools
    store, case = setup_case()
    for a in ('hold_unit', 'release_unit'):
        allow(store, a)
    tools.act('hand_to_staff', store, STREAM, case, AT, reason='Former tenant left a couch', evidence=[])
    note(store, case, 'Swept the hallway outside.')
    runtime.sweep(store, STREAM, AT, lambda *a: NS(choice='wait'))
    settled = lambda *a: NS(probabilities={'yes': 0.1 if 'settle that matter' in a[4] else 0.99}, reason=None)
    result = runtime.run_case(store, STREAM, case.id, AT, lambda *a: NS(choice='release_unit'), check=settled)
    assert any('not show staff settled' in str(s) or 'nothing staff wrote' in str(s) for s in result.steps), result.steps[-3:]


def test_a_note_after_release_reopens_the_case_without_a_new_hold():
    store, case = setup_case()
    allow(store, 'hold_unit')
    cases.record(store, STREAM, case.id, 'updated', {'state': 'done', 'wake': []}, AT)
    note(store, case, 'Neighbor says the smell in the corridor is gone.')
    runtime.sweep(store, STREAM, AT, lambda *a: NS(choice='wait'))
    units = [c for c in cases.load(store, STREAM).values() if c.unit == case.unit]
    assert len(units) == 1 and any('after the unit was released' in str(s) for s in units[0].steps)


def test_a_reopened_case_holds_the_unit_again_before_any_choice():
    from core.tilden import tools
    store, case = setup_case()
    for a in ('hold_unit', 'release_unit'):
        allow(store, a)
    cases.record(store, STREAM, case.id, 'updated', {'state': 'working', 'wake': [],
                 'steps': [{'action': 'hold_unit', 'allowed': True}, {'action': 'release_unit', 'allowed': True}]}, AT)
    runtime.run_case(store, STREAM, case.id, AT, lambda *a: NS(choice='wait'))
    holds = [e for e in store.events(STREAM) if e.kind == 'unit.blocked' and e.payload.get('case_id') == case.id]
    assert holds, 'the reopened case did not hold the unit first'
