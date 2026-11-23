"""A declined or returned shop payment is settled by a record (a new payment, a cancelled sale, a write-off), never by a staff word."""
from core import shop, work
from core.delinquency import SimulatedProcessor
from tests.test_core_operations_set_shop import NOW, World
from tests.test_shop_work_items import open_items

ITEM = 'shop payment needs review'


def totals(w):
    out = {}
    for _, account, cents, _c in w.store.journal('shop'):
        out[account] = out.get(account, 0) + cents
    return out


def sold_then_returned(w, processor):
    """A sale posted on a confirmed payment that the bank then took back."""
    quote = w.do('quote_sale', 'lock', quantity=1)
    payment = shop.pay(w.store, 'shop', processor, quote.made, 'card_ok', NOW)
    assert w.do('complete_sale', quote.made).allowed
    processor.settle(payment['outside'], 'returned', NOW)
    assert shop.webhook(w.store, processor, processor.take_events()[-1], NOW) == 'applied'
    assert w.state().payments[quote.made]['status'] == 'returned'
    return quote.made


def declined(w, processor, quantity=1):
    quote = w.do('quote_sale', 'lock', quantity=quantity)
    assert shop.pay(w.store, 'shop', processor, quote.made, 'card_declined', NOW)['status'] == 'failed'
    return quote.made


def test_done_i_checked_it_is_refused_for_a_declined_shop_payment():
    w = World()
    w.stock(3)
    quote = declined(w, SimulatedProcessor())
    [item] = open_items(w, ITEM)
    assert item.key == f'shop:payment:{quote}' and not item.attest
    d = work.run(w.store, 'shop', work.Act('say-done', 'finish_work', 'staff:office_manager', item.key, None, 'I checked it'), NOW)
    assert not d.allowed and 'the record closes this one' in d.check
    assert len(open_items(w, ITEM)) == 1


def test_cancelling_an_unpaid_sale_releases_the_stock_and_closes_the_item():
    w = World()
    w.stock(2)
    quote = declined(w, SimulatedProcessor(), 2)
    assert w.state().available('lock') == 0
    assert not w.do('cancel_sale', quote, reason=' ').allowed
    assert len(open_items(w, ITEM)) == 1
    assert w.do('cancel_sale', quote, reason='The buyer left').allowed
    assert w.state().available('lock') == 2 and w.state().quantity('lock') == 2
    assert open_items(w, ITEM) == []
    assert not w.do('cancel_sale', quote, reason='Again').allowed
    assert w.do('retry_payment', quote).check == 'This sale was cancelled.'
    assert w.do('quote_sale', 'lock', quantity=2).allowed
    assert shop.differences(w.state()) == []


def test_a_paid_or_posted_sale_cannot_be_cancelled():
    w = World()
    w.stock(3)
    quote = w.do('quote_sale', 'lock', quantity=1)
    shop.pay(w.store, 'shop', SimulatedProcessor(), quote.made, 'card_ok', NOW)
    refused = w.do('cancel_sale', quote.made, reason='Changed my mind')
    assert not refused.allowed and 'was paid' in refused.check
    assert w.do('complete_sale', quote.made).allowed
    refused = w.do('cancel_sale', quote.made, reason='Changed my mind')
    assert not refused.allowed and 'returned or written off' in refused.check
    assert w.state().quantity('lock') == 2


def test_a_retry_that_succeeds_posts_the_sale_once():
    w = World()
    w.stock(3)
    processor = SimulatedProcessor()
    quote = declined(w, processor)
    assert not w.do('retry_payment', 'nothing').allowed
    assert w.do('retry_payment', quote).allowed
    assert not w.do('retry_payment', quote).allowed             # a retry waiting for its answer is not asked twice
    [waiting] = open_items(w, ITEM)                             # the money is not recovered yet: the same item stays, waiting
    assert waiting.key == f'shop:payment:{quote}' and waiting.waits and 'tried again' in waiting.why
    assert shop.pay(w.store, 'shop', processor, quote, 'card_ok', NOW)['status'] == 'succeeded'
    assert w.do('complete_sale', quote).allowed
    assert not w.do('complete_sale', quote).allowed
    assert 'already taken' in w.do('retry_payment', quote).check
    assert w.state().quantity('lock') == 2 and len([m for m in w.state().moves if m['kind'] == 'sold']) == 1
    assert [a['status'] for a in w.state().attempts[quote]] == ['failed']
    t = totals(w)
    assert t['income:merchandise'] == -1500 and t['cost:merchandise'] == 500 and t['clearing:card'] == 1500 and t[f'receivable:shop:{quote}'] == 0
    assert open_items(w, ITEM) == [] and shop.differences(w.state()) == []


def test_a_returned_payment_is_closed_by_a_new_payment_without_posting_the_sale_again():
    w = World()
    w.stock(3)
    processor = SimulatedProcessor()
    quote = sold_then_returned(w, processor)
    assert len(open_items(w, ITEM)) == 1
    assert w.do('retry_payment', quote).allowed
    assert shop.pay(w.store, 'shop', processor, quote, 'card_ok', NOW)['status'] == 'succeeded'
    assert open_items(w, ITEM) == []
    assert w.state().quantity('lock') == 2 and len([m for m in w.state().moves if m['kind'] == 'sold']) == 1
    t = totals(w)
    assert t['income:merchandise'] == -1500 and t['clearing:card'] == 1500 and t[f'receivable:shop:{quote}'] == 0
    assert shop.differences(w.state()) == []


def test_a_write_off_closes_a_returned_payment_and_shows_in_the_books():
    w = World()
    w.stock(3)
    quote = sold_then_returned(w, SimulatedProcessor())
    assert totals(w).get('loss:merchandise-write-off', 0) == 0 and totals(w)[f'receivable:shop:{quote}'] == 1500
    assert not w.do('write_off_payment', quote, reason='').allowed
    assert len(open_items(w, ITEM)) == 1
    assert w.do('write_off_payment', quote, reason='The buyer cannot be reached').allowed
    t = totals(w)
    assert t['loss:merchandise-write-off'] == 1500 and t[f'receivable:shop:{quote}'] == 0 and t['clearing:card'] == 0
    assert t['income:merchandise'] == -1500             # the sale stands; the loss is its own entry and the money is never assumed paid
    assert open_items(w, ITEM) == []
    assert not w.do('write_off_payment', quote, reason='Again').allowed
    assert not w.do('retry_payment', quote).allowed
    assert w.state().quantity('lock') == 2 and shop.differences(w.state()) == []


def test_nothing_is_written_off_that_was_paid_or_never_sold():
    w = World()
    w.stock(3)
    processor = SimulatedProcessor()
    unsold = declined(w, processor)
    assert 'Only a posted sale' in w.do('write_off_payment', unsold, reason='Gone').check
    paid = w.do('quote_sale', 'lock', quantity=1)
    shop.pay(w.store, 'shop', processor, paid.made, 'card_ok', NOW)
    assert w.do('complete_sale', paid.made).allowed
    assert 'nothing to write off' in w.do('write_off_payment', paid.made, reason='Gone').check
