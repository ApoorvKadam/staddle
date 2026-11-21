"""The staff commands that write the walk and work-order records: one `run`, staff roles only, a command id returns its first
decision and a repeat writes nothing, the unit must exist, values come from the allowed lists, notes are capped, and a copy never
sees another copy's walk. They move no money, lease, block or overlock record."""
from datetime import timedelta

import pytest

from core import walk
from tests.test_core_walk import AT, FRONT, tiny

A, B = "visitor:a", "visitor:b"
MANAGER = "staff:manager"
SEEN = {"lock": "present", "whose": "tenant", "tag": "absent", "door": "closed"}


def ask(store, n, action, stream=A, source=FRONT, now=AT, **kw):
    return walk.run(store, stream, walk.Act(f"c{n}", action, source, **kw), now)


def start(store, n=1, stream=A, route=("A101", "A102", "A103")):
    d = ask(store, n, "start_walk", stream, route=list(route))
    assert d.allowed, d.check
    return d.made


def state(store, stream=A):
    return walk.load(store, stream)[1]


def test_staff_start_a_walk_and_the_command_says_which_walk_it_made():
    store = tiny()
    d = ask(store, 1, "start_walk", route=["A103", "A101"])
    assert (d.allowed, d.made, d.repeated) == (True, "W0001", False)
    w = state(store).walks["W0001"]
    assert (w.route, w.by, w.started) == (("A103", "A101"), FRONT, AT)
    assert ask(store, 2, "start_walk", route=["A102"]).made == "W0002"


def test_only_staff_roles_may_write_a_walk_or_a_work_order_and_a_refusal_writes_no_record():
    store = tiny()
    wid = start(store)
    for n, source in enumerate(["operator", "renter", "code", "fixed_workflow", "staff:janitor", "staff:", "staff"], start=10):
        for action, kw in (("start_walk", {"route": ["A101"]}),
                           ("observe_unit", {"about": wid, "unit": "A101", "form": SEEN}),
                           ("finish_walk", {"about": wid}),
                           ("report_work_order", {"unit": "A101", "text": "Door will not latch"})):
            d = ask(store, f"{n}{action}", action, source=source, **kw)
            assert not d.allowed and "staff" in d.check, (source, action, d.check)
    s = state(store)
    assert list(s.walks) == [wid] and not s.observations and not s.orders and not s.walks[wid].finished


def test_a_walk_needs_real_units_each_once():
    store = tiny()
    assert ask(store, 1, "start_walk", route=[]).check == "a walk needs at least one unit"
    assert ask(store, 2, "start_walk", route=["A101", "Z999"]).check == "there is no unit Z999 at this facility"
    assert ask(store, 3, "start_walk", route=["A101", "A101"]).check == "unit A101 is listed twice"
    assert not state(store).walks


def test_an_observation_is_recorded_once_per_unit_and_a_second_must_be_a_correction():
    store = tiny()
    wid = start(store)
    d = ask(store, 2, "observe_unit", about=wid, unit="A102", form={**SEEN, "whose": "facility overlock", "tag": "present"}, text="  Tag  on the lock ")
    assert d.allowed and d.made == "O0001"
    o = state(store).walks[wid].current("A102")
    assert (o.lock, o.whose, o.tag, o.door, o.note, o.by, o.at, o.evidence) == ("present", "facility overlock", "present", "closed", "Tag on the lock",
                                                                                FRONT, AT, "staff attested")
    again = ask(store, 3, "observe_unit", about=wid, unit="A102", form=SEEN)
    assert not again.allowed and "correction" in again.check and "O0001" in again.check
    fix = ask(store, 4, "correct_observation", about="O0001", form={**SEEN, "lock": "absent", "whose": "unknown"}, text="Looked again, no lock",
              now=AT + timedelta(minutes=3), source=MANAGER)
    assert fix.allowed and fix.made == "O0002"
    s = state(store)
    assert s.walks[wid].current("A102").id == "O0002" and s.observations["O0001"].superseded_by == "O0002"
    assert [o.by for o in s.walks[wid].history("A102")] == [FRONT, MANAGER]
    stale = ask(store, 5, "correct_observation", about="O0001", form=SEEN)
    assert not stale.allowed and "O0002" in stale.check


def test_an_observation_must_be_of_a_unit_on_the_route_of_a_walk_that_is_still_going():
    store = tiny()
    wid = start(store, route=("A101", "A102"))
    assert ask(store, 2, "observe_unit", about="W0099", unit="A101", form=SEEN).check == "there is no such walk"
    assert ask(store, 3, "observe_unit", about=wid, unit="A103", form=SEEN).check == "unit A103 is not on this walk's route"
    assert ask(store, 4, "observe_unit", about=wid, unit="Z999", form=SEEN).check == "there is no unit Z999 at this facility"
    assert ask(store, 5, "correct_observation", about="O0099", form=SEEN).check == "there is no such observation"
    assert ask(store, 6, "observe_unit", about=wid, unit="A101", form=SEEN).allowed
    assert ask(store, 7, "finish_walk", about=wid).allowed
    late = ask(store, 8, "observe_unit", about=wid, unit="A102", form=SEEN)
    assert not late.allowed and late.check == "that walk is finished"
    assert not ask(store, 9, "finish_walk", about=wid).allowed
    w = state(store).walks[wid]
    assert w.finished == (FRONT, AT) and w.not_checked == ("A102",)
    assert ask(store, 10, "correct_observation", about="O0001", form={**SEEN, "door": "open"}).allowed      # a correction is still welcome


def test_values_come_from_the_lists_and_notes_are_capped():
    store = tiny()
    wid = start(store)
    for bad, words in (({"lock": "broken"}, "lock must be one of: present, absent, unknown"),
                       ({"whose": "landlord"}, "whose must be one of: tenant, facility overlock, unknown"),
                       ({"tag": None}, "tag must be one of: present, absent, unknown"),
                       ({"door": "ajar"}, "door must be one of: closed, open, damaged, unknown")):
        d = ask(store, f"v{words}", "observe_unit", about=wid, unit="A101", form={**SEEN, **bad})
        assert (d.allowed, d.check) == (False, words)
    assert ask(store, "e", "observe_unit", about=wid, unit="A101", form={k: v for k, v in SEEN.items() if k != "door"}).check.startswith("door must")
    long = ask(store, "n", "observe_unit", about=wid, unit="A101", form=SEEN, text="x" * (walk.NOTE_MAX + 1))
    assert (long.allowed, long.check) == (False, f"keep it under {walk.NOTE_MAX} characters")
    assert ask(store, "ok", "observe_unit", about=wid, unit="A101", form=SEEN, text="x" * walk.NOTE_MAX).allowed
    assert not state(store).walks[wid].current("A102")


def test_a_work_order_moves_one_step_at_a_time_and_each_move_names_who_and_when():
    store = tiny()
    d = ask(store, 1, "report_work_order", unit="A102", text="Door will not latch")
    assert (d.allowed, d.made) == (True, "WO0001")
    oid = d.made
    skip = ask(store, 2, "close_work_order", about=oid, source=MANAGER)
    assert not skip.allowed and "reported" in skip.check and "triaged" in skip.check
    steps = [("triage_work_order", {}, "triaged"), ("assign_work_order", {"to": "person:Dana"}, "assigned"),
             ("record_order_work", {"text": "Fitted a new latch"}, "work recorded"), ("check_order_work", {"text": "Latches"}, "staff checked"),
             ("close_work_order", {}, "closed")]
    for k, (action, kw, expect) in enumerate(steps, start=3):
        who = MANAGER if k % 2 else FRONT
        assert ask(store, k, action, about=oid, source=who, now=AT + timedelta(hours=k), **kw).allowed
        assert state(store).orders[oid].state == expect
    o = state(store).orders[oid]
    assert [(c.state, c.by, c.at) for c in o.changes][1:] == [(s, MANAGER if k % 2 else FRONT, AT + timedelta(hours=k))
                                                               for k, (_, _, s) in enumerate(steps, start=3)]
    assert o.assigned_to == "person:Dana"
    done = ask(store, 20, "triage_work_order", about=oid)
    assert not done.allowed and "closed" in done.check


def test_a_work_order_needs_a_real_unit_words_and_a_real_assignee():
    store = tiny()
    assert ask(store, 1, "report_work_order", unit="Z999", text="Door will not latch").check == "there is no unit Z999 at this facility"
    assert ask(store, 2, "report_work_order", unit="A101", text=" ").check == "say in a few words what is wrong"
    assert ask(store, 3, "report_work_order", unit="A101", text="x" * (walk.NOTE_MAX + 1)).check == f"keep it under {walk.NOTE_MAX} characters"
    assert ask(store, 4, "triage_work_order", about="WO0009").check == "there is no such work order"
    oid = ask(store, 5, "report_work_order", unit="A101", text="Roof leak over the door").made
    assert ask(store, 6, "triage_work_order", about=oid).allowed
    for to in (None, "plumber", "person:", "person:Dana2"):
        d = ask(store, f"a{to}", "assign_work_order", about=oid, to=to)
        assert (d.allowed, d.check) == (False, "work goes to a staff role or a named person")
    assert ask(store, 7, "assign_work_order", about=oid, to="front_desk").allowed


def test_a_repeated_command_id_returns_its_first_decision_and_writes_nothing():
    store = tiny()
    first = ask(store, 1, "start_walk", route=["A101"])
    refused = ask(store, 2, "start_walk", route=["Z999"])
    count = store.db.execute("SELECT COUNT(*) FROM events WHERE stream = ?", (A,)).fetchone()[0]
    again = ask(store, 1, "start_walk", route=["A102", "A103"], now=AT + timedelta(days=1))        # same id, different ask
    assert (again.allowed, again.repeated, again.made) == (True, True, "W0001") and again.check == first.check
    nope = ask(store, 2, "start_walk", route=["A101"])
    assert (nope.allowed, nope.repeated, nope.check) == (False, True, refused.check)
    assert store.db.execute("SELECT COUNT(*) FROM events WHERE stream = ?", (A,)).fetchone()[0] == count
    assert state(store).walks["W0001"].route == ("A101",) and len(state(store).walks) == 1


def test_a_copy_never_sees_another_copys_walk_or_work_order_and_the_facility_keeps_none_of_it():
    store = tiny()
    wid = start(store, stream=A)
    ask(store, 2, "observe_unit", about=wid, unit="A101", form=SEEN)
    oid = ask(store, 3, "report_work_order", unit="A103", text="Light out in the hall").made
    assert state(store, B).walks == {} and state(store, B).orders == {} and state(store, B).observations == {}
    other = start(store, n=4, stream=B, route=("A102",))
    assert other == wid == "W0001"                                  # each copy numbers its own
    assert list(state(store, B).walks) == ["W0001"] and state(store, B).walks["W0001"].route == ("A102",)
    assert state(store, A).walks["W0001"].route == ("A101", "A102", "A103") and oid in state(store, A).orders
    assert state(store, "base").walks == {}
    assert store.db.execute("SELECT COUNT(*) FROM events WHERE stream = 'base' AND kind LIKE 'walk.%'").fetchone()[0] == 0


def test_the_commands_move_no_money():
    store = tiny()
    wid = start(store)
    ask(store, 2, "observe_unit", about=wid, unit="A101", form=SEEN)
    oid = ask(store, 3, "report_work_order", unit="A101", text="Door will not latch").made
    for k, action in enumerate(["triage_work_order", "assign_work_order", "record_order_work", "check_order_work", "close_work_order"], start=4):
        ask(store, k, action, about=oid, to="front_desk")
    assert store.journal(A) == []
    assert store.db.execute("SELECT COUNT(*) FROM journal_lines").fetchone()[0] == 0


@pytest.mark.parametrize("action", ["observe_unit", "finish_walk", "triage_work_order"])
def test_a_command_about_nothing_is_refused_in_plain_words(action):
    store = tiny()
    d = ask(store, 1, action)
    assert (d.allowed, d.check.startswith("there is no such")) == (False, True)
