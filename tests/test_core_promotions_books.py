"""The books learn promotional concessions as their own account: gross contract rent, the concession and the net due stay three facts."""
from datetime import date

from core.billing import Ledger
from core.books import at_local, fold, lines
from core.store import Store

NOV10 = at_local(date(2026, 11, 10), 10)


def lease_with_a_conceded_rent():
    """Rent of $120 billed for a month, $60 of it conceded as a first-month deal, the net $60 paid."""
    led = Ledger()
    led.set_up(at_local(date(2026, 10, 1)), "Northstar Storage", "Orlando, FL", ["A101"])
    t = led.add_tenant(at_local(date(2026, 11, 9), 9), "Dana Reyes", "+14075550101")
    lease = led.sign_lease(NOV10, t, "A101", 12000, [], start=date(2026, 11, 1))
    charge = led.books.of_lease[lease][0]
    return led, t, lease, charge


def concede(led, t, lease, charge, cents=6000):
    led.emit("promotion.concession_posted", NOV10, {"redemption": "PR0001", "promotion": "first-month", "charge": charge, "lease": lease, "tenant": t,
                                                    "period": "2026-11", "cents": cents, "gross_cents": 12000}, f"promotion:concession:{charge}", source="code")


def test_a_concession_is_its_own_line_and_the_gross_rent_is_untouched():
    led, t, lease, charge = lease_with_a_conceded_rent()
    concede(led, t, lease, charge)
    ch = led.books.charges[charge]
    assert (ch.cents, ch.conceded, ch.open_cents) == (12000, 6000, 6000)
    assert lines("promotion.concession_posted", led.pending[-1].payload) == [("contra:concessions", 6000, charge), (f"receivable:{lease}", -6000, charge)]
    assert led.books.receivable(lease) == 6000 and led.books.leases[lease].rent_cents == 12000


def test_the_net_payment_clears_the_receivable_with_no_hidden_write_off():
    led, t, lease, charge = lease_with_a_conceded_rent()
    concede(led, t, lease, charge)
    led.receive_payment(t, 6000, NOV10)
    assert led.books.receivable(lease) == 0 and led.books.charges[charge].open_cents == 0 and led.books.credit.get(t, 0) == 0
    store = Store()
    with store.write():
        store.append("base", led.pending)
    totals: dict[str, int] = {}
    for _, account, cents, _ in store.journal():
        totals[account] = totals.get(account, 0) + cents
    assert totals["income:rent"] == -12000 and totals["contra:concessions"] == 6000 and totals["clearing:card"] == 6000
    assert totals[f"receivable:{lease}"] == 0 and "contra:waivers" not in totals       # rent income and the concession show apart; nothing was waived


def test_a_returned_payment_reopens_the_net_amount_only():
    led, t, lease, charge = lease_with_a_conceded_rent()
    concede(led, t, lease, charge)
    pay = led.receive_payment(t, 6000, at_local(date(2026, 11, 2), 9))
    led.return_payment(at_local(date(2026, 11, 3), 9), pay)         # before the 6th, so no late fee comes with it
    assert led.books.charges[charge].open_cents == 6000 and led.books.receivable(lease) == 6000 and led.books.credit.get(t, 0) == 0


def test_a_conceded_charge_that_is_reversed_gives_the_concession_back_and_the_books_balance():
    led, t, lease, charge = lease_with_a_conceded_rent()
    concede(led, t, lease, charge)
    led.receive_payment(t, 6000, NOV10)
    led.reverse(NOV10, charge, "move-out", "staff:front_desk")
    b = led.books
    assert b.receivable(lease) == 0 and b.charges[charge].open_cents == 0 and b.credit[t] == 6000      # only what the tenant really paid comes back
    for e in led.pending:
        assert sum(c for _, c, _ in lines(e.kind, e.payload)) == 0
    store = Store()
    with store.write():
        store.append("base", led.pending)
    journal = store.journal()
    assert [c for _, a, c, _ in journal if a == "contra:concessions"] == [6000, -6000]        # given, then given back with the charge
    assert sum(c for _, a, c, _ in journal if a in ("income:rent", "contra:concessions")) == 0
    assert sum(c for _, a, c, _ in journal if a == f"receivable:{lease}") == 0


def test_the_journal_rebuilds_from_events_and_the_conceded_amount_folds_back():
    led, t, lease, charge = lease_with_a_conceded_rent()
    concede(led, t, lease, charge)
    led.receive_payment(t, 6000, NOV10)
    store = Store()
    with store.write():
        store.append("base", led.pending)
    rebuilt = [(e.id, a, c, ch) for e in store.events() for a, c, ch in lines(e.kind, e.payload)]
    assert store.journal() == rebuilt
    again = fold(store.events())
    assert again.charges[charge].conceded == 6000 and again.charges[charge].open_cents == 0


def test_a_month_whose_rent_was_conceded_counts_as_on_time_when_the_net_is_paid_in_time():
    led = Ledger()
    led.set_up(at_local(date(2026, 9, 1)), "Northstar Storage", "Orlando, FL", ["A101"])
    t = led.add_tenant(at_local(date(2026, 9, 9), 9), "Dana Reyes", "+14075550101")
    lease = led.sign_lease(at_local(date(2026, 9, 10), 10), t, "A101", 12000, [])
    led.receive_payment(t, led.books.receivable(lease), at_local(date(2026, 9, 10), 11))          # the move-in month, paid
    (charge,) = led.post_rent("2026-10")
    led.emit("promotion.concession_posted", at_local(date(2026, 10, 1)), {"redemption": "PR0001", "promotion": "first-month", "charge": charge, "lease": lease,
                                                                         "tenant": t, "period": "2026-10", "cents": 6000, "gross_cents": 12000}, "c", source="code")
    led.receive_payment(t, 6000, at_local(date(2026, 10, 2), 9))
    assert led.books.on_time(t, "2026-10")
