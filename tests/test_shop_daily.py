from core import shop, work
from core.billing import Ledger
from tests.test_core_operations_set_shop import NOW, World


def test_daily_check_opens_one_dated_item_for_stock_and_payment_differences():
    from datetime import timedelta
    w = World()
    w.stock(3)
    with w.store.write():
        led = Ledger(w.store.books('shop'))
        led.emit('shop.move', NOW, dict(id='unmatched', product='lock', quantity=-1, kind='sold', reference='missing', reason='Import'), 'unmatched')
        led.emit('shop.sale', NOW, dict(quote='unpaid', product='lock', quantity=1, price=1500, cost=500), 'unpaid')
        w.store.append('shop', led.pending)
    shop.sweep(w.store, 'shop', NOW)
    shop.sweep(w.store, 'shop', NOW)
    b, rent, wk = work.load(w.store, 'shop')
    items = [i for i in work.queue(b, rent, wk, NOW) if i.source == 'shop' and i.kind == 'shop reconciliation']
    assert len(items) == 1
    assert items[0].due == NOW.date()
    assert 'unmatched' in items[0].why and 'unpaid' in items[0].why
    opened = [e for e in w.store.stream_events('shop') if e.kind == 'work.opened' and e.payload['key'] == items[0].key]
    assert len(opened) == 1
    shop.sweep(w.store, 'shop', NOW + timedelta(days=1))
    b, rent, wk = work.load(w.store, 'shop')
    items = [i for i in work.queue(b, rent, wk, NOW + timedelta(days=1)) if i.source == 'shop' and i.kind == 'shop reconciliation']
    assert len(items) == 1 and items[0].state not in work.CLOSED
    assert items[0].due == NOW.date()


def test_daily_check_closes_when_a_recorded_handover_is_posted():
    w = World()
    w.stock(2)
    handover = w.do('record_handover', product='lock', quantity=1, reason='Goodwill', approval='Manager', handover='Handed over')
    shop.sweep(w.store, 'shop', NOW)
    b, rent, wk = work.load(w.store, 'shop')
    items = [i for i in work.queue(b, rent, wk, NOW) if i.source == 'shop' and i.kind == 'shop reconciliation']
    assert len(items) == 1 and 'give-away of lock (quantity 1)' in items[0].why       # names what was handed over, in words, not a record number
    assert w.do('adjust_stock', 'lock', kind='give-away', quantity=-1, reason='Goodwill', handover=handover.made).allowed
    shop.sweep(w.store, 'shop', NOW)
    b, rent, wk = work.load(w.store, 'shop')
    items = [i for i in work.queue(b, rent, wk, NOW) if i.source == 'shop' and i.kind == 'shop reconciliation']
    assert len(items) == 1 and items[0].state in work.CLOSED


def test_daily_check_accepts_confirmed_sale_and_returned_payment():
    from core.delinquency import SimulatedProcessor
    w = World()
    w.stock(2)
    quote = w.do('quote_sale', 'lock', quantity=1)
    processor = SimulatedProcessor()
    payment = shop.pay(w.store, 'shop', processor, quote.made, 'card_ok', NOW)
    assert w.do('complete_sale', quote.made).allowed
    assert shop.differences(w.state()) == []
    processor.settle(payment['outside'], 'returned', NOW)
    assert shop.webhook(w.store, processor, processor.take_events()[-1], NOW) == 'applied'
    assert shop.differences(w.state()) == []


def test_processor_check_recovers_a_lost_shop_payment_answer():
    from core.delinquency import SimulatedProcessor
    w = World()
    w.stock(1)
    quote = w.do('quote_sale', 'lock', quantity=1)
    processor = SimulatedProcessor()
    processor.lose_next_reply = True
    assert shop.pay(w.store, 'shop', processor, quote.made, 'card_ok', NOW)['status'] == 'open'
    processor.take_events()
    shop.reconcile(w.store, 'shop', processor, NOW)
    assert w.state().payments[quote.made]['status'] == 'succeeded'
    assert w.do('complete_sale', quote.made).allowed
    assert w.state().quantity('lock') == 0


def test_processor_refusal_is_recorded_for_staff_without_claiming_a_payment():
    from core.delinquency import ProcessorRefused, SimulatedProcessor

    class RefusingProcessor(SimulatedProcessor):
        def create(self, key, cents, card, metadata, now):
            raise ProcessorRefused('request refused')

    w = World()
    w.stock(1)
    quote = w.do('quote_sale', 'lock', quantity=1)
    processor = RefusingProcessor()
    shop.pay(w.store, 'shop', processor, quote.made, 'card_ok', NOW)
    b, rent, wk = work.load(w.store, 'shop')
    assert any(i.source == 'shop' and i.ref == quote.made for i in work.queue(b, rent, wk, NOW))
    assert w.state().payments[quote.made]['status'] == 'open'
    assert not w.state().sales and w.state().quantity('lock') == 1


def test_stale_processing_does_not_reopen_a_failed_shop_payment():
    from core.delinquency import SimulatedProcessor
    w = World()
    w.stock(1)
    quote = w.do('quote_sale', 'lock', quantity=1)
    processor = SimulatedProcessor()
    payment = shop.pay(w.store, 'shop', processor, quote.made, 'card_declined', NOW)
    assert payment['status'] == 'failed'
    processor.settle(payment['outside'], 'processing', NOW)
    event = processor.take_events()[-1]
    assert shop.webhook(w.store, processor, event, NOW) == 'applied'
    assert w.state().payments[quote.made]['status'] == 'failed'


def test_returned_confirmed_payment_without_sale_still_needs_review():
    from core.delinquency import SimulatedProcessor
    w = World()
    w.stock(1)
    quote = w.do('quote_sale', 'lock', quantity=1)
    processor = SimulatedProcessor()
    payment = shop.pay(w.store, 'shop', processor, quote.made, 'card_ok', NOW)
    processor.settle(payment['outside'], 'returned', NOW)
    assert shop.webhook(w.store, processor, processor.take_events()[-1], NOW) == 'applied'
    assert f'Payment for {quote.made} has no sale posting' in shop.differences(w.state())


def test_daily_check_detects_duplicate_count_adjustments():
    w = World()
    w.stock(3)
    count = w.do('record_count', 'lock', quantity=2)
    assert w.do('review_count', count.made, reason='Checked shelf').allowed
    assert w.do('adjust_stock', 'lock', kind='count variance', quantity=-1, count=count.made, reason='Counted').allowed
    with w.store.write():
        led = Ledger(w.store.books('shop'))
        led.emit('shop.move', NOW, dict(id='duplicate-adjustment', product='lock', quantity=-1,
                                       kind='count variance', reference=count.made, reason='Import'), 'duplicate-adjustment')
        w.store.append('shop', led.pending)
    assert f'Count {count.made} has more than one stock adjustment' in shop.differences(w.state())
