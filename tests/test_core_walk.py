"""The walk record: what staff saw on the floor, kept apart from what the FMS records.

A walk is a route of units, who walked it, when it started and when it finished (a walk may finish with units not checked; those
stay "not checked"). Each unit gets at most one observation per walk, written as staff attested; a correction is a new observation
that supersedes the earlier one, and nothing is edited or deleted. Work orders carry a repair from reported to closed, each change
with who and when. None of it writes the collections overlock record, a lease, a unit block or money."""
from datetime import date, timedelta

import pytest

from core import delinquency as col
from core import renting, walk
from core.billing import Ledger
from core.books import at_local
from core.gate import Command, evaluate
from core.seed import NOW, build
from core.store import Event, Store

DAY = date(2026, 11, 13)
AT = at_local(DAY, 15, 0)
UNITS = ["A101", "A102", "A103"]
FRONT = "staff:front_desk"


def tiny(units=UNITS):
    """A facility with a few units and nothing else."""
    store = Store()
    led = Ledger()
    led.set_up(at_local(date(2026, 11, 1)), "Northstar Storage", "Orlando, FL", units)
    with store.write():
        store.append("base", led.pending)
    return store


def put(store, stream, n, kind, payload, at=AT, source=FRONT):
    with store.write():
        store.append(stream, [Event(kind, at, payload, f"t{n}", source=source)])


def test_a_walk_keeps_its_route_who_walked_and_when_it_started_and_finished():
    store = tiny()
    put(store, "visitor:a", 1, "walk.started", {"walk": "W0001", "route": ["A101", "A102", "A103"], "by": FRONT})
    _, wk = walk.load(store, "visitor:a")
    w = wk.walks["W0001"]
    assert (w.route, w.by, w.started, w.finished) == (("A101", "A102", "A103"), FRONT, AT, None)
    put(store, "visitor:a", 2, "walk.finished", {"walk": "W0001", "by": "staff:manager"}, AT + timedelta(minutes=40))
    _, wk = walk.load(store, "visitor:a")
    w = wk.walks["W0001"]
    assert w.finished == ("staff:manager", AT + timedelta(minutes=40))


def saw(n, unit, lock="present", whose="tenant", tag="absent", door="closed", note="", supersedes=None, by=FRONT, walk_id="W0001"):
    return {"walk": walk_id, "observation": f"O{n:04d}", "unit": unit, "lock": lock, "whose": whose, "tag": tag, "door": door,
            "note": note, "by": by, "supersedes": supersedes}


def started(store, stream="visitor:a"):
    put(store, stream, 0, "walk.started", {"walk": "W0001", "route": UNITS, "by": FRONT})


def test_an_observation_says_what_was_seen_and_that_staff_attested_it():
    store = tiny()
    started(store)
    put(store, "visitor:a", 1, "walk.observed", saw(1, "A102", lock="present", whose="facility overlock", tag="present", door="damaged",
                                                    note="Latch bent"), AT + timedelta(minutes=9))
    _, wk = walk.load(store, "visitor:a")
    o = wk.walks["W0001"].current("A102")
    assert (o.id, o.unit, o.lock, o.whose, o.tag, o.door, o.note) == ("O0001", "A102", "present", "facility overlock", "present", "damaged", "Latch bent")
    assert (o.by, o.at, o.evidence) == (FRONT, AT + timedelta(minutes=9), "staff attested")
    assert wk.observations["O0001"] is o


def test_a_unit_not_looked_at_stays_not_checked_even_when_the_walk_has_finished():
    store = tiny()
    started(store)
    put(store, "visitor:a", 1, "walk.observed", saw(1, "A102"))
    put(store, "visitor:a", 2, "walk.finished", {"walk": "W0001", "by": FRONT}, AT + timedelta(minutes=30))
    _, wk = walk.load(store, "visitor:a")
    w = wk.walks["W0001"]
    assert w.finished and w.not_checked == ("A101", "A103")
    assert w.current("A101") is None


def test_a_correction_supersedes_the_earlier_observation_and_nothing_is_deleted():
    store = tiny()
    started(store)
    put(store, "visitor:a", 1, "walk.observed", saw(1, "A101", lock="absent", whose="unknown"))
    put(store, "visitor:a", 2, "walk.observed", saw(2, "A101", lock="present", whose="tenant", note="Lock was behind the door", supersedes="O0001"),
        AT + timedelta(minutes=2))
    _, wk = walk.load(store, "visitor:a")
    w = wk.walks["W0001"]
    assert w.current("A101").id == "O0002"
    assert [o.id for o in w.history("A101")] == ["O0001", "O0002"]
    assert wk.observations["O0001"].lock == "absent" and wk.observations["O0001"].superseded_by == "O0002"
    assert w.not_checked == ("A102", "A103")


def test_what_staff_saw_never_changes_the_overlock_record_a_lease_a_block_or_money():
    y = build(today=NOW.date(), cutoff=at_local(NOW.date()), standing=True)
    store = Store()
    with store.write():
        store.append("base", y.led.pending)
    books, rent = renting.load(store, "base")
    lease = next(x for x in books.leases.values() if x.ended is None and col.step_state(books, x.id, "overlock", NOW)[0] == "eligible")
    with store.write():                                     # the FMS records an overlock on this unit, through the collections command
        led = Ledger(store.books("base"))
        cmd = Command("o1", "overlock", lease.tenant, "staff:office_manager", lease=lease.id)
        assert evaluate(led.books, cmd, NOW).allowed
        col.act(led, cmd, NOW)
        store.append("base", led.pending)

    def read(stream):
        b, r = renting.load(store, stream)
        return (dict(col.state(b).overlocks), {k: (v.unit, v.rent_cents, v.ended) for k, v in b.leases.items()}, dict(r.blocks),
                store.journal(stream), {t: b.past_due(t, NOW) for t in b.tenants}, dict(b.credit))
    before = read("visitor:a")
    assert lease.id in before[0]
    put(store, "visitor:a", 0, "walk.started", {"walk": "W0001", "route": [lease.unit], "by": FRONT})
    put(store, "visitor:a", 1, "walk.observed", saw(1, lease.unit, lock="absent", whose="unknown", tag="absent", door="open"))
    put(store, "visitor:a", 2, "walk.observed", saw(2, lease.unit, lock="present", whose="facility overlock", tag="present", supersedes="O0001"))
    put(store, "visitor:a", 3, "walk.finished", {"walk": "W0001", "by": FRONT})
    assert walk.load(store, "visitor:a")[1].walks["W0001"].current(lease.unit).id == "O0002"      # the walk is on record...
    assert read("visitor:a") == before                      # ...and the other records are the same after it as before
    assert store.db.execute("SELECT COUNT(*) FROM journal_lines WHERE stream = 'visitor:a'").fetchone()[0] == 0
