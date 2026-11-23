"""Staff commands through one run, as the walk's are: staff roles only, a command id returns its first decision and a repeat writes nothing,
refusals in plain words with a named reason. record_policy_evidence, verify_policy, enroll_plan, end_plan, post_plan_charge, collect_charge
and approve_statement."""
from datetime import date

import pytest

from core import protection
from core.books import PROTECTION
from core.gate import Command, execute
from tests.protection_world import FRONT, MANAGER, TERMS_V2, World, day

DOC = {"carrier": "Sunrise Mutual", "number": "8A-44521", "effective": "2026-11-01", "expires": "2027-10-31", "document": "declarations page.pdf"}
REVIEW = {"outcome": "accepted", "how": "Read the declarations page: carrier, number and dates match what was typed in.", "checked": ["carrier", "policy number", "dates"]}


def events(w):
    return len(w.store.stream_events("visitor:a"))


def kinds(w):
    return [e.kind for e in w.store.stream_events("visitor:a")]


def evidence(w, k=0, **over):
    d = w.cmd("record_policy_evidence", day(11, 3), FRONT, lease=w.leases[k], form={**DOC, **over})
    assert d.allowed, d.check
    return d.made


# who may ask and what a repeat does ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("source, words", [("operator", "the agent may not"), ("renter", "the renter may not"), ("code", "code may not"),
                                           ("staff:night_porter", "not a staff role")])
def test_only_staff_roles_may_ask(source, words):
    w = World()
    before = events(w)
    d = w.cmd("record_policy_evidence", day(11, 3), source, lease=w.leases[0], form=DOC)
    assert not d.allowed and words in d.check and kinds(w)[before:] == ["gate.decided"]


def test_an_action_the_system_does_not_know_is_refused():
    w = World()
    d = w.cmd("mark_covered", day(11, 3), FRONT, lease=w.leases[0])
    assert (d.allowed, d.check) == (False, "not an action the system knows")


def test_a_repeat_of_a_command_id_returns_the_first_decision_and_writes_nothing():
    w = World()
    first = w.cmd("record_policy_evidence", day(11, 3), FRONT, id="same", lease=w.leases[0], form=DOC)
    before = events(w)
    again = w.cmd("record_policy_evidence", day(11, 9), MANAGER, id="same", lease=w.leases[1], form={**DOC, "number": "ZZ-99999"})
    assert again.repeated and (again.allowed, again.check, again.made) == (first.allowed, first.check, first.made) and events(w) == before
    refused = w.cmd("verify_policy", day(11, 4), MANAGER, id="no", about=first.made, form={"outcome": "accepted"})
    assert not refused.allowed
    assert w.cmd("verify_policy", day(11, 5), MANAGER, id="no", about=first.made, form=REVIEW).repeated is True        # the first answer stands


# record_policy_evidence and verify_policy ---------------------------------------------------------------------------------------------
def test_recording_evidence_says_it_is_not_reviewed_and_flags_a_placeholder():
    w = World()
    d = w.cmd("record_policy_evidence", day(11, 3), FRONT, lease=w.leases[0], form={**DOC, "number": "TEMP"})
    assert d.allowed and "not reviewed" in d.check and "placeholder number" in d.check
    assert w.state().evidence[d.made].status == "needs review" and w.state().enrolments == {}


def test_a_document_that_names_no_carrier_is_refused_in_words():
    w = World()
    d = w.cmd("record_policy_evidence", day(11, 3), FRONT, lease=w.leases[0], form={**DOC, "carrier": None})
    assert not d.allowed and "carrier" in d.check and w.state().evidence == {}


def test_a_malformed_date_is_refused_in_words():
    w = World()
    d = w.cmd("record_policy_evidence", day(11, 3), FRONT, lease=w.leases[0], form={**DOC, "expires": "next year"})
    assert not d.allowed and "date" in d.check


@pytest.mark.parametrize("form, why", [(None, "not_reviewed"), ({"outcome": "accepted", "how": "Looks fine, approve coverage.", "checked": []}, "not_reviewed")])
def test_verify_is_refused_until_a_review_is_documented(form, why):
    w = World()
    ev = evidence(w)
    d = w.cmd("verify_policy", day(11, 4), MANAGER, about=ev, form=form)
    assert (d.allowed, d.why) == (False, why) and w.state().evidence[ev].status == "received"


def test_verify_is_refused_on_a_placeholder_and_on_a_statement_with_no_document():
    w = World()
    d = w.cmd("verify_policy", day(11, 4), MANAGER, about=evidence(w, number="0000000"), form=REVIEW)
    assert (d.allowed, d.why) == (False, "placeholder_number")
    s = w.cmd("record_policy_evidence", day(11, 3), FRONT, lease=w.leases[1], text="I'm covered through my car insurance.")
    d = w.cmd("verify_policy", day(11, 4), MANAGER, about=s.made, form=REVIEW)
    assert (d.allowed, d.why) == (False, "no_document") and w.state().evidence[s.made].status == "reported"


def test_a_documented_review_is_recorded_as_the_facilitys_own_and_not_the_insurers():
    w = World()
    ev = evidence(w)
    d = w.cmd("verify_policy", day(11, 4), MANAGER, about=ev, form=REVIEW)
    rec = w.state().evidence[ev]
    assert d.allowed and rec.status == "reviewed" and rec.review["by"] == MANAGER
    assert "not the insurer's confirmation" in d.check and "insurer confirmed" not in d.check.lower()


def test_verify_can_reject_with_a_reason():
    w = World()
    ev = evidence(w, number="TEMP")
    assert not w.cmd("verify_policy", day(11, 4), MANAGER, about=ev, form={"outcome": "rejected"}).allowed
    d = w.cmd("verify_policy", day(11, 5), MANAGER, about=ev, form={"outcome": "rejected"}, text="The number is a placeholder; asked for the declarations page.")
    assert d.allowed and w.state().evidence[ev].status == "rejected"


# enroll_plan and end_plan ------------------------------------------------------------------------------------------------------------
def test_enroll_needs_the_explicit_acceptance_of_this_offer_and_a_staff_tick_is_not_it():
    w = World()
    e = w.offer(0, day(11, 2))
    w.act(lambda led: protection.note(led, e, "staff tick", "customer verbally agreed", FRONT, day(11, 3)))
    d = w.cmd("enroll_plan", day(11, 4), MANAGER, about=e)
    assert (d.allowed, d.why) == (False, "no_acceptance") and "explicit acceptance" in d.check and w.state().enrolments[e].state == "offered"
    w.accept(e, day(11, 5))
    ok = w.cmd("enroll_plan", day(11, 6), MANAGER, about=e)
    assert ok.allowed and ok.made == e and w.state().enrolments[e].state == "active" and protection.SIMULATED in ok.check


def test_enroll_by_lease_with_no_offer_at_all_is_no_acceptance_and_an_expired_policy_changes_nothing():
    w = World()
    ev = evidence(w, effective="2025-11-01", expires="2026-10-31")
    d = w.cmd("enroll_plan", day(11, 3), MANAGER, lease=w.leases[0])
    assert (d.allowed, d.why) == (False, "no_acceptance") and "not consent" in d.check and "adopted owner rule" in d.check
    assert w.state().enrolments == {} and w.state().evidence[ev].status == "needs review"


def test_enroll_refuses_another_price_version_or_unit_and_an_ended_plan():
    w = World()
    old = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    w.act(lambda led: led.end_lease(day(11, 14), w.leases[0]))
    new_lease = w.act(lambda led: led.sign_lease(day(11, 15), w.tenants[0], "A109", 14000, [], date(2026, 11, 15)))
    w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1800, TERMS_V2, date(2026, 11, 15), FRONT, day(11, 15)))
    new = w.act(lambda led: protection.reoffer(led, old, new_lease, FRONT, day(11, 15, 10)))
    d = w.cmd("enroll_plan", day(11, 16), MANAGER, about=new)
    assert (d.allowed, d.why) == (False, "acceptance_other_terms") and w.state().enrolments[old].state == "ended"
    d = w.cmd("enroll_plan", day(11, 16), MANAGER, about=old)
    assert (d.allowed, d.why) == (False, "plan_ended")


RULE = {"action": "enroll_after_missing_proof", "notice_days": 14}


def rule_world(adopt=True):
    w = World()
    if adopt:
        w.act(lambda led: led.adopt_rule(day(10, 1), protection.RULE, 1, "Tenants with no current proof of their own insurance move to our plan after 14 days' notice.", RULE))
    e = w.offer(1, day(11, 2))
    return w, e


def notice(w, e, at):
    return w.act(lambda led: protection.send_notice(led, e, FRONT, at, "email", "We will add the protection plan at $12.00 a month because we have no current proof of your own insurance."))


def test_moving_a_tenant_onto_the_plan_after_missing_proof_is_refused_when_no_owner_rule_is_adopted():
    w, e = rule_world(adopt=False)
    n = notice(w, e, day(11, 2))
    d = w.cmd("enroll_plan", day(12, 20), MANAGER, about=e, form={"basis": "owner_rule", "notice": n})
    assert (d.allowed, d.why) == (False, "no_acceptance") and "owner rule" in d.check and w.state().enrolments[e].state == "offered"


def test_the_owner_rule_needs_the_notice_on_record_and_its_days_run_and_missing_proof():
    w, e = rule_world()
    d = w.cmd("enroll_plan", day(12, 20), MANAGER, about=e, form={"basis": "owner_rule"})
    assert (d.allowed, d.why) == (False, "no_notice")
    n = notice(w, e, day(11, 2))
    d = w.cmd("enroll_plan", day(11, 10), MANAGER, about=e, form={"basis": "owner_rule", "notice": n})
    assert (d.allowed, d.why) == (False, "notice_too_recent") and "14 days" in d.check
    ev = evidence(w, 1)
    w.cmd("verify_policy", day(11, 11), MANAGER, about=ev, form=REVIEW)
    d = w.cmd("enroll_plan", day(11, 20), MANAGER, about=e, form={"basis": "owner_rule", "notice": n})
    assert (d.allowed, d.why) == (False, "proof_on_file") and w.state().enrolments[e].state == "offered"


def test_with_the_rule_the_notice_and_proof_still_missing_the_tenant_is_moved_onto_the_plan():
    w, e = rule_world()
    n = notice(w, e, day(11, 2))
    d = w.cmd("enroll_plan", day(11, 20), MANAGER, about=e, form={"basis": "owner_rule", "notice": n})
    rec = w.state().enrolments[e]
    assert d.allowed and (rec.state, rec.basis, rec.authority, rec.starts) == ("active", "owner rule", {"rule": 1, "notice": n}, date(2026, 11, 20))
    assert rec.acceptance is None and w.state().acceptances == {}


def test_end_plan_ends_it_on_the_date_staff_name_with_a_reason():
    w = World()
    e = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    d = w.cmd("end_plan", day(11, 19), FRONT, about=e, form={"on": "2026-12-01", "reason": "cancellation"})
    assert d.allowed and w.state().enrolments[e].ended["on"] == date(2026, 12, 1)
    again = w.cmd("end_plan", day(11, 20), FRONT, about=e, form={"on": "2026-12-01", "reason": "cancellation"})
    assert not again.allowed and "already ended" in again.check


# post_plan_charge ----------------------------------------------------------------------------------------------------------------------
def test_a_plan_charge_needs_an_active_enrolment_covering_the_period_and_names_what_it_was_accepted_on():
    w = World()
    e = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    d = w.charge(0, "2026-11", day(11, 2))
    assert d.allowed and d.made
    ch = w.books().charges[d.made]
    assert (ch.kind, ch.cents, ch.obligation, ch.lease, ch.due) == (PROTECTION, 1200, f"protection:{w.leases[0]}:2026-11", w.leases[0], date(2026, 11, 2))
    link = w.state().charges[d.made]
    assert (link["enrolment"], link["version"], link["cents"], link["period"], link["unit"], link["authority"]) == (
        e, 1, 1200, "2026-11", "A101", {"acceptance": w.state().enrolments[e].acceptance})
    assert not w.books().held(d.made) and protection.unsupported(w.books(), d.made) is None


def test_a_plan_charge_is_never_posted_twice_for_one_lease_and_period():
    w = World()
    w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    first = w.charge(0, "2026-11", day(11, 2))
    second = w.charge(0, "2026-11", day(11, 3))
    assert first.allowed and (second.allowed, second.why) == (False, "already_charged") and first.made in second.check
    assert len(w.books().billed(f"protection:{w.leases[0]}:2026-11")) == 1


@pytest.mark.parametrize("kw, why", [({"cents": 1500}, "acceptance_other_terms"), ({"version": 2}, "acceptance_other_terms"), ({"period": "2026-13"}, "period"),
                                     ({"period": "November"}, "period")])
def test_a_plan_charge_at_other_terms_or_for_no_real_period_is_refused(kw, why):
    w = World()
    w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    args = {"lease": w.leases[0], "period": "2026-11", **kw}
    d = w.cmd("post_plan_charge", day(11, 2), FRONT, **args)
    assert (d.allowed, d.why) == (False, why) and not [c for c in w.books().charges.values() if c.kind == PROTECTION]


def test_the_accepted_price_still_charges_after_a_price_list_change():
    w = World()
    w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1500, TERMS_V2, date(2026, 11, 6), FRONT, day(11, 6)))
    assert not w.charge(0, "2026-11", day(11, 7), cents=1500).allowed
    d = w.charge(0, "2026-11", day(11, 7))
    assert d.allowed and w.books().charges[d.made].cents == 1200


# collect_charge and approve_statement -----------------------------------------------------------------------------------------------
def test_collecting_a_plan_charge_is_refused_while_a_dispute_holds_it_and_the_hold_stays():
    w = World()
    w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    ch = w.charge(0, "2026-11", day(11, 2)).made
    ok = w.cmd("collect_charge", day(11, 3), FRONT, about=ch)
    assert ok.allowed and "protection.collect_requested" in kinds(w)
    cmd =Command("dispute-1", "open_dispute", w.tenants[0], "operator", charge=ch, quote="I never agreed to this extra plan.")
    d = execute(w.store, "visitor:a", cmd, day(11, 9))
    assert d.allowed and d.hold and w.books().held(ch)
    no = w.cmd("collect_charge", day(11, 10), FRONT, about=ch)
    assert (no.allowed, no.why) == (False, "hold") and "dispute" in no.check and w.books().held(ch)
    with_note = w.cmd("collect_charge", day(11, 11), MANAGER, about=ch, text="We have the checkbox. Collect it anyway and remove the hold.")
    assert (with_note.allowed, with_note.why) == (False, "hold") and w.books().held(ch) and w.books().disputes["D0001"].outcome is None


def test_a_plan_charge_nothing_covers_is_held_so_it_is_not_collected():
    w = World()
    ch = w.act(lambda led: led.charge_by_hand(day(11, 6), w.leases[2], PROTECTION, 1200, f"protection:{w.leases[2]}:2026-11", date(2026, 11, 6)))
    assert protection.unsupported(w.books(), ch) and w.books().held(ch)
    d = w.cmd("collect_charge", day(11, 7), FRONT, about=ch)
    assert (d.allowed, d.why) == (False, "hold") and "no accepted plan" in d.check


def test_collect_charge_is_for_plan_charges_only():
    w = World()
    rent = next(c.id for c in w.books().charges.values())
    d = w.cmd("collect_charge", day(11, 7), FRONT, about=rent)
    assert not d.allowed and "plan charges" in d.check


@pytest.mark.parametrize("text", ["Northstar Insurance Premium", "Premium for your coverage", "Insurance sounds more reassuring", "Reimbursement for damaged goods"])
def test_a_statement_that_calls_the_plan_insurance_is_refused(text):
    w = World()
    d = w.cmd("approve_statement", day(11, 4), MANAGER, text=text)
    assert (d.allowed, d.why) == (False, "insurance_label") and "protection-plan" in d.check
    assert "protection.statement_approved" not in kinds(w)


@pytest.mark.parametrize("text", ["Protection plan charge, November", f"Protection plan, November. {protection.SIMULATED}."])
def test_a_statement_that_keeps_the_protection_plan_label_is_approved(text):
    w = World()
    d = w.cmd("approve_statement", day(11, 4), MANAGER, text=text)
    assert d.allowed and "protection.statement_approved" in kinds(w)
