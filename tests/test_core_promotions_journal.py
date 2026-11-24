"""The journal lines of a first-month deal, exactly, for both "$1" products; every event balances and the journal rebuilds from the events."""
from collections import Counter

from core.books import fold, lines
from core.store import BASE

from .promotions_world import billing, books, facility, lease_of, rent_with, terms


def journal_of(store, lease):
    """Each journal line on the lease's charges, in the order written: (account, cents)."""
    mine = set(books(store).of_lease[lease])
    return [(a, c) for _, a, c, ch in store.journal(BASE) if ch in mine]


def test_a_dollar_for_the_rest_of_this_month_posts_gross_rent_the_concession_and_the_payments_as_separate_lines():
    store, t = facility()
    lease = lease_of(store, rent_with(store, t["ana"], "B201", "first-month"))
    rent_lines = [(a, c) for a, c in journal_of(store, lease) if a in ("income:rent", "contra:concessions")]
    assert rent_lines == [("income:rent", -8400), ("contra:concessions", 8300)]            # $84.00 of rent earned, $83.00 conceded
    assert sum(c for a, c in journal_of(store, lease) if a == f"receivable:{lease}") == 0   # $1.00 of rent and $25.00 of fee billed, conceded and paid
    assert [c for a, c in journal_of(store, lease) if a == "income:fees"] == [-2500]
    billing(store, lambda led: led.post_rent("2026-12"))
    dec = [(a, c) for a, c in journal_of(store, lease)][-2:]
    assert dec == [(f"receivable:{lease}", 12000), ("income:rent", -12000)]                  # December: the contract rent, nothing conceded


def test_a_dollar_for_the_first_full_month_concedes_the_month_after_the_stub_and_not_the_stub():
    store, t = facility(terms(name="$1 first full month", covers="first_full_month", proration="none"))
    lease = lease_of(store, rent_with(store, t["ana"], "B201", "first-month"))
    assert "contra:concessions" not in {a for a, _ in journal_of(store, lease)}              # the move-in bill carries no concession
    billing(store, lambda led: led.post_rent("2026-12"))
    assert [(a, c) for a, c in journal_of(store, lease) if a in ("income:rent", "contra:concessions")][-2:] == [("income:rent", -12000), ("contra:concessions", 11900)]
    billing(store, lambda led: led.post_rent("2027-01"))
    assert [(a, c) for a, c in journal_of(store, lease) if a == "contra:concessions"] == [("contra:concessions", 11900)]      # January is the contract rent again


def test_every_promotion_event_balances_and_the_journal_rebuilds_from_the_events():
    store, t = facility()
    rent_with(store, t["ana"], "B201", "first-month")
    billing(store, lambda led: led.post_rent("2026-12"))
    events = store.events(BASE)
    assert any(e.kind == "promotion.concession_posted" for e in events)
    for e in events:
        assert sum(c for _, c, _ in lines(e.kind, e.payload)) == 0, e.kind
    assert store.journal(BASE) == [(e.id, a, c, ch) for e in events for a, c, ch in lines(e.kind, e.payload)]
    again = fold(events)
    assert Counter(c.conceded for c in again.charges.values() if c.conceded) == Counter(c.conceded for c in books(store).charges.values() if c.conceded)
