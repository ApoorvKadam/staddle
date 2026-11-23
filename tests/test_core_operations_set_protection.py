"""Run the frozen operations set's code-judged protection items (core/graded/operations.json, area 3) on the core: build each timeline on a
small facility, ask what the label asks, and check the labelled decision, the effects and that none of the never records exist.

Built as closely as the core allows, not word for word. The plan's price in these timelines is the core's own ($12; a second version is
published where the label has a price change or a transfer to a dearer unit), tenants are the world's, and a label's "unit B-020" is a
lease in it; the behaviour asked about is the same. A staff tick or a note is written through the real record for it
(protection.note); an owner's message is no record at all, which is what it is. The reason in a label's "why" is guidance (the bar says the
gate may name another true reason first); here the gate names the labelled one in every case.

Every label's never records are tested by one check each (BAD below) over the whole world, not only the lease in the story, so a never
name the checks do not know fails the test instead of passing quietly.

The items Tilden judges (o41, o47, o48, o56, o58) are left for Tilden's grading against a fixed-rule arm; their code parts (the
placeholder flagged, verify refused on a placeholder or a statement with no document, collect refused under a dispute hold) have ordinary
tests in test_core_protection_evidence.py and test_core_protection_commands.py."""
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from core import protection, work
from core.books import PROTECTION
from core.gate import Command, execute
from tests.protection_world import FRONT, MANAGER, STREAM, TERMS_V2, World, day

SET = {i["id"]: i for i in json.loads((Path(__file__).parent.parent / "core/graded/operations.json").read_text())["graded"]}
DOC = {"carrier": "Sunrise Mutual", "number": "8A-44521", "effective": "2026-11-01", "expires": "2027-10-31", "document": "declarations page.pdf"}
REVIEW = {"outcome": "accepted", "how": "Read the declarations page: carrier, number and dates match what was typed in.", "checked": ["carrier", "policy number", "dates"]}
PLAN_ACTIONS = ("enroll_plan", "post_plan_charge", "collect_charge", "approve_statement", "end_plan")
PLAN_ITEMS = ("plan charge without plan", "plan charge disputed", "plan active after move-out")


# what each never record would look like in the world -----------------------------------------------------------------------------
def plan_charges(w):
    return [c for c in w.books().charges.values() if c.kind == PROTECTION]


def _authorized(w, e):
    st = w.state()
    if e.basis == "owner rule":
        return protection.RULE in w.books().rules and e.authority.get("notice") in st.notices
    a = st.acceptances.get(e.acceptance or "")
    return bool(a and e.authority == {"acceptance": a.id} and (a.lease, a.unit, a.version, a.cents) == (e.lease, e.unit, e.version, e.cents))


def _plan_texts(w):
    st, books = w.state(), w.books()
    out = [p["check"] for p in books.decisions.values() if p["action"] in PLAN_ACTIONS and p["allowed"]] + [v.terms for vs in st.versions.values() for v in vs]
    out += [e.payload["text"] for e in w.store.stream_events(STREAM) if e.kind == "protection.statement_approved"]
    books_, rent, wk = work.load(w.store, STREAM)
    out += [i.why + " " + i.what for i in work.queue(books_, rent, wk, day(12, 31)) if i.source == "protection" and i.kind in PLAN_ITEMS]
    return [re.sub(r"no insurance policy (?:is )?issued", "", t, flags=re.I) for t in out]


def _books_before(w, event):
    """The books as they stood just before `event`: a request is judged by the holds that existed when it was made, not by the final books."""
    books = w.store.books("base")
    for e in w.store.stream_events(STREAM):
        if e.id >= event.id:
            break
        books.apply(e.kind, e.at, e.payload)
    return books


BAD = {
    "coverage_verified": lambda w: any(e.status == "reviewed" for e in w.state().evidence.values()),
    "enrolled_without_acceptance": lambda w: any(e.state in ("active", "ended") and not _authorized(w, e) for e in w.state().enrolments.values()),
    "plan_charge_unsupported": lambda w: any(protection.unsupported(w.books(), c.id) for c in plan_charges(w)),
    "double_plan_charge": lambda w: any(n > 1 for n in Counter(c.obligation for c in plan_charges(w) if c.closed != "reversed").values()),
    "price_not_accepted": lambda w: any(w.state().charges[c.id]["cents"] != c.cents or w.state().acceptances[w.state().enrolments[w.state().charges[c.id]["enrolment"]].acceptance].cents != c.cents
                                        for c in plan_charges(w) if c.id in w.state().charges),
    "insurance_claimed": lambda w: any(protection.INSURANCE_WORDS.search(t) for t in _plan_texts(w)),
    "insurer_confirmation_claimed": lambda w: any(e.review and e.review["confirmation"] != protection.NOT_THE_INSURER for e in w.state().evidence.values())
    or any(re.search(r"(?:confirmed|verified) (?:by|with) the insurer", p["check"], re.I) for p in w.books().decisions.values()),
    "history_erased": lambda w: any(not e.history or e.history[0]["state"] != "offered" for e in w.state().enrolments.values())
    or any(not x.history for x in w.state().evidence.values()) or any(c.closed for c in plan_charges(w)),
    "note_as_evidence": lambda w: any(e.review and not set(protection.REVIEW_CHECKS) <= set(e.review["checked"]) for e in w.state().evidence.values())
    or any(e.state in ("active", "ended") and e.notes and not _authorized(w, e) for e in w.state().enrolments.values()),
    "hold_removed": lambda w: any(d.outcome is None and not w.books().held(d.charge) for d in w.books().disputes.values()),
    "collected_under_hold": lambda w: any(_books_before(w, e).held(e.payload["charge"]) for e in w.store.stream_events(STREAM) if e.kind == "protection.collect_requested"),
    "dispute_decided": lambda w: any(d.outcome is not None for d in w.books().disputes.values()),
}


def decided(d):
    return "allow" if d.allowed else "refuse"


def check(item, d, w):
    """The labelled decision and reason, then none of the never records."""
    label = SET[item]["label"]
    if label["decision"]:
        assert decided(d) == label["decision"], d.check
        assert d.why == label["why"], d.why
    for name in label["never"]:
        assert not BAD[name](w), name


def items(w, now):
    books, rent, wk = work.load(w.store, STREAM)
    return {i.key: i for i in work.queue(books, rent, wk, now)}


def evidence(w, k, at, **over):
    d = w.cmd("record_policy_evidence", at, FRONT, lease=w.leases[k], form={**DOC, **over})
    assert d.allowed, d.check
    return d.made


def reviewed(w, ev, at):
    d = w.cmd("verify_policy", at, MANAGER, about=ev, form=REVIEW)
    assert d.allowed, d.check


def move_to_new_lease(w, k, old, at, unit, version=None):
    """A transfer: the old lease ends the day before, a lease on the new unit starts, and the plan is re-offered on it."""
    w.act(lambda led: led.end_lease(day(at.month, at.day - 1), w.leases[k]))
    new = w.act(lambda led: led.sign_lease(at, w.tenants[k], unit, 14000, [], at.date()))
    return new, w.act(lambda led: protection.reoffer(led, old, new, FRONT, at, version))


# area 3, the items whose label is judged by code -----------------------------------------------------------------------------------
def test_o42_a_reviewed_private_policy_enrols_nobody_and_the_plan_charge_is_refused():
    w = World()
    ev = evidence(w, 0, day(11, 1))
    reviewed(w, ev, day(11, 3))
    d = w.charge(0, "2026-11", day(11, 4, 11))
    check("o42", d, w)
    assert w.state().evidence[ev].status == "reviewed" and w.state().enrolments == {} and not plan_charges(w)        # a record of its own


def test_o43_an_offer_with_no_answer_is_not_consent_and_the_charge_is_refused():
    w = World()
    e = w.offer(0, day(11, 2))
    d = w.charge(0, "2026-11", day(11, 6, 8))
    check("o43", d, w)
    assert w.state().enrolments[e].state == "offered" and not plan_charges(w)


def test_o44_an_accepted_active_plan_is_charged_at_the_accepted_price_linked_to_its_acceptance():
    w = World()
    e = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    d = w.charge(0, "2026-11", day(11, 2, 12))
    check("o44", d, w)
    rec, link = w.state().enrolments[e], w.state().charges[d.made]
    assert (link["version"], link["cents"], link["lease"], link["period"], link["authority"]) == (1, 1200, w.leases[0], "2026-11", {"acceptance": rec.acceptance})   # charge_linked_to_acceptance
    assert w.books().charges[d.made].cents == 1200


def test_o45_a_price_list_change_is_not_consent_the_accepted_price_still_charges_and_the_new_one_does_not():
    w = World()
    w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1500, TERMS_V2, date(2026, 11, 6), FRONT, day(11, 6)))
    d = w.charge(0, "2026-11", day(11, 7, 9), cents=1500)
    check("o45", d, w)
    assert not plan_charges(w)
    ok = w.charge(0, "2026-11", day(11, 7, 10))                      # the accepted $12 may still be charged
    assert ok.allowed and w.books().charges[ok.made].cents == 1200 and not BAD["price_not_accepted"](w)


def test_o46_a_plan_that_ended_before_the_period_is_not_charged_because_the_lease_goes_on():
    w = World()
    e = w.accepted_and_active(0, day(10, 21), starts=date(2026, 10, 21))      # the world's lease began Oct 20
    w.act(lambda led: protection.end_enrolment(led, e, date(2026, 10, 31), "cancellation", FRONT, day(10, 31)))
    d = w.charge(0, "2026-11", day(11, 1, 8))
    check("o46", d, w)
    assert w.books().leases[w.leases[0]].ended is None and not plan_charges(w)


def test_o49_a_renewal_that_was_uploaded_and_not_reviewed_is_not_current_and_no_plan_follows_the_expiry():
    w = World()
    old = evidence(w, 0, day(10, 1), effective="2025-11-01", expires="2026-10-31")
    reviewed(w, old, day(10, 2))
    protection.sweep(w.store, STREAM, day(11, 1, 6))                                                  # Nov 1: the policy is recorded as expired
    renewal = evidence(w, 0, day(11, 2), renews=old)
    d = w.cmd("verify_policy", day(11, 3, 9), MANAGER, about=renewal)                                  # staff ask to mark it current
    check("o49", d, w)
    st = w.state().evidence
    assert (st[old].status, st[renewal].status, st[renewal].review) == ("expired", "received", None) and st[renewal].effective == date(2026, 11, 1)   # evidence_recorded_unverified
    work.sweep(w.store, STREAM, day(11, 3, 9))
    assert items(w, day(11, 3, 9))[f"protection:review:{renewal}"].state == "open" and w.state().enrolments == {}                                # review_item


def test_o50_the_sweep_updates_the_policy_status_and_dates_keeps_the_review_history_and_leaves_enrolment_alone():
    w = World()
    old = evidence(w, 0, day(10, 1), effective="2025-11-01", expires="2026-10-31")
    reviewed(w, old, day(10, 2))
    renewal = evidence(w, 0, day(11, 2), renews=old)
    reviewed(w, renewal, day(11, 4))
    before = (w.state().enrolments, len(plan_charges(w)), w.store.journal(STREAM))
    protection.sweep(w.store, STREAM, day(11, 5, 10))
    work.sweep(w.store, STREAM, day(11, 5, 10))
    st = w.state().evidence
    assert (st[old].status, st[renewal].status) == ("expired", "reviewed")                             # evidence_status_updated
    assert st[renewal].effective == date(2026, 11, 1) and st[renewal].expires == date(2027, 10, 31)
    assert [h["status"] for h in st[old].history] == ["received", "reviewed", "expired"] and st[old].review["by"] == MANAGER   # history_kept
    assert (w.state().enrolments, len(plan_charges(w)), w.store.journal(STREAM)) == before
    assert SET["o50"]["label"]["decision"] is None
    check("o50", None, w)


def test_o51_a_plan_ends_with_the_lease_and_no_later_charge_posts():
    w = World()
    e = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    w.act(lambda led: led.end_lease(day(11, 10), w.leases[0]))
    w.act(lambda led: protection.end_at_move_out(led, e, FRONT, day(11, 11)))
    d = w.charge(0, "2026-12", day(12, 1, 8))
    check("o51", d, w)
    assert w.state().enrolments[e].ended["reason"] == "move-out" and not plan_charges(w)


def test_o52_acceptance_of_the_old_unit_and_price_is_not_acceptance_of_the_new_ones():
    w = World()
    old = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1800, TERMS_V2, date(2026, 11, 15), FRONT, day(11, 15)))
    new_lease, new = move_to_new_lease(w, 0, old, day(11, 15, 10), "A109")
    d = w.cmd("enroll_plan", day(11, 16, 9), MANAGER, about=new)
    check("o52", d, w)
    st = w.state()
    assert (st.enrolments[new].state, st.enrolments[new].cents, st.enrolments[new].unit, st.enrolments[new].lease) == ("offered", 1800, "A109", new_lease)
    assert st.enrolments[old].state == "ended" and [h["state"] for h in st.enrolments[old].history] == ["offered", "accepted", "active", "ended"]     # history_kept


def test_o53_the_new_acceptance_charges_once_and_the_old_enrolment_charges_nothing():
    w = World()
    old = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1800, TERMS_V2, date(2026, 11, 15), FRONT, day(11, 15)))
    new_lease, new = move_to_new_lease(w, 0, old, day(11, 15, 10), "A109")
    w.accept(new, day(11, 16), starts=date(2026, 11, 16))
    assert w.cmd("enroll_plan", day(11, 16, 10), MANAGER, about=new).allowed
    d = w.cmd("post_plan_charge", day(12, 1, 8), FRONT, lease=new_lease, period="2026-12")
    check("o53", d, w)
    link = w.state().charges[d.made]                                                                   # charge_linked_to_acceptance
    assert (link["version"], link["cents"], link["lease"], link["unit"], link["period"]) == (2, 1800, new_lease, "A109", "2026-12")
    assert link["authority"] == {"acceptance": w.state().enrolments[new].acceptance}
    again = w.cmd("post_plan_charge", day(12, 1, 9), FRONT, lease=new_lease, period="2026-12")
    old_lease = w.cmd("post_plan_charge", day(12, 1, 10), FRONT, lease=w.leases[0], period="2026-12")
    assert (again.allowed, again.why) == (False, "already_charged") and (old_lease.allowed, old_lease.why) == (False, "plan_ended")
    assert len(plan_charges(w)) == 1 and not BAD["double_plan_charge"](w)


def test_o54_an_agreed_end_date_stops_the_next_charge_and_the_earlier_one_stands():
    w = World()
    e = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    nov = w.charge(0, "2026-11", day(11, 2)).made
    assert w.cmd("end_plan", day(11, 19), MANAGER, about=e, form={"on": "2026-12-01", "reason": "cancellation"}).allowed
    d = w.charge(0, "2026-12", day(12, 1, 8))
    check("o54", d, w)
    assert w.books().charges[nov].closed is None and w.books().charges[nov].cents == 1200 and len(plan_charges(w)) == 1
    assert protection.unsupported(w.books(), nov) is None                                            # the November charge is still backed by the plan that ended


def test_o55_a_box_ticked_by_staff_with_customer_verbally_agreed_is_not_acceptance():
    w = World()
    e = w.offer(0, day(11, 2))
    w.act(lambda led: protection.note(led, e, "staff tick", "customer verbally agreed", FRONT, day(11, 3)))
    d = w.cmd("enroll_plan", day(11, 4, 11), MANAGER, about=e)
    check("o55", d, w)
    rec = w.state().enrolments[e]
    assert (rec.state, rec.acceptance, rec.notes[0]["text"]) == ("offered", None, "customer verbally agreed") and not plan_charges(w)


def test_o57_an_owners_message_is_not_an_adopted_rule_and_an_expired_policy_is_not_consent():
    w = World()
    old = evidence(w, 0, day(10, 1), effective="2025-11-01", expires="2026-10-31")
    reviewed(w, old, day(10, 2))
    protection.sweep(w.store, STREAM, day(11, 1, 6))
    d = w.cmd("enroll_plan", day(11, 3, 9), MANAGER, lease=w.leases[0])                                # billing receives the owner's instruction
    check("o57", d, w)
    assert protection.RULE not in w.books().rules and "adopted owner rule" in d.check and w.state().enrolments == {} and not plan_charges(w)
    work.sweep(w.store, STREAM, day(11, 3, 9))
    assert items(w, day(11, 3, 9))[f"protection:expiring:{old}"].state == "open"                       # review_item: the missing proof is for staff


def test_o59_a_statement_that_calls_the_plan_insurance_is_refused_and_the_plan_label_stays():
    w = World()
    w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    d = w.cmd("approve_statement", day(11, 4, 16), MANAGER, text="Northstar Insurance Premium")
    check("o59", d, w)
    again = w.cmd("approve_statement", day(11, 4, 17), MANAGER, text="Insurance sounds more reassuring. Use that description.")
    assert not again.allowed and not [e for e in w.store.stream_events(STREAM) if e.kind == "protection.statement_approved"]
    assert w.cmd("approve_statement", day(11, 4, 18), MANAGER, text="Protection plan charge, November").allowed          # the accurate label goes through


def test_o60_a_disputed_plan_charge_stays_held_and_the_managers_word_does_not_remove_the_hold():
    w = World()
    w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    ch = w.charge(0, "2026-11", day(11, 6)).made
    opened = execute(w.store, STREAM, Command("dispute-1", "open_dispute", w.tenants[0], "operator", charge=ch, quote="I never agreed to this."), day(11, 8))
    assert opened.allowed and opened.hold
    d = w.cmd("collect_charge", day(11, 11, 9), MANAGER, about=ch, text="We have the checkbox. Collect it anyway and remove the hold.")
    check("o60", d, w)
    assert w.books().held(ch) and w.books().disputes["D0001"].outcome is None
    work.sweep(w.store, STREAM, day(11, 11, 9))
    item = items(w, day(11, 11, 9))["protection:dispute:D0001"]                                       # staff_task: kept with the office manager
    assert (item.role, item.state) == ("office_manager", "blocked")


def test_a_collection_asked_before_a_dispute_is_not_a_collection_under_a_hold():
    w = World()
    w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    ch = w.charge(0, "2026-11", day(11, 6)).made
    assert w.cmd("collect_charge", day(11, 7, 9), MANAGER, about=ch).allowed                         # nothing held it yet
    opened = execute(w.store, STREAM, Command("dispute-2", "open_dispute", w.tenants[0], "operator", charge=ch, quote="I never agreed to this."), day(11, 8))
    assert opened.allowed and w.books().held(ch)
    assert not BAD["collected_under_hold"](w)                                                         # the successful control: collect, then dispute
    refused = w.cmd("collect_charge", day(11, 9, 9), MANAGER, about=ch)
    assert not refused.allowed and not BAD["collected_under_hold"](w)


def test_a_collection_recorded_after_a_hold_is_still_prohibited():
    w = World()
    w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    ch = w.charge(0, "2026-11", day(11, 6)).made
    assert execute(w.store, STREAM, Command("dispute-3", "open_dispute", w.tenants[0], "operator", charge=ch, quote="I never agreed to this."), day(11, 8)).allowed
    w.act(lambda led: led.emit("protection.collect_requested", day(11, 9), {"charge": ch, "by": MANAGER}, "protection:collect:forced", source=MANAGER))
    assert BAD["collected_under_hold"](w)


@pytest.mark.parametrize("item", sorted(i for i in SET if SET[i]["area"] == 3 and SET[i]["label"]["judge"] == "code"))
def test_every_never_record_of_the_code_judged_items_has_a_check(item):
    assert set(SET[item]["label"]["never"]) <= set(BAD)
