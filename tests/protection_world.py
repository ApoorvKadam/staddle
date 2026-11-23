"""A small facility for the protection tests: eight units, eight tenants with an active lease each, the year's money paid, and helpers
that write through the same ledger functions and staff commands the product uses. Every write goes to one visitor stream."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from core import protection
from core.billing import Ledger
from core.books import at_local
from core.store import Store

STREAM = "visitor:a"
NOV = date(2026, 11, 1)
UNITS = [f"A{n}" for n in range(101, 113)]
SPARE = UNITS[8:]                   # vacant units a transfer can move to
FRONT, MANAGER = "staff:front_desk", "staff:office_manager"
TERMS_V1 = "Simulated protection plan, version 1: a monthly fee for the plan's own listed benefits. No insurance policy is issued."
TERMS_V2 = "Simulated protection plan, version 2: a monthly fee for the plan's own listed benefits. No insurance policy is issued."


def day(m: int, d: int, hour: int = 9, minute: int = 0, year: int = 2026) -> datetime:
    return at_local(date(year, m, d), hour, minute)


class World:
    """Tenant `k` (0 to 7) rents UNITS[k] on a lease signed Oct 20 and paid for."""

    def __init__(self, path: str = ":memory:"):
        led = Ledger()
        led.set_up(day(10, 1), "Northstar Storage", "Orlando, FL", UNITS)
        self.tenants, self.leases = [], []
        for k, unit in enumerate(UNITS[:8]):
            t = led.add_tenant(day(10, 19), f"Tenant {k}", f"+1407555{k:04d}")
            lease = led.sign_lease(day(10, 20, 10), t, unit, 12000, [])
            led.receive_payment(t, sum(c.open_cents for c in led.books.open_charges(t)), day(10, 20, 10, 5))     # what the move-in charged, so no credit is left
            self.tenants.append(t)
            self.leases.append(lease)
        self.store = Store(path)
        with self.store.write():
            self.store.append("base", led.pending)
        self.n = 0
        self.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1200, TERMS_V1, date(2026, 10, 1), FRONT, day(10, 1)))

    # writing ----------------------------------------------------------------------------------------------------------
    def act(self, fn):
        """Run `fn(ledger)` in one write transaction on the visitor stream and return what it returns."""
        with self.store.write():
            books = self.store.books(STREAM)
            led = Ledger(books)
            out = fn(led)
            self.store.append(STREAM, led.pending)
            return out

    def books(self):
        return self.store.books(STREAM)

    def state(self):
        return protection.state(self.books())

    def cmd(self, action: str, now: datetime, source: str = FRONT, **kw):
        self.n += 1
        return protection.run(self.store, STREAM, protection.Act(kw.pop("id", f"c{self.n}"), action, source, **kw), now)

    # the plan's own records -----------------------------------------------------------------------------------------
    def offer(self, k: int, at: datetime, **kw) -> str:
        return self.act(lambda led: protection.offer(led, self.leases[k], FRONT, at, **kw))

    def accept(self, enrolment: str, at: datetime, starts: date | None = None, **over) -> str:
        e = self.state().enrolments[enrolment]
        args = {"by": e.tenant, "how": "signed agreement", "words": "I agree to the protection plan at this price.", "version": e.version,
                "cents": e.cents, "starts": starts or at.date(), **over}
        return self.act(lambda led: protection.accept(led, enrolment, at=at, **args))

    def enrol(self, enrolment: str, at: datetime) -> None:
        self.act(lambda led: protection.activate(led, enrolment, MANAGER, at))

    def accepted_and_active(self, k: int, at: datetime, starts: date | None = None) -> str:
        """The usual road: an offer, the tenant's explicit acceptance, staff activate it."""
        e = self.offer(k, at - timedelta(hours=1))
        self.accept(e, at, starts)
        self.enrol(e, at + timedelta(minutes=5))
        return e

    def charge(self, k: int, period: str, at: datetime, **kw):
        return self.cmd("post_plan_charge", at, FRONT, lease=self.leases[k], period=period, **kw)
