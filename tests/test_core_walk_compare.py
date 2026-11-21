"""The comparison: each unit's standing observation read against the FMS records (the collections overlock record, whether the lease
has anything past due, the unit's renting status, open work orders). Each contradiction is one finding. An unknown is never agreement
and never a contradiction: it is one finding asking staff to look again. Nothing here writes a record."""
from datetime import date, timedelta

from core import reconcile, renting, walk
from core.billing import Ledger
from core.books import at_local
from core.evidence import clock
from core.store import Event, Store

NOW = at_local(date(2026, 11, 14), 9)
WALKED = at_local(date(2026, 11, 13), 15)
FRONT, MANAGER = "staff:front_desk", "staff:office_manager"
STREAM = "visitor:a"
UNITS = ["A101", "A102", "A103", "A104", "A105"]
PAID, LATE, EMPTY, MOVING, SECOND = UNITS[:5]


class World:
    """A facility: A101 rented and paid up, A102 rented and 25 days past due, A103 and A105 vacant, A104 rented and paid up."""

    def __init__(self, path=":memory:"):
        led = Ledger()
        led.set_up(at_local(date(2026, 10, 1)), "Northstar Storage", "Orlando, FL", UNITS)
        self.tenants, self.leases = {}, {}
        for unit, who, pays in ((PAID, "Ana Alvarez", True), (LATE, "Ben Brooks", False), (MOVING, "Cara Chen", True)):
            t = led.add_tenant(at_local(date(2026, 10, 19)), who, "+14075550000")
            self.tenants[unit], self.leases[unit] = t, led.sign_lease(at_local(date(2026, 10, 20), 10), t, unit, 12000, [])
            if pays:
                led.receive_payment(t, 12000, at_local(date(2026, 10, 20), 10, 5))
        self.store = Store(path)
        with self.store.write():
            self.store.append("base", led.pending)
        self.n, self.path = 0, path

    @classmethod
    def reopen(cls, w):
        """The same database read by a new process: nothing is kept but what was written."""
        new = cls.__new__(cls)
        new.store, new.n, new.path = Store(w.path), w.n, w.path
        new.tenants, new.leases = w.tenants, w.leases
        return new

    def base(self, kind, payload, at=WALKED - timedelta(days=3)):
        self.n += 1
        with self.store.write():
            self.store.append("base", [Event(kind, at, payload, f"w{self.n}", source=MANAGER)])

    def overlock(self, unit, at=WALKED - timedelta(days=3)):
        self.base("overlock.recorded", {"lease": self.leases[unit], "tenant": self.tenants[unit], "by": MANAGER, "record_only": True}, at)

    def moving_out(self, unit):
        with self.store.write():
            books, rent = renting.load(self.store, "base")
            led = renting.RentLedger(books, rent)
            rid = renting.adopt_existing_lease(led, self.leases[unit], "seeded_existing", NOW)
            led.emit("moveout.requested", NOW, {"moveout": "MO0001", "rental": rid, "on": "2026-11-20"}, "mo1", source=FRONT)
            led.emit("rental.ending", NOW, {"rental": rid, "reason": "move-out"}, "end1", source=FRONT)
            self.store.append("base", led.pending)

    def walk(self, *units, stream=STREAM, at=WALKED, **seen):
        """A walk over the units; each is observed with the same values (the defaults say a tenant's lock, no tag, a closed door)."""
        self.n += 1
        d = walk.run(self.store, stream, walk.Act(f"s{self.n}", "start_walk", FRONT, route=list(units)), at)
        assert d.allowed, d.check
        form = {"lock": "present", "whose": "tenant", "tag": "absent", "door": "closed", **seen}
        for i, u in enumerate(units):
            o = walk.run(self.store, stream, walk.Act(f"o{self.n}{u}", "observe_unit", FRONT, about=d.made, unit=u, form=form), at + timedelta(minutes=9 * (i + 1)))
            assert o.allowed, o.check
        return d.made

    def found(self, stream=STREAM, now=NOW):
        books, rent = renting.load(self.store, stream)
        return reconcile.compare(books, rent, now)


def slugs(found):
    return sorted((f.unit, f.slug) for f in found)


def test_a_facility_overlock_on_the_door_of_a_unit_whose_tenant_owes_nothing_past_due_is_one_finding():
    w = World()
    w.walk(PAID, whose="facility overlock", tag="present")
    (f,) = w.found()
    assert (f.unit, f.slug, f.role, f.tenant, f.lease) == (PAID, "overlock_on_paid", "office_manager", "Ana Alvarez", w.leases[PAID])
    assert f.why.startswith(f"On {clock(WALKED + timedelta(minutes=9))}, staff saw the facility overlock on the door of unit {PAID}.")
    assert "nothing past due for Ana Alvarez" in f.why and "no overlock recorded" in f.why
    assert f.key == f"walk:{PAID}:overlock_on_paid" and f.due == date(2026, 11, 14)
    w.overlock(PAID)                                   # the record says an overlock too: the ledger still shows nothing past due
    (g,) = w.found()
    assert g.slug == "overlock_on_paid" and "overlock on unit A101 recorded Nov 10" in g.why


def test_a_tag_alone_counts_as_the_facility_overlock_and_a_tenants_own_lock_does_not():
    w = World()
    w.walk(PAID, lock="absent", whose="unknown", tag="present")
    assert slugs(w.found()) == [(PAID, "overlock_on_paid")]
    w2 = World()
    w2.walk(PAID)
    assert w2.found() == []


def test_an_overlock_seen_on_a_unit_that_owes_agrees_with_the_ledger_whether_or_not_it_is_recorded():
    w = World()
    w.walk(LATE, whose="facility overlock", tag="present")
    assert w.found() == []
    w.overlock(LATE)
    assert w.found() == []


def test_an_overlock_in_the_fms_and_no_lock_or_tag_on_the_door_is_one_finding():
    w = World()
    w.overlock(LATE)
    w.walk(LATE, lock="absent", whose="unknown", tag="absent")
    (f,) = w.found()
    assert (f.unit, f.slug, f.role) == (LATE, "overlock_not_found", "office_manager")
    assert "overlock on unit A102 recorded Nov 10" in f.why and "no lock or tag" in f.why and "25 days" in f.why
    assert "Ben Brooks" in f.why


def test_an_overlock_in_the_fms_agrees_with_a_tag_or_the_facilitys_lock_and_a_tenants_lock_is_neither_agreement_nor_contradiction_here():
    for seen in ({"whose": "facility overlock", "tag": "present"}, {"lock": "absent", "whose": "unknown", "tag": "present"}, {"whose": "facility overlock"},
                 {"whose": "tenant", "tag": "absent"}):
        w = World()
        w.overlock(LATE)
        w.walk(LATE, **seen)
        assert w.found() == [], seen


def test_a_lock_that_is_the_tenants_or_unknown_on_a_vacant_unit_is_one_finding_and_the_facilitys_own_lock_is_not():
    for whose in ("tenant", "unknown"):
        w = World()
        w.walk(EMPTY, whose=whose)
        (f,) = w.found()
        assert (f.unit, f.slug, f.role, f.lease) == (EMPTY, "lock_on_vacant", "front_desk", None)
        assert "renting lists as vacant" in f.why and "staff saw a lock" in f.why
    for seen in ({"whose": "facility overlock", "tag": "present"}, {"lock": "absent", "whose": "unknown"}):
        w = World()
        w.walk(EMPTY, **seen)
        assert w.found() == [], seen


def test_a_rented_unit_with_its_door_open_or_no_lock_and_no_move_out_is_one_finding():
    for seen in ({"door": "open"}, {"lock": "absent", "whose": "unknown"}):
        w = World()
        w.walk(PAID, **seen)
        (f,) = w.found()
        assert (f.unit, f.slug, f.role) == (PAID, "door_open", "front_desk")
        assert "no move-out is recorded" in f.why and "Ana Alvarez" in f.why
    w = World()
    w.walk(PAID, door="damaged")                       # a damaged door is a maintenance matter, not a contradiction with the records
    assert w.found() == []


def test_a_unit_with_a_move_out_recorded_is_not_flagged_for_an_open_door_or_no_lock():
    w = World()
    w.moving_out(MOVING)
    w.walk(MOVING, door="open", lock="absent", whose="unknown")
    assert w.found() == []


def test_a_missing_lock_on_a_unit_with_an_overlock_record_is_one_finding_not_two():
    w = World()
    w.overlock(LATE)
    w.walk(LATE, lock="absent", whose="unknown", tag="absent")
    assert slugs(w.found()) == [(LATE, "overlock_not_found")]


def test_an_unknown_is_never_agreement_and_never_a_contradiction_it_is_one_item_asking_to_look_again():
    w = World()
    w.walk(PAID, tag="unknown")
    (f,) = w.found()
    assert (f.unit, f.slug) == (PAID, "look_again") and "whether the overlock tag was there" in f.why and "look again" in f.closes.lower()
    w2 = World()
    w2.walk(EMPTY, lock="unknown", whose="unknown", tag="unknown", door="unknown")
    (g,) = w2.found()
    assert g.slug == "look_again" and all(x in g.why for x in ("whether a lock was on the door", "whether the overlock tag was there", "the state of the door"))
    w3 = World()
    w3.walk(PAID, whose="unknown")                      # a lock whose owner nobody could tell
    assert slugs(w3.found()) == [(PAID, "look_again")]
    w4 = World()
    w4.walk(PAID, whose="facility overlock", tag="present", door="unknown")     # a contradiction already asks staff to look: no second item
    assert slugs(w4.found()) == [(PAID, "overlock_on_paid")]


def test_a_unit_never_walked_has_no_finding_and_ordinary_units_agree():
    w = World()
    w.walk(PAID)
    w.walk(EMPTY, lock="absent", whose="unknown", at=WALKED + timedelta(hours=1))
    assert w.found() == []


def test_the_standing_observation_is_the_newest_walks_and_a_correction_supersedes_it():
    w = World()
    first = w.walk(PAID, EMPTY, at=WALKED - timedelta(days=2), whose="facility overlock", tag="present")
    assert slugs(w.found()) == [(PAID, "overlock_on_paid")]
    w.walk(PAID, at=WALKED)                             # the newer walk finds an ordinary door: that stands now
    assert w.found() == []
    _, wk = walk.load(w.store, STREAM)
    assert wk.standing(PAID).walk != first and wk.standing(EMPTY).walk == first      # a unit the newer walk skipped keeps its older reading
    fix = walk.run(w.store, STREAM, walk.Act("fix", "correct_observation", FRONT, about=wk.standing(EMPTY).id,
                                              form={"lock": "absent", "whose": "unknown", "tag": "absent", "door": "closed"}), WALKED + timedelta(hours=2))
    assert fix.allowed
    assert slugs(w.found()) == []                       # the corrected reading agrees; the one it superseded no longer stands


def test_the_comparison_writes_nothing():
    w = World()
    w.walk(PAID, whose="facility overlock", tag="present")
    count = w.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    w.found(), w.found()
    assert w.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == count
    assert w.store.journal(STREAM) == [] and w.store.db.execute("SELECT COUNT(*) FROM events WHERE kind LIKE 'overlock.%'").fetchone()[0] == 0
