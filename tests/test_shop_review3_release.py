"""Review fix on add-ons of a rental that closes: merchandise nobody paid for goes back on the shelf with the rental, one whose payment can
still settle keeps its reservation until the processor answers. Each test fails on the code before the fix."""
from datetime import timedelta

import pytest

from core import delinquency, renting, shop
from core.delinquency import SimulatedProcessor
from core.store import BASE

from .test_core_renting import NOW as RENT_NOW
from .test_core_renting import act, books, ok
from .test_core_renting import rent as rent_of
from .test_core_renting_pay import ready
from .test_shop_review_fixes import STAFF, Quiet, stocked_rental
from .test_shop_review3_unsent import funded_rental_with_a_lock, stopped_after_writing_the_intent


def cancel(store, rn):
    ok(store, act('cancel_reservation', 'staff:manager', rent_of(store).rentals[rn].reservation), RENT_NOW)


def test_an_add_on_nobody_paid_for_is_released_when_its_rental_is_cancelled():
    store, dana, step = stocked_rental(1)
    rn = ready(store, dana)
    quote, = renting.add_shop_items(store, BASE, rn, [('lock', 1)], STAFF, RENT_NOW)
    assert shop.state(books(store)).available('lock') == 0                    # held for the checkout
    cancel(store, rn)
    st = shop.state(books(store))
    assert rent_of(store).rentals[rn].state == 'cancelled'
    assert quote in st.cancelled and st.available('lock') == 1                  # the last lock is for sale again
    assert step('walk-in', 'quote_sale', 'lock', quantity=1).allowed


def test_only_the_add_ons_of_the_cancelled_rental_are_released():
    store, dana, step = stocked_rental(3)
    rn = ready(store, dana)
    mine, = renting.add_shop_items(store, BASE, rn, [('lock', 1)], STAFF, RENT_NOW)
    walk_in = step('walk-in', 'quote_sale', 'lock', quantity=1).made          # a counter quote, not attached to any rental
    cancel(store, rn)
    st = shop.state(books(store))
    assert mine in st.cancelled and walk_in not in st.cancelled


def test_an_add_on_whose_payment_can_still_settle_keeps_its_reservation_until_the_processor_answers(monkeypatch):
    store, rn, quote = funded_rental_with_a_lock()
    stopped_after_writing_the_intent(monkeypatch, store, rn)                    # the intent is written, its request may still reach the processor
    cancel(store, rn)
    st = shop.state(books(store))
    assert rent_of(store).rentals[rn].state == 'cancelled' and quote not in st.cancelled and st.available('lock') == 1       # still reserved (2 in stock)
    processor = Quiet()
    later = RENT_NOW + timedelta(minutes=16)
    delinquency.reconcile(store, BASE, processor, later)                        # the processor holds nothing for the key: the intent is closed
    renting.settle_payments(store, BASE, later)
    st = shop.state(books(store))
    assert quote in st.cancelled and st.available('lock') == 2                  # nothing can settle now, so the add-on is released


def test_a_late_payment_for_a_cancelled_rentals_add_on_is_posted_not_released(monkeypatch):
    store, rn, quote = funded_rental_with_a_lock()
    stopped_after_writing_the_intent(monkeypatch, store, rn)
    cancel(store, rn)
    processor = SimulatedProcessor()
    [intent] = delinquency.state(books(store)).intents.values()
    processor.create(intent.key, intent.cents, 'card_ok', {'copy': BASE, 'intent': intent.id, 'nonce': delinquency.state(books(store)).nonce}, RENT_NOW)
    delinquency.reconcile(store, BASE, processor, RENT_NOW)
    renting.settle_payments(store, BASE, RENT_NOW)
    st = shop.state(books(store))
    assert quote in st.sales and quote not in st.cancelled and st.quantity('lock') == 1          # the money was taken, so the sale stands
