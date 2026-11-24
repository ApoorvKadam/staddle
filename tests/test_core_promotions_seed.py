"""The seeded promotion on the live copy's year: one live first-month deal on 10x10 units, used by one move-in made through the real renting
commands. Everything else in the year is exactly what it was without it."""
from collections import Counter
from datetime import date

import pytest

from core import promotions
from core.books import at_local, lines, local
from core.seed import TODAY, build



def year(today=TODAY, **kw):
    return build(today=today, cutoff=at_local(today), standing=True, **kw)


def money(y):
    b = y.led.books
    return (Counter((a, c, ch) for e in y.led.pending for a, c, ch in lines(e.kind, e.payload)),
            {c.id: (c.lease, c.kind, c.cents, c.due, c.paid, c.closed, c.conceded) for c in b.charges.values()},
            {p.id: (p.tenant, p.cents, p.returned) for p in b.payments.values()}, dict(b.credit), dict(b.refunds))


def test_the_year_is_the_same_but_for_the_one_promoted_move_in():
    with_it, without = year(), year(promoted=False)
    (red,) = promotions.state(with_it.led.books).redemptions.values()
    assert promotions.state(without.led.books).redemptions == {} and without.led.books.promotions is None
    (lines_a, charges_a, pays_a, credit_a, refunds_a), (lines_b, charges_b, pays_b, credit_b, refunds_b) = money(with_it), money(without)
    assert not lines_b - lines_a                                                                  # nothing the year had before is gone or changed
    assert all(charges_a[k] == v for k, v in charges_b.items()) and all(pays_a[k] == v for k, v in pays_b.items())
    new_charges = {k for k in charges_a if k not in charges_b}
    assert new_charges and all(charges_a[k][0] == red.lease for k in new_charges)               # every new charge belongs to the promoted lease
    extra = lines_a - lines_b
    assert {a for a, _, ch in extra if ch is None} == {"clearing:card", f"credit:{red.tenant}"} and all(ch in new_charges for _, _, ch in extra if ch)      # her payment and the credit it passed through
    assert sum(c for (a, c, _), n in extra.items() for _ in range(n) if a == f"credit:{red.tenant}") == 0
    assert {k: v for k, v in credit_a.items() if k != red.tenant} == credit_b and refunds_a == refunds_b


def test_the_promotion_is_live_on_10x10_units_and_was_used_through_the_commands():
    y = year()
    b = y.led.books
    (v,) = promotions.state(b).versions["first-month"]
    assert v.terms["unit_types"] == ["10x10"] and v.effective <= TODAY <= date.fromisoformat(v.terms["expires"]) and v.terms["covers"] == "rest_of_first_month"
    (red,) = promotions.state(b).redemptions.values()
    lease = b.leases[red.lease]
    priced = next(e.payload for e in y.led.pending if e.kind == "unit.priced" and e.payload["unit"] == red.unit)
    assert priced["size"] == "10x10" and lease.rent_cents == priced["rent_cents"]               # the lease keeps the unit's contract rent
    first = b.charges[b.of_lease[lease.id][0]]
    assert first.conceded > 0 and first.conceded + first.net_cents == first.cents and red.concessions[0]["cents"] == first.conceded and b.receivable(lease.id) == 0
    for step in ("reserve", "checkout", "pay", "accept"):                                         # the real commands, each decided and recorded by the gate
        assert b.decisions[f"seed:promotion:{step}"]["allowed"] is True


@pytest.mark.parametrize("today", [date(2026, 12, 1), date(2026, 12, 3), date(2027, 1, 20), date(2026, 11, 2)])
def test_every_day_the_copy_can_open_on_has_the_deal_live_and_one_clean_redemption(today):
    y = year(today)
    b = y.led.books
    (v,) = promotions.state(b).versions["first-month"]
    assert v.effective <= today <= date.fromisoformat(v.terms["expires"])
    (red,) = promotions.state(b).redemptions.values()
    lease = b.leases[red.lease]
    assert local(lease.signed_at).date() < today and all(b.charges[i].due <= today for i in b.of_lease[lease.id])
    assert b.receivable(lease.id) == (lease.rent_cents if today.day == 1 else 0)        # on the 1st her new month's rent has just come due, unpaid
    assert max(e.at for e in y.led.pending) <= at_local(today)
