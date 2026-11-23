"""Shop money and stock defects found in review: each test fails on the code before the fix.

Counter payments that never reached the processor, replayed forms, late confirmations, payments that arrive after a cancellation, refunds
and counts that disagree. Items close only on a record, never on a staff word."""
from datetime import timedelta

import pytest

from core import delinquency, renting, shop, work
from core.billing import Ledger, Refused
from core.delinquency import ProcessorRefused, ProcessorTimeout, SimulatedProcessor
from core.store import BASE
from desk.front_desk import FrontDesk

from .test_core_operations_set_shop import NOW, World
from .test_core_renting import NOW as RENT_NOW
from .test_core_renting import books, facility
from .test_core_renting import rent as rent_of
from .test_core_renting_pay import ready
from .test_front_desk_sweep import clock
from .test_shop_work_items import open_items

ITEM = 'shop payment needs review'
STAFF = 'staff:office_manager'
LATER = NOW + timedelta(minutes=20)


class Dropping(SimulatedProcessor):
    """A processor that does not get the request: the call times out before any payment exists, until it is put right."""
    def __init__(self):
        super().__init__()
        self.down = True

    def create(self, key, cents, card, metadata, now):
        if self.down:
            raise ProcessorTimeout(key)
        return super().create(key, cents, card, metadata, now)


def totals(w):
    out = {}
    for _, account, cents, _c in w.store.journal('shop'):
        out[account] = out.get(account, 0) + cents
    return out


def quote_and_ask(w, processor, card='card_ok', quantity=1):
    quote = w.do('quote_sale', 'lock', quantity=quantity)
    assert quote.allowed
    return quote.made, shop.pay(w.store, 'shop', processor, quote.made, card, NOW)


def stocked_rental(lock_stock=2):
    store, dana, _ = facility()

    def step(ident, action, about=None, **form):
        return shop.run(store, BASE, shop.Act(ident, action, STAFF, about, form), RENT_NOW)
    step('product', 'publish_product', 'lock', price=1500, cost=500, tax_class='standard', active=True)
    delivery = step('delivery', 'record_delivery', product='lock', quantity=lock_stock, reason='Order')
    receipt = step('receipt', 'record_receipt', product='lock', quantity=lock_stock, delivery=delivery.made, reason='Counted')
    step('stock', 'receive_stock', receipt.made)
    return store, dana, step


def test_a_product_that_is_not_text_is_refused_not_a_crash():
    store, dana, _ = stocked_rental()
    rn = ready(store, dana)
    before = len(store.events())
    with pytest.raises(Refused):
        renting.add_shop_items(store, BASE, rn, [(['lock'], 1)], STAFF, RENT_NOW)
    with pytest.raises(Refused):
        renting.add_shop_items(store, BASE, rn, [({'x': 1}, 1)], STAFF, RENT_NOW)
    assert len(store.events()) == before
    w = World()
    w.stock(2)
    refused = w.do('quote_sale', ['lock'], quantity=1)
    assert not refused.allowed and 'words' in refused.check


def test_a_counter_payment_that_never_reached_the_processor_is_resumed_with_its_own_key():
    w = World()
    w.stock(3)
    processor = Dropping()
    quote, payment = quote_and_ask(w, processor)
    assert payment['status'] == 'open' and not processor.payments
    assert not w.do('retry_payment', quote).allowed and not w.do('cancel_sale', quote, reason='Gone').allowed     # it may still come in
    shop.reconcile(w.store, 'shop', processor, NOW + timedelta(minutes=5))                    # too early to say nothing exists
    assert not w.state().payments[quote].get('problem')
    processor.down = False
    assert w.do('resume_payment', quote, key=payment['key']).allowed
    resumed = shop.pay(w.store, 'shop', processor, quote, '', NOW, w.state().payments[quote]['key'])
    assert resumed['status'] == 'succeeded' and resumed['key'] == payment['key'] and len(processor.payments) == 1     # the same attempt, never a second payment
    shop.complete_confirmed(w.store, 'shop', NOW)
    assert quote in w.state().sales and w.state().quantity('lock') == 2


def test_a_payment_the_processor_confirms_it_never_got_can_be_tried_again_or_cancelled():
    w = World()
    w.stock(3)
    processor = Dropping()
    quote, _ = quote_and_ask(w, processor)
    assert w.state().available('lock') == 2
    shop.reconcile(w.store, 'shop', processor, LATER)           # the processor holds nothing for the key after the request had its time
    assert w.state().payments[quote]['problem'] and len(open_items(w, ITEM)) == 1
    assert w.do('cancel_sale', quote, reason='The buyer left').allowed
    assert w.state().available('lock') == 3 and open_items(w, ITEM) == []
    again, _ = quote_and_ask(w, processor)
    shop.reconcile(w.store, 'shop', processor, LATER)
    assert w.do('retry_payment', again).allowed                  # a new attempt only once nothing can come in on the old one
    assert [a['status'] for a in w.state().attempts[again]] == ['open']


def test_a_refused_request_is_confirmed_with_the_processor_before_the_sale_is_closed():
    class Refusing(SimulatedProcessor):
        def create(self, key, cents, card, metadata, now):
            raise ProcessorRefused('refused')

        def find(self, key, since=None):
            raise ProcessorTimeout(key)

    w = World()
    w.stock(2)
    quote, payment = quote_and_ask(w, Refusing())
    assert not w.state().payments[quote].get('problem')           # the processor could not say whether it holds a payment
    assert not w.do('cancel_sale', quote, reason='Gone').allowed
    refused = Refusing()
    refused.find = lambda key, since=None: None                   # it holds nothing for the key
    shop.pay(w.store, 'shop', refused, quote, 'card_ok', NOW)
    assert w.state().payments[quote]['problem'] and w.do('cancel_sale', quote, reason='Gone').allowed


def test_the_declined_billing_entry_is_the_rent_without_the_add_on():
    store, dana, _ = stocked_rental()
    rn = ready(store, dana)
    rental_total = rent_of(store).rentals[rn].quote['total_cents']
    renting.add_shop_items(store, BASE, rn, [('lock', 1)], STAFF, RENT_NOW)
    processor = SimulatedProcessor()
    assert renting.pay_rental(store, BASE, processor, rn, 'card_declined', RENT_NOW).state == 'failed'
    assert [d['cents'] for d in books(store).declined] == [rental_total]          # the add-on's $15 is declined in the shop's own records
    assert next(iter(processor.payments.values()))['cents'] == rental_total + 1500
    assert shop.state(books(store)).payments[next(iter(shop.state(books(store)).quotes))]['status'] == 'failed'


def test_the_review_item_waits_while_a_retry_has_no_answer_and_closes_on_its_success():
    w = World()
    w.stock(3)
    processor = SimulatedProcessor()
    quote, payment = quote_and_ask(w, processor, 'card_declined')
    assert w.do('retry_payment', quote, card='card_pending').allowed
    shop.pay(w.store, 'shop', processor, quote, 'card_pending', NOW)
    assert w.state().payments[quote]['status'] == 'processing'
    [item] = open_items(w, ITEM)
    assert item.waits and item.tone == 'wait'
    processor.settle(w.state().payments[quote]['outside'], 'succeeded', NOW)
    shop.webhook(w.store, processor, processor.take_events()[-1], NOW)
    shop.complete_confirmed(w.store, 'shop', NOW)
    assert open_items(w, ITEM) == [] and quote in w.state().sales


class Flaky(SimulatedProcessor):
    """The next request to create a payment is lost before it reaches the processor."""
    def __init__(self, inner):
        self.__dict__ = inner.__dict__
        self.lose = False

    def create(self, key, cents, card, metadata, now):
        if self.lose:
            self.lose = False
            raise ProcessorTimeout(key)
        return super().create(key, cents, card, metadata, now)


def test_a_replayed_form_resumes_only_the_attempt_it_made_with_its_own_card():
    desk = FrontDesk(':memory:', clock=clock)
    desk.processor = Flaky(desk.processor)
    store, visitor = desk.ready(), 'replay'
    stream = desk.stream(visitor)
    sale = shop.Act('form-sale', 'quote_sale', STAFF, 'disc lock', {'quantity': 1})
    desk.shop_counter_sale(visitor, sale, 'card_declined')
    quote = 'form-sale'
    assert shop.state(store.books(stream)).payments[quote]['status'] == 'failed'
    desk.processor.lose = True                                   # the retry's request never gets through
    desk.shop_retry_payment(visitor, shop.Act('form-retry', 'retry_payment', STAFF, quote), 'card_ok')
    retry_key = shop.state(store.books(stream)).payments[quote]['key']
    assert shop.state(store.books(stream)).payments[quote]['status'] == 'open' and desk.processor.find(retry_key) is None
    desk.shop_counter_sale(visitor, sale, 'card_declined')       # the first form is sent again with its own card
    current = shop.state(store.books(stream)).payments[quote]
    assert current['key'] == retry_key and current['status'] == 'open' and desk.processor.find(retry_key) is None     # the new attempt is not asked on the old card
    desk.shop_retry_payment(visitor, shop.Act('form-retry', 'retry_payment', STAFF, quote), 'card_declined')          # the retry form again, whatever card it carries
    assert shop.state(store.books(stream)).payments[quote]['status'] == 'succeeded'                                 # asked once more with the card it was made with
    assert quote in shop.state(store.books(stream)).sales and len(desk.processor.payments) == 2


def test_a_late_confirmation_posts_the_counter_sale_but_never_for_a_cancelled_quote():
    desk = FrontDesk(':memory:', clock=clock)
    store, stream = desk.ready(), 'visitor:late'
    first = shop.run(store, stream, shop.Act('late-quote', 'quote_sale', STAFF, 'disc lock', {'quantity': 1}), clock())
    desk.processor.lose_next_reply = True
    assert shop.pay(store, stream, desk.processor, first.made, 'card_ok', clock())['status'] == 'open'
    desk.processor.take_events()
    desk.sweep()
    st = shop.state(store.books(stream))
    assert st.payments[first.made]['status'] == 'succeeded' and first.made in st.sales and st.quantity('disc lock') == 11
    desk.sweep()
    assert len([m for m in shop.state(store.books(stream)).moves if m['kind'] == 'sold' and m['reference'] == first.made]) == 1


def payment_after_cancellation(processor):
    """A counter payment whose request was lost, closed by the processor's own record and cancelled, which then turns up paid."""
    w = World()
    w.stock(3)
    quote, payment = quote_and_ask(w, processor)
    shop.reconcile(w.store, 'shop', processor, LATER)
    assert w.do('cancel_sale', quote, reason='The buyer left').allowed
    processor.down = False
    processor.create(payment['key'], payment['cents'], 'card_ok', {'copy': 'shop', 'shop_quote': quote}, LATER)
    return w, quote, payment


def test_a_payment_that_succeeds_after_the_sale_was_cancelled_is_owed_back_by_a_refund_record():
    processor = Dropping()
    w, quote, payment = payment_after_cancellation(processor)
    shop.reconcile(w.store, 'shop', processor, LATER)
    shop.complete_confirmed(w.store, 'shop', LATER)
    st = w.state()
    assert st.payments[quote]['status'] == 'succeeded' and quote not in st.sales and st.available('lock') == 3     # no sale, stock stays released
    assert not any(m['kind'] == 'sold' for m in st.moves) and shop.differences(st) == []
    shop.sweep(w.store, 'shop', LATER)
    [item] = open_items(w, 'shop refund owed')
    assert item.key == f"shop:refund:{quote}:{payment['key']}" and not item.attest
    said = work.run(w.store, 'shop', work.Act('say-refunded', 'finish_work', STAFF, item.key, None, 'I refunded it'), LATER)
    assert not said.allowed and len(open_items(w, 'shop refund owed')) == 1                                       # a staff word closes nothing
    assert not w.do('refund_payment', quote, key='nothing', reason='Money back').allowed
    assert not w.do('refund_payment', quote, key=payment['key'], reason=' ').allowed
    assert w.do('refund_payment', quote, key=payment['key'], reason='Cancelled before it arrived').allowed
    assert open_items(w, 'shop refund owed') == [] and not w.do('refund_payment', quote, key=payment['key'], reason='Again').allowed
    t = totals(w)
    assert t[f'receivable:shop:{quote}'] == 0 and t['clearing:card'] == 1500 and sum(v for k, v in t.items() if k.startswith('refund:shop:')) == -1500
    assert t.get('income:merchandise', 0) == 0 and w.state().quantity('lock') == 3


def test_staff_can_ask_for_a_recount_when_two_counts_disagree():
    w = World()
    w.stock(12)
    a = w.do('record_count', 'lock', quantity=9)
    b = w.do('record_count', 'lock', quantity=10)
    assert 'recount' in w.do('review_count', a.made, reason='Look').check                        # approving either one is still refused
    assert w.do('review_count', a.made, reason='The shelves were checked in a hurry', recount=True).allowed
    refused = w.do('review_count', b.made, reason='Accept it')
    assert not refused.allowed and refused.why == 'counts_conflict'                               # the conflict stands until a new count
    assert not w.do('adjust_stock', 'lock', kind='count variance', quantity=-2, count=b.made, reason='Fix').allowed
    c = w.do('record_count', 'lock', quantity=10)
    assert w.state().superseded(a.made) and not w.state().superseded(b.made)
    assert w.do('review_count', b.made, reason='Two counts agree').allowed
    assert w.do('adjust_stock', 'lock', kind='count variance', quantity=-2, count=b.made, reason='Two counts agree').allowed
    assert w.state().quantity('lock') == 10 and c.allowed


def test_merchandise_totals_come_from_the_same_snapshot_as_sales_and_stock():
    from desk.fms import Facility
    desk = FrontDesk(':memory:', clock=clock)
    visitor = 'snapshot'
    desk.ready()
    page = Facility(desk, visitor)
    before = page.shop_page()
    assert desk.shop_counter_sale(visitor, shop.Act('snap-sale', 'quote_sale', STAFF, 'disc lock', {'quantity': 1}), 'card_ok')
    shown = page.shop_page()                                      # the page that was read before the sale still describes one moment
    assert shown['books'] == before['books'] and len(shown['sales']) == len(before['sales'])
    assert Facility(desk, visitor).shop_page()['books']['revenue'] == before['books']['revenue'] + 1500


def test_an_unpaid_refund_is_cancelled_when_the_original_charge_comes_back():
    w = World()
    w.stock(3)
    processor = SimulatedProcessor()
    quote, payment = quote_and_ask(w, processor)
    assert w.do('complete_sale', quote).allowed
    ret = w.do('record_return', quote, quantity=1, condition='resalable', reason='Wrong size')
    assert w.do('return_stock', ret.made).allowed
    refund = w.do('request_refund', ret.made)
    assert refund.allowed and totals(w)[f'refund:shop:{refund.made}'] == -1500
    processor.settle(payment['outside'], 'returned', NOW)
    assert shop.webhook(w.store, processor, processor.take_events()[-1], NOW) == 'applied'
    assert w.state().refunds[refund.made]['cancelled']
    t = totals(w)
    assert t[f'refund:shop:{refund.made}'] == 0 and t['income:merchandise'] == -1500 and t[f'receivable:shop:{quote}'] == 1500 and t['clearing:card'] == 0
    assert shop.differences(w.state()) == []


class Quiet(SimulatedProcessor):
    """A processor whose request for the move-in payment is lost before it holds anything."""
    down = True

    def create(self, key, cents, card, metadata, now):
        if self.down:
            raise ProcessorTimeout(key)
        return super().create(key, cents, card, metadata, now)


def test_an_add_on_staff_cancelled_is_not_posted_again_when_the_move_in_payment_turns_up():
    store, dana, step = stocked_rental(1)
    rn = ready(store, dana)
    quote, = renting.add_shop_items(store, BASE, rn, [('lock', 1)], STAFF, RENT_NOW)
    processor = Quiet()
    intent = renting.pay_rental(store, BASE, processor, rn, 'card_ok', RENT_NOW)
    assert intent.state == 'open'
    soon = RENT_NOW + timedelta(minutes=16)
    delinquency.reconcile(store, BASE, processor, soon)         # the processor holds nothing for the key: the intent is closed
    assert delinquency.state(books(store)).intents[intent.id].state == 'closed'
    assert step('cancel', 'cancel_sale', quote, reason='The renter will pay later').allowed
    sold = step('walk-in', 'quote_sale', 'lock', quantity=1)                 # the released lock goes to someone else
    assert sold.allowed and shop.state(books(store)).available('lock') == 0
    processor.down = False
    nonce = delinquency.state(books(store)).nonce
    processor.create(intent.key, intent.cents, 'card_ok', {'copy': BASE, 'intent': intent.id, 'nonce': nonce}, soon)
    delinquency.reconcile(store, BASE, processor, soon)         # the charge did go through after all
    st = shop.state(books(store))
    assert delinquency.state(books(store)).intents[intent.id].state == 'succeeded'
    assert quote not in st.sales and st.quantity('lock') == 1 and st.available('lock') == 0         # not posted, stock not taken twice
    assert [q for q, a in st.refunds_owed()] == [quote]


def test_a_payment_still_with_the_processor_is_never_tried_again_because_a_request_was_refused():
    """The refusal of one request does not say the earlier payment cannot come in: a retry then would charge the buyer twice."""
    class Refusing(SimulatedProcessor):
        refusing = False

        def create(self, key, cents, card, metadata, now):
            if self.refusing:
                raise ProcessorRefused('refused')
            return super().create(key, cents, card, metadata, now)

    w = World()
    w.stock(3)
    processor = Refusing()
    quote, payment = quote_and_ask(w, processor, 'card_pending')
    assert w.state().payments[quote]['status'] == 'processing'
    processor.refusing = True
    shop.pay(w.store, 'shop', processor, quote, 'card_pending', NOW)         # the processor still holds the payment
    assert not w.state().payments[quote].get('problem')
    assert not w.do('retry_payment', quote).allowed and not w.do('cancel_sale', quote, reason='Gone').allowed
    assert len(processor.payments) == 1


def late_success_on_a_replaced_attempt(by):
    """A declined sale paid again by a retry that succeeded, then the first attempt turns up paid at the processor."""
    w = World()
    w.stock(3)
    processor = SimulatedProcessor()
    quote, first = quote_and_ask(w, processor, 'card_declined')
    assert w.do('retry_payment', quote, card='card_ok').allowed
    assert shop.pay(w.store, 'shop', processor, quote, 'card_ok', NOW)['status'] == 'succeeded'
    assert shop.complete_confirmed(w.store, 'shop', NOW) == 1
    processor.take_events()
    processor.settle(first['outside'], 'succeeded', NOW)
    if by == 'webhook':
        assert shop.webhook(w.store, processor, processor.take_events()[-1], NOW) == 'applied'
    else:
        shop.reconcile(w.store, 'shop', processor, NOW)
    return w, quote, first


@pytest.mark.parametrize('by', ['webhook', 'reconciliation'])
def test_a_late_success_on_a_replaced_attempt_is_recorded_and_owed_back_until_a_refund_record(by):
    w, quote, first = late_success_on_a_replaced_attempt(by)
    st = w.state()
    assert [a['status'] for a in st.attempts[quote]] == ['succeeded'] and st.payments[quote]['status'] == 'succeeded'
    assert len([m for m in st.moves if m['kind'] == 'sold']) == 1                          # one sale, one stock movement
    [item] = open_items(w, 'shop refund owed')
    assert item.key == f"shop:refund:{quote}:{first['key']}"
    assert totals(w)['clearing:card'] == 3000                                                  # both charges are on the books
    assert not w.do('refund_payment', quote, key=st.payments[quote]['key'], reason='Wrong attempt').allowed
    assert w.do('refund_payment', quote, key=first['key'], reason='Charged twice').allowed
    assert open_items(w, 'shop refund owed') == []
    t = totals(w)
    assert t[f'receivable:shop:{quote}'] == 0
    assert sum(v for k, v in t.items() if k.startswith('refund:shop:')) == -1500 and t['income:merchandise'] == -1500


def test_an_add_on_cannot_be_cancelled_while_its_rental_payment_can_still_settle():
    store, dana, step = stocked_rental()
    rn = ready(store, dana)
    quote, = renting.add_shop_items(store, BASE, rn, [('lock', 1)], STAFF, RENT_NOW)
    processor = Quiet()
    intent = renting.pay_rental(store, BASE, processor, rn, 'card_ok', RENT_NOW)
    assert intent.state == 'open'
    with store.write():                                                   # a mark that the processor refused something, while the intent is still waiting
        led = Ledger(books(store))
        led.emit('shop.payment_problem', RENT_NOW, {'quote': quote}, 'stale-problem', source='processor')
        store.append(BASE, led.pending)
    refused = step('cancel-early', 'cancel_sale', quote, reason='Changed mind')
    assert not refused.allowed and 'can still go through' in refused.check
    processor.down = False
    processor.create(intent.key, intent.cents, 'card_ok', {'copy': BASE, 'intent': intent.id, 'nonce': delinquency.state(books(store)).nonce}, RENT_NOW)
    delinquency.reconcile(store, BASE, processor, RENT_NOW)
    st = shop.state(books(store))
    assert quote in st.sales and quote not in st.cancelled and st.quantity('lock') == 1
