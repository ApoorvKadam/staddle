"""Enrolments: offered, accepted, active (simulated), ended, with covered periods. Ending at move-out prorates the unused days once, by
actual days. A transfer or a price or terms change re-offers; the enrolment never moves to a new version or price without a new
acceptance."""
from datetime import date

import pytest

from core import protection
from core.billing import Refused
from core.books import PROTECTION
from tests.protection_world import FRONT, MANAGER, SPARE, TERMS_V2, World, day


def hand_charge(w, k, period, at, cents=1200):
    """The plan's monthly charge for a period, posted and recorded against the enrolment that covers it as the plan's own posting does, so an
    ending has something to prorate."""
    y, m = map(int, period.split("-"))

    def post(led):
        cid = led.charge_by_hand(at, w.leases[k], PROTECTION, cents, f"protection:{w.leases[k]}:{period}", date(y, m, 1), credit=False)
        e = protection.covering(protection.state(led.books), w.leases[k], period)
        led.emit("protection.charged", at, {"charge": cid, "enrolment": e.id, "authority": e.authority, "lease": e.lease, "unit": e.unit, "period": period,
                                            "version": e.version, "cents": cents}, f"protection:charged:{e.lease}:{period}", source=FRONT)
        return cid
    return w.act(post)


def test_an_accepted_offer_becomes_active_only_when_staff_activate_it():
    w = World()
    e = w.offer(0, day(11, 2))
    w.accept(e, day(11, 3), starts=date(2026, 11, 3))
    assert w.state().enrolments[e].state == "accepted"
    w.act(lambda led: protection.activate(led, e, MANAGER, day(11, 3, 10)))
    rec = w.state().enrolments[e]
    assert (rec.state, rec.starts, rec.basis, rec.authority) == ("active", date(2026, 11, 3), "acceptance", {"acceptance": rec.acceptance})
    assert [h["state"] for h in rec.history] == ["offered", "accepted", "active"] and rec.history[-1]["by"] == MANAGER


def test_an_offer_nobody_accepted_cannot_be_activated():
    w = World()
    e = w.offer(0, day(11, 2))
    with pytest.raises(Refused, match="no explicit acceptance"):
        w.act(lambda led: protection.activate(led, e, MANAGER, day(11, 3)))
    assert w.state().enrolments[e].state == "offered"


def test_covered_periods_run_from_the_start_month_until_the_end_date():
    w = World()
    e = w.accepted_and_active(0, day(11, 3), starts=date(2026, 11, 3))
    rec = w.state().enrolments[e]
    assert [rec.covers(p) for p in ("2026-10", "2026-11", "2027-02")] == [False, True, True]
    assert rec.periods(date(2027, 1, 9)) == ["2026-11", "2026-12", "2027-01"]
    w.act(lambda led: protection.end_enrolment(led, e, date(2026, 12, 1), "cancellation", FRONT, day(11, 19)))
    rec = w.state().enrolments[e]
    assert [rec.covers(p) for p in ("2026-11", "2026-12")] == [True, False] and rec.periods(date(2027, 3, 1)) == ["2026-11"]


def test_ending_at_move_out_prorates_the_unused_days_once_by_actual_days():
    w = World()
    e = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    charge = hand_charge(w, 0, "2026-11", day(11, 1))
    w.act(lambda led: led.end_lease(day(11, 10), w.leases[0]))
    w.act(lambda led: protection.end_at_move_out(led, e, FRONT, day(11, 11)))
    rec = w.state().enrolments[e]
    # the last day was Nov 10, so Nov 11 to Nov 30 is unused: 20 of 30 days of $12.00
    assert (rec.state, rec.ended["on"], rec.ended["reason"], rec.ended["unused_days"], rec.ended["unused_cents"], rec.ended["charge"]) == (
        "ended", date(2026, 11, 11), "move-out", 20, 800, charge)
    with pytest.raises(Refused, match="already ended"):
        w.act(lambda led: protection.end_at_move_out(led, e, FRONT, day(11, 12)))
    assert [x.kind for x in w.store.stream_events("visitor:a") if x.kind == "protection.ended"] == ["protection.ended"]
    assert w.books().charges[charge].cents == 1200 and w.books().charges[charge].closed is None       # a record of the unused days; no money moves here


def test_the_unused_days_are_counted_in_the_actual_days_of_that_month():
    w = World()
    e = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    hand_charge(w, 0, "2027-02", day(2, 1, year=2027))                  # February 2027 has 28 days
    w.act(lambda led: led.end_lease(day(2, 14, year=2027), w.leases[0]))
    w.act(lambda led: protection.end_at_move_out(led, e, FRONT, day(2, 15, year=2027)))
    ended = w.state().enrolments[e].ended
    assert (ended["on"], ended["unused_days"], ended["unused_cents"]) == (date(2027, 2, 15), 14, 600)


def test_an_end_with_no_charge_for_that_month_has_nothing_to_prorate_and_ending_on_the_first_gives_back_a_whole_month():
    w = World()
    quiet = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    w.act(lambda led: protection.end_enrolment(led, quiet, date(2026, 12, 1), "cancellation", FRONT, day(11, 19)))
    assert w.state().enrolments[quiet].ended["unused_days"] == 0 and w.state().enrolments[quiet].ended["charge"] is None
    whole = w.accepted_and_active(1, day(11, 1), starts=date(2026, 11, 1))
    hand_charge(w, 1, "2026-12", day(12, 1))
    w.act(lambda led: protection.end_enrolment(led, whole, date(2026, 12, 1), "cancellation", FRONT, day(12, 1, 10)))
    assert (w.state().enrolments[whole].ended["unused_days"], w.state().enrolments[whole].ended["unused_cents"]) == (31, 1200)


def test_an_end_before_coverage_started_or_for_a_plan_that_is_not_active_is_refused():
    w = World()
    e = w.offer(0, day(11, 2))
    with pytest.raises(Refused, match="only an active plan"):
        w.act(lambda led: protection.end_enrolment(led, e, date(2026, 12, 1), "cancellation", FRONT, day(11, 19)))
    w.accept(e, day(11, 3), starts=date(2026, 11, 3))
    w.act(lambda led: protection.activate(led, e, MANAGER, day(11, 3, 10)))
    with pytest.raises(Refused, match="before coverage"):
        w.act(lambda led: protection.end_enrolment(led, e, date(2026, 11, 2), "cancellation", FRONT, day(11, 19)))
    with pytest.raises(Refused, match="reason"):
        w.act(lambda led: protection.end_enrolment(led, e, date(2026, 12, 1), "tenant seemed unhappy", FRONT, day(11, 19)))


def test_a_transfer_ends_the_old_enrolment_and_re_offers_on_the_new_lease_with_no_acceptance_carried_over():
    w = World()
    old = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    w.act(lambda led: led.end_lease(day(11, 14), w.leases[0]))
    new_lease = w.act(lambda led: led.sign_lease(day(11, 15), w.tenants[0], SPARE[0], 14000, [], date(2026, 11, 15)))
    w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1800, TERMS_V2, date(2026, 11, 15), FRONT, day(11, 15)))
    new = w.act(lambda led: protection.reoffer(led, old, new_lease, FRONT, day(11, 15, 10)))
    st = w.state()
    assert (st.enrolments[old].state, st.enrolments[old].ended["reason"]) == ("ended", "transfer")
    n = st.enrolments[new]
    assert (n.state, n.lease, n.unit, n.version, n.cents, n.acceptance, n.replaces) == ("offered", new_lease, SPARE[0], 2, 1800, None, None)
    assert st.enrolments[old].cents == 1200 and st.enrolments[old].acceptance           # the earlier enrolment stays in the history as it was
    assert not any(a.enrolment == new for a in st.acceptances.values())


def test_a_new_version_replaces_the_old_enrolment_only_after_a_new_acceptance_and_activation():
    w = World()
    old = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1500, TERMS_V2, date(2026, 11, 6), FRONT, day(11, 6)))
    assert (w.state().enrolments[old].version, w.state().enrolments[old].cents, w.state().enrolments[old].state) == (1, 1200, "active")
    with pytest.raises(Refused, match="newer version replaces it only when the offer names it"):
        w.offer(0, day(11, 7))
    new = w.offer(0, day(11, 7), replaces=old)
    assert w.state().enrolments[old].state == "active"                                   # an offer changes nothing
    w.accept(new, day(11, 8), starts=date(2026, 12, 1))
    assert w.state().enrolments[old].state == "active"                                   # nor does the acceptance, until staff activate it
    w.act(lambda led: protection.activate(led, new, MANAGER, day(11, 8, 10)))
    st = w.state()
    assert (st.enrolments[old].state, st.enrolments[old].ended["on"], st.enrolments[old].ended["reason"]) == ("ended", date(2026, 12, 1), "newer version")
    assert (st.enrolments[new].state, st.enrolments[new].cents) == ("active", 1500)
    assert [st.enrolments[old].covers(p) for p in ("2026-11", "2026-12")] == [True, False] and st.enrolments[new].covers("2026-12")


def test_the_simulated_wording_is_the_one_a_staff_member_reads():
    assert protection.SIMULATED == "Simulated protection-plan enrolment, no insurance policy issued"
