"""Payment attempts and refunds: the answer is credited to the attempt the processor answered, a form is bound to the attempt it shows,
a cancelled refund does not block a new one, a return cancels only the refunds its own attempt funded, and the owed-back item for a
replaced attempt says what happened. Each test fails on the code before the fix."""
from core import shop
from core.delinquency import SimulatedProcessor

from .test_core_operations_set_shop import NOW, World
from .test_fms import seen, sign_in, site
from .test_fms_renting_pages import desk_of
from .test_fms_shop_pages import hidden, part, post, state_of
from .test_shop_review_fixes import Dropping, late_success_on_a_replaced_attempt
from .test_shop_work_items import open_items


def posted_sale(card='card_ok'):
    """A counter sale paid and posted, with one lock brought back and put on the shelf."""
    w = World()
    w.stock(5)
    processor = SimulatedProcessor()
    quote = w.do('quote_sale', 'lock', quantity=1).made
    first = shop.pay(w.store, 'shop', processor, quote, card, NOW)
    shop.complete_confirmed(w.store, 'shop', NOW)
    return w, processor, quote, first


def brought_back(w, quote):
    returned = w.do('record_return', quote, quantity=1, condition='resalable', reason='Wrong size')
    assert returned.allowed and w.do('return_stock', returned.made).allowed
    return returned.made


def test_the_attempt_a_late_answer_belongs_to_is_the_one_returned():
    w = World()
    w.stock(3)
    processor = SimulatedProcessor()
    quote = w.do('quote_sale', 'lock', quantity=1).made
    first = shop.pay(w.store, 'shop', processor, quote, 'card_declined', NOW)
    assert w.do('retry_payment', quote, card='card_ok').allowed                                 # a newer attempt is now current
    current = w.state().payments[quote]
    assert current['key'] != first['key']
    processor.settle(first['outside'], 'succeeded', NOW)                                        # the first one turns up paid
    answered = shop._record_answer(w.store, 'shop', quote, processor.retrieve(first['outside']), NOW)
    assert answered['key'] == first['key'] and answered['status'] == 'succeeded'                # not the current attempt, which is still open
    assert w.state().payments[quote]['key'] == current['key'] and w.state().payments[quote]['status'] == 'open'
    same = shop._record_answer(w.store, 'shop', quote, processor.retrieve(first['outside']), NOW)
    assert same['key'] == first['key']                                                          # a repeat names the same attempt


def test_a_resume_form_is_bound_to_the_attempt_it_shows():
    w = World()
    w.stock(3)
    processor = SimulatedProcessor()
    quote = w.do('quote_sale', 'lock', quantity=1).made
    first = shop.pay(w.store, 'shop', processor, quote, 'card_declined', NOW)
    assert w.do('retry_payment', quote, card='card_ok').allowed                                 # another tab started the second attempt
    second = w.state().payments[quote]
    stale = w.do('resume_payment', quote, key=first['key'])
    assert not stale.allowed and stale.why == 'attempt_replaced' and 'older payment attempt' in stale.check
    assert not w.do('resume_payment', quote).allowed                                            # a form that names no attempt is not taken
    taken = w.do('resume_payment', quote, key=second['key'])
    assert taken.allowed and taken.made == second['key']


def test_the_resume_form_on_the_page_names_its_attempt_and_an_old_one_is_refused(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        fd.processor = Dropping()
        assert post(c, "sell", product="disc lock", quantity="1", card="card_ok").status_code == 303
        st, *_ = state_of(app, c)
        [quote] = st.payments
        sales = part(c.get("/shop").text, "sales")
        assert hidden(sales, "key") == st.payments[quote]["key"]                                # the form carries the attempt it shows
        refused = post(c, "resume_payment", about=quote, key="form:an-older-attempt")
        assert "Nothing changed. This page showed an older payment attempt" in seen(c.get(refused.headers["location"]).text)
        assert st.payments[quote]["status"] == "open" and not fd.processor.payments


def test_a_cancelled_refund_does_not_block_a_new_refund_for_the_same_return():
    w, processor, quote, first = posted_sale()
    returned = brought_back(w, quote)
    refund = w.do('request_refund', returned)
    assert refund.allowed and not w.do('request_refund', returned).allowed                      # a live refund still blocks a second one
    processor.settle(first['outside'], 'returned', NOW)                                         # the bank takes the first payment back: its unsent refund is cancelled
    shop.reconcile(w.store, 'shop', processor, NOW)
    assert w.state().refunds[refund.made]['cancelled']
    assert w.do('retry_payment', quote, card='card_ok').allowed
    second = shop.pay(w.store, 'shop', processor, quote, 'card_ok', NOW)
    assert second['status'] == 'succeeded'
    again = w.do('request_refund', returned)                                                    # the replacement payment holds the money, so the return is refundable
    assert again.allowed and w.state().refunds[again.made]['key'] == second['key']


def test_a_return_cancels_only_the_refunds_its_own_attempt_funded():
    w = World()
    w.stock(5)
    processor = SimulatedProcessor()
    quote = w.do('quote_sale', 'lock', quantity=1).made
    first = shop.pay(w.store, 'shop', processor, quote, 'card_declined', NOW)
    assert w.do('retry_payment', quote, card='card_ok').allowed
    second = shop.pay(w.store, 'shop', processor, quote, 'card_ok', NOW)
    shop.complete_confirmed(w.store, 'shop', NOW)
    returned = brought_back(w, quote)
    refund = w.do('request_refund', returned).made
    assert w.state().refunds[refund]['key'] == second['key']                                    # the matched refund records the attempt that funds it
    processor.settle(first['outside'], 'succeeded', NOW)                                        # the first attempt turns up paid, then comes back
    shop.reconcile(w.store, 'shop', processor, NOW)
    processor.settle(first['outside'], 'returned', NOW)
    shop.reconcile(w.store, 'shop', processor, NOW)
    assert [a['status'] for a in w.state().attempts[quote]] == ['returned']
    assert not w.state().refunds[refund].get('cancelled')                                       # the other attempt's refund stands
    processor.settle(second['outside'], 'returned', NOW)                                        # its own attempt coming back does cancel it
    shop.reconcile(w.store, 'shop', processor, NOW)
    assert w.state().refunds[refund]['cancelled']


def test_a_refund_owed_for_a_replaced_attempt_says_an_extra_charge_and_claims_no_handover():
    w, quote, first = late_success_on_a_replaced_attempt('reconciliation')
    [item] = open_items(w, 'shop refund owed')
    text = f'{item.why} {item.closes}'
    assert 'earlier attempt' in text and 'extra charge' in text and 'owed back' in text
    for claim in ('cancelled', 'written off', 'handed over', 'No sale was posted'):
        assert claim not in text
