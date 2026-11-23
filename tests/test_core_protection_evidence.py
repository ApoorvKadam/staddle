"""The tenant's own insurance evidence: a record apart from the facility's plan. Carrier, policy number, dates; reported, document received,
reviewed, rejected, expired, needs review; who reviewed it and how. A placeholder number puts it in needs review. Staff review is the
facility's check of a document, never the insurer's confirmation. An expiry date is required before a review can pass."""
from datetime import date

import pytest

from core import protection
from core.billing import Refused
from tests.protection_world import FRONT, MANAGER, World, day

DOC = {"carrier": "Sunrise Mutual", "number": "8A-44521", "effective": date(2026, 11, 1), "expires": date(2027, 10, 31), "document": "declarations page.pdf"}
CHECKED = ["carrier", "policy number", "dates"]


def record(w, k=0, at=None, **over):
    return w.act(lambda led: protection.record_evidence(led, w.leases[k], FRONT, at or day(11, 3), **{**DOC, **over}))


def review(w, ev, at=None, by=MANAGER, **over):
    args = {"outcome": "accepted", "how": "Read the declarations page: carrier, number and dates match what was typed in.", "checked": CHECKED, **over}
    return w.act(lambda led: protection.review_evidence(led, ev, by, at or day(11, 4), **args))


@pytest.mark.parametrize("number, placeholder", [("TEMP", True), ("temp-123", True), ("0000000", True), ("1111111", True), ("TBD", True), ("N/A", True),
                                                  ("123456789", True), ("", True), ("8A-44521", False), ("POL-2026-77421", False), ("AB1234567", False)])
def test_placeholder_numbers_are_told_from_real_ones(number, placeholder):
    assert protection.placeholder_number(number) is placeholder


def test_a_statement_with_no_document_is_reported_and_nothing_else():
    w = World()
    ev = w.act(lambda led: protection.record_evidence(led, w.leases[0], FRONT, day(11, 2), statement="I'm already fully insured through my car insurance."))
    rec = w.state().evidence[ev]
    assert (rec.status, rec.document, rec.flags, rec.statement) == ("reported", None, (), "I'm already fully insured through my car insurance.")
    assert (rec.lease, rec.unit, rec.tenant, rec.recorded_by, rec.recorded_at) == (w.leases[0], "A101", w.tenants[0], FRONT, day(11, 2))


def test_a_document_is_recorded_as_received_and_not_reviewed():
    w = World()
    ev = record(w)
    rec = w.state().evidence[ev]
    assert (rec.status, rec.carrier, rec.number, rec.effective, rec.expires, rec.flags, rec.review) == (
        "received", "Sunrise Mutual", "8A-44521", date(2026, 11, 1), date(2027, 10, 31), (), None)


def test_a_placeholder_number_puts_it_in_needs_review_and_is_flagged():
    w = World()
    ev = record(w, number="TEMP")
    rec = w.state().evidence[ev]
    assert rec.status == "needs review" and "placeholder number" in rec.flags
    assert [h["status"] for h in rec.history] == ["needs review"]


def test_a_document_with_no_expiry_or_one_already_past_needs_review_too():
    w = World()
    no_expiry, lapsed = record(w, 0, expires=None), record(w, 1, effective=date(2025, 11, 1), expires=date(2026, 10, 31))
    assert w.state().evidence[no_expiry].flags == ("no expiry date",)
    assert w.state().evidence[lapsed].flags == ("already expired",)


@pytest.mark.parametrize("over, why", [
    ({"carrier": None}, "carrier"),
    ({"number": None}, "policy number"),
    ({"effective": date(2027, 11, 1)}, "ends before it starts"),
])
def test_a_document_that_does_not_name_its_carrier_number_or_has_impossible_dates_is_refused(over, why):
    w = World()
    with pytest.raises(Refused, match=why):
        record(w, **over)
    assert w.state().evidence == {}


def test_the_same_document_twice_is_refused_and_a_renewal_is_its_own_record():
    w = World()
    first = record(w)
    with pytest.raises(Refused, match="already on record") as refused:
        record(w, at=day(11, 5))
    assert first not in str(refused.value)      # the words name no record
    renewal = record(w, at=day(11, 6), number="8A-99001", renews=first)
    assert w.state().evidence[renewal].renews == first and len(w.state().evidence) == 2


def test_a_review_that_passes_says_who_what_was_checked_and_that_it_is_not_the_insurers():
    w = World()
    ev = record(w)
    review(w, ev)
    rec = w.state().evidence[ev]
    assert rec.status == "reviewed" and rec.review["by"] == MANAGER and rec.review["checked"] == CHECKED and rec.review["at"] == day(11, 4)
    assert rec.review["confirmation"] == "Staff review of the document; not confirmed with the insurer"
    assert [h["status"] for h in rec.history] == ["received", "reviewed"]
    assert "insurer confirmed" not in str(rec.review).lower() and "verified with the insurer" not in str(rec.review).lower()


@pytest.mark.parametrize("over, why", [
    ({"statement": "I am covered through my car insurance.", "document": None, "carrier": None, "number": None}, "no_document"),
    ({"number": "0000000"}, "placeholder_number"),
    ({"expires": None}, "no_expiry"),
    ({"effective": date(2025, 11, 1), "expires": date(2026, 10, 31)}, "expired"),
])
def test_a_review_cannot_pass_without_a_document_a_real_number_and_an_expiry_that_is_still_ahead(over, why):
    w = World()
    ev = w.act(lambda led: protection.record_evidence(led, w.leases[0], FRONT, day(11, 3), **{**({} if "document" in over else DOC), **over}))
    assert protection.review_check(w.books(), ev, "accepted", "Read the document", CHECKED, "", day(11, 4))[0] == why
    with pytest.raises(Refused):
        review(w, ev)
    assert w.state().evidence[ev].status != "reviewed" and w.state().evidence[ev].review is None


@pytest.mark.parametrize("how, checked", [("Looks fine, approve coverage.", []), ("Looks fine, approve coverage.", ["carrier"]),
                                           ("ok", CHECKED), ("", CHECKED)])
def test_a_note_that_says_it_looks_fine_is_not_a_documented_review(how, checked):
    w = World()
    ev = record(w)
    assert protection.review_check(w.books(), ev, "accepted", how, checked, "", day(11, 4))[0] == "not_reviewed"
    with pytest.raises(Refused, match="no staff review"):
        review(w, ev, how=how, checked=checked)
    assert w.state().evidence[ev].status == "received"


def test_a_rejection_needs_a_reason_and_keeps_the_record():
    w = World()
    ev = record(w, number="TEMP")
    with pytest.raises(Refused, match="why"):
        review(w, ev, outcome="rejected", how="", checked=[])
    w.act(lambda led: protection.review_evidence(led, ev, MANAGER, day(11, 4), "rejected", "", [], "Number is a placeholder; asked for the declarations page."))
    rec = w.state().evidence[ev]
    assert rec.status == "rejected" and rec.review["note"].startswith("Number is a placeholder") and [h["status"] for h in rec.history] == ["needs review", "rejected"]
    with pytest.raises(Refused, match="rejected"):
        review(w, ev)


def test_a_policy_past_its_expiry_becomes_expired_and_keeps_its_history_while_the_renewal_stands_beside_it():
    w = World()
    old =w.act(lambda led: protection.record_evidence(led, w.leases[0], FRONT, day(10, 1), **{**DOC, "effective": date(2025, 11, 1), "expires": date(2026, 12, 31)}))
    review(w, old, at=day(10, 2))
    renewal = record(w, at=day(11, 2), number="8A-77001", effective=date(2027, 1, 1), expires=date(2027, 12, 31), renews=old)
    with pytest.raises(Refused, match="not yet"):
        w.act(lambda led: protection.expire_evidence(led, old, day(11, 3)))
    w.act(lambda led: protection.expire_evidence(led, old, day(1, 2, year=2027)))
    rec = w.state().evidence[old]
    assert rec.status == "expired" and [h["status"] for h in rec.history] == ["received", "reviewed", "expired"] and rec.review["by"] == MANAGER
    assert w.state().evidence[renewal].status == "received" and protection.standing(w.state(), w.leases[0]).id == renewal
    with pytest.raises(Refused, match="already"):
        w.act(lambda led: protection.expire_evidence(led, old, day(1, 3, year=2027)))


def test_recording_and_reviewing_evidence_enrols_nobody_and_moves_no_money():
    w = World()
    before = w.store.journal("visitor:a"), w.store.journal("base")
    ev = record(w)
    review(w, ev)
    st = w.state()
    assert st.enrolments == {} and st.acceptances == {} and (w.store.journal("visitor:a"), w.store.journal("base")) == before
    assert st.evidence[ev].status == "reviewed"
