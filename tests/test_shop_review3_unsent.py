"""Review fix on add-on-only payments: an intent written for add-ons alone but never sent is found again from its rental and resumed with
its own key, without touching the rental's billing. The first two tests fail on the code before the fix."""
from datetime import timedelta

import pytest

from core import delinquency, renting
from core.billing import Ledger, Refused
from core.delinquency import SimulatedProcessor
from core.store import BASE

from .test_core_renting import NOW as RENT_NOW
from .test_core_renting import accept as accept_rental
from .test_core_renting import act, billing, books, ok
from .test_core_renting import rent as rent_of
from .test_shop_review_fixes import STAFF, Quiet, stocked_rental


def funded_rental_with_a_lock():
    """A rental whose account credit already covers the rent, with one lock added at the desk: only the lock is left to pay."""
    store, dana, _ = stocked_rental()
    reservation = ok(store, act('reserve', 'staff:manager', 'A101', dana, start=(RENT_NOW + timedelta(days=5)).date().isoformat()), RENT_NOW)
    rn = ok(store, act('start_checkout', 'staff:manager', reservation, dana), RENT_NOW)
    billing(store, lambda led: led.receive_payment(dana, rent_of(store).rentals[rn].quote['total_cents'], RENT_NOW))
    assert accept_rental(store, dana, rn, RENT_NOW).allowed
    quote, = renting.add_shop_items(store, BASE, rn, [('lock', 1)], STAFF, RENT_NOW)
    return store, rn, quote


def stopped_after_writing_the_intent(monkeypatch, store, rn):
    """The checkout stops after the intent is committed and before the request is sent."""
    def stop(*args, **kwargs):
        raise renting.Crash('stopped after the intent was written')
    real_send = delinquency.send
    monkeypatch.setattr(delinquency, 'send', stop)
    with pytest.raises(renting.Crash):
        renting.pay_rental(store, BASE, SimulatedProcessor(), rn, 'card_ok', RENT_NOW, expected_cents=1500)
    monkeypatch.setattr(delinquency, 'send', real_send)


def test_an_add_on_only_intent_that_was_never_sent_is_resumed_with_its_own_key(monkeypatch):
    store, rn, quote = funded_rental_with_a_lock()
    stopped_after_writing_the_intent(monkeypatch, store, rn)
    [written] = delinquency.state(books(store)).intents.values()
    assert written.state == 'open' and rent_of(store).rentals[rn].pay_ops == []                # nothing was sent, and the rental's billing is untouched
    processor = SimulatedProcessor()
    intent = renting.pay_rental(store, BASE, processor, rn, 'card_ok', RENT_NOW, expected_cents=1500)
    (charged,) = processor.payments.values()
    assert intent.id == written.id and intent.state == 'succeeded' and charged['key'] == written.key and charged['cents'] == 1500
    assert len(delinquency.state(books(store)).intents) == 1 and rent_of(store).rentals[rn].pay_ops == []


def test_an_unsent_add_on_only_intent_is_not_resumed_for_another_amount(monkeypatch):
    store, rn, quote = funded_rental_with_a_lock()
    stopped_after_writing_the_intent(monkeypatch, store, rn)
    processor = SimulatedProcessor()
    with pytest.raises(renting.TotalChanged):
        renting.pay_rental(store, BASE, processor, rn, 'card_ok', RENT_NOW, expected_cents=1000)
    assert not processor.payments


def test_a_new_payment_is_still_refused_while_an_unrelated_payment_of_the_tenant_is_out():
    store, rn, quote = funded_rental_with_a_lock()
    processor = Quiet()
    with store.write():                                                                        # another intent of the same tenant, not this rental's
        led = Ledger(books(store))
        delinquency.open_intent(led, BASE, processor, rent_of(store).rentals[rn].tenant, 700, RENT_NOW)
        store.append(BASE, led.pending)
    with pytest.raises(Refused, match='outcome is not known'):
        renting.pay_rental(store, BASE, processor, rn, 'card_ok', RENT_NOW, expected_cents=1500)
    assert not processor.payments
