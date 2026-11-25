"""The monthly rent run bills an approved, noticed increase at its new rent once its effective day has come, and nothing else ever does.
The lease's rent becomes the new amount when the bill is posted; a held, stopped, un-noticed or un-approved increase changes neither."""
from datetime import date

import pytest

from core import price_law as pl
from core.billing import Ledger, Refused
from core.books import at_local
from tests.test_core_price_law_gate import NOW, STREAM, Desk


def florida():
    d = Desk("Northstar Storage", "Orlando, FL")
    assert d.checked("FL").allowed
    assert d.terms(notice_days=30, notice_methods=["first_class_mail"], email_notice_authorized=False).allowed
    return d


def noticed(d):
    """Proposed, approved, noticed and counted: effective 2026-12-23 at 10500."""
    assert d.change("check_price_change", "proposal", NOW).allowed
    assert d.change("approve_price_change", "approval", NOW).allowed
    assert d.notice().allowed
    assert d.change("check_price_change", "notice_completion", NOW).allowed


def rent_run(d, month):
    with d.store.write():
        led = Ledger(d.books())
        made = led.post_rent(month)
        d.store.append(STREAM, led.pending)
    return [d.books().charges[c] for c in made]


def rent_of(d):
    return d.books().leases["L0001"].rent_cents


def test_the_rent_run_after_the_effective_day_posts_the_increase_at_the_new_rent_and_the_lease_follows():
    d = florida()
    noticed(d)
    [december] = rent_run(d, "2026-12")                     # the 1st is before the 23rd: the old rent, nothing changes
    assert december.cents == 10000 and rent_of(d) == 10000 and not d.books().price_law.postings
    [january] = rent_run(d, "2027-01")
    assert january.cents == 10500 and january.due == date(2027, 1, 1) and january.obligation == "rent:L0001:2027-01"
    assert rent_of(d) == 10500
    basis = pl.posted_basis(d.books(), "L0001:2027-01")
    assert basis is not None and basis.change == "PC1" and basis.point == "invoice" and basis.outcome == "allow"
    [february] = rent_run(d, "2027-02")                     # later months bill the lease's rent, which is now the new one
    assert february.cents == 10500


def test_running_the_same_month_again_posts_nothing_more():
    d = florida()
    noticed(d)
    rent_run(d, "2027-01")
    events = len(d.store.events(STREAM))
    assert rent_run(d, "2027-01") == []
    assert len(d.store.events(STREAM)) == events and len([c for c in d.books().charges.values() if c.obligation == "rent:L0001:2027-01"]) == 1


@pytest.mark.parametrize("stopped", ["not approved", "not noticed", "held", "stale"])
def test_an_increase_that_is_not_fully_through_never_takes_effect(stopped):
    d = florida()
    assert d.change("check_price_change", "proposal", NOW).allowed
    if stopped != "not approved":
        assert d.change("approve_price_change", "approval", NOW).allowed
    if stopped not in ("not approved", "not noticed"):
        assert d.notice().allowed and d.change("check_price_change", "notice_completion", NOW).allowed
    if stopped == "held":                                   # staff stopped checking for emergencies: the status is unknown again
        assert d.go("record_emergency_check", place="FL", checked_on="2026-11-23", valid_until="2026-12-01", source="state page", active=[]).allowed
    if stopped == "stale":                                  # the lease's terms changed after the approval
        assert d.terms(notice_days=45, notice_methods=["first_class_mail"]).allowed
    [january] = rent_run(d, "2027-01")
    assert january.cents == 10000 and rent_of(d) == 10000 and not d.books().price_law.postings


def test_a_held_increase_is_retried_by_a_later_run_once_it_clears():
    d = florida()
    noticed(d)
    assert d.go("record_emergency_check", place="FL", checked_on="2026-11-23", valid_until="2026-12-01", source="state page", active=[]).allowed
    [january] = rent_run(d, "2027-01")
    assert january.cents == 10000 and rent_of(d) == 10000
    assert d.go("record_emergency_check", place="FL", checked_on="2026-11-23", valid_until="2027-06-30", source="state page", active=[]).allowed
    [february] = rent_run(d, "2027-02")
    assert february.cents == 10500 and rent_of(d) == 10500 and pl.posted_basis(d.books(), "L0001:2027-02") is not None


def test_an_increase_is_never_posted_over_a_month_already_billed():
    d2 = florida()
    noticed(d2)
    rent_run(d2, "2026-12")
    decision = d2.change("check_price_change", "invoice", at_local(date(2026, 12, 23), 10), effective_on="2026-12-23", invoice="L0001:2026-12")
    with d2.store.write():
        led = Ledger(d2.books())
        with pytest.raises(Refused, match="already billed"):
            pl.post_invoice(led, at_local(date(2026, 12, 23), 10), decision=decision.made)


def test_a_month_before_the_increase_keeps_its_old_rent_when_it_is_billed_afterwards():
    d = florida()
    noticed(d)
    [january] = rent_run(d, "2027-01")                      # December was never run, so the increase posts with January
    assert january.cents == 10500 and rent_of(d) == 10500
    [december] = rent_run(d, "2026-12")                     # billed late, at the rent that month was under
    assert december.cents == 10000 and december.obligation == "rent:L0001:2026-12"
    [february] = rent_run(d, "2027-02")
    assert february.cents == 10500


def test_a_withdrawn_increase_is_never_billed_and_a_billed_one_cannot_be_withdrawn():
    d = florida()
    noticed(d)
    assert d.go("withdraw_price_change", about="PC1").allowed
    assert not d.go("withdraw_price_change", about="PC1").allowed           # once only
    assert not d.go("withdraw_price_change", about="PC9").allowed           # and only a change that is on file
    [january] = rent_run(d, "2027-01")
    assert january.cents == 10000 and rent_of(d) == 10000 and not d.books().price_law.postings
    billed = florida()
    noticed(billed)
    rent_run(billed, "2027-01")
    refusal = billed.go("withdraw_price_change", about="PC1")
    assert not refusal.allowed and "already billed" in refusal.check and rent_of(billed) == 10500


def test_a_stale_approval_is_cleared_by_approving_again_and_the_next_run_bills_it():
    d = florida()
    noticed(d)
    # the agreement is recorded again after the approval, with the same notice terms: the approval rests on the older record
    assert d.terms(notice_days=30, notice_methods=["first_class_mail"], email_notice_authorized=False, agreement_sha256="f" * 64).allowed
    [january] = rent_run(d, "2027-01")
    assert january.cents == 10000 and rent_of(d) == 10000
    assert d.change("approve_price_change", "approval", at_local(date(2027, 1, 2), 10)).allowed       # the notice already given still counts
    [february] = rent_run(d, "2027-02")
    assert february.cents == 10500 and rent_of(d) == 10500
