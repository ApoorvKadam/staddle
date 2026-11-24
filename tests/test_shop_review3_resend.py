"""Review fix on counter payments: an attempt the processor was confirmed to hold nothing for is never sent again, however the form is
replayed, while an open attempt with no recorded problem still resumes with its own key. The first test fails on the code before the fix."""
from datetime import timedelta

from core import shop

from .test_core_operations_set_shop import NOW, World
from .test_shop_review_fixes import Dropping, quote_and_ask


def test_an_attempt_confirmed_to_hold_no_payment_is_not_sent_again_by_a_replayed_form():
    w = World()
    w.stock(3)
    processor = Dropping()
    quote, payment = quote_and_ask(w, processor)
    shop.reconcile(w.store, 'shop', processor, NOW + timedelta(minutes=20))     # the processor holds nothing for the key
    assert w.state().payments[quote]['problem']
    processor.down = False
    for which in ('first', payment['key'], None):
        again = shop.pay(w.store, 'shop', processor, quote, 'card_ok', NOW, which)
        assert again['key'] == payment['key'] and again['problem']              # its recorded problem comes back
    assert not processor.payments                                               # nothing was sent, so nothing can settle behind staff's retry


def test_staff_can_start_a_new_attempt_after_the_problem_and_the_old_form_cannot_pay_behind_it():
    w = World()
    w.stock(3)
    processor = Dropping()
    quote, payment = quote_and_ask(w, processor)
    shop.reconcile(w.store, 'shop', processor, NOW + timedelta(minutes=20))
    processor.down = False
    assert w.do('retry_payment', quote, card='card_ok').allowed
    replay = shop.pay(w.store, 'shop', processor, quote, 'card_ok', NOW, payment['key'])      # the form of the first attempt, sent again
    assert replay['key'] == payment['key'] and not processor.payments


def test_an_open_attempt_with_no_problem_still_resumes_with_its_own_key():
    w = World()
    w.stock(3)
    processor = Dropping()
    quote, payment = quote_and_ask(w, processor)
    processor.down = False
    resumed = shop.pay(w.store, 'shop', processor, quote, '', NOW, 'first')
    assert resumed['key'] == payment['key'] and resumed['status'] == 'succeeded' and len(processor.payments) == 1
