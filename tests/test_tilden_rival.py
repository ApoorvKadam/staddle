"""The fixed-rule readiness worker (core/tilden/rival.py), one case per behaviour, on a small facility. No model and no network."""
import importlib
from datetime import timedelta
from types import SimpleNamespace

import pytest

from core import renting, walk
from core.billing import Ledger
from core.tilden import authority, cases, rival, tools
from core.tilden.rival import read_finish, read_words
from tests.test_core_walk import AT, FRONT, tiny

S = 'visitor:a'
UNIT = 'A102'


def fake_check(store, stream, case_id, kind, question, text, now):
    """Stands in for the gate's second opinion so no network is used: tenant-items work is refused, everything else is agreed with."""
    return SimpleNamespace(probabilities={'yes': 0.05 if 'remove or dispose' in question else 0.9}, reason=None)


class World:
    """A unit whose tenant has moved out, and the staff entries that follow, each one followed by a call to the worker."""

    def __init__(self):
        self.store = tiny()
        self.n = 0
        self.tick = 0
        for action in ('open_work_order', 'link_reports', 'close_report_no_work', 'hold_unit', 'release_unit', 'request_inspection'):
            authority.grant(self.store, S, action, {'unit': UNIT}, AT + timedelta(days=30), 'owner', AT)

    @property
    def now(self):
        self.tick += 1
        return AT + timedelta(minutes=self.tick)

    def arm(self):
        importlib.reload(rival)         # nothing may live in the module between calls: a restart loses it
        rival.arm(self.store, S, self.now, check=fake_check)

    def move_out(self):
        led = Ledger(self.store.books(S))
        tenant = led.add_tenant(AT, 'Pat Lee', '555-0100')
        lease = led.sign_lease(AT - timedelta(days=60), tenant, UNIT, 10000, [])
        led.end_lease(self.now, lease)
        with self.store.write():
            self.store.append(S, led.pending)
        self.arm()

    def staff(self, action, **kw):
        self.n += 1
        d = walk.run(self.store, S, walk.Act(f'w{self.n}', action, FRONT, **kw), self.now)
        assert d.allowed, d.check
        return d.made

    def note(self, text, **form):
        self.n += 1
        walk_id = self.staff('start_walk', route=[UNIT])
        values = {'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'closed', **form}
        self.staff('observe_unit', about=walk_id, unit=UNIT, form=values, text=text)
        self.arm()

    def report(self, text):
        order = self.staff('report_work_order', unit=UNIT, text=text)
        self.arm()
        return order

    def finish(self, order, did='', checked='', closed=''):
        """Triage, assign, record the work (`did`), check it (`checked`) and close (`closed`)."""
        checked, closed = checked or did, closed or did         # the gate reads the words a staff member closed an order with
        for action, text in (('triage_work_order', None), ('assign_work_order', None), ('record_order_work', did),
                             ('check_order_work', checked), ('close_work_order', closed)):
            self.staff(action, about=order, text=text or None, to='front_desk' if action == 'assign_work_order' else None)
        self.arm()

    def orders(self):
        return list(walk.load(self.store, S)[1].orders.values())

    def mine(self):
        return [o for o in self.orders() if o.reported_by == 'agent:tilden']

    def status(self):
        _, rent = renting.load(self.store, S)
        return rent.status(UNIT, AT + timedelta(days=1))

    def ready(self):
        from core import reconcile
        books, rent = renting.load(self.store, S)
        return reconcile.readiness(books, rent, UNIT, AT + timedelta(days=1))

    def case(self):
        return next(iter(cases.load(self.store, S).values()))

    def inspection(self):
        return next(o for o in self.orders() if o.kind == 'check' and o.state != 'closed')


def test_a_clean_unit_gets_a_case_is_held_at_the_move_out_and_released_by_a_look_that_says_ready():
    w = World()
    w.move_out()
    assert w.case().unit == UNIT
    assert w.status() == 'blocked'                       # nothing is known about the unit yet
    w.note('Unit is clean, swept out, no issues.')
    assert w.mine() == [] and w.orders() == []
    assert w.status() == 'vacant' and w.ready() == []


def test_the_unit_is_held_at_the_move_out_and_stays_held_without_a_look():
    w = World()
    w.move_out()
    assert w.status() == 'blocked'
    [held] = [e for e in w.store.stream_events(S) if e.kind == 'unit.blocked']
    assert held.source == 'agent:tilden'
    w.arm()
    w.note('A caller asked if the 5x10 is available next week.')      # a note that says nothing about the unit is not a look
    assert w.status() == 'blocked' and w.mine() == []
    w.note('Unit is clean and ready to rent.')
    assert w.status() == 'vacant'
    assert len([e for e in w.store.stream_events(S) if e.kind == 'unit.blocked']) == 1


def test_a_look_written_before_the_last_repair_does_not_count():
    w = World()
    w.move_out()
    w.note('Roll-up door is off the track, otherwise the unit is clean and fine.')
    [order] = w.mine()
    w.finish(order.id, 'Back on the track', '', 'Rolls smooth')
    assert w.status() == 'blocked'                       # the earlier note was before the repair; the inspection is still to come
    w.finish(w.inspection().id, 'Inspected: door rolls fine, unit is clean, all good.')
    assert w.status() == 'vacant'


def test_one_case_per_move_out_even_when_called_again():
    w = World()
    w.move_out()
    w.arm()
    w.arm()
    assert len(cases.load(w.store, S)) == 1


def test_a_repair_then_an_inspection_that_passes_releases_the_unit():
    w = World()
    w.move_out()
    w.note('Roll-up door is off the track on the left.')
    [order] = w.mine()
    assert 'off the track' in order.problem and w.status() == 'blocked'
    w.finish(order.id, 'Put the door back on the track', '', 'Door rolls smooth')
    check = w.inspection()                                  # asked for once the repair was done, and only once
    w.arm()
    assert len([o for o in w.orders() if o.kind == 'check']) == 1
    assert w.status() == 'blocked' and w.ready()
    w.finish(check.id, 'Inspected: door rolls fine, lock catches, unit is clean.')
    assert w.status() == 'vacant' and w.ready() == []


def test_an_inspection_that_finds_it_still_broken_keeps_the_hold_and_goes_to_staff():
    w = World()
    w.move_out()
    w.note('Latch is bent and will not catch.')
    [first] = w.mine()
    w.finish(first.id, 'Straightened the latch')
    w.finish(w.inspection().id, 'Inspected, latch still does not catch on the left side.')
    assert w.status() == 'blocked'
    assert rival.FAILING in _topics(w) and w.case().state == 'needs staff'
    assert not [o for o in w.mine() if o.kind != 'check' and o.id != first.id]     # no order is opened for what an inspection found
    again = w.report('Latch still broken after the repair.')                        # staff report it; the repair record carries the work
    w.finish(again, 'Replaced the latch, catches fine')
    w.finish(w.inspection().id, 'Inspected: latch catches, door closes and locks. All good.')
    assert w.status() == 'vacant' and w.ready() == []


def test_two_reports_of_one_fault_are_linked_not_duplicated():
    w = World()
    w.move_out()
    first = w.report('Roller cracked, door jumps.')
    second = w.report('Wheel on the door is cracked.')
    orders = {o.id: o for o in w.orders()}
    assert orders[second].joined_into == first and orders[first].state != 'closed'
    assert not [o for o in w.mine() if o.kind != 'check']       # nothing was opened for what staff had already reported
    third = w.report('Lock cylinder spins freely')               # a different part stays its own order
    assert {o.id: o for o in w.orders()}[third].joined_into is None


def test_a_note_about_a_fault_staff_already_reported_opens_nothing_new():
    w = World()
    w.move_out()
    w.report('Seal ripped at the bottom right.')
    w.note('Weatherstrip on the bottom is ripped.')
    assert w.mine() == [] and len(w.orders()) == 1


def test_belongings_are_handed_to_staff_and_the_unit_stays_held():
    w = World()
    w.move_out()
    w.note('Former tenant called, says he left a couch and some boxes inside and will come for them Friday.')
    case = w.case()
    assert case.state == 'needs staff' and 'couch' in _asked(case)
    assert w.mine() == [] and w.status() == 'blocked'
    w.note('Floor is dusty and there are cobwebs.')              # cleaning is still opened, and the unit still waits for a person
    [cleaning] = w.mine()
    w.finish(cleaning.id, 'Swept and mopped')
    assert w.status() == 'blocked' and rival.BELONGINGS in _topics(w)


def test_belongings_cleared_by_staff_let_the_unit_go():
    w = World()
    w.move_out()
    w.note('Boxes of junk left behind in the back.', contents='seen')
    assert w.status() == 'blocked'
    w.note('Former tenant picked up everything, unit is empty.', contents='not seen')
    assert w.status() == 'blocked'                       # cleared, but nobody has said the unit is ready to rent
    w.note('Unit is clean, no issues.', contents='not seen')
    assert w.status() == 'vacant' and w.ready() == []


@pytest.mark.parametrize('words, reason', [
    ('Tenant says the dent was already there when he moved in and wants his deposit back.', 'Dispute'),
    ('Charge the former tenant $75 for the broken panel.', 'Dispute'),
    ('Mouse droppings in the back corner.', 'Pests'),
    ('Smells like gas near the back wall.', 'Safety'),
    ('Police came by about a break-in.', 'Legal'),
    ('Something is off about this unit, not sure what.', 'unclear'),
])
def test_what_only_a_person_can_decide_goes_to_staff(words, reason):
    w = World()
    w.move_out()
    w.note(words)
    assert w.case().state == 'needs staff' and reason in _asked(w.case())
    assert w.status() == 'blocked'
    assert not [o for o in w.mine() if reason == 'Pests']


def test_a_block_staff_placed_is_never_lifted():
    w = World()
    d = renting.run(w.store, S, renting.Act('staff-hold', 'block_unit', FRONT, UNIT, args={'reason': 'Waiting on the owner'}), AT)
    assert d.allowed
    led = Ledger(w.store.books(S))
    tenant = led.add_tenant(AT, 'Pat Lee', '555-0100')
    lease = led.sign_lease(AT - timedelta(days=60), tenant, UNIT, 10000, [])
    led.end_lease(w.now, lease)
    with w.store.write():
        w.store.append(S, led.pending)
    w.arm()
    w.note('Roll-up door is off the track.')
    [order] = w.mine()
    w.finish(order.id, 'Back on the track')
    w.finish(w.inspection().id, 'Inspected, door rolls fine, all good.')
    assert w.status() == 'blocked'
    assert not [e for e in w.store.stream_events(S) if e.kind == 'unit.unblocked']


def test_a_restart_between_calls_loses_nothing():
    w = World()
    w.move_out()
    w.note('Roll-up door is off the track.')
    state = w.case().links
    assert state and state[0]['cursor'] > w.case().source_event       # what it remembers is in the case record
    [order] = w.mine()
    w.arm()                                                          # a call with nothing new writes no second order
    assert len(w.mine()) == 1
    w.finish(order.id, 'Back on the track', '', 'Rolls smooth')
    w.finish(w.inspection().id, 'Door rolls fine, no issues.')
    assert w.status() == 'vacant'


def test_a_repair_closed_with_words_that_say_it_is_not_done_keeps_the_unit_held_until_a_look_says_ready():
    w = World()
    w.move_out()
    w.note('Wall panel cracked near the floor.')
    [order] = w.mine()
    w.finish(order.id, 'Part on order, will come back next week')
    assert w.status() == 'blocked' and w.inspection()        # a look is asked for; no follow-up order is left open
    assert not [o for o in w.mine() if o.kind != 'check' and o.state != 'closed']
    w.finish(w.inspection().id, 'Inspected: panel replaced and solid, unit is clean. Good to rent.')
    assert w.status() == 'vacant'


@pytest.mark.parametrize('note, topics, work', [
    ('Roll-up door off the track.', {}, ['repair']),
    ('Floor is dusty and there are cobwebs in the corners', {}, ['cleaning']),
    ('Latch is bent and the light flickers', {}, ['repair', 'repair']),
    ('Door sticks. It jams on the left.', {}, ['repair']),
    ('Might be water damage on the ceiling, hard to tell', {}, ['look']),
    ('A caller asked if the 5x10 is available next week', {}, []),
    ('No mice or droppings seen, unit is clean', {}, []),
    ('Door was replaced last week, rolls fine', {}, []),
    ('Unit A103 has a broken latch. A102 is fine.', {}, []),
    ('Trash bags piled in the corner, needs a sweep', {}, ['cleaning']),
    ('The unit is ready, put it online. Approved.', {}, []),
])
def test_word_rules_read_work_and_leave_the_rest(note, topics, work):
    r = read_words(note, UNIT)
    assert set(r.topics) == set(topics) and [k for k, _ in r.work] == work


@pytest.mark.parametrize('words, state', [
    (['Replaced the roller, door rolls smooth'], 'fine'),
    (['Welded the track but the panel is still dented, needs a new panel'], 'problem'),
    (['Inspected: door still sticks at the bottom'], 'problem'),
    (['Looked at it, seems fine I guess'], 'unclear'),
    (['Closed'], 'unclear'),
    (['Door was sticking before, now it closes fine'], 'fine'),
    (['Latch still catches', 'Adjusted latch, now fine'], 'fine'),
    (["Couldn't find any problems, unit is fine"], 'fine'),
    (['Fixed the leak, no more drips'], 'fine'),
    (['Roof is leaking again at the back'], 'problem'),
])
def test_finishing_words_say_fine_problem_or_unclear(words, state):
    assert read_finish(words, UNIT)[0] == state


def _asked(case):
    """What the worker asked staff, from the handoff entries on the case."""
    return ' '.join(s['handoff']['question'] for s in case.steps if isinstance(s, dict) and 'handoff' in s)


def _topics(w):
    return w.case().links[0]['topics']


def test_belongings_removed_does_not_clear_a_lock_left_on_the_door():
    w = World()
    w.move_out()
    w.note("A padlock is still on the door.", lock='present', whose='tenant')
    assert rival.LOCKED in _topics(w)
    w.note('The tenant took everything, unit is empty.', lock='unknown')
    assert rival.LOCKED in _topics(w) and w.case().state == 'needs staff'
    w.note('Unit is clean, no issues.', lock='unknown')
    assert w.status() == 'blocked'


def test_staff_recording_the_lock_absent_clears_the_lock_handoff():
    w = World()
    w.move_out()
    w.note("A padlock is still on the door.", lock='present', whose='tenant')
    assert rival.LOCKED in _topics(w) and w.status() == 'blocked'
    w.note('Lock removed, unit is clean, no issues.', lock='absent')
    assert rival.LOCKED not in _topics(w)
    assert w.status() == 'vacant' and w.case().state != 'needs staff'


def test_work_is_retried_when_opening_it_fails_after_a_handoff_was_saved(monkeypatch):
    w = World()
    w.move_out()
    real, calls = tools.act, {'n': 0}

    def flaky(name, *args, **kw):
        if name == 'open_work_order' and calls['n'] == 0:
            calls['n'] = 1
            raise RuntimeError('store went away')
        return real(name, *args, **kw)

    monkeypatch.setattr(tools, 'act', flaky)
    with pytest.raises(RuntimeError):
        w.note('Customer left a couch behind and the roll-up door is off the track.')
    assert not w.mine() and rival.BELONGINGS in _topics(w)
    w.arm()                                              # the next pass reads the same note again and still opens the order, once
    assert len([o for o in w.mine() if o.kind != 'check']) == 1
    w.arm()
    assert len([o for o in w.mine() if o.kind != 'check']) == 1 and list(_topics(w)).count(rival.BELONGINGS) == 1


def test_cleaning_orders_for_different_places_stay_separate():
    w = World()
    w.move_out()
    w.note('Floor is dusty and needs a sweep.')
    w.note('Sticky residue on the walls.')
    clean = [o for o in w.orders() if o.problem.startswith('Cleaning needed')]
    assert len(clean) == 2 and not any(o.joined_into for o in clean)
    w.note('Floor is dusty again, needs sweeping.')       # the same place as an open order opens nothing new
    assert len([o for o in w.orders() if o.problem.startswith('Cleaning needed')]) == 2


def _round(w, did, finding, next_report=None):
    """Finish the open repair with `did`, let the inspection that follows say `finding`, then staff report what it found."""
    order = next(o for o in w.orders() if o.state != 'closed' and o.kind != 'check')
    w.finish(order.id, did)
    w.finish(w.inspection().id, finding)
    return w.report(next_report) if next_report else None


def test_three_inspections_finding_three_different_faults_do_not_say_the_repair_keeps_failing():
    w = World()
    w.move_out()
    w.note('Latch is bent and will not catch.')
    _round(w, 'Adjusted it', 'Inspected, light still flickers.', 'Light flickers in the ceiling')
    _round(w, 'Adjusted it', 'Inspected, hinge still loose.', 'Hinge is loose')
    _round(w, 'Adjusted it', 'Inspected, seal still torn.')
    assert rival.FAILING not in _topics(w)


def test_a_repair_that_said_it_fixed_the_part_and_an_inspection_finding_it_failing_goes_to_staff_at_once():
    w = World()
    w.move_out()
    first = w.report('Roof leaks.')
    w.finish(first, 'Roof patched.')
    w.finish(w.inspection().id, 'Roof still leaks.')
    assert rival.FAILING in _topics(w) and w.case().state == 'needs staff' and w.status() == 'blocked'


def test_an_unfinished_repair_found_failing_is_not_handed_off_early_but_is_after_three_inspections():
    w = World()
    w.move_out()
    w.report('Roof leaks.')
    first = next(o.id for o in w.orders() if o.kind != 'check')
    w.finish(first, "Patched part of it, not finished, roofer will come back.")
    w.finish(w.inspection().id, 'Roof still leaks.')
    assert rival.FAILING not in _topics(w)
    second = w.report('Roof still leaks.')
    w.finish(second, 'Patched another part, not finished.')
    w.finish(w.inspection().id, 'Roof still leaks.')
    assert rival.FAILING not in _topics(w)
    third = w.report('Roof still leaks.')
    w.finish(third, 'Sealed a seam, not finished.')
    w.finish(w.inspection().id, 'Roof still leaks.')
    assert rival.FAILING in _topics(w) and w.case().state == 'needs staff'


def test_a_note_that_defers_to_the_walk_through_is_not_the_look():
    w = World()
    w.move_out()
    w.note("Scrape on the old number sticker at the bottom of the door. Looks ugly but I don't see damage. I'll flag it for the walk-through.")
    assert w.orders() == [] and w.status() == 'blocked'         # a scuffed sticker is not work, and the note is not the look
    w.note('Walked it: door, latch, floor and walls are fine. Empty and clean. Good to rent.')
    assert w.status() == 'vacant'


def test_hearsay_that_the_unit_is_empty_is_not_a_look():
    w = World()
    w.move_out()
    w.note('Former renter called and says everything is clean and fine.')
    assert w.status() == 'blocked'


def test_a_repair_record_that_goes_back_over_the_whole_room_is_the_look():
    w = World()
    w.move_out()
    order = w.report('Hasp screws are stripped, padlock will not stay secure.')
    w.finish(order, 'Installed a new hasp, pulled a padlock firmly to test it. Rechecked the room, still clean and empty with no other issues.')
    assert w.status() == 'blocked'          # the gate wants a record written after the repair finished; the worker waits for one
    assert not [o for o in w.orders() if o.kind == 'check']       # nothing was asked for: the record already holds the look
    w.note('Closed the hasp ticket, unit is back in available inventory.')
    assert w.status() == 'vacant'


def test_a_repair_record_that_only_asks_for_a_check_is_not_the_look():
    w = World()
    w.move_out()
    order = w.report('Ceiling light will not come on.')
    w.finish(order, 'Replaced the fixture, light comes on now. Asked staff to make the final room check.')
    assert w.status() == 'blocked' and w.inspection()
    w.finish(w.inspection().id, 'Light works, room is empty, dry and clean.')
    assert w.status() == 'vacant'


def test_hedged_belongings_wait_for_a_look_and_a_padlock_is_not_belongings():
    w = World()
    w.move_out()
    w.note('Looks like three boxes were left at the back. Might need a junk pickup.')
    assert not _topics(w) and w.status() == 'blocked'
    w.note('Their padlock is still on the door, cannot get inside to check.')
    assert not _topics(w) and w.status() == 'blocked'
    w.note('Padlock cut off with their written permission. Inside is empty and clean, no boxes. Ready.')
    assert w.status() == 'vacant'


def test_a_look_after_a_handoff_answers_it_but_the_same_words_do_not():
    w = World()
    w.move_out()
    w.note('Two crates and a chair are still inside. They might belong to the renter and I do not have permission to throw them out.')
    assert rival.ESCALATE in _topics(w) and w.status() == 'blocked'
    w.note('Former renter says he loaded the chair and both crates.')              # his word is not a look
    assert w.status() == 'blocked'
    w.note('All three items are gone. No other belongings, floor clean and dry, door locks normally.')
    assert w.status() == 'vacant' and w.case().state != 'needs staff'


def test_a_look_that_fixes_the_unit_but_names_a_new_problem_does_not_release():
    w = World()
    w.move_out()
    w.note('Everything is clean and fine, but there is a small propane cylinder in the corner and a supervisor needs to authorize its removal.')
    assert w.case().state == 'needs staff' and w.status() == 'blocked'


@pytest.mark.parametrize('words, labels', [
    ('Need management to decide how to handle the property.', {rival.ESCALATE}),
    ('Manager needs to decide whether to preserve the unit for an adjuster.', {rival.ESCALATE}),
    ('Former renter still owes 85 dollars on the closed account. No lien or hold on this empty space.', set()),
    ('Raised concrete edge near the rear wall, trip hazard. Maintenance needs to level and patch it.', set()),
    ('Manager decided to restore the open layout. Authorized removing the divider and filling the anchor holes.', set()),
    ('Damp blanket left behind and condensation on the lower wall.', set()),
    ('Not sure about this one.', {rival.UNCLEAR}),
])
def test_what_asks_for_a_person_and_what_does_not(words, labels):
    r = read_words(words, UNIT)
    assert set(r.topics) == labels
    if 'Authorized' in words:
        assert r.work == []                      # a manager's ruling is not a new fault


class Counting:
    """A fake second opinion that counts how often each question is asked and answers by keyword."""

    def __init__(self, **answers):
        self.answers = answers          # keyword in the question -> probability
        self.calls = []

    def __call__(self, store, stream, case_id, kind, question, text, now):
        self.calls.append(question)
        p = next((v for k, v in self.answers.items() if k in question), 0.05 if 'remove or dispose' in question else 0.9)
        return SimpleNamespace(probabilities={'yes': p}, reason=None)

    def asked(self, keyword):
        return len([q for q in self.calls if keyword in q])


def world_with(check):
    w = World()
    w.arm = lambda: (importlib.reload(rival), rival.arm(w.store, S, w.now, check=check))
    return w


def test_a_handoff_the_gate_calls_routine_is_dropped_and_not_repeated():
    check = Counting(**{'hand this storage unit': 0.1})
    w = world_with(check)
    w.move_out()
    w.note('Mouse droppings in the back corner.')
    assert w.case().state != 'needs staff' and not _topics(w)       # the gate said routine work: no handoff, and the unit stays held
    assert w.status() == 'blocked'
    for _ in range(3):
        w.arm()
    assert check.asked('hand this storage unit') == 1               # not asked again on every call
    w.note('Unit is clean, no issues.')
    assert w.status() == 'vacant'


def test_a_refused_link_waits_for_a_new_record_before_it_is_tried_again():
    check = Counting(**{'one fault': 0.3})
    w = world_with(check)
    w.move_out()
    first = w.report('Latch will not close.')
    second = w.report('Door latch is broken.')
    orders = {o.id: o for o in w.orders()}
    assert orders[second].joined_into is None and orders[first].state != 'closed'      # the gate was not sure: two orders stay
    asked = check.asked('one fault')
    assert asked >= 1
    for _ in range(3):
        w.arm()
    assert check.asked('one fault') == asked                                          # nothing new was written, so not asked again


def test_a_release_the_gate_refuses_is_not_asked_again_until_something_new_is_written():
    check = Counting(**{'fine to rent again': 0.1})
    w = world_with(check)
    w.move_out()
    w.note('Unit is clean and ready to rent.')
    assert w.status() == 'blocked'
    asked = check.asked('fine to rent again')
    assert asked == 1
    for _ in range(3):
        w.arm()
    assert check.asked('fine to rent again') == asked
    w.note('Walked it a second time: clean, empty, door works. Ready to rent.')                # a new look is a new reason to ask
    assert check.asked('fine to rent again') == asked + 1


def test_work_the_gate_refuses_as_removing_a_tenants_things_goes_to_staff():
    check = Counting(**{'remove or dispose': 0.95})
    w = world_with(check)
    w.move_out()
    w.note('Roll-up door is off the track.')
    assert not w.mine() and rival.BELONGINGS in _topics(w)
    assert check.asked('remove or dispose') == 1


def test_a_report_a_later_look_finds_fine_is_closed_as_needing_no_work():
    w = World()
    w.move_out()
    order = w.report('There is a rattling sound when the door moves.')
    assert w.status() == 'blocked'
    w.note('Walked it. The rattle was a loose number tag on the outside. Door, track and latch are solid, room is clean and empty. Ready to rent.')
    assert w.status() == 'vacant'
    assert next(e for e in w.store.stream_events(S) if e.kind == 'work_order.closed' and e.payload['order'] == order).payload['no_work']


def test_a_report_the_look_does_not_speak_to_stays_open():
    w = World()
    w.move_out()
    w.report('Door latch is broken.')
    w.note('Walked it. Floor is clean and swept, walls are fine. Ready to rent.')
    assert w.status() == 'blocked'
    assert not [e for e in w.store.stream_events(S) if e.kind == 'work_order.closed']


def test_a_ruling_after_a_handoff_is_judged_while_it_is_still_in_view():
    check = Counting()
    w = world_with(check)
    w.move_out()
    w.note('Found a plywood divider screwed into the floor. We need management to decide whether to keep it or remove it.')
    w.note('Manager decided to restore the open layout. Authorized removing the divider. Keep the unit blocked until done.')
    order = w.report('Remove the plywood divider and patch the anchor holes.')
    w.finish(order, 'Took down the divider, removed the anchors and patched the holes.')
    assert check.asked('Do the staff records') >= 1                 # asked when the work finished, before a long repair log hides the ruling
    w.finish(w.inspection().id, 'Divider gone, floor patches solid, no debris. Door works, room is empty and dry.')
    assert w.status() == 'vacant'


def test_the_practice_and_pilot_records_replay_with_no_early_release_and_no_needless_handoff():
    """The two sets of authored records (core/tilden/replay.py scores them): practice must be right in full; pilot may only miss
    the three records whose reported order can be closed by no tool a worker has."""
    import re
    from core.tilden import replay
    practice = replay.parse(open('tests/fixtures/unit_ready_practice.txt').read())
    totals = replay.totals([replay.score(c, replay.replay(c, lambda store, stream, now: rival.arm(store, stream, now, check=fake_check))) for c in practice])
    assert totals['release_right'] == len(practice) and totals['released_early'] == 0
    assert totals['missed_handoff'] == 0 and totals['premature_handoff'] == 0 and totals['needless_handoff'] == 0
    text = open('tests/fixtures/unit_ready_pilot.txt').read()
    blocks = re.split(r'(?m)^\s*(?=U\d+\.\s*$)', text.strip())
    pilot = replay.parse('\n'.join(b for b in blocks if not re.match(r'\s*U30\.', b)))
    totals = replay.totals([replay.score(c, replay.replay(c, lambda store, stream, now: rival.arm(store, stream, now, check=fake_check))) for c in pilot])
    assert totals['released_early'] == 0 and totals['needless_handoff'] == 0 and totals['premature_handoff'] == 0
    assert totals['missed_handoff'] == 0 and totals['release_right'] >= 26


# ---- review fixes: partial work, refused work orders that stay wanted, no-work closures ---------------------------------------
@pytest.mark.parametrize('words', [
    ['Fixed half of the latch'],
    ['Partly fixed the track, the rest is still to do'],
    ['Only one of the two hinges was replaced'],
    ['Replaced one of the three hinges, the others wait for parts'],
])
def test_explicit_partial_work_is_unfinished_not_a_fix(words):
    assert read_finish(words, UNIT)[0] == 'problem'


@pytest.mark.parametrize('words', [
    ['Replaced the roller, the door sits half an inch higher now and rolls smooth'],
    ['Fixed the latch, it sits half an inch lower now and the door closes fine'],
    ['Adjusted the track, it moves only one inch either way and the door rolls fine'],
])
def test_a_measurement_with_half_or_one_in_it_is_not_unfinished_work(words):
    assert read_finish(words, UNIT)[0] == 'fine'


def _door_orders(w):
    return [o for o in w.mine() if o.kind != 'check']


def _damaged_door_refused():
    """A walk note with the door marked damaged, while the gate's second check says the work would remove a tenant's things."""
    check = Counting(**{'remove or dispose': 0.95})
    w = world_with(check)
    w.move_out()
    w.note('Panel is dented near the bottom.', door='damaged')
    assert not _door_orders(w) and rival.BELONGINGS in _topics(w)          # refused: the unit stays held and staff are asked
    return w, check


def test_a_refused_work_order_for_a_damaged_door_is_opened_when_a_later_staff_record_makes_it_possible():
    w, check = _damaged_door_refused()
    w.arm()
    assert not _door_orders(w)                                             # nothing new was written: not tried again
    check.answers['remove or dispose'] = 0.05                              # the second check no longer sees tenant items
    w.report('Light flickers in the ceiling.')                             # a new staff record, saying nothing about the door
    [order] = _door_orders(w)
    assert 'dented' in order.problem
    w.arm()
    w.arm()
    assert len(_door_orders(w)) == 1


def test_a_refused_work_order_is_not_retried_when_staff_corrected_the_observation_it_answered():
    w, check = _damaged_door_refused()
    check.answers['remove or dispose'] = 0.05
    obs = max(walk.load(w.store, S)[1].observations)
    w.staff('correct_observation', about=obs, form={'lock': 'absent', 'whose': 'unknown', 'tag': 'absent', 'door': 'closed'},
            text='Panel is fine, nothing wrong with the door.')
    w.arm()
    assert not _door_orders(w)


def test_a_refused_work_order_is_not_retried_when_a_later_look_finds_the_door_fine():
    w, check = _damaged_door_refused()
    check.answers['remove or dispose'] = 0.05
    w.note('Walked it again. Door and panel are fine, nothing wrong. Room is clean and empty.')
    assert not _door_orders(w)


def test_a_work_order_that_went_through_on_retry_is_not_opened_again_after_the_repair():
    w, check = _damaged_door_refused()
    check.answers['remove or dispose'] = 0.05
    w.report('Light flickers in the ceiling.')
    [order] = _door_orders(w)
    w.finish(order.id, 'Replaced the dented panel')
    w.report('Latch is bent and will not catch.')
    assert len(_door_orders(w)) == 1                                        # the panel order stands alone
    assert sum('dent' in o.problem for o in _door_orders(w)) == 1


def test_a_refused_work_order_is_not_retried_when_a_newer_walk_sees_the_door_undamaged():
    w, check = _damaged_door_refused()
    check.answers['remove or dispose'] = 0.05
    w.note('Customer called about parking.', door='closed')
    assert not any('dent' in o.problem for o in _door_orders(w))


def test_a_report_closed_with_no_work_is_not_the_last_repair_for_a_later_failed_repair_handoff():
    w = World()
    w.move_out()
    light = w.report('Light flickers in the ceiling.')
    w.finish(light, 'Replaced the bulb and the fixture.')                  # an inspection is now asked for
    rattle = w.report('There is a rattling sound when the door moves.')
    w.note('Walked it. The rattle was a loose number tag on the outside. Door, track and latch are solid, room is clean and empty. Ready to rent.')
    closed = next(e for e in w.store.stream_events(S) if e.kind == 'work_order.closed' and e.payload['order'] == rattle)
    assert closed.payload['no_work']                                       # nothing was repaired for the rattle
    w.finish(w.inspection().id, 'Inspected: the door still rattles at the track.')
    assert rival.FAILING not in _topics(w)                                 # the only repair was the light, which this fault is not about
