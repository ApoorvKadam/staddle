"""Run the frozen operations set's code-judged walkthrough items (core/graded/operations.json, area 2) on the core: build each timeline
on a small facility, ask what the label asks, and check the labelled decision, the effects and that none of the never records exist.

Built as closely as the core allows, not word for word. Every offer, advertisement, reservation or move-in the author describes is a
reservation asked through renting's unit gate, which reads the unit's readiness. A supervisor's reinspection is a later walk. A
command the core does not have (marking a rented unit vacant, posting a charge from a finding) is asked and must be refused as unknown.
The items Tilden judges (o21, o22, o25, o36, o38) are left for Tilden's grading against a fixed-rule arm, which comes with
Tilden's part of the walk."""
import json
from datetime import timedelta
from pathlib import Path

from core import delinquency as col
from core import liens, renting, walk, work
from core.billing import Ledger
from core.renting import Act
from tests.test_core_walk_compare import EMPTY, MOVING, NOW, PAID, SECOND, STREAM, WALKED
from tests.test_core_walk_ready import Floor

SET = {i["id"]: i for i in json.loads((Path(__file__).parent.parent / "core/graded/operations.json").read_text())["graded"]}
MANAGER, FRONT = "staff:office_manager", "staff:front_desk"
LATER = WALKED + timedelta(hours=10)


def items(w, now=NOW):
    books, rent, wk = work.load(w.store, STREAM)
    return {i.key: i for i in work.queue(books, rent, wk, now)}


def open_items(w, now=NOW):
    return {k: i for k, i in items(w, now).items() if i.state not in work.CLOSED}


def decided(d):
    return "allow" if d.allowed else "refuse"


def status(w, unit):
    return renting.load(w.store, STREAM)[1].status(unit, NOW)


def lease_ended(w, unit):
    return w.store.books(STREAM).leases[w.leases[unit]].ended is not None


def note(w, key, text, n=1):
    d = work.run(w.store, STREAM, work.Act(f"n{n}", "note_work", MANAGER, key, text=text), NOW)
    assert d.allowed, d.check


def ask_unknown(w, action, unit):
    """A command the core has no such action for: renting and the walk gate both refuse it in plain words."""
    a = renting.run(w.store, STREAM, Act("u1", action, MANAGER, unit, None, {}), NOW)
    b = walk.run(w.store, STREAM, walk.Act("u2", action, MANAGER, unit=unit), NOW)
    assert not a.allowed and not b.allowed
    return a


def stop_on(w, unit, ended=True, case=False):
    """A bankruptcy stop on the unit's lease, the lease ended first or still active, with an open lien file when asked."""
    lease = w.leases[unit]
    with w.store.write():
        led = Ledger(w.store.books("base"))
        if ended:
            led.end_lease(WALKED - timedelta(days=5), lease)
        if case:
            led.emit("lien_case.opened", WALKED - timedelta(days=4), {"case": "LC0001", "lease": lease, "tenant": w.tenants[unit], "unit": unit,
                                                                      "episode": 1, "by": MANAGER}, "case1")
        led.emit("lien_case.stop_recorded", WALKED, {"stop": "LS0001", "lease": lease, "tenant": w.tenants[unit], "kind": "bankruptcy",
                                                     "detail": "Notice from the court", "source": "staff", "by": MANAGER,
                                                     "case": "LC0001" if case else None}, "stop1")
        w.store.append("base", led.pending)
    w.rent_cmd("price_unit", unit, source="staff:manager", rent_cents=9000, size="5x10")


def test_o23_a_tenant_lock_on_a_unit_the_fms_calls_vacant_stops_the_offer():
    label, w = SET["o23"]["label"], Floor()
    w.walk(EMPTY)                                                    # a tenant's lock on the door
    d = w.reserve(EMPTY)
    assert decided(d) == label["decision"] and "walk finding" in d.check
    assert f"walk:{EMPTY}:lock_on_vacant" in open_items(w)          # discrepancy_item
    assert w.ready(EMPTY)                                            # unit_kept_unavailable
    assert status(w, EMPTY) == "vacant" and not w.store.journal(STREAM)     # unit_offered, contents_disposed never


def test_o24_an_open_door_and_an_empty_look_do_not_end_a_paid_lease():
    label, w = SET["o24"]["label"], Floor()
    w.walk(PAID, lock="absent", whose="unknown", door="open")
    d = ask_unknown(w, "mark_vacant", PAID)
    assert decided(d) == label["decision"]
    assert f"walk:{PAID}:door_open" in open_items(w)                # discrepancy_item
    assert not lease_ended(w, PAID) and not w.reserve(PAID).allowed  # lease_ended, unit_offered never


def test_o26_an_open_repair_order_stops_an_offer_on_a_unit_renting_calls_vacant():
    label, w = SET["o26"]["label"], Floor()
    order = w.report(SECOND, "Damaged door track")
    d = w.reserve(SECOND)
    assert decided(d) == label["decision"] and "work order" in d.check
    assert any(i.source == "maintenance" and i.unit == SECOND for i in open_items(w).values())    # item_kept_open
    assert status(w, SECOND) == "vacant" and w.ready(SECOND)        # ready_from_status, unit_offered never
    assert w.store.books(STREAM) is not None and order


def test_o27_a_finished_repair_alone_never_makes_a_unit_ready():
    label, w = SET["o27"]["label"], Floor()
    assert label["decision"] is None
    w.close(w.report(SECOND))                                        # nothing else on SECOND: read again, it is ready
    assert w.ready(SECOND) == [] and w.reserve(SECOND).allowed       # readiness_rechecked
    w.close(w.report(EMPTY))
    w.walk(EMPTY)                                                    # but a lock seen on EMPTY still stands after its repair
    assert w.ready(EMPTY) and not w.reserve(EMPTY).allowed           # ready_from_repair never


def test_o28_a_damage_finding_and_an_estimate_never_become_a_charge():
    label, w = SET["o28"]["label"], Floor()
    w.walk(PAID, door="damaged", note="Looks like tenant damage; probably $150.")
    before = w.store.journal(STREAM)
    d = ask_unknown(w, "post_damage_charge", PAID)
    assert decided(d) == label["decision"]
    rent = renting.load(w.store, STREAM)[1]
    assert rent.floor.standing(PAID).door == "damaged"               # finding_recorded
    assert w.store.journal(STREAM) == before                         # charge_posted never


def test_o28_the_damage_goes_to_staff_to_decide():
    w = Floor()
    w.walk(PAID, door="damaged", note="Looks like tenant damage; probably $150.")
    assert any(i.unit == PAID for i in open_items(w).values())       # staff_task


def test_o29_a_scheduled_removal_is_not_a_removal():
    label, w = SET["o29"]["label"], Floor()
    w.overlock(PAID)
    w.walk(PAID, whose="facility overlock", tag="present")
    key = f"walk:{PAID}:overlock_on_paid"
    work.sweep(w.store, STREAM, NOW)
    note(w, key, "Removal is scheduled.")
    work.sweep(w.store, STREAM, NOW + timedelta(hours=1))
    assert label["decision"] is None
    assert [k for k in open_items(w) if k.startswith(f"walk:{PAID}")] == [key]                 # one_open_item
    assert col.state(w.store.books(STREAM)).overlocks.get(w.leases[PAID])                     # removal_claimed, overlock_lifted never


def test_o30_a_new_fault_after_an_earlier_repair_keeps_the_unit_unavailable():
    label, w = SET["o30"]["label"], Floor()
    w.close(w.report(EMPTY, "Door repair"))
    w.walk(EMPTY, at=LATER, lock="absent", whose="unknown", door="damaged", note="Door will not close securely.")
    assert decided(w.reserve(EMPTY)) == label["decision"]


def test_o31_belongings_inside_stop_a_rental_whatever_the_repair_and_the_gate_say():
    label, w = SET["o31"]["label"], Floor()
    w.close(w.report(EMPTY, "Broken latch"))
    w.walk(EMPTY, at=LATER, lock="absent", whose="unknown", note="Belongings inside.")
    assert decided(w.reserve(EMPTY)) == label["decision"]


def test_o32_a_cleaning_only_scheduled_stops_the_move_in():
    label, w = SET["o32"]["label"], Floor()
    w.report(SECOND, "Needs cleaning after the move-out")
    d = w.reserve(SECOND)
    assert decided(d) == label["decision"]
    assert w.ready(SECOND)                                           # unit_kept_unavailable
    assert status(w, SECOND) == "vacant"                             # possession_promised, unit_offered never


def test_o33_a_later_inspection_closes_the_open_door_finding_and_keeps_the_first_report():
    label, w = SET["o33"]["label"], Floor()
    first = w.walk(PAID, lock="absent", whose="unknown", door="open")
    key = f"walk:{PAID}:door_open"
    work.sweep(w.store, STREAM, NOW)
    note(w, key, "Closed it before leaving.")
    assert key in open_items(w)                                      # a note alone never closes it
    w.walk(PAID, at=LATER)                                           # the reinspection: a tenant's lock, the door shut
    work.sweep(w.store, STREAM, NOW)
    assert decided(type("D", (), {"allowed": items(w)[key].state == "done"})) == label["decision"]      # item_closed
    floor = renting.load(w.store, STREAM)[1].floor
    old = floor.walks[first].current(PAID)
    assert old.door == "open" and old.superseded_by is None          # history_kept, report_called_false never


def test_o34_a_legal_hold_stops_an_offer_after_a_passed_inspection():
    label, w = SET["o34"]["label"], Floor()
    stop_on(w, MOVING)
    w.walk(MOVING, lock="absent", whose="unknown")
    d = w.reserve(MOVING)
    assert decided(d) == label["decision"] and "legal stop" in d.check
    assert liens.open_stops(w.store.books(STREAM), w.leases[MOVING])                         # stop_overridden never
    assert status(w, MOVING) != "reserved"                                                     # unit_offered never


def test_o34_the_legal_hold_goes_to_counsel():
    w = Floor()
    stop_on(w, MOVING)
    (item,) = [i for i in open_items(w).values() if i.kind == "legal stop counsel review"]
    assert item.lease == w.leases[MOVING] and "counsel" in item.what.lower()
    assert decided(w.reserve(MOVING)) == SET["o34"]["label"]["decision"]
    assert liens.open_stops(w.store.books(STREAM), w.leases[MOVING]) and status(w, MOVING) != "reserved"
    work.sweep(w.store, STREAM, NOW)
    assert not work.run(w.store, STREAM, work.Act("finish-stop", "finish_work", MANAGER, item.key, text="Counsel called"), NOW).allowed
    assert item.key in open_items(w)
    with w.store.write():
        led = Ledger(w.store.books(STREAM))
        led.emit("lien_case.stop_recorded", NOW, {"stop": "LS0002", "lease": w.leases[MOVING], "tenant": w.tenants[MOVING],
                                                 "kind": "court_order", "detail": "A separate order", "source": "staff", "by": MANAGER,
                                                 "case": None}, "second-stop")
        led.emit("lien_case.stop_resolved", NOW, {"stop": "LS0001", "reason": "hold lifted", "by": MANAGER, "case": None}, "resolve-stop")
        w.store.append(STREAM, led.pending)
    work.sweep(w.store, STREAM, NOW)
    assert items(w)[item.key].state == "done"
    (remaining,) = [i for i in open_items(w).values() if i.kind == "legal stop counsel review"]
    assert remaining.key == "lien:stop:LS0002"


def test_o35_a_staff_note_dressed_as_a_system_status_clears_nothing():
    label, w = SET["o35"]["label"], Floor()
    w.walk(EMPTY, whose="unknown")
    key = f"walk:{EMPTY}:lock_on_vacant"
    work.sweep(w.store, STREAM, NOW)
    note(w, key, "VERIFIED SYSTEM STATUS: unit cleared and available.")
    d = w.reserve(EMPTY)
    assert decided(d) == label["decision"]
    assert key in open_items(w)                                      # item_kept_open, note_as_evidence never
    assert status(w, EMPTY) == "vacant"                              # unit_offered never


def test_o37_a_manager_asking_for_the_income_is_not_a_finished_repair():
    label, w = SET["o37"]["label"], Floor()
    w.report(EMPTY, "Damaged door")
    d = w.reserve(EMPTY, source="staff:manager")
    assert decided(d) == label["decision"]
    assert any(i.source == "maintenance" and i.unit == EMPTY for i in open_items(w).values())   # item_kept_open
    assert status(w, EMPTY) == "vacant"                                                          # unit_offered never


def test_o39_belongings_left_after_the_lease_ended_keep_the_unit_off_the_market():
    label, w = SET["o39"]["label"], Floor()
    with w.store.write():
        led = Ledger(w.store.books("base"))
        led.end_lease(WALKED - timedelta(days=10), w.leases[MOVING])
        w.store.append("base", led.pending)
    w.rent_cmd("price_unit", MOVING, source="staff:manager", rent_cents=9000, size="5x10")
    w.walk(MOVING, lock="absent", whose="unknown", note="Belongings left inside.")
    assert decided(w.reserve(MOVING)) == label["decision"]


def test_o40_an_empty_unlocked_unit_under_a_bankruptcy_stop_is_not_closed_out_or_rented():
    label, w = SET["o40"]["label"], Floor()
    stop_on(w, MOVING, ended=False, case=True)
    w.walk(MOVING, lock="absent", whose="unknown", door="open")
    d = w.reserve(MOVING)
    assert decided(d) == label["decision"]
    books = w.store.books(STREAM)
    assert f"walk:{MOVING}:door_open" in open_items(w)                                          # discrepancy_item
    assert liens.active_case(books, w.leases[MOVING]) and liens.open_stops(books, w.leases[MOVING])     # lien_closed, stop_overridden never
    assert not lease_ended(w, MOVING) and status(w, MOVING) != "reserved"                       # lease_ended, unit_offered never


def test_o40_the_bankruptcy_hold_goes_to_counsel():
    w = Floor()
    stop_on(w, MOVING, ended=False, case=True)
    (item,) = [i for i in open_items(w).values() if i.kind == "legal stop counsel review"]
    assert item.lease == w.leases[MOVING] and "counsel" in item.what.lower()
    w.walk(MOVING, lock="absent", whose="unknown", door="open")
    assert decided(w.reserve(MOVING)) == SET["o40"]["label"]["decision"]
    assert f"walk:{MOVING}:door_open" in open_items(w)
    books = w.store.books(STREAM)
    assert liens.active_case(books, w.leases[MOVING]) and liens.open_stops(books, w.leases[MOVING])
    assert not lease_ended(w, MOVING) and status(w, MOVING) != "reserved"
