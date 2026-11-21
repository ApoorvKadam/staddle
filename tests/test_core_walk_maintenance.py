"""An open work order is an item of work (source "maintenance") and closes when the order closes after its staff check. Two reports of
the same fault on one unit join one order only when staff say so by naming the open order; the code never guesses two notes are one."""
from datetime import timedelta

from core import walk, work
from tests.test_core_walk_compare import EMPTY, FRONT, NOW, PAID, STREAM, WALKED, World
from tests.test_core_walk_work import items

MANAGER = "staff:office_manager"
WHEN = WALKED


def report(w, n, unit=PAID, text="Door will not latch", about=None, at=WHEN, source=FRONT):
    return walk.run(w.store, STREAM, walk.Act(f"r{n}", "report_work_order", source, about=about, unit=unit, text=text), at)


def move(w, n, action, order, at, source=MANAGER, **kw):
    d = walk.run(w.store, STREAM, walk.Act(f"m{n}", action, source, about=order, **kw), at)
    assert d.allowed, d.check
    return d


def test_an_open_work_order_is_one_item_naming_the_unit_the_fault_who_reported_it_and_where_it_stands():
    w = World()
    oid = report(w, 1).made
    i = items(w)[f"maintenance:{oid}"]
    assert (i.source, i.kind, i.unit, i.role, i.state, i.attest, i.tone) == ("maintenance", "work order", PAID, "office_manager", "open", False, "act")
    assert i.what == f"Work order on unit {PAID}: Door will not latch" and i.ref == oid and i.due == WHEN.date()
    assert "Reported Nov 13 at 3:00 pm by front desk" in i.why and "Not triaged yet" in i.why
    assert "closed after staff checked the work" in i.closes
    move(w, 2, "triage_work_order", oid, WHEN + timedelta(hours=1))
    move(w, 3, "assign_work_order", oid, WHEN + timedelta(hours=2), to="front_desk")
    j = items(w)[f"maintenance:{oid}"]
    assert j.role == "front_desk" and "Assigned to front desk" in j.why


def test_the_item_stays_open_through_the_work_and_the_check_and_closes_only_when_the_order_closes():
    w = World()
    oid = report(w, 1).made
    key = f"maintenance:{oid}"
    work.sweep(w.store, STREAM, NOW)
    for n, (action, kw) in enumerate((("triage_work_order", {}), ("assign_work_order", {"to": "front_desk"}), ("record_order_work", {"text": "New latch fitted"}),
                                      ("check_order_work", {"text": "Door latches"})), start=2):
        move(w, n, action, oid, WHEN + timedelta(hours=n), **kw)
        assert items(w)[key].state == "open", action
    assert "Staff checked the work" in items(w)[key].why
    assert not work.run(w.store, STREAM, work.Act("fin", "finish_work", MANAGER, key, text="It is done"), NOW).allowed     # a word does not close it
    move(w, 9, "close_work_order", oid, WHEN + timedelta(days=1))
    done = items(w, NOW + timedelta(days=1))[key]
    assert (done.state, done.evidence) == ("done", "The work order was closed after staff checked the work.")
    assert work.sweep(w.store, STREAM, NOW + timedelta(days=1)) >= 1 and work.sweep(w.store, STREAM, NOW + timedelta(days=1)) == 0


def test_two_reports_of_one_fault_are_two_orders_until_staff_link_them():
    w = World()
    first, second = report(w, 1).made, report(w, 2, text="Door will not latch", at=WHEN + timedelta(days=1), source="staff:manager").made
    assert first != second                                        # the code never guesses that two notes are the same fault
    assert [k for k in items(w) if k.startswith("maintenance:")] == [f"maintenance:{first}", f"maintenance:{second}"]


def test_a_report_that_names_the_open_order_joins_it_and_opens_no_second_one():
    w = World()
    oid = report(w, 1).made
    d = report(w, 2, text="Latch still sticks, tenant came back", about=oid, at=WHEN + timedelta(days=1), source="staff:manager")
    assert (d.allowed, d.made) == (True, oid)
    _, wk = walk.load(w.store, STREAM)
    o = wk.orders[oid]
    assert len(wk.orders) == 1 and [(r.by, r.text) for r in o.reports] == [(FRONT, "Door will not latch"), ("staff:manager", "Latch still sticks, tenant came back")]
    assert o.changes[-1].state == "reported again" and o.state == "reported"
    only = [k for k in items(w, NOW + timedelta(days=1)) if k.startswith("maintenance:")]
    assert only == [f"maintenance:{oid}"]
    assert "Reported again Nov 14 at 3:00 pm by manager: Latch still sticks, tenant came back" in items(w, NOW + timedelta(days=1))[only[0]].why


def test_a_report_can_join_only_an_open_order_on_the_same_unit():
    w = World()
    oid = report(w, 1).made
    assert report(w, 2, about="WO0099").check == "there is no such work order"
    assert report(w, 3, unit=EMPTY, about=oid).check == f"that work order is on unit {PAID}, not {EMPTY}"
    assert report(w, 4, text=" ", about=oid).check == "say in a few words what is wrong"
    for n, (action, kw) in enumerate((("triage_work_order", {}), ("assign_work_order", {"to": "manager"}), ("record_order_work", {}), ("check_order_work", {}),
                                      ("close_work_order", {})), start=5):
        move(w, n, action, oid, WHEN + timedelta(hours=n), **kw)
    assert report(w, 20, about=oid).check == "that work order is closed; report the fault as a new order"
    assert report(w, 21, unit=None, about=oid).check == "that work order is closed; report the fault as a new order"


def test_a_report_that_names_an_order_may_leave_the_unit_out():
    w = World()
    oid = report(w, 1).made
    assert report(w, 2, unit=None, about=oid, text="Still broken").made == oid
    assert report(w, 3, unit=None, text="No unit and no order").check == "there is no unit None at this facility"
