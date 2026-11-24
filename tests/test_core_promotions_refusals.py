"""Refusals with plain words and a machine name, all before anything is written; stacking, repeat use, the budget, and what a transfer does
to a deal."""
from datetime import date, timedelta

import pytest

from core import promotions
from core.books import at_local

from .promotions_world import BY, billing, books, charges, facility, kinds, lease_of, rent, rent_with, reserve, terms, transfer
from .test_core_renting import NOW, accept, act, go, ok


def refused(store, tenant, unit, *promos, code, words, now=NOW):
    """A reserve the gate refuses: the decision names the reason, says it in words, and the only record written is the decision itself."""
    before = kinds(store)
    d = reserve(store, tenant, unit, *promos, now=now)
    assert not d.allowed and d.why == code and words in d.check, (d.why, d.check)
    assert kinds(store)[len(before):] == ["gate.decided"]
    assert not any(k.startswith(("promotion.", "reservation.", "rental.", "lease.", "charge.")) for k in kinds(store)[len(before):])
    return d


def test_terms_that_are_incomplete_in_force_are_refused():
    store, t = facility()
    broken = {k: v for k, v in terms().items() if k != "expires"}
    billing(store, lambda led: led.emit("promotion.version_published", at_local(date(2026, 10, 1), 9), {"promotion": "broken", "version": 1, "terms": broken,
                                                                                                   "effective": "2026-10-01", "by": BY}, "v:broken", source=BY))
    refused(store, t["ana"], "B201", "broken", code="PROMOTION_TERMS_INCOMPLETE", words="expiry date is missing")


@pytest.mark.parametrize("who, unit, promo, words", [("ana", "A101", "first-month", "for 10x10 units; unit A101 is 5x10"),
                                                      ("dee", "B201", "first-month", "for new customers"),
                                                      ("ana", "B201", "no-such-deal", "there is no such promotion")])
def test_a_rental_that_does_not_fit_the_offer_is_not_eligible(who, unit, promo, words):
    store, t = facility()
    refused(store, t[who], unit, promo, code="PROMOTION_NOT_ELIGIBLE", words=words)


def test_an_offer_past_its_expiry_or_not_yet_in_force_is_not_eligible():
    store, t = facility()
    billing(store, lambda led: promotions.publish_version(led, "later", terms(), date(2026, 12, 1), BY, at_local(date(2026, 10, 1), 9)))
    refused(store, t["ana"], "B201", "later", code="PROMOTION_NOT_ELIGIBLE", words="not in force yet")
    refused(store, t["ana"], "B201", "first-month", code="PROMOTION_NOT_ELIGIBLE", words="ended on December 31, 2026", now=at_local(date(2027, 1, 5), 10))


def test_the_redemption_limit_counts_deals_in_checkout_as_well_as_signed_ones():
    store, t = facility(terms(max_redemptions=1))
    held = reserve(store, t["ana"], "B201", "first-month")
    assert held.allowed
    refused(store, t["ben"], "B202", "first-month", code="PROMOTION_BUDGET_EXCEEDED", words="limit of 1 redemptions")      # ana's hold uses the one left
    ok(store, act("cancel_reservation", "renter", held.made, t["ana"]), NOW)
    assert reserve(store, t["ben"], "B202", "first-month").allowed                                                           # her hold is gone, so it is free again


def test_the_budget_in_cents_stops_a_deal_that_would_pass_it():
    store, t = facility(terms(max_concession_cents=8000))
    refused(store, t["ana"], "B201", "first-month", code="PROMOTION_BUDGET_EXCEEDED", words="$80.00 of its $80.00 budget left; this rental would take $83.00")
    store2, t2 = facility(terms(max_concession_cents=10000))
    rent_with(store2, t2["ana"], "B201", "first-month")                        # $83.00 given, $17.00 left
    refused(store2, t2["ben"], "B202", "first-month", code="PROMOTION_BUDGET_EXCEEDED", words="$17.00 of its $100.00 budget left")


def test_a_deal_already_used_by_the_customer_is_refused_on_a_second_rental():
    store, t = facility(terms(customers="any"))
    rent_with(store, t["dee"], "B201", "first-month")
    refused(store, t["dee"], "B202", "first-month", code="PROMOTION_ALREADY_REDEEMED", words="already used by this customer on unit B201")


def test_a_deal_on_a_rental_in_checkout_cannot_be_started_again_by_the_same_customer():
    store, t = facility()
    assert reserve(store, t["ana"], "B201", "first-month").allowed
    refused(store, t["ana"], "B202", "first-month", code="PROMOTION_ALREADY_REDEEMED", words="already on a rental this customer has in checkout")


def test_the_deal_used_elsewhere_before_acceptance_is_refused_at_acceptance_and_nothing_is_written():
    store, t = facility()
    d = reserve(store, t["ana"], "B201", "first-month")
    rn = ok(store, act("start_checkout", "renter", d.made, t["ana"]), NOW)
    old = next(iter(books(store).leases))
    billing(store, lambda led: led.emit("promotion.redeemed", NOW, {"redemption": "PR9000", "promotion": "first-month", "version": 1, "lease": old, "tenant": t["ana"],
                                                                    "unit": "A102", "start": "2026-04-01", "periods": ["2026-04"], "planned_cents": 100,
                                                                    "first_full_price": "2026-05-01", "carried_from": None}, "promotion:redeem:x", source="code"))
    before = kinds(store)
    r = accept(store, t["ana"], rn)
    assert not r.allowed and r.why == "PROMOTION_ALREADY_REDEEMED" and kinds(store)[len(before):] == ["gate.decided"] and not rent(store).rentals[rn].accepted


def stacked(first_names, second_names):
    store, t = facility(terms(stacks_with=first_names))
    billing(store, lambda led: promotions.publish_version(led, "second", terms(name="50% off what is left", price_cents=None, percent=50, stacks_with=second_names),
                                                          date(2026, 10, 1), BY, at_local(date(2026, 10, 1), 9)))
    return store, t


def test_two_deals_that_never_named_each_other_do_not_stack():
    store, t = stacked([], [])
    refused(store, t["ana"], "B201", "first-month", "second", code="PROMOTION_STACK_CONFLICT", words="cannot be combined")


def test_a_deal_that_only_one_side_allows_does_not_stack():
    store, t = stacked(["second"], [])
    refused(store, t["ana"], "B201", "first-month", "second", code="PROMOTION_STACK_CONFLICT", words="both offers to name each other")


def test_deals_that_name_each_other_stack_in_order_and_never_pass_the_gross_rent():
    store, t = stacked(["second"], ["first-month"])
    rn = rent_with(store, t["ana"], "B201", "first-month", "second")
    lease = lease_of(store, rn)
    # $84.00 gross: the first deal takes $83.00 and leaves $1.00; the second takes half of that, $0.50 (half up)
    assert charges(store, lease)[0][2:4] == (8400, 8300 + 50)
    assert rent(store).rentals[rn].quote["total_cents"] == 50 + 2500
    assert sorted(r.promotion for r in promotions.state(books(store)).redemptions.values()) == ["first-month", "second"]


# transfers ------------------------------------------------------------------------------------------------------------------------
FIRST_FULL = dict(name="$1 first full month", covers="first_full_month", proration="none")
NOV12 = at_local(date(2026, 11, 12), 10)


def test_a_transfer_carries_a_deal_whose_terms_allow_it_and_the_new_lease_gives_the_months_left():
    store, t = facility(terms(transfer="carry", **FIRST_FULL))
    rn = rent_with(store, t["ana"], "B201", "first-month")
    old = lease_of(store, rn)
    (red,) = promotions.state(books(store)).redemptions.values()
    tr = transfer(store, t["ana"], rn, "B202", NOV12)
    new = lease_of(store, rent(store).transfers[tr].target)
    quote = rent(store).rentals[rent(store).transfers[tr].target].quote
    assert quote["promotion"]["first_full_price"] == "2027-01-01" and quote["promotion"]["carried_from"] == [red.id]
    st = promotions.state(books(store))
    carried = next(r for r in st.redemptions.values() if r.lease == new)
    assert carried.carried_from == red.id and carried.periods == ["2026-12"] and st.redemptions[red.id].state == "ended" and st.redemptions[red.id].ended["to"] == new
    billing(store, lambda led: led.post_rent("2026-12"))
    assert [c for c in charges(store, new) if c[1] == f"rent:{new}:2026-12"] == [("rent", f"rent:{new}:2026-12", 12000, 11900, 100, "2026-12-01")]
    assert not [c for c in charges(store, old) if c[1].endswith("2026-12")]             # the old lease ended before the 1st


def test_a_transfer_without_leave_ends_the_deal_and_the_quote_says_so():
    store, t = facility(terms(transfer="end", **FIRST_FULL))
    rn = rent_with(store, t["ana"], "B201", "first-month")
    tr = transfer(store, t["ana"], rn, "B202", NOV12)
    target = rent(store).transfers[tr].target
    new, quote = lease_of(store, target), rent(store).rentals[target].quote
    assert "promotion" not in quote and quote["promotion_ended"][0]["reason"] == "its terms do not follow a transfer"
    assert "ends with that unit and does not carry to this one" in rent(store).agreements[rent(store).rentals[target].agreement].text
    st = promotions.state(books(store))
    (red,) = [r for r in st.redemptions.values()]
    assert red.state == "ended" and red.ended["to"] is None and red.ended["reason"] == "transfer"
    billing(store, lambda led: led.post_rent("2026-12"))
    assert [c[2:5] for c in charges(store, new) if c[1] == f"rent:{new}:2026-12"] == [(12000, 0, 12000)]       # the contract rent, no deal


def test_a_deal_that_allows_a_transfer_still_ends_when_the_new_unit_is_another_size():
    store, t = facility(terms(transfer="carry", **FIRST_FULL))
    rn = rent_with(store, t["ana"], "B201", "first-month")
    tr = transfer(store, t["ana"], rn, "A101", NOV12)
    quote = rent(store).rentals[rent(store).transfers[tr].target].quote
    assert "promotion" not in quote and "the new unit is 5x10" in quote["promotion_ended"][0]["reason"]


def test_a_deal_already_given_in_full_has_nothing_left_to_carry_and_just_ends():
    store, t = facility(terms(transfer="carry"))                  # "$1 for the rest of this month": given at move-in
    rn = rent_with(store, t["ana"], "B201", "first-month")
    tr = transfer(store, t["ana"], rn, "B202", NOV12)
    target = rent(store).transfers[tr].target
    quote = rent(store).rentals[target].quote
    assert "promotion" not in quote and "promotion_ended" not in quote
    (red,) = promotions.state(books(store)).redemptions.values()
    assert red.state == "ended"
    new = lease_of(store, target)
    assert charges(store, new)[0][3] == 0                         # the new unit's own stub gets no second helping
