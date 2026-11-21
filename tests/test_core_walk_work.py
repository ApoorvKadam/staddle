"""Findings as work: each contradiction between what staff saw and what the FMS records is one item of work (source "walk"), with a
stable key per unit and kind. It closes only when a later observation or a record change makes the records agree, or staff say what
they checked. Nothing is corrected from a comparison: no overlock lifted or recorded, no lease ended, no unit blocked or freed, no money."""
from datetime import timedelta

from core import delinquency as col
from core import renting, work
from core.billing import Ledger
from tests.test_core_walk_compare import EMPTY, LATE, NOW, PAID, STREAM, WALKED, World

MANAGER = "staff:office_manager"


def items(w, now=NOW, stream=STREAM):
    books, rent, wk = work.load(w.store, stream)
    return {i.key: i for i in work.queue(books, rent, wk, now)}


def ask(w, n, action, key, now=NOW, source=MANAGER, **kw):
    return work.run(w.store, STREAM, work.Act(f"k{n}", action, source, key, **kw), now)


def records(w, stream=STREAM):
    books, rent = renting.load(w.store, stream)
    return (dict(col.state(books).overlocks), {k: (v.unit, v.ended) for k, v in books.leases.items()}, dict(rent.blocks), w.store.journal(stream),
            {u: rent.status(u, NOW) for u in books.units})


def test_each_finding_is_one_item_of_work_naming_the_records_that_disagree():
    w = World()
    w.walk(PAID, whose="facility overlock", tag="present")
    i = items(w)[f"walk:{PAID}:overlock_on_paid"]
    assert (i.source, i.kind, i.role, i.state, i.unit, i.tenant, i.lease, i.tone) == (
        "walk", "overlock seen on a paid-up unit", "office_manager", "open", PAID, w.tenants[PAID], w.leases[PAID], "act")
    assert "staff saw the facility overlock" in i.why and "nothing past due for Ana Alvarez" in i.why and "no overlock recorded" in i.why
    assert i.closes and i.attest and i.due == NOW.date()
    assert [k for k in items(w) if k.startswith("walk:")] == [i.key]


def test_the_same_condition_is_one_item_however_often_the_daily_check_runs_and_after_a_restart(tmp_path):
    w = World(str(tmp_path / "copy.db"))
    w.overlock(LATE)
    w.walk(LATE, lock="absent", whose="unknown", tag="absent")
    w.walk(EMPTY, whose="tenant", at=WALKED + timedelta(hours=1))
    first = work.sweep(w.store, STREAM, NOW)
    assert first == 2 and work.sweep(w.store, STREAM, NOW) == 0 and work.sweep(w.store, STREAM, NOW + timedelta(minutes=5)) == 0
    keys = sorted(k for k in items(w) if k.startswith("walk:"))
    assert keys == [f"walk:{LATE}:overlock_not_found", f"walk:{EMPTY}:lock_on_vacant"]
    restarted = World.reopen(w)
    assert work.sweep(restarted.store, STREAM, NOW) == 0
    assert sorted(k for k in items(restarted) if k.startswith("walk:")) == keys


def test_a_later_observation_that_agrees_closes_the_item_and_one_that_disagrees_again_reopens_it():
    w = World()
    w.walk(PAID, whose="facility overlock", tag="present")
    key = f"walk:{PAID}:overlock_on_paid"
    work.sweep(w.store, STREAM, NOW)
    w.walk(PAID, at=WALKED + timedelta(days=1))                     # the next walk finds an ordinary door
    later = NOW + timedelta(days=1)
    done = items(w, later)[key]
    assert done.state == "done" and done.evidence == "A later look found the records agree, or a record changed so they do."
    assert work.sweep(w.store, STREAM, later) == 1                  # the check closes it, once
    assert work.sweep(w.store, STREAM, later) == 0
    w.walk(PAID, whose="facility overlock", tag="present", at=WALKED + timedelta(days=2))
    back = items(w, later + timedelta(days=1))[key]
    assert (back.state, back.cycle) == ("reopened", 2)


def test_a_record_change_that_makes_the_records_agree_closes_the_item():
    w = World()
    w.walk(EMPTY, whose="tenant")                                  # a lock on a unit renting lists as vacant
    key = f"walk:{EMPTY}:lock_on_vacant"
    assert items(w)[key].state == "open"
    work.sweep(w.store, STREAM, NOW)
    with w.store.write():                                           # the unit is rented to the lock's owner
        led = Ledger(w.store.books("base"))
        t = led.add_tenant(NOW, "Dee Diaz", "+14075550001")
        led.sign_lease(NOW, t, EMPTY, 9000, [])
        w.store.append("base", led.pending)
    assert items(w)[key].state == "done"


def test_acknowledging_is_not_finishing_and_staff_close_it_by_saying_what_they_checked():
    w = World()
    w.walk(PAID, door="open")
    key = f"walk:{PAID}:door_open"
    assert ask(w, 1, "acknowledge_work", key).allowed and items(w)[key].state == "open"
    assert not ask(w, 2, "finish_work", key, text="").allowed
    assert ask(w, 3, "finish_work", key, text="Walked back: the door was shut and locked, the earlier look was wrong").allowed
    done = items(w)[key]
    assert done.state == "done" and done.evidence.startswith("Staff checked: Walked back")
    work.sweep(w.store, STREAM, NOW)
    assert items(w)[key].state == "done"                            # the observation still stands, and the item stays done


def test_nothing_is_corrected_from_a_comparison():
    w = World()
    w.overlock(LATE)
    w.walk(PAID, whose="facility overlock", tag="present")             # an overlock on a paid-up unit
    w.walk(LATE, lock="absent", whose="unknown", tag="absent", at=WALKED + timedelta(hours=1))      # an overlock record with none on the door
    w.walk(EMPTY, at=WALKED + timedelta(hours=2))                      # a lock on a vacant unit
    before = records(w)
    work.sweep(w.store, STREAM, NOW)
    keys = [k for k in items(w) if k.startswith("walk:")]
    assert len(keys) == 3
    for n, key in enumerate(keys):
        ask(w, n, "acknowledge_work", key)
        ask(w, f"s{n}", "start_work", key)
        ask(w, f"n{n}", "note_work", key, text="Looking into it")
    assert records(w) == before
    assert w.store.db.execute("SELECT COUNT(*) FROM events WHERE stream = ? AND kind IN ('overlock.recorded', 'overlock.lifted', 'lease.ended', "
                              "'unit.blocked', 'unit.unblocked')", (STREAM,)).fetchone()[0] == 0


def test_a_staff_word_expires_when_its_condition_goes_so_the_same_problem_coming_back_reopens_the_item():
    w = World()
    w.walk(PAID, door="open")
    key = f"walk:{PAID}:door_open"
    assert ask(w, 1, "finish_work", key, text="Walked back: the door was shut and locked").allowed
    work.sweep(w.store, STREAM, NOW)
    assert items(w)[key].state == "done"
    w.walk(PAID, at=WALKED + timedelta(days=1))                     # the next walk agrees: the condition is gone
    later = NOW + timedelta(days=1)
    work.sweep(w.store, STREAM, later)
    assert work.sweep(w.store, STREAM, later) == 0                  # the word expires once, and only once
    w.walk(PAID, door="open", at=WALKED + timedelta(days=2))         # the door is found open again
    back = items(w, later + timedelta(days=1))[key]
    assert (back.state, back.cycle) == ("reopened", 2)
