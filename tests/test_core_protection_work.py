"""Protection in the work queue: one item for each condition, through core/work.py. Evidence to review (a placeholder included), a policy
expiring soon or lapsed, a plan charge with no plan behind it (which holds collections on that charge), a dispute for staff to decide, and
a plan still active after the lease ended. An item closes on the record that shows the work done, never on an acknowledgement."""
from datetime import date

from core import protection, work
from core.books import PROTECTION
from core.gate import Command, execute
from tests.protection_world import FRONT, MANAGER, STREAM, World, day

DOC = {"carrier": "Sunrise Mutual", "number": "8A-44521", "effective": "2026-11-01", "expires": "2027-10-31", "document": "declarations page.pdf"}
REVIEW = {"outcome": "accepted", "how": "Read the declarations page: carrier, number and dates match what was typed in.", "checked": ["carrier", "policy number", "dates"]}
NOW = day(11, 10)


def items(w, now=NOW):
    books, rent, wk = work.load(w.store, STREAM)
    return {i.key: i for i in work.queue(books, rent, wk, now)}


def mine(w, now=NOW):
    return {k: i for k, i in items(w, now).items() if i.source == "protection"}


def opened(w):
    return [e.payload["key"] for e in w.store.stream_events(STREAM) if e.kind == "work.opened"]


def record(w, k=0, at=day(11, 3), **over):
    d = w.cmd("record_policy_evidence", at, FRONT, lease=w.leases[k], form={**DOC, **over})
    assert d.allowed, d.check
    return d.made


def review(w, ev, at=day(11, 4)):
    d = w.cmd("verify_policy", at, MANAGER, about=ev, form=REVIEW)
    assert d.allowed, d.check


def test_a_policy_document_to_review_is_one_item_for_the_front_desk_due_two_days_after_it_arrived():
    w = World()
    ev = record(w)
    (i,) = mine(w).values()
    assert (i.key, i.kind, i.role, i.due, i.unit, i.state, i.attest) == (f"protection:review:{ev}", "policy to review", "front_desk", date(2026, 11, 5), "A101", "open", False)
    assert "Tenant 0" in i.why and "Sunrise Mutual" in i.why and "not reviewed" in i.why


def test_a_placeholder_number_is_named_on_the_item_and_a_statement_asks_for_the_document():
    w = World()
    ph = record(w, number="TEMP")
    st = w.cmd("record_policy_evidence", day(11, 4), FRONT, lease=w.leases[1], text="I'm covered through my car insurance.").made
    its = mine(w)
    assert "placeholder" in its[f"protection:review:{ph}"].why and its[f"protection:review:{ph}"].what == "Policy number looks like a placeholder"
    assert its[f"protection:review:{st}"].what == "Policy document needed" and its[f"protection:review:{st}"].due == date(2026, 11, 6)


def test_the_item_closes_on_the_review_and_not_on_an_acknowledgement():
    w = World()
    ev = record(w)
    key = f"protection:review:{ev}"
    work.sweep(w.store, STREAM, NOW)
    d = work.run(w.store, STREAM, work.Act("a1", "acknowledge_work", MANAGER, key), NOW)
    assert d.allowed and items(w)[key].state == "open" and items(w)[key].acknowledged        # seen, not finished
    fin = work.run(w.store, STREAM, work.Act("f1", "finish_work", MANAGER, key, text="Looked at it"), NOW)
    assert not fin.allowed and "the record closes this one" in fin.check
    review(w, ev, day(11, 9))
    work.sweep(w.store, STREAM, NOW)
    assert items(w)[key].state == "done" and "Staff reviewed" in items(w)[key].evidence


def test_one_item_however_often_the_daily_check_runs():
    w = World()
    record(w)
    for _ in range(3):
        assert work.sweep(w.store, STREAM, NOW) >= 0
    assert work.sweep(w.store, STREAM, NOW) == 0 and len(opened(w)) == 1


def test_a_reviewed_policy_expiring_within_thirty_days_is_an_item_before_it_lapses_and_stays_after():
    w = World()
    ev = record(w, effective="2025-11-20", expires="2026-12-05")
    review(w, ev, day(10, 1))
    assert mine(w, day(11, 4)) == {}                                                          # 31 days ahead: nothing yet
    i = mine(w, day(11, 6))[f"protection:expiring:{ev}"]
    assert (i.kind, i.role, i.due, i.attest, i.state) == ("policy expiring", "front_desk", date(2026, 12, 5), True, "open") and "Dec 5" in i.why
    work.sweep(w.store, STREAM, day(12, 7))
    w.act(lambda led: protection.expire_evidence(led, ev, day(12, 7)))
    late = mine(w, day(12, 8))[f"protection:expiring:{ev}"]
    assert late.what == "Policy expired" and late.overdue and w.state().evidence[ev].status == "expired"


def test_the_expiring_item_closes_only_when_a_renewal_is_reviewed():
    w = World()
    old = record(w, effective="2025-11-20", expires="2026-12-05")
    review(w, old, day(10, 1))
    key = f"protection:expiring:{old}"
    work.sweep(w.store, STREAM, day(11, 20))
    new = record(w, number="8A-90001", effective="2026-12-06", expires="2027-12-05", renews=old, at=day(11, 21))
    assert items(w, day(11, 22))[key].state == "open" and f"protection:review:{new}" in mine(w, day(11, 22))     # a renewal waiting is not a renewal reviewed
    review(w, new, day(11, 23))
    work.sweep(w.store, STREAM, day(11, 24))
    assert items(w, day(11, 24))[key].state == "done" and f"protection:expiring:{new}" not in mine(w, day(11, 24))


def test_a_plan_charge_with_no_plan_behind_it_is_a_blocked_correction_item_that_holds_collection():
    w = World()
    ch = w.act(lambda led: led.charge_by_hand(day(11, 6), w.leases[2], PROTECTION, 1200, f"protection:{w.leases[2]}:2026-11", date(2026, 11, 6)))
    i = mine(w)[f"protection:charge:{ch}"]
    assert (i.kind, i.role, i.state, i.due, i.unit) == ("plan charge without plan", "office_manager", "blocked", date(2026, 11, 6), "A103")
    assert "no accepted plan" in i.why and "held" in i.why and w.books().held(ch)
    work.sweep(w.store, STREAM, NOW)                                                           # the daily check opens it
    w.act(lambda led: led.reverse(day(11, 8), ch, "no accepted plan", MANAGER))
    work.sweep(w.store, STREAM, NOW)
    assert items(w)[f"protection:charge:{ch}"].state == "done" and not w.books().held(ch)


def test_a_supported_plan_charge_opens_no_item():
    w = World()
    w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    assert w.charge(0, "2026-11", day(11, 2)).allowed and mine(w) == {}


def test_a_disputed_plan_charge_is_an_item_for_staff_to_decide_until_they_do():
    w = World()
    w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    ch = w.charge(0, "2026-11", day(11, 2)).made
    d = execute(w.store, STREAM, Command("d1", "open_dispute", w.tenants[0], "operator", charge=ch, quote="I never agreed to this extra plan."), day(11, 9))
    assert d.allowed and d.hold
    key = f"protection:dispute:{w.books().disputes['D0001'].id}"
    i = mine(w, day(11, 10))[key]
    assert (i.kind, i.role, i.state, i.due) == ("plan charge disputed", "office_manager", "blocked", date(2026, 11, 19)) and "I never agreed" in i.why
    work.sweep(w.store, STREAM, day(11, 10))
    w.act(lambda led: led.decide_dispute(day(11, 12), "D0001", "staff kept the charge"))
    assert items(w, day(11, 12))[key].state == "done"


def test_a_plan_still_active_after_the_lease_ended_is_an_item_until_staff_end_it():
    w = World()
    e = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    w.act(lambda led: led.end_lease(day(11, 10), w.leases[0]))
    i = mine(w, day(11, 12))[f"protection:ended-lease:{e}"]
    assert (i.kind, i.role, i.due, i.unit) == ("plan active after move-out", "front_desk", date(2026, 11, 10), "A101")
    work.sweep(w.store, STREAM, day(11, 12))
    assert w.cmd("end_plan", day(11, 12), FRONT, about=e, form={"on": "2026-11-11", "reason": "move-out"}).allowed
    work.sweep(w.store, STREAM, day(11, 12))
    assert items(w, day(11, 12))[f"protection:ended-lease:{e}"].state == "done"


def test_nothing_in_the_protection_records_writes_money_through_the_queue():
    w = World()
    record(w, number="TEMP")
    before = w.store.journal(STREAM)
    work.sweep(w.store, STREAM, NOW)
    assert w.store.journal(STREAM) == before
