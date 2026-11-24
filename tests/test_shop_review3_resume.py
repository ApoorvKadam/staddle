"""Review fix on resuming a move-in payment: the amount the renter was shown is checked against the intent that would be sent, so a
resume never takes a different amount. The first test fails on the code before the fix."""
import pytest

from core import renting
from core.store import BASE

from .test_core_renting import NOW as RENT_NOW
from .test_core_renting import billing
from .test_core_renting import rent as rent_of
from .test_core_renting_pay import ready
from .test_shop_review_fixes import STAFF, Quiet, stocked_rental


def test_a_payment_still_out_is_not_resumed_for_a_different_amount_than_the_renter_was_shown():
    store, dana, _ = stocked_rental()
    rn = ready(store, dana)
    rental_only = rent_of(store).rentals[rn].quote['total_cents']
    renting.add_shop_items(store, BASE, rn, [('lock', 1)], STAFF, RENT_NOW)
    total = rental_only + 1500
    processor = Quiet()
    intent = renting.pay_rental(store, BASE, processor, rn, 'card_ok', RENT_NOW, expected_cents=total)
    assert intent.state == 'open' and intent.cents == total and not processor.payments         # the request was lost
    processor.down = False
    with pytest.raises(renting.TotalChanged):                                                  # a page that showed the rental alone
        renting.pay_rental(store, BASE, processor, rn, 'card_ok', RENT_NOW, expected_cents=rental_only)
    assert not processor.payments                                                              # the larger amount was not sent
    resumed = renting.pay_rental(store, BASE, processor, rn, 'card_ok', RENT_NOW, expected_cents=total)
    (charged,) = processor.payments.values()
    assert resumed.id == intent.id and resumed.state == 'succeeded' and charged['cents'] == total and charged['key'] == intent.key


def test_the_checkout_total_is_the_open_request_amount_after_credit_arrives():
    store, dana, _ = stocked_rental()
    rn = ready(store, dana)
    rental_only = rent_of(store).rentals[rn].quote['total_cents']
    renting.add_shop_items(store, BASE, rn, [('lock', 1)], STAFF, RENT_NOW)
    total = rental_only + 1500
    processor = Quiet()
    intent = renting.pay_rental(store, BASE, processor, rn, 'card_ok', RENT_NOW, expected_cents=total)     # the request was lost
    billing(store, lambda led: led.receive_payment(dana, 2500, RENT_NOW))                                    # credit arrives meanwhile
    books, rent = renting.load(store, BASE)
    shown = renting.checkout_due(books, rent, rent.rentals[rn], RENT_NOW)[2]
    assert shown == total                       # the page shows what the open request holds, so the renter can resume it
    processor.down = False
    resumed = renting.pay_rental(store, BASE, processor, rn, 'card_ok', RENT_NOW, expected_cents=shown)
    assert resumed.id == intent.id and resumed.state == 'succeeded'
