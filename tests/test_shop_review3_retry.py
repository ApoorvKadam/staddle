"""Review fix on a declined move-in checkout with add-ons: a new mixed attempt is allowed once the earlier one can no longer succeed, the old
attempt stays on record under its own key, and a late answer from it never settles the new one. Each test fails on the code before the fix."""
import pytest

from core import delinquency, renting, shop
from core.billing import Refused
from core.delinquency import ProcessorRefused, SimulatedProcessor
from core.store import BASE

from .test_core_renting import NOW as RENT_NOW
from .test_core_renting import books
from .test_core_renting import rent as rent_of
from .test_core_renting_pay import deliver, ready
from .test_shop_review_fixes import STAFF, Quiet, stocked_rental


def declined_mixed_checkout(stock=2):
    store, dana, step = stocked_rental(stock)
    rn = ready(store, dana)
    quote, = renting.add_shop_items(store, BASE, rn, [('lock', 1)], STAFF, RENT_NOW)
    processor = SimulatedProcessor()
    first = renting.pay_rental(store, BASE, processor, rn, 'card_declined', RENT_NOW)
    assert first.state == 'failed' and shop.state(books(store)).payments[quote]['status'] == 'failed'
    return store, step, processor, rn, quote, first


def test_a_declined_mixed_checkout_is_paid_again_with_its_add_ons_under_a_new_key():
    store, step, processor, rn, quote, first = declined_mixed_checkout()
    second = renting.pay_rental(store, BASE, processor, rn, 'card_ok', RENT_NOW)
    st = shop.state(books(store))
    assert second.state == 'succeeded' and second.key != first.key and second.cents == first.cents
    assert quote in st.sales and st.quantity('lock') == 1 and len([m for m in st.moves if m['kind'] == 'sold']) == 1
    assert [(a['key'], a['status']) for a in st.attempts[quote]] == [(first.key, 'failed')]       # the declined attempt stays under its own key
    assert st.payments[quote]['key'] == second.key and st.payments[quote]['intent'] == second.id
    assert rent_of(store).rentals[rn].state == 'active' and len(processor.payments) == 2


def test_a_late_answer_from_the_declined_attempt_never_settles_the_new_one():
    store, step, processor, rn, quote, first = declined_mixed_checkout()
    second = renting.pay_rental(store, BASE, processor, rn, 'card_pending', RENT_NOW)             # the new attempt is still with the processor
    assert second.state == 'pending'
    processor.take_events()
    processor.settle(first.outside, 'succeeded', RENT_NOW)                                        # the declined one turns up paid
    deliver(store, processor)
    st = shop.state(books(store))
    assert st.payments[quote]['key'] == second.key and st.payments[quote]['status'] == 'processing'    # the new attempt did not move
    assert quote not in st.sales and st.quantity('lock') == 2
    assert [(a['key'], a['status']) for a in st.attempts[quote]] == [(first.key, 'succeeded')]    # the late success is on the old attempt alone
    assert [(q, a['key']) for q, a in st.refunds_owed()] == [(quote, first.key)]                  # and shows as money owed back
    processor.settle(second.outside, 'succeeded', RENT_NOW)                                       # the new attempt then comes in
    deliver(store, processor)
    st = shop.state(books(store))
    assert quote in st.sales and len([m for m in st.moves if m['kind'] == 'sold']) == 1


def test_a_mixed_retry_never_replaces_an_attempt_that_can_still_settle():
    store, dana, step = stocked_rental()
    rn = ready(store, dana)
    quote, = renting.add_shop_items(store, BASE, rn, [('lock', 1)], STAFF, RENT_NOW)
    processor = SimulatedProcessor()
    pending = renting.pay_rental(store, BASE, processor, rn, 'card_pending', RENT_NOW)
    again = renting.pay_rental(store, BASE, processor, rn, 'card_ok', RENT_NOW)                   # the same attempt is asked about, never a second one
    assert again.id == pending.id and again.key == pending.key and len(processor.payments) == 1
    assert len(delinquency.state(books(store)).intents) == 1
    assert not shop.state(books(store)).attempts and shop.state(books(store)).payments[quote]['intent'] == pending.id


def test_a_second_refused_attempt_is_marked_so_staff_can_try_again_or_cancel():
    class Refusing(SimulatedProcessor):
        def create(self, key, cents, card, metadata, now):
            raise ProcessorRefused('refused')

    store, dana, step = stocked_rental()
    rn = ready(store, dana)
    quote, = renting.add_shop_items(store, BASE, rn, [('lock', 1)], STAFF, RENT_NOW)
    assert renting.pay_rental(store, BASE, Refusing(), rn, 'card_ok', RENT_NOW).state == 'closed'
    assert renting.pay_rental(store, BASE, Refusing(), rn, 'card_ok', RENT_NOW).state == 'closed'      # the second attempt is refused too
    st = shop.state(books(store))
    assert len(st.attempts[quote]) == 1 and st.payments[quote]['problem'] and st.can_close(quote)


def test_an_add_on_replaced_after_a_decline_is_paid_with_the_rental_again():
    store, step, processor, rn, quote, first = declined_mixed_checkout()
    assert step('cancel-declined', 'cancel_sale', quote, reason='The renter wants another item').allowed
    other, = renting.add_shop_items(store, BASE, rn, [('lock', 1)], STAFF, RENT_NOW)               # the rental's declined payment does not block a new add-on
    second = renting.pay_rental(store, BASE, processor, rn, 'card_ok', RENT_NOW)
    st = shop.state(books(store))
    assert second.state == 'succeeded' and second.shop_quotes == [other] and other in st.sales and quote not in st.sales
    assert rent_of(store).rentals[rn].state == 'active'


def test_an_add_on_still_cannot_be_added_while_a_payment_of_the_rental_is_out():
    store, dana, step = stocked_rental(3)
    rn = ready(store, dana)
    renting.add_shop_items(store, BASE, rn, [('lock', 1)], STAFF, RENT_NOW)
    renting.pay_rental(store, BASE, Quiet(), rn, 'card_ok', RENT_NOW)                              # the request was lost: the payment may still come in
    with pytest.raises(Refused):
        renting.add_shop_items(store, BASE, rn, [('lock', 1)], STAFF, RENT_NOW)
