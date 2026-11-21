"""Unit readiness, derived at the moment it is asked and never stored: a unit is ready to offer only when renting calls it vacant, no walk
contradiction stands on it, no work order on it is open, no lease on it is active, no hold or legal stop covers it, and its gate state is
clear. It answers with every failing reason in plain words. Renting's unit gate refuses offering, reserving or a move-in on a unit that is
not ready with the first reason; closing a work order removes only its own reason."""
from datetime import timedelta

from core import reconcile, renting, walk
from core.billing import Ledger
from core.renting import Act
from core.store import BASE
from tests import test_core_renting as rr
from tests.test_core_walk_compare import EMPTY, FRONT, LATE, MOVING, NOW, PAID, SECOND, STREAM, WALKED, World

MANAGER = "staff:office_manager"
SEEN = {"lock": "present", "whose": "tenant", "tag": "absent", "door": "closed"}


class Floor(World):
    """The World with every vacant unit priced, so the unit gate can be asked."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.k = 0
        for u in (EMPTY, SECOND):
            self.rent_cmd("price_unit", u, source="staff:manager", rent_cents=9000, size="5x10")

    def rent_cmd(self, action, about, tenant=None, source="renter", now=NOW, **args):
        self.k += 1
        return renting.run(self.store, STREAM, Act(f"x{self.k}", action, source, about, tenant, args), now)

    def ready(self, unit, now=NOW):
        books, rent = renting.load(self.store, STREAM)
        return reconcile.readiness(books, rent, unit, now)

    def report(self, unit, text="Door will not latch"):
        self.k += 1
        d = walk.run(self.store, STREAM, walk.Act(f"y{self.k}", "report_work_order", FRONT, unit=unit, text=text), WALKED)
        assert d.allowed, d.check
        return d.made

    def move(self, order, *steps):
        for action, kw in steps:
            self.k += 1
            d = walk.run(self.store, STREAM, walk.Act(f"z{self.k}", action, MANAGER, about=order, **kw), WALKED + timedelta(hours=self.k))
            assert d.allowed, d.check

    def close(self, order):
        self.move(order, ("triage_work_order", {}), ("assign_work_order", {"to": "front_desk"}), ("record_order_work", {"text": "Fixed"}),
                  ("check_order_work", {"text": "Checked"}), ("close_work_order", {}))

    def reserve(self, unit, source="renter"):
        return self.rent_cmd("reserve", unit, self.tenants[PAID], source=source)


def test_a_vacant_unit_nothing_stands_on_is_ready_and_can_be_reserved():
    w = Floor()
    assert w.ready(EMPTY) == []
    assert w.reserve(EMPTY).allowed
    assert w.ready(EMPTY) == ["the unit is reserved"]               # the hold is a reason too


def test_a_rented_unit_says_so_and_keeps_renting_s_own_words():
    w = Floor()
    assert w.ready(PAID) == ["the unit is occupied"]
    w.rent_cmd("price_unit", PAID, source="staff:manager", rent_cents=9000, size="5x10")
    assert w.reserve(PAID).check == "the unit is occupied"


def test_an_open_work_order_makes_the_unit_not_ready_and_the_gate_refuses_with_the_reason():
    w = Floor()
    oid = w.report(EMPTY)
    why = f"work order {oid} is still open (Door will not latch)"
    assert w.ready(EMPTY) == [why]
    d = w.reserve(EMPTY)
    assert (d.allowed, d.check) == (False, why)
    assert w.ready(SECOND) == []                                    # another unit is not touched by it


def test_a_walk_contradiction_on_the_unit_makes_it_not_ready_until_a_later_look_agrees():
    w = Floor()
    w.walk(EMPTY, whose="tenant")                                   # a lock on a vacant unit
    (reason,) = w.ready(EMPTY)
    assert reason.startswith("a walk finding stands on the unit: a lock on the door of unit A103")
    assert w.reserve(EMPTY).check == reason
    w.walk(EMPTY, lock="absent", whose="unknown", at=WALKED + timedelta(days=1))
    assert w.ready(EMPTY, NOW + timedelta(days=1)) == []
    assert w.reserve(EMPTY).allowed


def test_a_look_again_item_alone_does_not_stop_an_offer():
    w = Floor()
    w.walk(EMPTY, lock="unknown", whose="unknown")
    assert [f.slug for f in w.found()] == ["look_again"] and w.ready(EMPTY) == []


def test_closing_a_work_order_removes_only_its_own_reason_and_the_unit_is_offered_when_every_reason_is_gone():
    w = Floor()
    w.walk(EMPTY, whose="tenant")
    oid = w.report(EMPTY)
    finding, order = w.ready(EMPTY)[0], w.ready(EMPTY)[1]
    assert finding.startswith("a walk finding") and order.startswith(f"work order {oid}")
    w.close(oid)
    assert w.ready(EMPTY, NOW + timedelta(days=1)) == [finding]       # the order's reason is gone; the finding's is not
    assert w.reserve(EMPTY).check == finding
    w.walk(EMPTY, lock="absent", whose="unknown", at=WALKED + timedelta(days=1))
    assert w.ready(EMPTY, NOW + timedelta(days=1)) == [] and w.reserve(EMPTY).allowed


def test_every_failing_reason_is_listed_in_plain_words_in_order():
    w = Floor()
    w.walk(PAID, door="open")
    oid = w.report(PAID, "Roof leaks over the door")
    assert w.ready(PAID) == ["the unit is occupied",
                             "a walk finding stands on the unit: rented unit A101 seen with its door open or no lock",
                             f"work order {oid} is still open (Roof leaks over the door)"]


def test_a_lease_that_is_still_active_is_a_reason_when_renting_does_not_already_say_the_unit_is_occupied():
    w = Floor()
    w.reserve(EMPTY)                                                # renting calls it reserved ...
    with w.store.write():                                           # ... and a lease on it is signed outside renting
        led = Ledger(w.store.books(STREAM))
        t = led.add_tenant(NOW, "Dee Diaz", "+14075550001")
        led.sign_lease(NOW, t, EMPTY, 9000, [])
        w.store.append(STREAM, led.pending)
    assert w.ready(EMPTY) == ["the unit is reserved", "a lease on the unit is still active"]


def test_a_legal_stop_or_an_open_lien_file_on_the_unit_is_a_reason():
    w = Floor()
    lease = w.leases[MOVING]
    with w.store.write():                                           # the tenant moved out, the unit is vacant, a stop is still on its record
        led = Ledger(w.store.books("base"))
        led.end_lease(WALKED - timedelta(days=5), lease)
        led.emit("lien_case.stop_recorded", WALKED, {"stop": "LS0001", "lease": lease, "tenant": w.tenants[MOVING], "kind": "bankruptcy",
                                                     "detail": "Notice from the court", "source": "staff", "by": MANAGER, "case": None}, "stop1")
        w.store.append("base", led.pending)
    assert w.ready(MOVING) == ["a legal stop covers the unit (a bankruptcy)"]
    with w.store.write():
        led = Ledger(w.store.books("base"))
        led.emit("lien_case.stop_resolved", WALKED, {"stop": "LS0001", "reason": "Dismissed", "by": MANAGER, "case": None}, "stop2")
        led.emit("lien_case.opened", WALKED, {"case": "LC0001", "lease": lease, "tenant": w.tenants[MOVING], "unit": MOVING, "episode": 1, "by": MANAGER}, "case1")
        w.store.append("base", led.pending)
    assert w.ready(MOVING) == ["a lien file is open on the unit"]


def test_an_old_gate_code_that_is_not_revoked_is_a_reason_after_a_rental_ends():
    w = Floor()
    with w.store.write():
        books, rent = renting.load(w.store, "base")
        led = renting.RentLedger(books, rent)
        rid = renting.adopt_existing_lease(led, w.leases[MOVING], "seeded_existing", NOW)
        led.end_lease(NOW, w.leases[MOVING])
        led.emit("rental.ended", NOW, {"rental": rid}, "ended1", source=FRONT)
        w.store.append("base", led.pending)
    assert w.ready(MOVING) == ["the unit is blocked", "the gate system has not been checked for a code from before Staddle"]


def test_readiness_is_read_when_asked_and_stores_nothing():
    w = Floor()
    w.walk(EMPTY, whose="tenant")
    count = w.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    w.ready(EMPTY), w.ready(EMPTY), w.ready(SECOND)
    assert w.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == count
    assert not hasattr(renting.Rent, "ready") and w.store.journal(STREAM) == []


def test_a_move_in_is_refused_while_a_fault_reported_after_the_hold_is_open_and_goes_through_once_it_is_closed():
    store, dana, _ = rr.facility()
    rn = rr.checkout(store, dana, "A101")
    order = walk.run(store, BASE, walk.Act("o1", "report_work_order", FRONT, unit="A101", text="Roof leaks over the door"), rr.NOW)
    assert order.allowed
    assert rr.accept(store, dana, rn).allowed and rr.pay(store, dana, rn, 9500).allowed
    r = rr.rent(store)
    assert r.rentals[rn].state != "active" and r.status("A101", rr.NOW) == "reserved"           # paid and signed, but the unit is not ready
    why = f"work order {order.made} is still open (Roof leaks over the door)"
    assert any(t.open and t.kind == "activation" and why in t.reason for t in r.tasks.values())
    refused = rr.go(store, rr.act("activate", "code", rn))
    assert (refused.allowed, refused.check) == (False, why)
    for n, (action, kw) in enumerate((("triage_work_order", {}), ("assign_work_order", {"to": "front_desk"}), ("record_order_work", {"text": "Sealed"}),
                                      ("check_order_work", {"text": "Dry"}), ("close_work_order", {}))):
        assert walk.run(store, BASE, walk.Act(f"o{n + 2}", action, MANAGER, about=order.made, **kw), rr.NOW).allowed
    assert rr.go(store, rr.act("activate", "code", rn)).allowed and rr.rent(store).rentals[rn].state == "active"


def test_offering_from_the_waitlist_is_refused_for_a_unit_that_is_not_ready():
    w = Floor()
    oid = w.report(EMPTY)
    entry = w.rent_cmd("join_waitlist", "5x10", w.tenants[PAID], source="staff:front_desk")
    assert entry.allowed, entry.check
    until = (NOW + timedelta(days=1)).isoformat()
    d = w.rent_cmd("offer_unit", entry.made, source="staff:front_desk", unit=EMPTY, until=until)
    assert (d.allowed, d.check) == (False, f"work order {oid} is still open (Door will not latch)")
    w.close(oid)
    assert w.rent_cmd("offer_unit", entry.made, source="staff:front_desk", unit=EMPTY, until=until).allowed
