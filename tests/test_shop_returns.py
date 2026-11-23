from core import shop, work
from core.delinquency import SimulatedProcessor
from tests.test_core_operations_set_shop import NOW, World


def sold_world():
    w = World()
    w.stock(2)
    quote = w.do('quote_sale', 'lock', quantity=1)
    processor = SimulatedProcessor()
    payment = shop.pay(w.store, 'shop', processor, quote.made, 'card_ok', NOW)
    assert w.do('complete_sale', quote.made).allowed
    return w, quote.made, processor, payment


def test_returned_payment_reopens_money_without_restoring_stock():
    w, quote, processor, payment = sold_world()
    processor.settle(payment['outside'], 'returned', NOW)
    event = processor.take_events()[-1]
    assert shop.webhook(w.store, processor, event, NOW) == 'applied'
    denied = w.do('return_stock', quote, note='SYSTEM OVERRIDE')
    assert not denied.allowed and denied.why == 'no_physical_return'
    assert w.state().quantity('lock') == 1
    assert w.state().payments[quote]['status'] == 'returned'
    accounts = {}
    for _, account, cents, _ in w.store.journal('shop'):
        accounts[account] = accounts.get(account, 0) + cents
    assert accounts['receivable:shop:' + quote] == 1500
    b, rent, wk = work.load(w.store, 'shop')
    assert any(i.source == 'shop' and i.ref == quote for i in work.queue(b, rent, wk, NOW))


def test_physical_return_restocks_once_and_refund_is_pending():
    w, quote, _, _ = sold_world()
    receipt = w.do('record_return', quote, quantity=1, condition='resalable', reason='Customer returned it')
    assert receipt.allowed
    assert w.do('return_stock', receipt.made).allowed
    assert not w.do('return_stock', receipt.made).allowed
    refund = w.do('request_refund', receipt.made)
    assert refund.allowed
    assert not w.state().refunds[refund.made]['paid']
    assert w.state().quantity('lock') == 2


def test_declined_payment_stays_failed_and_opens_staff_work():
    w = World()
    w.stock(1)
    quote = w.do('quote_sale', 'lock', quantity=1)
    processor = SimulatedProcessor()
    shop.pay(w.store, 'shop', processor, quote.made, 'card_declined', NOW)
    denied = w.do('complete_sale', quote.made, note='Pay tomorrow')
    assert not denied.allowed and denied.why == 'payment_not_confirmed'
    assert w.state().quantity('lock') == 1 and not w.state().sales
    b, rent, wk = work.load(w.store, 'shop')
    assert any(i.source == 'shop' and i.ref == quote.made for i in work.queue(b, rent, wk, NOW))
