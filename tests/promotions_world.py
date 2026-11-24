"""A small facility for the promotion tests: three 10x10 units and one 5x10, three new tenants and one who rented here before, and a
first-month promotion published as version 1. Every write goes through renting's own commands or the billing functions."""
from __future__ import annotations

from datetime import date

from core import promotions
from core.billing import Ledger
from core.books import at_local
from core.renting import Act, load, run
from core.store import BASE, Store

from .test_core_renting import NOW, act, accept, billing, books, go, ok, pay, rent  # noqa: F401  (the renting tests' own helpers)

BY = "staff:office_manager"
UNITS = (("A101", 10000, "5x10"), ("B201", 12000, "10x10"), ("B202", 12000, "10x10"), ("B203", 12000, "10x10"), ("B204", 12000, "10x10"))


def terms(**over) -> dict:
    """The "$1 for the rest of this month" deal on 10x10 units, for new customers only."""
    base = {"name": "$1 first month", "unit_types": ["10x10"], "customers": "new", "price_cents": 100, "covers": "rest_of_first_month",
            "proration": "by_days", "expires": "2026-12-31", "stacks_with": [], "transfer": "end", "max_redemptions": 5, "max_concession_cents": 100000}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def facility(deal: dict | None = None, promotion: str = "first-month"):
    """Returns (store, tenants): ana, ben and cy have never rented here; dee rented a unit in the spring and left."""
    store = Store()
    led = Ledger()
    led.set_up(at_local(date(2026, 3, 1)), "Northstar Storage", "Orlando, FL", [u for u, _, _ in UNITS] + ["A102"])
    tenants = {n: led.add_tenant(at_local(date(2026, 9, 1), 9), n.title(), f"+1407555{i:04d}") for i, n in enumerate(("ana", "ben", "cy", "dee"))}
    old = led.sign_lease(at_local(date(2026, 4, 1), 10), tenants["dee"], "A102", 9000, [])
    led.receive_payment(tenants["dee"], led.books.receivable(old), at_local(date(2026, 4, 1), 11))
    led.end_lease(at_local(date(2026, 5, 31), 18), old)
    with store.write():
        store.append(BASE, led.pending)
    for unit, cents, size in UNITS:
        ok(store, Act(f"price-{unit}", "price_unit", "staff:manager", unit, args={"rent_cents": cents, "size": size}), at_local(date(2026, 9, 1), 9))
    if deal is not False:
        billing(store, lambda led: promotions.publish_version(led, promotion, deal or terms(), date(2026, 10, 1), BY, at_local(date(2026, 10, 1), 9)))
    return store, tenants


def reserve(store, tenant, unit, *promos, now=NOW, start=None, **args):
    """The renter's reserve command; returns the decision."""
    extra = {"promotions": list(promos)} if promos else {}
    if start:
        extra["start"] = start.isoformat()
    return go(store, act("reserve", "staff:front_desk" if start else "renter", unit, tenant, **extra, **args), now)


def rent_with(store, tenant, unit, *promos, now=NOW, start=None):
    """Reserve, check out, pay what the quote asks and accept the agreement as shown: returns the rental id."""
    d = reserve(store, tenant, unit, *promos, now=now, start=start)
    assert d.allowed, d.check
    rn = ok(store, act("start_checkout", "renter", d.made, tenant), now)
    r = rent(store)
    assert pay(store, tenant, rn, r.rentals[rn].quote["total_cents"], now).allowed
    assert accept(store, tenant, rn, now).allowed
    return rn


def lease_of(store, rental):
    return rent(store).rentals[rental].lease


def charges(store, lease):
    b = books(store)
    return [(c.kind, c.obligation, c.cents, c.conceded, c.open_cents, c.due.isoformat()) for i in b.of_lease[lease] if (c := b.charges[i])]


def kinds(store):
    return [e.kind for e in store.events(BASE)]


def transfer(store, tenant, rental, unit, now):
    """Request, approve, accept and complete a transfer the way the renting tests do; returns the transfer id."""
    from datetime import timedelta
    tr = ok(store, act("request_transfer", "renter", rental, tenant, unit=unit), now)
    ok(store, act("approve_transfer", "staff:manager", tr, until=(now + timedelta(days=1)).isoformat()), now)
    state = rent(store)
    new = state.transfers[tr].target
    short = state.short(state.rentals[new], now, state.rentals[rental])
    if short:
        billing(store, lambda led: led.receive_payment(tenant, short, now))
    assert accept(store, tenant, new, now).allowed
    ok(store, act("complete_transfer", "staff:manager", tr), now)
    return tr
