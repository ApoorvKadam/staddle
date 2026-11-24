from types import SimpleNamespace as NS

from core.store import Event
from core.tilden import cases, runtime, tools
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
    allow(store, 'request_inspection')      # code's routine steps run after the handoff, so the case shows needs staff instead of an approval wait
    note(store, case, 'Former tenant left a mattress.')
    client = Client(response(block('open_work_order', 'a', text='Remove mattress', answers_record='O1')),
                    response(block('hand_to_staff', 'b', question='Property left behind', record_ids=['O1'], role='front_desk', wake_evidence='staff note')))
    result = runtime.run_case(store, STREAM, case.id, AT, client=client, readers=reader('seen'), check=check(0.9))
    assert result.state == 'needs staff'
    answer = client.calls[1]['messages'][-1]['content'][0]
    assert answer['is_error'] and answer['content'] == 'items a former tenant left are a staff decision'
    assert not any(e.kind == 'work_order.reported' and e.payload.get('kind') != 'check' for e in store.events(STREAM))


def test_refusal_budget_and_restart(monkeypatch):
    store, case = setup_case()
    allow(store, 'hold_unit')
    client = Client(response(stop='refusal'))
    assert runtime.run_case(store, STREAM, case.id, AT, client=client).state == 'blocked'
    store, case = setup_case()
    allow(store, 'hold_unit')
    allow(store, 'request_inspection')      # the routine inspection request runs on its own, so no approval wait hides the model's wait
    client = Client(response(block('note_plan', 'a', steps=['Inspect'])), response(stop='end_turn'))
    assert runtime.run_case(store, STREAM, case.id, AT, client=client).state == 'waiting'
    again = Client(response(stop='refusal'))
    runtime.run_case(store, STREAM, case.id, AT, client=again)
    assert len(again.calls[0]['messages']) == 1 and 'Inspect' in again.calls[0]['messages'][0]['content']
    store, case = setup_case()
    allow(store, 'hold_unit')
    allow(store, 'request_inspection')
    cases.record(store, STREAM, case.id, 'updated', {'steps': [{'decisions': runtime.MAX_DECISIONS}]}, AT)
    client = Client()
    monkeypatch.setattr(runtime, 'MAX_DECISIONS', 0)
    assert runtime.run_case(store, STREAM, case.id, AT, client=client).state == 'paused'
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
    assert result.state == 'blocked'
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
                    response(block('hand_to_staff', 'b', question='Look', record_ids='O1', role='front_desk', wake_evidence='staff note')),
                    response(block('wait_for', 'c', record='work_orders', what='staff')))
    result = runtime.run_case(store, STREAM, case.id, AT, client=client, readers=reader(), check=check(0.01))
    first, second = client.calls[1]['messages'][-1]['content'][0], client.calls[2]['messages'][-1]['content'][0]
    assert first.get('is_error') and 'Unknown arguments' in first['content']
    assert second.get('is_error') and 'record ids' in second['content']
    assert result.state == 'waiting' and not any(e.kind == 'tilden.approval.used' for e in store.events(STREAM))


def test_a_loose_wait_still_wakes_on_a_new_note_and_one_case_per_unit():
    store, case = setup_case()
    allow(store, 'hold_unit')
    cases.record(store, STREAM, case.id, 'updated', {'state': 'waiting', 'wake': [{'record': 'something vague', 'what': 'x'}]}, AT)
    note(store, case, 'Unit swept and empty, ready to rent.')
    client = Client(response(stop='end_turn'))
    runtime.sweep(store, STREAM, AT, client=client)
    assert client.calls, 'the waiting case was not woken by the new note'
    assert len([c for c in cases.load(store, STREAM).values() if c.unit == case.unit]) == 1


def test_an_error_in_one_case_goes_to_staff_and_the_sweep_goes_on(monkeypatch):
    store, case = setup_case()
    allow(store, 'hold_unit')
    with store.write():
        source = store.append(STREAM, [Event('test.source', AT, {}, 'second-source')])[0]
    other = cases.open_case(store, STREAM, source, 'A101', AT)
    ran = []

    def flaky(store, stream, case_id, now, *a, **kwargs):
        # takes what the sweep passes (readers positionally; chooser, client and check by name), so only the error below can fire
        ran.append(case_id)
        if case_id == case.id:
            raise RuntimeError('boom')
        cases.record(store, stream, case_id, 'updated', {'state': 'waiting', 'wake': [{'work_orders': True}]}, now)
    monkeypatch.setattr(runtime, 'run_case', flaky)
    runtime.sweep(store, STREAM, AT)
    broken, fine = cases.load(store, STREAM)[case.id], cases.load(store, STREAM)[other.id]
    assert broken.state == 'blocked' and 'stopped on an error (RuntimeError)' in broken.steps[-1]['reason']
    # the other case was still run after the broken one, and its work was kept
    assert set(ran) == {case.id, other.id} and fine.state == 'waiting'


def test_a_handoff_holds_a_vacant_unit_first():
    store, case = setup_case()
    allow(store, 'hold_unit')
    allow(store, 'request_inspection')
    client = Client(response(block('hand_to_staff', 'a', question='Nobody has looked at it', record_ids=[], role='front_desk', wake_evidence='staff note')))
    result = runtime.run_case(store, STREAM, case.id, AT, client=client, readers=reader(), check=check(0.99))
    assert result.state == 'needs staff'
    assert any(e.kind == 'unit.blocked' and e.payload.get('case_id') == case.id for e in store.events(STREAM))


def test_a_case_handed_to_staff_is_picked_up_again_when_staff_add_records():
    store, case = setup_case()
    allow(store, 'hold_unit')
    cases.record(store, STREAM, case.id, 'updated', {'state': 'needs staff', 'wake': []}, AT)
    client = Client(response(stop='end_turn'))
    runtime.sweep(store, STREAM, AT, client=client)
    assert not client.calls, 'nothing new from staff, so the case stays with staff'
    note(store, case, 'Talked to the former tenant; the items are gone and the unit is clear.')
    runtime.sweep(store, STREAM, AT, client=client)
    assert client.calls and any('picked up again' in str(s) for s in cases.load(store, STREAM)[case.id].steps)


def test_new_staff_evidence_reasks_only_when_unsettled():
    store, case = setup_case()
    allow(store, 'hold_unit')
    allow(store, 'request_inspection')
    tools.act('hand_to_staff', store, STREAM, case, AT, question='What happens to the couch?', record_ids=['old'], role='front_desk', wake_evidence='staff note')
    note(store, case, 'Still deciding about the couch.')
    # an unsettled handoff stays open and the case keeps working: the model judges the new record once, code runs its routine steps
    client = Client(response(stop='end_turn'))
    runtime.sweep(store, STREAM, AT, client=client, check=check(0.1))
    again = cases.load(store, STREAM)[case.id]
    assert len(client.calls) == 1 and again.state == 'needs staff'
    assert any(s.get('unsettled_question') for s in again.steps) and not any(s.get('settled_question') for s in again.steps)
    assert len([s for s in again.steps if s.get('handoff')]) == 1
    count = len(again.history)
    runtime.sweep(store, STREAM, AT, client=Client(), check=check(0.1))
    assert len(cases.load(store, STREAM)[case.id].history) == count
    with store.write():
        store.append(STREAM, [Event('walk.observed', AT, {'walk': 'W1', 'observation': 'O2', 'unit': case.unit, 'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'damaged', 'note': 'The couch was collected.', 'by': 'staff:manager', 'supersedes': None}, 'walk-note-2')])
    client = Client(response(stop='end_turn'))
    runtime.sweep(store, STREAM, AT, client=client, check=check(0.99))
    result = cases.load(store, STREAM)[case.id]
    assert len(client.calls) == 1 and result.state == 'waiting' and any(s.get('settled_question') for s in result.steps)


def test_a_note_after_release_reopens_the_case_without_a_new_hold():
    store, case = setup_case()
    allow(store, 'hold_unit')
    cases.record(store, STREAM, case.id, 'updated', {'state': 'done', 'wake': []}, AT)
    note(store, case, 'Neighbor says the smell in the corridor is gone.')
    runtime.sweep(store, STREAM, AT, client=Client(response(stop='end_turn')))
    units = [c for c in cases.load(store, STREAM).values() if c.unit == case.unit]
    assert len(units) == 1 and any('after the unit was released' in str(s) for s in units[0].steps)


def test_a_reopened_case_holds_the_unit_again_before_any_choice():
    from core.tilden import tools
    store, case = setup_case()
    for a in ('hold_unit', 'release_unit'):
        allow(store, a)
    cases.record(store, STREAM, case.id, 'updated', {'state': 'working', 'wake': [],
                 'steps': [{'action': 'hold_unit', 'allowed': True}, {'action': 'release_unit', 'allowed': True}]}, AT)
    runtime.run_case(store, STREAM, case.id, AT, client=Client(response(stop='end_turn')))
    holds = [e for e in store.events(STREAM) if e.kind == 'unit.blocked' and e.payload.get('case_id') == case.id]
    assert holds, 'the reopened case did not hold the unit first'


def test_paused_wakes_with_fresh_budget_but_lifetime_spend_holds(monkeypatch):
    store, case = setup_case()
    allow(store, 'hold_unit')
    allow(store, 'request_inspection')      # a spend or decision cap stops the model; code's routine steps still run
    monkeypatch.setattr(runtime, 'MAX_DECISIONS', 1)
    first = Client(response(block('note_plan', 'a', steps=['Inspect'])))
    paused = runtime.run_case(store, STREAM, case.id, AT, client=first)
    assert paused.state == 'paused'
    note(store, case, 'The unit was checked.')
    monkeypatch.setattr(runtime, 'MAX_DECISIONS', 2)
    second = Client(response(stop='end_turn'))
    runtime.sweep(store, STREAM, AT, client=second)
    assert second.calls and cases.load(store, STREAM)[case.id].state == 'waiting'
    with store.write():
        store.append(STREAM, [Event('tilden.decision', AT, {'case': case.id, 'cost_cents': runtime.CASE_CENTS}, 'case-cap')])
    result = runtime.run_case(store, STREAM, case.id, AT, client=Client())
    assert result.state == 'paused'


def test_latest_check_controls_repeated_question():
    store, case = setup_case()
    allow(store, 'hold_unit')
    allow(store, 'request_inspection')
    question = 'What happens to the couch?'
    steps = [{'handoff': {'question': question}}, {'settled_question': question, 'evidence_ids': [123], 'p': 0.9}]
    cases.record(store, STREAM, case.id, 'updated', {'steps': steps}, AT)
    args = dict(question=question, record_ids=[], role='front_desk', wake_evidence='staff note')
    client = Client(response(block('hand_to_staff', 'a', **args)), response(stop='end_turn'))
    result = runtime.run_case(store, STREAM, case.id, AT, client=client)
    assert 'settled this question' in client.calls[1]['messages'][-1]['content'][0]['content']
    assert len([s for s in result.steps if s.get('handoff')]) == 1
    steps = result.steps + [{'unsettled_question': question, 'evidence_ids': [124], 'p': 0.1}]
    cases.record(store, STREAM, case.id, 'updated', {'state': 'working', 'steps': steps}, AT)
    result = runtime.run_case(store, STREAM, case.id, AT, client=Client(response(block('hand_to_staff', 'b', **args))))
    handoffs = [s for s in result.steps if s.get('handoff')]
    assert result.state == 'needs staff' and len(handoffs) == 2 and handoffs[-1]['handoff']['question'] == question


def test_a_handoff_the_second_check_finds_routine_is_refused():
    store, case = setup_case()
    allow(store, 'hold_unit')
    note(store, case, 'Cardboard in the back corner, needs a sweep.')
    client = Client(response(block('hand_to_staff', 'a', question='Is the cardboard the tenant\'s?', record_ids=['O1'], role='front_desk', wake_evidence='staff note')),
                    response(block('wait_for', 'b', record='work_orders', what='cleaning')))
    result = runtime.run_case(store, STREAM, case.id, AT, client=client, readers=reader(), check=check(0.1))
    first = client.calls[1]['messages'][-1]['content'][0]
    assert first.get('is_error') and 'routine work' in first['content']
    assert result.state != 'needs staff'
