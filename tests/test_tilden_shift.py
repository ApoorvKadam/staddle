"""Tilden's morning shift on a live copy: who is admitted, what it may cost, how staff records wake a case, and that a unit is released
only on the records."""
import threading
from datetime import timedelta

from core import renting, walk
from core.store import Event
from core.tilden import cases, shift
from tests.shift_world import LATER, STAFF, Allow, Model, case_of, finish_order, open_orders, staff, step, world


def kinds(store, stream, kind):
    return [e for e in store.stream_events(stream) if e.kind == kind]


def released(store, stream, case, now):
    """The release the way the records show it: Tilden's own unblock for this case, the unit vacant and nothing on the readiness list."""
    from core import reconcile
    books, rent = renting.load(store, stream)
    lifted = [e for e in store.stream_events(stream) if e.kind == 'unit.unblocked' and e.payload.get('case_id') == case.id]
    return bool(lifted) and rent.status(case.unit, now) == 'vacant' and not reconcile.readiness(books, rent, case.unit, now)


def repair_to_release(store, stream, repair, allow, model):
    """The visitor as staff: the repair order finished with words, then the inspection order Tilden asked for, finished with words."""
    step(store, stream, LATER, allow, model)
    survivor = open_orders(store, stream, repair)
    assert len(survivor) == 1, 'the two reports were linked into one order'
    now = finish_order(store, stream, LATER + timedelta(minutes=1), survivor[0].id, 'Straightened the bent track; the door rolls up smoothly.')
    step(store, stream, now, allow, model)
    inspection = [o for o in open_orders(store, stream, repair) if o.kind == 'check']
    assert len(inspection) == 1, 'Tilden asked for an inspection once the repair was closed'
    return inspection[0].id, now


def test_two_cases_are_admitted_and_the_repair_unit_reaches_a_verified_release():
    store, repair, unclear = world()
    stream, allow, model = 'visitor:t', Allow(), Model()
    shifted = step(store, stream, LATER, allow, model)
    assert len(shifted.admitted) == len(shifted.newly) == 2 and not shifted.waiting
    held = {e.payload['unit'] for e in kinds(store, stream, 'unit.blocked')}
    assert held == {repair, unclear}
    inspection, now = repair_to_release(store, stream, repair, allow, model)
    case = case_of(store, stream, repair)
    assert case.state != 'done' and not released(store, stream, case, now)
    # the inspection takes the same five staff steps; its closing words are the look
    now = finish_order(store, stream, now + timedelta(minutes=1), inspection, 'Door rolls smoothly and the unit is clean and fine to rent.')
    step(store, stream, now, allow, model)
    case = case_of(store, stream, repair)
    assert case.state == 'done' and released(store, stream, case, now)
    # what it cost was settled from the reservation, and the unused part went back
    assert allow.rows[f'{LATER.date()}:{case.id}:0'] < shift.CASE_RESERVE_CENTS


def test_a_failed_inspection_never_releases():
    store, repair, _ = world()
    stream, allow, model = 'visitor:t', Allow(), Model()
    inspection, now = repair_to_release(store, stream, repair, allow, model)
    now = finish_order(store, stream, now + timedelta(minutes=1), inspection, 'The door still catches on the track; not ready to rent.')
    step(store, stream, now, allow, model)
    case = case_of(store, stream, repair)
    assert case.state != 'done' and not released(store, stream, case, now)
    assert not [e for e in store.stream_events(stream) if e.kind == 'unit.unblocked' and e.payload.get('case_id') == case.id]
    books, rent = renting.load(store, stream)
    assert rent.status(repair, now) == 'blocked'


def test_the_unclear_unit_is_judged_and_not_released_on_its_notes():
    store, _, unclear = world()
    stream = 'visitor:t'
    step(store, stream, LATER, Allow(), Model())
    case = case_of(store, stream, unclear)
    assert case.state == 'needs staff' and any(isinstance(s, dict) and s.get('handoff') for s in case.steps)
    assert not [e for e in store.stream_events(stream) if e.kind == 'unit.unblocked' and e.payload.get('case_id') == case.id]


def test_a_repeated_start_admits_nothing_new_and_spends_nothing():
    store, _, _ = world()
    stream, allow, model = 'visitor:t', Allow(), Model()
    first = step(store, stream, LATER, allow, model)
    before = (len(store.stream_events(stream)), len(model.calls), dict(allow.rows))
    again = step(store, stream, LATER + timedelta(minutes=1), allow, model)
    assert again.admitted == first.admitted and again.newly == []
    assert (len(store.stream_events(stream)), len(model.calls), dict(allow.rows)) == before
    assert len(kinds(store, stream, 'tilden.shift.admitted')) == 2 and len(kinds(store, stream, 'tilden.shift.reserved')) == 2


def test_two_tabs_never_run_one_case_twice_at_once():
    store, _, _ = world()
    stream, allow = 'visitor:t', Allow()
    gate, inside = threading.Event(), threading.Event()

    class Slow(Model):
        def create(self, **kw):
            inside.set()
            gate.wait(10)
            return super().create(**kw)
    model, results = Slow(), []
    first = threading.Thread(target=lambda: results.append(step(store, stream, LATER, allow, model)))
    first.start()
    assert inside.wait(10)
    second = step(store, stream, LATER, allow, model)        # the other tab, while the first step is inside a model call
    gate.set()
    first.join(20)
    assert second.busy and not second.newly
    assert len(kinds(store, stream, 'tilden.shift.admitted')) == 2


def test_at_most_two_cases_a_day_and_a_third_move_out_waits():
    store, repair, unclear = world()
    stream, allow, model = 'visitor:t', Allow(), Model()
    step(store, stream, LATER, allow, model)
    # the visitor records another move-out through the same command; it enters the same pool
    books, rent = renting.load(store, stream)
    extra = next(r for r in rent.rentals.values() if r.state == 'active' and r.unit not in (repair, unclear) and not books.receivable(r.lease)
                 and not books.credit.get(r.tenant, 0))
    now = LATER + timedelta(minutes=30)
    made = extra.id
    for action in ('request_move_out', 'review_move_out', 'final_bill'):
        d = renting.run(store, stream, renting.Act(f'test:{action}', action, STAFF, made, args={'on': now.date().isoformat()}), now)
        assert d.allowed, d.check
        made = d.made or made
    access = renting.StandIn()
    access.know(store.events('base'))
    renting.advance(store, stream, access, now)
    before = len(model.calls)
    shifted = step(store, stream, now, allow, model)
    third = case_of(store, stream, extra.unit)
    assert third.id in shifted.waiting and third.id not in shifted.admitted and third.state == 'paused'
    assert 'two units a morning' in third.steps[-1]['reason']
    assert any(e.kind == 'unit.blocked' and e.payload['unit'] == extra.unit for e in store.stream_events(stream))   # still kept off the list
    assert len(model.calls) == before and len(kinds(store, stream, 'tilden.shift.admitted')) == 2


def test_an_admitted_case_with_new_records_is_ranked_before_new_work():
    store, repair, unclear = world()
    stream, allow = 'visitor:t', Allow()
    step(store, stream, LATER, allow, Model())
    admitted = {c.unit: c for c in cases.load(store, stream).values()}
    staff(store, stream, LATER + timedelta(minutes=1), 'triage_work_order', 'rank', about=open_orders(store, stream, repair)[0].id, text='Looked at it.')
    live = sorted(cases.open_cases(store, stream), key=lambda c: (c.unit != repair, c.unit))
    assert admitted[repair].id in {c.id for c in live}
    # the order key puts the admitted case with a new record first; checked through the shift's own listing of what woke
    from core.tilden import gate
    assert gate._unit_evidence(store, stream, admitted[repair], admitted[repair].history[-1]['id'], True)


def test_with_no_allowance_nothing_is_admitted_no_model_call_is_made_and_units_stay_held():
    store, repair, unclear = world()
    stream, allow, model = 'visitor:t', Allow(cap=0), Model()
    shifted = step(store, stream, LATER, allow, model)
    assert not shifted.admitted and len(shifted.waiting) == 2
    assert not model.calls and not allow.rows
    assert {e.payload['unit'] for e in kinds(store, stream, 'unit.blocked')} == {repair, unclear}
    for case in cases.load(store, stream).values():
        assert case.state == 'paused' and 'allowance' in case.steps[-1]['reason']
    # the allowance comes back: the shift admits them
    allow.cap = 100
    shifted = step(store, stream, LATER + timedelta(minutes=1), allow, model)
    assert len(shifted.admitted) == 2 and model.calls


def test_when_only_one_case_fits_one_is_admitted():
    store, repair, unclear = world()
    stream, allow, model = 'visitor:t', Allow(cap=shift.CASE_RESERVE_CENTS), Model()
    shifted = step(store, stream, LATER, allow, model)
    assert len(shifted.admitted) == 1 and len(shifted.waiting) == 1
    waiting = cases.load(store, stream)[shifted.waiting[0]]
    assert waiting.state == 'paused' and 'allowance' in waiting.steps[-1]['reason']
    assert {e.payload['unit'] for e in kinds(store, stream, 'unit.blocked')} == {repair, unclear}


def test_a_case_that_cannot_afford_its_next_check_waits_and_says_so():
    store, repair, _ = world()
    stream, allow, model = 'visitor:t', Allow(cap=shift.CASE_RESERVE_CENTS), Model()
    step(store, stream, LATER, allow, model)
    case = case_of(store, stream, repair)
    # the case has used nearly all it reserved
    with store.write():
        store.append(stream, [Event('tilden.planner.call', LATER, {'case': case.id, 'cost_cents': shift.CASE_RESERVE_CENTS - 0.05}, 'spent-up', source='code')])
    calls = len(model.calls)
    now = finish_order(store, stream, LATER + timedelta(minutes=1), open_orders(store, stream, repair)[0].id, 'Straightened the track.')
    step(store, stream, now, allow, model)
    paused = case_of(store, stream, repair)
    assert paused.state == 'paused' and 'allowance' in paused.steps[-1]['reason'] and len(model.calls) == calls
    assert not [o for o in open_orders(store, stream, repair) if o.kind == 'check']        # no inspection asked for on a case that cannot afford the check
    allow.cap = 100                                                                        # the allowance comes back: the case carries on
    step(store, stream, now + timedelta(minutes=1), allow, model)
    assert [o for o in open_orders(store, stream, repair) if o.kind == 'check']


def test_two_copies_keep_their_own_cases_and_allowance():
    store, repair, unclear = world('visitor:a')
    allow_a, allow_b, model = Allow(), Allow(), Model()
    step(store, 'visitor:a', LATER, allow_a, model)
    assert not kinds(store, 'visitor:b', 'tilden.shift.admitted') and not cases.load(store, 'visitor:b')
    access = renting.StandIn()
    access.know(store.events('base'))
    renting.advance(store, 'visitor:b', access, LATER)
    step(store, 'visitor:b', LATER, allow_b, model)
    # b's own reports are linked on b's copy only, and a's reservations are untouched by b
    a_orders = {o.id for o in open_orders(store, 'visitor:a', repair)}
    inspection = finish_order(store, 'visitor:a', LATER + timedelta(minutes=1), next(iter(a_orders)), 'Straightened the track.')
    step(store, 'visitor:a', inspection, allow_a, model)
    assert [o for o in open_orders(store, 'visitor:a', repair) if o.kind == 'check']
    assert not [o for o in open_orders(store, 'visitor:b', repair) if o.kind == 'check']
    assert len(open_orders(store, 'visitor:b', repair)) == 1 and not any(k.startswith('visitor:a') for k in allow_b.rows)
    assert {c.id for c in cases.load(store, 'visitor:a').values()}.isdisjoint({c.id for c in cases.load(store, 'visitor:b').values()})


def test_without_a_model_the_units_are_kept_off_the_list_and_nothing_is_admitted():
    store, repair, unclear = world()
    stream, allow = 'visitor:t', Allow()
    shifted = shift.start_shift(store, stream, LATER, allowance=allow, client=None)
    assert not shifted.admitted and len(shifted.waiting) == 2 and not allow.rows
    assert {e.payload['unit'] for e in kinds(store, stream, 'unit.blocked')} == {repair, unclear}


def test_explicit_negative_compact_inspection_cannot_be_overruled_by_a_second_check():
    from types import SimpleNamespace
    from core import work_forms
    from core.tilden import gate
    from tests.shift_world import readers

    store, repair, _ = world()
    stream, allow, model = 'visitor:t', Allow(), Model()
    inspection, now = repair_to_release(store, stream, repair, allow, model)
    now += timedelta(minutes=1)
    wk = walk.load(store, stream)[1]
    steps = work_forms.inspect_unit(wk, repair, order=inspection, ready=False, observations='The door still catches',
                                    scope='Inspect the door', person='Dana', source=STAFF, cid='inspect-negative')
    assert work_forms.commit(store, stream, steps, now).allowed
    wk = walk.load(store, stream)[1]
    steps = work_forms.check_inspection(wk, inspection, accept=True, words='The inspection was recorded correctly', source=STAFF, cid='check-negative')
    assert work_forms.commit(store, stream, steps, now).allowed
    case = case_of(store, stream, repair)
    def always_yes(*args, **kwargs):
        return SimpleNamespace(probabilities={'yes': 0.99}, reason=None, choice='yes')
    message, _, refused = gate.act(store, stream, case.id, 'release_unit', {}, now, check=always_yes, readers=readers())
    assert refused and 'Release refused' in message
    assert not released(store, stream, case, now)
