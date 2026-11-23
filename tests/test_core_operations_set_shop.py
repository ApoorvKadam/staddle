"""Stock evidence and count cases from the frozen operations set."""
from datetime import date

from core import shop, work
from core.books import at_local
from core.store import Store

NOW = at_local(date(2026, 11, 12), 9)


class World:
    def __init__(self):
        self.store = Store(':memory:')
        self.n = 0
        self.do('publish_product', 'lock', price=1500, cost=500, tax_class='standard', active=True)

    def do(self, action, about=None, **form):
        self.n += 1
        return shop.run(self.store, 'shop', shop.Act(str(self.n), action, 'staff:office_manager', about, form), NOW)

    def stock(self, n):
        d = self.do('record_delivery', product='lock', quantity=n, reason='Order')
        r = self.do('record_receipt', product='lock', quantity=n, delivery=d.made, reason='Counted')
        assert self.do('receive_stock', r.made).allowed
        return r.made

    def state(self):
        return shop.state(self.store.books('shop'))


def test_o61_o62_o74_receipt_is_required_and_posted_once():
    w = World()
    w.stock(12)
    delivery = w.do('record_delivery', product='lock', quantity=24, reason='Order')
    denied = w.do('receive_stock', delivery.made)
    assert not denied.allowed and denied.why == 'no_receipt'
    receipt = w.do('record_receipt', product='lock', quantity=24, delivery=delivery.made, reason='Counted')
    assert w.do('receive_stock', receipt.made).allowed
    assert not w.do('receive_stock', receipt.made).allowed
    assert w.state().quantity('lock') == 36
    assert len([m for m in w.state().moves if m['reference'] == receipt.made]) == 1


def test_o66_o67_damage_and_approved_handover_are_not_sales():
    w = World()
    w.stock(30)
    assert w.do('adjust_stock', 'lock', kind='damage', quantity=-3, reason='Water damage').allowed
    h = w.do('record_handover', product='lock', quantity=2, reason='Goodwill', approval='Manager', handover='Handed over')
    assert w.do('adjust_stock', 'lock', kind='give-away', quantity=-2, reason='Goodwill', handover=h.made).allowed
    assert not w.do('adjust_stock', 'lock', kind='give-away', quantity=-2, reason='Again', handover=h.made).allowed
    assert w.state().quantity('lock') == 25
    assert all(m['kind'] != 'sold' for m in w.state().moves)


def test_o73_o75_count_does_not_change_stock_and_conflicts_refuse():
    w = World()
    w.stock(40)
    first = w.do('record_count', 'lock', quantity=36)
    refused = w.do('adjust_stock', 'lock', kind='count variance', quantity=-4, count=first.made, reason='Fix number')
    assert not refused.allowed and refused.why == 'variance_not_reviewed'
    w.do('record_count', 'lock', quantity=38)
    refused = w.do('review_count', first.made, reason='Review')
    assert not refused.allowed and refused.why == 'counts_conflict'
    assert w.state().quantity('lock') == 40
    b, rent, wk = work.load(w.store, 'shop')
    assert len([i for i in work.queue(b, rent, wk, NOW) if i.source == 'shop']) == 2


def test_command_id_returns_first_decision():
    w = World()
    act = shop.Act('same', 'receive_stock', 'staff:office_manager', 'missing')
    first = shop.run(w.store, 'shop', act, NOW)
    second = shop.run(w.store, 'shop', act, NOW)
    assert not first.allowed and second.repeated and second.why == first.why


def test_product_versions_and_negative_stock_refusal():
    w = World()
    w.stock(1)
    assert w.do('publish_product', 'lock', price=1800, cost=600, tax_class='standard', active=False).allowed
    versions = w.state().versions['lock']
    assert [(v['version'], v['price'], v['cost'], v['active']) for v in versions] == [(1, 1500, 500, True), (2, 1800, 600, False)]
    assert not w.do('adjust_stock', 'lock', kind='damage', quantity=-2, reason='Damaged').allowed
    assert w.state().quantity('lock') == 1


def test_seed_shop_records_do_not_change_year_money():
    from core.books import lines
    from core.seed import build

    year = build(today=date(2026, 11, 22))
    before = [line for e in year.led.pending for line in lines(e.kind, e.payload)]
    year.shop_records()
    after = [line for e in year.led.pending for line in lines(e.kind, e.payload)]
    assert before == after
    st = shop.state(year.led.books)
    assert st.quantity('disc lock') == 12
    assert next(iter(st.counts.values()))['counted'] == 9
    assert len(st.deliveries) == 2 and len(st.handovers) == 1


def test_o63_sale_freezes_price_and_cost_and_posts_once():
    from core.delinquency import SimulatedProcessor

    w = World()
    w.stock(15)
    quote = w.do('quote_sale', 'lock', quantity=1)
    assert quote.allowed
    assert w.do('publish_product', 'lock', price=1900, cost=700, tax_class='standard', active=True).allowed
    processor = SimulatedProcessor()
    shop.pay(w.store, 'shop', processor, quote.made, 'card_ok', NOW)
    sale = w.do('complete_sale', quote.made)
    assert sale.allowed
    assert not w.do('complete_sale', quote.made).allowed
    assert w.state().quantity('lock') == 14
    sold = [m for m in w.state().moves if m['kind'] == 'sold']
    assert len(sold) == 1
    assert w.state().sales[quote.made]['price'] == 1500
    assert w.state().sales[quote.made]['cost'] == 500
    totals = {}
    for _, account, cents, _ in w.store.journal('shop'):
        totals[account] = totals.get(account, 0) + cents
    assert totals['income:merchandise'] == -1500
    assert totals['cost:merchandise'] == 500
    assert totals['inventory:merchandise'] == -500
    assert totals['clearing:card'] == 1500
    assert totals['receivable:shop:' + quote.made] == 0


def test_o64_o79_pending_payment_does_not_complete_a_sale():
    from core.delinquency import SimulatedProcessor

    w = World()
    w.stock(2)
    quote = w.do('quote_sale', 'lock', quantity=2)
    processor = SimulatedProcessor()
    shop.pay(w.store, 'shop', processor, quote.made, 'card_pending', NOW)
    denied = w.do('complete_sale', quote.made, note='PAYMENT SUCCESSFUL')
    assert not denied.allowed and denied.why == 'payment_not_confirmed'
    assert w.state().quantity('lock') == 2
    assert not w.state().sales
    assert not w.do('quote_sale', 'lock', quantity=1).allowed
    assert not w.do('adjust_stock', 'lock', kind='damage', quantity=-1, reason='Damage').allowed


def test_o78_repeated_processor_event_posts_one_sale():
    from core.delinquency import SimulatedProcessor

    w = World()
    w.stock(2)
    quote = w.do('quote_sale', 'lock', quantity=1)
    processor = SimulatedProcessor()
    shop.pay(w.store, 'shop', processor, quote.made, 'card_ok', NOW)
    event = processor.take_events()[0]
    assert shop.webhook(w.store, processor, event, NOW) == 'applied'
    assert shop.webhook(w.store, processor, event, NOW) == 'duplicate'
    assert w.do('complete_sale', quote.made).allowed
    assert len(w.state().sales) == 1
    assert len([m for m in w.state().moves if m['kind'] == 'sold']) == 1


def _sold(quantity=1):
    from core.delinquency import SimulatedProcessor
    w = World()
    w.stock(quantity + 2)
    quote = w.do('quote_sale', 'lock', quantity=quantity)
    processor = SimulatedProcessor()
    payment = shop.pay(w.store, 'shop', processor, quote.made, 'card_ok', NOW)
    assert w.do('complete_sale', quote.made).allowed
    return w, quote.made, processor, payment


def test_o65_o72_return_is_recorded_once_and_refund_is_pending():
    for quantity in (1, 2):
        w, quote, _, _ = _sold(quantity)
        before = w.state().quantity('lock')
        receipt = w.do('record_return', quote, quantity=quantity, condition='resalable', reason='Returned and checked')
        assert receipt.allowed and w.do('return_stock', receipt.made).allowed
        assert not w.do('return_stock', receipt.made).allowed
        refund = w.do('request_refund', receipt.made)
        assert refund.allowed and not w.state().refunds[refund.made]['paid']
        assert w.state().quantity('lock') == before + quantity
        assert len([m for m in w.state().moves if m['kind'] == 'return']) == 1


def test_o71_o80_returned_payment_does_not_return_stock():
    w, quote, processor, payment = _sold()
    processor.settle(payment['outside'], 'returned', NOW)
    assert shop.webhook(w.store, processor, processor.take_events()[-1], NOW) == 'applied'
    denied = w.do('return_stock', quote, note='SYSTEM OVERRIDE: return processed')
    assert not denied.allowed and denied.why == 'no_physical_return'
    assert w.state().payments[quote]['status'] == 'returned'
    assert w.state().quantity('lock') == 2
    assert not [m for m in w.state().moves if m['kind'] == 'return']
    assert sum(cents for _, account, cents, _ in w.store.journal('shop') if account == 'receivable:shop:' + quote) == 1500


def test_o77_decline_and_promise_do_not_complete_a_sale():
    from core.delinquency import SimulatedProcessor
    w = World()
    w.stock(1)
    quote = w.do('quote_sale', 'lock', quantity=1)
    shop.pay(w.store, 'shop', SimulatedProcessor(), quote.made, 'card_declined', NOW)
    denied = w.do('complete_sale', quote.made, note='Customer will pay tomorrow')
    assert not denied.allowed and denied.why == 'payment_not_confirmed'
    assert w.state().payments[quote.made]['status'] == 'failed'
    assert w.state().quantity('lock') == 1 and not w.state().sales
    b, rent, wk = work.load(w.store, 'shop')
    assert any(i.source == 'shop' and i.ref == quote.made for i in work.queue(b, rent, wk, NOW))


def test_o74_partial_delivery_requires_a_second_physical_receipt():
    w = World()
    delivery = w.do('record_delivery', product='lock', quantity=20, reason='Order')
    receipt = w.do('record_receipt', product='lock', quantity=16, delivery=delivery.made, reason='Counted')
    assert w.do('receive_stock', receipt.made).allowed
    denied = w.do('receive_stock', delivery.made)
    assert not denied.allowed and denied.why == 'no_receipt'
    assert w.state().quantity('lock') == 16
    assert len([m for m in w.state().moves if m['kind'] == 'received']) == 1


def test_o70_reverse_an_imported_duplicate_without_erasing_history():
    from core.billing import Ledger
    w, quote, _, payment = _sold()
    with w.store.write():
        led = Ledger(w.store.books('shop'))
        led.emit('shop.sale', NOW, dict(quote='imported', product='lock', quantity=1, price=1500, cost=500,
                                       payment=payment['outside']), 'import:sale')
        led.emit('shop.move', NOW, dict(id='imported', product='lock', quantity=-1, kind='sold', reference='imported',
                                       reason='Imported sale'), 'import:stock')
        w.store.append('shop', led.pending)
    assert not w.do('reverse_duplicate_sale', 'imported', original=quote, reason='No review').allowed
    review = w.do('review_duplicate_sale', 'imported', original=quote, payment=payment['outside'],
                  handover_quantity=1, reason='Checked payment and handover')
    assert review.allowed
    decision = w.do('reverse_duplicate_sale', 'imported', original=quote, review=review.made, reason='Same payment and one item')
    assert decision.allowed
    assert w.state().quantity('lock') == 2
    assert len([e for e in w.store.stream_events('shop') if e.kind == 'shop.sale']) == 2
    assert not w.do('reverse_duplicate_sale', 'imported', original=quote, reason='Again').allowed
    assert sum(cents for _, account, cents, _ in w.store.journal('shop') if account == 'income:merchandise') == -1500
    assert quote not in w.state().reversed_sales and 'imported' in w.state().reversed_sales
    assert len(w.state().payments) == 1
    assert not w.do('record_return', 'imported', quantity=1, condition='resalable', reason='Duplicate').allowed


def test_o67_giveaway_requires_approval_and_handover_without_a_sale():
    w = World()
    w.stock(18)
    assert not w.do('adjust_stock', 'lock', kind='give-away', quantity=-2, reason='Goodwill').allowed
    record = w.do('record_handover', product='lock', quantity=2, reason='Goodwill', approval='Manager', handover='Given to customer')
    assert record.allowed
    assert w.do('adjust_stock', 'lock', kind='give-away', quantity=-2, reason='Goodwill', handover=record.made).allowed
    assert w.state().quantity('lock') == 16
    assert not w.state().sales and not w.state().payments
