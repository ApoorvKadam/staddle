"""Redeeming a first-month deal at checkout and move-in: the quote shows the first full-price bill, the lease keeps its contract rent, the
concession is its own line against the covered period, and the return to the contract rent was disclosed and accepted."""
from datetime import date

from core import promotions
from core.books import at_local

from .promotions_world import billing, books, charges, facility, lease_of, rent, rent_with, reserve, terms
from .test_core_renting import NOW, act, ok

NOV1 = at_local(date(2026, 11, 1), 10)


def test_the_quote_shows_the_first_full_price_billing_date_before_acceptance():
    store, t = facility()
    d = reserve(store, t["ana"], "B201", "first-month")
    assert d.allowed, d.check
    q = rent(store).reservations[d.made].quote
    assert q["promotion"]["first_full_price"] == "2026-12-01" and q["promotion"]["rent_cents"] == 12000
    assert (q["first_cents"], q["promotion"]["first_concession_cents"], q["total_cents"]) == (8400, 8300, 100 + 2500)      # the net first month and the admin fee
    rn = ok(store, act("start_checkout", "renter", d.made, t["ana"]), NOW)
    r = rent(store)
    text = r.agreements[r.rentals[rn].agreement].text            # shown, and hashed into the acceptance, before the renter says yes
    assert "$1 first month" in text and "December 1, 2026" in text and "$120.00 a month" in text and "not a rent increase" in text
    assert r.rentals[rn].state != "active" and not r.rentals[rn].accepted


def test_a_dollar_for_the_rest_of_this_month_bills_the_stub_at_one_dollar_and_then_the_contract_rent():
    store, t = facility()
    rn = rent_with(store, t["ana"], "B201", "first-month")
    lease = lease_of(store, rn)
    assert books(store).leases[lease].rent_cents == 12000                                  # the contract rent is never lowered to the deal price
    # gross rent for the 21 days left is $84.00, the deal takes $83.00 off, $1.00 is due; the admin fee is its own charge
    assert charges(store, lease) == [("rent", f"rent:{lease}:2026-11", 8400, 8300, 0, "2026-11-10"), ("admin_fee", f"admin_fee:{lease}", 2500, 0, 0, "2026-11-10")]
    assert books(store).receivable(lease) == 0 and rent(store).rentals[rn].payments
    billing(store, lambda led: led.post_rent("2026-12"))
    assert charges(store, lease)[2] == ("rent", f"rent:{lease}:2026-12", 12000, 0, 12000, "2026-12-01")      # back to the contract rent, in full, with no conceded line
    assert books(store).leases[lease].rent_cents == 12000


def test_a_dollar_for_the_first_full_month_bills_the_stub_in_full_and_the_deal_on_the_first_full_month():
    store, t = facility(terms(name="$1 first full month", covers="first_full_month", proration="none"))
    rn = rent_with(store, t["ana"], "B201", "first-month")
    lease = lease_of(store, rn)
    q = rent(store).rentals[rn].quote
    assert (q["first_cents"], q["promotion"]["first_concession_cents"], q["total_cents"], q["promotion"]["first_full_price"]) == (8400, 0, 8400 + 2500, "2027-01-01")
    billing(store, lambda led: led.post_rent("2026-12"))
    billing(store, lambda led: led.post_rent("2027-01"))
    assert [c[1:] for c in charges(store, lease) if c[0] == "rent"] == [
        (f"rent:{lease}:2026-11", 8400, 0, 0, "2026-11-10"),     # the stub before the first full month is billed as usual (paid at move-in)
        (f"rent:{lease}:2026-12", 12000, 11900, 100, "2026-12-01"),        # the first full month: gross rent, a concession line, $1.00 due
        (f"rent:{lease}:2027-01", 12000, 0, 12000, "2027-01-01")]          # then the contract rent again
    assert books(store).leases[lease].rent_cents == 12000


def test_fifty_percent_off_the_first_month_posts_gross_rent_concession_and_net_as_separate_lines():
    store, t = facility(terms(name="50% off the first month", price_cents=None, percent=50))
    rn = rent_with(store, t["ana"], "B201", "first-month", now=NOV1)
    lease = lease_of(store, rn)
    assert charges(store, lease)[0] == ("rent", f"rent:{lease}:2026-11", 12000, 6000, 0, "2026-11-01")
    assert rent(store).rentals[rn].quote["total_cents"] == 6000 + 2500
    totals: dict[str, int] = {}
    mine = set(books(store).of_lease[lease])
    for _, account, cents, charge in store.journal():
        if charge in mine:
            totals[account] = totals.get(account, 0) + cents
    assert totals["income:rent"] == -12000 and totals["contra:concessions"] == 6000 and totals[f"receivable:{lease}"] == 0


def test_the_redemption_is_its_own_record_and_the_return_to_contract_rent_is_a_disclosed_ending():
    store, t = facility()
    rn = rent_with(store, t["ana"], "B201", "first-month")
    lease = lease_of(store, rn)
    (red,) = promotions.state(books(store)).redemptions.values()
    assert (red.promotion, red.version, red.lease, red.tenant, red.unit, red.periods, red.planned_cents) == ("first-month", 1, lease, t["ana"], "B201", ["2026-11"], 8300)
    assert [(c["period"], c["cents"]) for c in red.concessions] == [("2026-11", 8300)] and red.state == "active"
    assert promotions.ending_disclosed(books(store), lease, date(2026, 12, 1)).id == red.id        # the 1st bill at the contract rent was accepted up front
    assert promotions.ending_disclosed(books(store), lease, date(2027, 1, 1)) is None              # any other rent change is not covered by the deal


def test_a_later_move_in_day_signs_at_acceptance_and_the_deal_covers_the_rest_of_that_month():
    store, t = facility()
    rn = rent_with(store, t["ben"], "B202", "first-month", start=date(2026, 11, 20))
    lease = lease_of(store, rn)
    q = rent(store).rentals[rn].quote
    assert q["first_cents"] == 4400 and q["promotion"]["periods"][0]["concession_cents"] == 4300            # 11 days of $120.00
    assert charges(store, lease)[0][2:5] == (4400, 4300, 0)


def test_a_rental_without_a_promotion_is_billed_exactly_as_before():
    store, t = facility()
    rn = rent_with(store, t["ana"], "B201")
    lease = lease_of(store, rn)
    assert charges(store, lease)[0] == ("rent", f"rent:{lease}:2026-11", 8400, 0, 0, "2026-11-10")
    assert "promotion" not in rent(store).rentals[rn].quote and promotions.state(books(store)).redemptions == {}
    text = rent(store).agreements[rent(store).rentals[rn].agreement].text
    assert "Deal" not in text
