"""Edges an independent review of the protection branch found: each test names the thing that went wrong at the edge."""
from datetime import date, timedelta

from core import protection
from core.books import PROTECTION
from tests.protection_world import FRONT, MANAGER, SPARE, World, day


def test_a_charge_for_year_zero_is_refused_as_a_period_and_writes_no_charge():
    w = World()
    w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    d = w.charge(0, "0000-11", day(11, 2))
    assert (d.allowed, d.why) == (False, "period")
    assert not [c for c in w.books().charges.values() if c.kind == PROTECTION]


def test_today_says_a_collection_request_was_approved_not_that_money_was_collected():
    from types import SimpleNamespace
    from desk.fms import Facility
    w = World()
    w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    ch = w.charge(0, "2026-11", day(11, 2)).made
    d = w.cmd("collect_charge", day(11, 3), FRONT, about=ch)
    assert d.allowed
    me = SimpleNamespace(b=w.books(), people={w.tenants[0]: {"name": "Tenant 0"}})
    said = Facility.protection_sentence(me, {"action": "collect_charge", "allowed": True, "about": ch, "lease": w.leases[0]})
    assert said == "Approved collecting a protection plan charge for Tenant 0" and "Collected" not in said


def test_a_charge_under_the_owner_rule_names_the_rule_and_notice_never_a_tenant_acceptance():
    from tests.test_core_protection_commands import notice, rule_world
    w, e = rule_world()
    n = notice(w, e, day(11, 2))
    assert w.cmd("enroll_plan", day(11, 20), MANAGER, about=e, form={"basis": "owner_rule", "notice": n}).allowed
    d = w.cmd("post_plan_charge", day(11, 21), FRONT, lease=w.leases[1], period="2026-11")
    assert d.allowed and "acceptance" not in d.check and "owner's rule" in d.check and n in d.check
    own = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    again = w.cmd("post_plan_charge", day(11, 21), FRONT, lease=w.leases[0], period="2026-11")
    assert again.allowed and "on the acceptance of" in again.check and w.state().enrolments[own].acceptance in again.check


def test_rentals_shows_a_plan_that_starts_next_month_as_scheduled_until_coverage_begins():
    from types import SimpleNamespace
    from desk.fms import Facility
    w = World()
    w.accepted_and_active(0, day(11, 3), starts=date(2026, 12, 1))
    word = lambda today: Facility.protection_word(SimpleNamespace(b=w.books(), today=today), w.leases[0])
    assert word(date(2026, 11, 3)) == "Plan starts Dec 1"
    assert word(date(2026, 11, 30)) == "Plan starts Dec 1"
    assert word(date(2026, 12, 1)) == "Plan active"


def test_a_replacement_must_be_a_newer_version_than_the_plan_it_replaces():
    import pytest
    from core.billing import Refused
    from tests.protection_world import TERMS_V2
    w = World()
    v1 = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1500, TERMS_V2, date(2026, 11, 2), FRONT, day(11, 2)))
    with pytest.raises(Refused, match="newer version"):
        w.offer(0, day(11, 3), version=1, replaces=v1)                       # the same version is no replacement
    v2 = w.offer(0, day(11, 3), version=2, replaces=v1)                      # a newer one is
    w.accept(v2, day(11, 4), starts=date(2026, 11, 4))
    w.enrol(v2, day(11, 4, 10))
    assert (w.state().enrolments[v1].state, w.state().enrolments[v2].state) == ("ended", "active")
    with pytest.raises(Refused, match="newer version"):
        w.offer(0, day(11, 5), version=1, replaces=v2)                       # back to an older version ends version 2: refused
    assert w.state().enrolments[v2].state == "active" and len(w.state().enrolments) == 2


def test_a_plan_accepted_for_after_move_out_cannot_be_activated_and_never_charges():
    w = World()
    e = w.offer(0, day(11, 2))
    w.accept(e, day(11, 3), starts=date(2026, 11, 20))
    w.act(lambda led: led.end_lease(day(11, 15), w.leases[0]))               # the tenant moved out Nov 15
    d = w.cmd("enroll_plan", day(11, 16), MANAGER, about=e)
    assert (d.allowed, d.why) == (False, "lease_ended") and "ended" in d.check
    assert w.state().enrolments[e].state == "accepted" and not [x for x in w.store.stream_events("visitor:a") if x.kind == "protection.enrolled"]
    assert not w.charge(0, "2026-11", day(11, 17)).allowed


def test_a_plan_already_active_while_the_lease_ran_keeps_its_billing_for_that_month():
    w = World()
    w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    w.act(lambda led: led.end_lease(day(11, 15), w.leases[0]))
    assert w.charge(0, "2026-11", day(11, 16)).allowed                        # it covered days of the lease; December is the lease's end
    assert not w.charge(0, "2026-12", day(12, 1)).allowed


def test_a_plan_cannot_start_before_a_future_lease_begins_under_the_owner_rule():
    from tests.test_core_protection_commands import notice, rule_world
    w, _ = rule_world()
    lease = w.act(lambda led: led.sign_lease(day(11, 2), w.tenants[2], SPARE[0], 12000, [], date(2026, 12, 15)))
    e = w.act(lambda led: protection.offer(led, lease, FRONT, day(11, 2)))
    n = notice(w, e, day(11, 2))
    d = w.cmd("enroll_plan", day(11, 20), MANAGER, about=e, form={"basis": "owner_rule", "notice": n})
    assert not d.allowed and "lease" in d.check and w.state().enrolments[e].state == "offered"


def test_a_plan_charge_is_refused_when_coverage_only_starts_after_the_lease_ended():
    w = World()
    e = w.offer(0, day(11, 2))
    acc = w.accept(e, day(11, 3), starts=date(2026, 11, 20))
    w.act(lambda led: led.end_lease(day(11, 15), w.leases[0]))
    # an activation written before the check existed (the command now refuses it), so the billing check must still hold on its own
    w.act(lambda led: led.emit("protection.enrolled", day(11, 16), {"enrolment": e, "starts": "2026-11-20", "basis": "acceptance",
                                                                    "authority": {"acceptance": acc}, "by": MANAGER}, f"protection:enrol:{e}", source=MANAGER))
    d = w.charge(0, "2026-11", day(11, 17))
    assert (d.allowed, d.why) == (False, "plan_ended")


def test_coverage_cannot_start_before_the_selected_version_takes_effect():
    import pytest
    from core.billing import Refused
    from tests.protection_world import TERMS_V2
    w = World()
    w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1500, TERMS_V2, date(2026, 12, 1), FRONT, day(11, 1)))
    e = w.offer(0, day(11, 3), version=2)                                     # future terms can be offered
    with pytest.raises(Refused, match="takes effect 2026-12-01"):
        w.accept(e, day(11, 4), starts=date(2026, 11, 5))                      # but coverage cannot begin on them early
    assert w.state().enrolments[e].state == "offered"
    w.accept(e, day(11, 4), starts=date(2026, 12, 1))                         # on or after the effective date it can
    w.enrol(e, day(11, 5))
    assert w.state().enrolments[e].state == "active"
    assert not w.charge(0, "2026-11", day(11, 6)).allowed                      # no November charge at version 2's price


def test_the_owner_rule_cannot_start_coverage_before_the_version_takes_effect():
    from tests.protection_world import TERMS_V2
    from tests.test_core_protection_commands import notice, rule_world
    w, _ = rule_world()
    w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1500, TERMS_V2, date(2026, 12, 1), FRONT, day(11, 1)))
    e = w.act(lambda led: protection.offer(led, w.leases[2], FRONT, day(11, 2), version=2))
    n = notice(w, e, day(11, 2))
    d = w.cmd("enroll_plan", day(11, 20), MANAGER, about=e, form={"basis": "owner_rule", "notice": n})
    assert (d.allowed, d.why) == (False, "before_effective") and "2026-12-01" in d.check
    assert w.state().enrolments[e].state == "offered"
    d = w.cmd("enroll_plan", day(12, 2), MANAGER, about=e, form={"basis": "owner_rule", "notice": n})
    assert d.allowed and w.state().enrolments[e].starts == date(2026, 12, 2)


def test_a_staff_rule_stored_under_the_plan_rule_name_is_not_the_owner_rule_and_refuses_cleanly():
    w = World()
    w.act(lambda led: led.adopt_rule(day(10, 1), protection.RULE, 1, "Send plan questions to staff.", {"action": "send_to_staff", "actions": ["explain_charge"]}))
    e = w.offer(1, day(11, 2))
    n = w.act(lambda led: protection.send_notice(led, e, FRONT, day(11, 2), "email", "We will add the protection plan at $12.00 a month because we have no proof."))
    d = w.cmd("enroll_plan", day(11, 30), MANAGER, about=e, form={"basis": "owner_rule", "notice": n})
    assert not d.allowed and "owner rule" in d.check and w.state().enrolments[e].state == "offered"


def test_a_hand_posted_plan_charge_with_no_authority_record_cannot_spend_tenant_credit():
    w = World()
    w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    w.accepted_and_active(1, day(11, 1), starts=date(2026, 11, 1))
    for k in (0, 1):
        w.act(lambda led, k=k: led.receive_payment(w.tenants[k], 5000, day(11, 2)))                      # $50 of credit each, nothing owed
    hand = w.act(lambda led: led.charge_by_hand(day(11, 3), w.leases[0], PROTECTION, 1200, f"protection:{w.leases[0]}:2026-11", date(2026, 11, 3)))
    ch = w.books().charges[hand]
    assert ch.open_cents == 1200 and w.books().credit[w.tenants[0]] == 5000                                # right lease, month and price, but nobody authorised it
    assert "no record" in protection.unsupported(w.books(), hand) and w.books().held(hand)
    d = w.charge(1, "2026-11", day(11, 3))                                                                # the authorised path is recorded first, then credit applies
    assert d.allowed and w.books().charges[d.made].open_cents == 0 and w.books().credit[w.tenants[1]] == 3800
    assert not w.books().held(d.made) and protection.unsupported(w.books(), d.made) is None


DOC = {"carrier": "Sunrise Mutual", "number": "8A-44521", "document": "declarations page.pdf"}
REVIEW = {"outcome": "accepted", "how": "Read the declarations page: carrier, number and dates match what was typed in.", "checked": ["carrier", "policy number", "dates"]}


def policy(w, k, at, effective, expires, reviewed_on=None, **more):
    d = w.cmd("record_policy_evidence", at, FRONT, lease=w.leases[k], form={**DOC, "effective": effective, "expires": expires, **more})
    assert d.allowed, d.check
    if reviewed_on:
        assert w.cmd("verify_policy", reviewed_on, MANAGER, about=d.made, form=REVIEW).allowed
    return d.made


def by_rule(w, e, n, at):
    return w.cmd("enroll_plan", at, MANAGER, about=e, form={"basis": "owner_rule", "notice": n})


def test_an_unreviewed_renewal_does_not_hide_the_reviewed_policy_that_is_still_current():
    from tests.test_core_protection_commands import notice, rule_world
    w, e = rule_world()
    n = notice(w, e, day(11, 2))
    old = policy(w, 1, day(11, 1), "2026-01-01", "2026-11-30", reviewed_on=day(11, 3))
    renewal = policy(w, 1, day(11, 15), "2026-12-01", "2027-11-30", renews=old)                  # uploaded, nobody has reviewed it
    d = by_rule(w, e, n, day(11, 20))
    assert (d.allowed, d.why) == (False, "proof_on_file") and w.state().enrolments[e].state == "offered"
    assert w.state().evidence[renewal].status == "received"                                      # still waiting for review, and not replaced the proof on file
    d = by_rule(w, e, n, day(12, 5))                                                              # the reviewed policy ended Nov 30; the renewal is still unreviewed
    assert d.allowed


def test_a_reviewed_policy_that_has_not_started_is_not_current_proof():
    from tests.test_core_protection_commands import notice, rule_world
    w, e = rule_world()
    n = notice(w, e, day(11, 2))
    policy(w, 1, day(11, 3), "2026-12-01", "2027-11-30", reviewed_on=day(11, 4))
    d = by_rule(w, e, n, day(11, 20))
    assert d.allowed and w.state().enrolments[e].state == "active"                               # nothing covered Nov 20, so proof was missing


def test_a_reviewed_policy_with_no_start_date_counts_as_started_so_the_tenant_is_not_enrolled():
    from tests.test_core_protection_commands import notice, rule_world
    w, e = rule_world()
    n = notice(w, e, day(11, 2))
    policy(w, 1, day(11, 3), None, "2027-11-30", reviewed_on=day(11, 4))
    d = by_rule(w, e, n, day(11, 20))
    assert (d.allowed, d.why) == (False, "proof_on_file")


def test_a_transfer_that_cannot_make_its_new_offer_leaves_the_original_plan_active():
    import pytest
    from core.billing import Refused
    w = World()
    old = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    new = w.act(lambda led: led.sign_lease(day(11, 15, 10), w.tenants[0], SPARE[0], 14000, [], date(2026, 11, 15)))
    w.act(lambda led: protection.offer(led, new, FRONT, day(11, 15, 11)))        # the destination already has an open offer
    seen = {}

    def attempt(led):
        with pytest.raises(Refused, match="already open"):
            protection.reoffer(led, old, new, FRONT, day(11, 16))
        seen["pending"] = [e.kind for e in led.pending]             # a caller that catches the refusal still appends what is pending, as the gate does
        seen["state"] = protection.state(led.books).enrolments[old].state
    w.act(attempt)
    assert seen == {"pending": [], "state": "active"}
    rec = w.state().enrolments[old]
    assert rec.state == "active" and rec.ended is None
    assert not [e for e in w.store.stream_events("visitor:a") if e.kind == "protection.ended"]


def test_unused_days_come_only_from_the_charge_that_belongs_to_the_enrolment_being_ended():
    from tests.protection_world import TERMS_V2
    w = World()
    v1 = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    nov = w.charge(0, "2026-11", day(11, 1, 10)).made                             # version 1 was billed for November
    w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1500, TERMS_V2, date(2026, 11, 2), FRONT, day(11, 2)))
    v2 = w.offer(0, day(11, 9), version=2, replaces=v1)
    w.accept(v2, day(11, 9, 10), starts=date(2026, 11, 10))
    w.enrol(v2, day(11, 9, 11))                                                    # version 2 takes over Nov 10 and version 1 ends
    first = w.state().enrolments[v1].ended
    assert (first["on"], first["unused_days"], first["charge"]) == (date(2026, 11, 10), 21, nov)
    w.act(lambda led: protection.end_enrolment(led, v2, date(2026, 11, 20), "cancellation", FRONT, day(11, 19)))
    second = w.state().enrolments[v2].ended
    assert (second["unused_days"], second["unused_cents"], second["charge"]) == (0, 0, None)     # no charge of its own for November: nothing against version 1's


def test_a_plan_that_starts_and_ends_on_the_same_day_covers_no_month_and_posts_no_charge():
    w = World()
    e = w.accepted_and_active(0, day(11, 15), starts=date(2026, 11, 15))
    w.act(lambda led: protection.end_enrolment(led, e, date(2026, 11, 15), "cancellation", FRONT, day(11, 15, 12)))      # the end day is not covered
    rec = w.state().enrolments[e]
    assert not rec.covers("2026-11") and rec.periods(date(2027, 1, 1)) == []
    d = w.charge(0, "2026-11", day(11, 16))
    assert (d.allowed, d.why) == (False, "plan_ended") and not [c for c in w.books().charges.values() if c.kind == PROTECTION]


def test_a_plan_that_covers_a_single_day_is_still_eligible_for_that_month():
    w = World()
    e = w.accepted_and_active(0, day(11, 15), starts=date(2026, 11, 15))
    w.act(lambda led: protection.end_enrolment(led, e, date(2026, 11, 16), "cancellation", FRONT, day(11, 15, 12)))      # covers November 15 only
    rec = w.state().enrolments[e]
    assert rec.covers("2026-11") and rec.periods(date(2027, 1, 1)) == ["2026-11"]
    assert w.charge(0, "2026-11", day(11, 16)).allowed


def _queue(w, now):
    from core import work
    work.sweep(w.store, "visitor:a", now)
    books, rent, wk = work.load(w.store, "visitor:a")
    return {i.key: i for i in work.queue(books, rent, wk, now)}


def test_reviewing_the_document_a_statement_asked_for_closes_that_statement_request_only():
    w = World()
    said = w.cmd("record_policy_evidence", day(11, 1), FRONT, lease=w.leases[0], text="I have my own policy with Sunrise Mutual.")
    other = w.cmd("record_policy_evidence", day(11, 1, 10), FRONT, lease=w.leases[1], text="I have a policy too.")
    assert said.allowed and other.allowed
    key = f"protection:review:{said.made}"
    assert _queue(w, day(11, 2))[key].state == "open"
    doc = policy(w, 0, day(11, 3), "2026-11-01", "2027-10-31")                                    # the requested document arrives
    assert w.state().evidence[doc].renews == said.made                                            # linked to the statement it answers
    assert _queue(w, day(11, 3, 12))[key].state == "open"                                          # not reviewed yet: the request stands
    elsewhere = policy(w, 2, day(11, 3), "2026-11-01", "2027-10-31", reviewed_on=day(11, 4))       # another tenant's reviewed upload is unrelated
    q = _queue(w, day(11, 4, 12))
    assert q[key].state == "open" and q[f"protection:review:{other.made}"].state == "open"
    assert w.cmd("verify_policy", day(11, 5), MANAGER, about=doc, form=REVIEW).allowed
    q = _queue(w, day(11, 5, 12))
    assert q[key].state == "done" and q[f"protection:review:{other.made}"].state == "open" and elsewhere


def test_a_reviewed_upload_closes_the_statement_it_names_and_not_a_later_one():
    w = World()
    first = w.cmd("record_policy_evidence", day(11, 1), FRONT, lease=w.leases[0], text="I have my own policy with Sunrise Mutual.").made
    second = w.cmd("record_policy_evidence", day(11, 1, 10), FRONT, lease=w.leases[0], text="Also a second policy.").made
    assert _queue(w, day(11, 2))[f"protection:review:{first}"].state == "open"
    doc = policy(w, 0, day(11, 3), "2026-11-01", "2027-10-31", reviewed_on=day(11, 4), renews=first)
    q = _queue(w, day(11, 5))
    assert q[f"protection:review:{first}"].state == "done" and q[f"protection:review:{second}"].state == "open" and doc


def test_a_document_that_expires_unread_closes_its_task_without_saying_staff_reviewed_it():
    w = World()
    unread = policy(w, 0, day(11, 1), "2026-01-01", "2026-11-05")
    checked = policy(w, 1, day(11, 1), "2026-01-01", "2026-11-05")
    assert _queue(w, day(11, 2))[f"protection:review:{unread}"].state == "open"
    assert w.cmd("verify_policy", day(11, 3), MANAGER, about=checked, form=REVIEW).allowed
    protection.sweep(w.store, "visitor:a", day(11, 7, 6))
    q = _queue(w, day(11, 7, 7))
    assert w.state().evidence[unread].status == "expired" and w.state().evidence[unread].review is None
    assert (q[f"protection:review:{unread}"].state, q[f"protection:review:{unread}"].evidence) == (
        "cancelled", "The policy ended before staff reviewed it, so it is recorded as expired.")
    assert (q[f"protection:review:{checked}"].state, q[f"protection:review:{checked}"].evidence) == ("done", "Staff reviewed the document and recorded the result.")


def test_a_word_that_a_renewal_was_asked_for_does_not_hide_the_policy_expired_item_once_it_lapses():
    from core import work
    w = World()
    old = policy(w, 0, day(10, 1), "2025-12-06", "2026-12-05", reviewed_on=day(10, 2))
    key = f"protection:expiring:{old}"
    assert _queue(w, day(11, 20))[key].state == "open"
    fin = work.run(w.store, "visitor:a", work.Act("f1", "finish_work", MANAGER, key, text="Called the tenant and asked for the renewal."), day(11, 20, 10))
    assert fin.allowed
    assert _queue(w, day(11, 21))[key].state == "done"                                                # the reminder is done while the policy still runs
    protection.sweep(w.store, "visitor:a", day(12, 6, 6))
    item = _queue(w, day(12, 6, 7))[key]
    assert item.what == "Policy expired" and item.state not in work.CLOSED and item.cycle == 2          # the lapse is a new problem: open again
    again = work.sweep(w.store, "visitor:a", day(12, 6, 8))
    assert again == 0                                                                                  # a repeat sweep of the same lapse writes nothing
    late = work.run(w.store, "visitor:a", work.Act("f2", "finish_work", MANAGER, key, text="Tenant says it renews next week."), day(12, 7))
    assert late.allowed and _queue(w, day(12, 7, 1))[key].state == "done"                              # a word said after the lapse holds it


def test_a_plan_charge_for_a_month_that_does_not_exist_is_held_and_nothing_raises():
    from core import work
    w = World()
    w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    bad = w.act(lambda led: led.charge_by_hand(day(11, 2), w.leases[0], PROTECTION, 1200, f"protection:{w.leases[0]}:2026-13", date(2026, 11, 2)))
    assert "month" in protection.unsupported(w.books(), bad)
    assert w.books().held(bad)
    w.act(lambda led: led.receive_payment(w.tenants[0], 1200, day(11, 3)))       # normal payment selection skips the held charge instead of raising
    assert w.books().charges[bad].open_cents == 1200
    books, rent, wk = work.load(w.store, "visitor:a")
    assert [i for i in work.queue(books, rent, wk, day(11, 4)) if i.kind == "plan charge without plan"]


def test_a_transfer_offers_the_product_of_the_plan_it_ends():
    w = World()
    w.act(lambda led: protection.publish_version(led, "storage_cover", 900, "Simulated cover, version 1: a monthly fee for its listed benefits.", date(2026, 10, 1), FRONT, day(10, 1)))
    old = w.act(lambda led: protection.offer(led, w.leases[0], FRONT, day(11, 1, 8), product="storage_cover"))
    w.accept(old, day(11, 1), starts=date(2026, 11, 1))
    w.enrol(old, day(11, 1, 10))
    new_lease = w.act(lambda led: led.sign_lease(day(11, 15, 10), w.tenants[0], SPARE[0], 14000, [], date(2026, 11, 15)))
    w.act(lambda led: led.end_lease(day(11, 14), w.leases[0]))
    new = w.act(lambda led: protection.reoffer(led, old, new_lease, FRONT, day(11, 15, 11)))
    n = w.state().enrolments[new]
    assert (n.product, n.cents) == ("storage_cover", 900)


def test_a_same_lease_transfer_to_a_version_that_does_not_exist_leaves_the_plan_active():
    import pytest
    from core.billing import Refused
    w = World()
    old = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    seen = {}

    def attempt(led):
        with pytest.raises(Refused, match="no version 999"):
            protection.reoffer(led, old, w.leases[0], FRONT, day(11, 16), 999)
        seen["pending"] = [e.kind for e in led.pending]
        seen["state"] = protection.state(led.books).enrolments[old].state
    w.act(attempt)
    assert seen == {"pending": [], "state": "active"}


def test_a_same_lease_transfer_to_a_newer_version_ends_the_old_plan_and_offers_the_new_one():
    from tests.protection_world import TERMS_V2
    w = World()
    old = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1500, TERMS_V2, date(2026, 11, 6), FRONT, day(11, 6)))
    new = w.act(lambda led: protection.reoffer(led, old, w.leases[0], FRONT, day(11, 16), 2))
    st = w.state()
    assert (st.enrolments[old].state, st.enrolments[new].state, st.enrolments[new].version) == ("ended", "offered", 2)


def test_a_same_lease_transfer_is_refused_while_another_offer_is_open_on_the_lease():
    import pytest
    from core.billing import Refused
    from tests.protection_world import TERMS_V2
    w = World()
    old = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1500, TERMS_V2, date(2026, 11, 6), FRONT, day(11, 6)))
    w.offer(0, day(11, 7), replaces=old)                                                          # a replacement is already waiting on this lease
    seen = {}

    def attempt(led):
        with pytest.raises(Refused, match="already open"):
            protection.reoffer(led, old, w.leases[0], FRONT, day(11, 16), 2)
        seen["state"] = protection.state(led.books).enrolments[old].state
    w.act(attempt)
    assert seen == {"state": "active"}


def test_a_statement_whose_upload_passed_review_is_not_the_one_the_next_upload_answers():
    w = World()
    first = w.cmd("record_policy_evidence", day(11, 1), FRONT, lease=w.leases[0], text="I have my own policy with Sunrise Mutual.").made
    second = w.cmd("record_policy_evidence", day(11, 1, 10), FRONT, lease=w.leases[0], text="Also a second policy.").made
    one = policy(w, 0, day(11, 3), "2026-11-01", "2027-10-31", reviewed_on=day(11, 4))             # answers the newest open statement
    assert w.state().evidence[one].renews == second
    two = policy(w, 0, day(11, 5), "2026-11-02", "2027-11-01")
    assert w.state().evidence[two].renews == first                                                    # the one still unanswered, not the completed request


def test_a_plan_on_a_lease_that_ended_cannot_cover_days_after_the_tenant_left():
    import pytest
    from core.billing import Refused
    w = World()
    old = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    w.act(lambda led: led.end_lease(day(11, 14), w.leases[0]))
    left = w.books().leases[w.leases[0]].ended
    seen = {}

    def late(led):
        with pytest.raises(Refused, match="lease ended"):
            protection.end_enrolment(led, old, left + timedelta(days=6), "move-out", FRONT, day(11, 20))
        seen["pending"] = [e.kind for e in led.pending]
    w.act(late)
    assert seen["pending"] == [] and w.state().enrolments[old].state == "active"
    w.act(lambda led: protection.end_enrolment(led, old, left + timedelta(days=1), "move-out", FRONT, day(11, 20)))
    assert w.state().enrolments[old].ended["on"] == left + timedelta(days=1)


def test_a_statement_stays_open_for_the_next_upload_while_its_own_upload_is_unreviewed():
    w = World()
    first = w.cmd("record_policy_evidence", day(11, 1), FRONT, lease=w.leases[0], text="I have my own policy with Sunrise Mutual.").made
    one = policy(w, 0, day(11, 3), "2026-11-01", "2027-10-31")                                        # uploaded, nobody has reviewed it
    assert w.state().evidence[one].renews == first
    two = policy(w, 0, day(11, 5), "2026-11-02", "2027-11-01")
    assert w.state().evidence[two].renews == first
