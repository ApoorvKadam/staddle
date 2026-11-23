"""Money shown must equal money charged: the counter sale and the move-in checkout show what they will charge, and the core refuses
(in plain words, with the new amount) when it changed since the page was shown. Each test fails on the code before the fix."""
import json
import re
from datetime import date, timedelta

import pytest

from core import delinquency, renting, shop
from core.billing import Refused
from core.books import at_local
from core.delinquency import SimulatedProcessor
from core.store import BASE

from .test_core_operations_set_shop import World
from .test_core_renting import NOW as RENT_NOW
from .test_core_renting import act, billing, books, facility, ok
from .test_core_renting import accept as accept_rental
from .test_core_renting import rent as rent_of
from .test_core_renting_pay import ready
from .test_fms import seen, sign_in, site
from .test_fms_renting_pages import desk_of, new_tenant, start_rental, token, vacant
from .test_fms_shop_pages import part
from .test_shop_review_fixes import STAFF, stocked_rental


def test_a_counter_sale_is_refused_with_the_new_price_when_the_price_changed():
    w = World()
    w.stock(5)
    assert w.do('quote_sale', 'lock', quantity=1, price=1500).allowed             # the price the page showed is the price now
    w.do('publish_product', 'lock', price=2000, cost=500, tax_class='standard', active=True)      # another tab changed it
    before = len(w.state().quotes)
    refused = w.do('quote_sale', 'lock', quantity=1, price=1500)
    assert not refused.allowed and refused.why == 'price_changed'
    assert '$20.00' in refused.check and '$15.00' in refused.check and 'Check the new price with the buyer' in refused.check
    assert len(w.state().quotes) == before                                       # nothing was quoted, so nothing can be charged
    assert w.do('quote_sale', 'lock', quantity=1, price=2000).allowed
    assert not w.do('quote_sale', 'lock', quantity=1, price=None).allowed        # a form that did not send the price it showed is not taken


def test_the_counter_page_sends_the_price_it_shows_and_a_stale_page_charges_nothing(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        page = c.get("/shop").text
        counter = part(page, "counter")
        shown = json.loads(re.search(r'name="shown" value="([^"]+)"', counter).group(1).replace("&#34;", '"'))
        assert shown == {"disc lock": 1500} and "$15.00" in seen(counter)
        assert fd.staff_shop(ident, shop.Act("price-up", "publish_product", STAFF, "disc lock", {"price": 2000, "cost": 500, "tax_class": "standard", "active": True}))
        paid_before = len(fd.processor.payments)
        refused = c.post("/shop", data={"token": token(page), "action": "sell", "product": "disc lock", "quantity": "1", "card": "card_ok",
                                        "shown": json.dumps(shown)}, follow_redirects=False)
        after = seen(c.get(refused.headers["location"]).text)
        assert "The price of disc lock is now $20.00, not $15.00 as the page showed" in after
        assert len(fd.processor.payments) == paid_before and not shop.state(fd.ready().books(fd.stream(ident))).quotes     # no quote, no charge
        fresh = c.get("/shop").text
        assert "$20.00" in seen(part(fresh, "counter"))
        sold = c.post("/shop", data={"token": token(fresh), "action": "sell", "product": "disc lock", "quantity": "1", "card": "card_ok",
                                     "shown": re.search(r'name="shown" value="([^"]+)"', fresh).group(1).replace("&#34;", '"')}, follow_redirects=False)
        assert sold.status_code == 303 and list(fd.processor.payments.values())[-1]["cents"] == 2000


def test_the_checkout_total_names_the_add_ons_and_a_changed_total_is_asked_again():
    store, dana, _ = stocked_rental()
    processor = SimulatedProcessor()
    rn = ready(store, dana)
    due = renting.checkout_due(books(store), rent_of(store), rent_of(store).rentals[rn], RENT_NOW)
    assert due == (rent_of(store).rentals[rn].quote['total_cents'], [], rent_of(store).rentals[rn].quote['total_cents'])
    rental_only = due[0]
    quote, = renting.add_shop_items(store, BASE, rn, [('lock', 1)], STAFF, RENT_NOW)
    cents, quotes, total = renting.checkout_due(books(store), rent_of(store), rent_of(store).rentals[rn], RENT_NOW)
    assert (cents, quotes, total) == (rental_only, [quote], rental_only + 1500)
    with pytest.raises(renting.TotalChanged) as shown:        # the renter saw the rental alone
        renting.pay_rental(store, BASE, processor, rn, 'card_ok', RENT_NOW, expected_cents=rental_only)
    assert shown.value.total == total and f'${total / 100:,.2f}' in str(shown.value) and not processor.payments
    assert not delinquency.state(books(store)).intents and rent_of(store).rentals[rn].pay_ops == []
    intent = renting.pay_rental(store, BASE, processor, rn, 'card_ok', RENT_NOW, expected_cents=total)
    (charged,) = processor.payments.values()
    assert intent.state == 'succeeded' and charged['cents'] == total


def test_add_ons_are_paid_when_credit_already_covers_the_rental():
    store, dana, step = stocked_rental()
    now = RENT_NOW
    reservation = ok(store, act('reserve', 'staff:manager', 'A101', dana, start=(now + timedelta(days=5)).date().isoformat()), now)
    rn = ok(store, act('start_checkout', 'staff:manager', reservation, dana), now)
    total = rent_of(store).rentals[rn].quote['total_cents']
    billing(store, lambda led: led.receive_payment(dana, total, now))
    assert accept_rental(store, dana, rn, now).allowed
    assert rent_of(store).short(rent_of(store).rentals[rn], now) == 0 and rent_of(store).rentals[rn].pay_ops == []         # funded, no payment operation
    processor = SimulatedProcessor()
    with pytest.raises(Refused, match='already paid for'):                  # neither the rental nor any add-on asks for money
        renting.pay_rental(store, BASE, processor, rn, 'card_ok', now)
    assert not processor.payments
    quote, = renting.add_shop_items(store, BASE, rn, [('lock', 1)], STAFF, now)
    payments_before = len(books(store).payments)
    intent = renting.pay_rental(store, BASE, processor, rn, 'card_ok', now, expected_cents=1500)
    (charged,) = processor.payments.values()
    st = shop.state(books(store))
    assert intent.state == 'succeeded' and charged['cents'] == 1500 and quote in st.sales          # the lock was paid and sold, nothing else
    assert rent_of(store).rentals[rn].pay_ops == [] and len(books(store).payments) == payments_before and intent.payment is None        # no rental payment
    assert not books(store).declined


def test_a_declined_add_on_only_payment_records_no_declined_rental_payment():
    store, dana, step = stocked_rental()
    reservation = ok(store, act('reserve', 'staff:manager', 'A101', dana, start=(RENT_NOW + timedelta(days=5)).date().isoformat()), RENT_NOW)
    rn = ok(store, act('start_checkout', 'staff:manager', reservation, dana), RENT_NOW)
    billing(store, lambda led: led.receive_payment(dana, rent_of(store).rentals[rn].quote['total_cents'], RENT_NOW))
    assert accept_rental(store, dana, rn, RENT_NOW).allowed
    renting.add_shop_items(store, BASE, rn, [('lock', 1)], STAFF, RENT_NOW)
    intent = renting.pay_rental(store, BASE, SimulatedProcessor(), rn, 'card_declined', RENT_NOW)
    assert intent.state == 'failed' and not books(store).declined and rent_of(store).rentals[rn].pay_ops == []


def test_the_checkout_page_shows_add_ons_and_the_combined_total_and_a_moved_total_is_asked_again(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        unit, = vacant(fd, ident)
        tenant = new_tenant(fd, ident)
        _, rn = start_rental(fd, ident, tenant, unit, accept=False)
        money = lambda cents: f"${cents // 100:,}" + (f".{cents % 100:02d}" if cents % 100 else "")
        old = c.get(f"/rent/checkout/{rn}").text
        rental_only = int(re.search(r'name="total" value="(\d+)"', old).group(1))
        assert "disc lock" not in seen(old)
        renting.add_shop_items(fd.ready(), fd.stream(ident), rn, [("disc lock", 1)], STAFF, fd.clock())       # staff add a lock while the old page is open
        stale = c.post(f"/rent/checkout/{rn}", data={"token": token(old), "card": "card_ok", "accept": "yes", "total": str(rental_only),
                                                      "sha256": re.search(r'name="sha256" value="([^"]+)"', old).group(1)}, follow_redirects=False)
        assert stale.status_code == 303 and stale.headers["location"] == f"/rent/checkout/{rn}?changed=1"
        assert not fd.processor.payments and not rent_of_desk(fd, ident).rentals[rn].accepted         # nothing accepted, nothing charged
        page = c.get(stale.headers["location"]).text
        text = seen(page)
        assert "The total changed while you were looking at this page" in text and "1 disc lock, added at the desk" in text
        total = rental_only + 1500
        assert money(total) in text and int(re.search(r'name="total" value="(\d+)"', page).group(1)) == total
        paid = c.post(f"/rent/checkout/{rn}", data={"token": token(page), "card": "card_ok", "accept": "yes", "total": str(total),
                                                     "sha256": re.search(r'name="sha256" value="([^"]+)"', page).group(1)}, follow_redirects=False)
        assert paid.headers["location"] == f"/rent/result/{rn}"
        (charge,) = fd.processor.payments.values()
        assert charge["cents"] == total and rent_of_desk(fd, ident).rentals[rn].state == "active"


def rent_of_desk(fd, ident):
    return renting.load(fd.ready(), fd.stream(ident))[1]
