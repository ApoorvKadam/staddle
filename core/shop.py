"""Shop products, physical stock evidence and counts, folded from the copy's events.

Quantities are whole items. A count is an observation, not a stock movement.
"""
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from .billing import Ledger
from .books import Books, local
from .evidence import money
from .gate import Decision, STAFF_ROLES
from .store import Store

# every staff step the shop's command run takes; Today leaves them to the Shop page, which keeps their history
ACTIONS = frozenset({'adjust_stock', 'cancel_sale', 'complete_sale', 'publish_product', 'quote_sale', 'receive_stock', 'record_count',
                     'record_delivery', 'record_handover', 'record_receipt', 'record_return', 'refund_payment', 'request_refund',
                     'resume_payment', 'retry_payment', 'return_stock', 'reverse_duplicate_sale', 'review_count', 'review_duplicate_sale',
                     'write_off_payment'})

PAYMENT_MOVES = {'open': ('processing', 'succeeded', 'failed', 'returned'),
                 'processing': ('succeeded', 'failed', 'returned'), 'failed': ('succeeded', 'returned'),
                 'succeeded': ('returned',), 'returned': ()}


@dataclass
class State:
    versions: dict = field(default_factory=dict)
    deliveries: dict = field(default_factory=dict)
    receipts: dict = field(default_factory=dict)
    handovers: dict = field(default_factory=dict)
    moves: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    quotes: dict = field(default_factory=dict)
    payments: dict = field(default_factory=dict)
    sales: dict = field(default_factory=dict)
    webhooks: set = field(default_factory=set)
    returns: dict = field(default_factory=dict)
    refunds: dict = field(default_factory=dict)
    attachments: dict = field(default_factory=dict)
    reversed_sales: set = field(default_factory=set)
    corrections: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    duplicate_reviews: dict = field(default_factory=dict)
    cancelled: dict = field(default_factory=dict)
    write_offs: dict = field(default_factory=dict)
    attempts: dict = field(default_factory=dict)

    def apply(self, kind, at, p):
        p = dict(p, at=at)
        if kind == 'shop.product':
            self.versions.setdefault(p['product'], []).append(p)
        elif kind == 'shop.delivery':
            self.deliveries[p['id']] = p
        elif kind == 'shop.receipt':
            self.receipts[p['id']] = p
        elif kind == 'shop.handover':
            self.handovers[p['id']] = p
        elif kind == 'shop.move':
            self.moves.append(p)
        elif kind == 'shop.count':
            self.counts[p['id']] = p
        elif kind == 'shop.count_reviewed':
            self.counts[p['count']]['review'] = dict(p, seq=len(self.counts))        # counts recorded from here on are the ones a recount waits for
        elif kind == 'shop.quote':
            self.quotes[p['id']] = p
        elif kind == 'shop.payment_requested':
            self.payments[p['quote']] = p
        elif kind == 'shop.payment_answered':
            self.payments[p['quote']].update(p)
        elif kind == 'shop.payment_problem':
            self.payments[p['quote']]['problem'] = True
        elif kind == 'shop.sale':
            self.sales[p['quote']] = p
        elif kind == 'shop.webhook':
            self.webhooks.add(p['event'])
        elif kind == 'shop.return':
            self.returns[p['id']] = p
        elif kind == 'shop.refund':
            self.refunds[p['id']] = p
        elif kind == 'shop.checkout_attached':
            self.attachments[p['quote']] = p['rental']
        elif kind == 'shop.sale_reversed':
            self.reversed_sales.add(p['quote'])
            self.corrections.append(p)
        elif kind == 'shop.checked':
            self.checks.append(p)
        elif kind == 'shop.duplicate_review':
            self.duplicate_reviews[p['id']] = p
        elif kind == 'shop.quote_cancelled':
            self.cancelled[p['quote']] = p
        elif kind == 'shop.write_off':
            self.write_offs[p['quote']] = p
        elif kind == 'shop.payment_retried':
            old = self.payments[p['quote']]
            self.attempts.setdefault(p['quote'], []).append(old)       # the attempt that did not come in stays on record
            self.payments[p['quote']] = dict(quote=p['quote'], key=p['key'], cents=old['cents'], status='open', at=p['at'], attempt=len(self.attempts[p['quote']]) + 1, card=p.get('card'))
        elif kind == 'shop.attempt_answered':           # the processor's later word on an attempt a newer one replaced
            for a in self.attempts.get(p['quote'], []):
                if a['key'] == p['key']:
                    a.update(status=p['status'], previous=p['previous'], outside=p['outside'])
        elif kind == 'shop.refund_cancelled':
            self.refunds[p['refund']]['cancelled'] = True

    def every_attempt(self, quote):
        """Each payment attempt on a sale, the first one first and the current one last."""
        return self.attempts.get(quote, []) + ([self.payments[quote]] if quote in self.payments else [])

    def can_close(self, quote):
        """Whether the processor's own answer says the sale's last attempt can no longer succeed: it was declined or returned, or the
        processor confirmed it holds no payment for the attempt's key. An attempt still open or processing can come in, so it is neither
        tried again nor cancelled."""
        payment = self.payments.get(quote) or {}
        return payment.get('status') in ('failed', 'returned') or (payment.get('status') == 'open' and bool(payment.get('problem')))

    def refunds_owed(self):
        """Money the processor took that no sale holds: a payment that came in after its sale was cancelled or written off, or on an attempt
        a newer one had replaced. Each is owed back until a refund record names it; a staff word never closes it."""
        out = []
        for quote, current in self.payments.items():
            for a in self.every_attempt(quote):
                if a['status'] == 'succeeded' and (a is not current or quote in self.cancelled or quote in self.write_offs) \
                        and not any(r.get('unmatched') and r.get('key') == a['key'] and not r.get('cancelled') for r in self.refunds.values()):
                    out.append((quote, a))
        return out

    def quantity(self, product):
        return (sum(m['quantity'] for m in self.moves if m['product'] == product)
                + sum(c['quantity'] for c in self.corrections if c['product'] == product))

    def available(self, product):
        reserved = sum(q['quantity'] for q in self.quotes.values() if q['product'] == product and q['id'] not in self.sales and q['id'] not in self.cancelled)
        return self.quantity(product) - reserved

    def superseded(self, count_id):
        """A count staff asked to have redone and a newer count of the same product has since been recorded: the newer one stands in for it."""
        c = self.counts[count_id]
        if not (c.get('review') or {}).get('recount'):
            return False
        ids = list(self.counts)
        return any(self.counts[i]['product'] == c['product'] for i in ids[max(c['review']['seq'], ids.index(count_id) + 1):])

    def in_conflict(self, count):
        """Another count of the product with no stock movement between the two disagrees with this one, and no recount stands in for it."""
        return any(c['product'] == count['product'] and c['move_cutoff'] == count['move_cutoff'] and c['counted'] != count['counted']
                   and not self.superseded(c['id']) for c in self.counts.values())

    def count_variance_refusal(self, product, quantity, count, reviewed=False):
        """Why a count's difference cannot be posted to stock now, as (why, words), or None when the core would take it: the one check the
        gate and the Shop page both use. `reviewed` asks it as if the count had just been reviewed."""
        review = (count or {}).get('review') or ({'reason': 'review'} if reviewed and count else None)
        if count and self.in_conflict(count):
            return 'counts_conflict', 'Two counts with no movement between them disagree; a recount is needed.'
        if count and (review or {}).get('recount'):
            return 'recount_asked', 'A recount was asked for; the new count is reviewed instead of this one.'
        if not count or count['product'] != product or not review or count['variance'] != quantity or count['move_cutoff'] != self.stock_revision(product):
            return 'variance_not_reviewed', 'The count difference has not been reviewed against the current stock.'
        if self.available(product) + quantity < 0:
            return None, 'The adjustment would make stock negative.'
        return None

    def stock_revision(self, product):
        return sum(m['product'] == product for m in self.moves) + sum(c['product'] == product for c in self.corrections)


def state(books: Books) -> State:
    return books.shop or State()


@dataclass
class Act:
    id: str
    action: str
    source: str
    about: str | None = None
    form: dict | None = None


def _rental_payment_open(led: Ledger, payment: dict | None) -> bool:
    """Whether the move-in payment an add-on rides on is still with the processor: its intent is open or processing, so it can still settle."""
    from . import delinquency
    intent = delinquency.state(led.books).intents.get((payment or {}).get('intent') or '')
    return bool(intent) and intent.state in delinquency.UNSETTLED


def post_sale(led: Ledger, quote_id: str, quote: dict, now: datetime, source: str, command: str | None = None, move: str | None = None) -> None:
    """Post a confirmed sale and take its stock out: the one step every way of confirming a payment ends in."""
    led.emit('shop.sale', now, dict(quote=quote_id, product=quote['product'], quantity=quote['quantity'], price=quote['price'], cost=quote['cost']),
             'shop:sale:' + quote_id, source=source, command=command)
    led.emit('shop.move', now, dict(id=move or 'sold:' + quote_id, product=quote['product'], quantity=-quote['quantity'], kind='sold', reference=quote_id,
                                    reason='Confirmed sale'), 'shop:sold:' + quote_id, source=source, command=command)


def command(led: Ledger, act: Act, now: datetime) -> Decision:
    if act.id in led.books.decisions:
        p = led.books.decisions[act.id]
        return Decision(p['allowed'], p['action'], p['check'], command=act.id, repeated=True, made=p.get('made'), why=p.get('why'))
    st, f = state(led.books), act.form or {}
    ok, why, check, made = False, None, 'That step is not available.', None
    event, payload = None, None
    if not isinstance(act.source, str) or not act.source.startswith('staff:') or act.source[6:] not in STAFF_ROLES:
        check = 'Only staff may make this shop record.'
    elif any(r is not None and not isinstance(r, str) for r in (act.about, f.get('product'), f.get('delivery'), f.get('count'), f.get('handover'),
                                                               f.get('original'), f.get('review'), f.get('key'))):
        check = 'Name each product and record in words.'
    elif act.action == 'publish_product':
        product = act.about
        if (isinstance(product, str) and product.strip() and type(f.get('price')) is int and f['price'] >= 0
                and type(f.get('cost')) is int and f['cost'] >= 0 and isinstance(f.get('tax_class'), str)
                and f['tax_class'].strip() and type(f.get('active')) is bool):
            event, made = 'product', product
            payload = dict(product=product, version=len(st.versions.get(product, [])) + 1, price=f['price'], cost=f['cost'], tax_class=f['tax_class'], active=f['active'])
        else:
            check = 'Give a product, price, cost, tax class and whether it is active.'
    elif act.action in ('record_delivery', 'record_receipt', 'record_handover'):
        product, quantity = f.get('product'), f.get('quantity')
        if product not in st.versions or type(quantity) is not int or quantity <= 0 or not isinstance(f.get('reason'), str) or not f['reason'].strip():
            check = 'Give an existing product, a positive quantity and a reason.'
        elif act.action == 'record_receipt' and (f.get('delivery') not in st.deliveries or st.deliveries[f['delivery']]['product'] != product
                or sum(r['quantity'] for r in st.receipts.values() if r['delivery'] == f['delivery']) + quantity > st.deliveries[f['delivery']]['quantity']):
            check = 'The physical receipt must match the delivery and its remaining quantity.'
        elif act.action == 'record_handover' and (not f.get('approval') or not f.get('handover')):
            check = 'Record the approval and the physical handover.'
        else:
            event = {'record_delivery': 'delivery', 'record_receipt': 'receipt', 'record_handover': 'handover'}[act.action]
            made = act.id
            payload = dict(f, id=made)
    elif act.action == 'receive_stock':
        receipt = st.receipts.get(act.about)
        if not receipt:
            why, check = 'no_receipt', 'The stock has not been physically received and recorded against a delivery.'
        elif any(m.get('reference') == act.about and m['kind'] == 'received' for m in st.moves):
            check = 'This receipt has already been posted.'
        else:
            event, made = 'move', act.id
            payload = dict(id=made, product=receipt['product'], quantity=receipt['quantity'], kind='received', reference=act.about, reason=receipt['reason'])
    elif act.action == 'record_count':
        product, quantity = act.about, f.get('quantity')
        if product not in st.versions or type(quantity) is not int or quantity < 0:
            check = 'Give an existing product and a nonnegative count.'
        else:
            event, made = 'count', act.id
            payload = dict(id=made, product=product, book=st.quantity(product), counted=quantity, variance=quantity-st.quantity(product), move_cutoff=st.stock_revision(product), linked_moves=[], review=None)
    elif act.action == 'quote_sale':
        product, quantity = act.about, f.get('quantity')
        versions = st.versions.get(product, [])
        if not versions or not versions[-1]['active']:
            check = 'Choose an active product.'
        elif type(quantity) is not int or quantity <= 0 or quantity > st.available(product):
            check = 'Choose a positive quantity available in stock.'
        elif 'price' in f and type(f['price']) is not int:
            why, check = 'price_not_shown', 'Reload the page to see the current price, then sell again.'
        elif 'price' in f and f['price'] != versions[-1]['price']:
            why, check = 'price_changed', (f"The price of {product} is now {money(versions[-1]['price'])}, not {money(f['price'])} as the page showed. "
                                          'Check the new price with the buyer, then sell again.')
        elif versions[-1]['price'] * quantity < 50:
            check = 'The payment must be at least 50 cents.'
        else:
            v = versions[-1]
            event, made = 'quote', act.id
            payload = dict(id=made, product=product, quantity=quantity, version=v['version'], price=v['price'], cost=v['cost'], tax_class=v['tax_class'])
    elif act.action == 'complete_sale':
        quote, payment = st.quotes.get(act.about), st.payments.get(act.about)
        if not quote or not payment or payment.get('status') != 'succeeded' or act.about in st.cancelled:
            why, check = 'payment_not_confirmed', 'The payment has not been confirmed.'
        elif act.about in st.sales:
            check = 'This sale has already been posted.'
        else:
            post_sale(led, act.about, quote, now, act.source, act.id, act.id)
            ok, check, made = True, 'The sale was posted.', act.about
    elif act.action == 'retry_payment':
        quote, payment = st.quotes.get(act.about), st.payments.get(act.about)
        status = (payment or {}).get('status')
        if not quote or not payment:
            check = 'There is no payment on this sale to try again.'
        elif act.about in st.cancelled:
            check = 'This sale was cancelled.'
        elif act.about in st.write_offs:
            check = 'This payment was written off.'
        elif act.about in st.attachments:
            check = 'This payment belongs to a move-in checkout; cancel the add-on or pay from the rental.'
        elif status == 'succeeded':
            check = 'This payment was already taken.'
        elif not st.can_close(act.about):
            check = 'The card processor has not answered the last payment yet.'
        else:
            event, made = 'payment_retried', act.about
            payload = dict(quote=act.about, key=f'shop:retry:{act.id}', previous=status, card=f.get('card') if isinstance(f.get('card'), str) else None)
    elif act.action == 'cancel_sale':
        quote, payment = st.quotes.get(act.about), st.payments.get(act.about)
        status = (payment or {}).get('status')
        if not quote:
            check = 'There is no such sale to cancel.'
        elif act.about in st.cancelled:
            check = 'This sale was already cancelled.'
        elif act.about in st.sales:
            check = 'A posted sale is returned or written off, not cancelled.'
        elif status == 'succeeded' or (payment and not st.can_close(act.about)):
            check = 'This sale was paid or the card processor has not answered yet, so it cannot be cancelled.'
        elif act.about in st.attachments and _rental_payment_open(led, payment):
            check = 'The rental payment this add-on is part of can still go through, so the add-on cannot be cancelled yet.'
        elif not isinstance(f.get('reason'), str) or not f['reason'].strip():
            check = 'Say why the sale is cancelled.'
        else:
            event, made = 'quote_cancelled', act.about
            payload = dict(quote=act.about, product=quote['product'], quantity=quote['quantity'], reason=f['reason'], by=act.source)
    elif act.action == 'resume_payment':
        quote, payment = st.quotes.get(act.about), st.payments.get(act.about)
        if not quote or not payment or act.about in st.cancelled or act.about in st.attachments or payment['status'] not in ('open', 'processing') or payment.get('problem'):
            check = 'There is no payment waiting for the card processor on this sale.'
        elif f.get('key') != payment['key']:
            why, check = 'attempt_replaced', 'This page showed an older payment attempt than the one now on the sale. Reload the page and look again.'
        else:
            ok, check, made = True, 'The payment is being asked about again.', payment['key']      # the form is bound to this attempt and its key
    elif act.action == 'refund_payment':
        owed = {(q, a['key']): a for q, a in st.refunds_owed()}.get((act.about, f.get('key')))
        if owed is None:
            check = 'There is no payment owed back on that sale.'
        elif not isinstance(f.get('reason'), str) or not f['reason'].strip():
            check = 'Say why the money is refunded.'
        else:
            event, made = 'refund', act.id
            payload = dict(id=made, return_id=None, quote=act.about, cents=owed['cents'], paid=False, unmatched=True, key=owed['key'], reason=f['reason'], by=act.source)
    elif act.action == 'write_off_payment':
        sale, payment = st.sales.get(act.about), st.payments.get(act.about)
        if not sale or not payment or act.about in st.reversed_sales:
            check = 'Only a posted sale whose payment did not come in can be written off.'
        elif act.about in st.write_offs:
            check = 'This payment was already written off.'
        elif payment['status'] not in ('returned', 'failed'):
            check = 'The payment came in or is still with the card processor, so there is nothing to write off.'
        elif not isinstance(f.get('reason'), str) or not f['reason'].strip():
            check = 'Say why the money is written off.'
        else:
            event, made = 'write_off', act.about
            payload = dict(quote=act.about, cents=payment['cents'], reason=f['reason'], by=act.source)
    elif act.action == 'record_return':
        sale = st.sales.get(act.about)
        quantity = f.get('quantity')
        already = sum(r['quantity'] for r in st.returns.values() if r['quote'] == act.about)
        if (not sale or act.about in st.reversed_sales or type(quantity) is not int or quantity <= 0 or already + quantity > sale['quantity']
                or f.get('condition') != 'resalable' or not isinstance(f.get('reason'), str) or not f['reason'].strip()):
            check = 'Record a resalable physical return against a sale, with its quantity and reason.'
        else:
            event, made = 'return', act.id
            payload = dict(id=made, quote=act.about, product=sale['product'], quantity=quantity, condition=f['condition'], reason=f['reason'])
    elif act.action == 'return_stock':
        receipt = st.returns.get(act.about)
        if not receipt:
            why, check = 'no_physical_return', 'There is no recorded physical return to put back in stock.'
        elif any(m['kind'] == 'return' and m.get('reference') == act.about for m in st.moves):
            check = 'This return has already been posted.'
        else:
            sale = st.sales[receipt['quote']]
            led.emit('shop.return_posted', now, dict(return_id=act.about, quantity=receipt['quantity'], cost=sale['cost']),
                     'shop:return-posted:' + act.about, source=act.source, command=act.id)
            event, made = 'move', act.id
            payload = dict(id=made, product=receipt['product'], quantity=receipt['quantity'], kind='return', reference=act.about, reason=receipt['reason'])
    elif act.action == 'request_refund':
        receipt = st.returns.get(act.about)
        sale = st.sales.get(receipt['quote']) if receipt else None
        payment = st.payments.get(receipt['quote']) if receipt else None
        if not receipt or not sale or not payment or payment['status'] != 'succeeded':
            check = 'A refund needs a return and a payment that has not come back.'
        elif any(r['return_id'] == act.about and not r.get('cancelled') for r in st.refunds.values()):
            check = 'A refund for this return is already recorded.'
        else:
            event, made = 'refund', act.id
            payload = dict(id=made, return_id=act.about, quote=receipt['quote'], cents=sale['price'] * receipt['quantity'], paid=False, key=payment['key'])
    elif act.action == 'review_duplicate_sale':
        duplicate, original = st.sales.get(act.about), st.sales.get(f.get('original'))
        payment = st.payments.get(f.get('original'))
        if (not duplicate or not original or act.about == f.get('original') or act.about in st.reversed_sales
                or not payment or payment.get('status') not in ('succeeded', 'returned') or not payment.get('outside')
                or duplicate.get('payment') != payment['outside'] or f.get('payment') != payment['outside']
                or type(original.get('quantity')) is not int or original['quantity'] <= 0
                or type(f.get('handover_quantity')) is not int or f['handover_quantity'] != original['quantity']
                or not isinstance(f.get('reason'), str) or not f['reason'].strip()):
            check = 'Check the original payment and the quantity handed over before reviewing a duplicate.'
        else:
            event, made = 'duplicate_review', act.id
            payload = dict(id=made, quote=act.about, original=f['original'], payment=f['payment'],
                           handover_quantity=f['handover_quantity'], reason=f['reason'], by=act.source)
    elif act.action == 'reverse_duplicate_sale':
        duplicate, original = st.sales.get(act.about), st.sales.get(f.get('original'))
        original_payment = st.payments.get(f.get('original'))
        review = st.duplicate_reviews.get(f.get('review'))
        moves = [m for m in st.moves if m['kind'] == 'sold' and m.get('reference') == act.about]
        if (not duplicate or not original or act.about == f.get('original') or not original_payment
                or original_payment['status'] not in ('succeeded', 'returned') or not original_payment.get('outside')
                or not review or review['quote'] != act.about or review['original'] != f.get('original')
                or review['payment'] != original_payment['outside'] or review['handover_quantity'] != original['quantity']
                or duplicate.get('payment') != original_payment['outside'] or act.about in st.reversed_sales
                or f.get('original') in st.reversed_sales or len(moves) != 1
                or any(r['quote'] == act.about for r in st.returns.values())
                or not isinstance(duplicate.get('product'), str) or type(duplicate.get('quantity')) is not int
                or duplicate['quantity'] <= 0 or type(duplicate.get('price')) is not int or type(duplicate.get('cost')) is not int
                or any(duplicate.get(k) != original.get(k) for k in ('product', 'quantity', 'price', 'cost'))
                or moves[0]['product'] != duplicate['product'] or moves[0]['quantity'] != -duplicate['quantity']
                or not isinstance(f.get('reason'), str) or not f['reason'].strip()):
            check = 'The duplicate sale and its matching payment and stock movement must be checked first.'
        else:
            led.emit('shop.sale_reversed', now, dict(quote=act.about, original=f['original'], product=duplicate['product'], quantity=duplicate['quantity'],
                     price=duplicate['price'], cost=duplicate['cost'], reason=f['reason']),
                     'shop:reverse:' + act.about, source=act.source, command=act.id)
            ok, check, made = True, 'The duplicate sale was reversed.', act.about
    elif act.action == 'review_count':
        count = st.counts.get(act.about)
        if not count or not isinstance(f.get('reason'), str) or not f['reason'].strip():
            check = 'Give an existing count and the review findings.'
        elif f.get('recount') is not True and st.in_conflict(count):
            why, check = 'counts_conflict', 'Two counts with no movement between them disagree; a recount is needed.'
        else:
            event, made = 'count_reviewed', act.about
            payload = dict(count=act.about, reason=f['reason'], by=act.source, recount=f.get('recount') is True)
    elif act.action == 'adjust_stock':
        product, quantity, kind, reason = act.about, f.get('quantity'), f.get('kind'), f.get('reason')
        count = st.counts.get(f.get('count'))
        if product not in st.versions or type(quantity) is not int or quantity == 0 or not isinstance(reason, str) or not reason.strip():
            check = 'Give an existing product, quantity and reason.'
        elif kind == 'count variance' and (refusal := st.count_variance_refusal(product, quantity, count)) and refusal[0]:
            why, check = refusal
        elif kind == 'give-away' and (f.get('handover') not in st.handovers or st.handovers[f['handover']]['product'] != product or st.handovers[f['handover']]['quantity'] != -quantity or any(m.get('reference') == f['handover'] for m in st.moves)):
            check = 'The approval and physical handover must be recorded once.'
        elif kind not in ('damage', 'give-away', 'count variance') or (kind != 'count variance' and quantity >= 0):
            check = 'Choose a supported stock adjustment.'
        elif st.available(product) + quantity < 0:
            check = 'The adjustment would make stock negative.'
        else:
            event, made = 'move', act.id
            payload = dict(id=made, product=product, quantity=quantity, kind=kind, reference=f.get('count') if kind == 'count variance' else f.get('handover'), reason=reason)
    if event:
        led.emit('shop.' + event, now, payload, 'shop:' + act.id, source=act.source, command=act.id)
        ok, check = True, 'The shop record was saved.'
    led.emit('gate.decided', now, dict(id=act.id, action=act.action, source=act.source, about=act.about, lease=None, command=act.id, allowed=ok, check=check, why=why, made=made), 'gate:' + act.id, source=act.source, command=act.id)
    return Decision(ok, act.action, check, command=act.id, made=made, why=why)


def run(store: Store, stream: str, act: Act, now: datetime) -> Decision:
    with store.write():
        led = Ledger(store.books(stream))
        decision = command(led, act, now)
        if not decision.repeated:
            decision.events = [e.id for e in store.append(stream, led.pending) if e.id is not None]
        return decision


def attempt(st: State, quote: str, which: str | None = None) -> dict | None:
    """The payment attempt a form is about: the current one (None), the first one the sale made ('first'), or the one with that key."""
    every = st.every_attempt(quote)
    if not every:
        return None
    return every[-1] if which is None else every[0] if which == 'first' else next((a for a in every if a['key'] == which), None)


def pay(store: Store, stream: str, processor, quote: str, card: str, now: datetime, which: str | None = None):
    """Ask once for the frozen quote's payment and keep the processor's answer. A form that asks again is bound to the attempt it
    created (`which`): it resumes that attempt with the card and key it was made with, or gets that attempt's recorded result back."""
    from .billing import Refused
    from .delinquency import ProcessorRefused, ProcessorTimeout

    with store.write():
        books = store.books(stream)
        st = state(books)
        q = st.quotes.get(quote)
        if not q:
            raise Refused('no open shop quote')
        target = attempt(st, quote, which)
        current = st.payments.get(quote)
        open_now = (quote not in st.cancelled and quote not in st.attachments
                    and not (quote in st.sales and (current or {}).get('status') not in ('open', 'processing')))
        if which is not None and target is not None and (target is not current or not open_now):
            return target                       # an older attempt, or a sale that is no longer open: its recorded result, never asked again
        if not open_now:
            raise Refused('this quote belongs to a move-in payment' if quote in st.attachments else 'no open shop quote')    # a posted sale is asked again only for a new attempt after its payment came back
        if current is None:
            led = Ledger(books)
            key = f'{stream}:shop:{uuid4().hex}'
            led.emit('shop.payment_requested', now, dict(quote=quote, key=key, cents=q['price'] * q['quantity'], status='open', card=card),
                     'shop:payment:' + quote, source='processor')
            store.append(stream, led.pending)
        payment = state(books).payments[quote]
        if which not in (None, 'first') and payment['key'] != which:
            raise Refused('no such payment attempt')
        if payment['status'] not in ('open', 'processing'):
            return payment
        card = payment.get('card') or card
        if not card:
            raise Refused('no card is on record for this payment')
    try:
        answer = processor.create(payment['key'], payment['cents'], card, {'copy': stream, 'shop_quote': quote}, now)
    except ProcessorTimeout:
        return payment
    except ProcessorRefused:
        return _confirm_none(store, stream, processor, quote, payment['key'], now)
    return _record_answer(store, stream, quote, answer, now)


def _confirm_none(store: Store, stream: str, processor, quote: str, key: str, now: datetime):
    """The processor refused a request: only its own record that it holds no payment for the key lets the sale be tried again or cancelled.
    Everything is read for the attempt that was sent (by its key), never for a newer one that replaced it while the request was out."""
    from .billing import Refused
    from .delinquency import ProcessorRefused, ProcessorTimeout
    with store.write():
        sent = next((a for a in state(store.books(stream)).every_attempt(quote) if a['key'] == key), None)
    if sent is None:
        raise Refused('no such payment attempt')
    try:
        found = processor.find(key, since=sent['at'])
    except (ProcessorRefused, ProcessorTimeout):
        return sent                             # not confirmed: the sale stays as it is
    if found is not None:
        return _record_answer(store, stream, quote, found, now) or sent
    return _record_none(store, stream, quote, key, now)


def _record_none(store: Store, stream: str, quote: str, key: str, now: datetime):
    """Write down that the processor confirmed it holds no payment for this attempt's key, if that attempt is still the current one.
    Returns the attempt with that key."""
    with store.write():
        books = store.books(stream)
        current = state(books).payments[quote]
        if current['key'] == key and current['status'] == 'open' and not current.get('problem'):
            led = Ledger(books)
            led.emit('shop.payment_problem', now, {'quote': quote}, 'shop:payment-problem:' + quote + (f":{current['attempt']}" if current.get('attempt') else ''),
                     source='processor')
            store.append(stream, led.pending)
        return next(a for a in state(books).every_attempt(quote) if a['key'] == key)


def _attempt_for(st: State, quote: str, answer: dict, stream: str) -> dict | None:
    """The attempt of this sale the processor's answer is about, the current one or one a newer attempt replaced: by its key and amount,
    and the copy and quote it carries."""
    meta = answer.get('metadata') or {}
    if meta.get('copy') != stream or meta.get('shop_quote') != quote:
        return None
    return next((a for a in reversed(st.every_attempt(quote)) if a['key'] == answer.get('key') and a['cents'] == answer.get('cents')), None)


def _record_answer(store: Store, stream: str, quote: str, answer: dict, now: datetime):
    with store.write():
        books = store.books(stream)
        found = _attempt_for(state(books), quote, answer, stream)
        if found is None:
            return None
        led = Ledger(books)
        _emit_answer(led, quote, found, answer, now)
        store.append(stream, led.pending)
        return next(a for a in reversed(state(books).every_attempt(quote)) if a['key'] == found['key'])      # the attempt the processor answered


def _emit_answer(led: Ledger, quote: str, attempt: dict, answer: dict, now: datetime) -> bool:
    """Record the processor's answer on the attempt it is about, if it moves forward. A late success on an attempt a newer one replaced
    is recorded too: it is money taken twice and shows as owed back. A return of money that was taken cancels the refund set aside for
    it that has not gone out."""
    status, previous = answer.get('status'), attempt['status']
    if status not in PAYMENT_MOVES.get(previous, ()):
        return False
    if attempt is state(led.books).payments[quote]:
        led.emit('shop.payment_answered', now, dict(quote=quote, status=status, previous=previous, outside=answer['id'], cents=attempt['cents']),
                 f'shop:answer:{quote}:{status}' + (f":{attempt['attempt']}" if attempt.get('attempt') else ''), source='processor')
    else:
        led.emit('shop.attempt_answered', now, dict(quote=quote, key=attempt['key'], status=status, previous=previous, outside=answer['id'], cents=attempt['cents']),
                 f"shop:attempt:{quote}:{attempt['key']}:{status}", source='processor')
    if status == 'returned' and previous == 'succeeded':
        cancel_refunds(led, quote, attempt['key'], now)
    return True


def cancel_refunds(led: Ledger, quote: str, key: str, now: datetime) -> None:
    """The charge for this attempt came back: a refund set aside for it and not yet sent is cancelled and its liability reversed, so the
    books do not show money owed that the processor already returned. A refund that went out stays: that payment is not erased."""
    for r in list(state(led.books).refunds.values()):
        if r['quote'] == quote and not r.get('paid') and not r.get('cancelled') and r.get('key') in (None, key):
            led.emit('shop.refund_cancelled', now, dict(refund=r['id'], quote=quote, cents=r['cents'], unmatched=bool(r.get('unmatched'))),
                     'shop:refund-cancelled:' + r['id'], source='processor')


def webhook(store: Store, processor, event: dict, now: datetime) -> str:
    metadata = event.get('metadata') or {}
    stream, quote = metadata.get('copy'), metadata.get('shop_quote')
    if not stream or not quote or not event.get('id') or not event.get('payment'):
        return 'unknown'
    answer = processor.retrieve(event['payment'])
    with store.write():
        books = store.books(stream)
        st = state(books)
        if event['id'] in st.webhooks:
            return 'duplicate'
        found = _attempt_for(st, quote, answer, stream)
        if found is None:
            return 'unknown'
        led = Ledger(books)
        led.emit('shop.webhook', now, dict(event=event['id'], quote=quote), 'shop:webhook:' + event['id'], source='processor')
        _emit_answer(led, quote, found, answer, now)
        store.append(stream, led.pending)
    return 'applied'


def reconcile(store: Store, stream: str, processor, now: datetime) -> None:
    """Read standalone payments from the processor after a lost answer or event."""
    from .delinquency import IN_FLIGHT, ProcessorRefused, ProcessorTimeout
    st = state(store.books(stream))
    for quote, payment in [(q, a) for q in st.payments for a in st.every_attempt(q)]:        # every attempt: an older one can still come in
        if payment.get('intent') or payment['status'] == 'returned':
            continue
        try:
            answer = (processor.retrieve(payment['outside']) if payment.get('outside')
                      else processor.find(payment['key'], since=payment['at']))
        except (ProcessorRefused, ProcessorTimeout):
            continue
        if answer is not None:
            _record_answer(store, stream, quote, answer, now)
        elif payment is st.payments[quote] and not payment.get('outside') and payment['status'] == 'open' and now - payment['at'] >= IN_FLIGHT:
            _record_none(store, stream, quote, payment['key'], now)         # the processor holds nothing for the key: the sale can be tried again or cancelled


def complete_confirmed(store: Store, stream: str, now: datetime) -> int:
    """Post the sale of every counter payment the processor confirmed whose sale is not posted yet: the step a confirmation that came
    late (a webhook, or the hourly check after a lost answer) ends in, the same step as an answer that came at once. A cancelled quote
    is never posted, and a posted one is skipped, so running it again changes nothing."""
    with store.write():
        books = store.books(stream)
        st = state(books)
        led = Ledger(books)
        for quote, payment in st.payments.items():
            q = st.quotes.get(quote)
            if q and payment['status'] == 'succeeded' and not any(quote in held for held in (st.sales, st.cancelled, st.write_offs, st.attachments)):
                post_sale(led, quote, q, now, 'processor')
        store.append(stream, led.pending)
        return len(led.pending) // 2


def differences(st: State) -> list[str]:
    """Compare stock movements and sale postings with their own supporting records."""
    out = []
    for receipt in st.receipts.values():
        found = [m for m in st.moves if m['kind'] == 'received' and m.get('reference') == receipt['id']]
        if len(found) != 1 or found[0]['product'] != receipt['product'] or found[0]['quantity'] != receipt['quantity']:
            out.append(f"Receipt {receipt['id']} does not match one stock receipt")
    for handover in st.handovers.values():
        found = [m for m in st.moves if m['kind'] == 'give-away' and m.get('reference') == handover['id']]
        if len(found) != 1 or found[0]['product'] != handover['product'] or found[0]['quantity'] != -handover['quantity']:
            out.append(f"Handover {handover['id']} does not match one stock adjustment")
    for receipt in st.returns.values():
        found = [m for m in st.moves if m['kind'] == 'return' and m.get('reference') == receipt['id']]
        if len(found) != 1 or found[0]['product'] != receipt['product'] or found[0]['quantity'] != receipt['quantity']:
            out.append(f"Return {receipt['id']} does not match one stock return")
    for quote, sale in st.sales.items():
        if quote in st.reversed_sales:
            continue
        payment = st.payments.get(quote)
        frozen = st.quotes.get(quote)
        if (not frozen or any(sale.get(k) != frozen.get(k) for k in ('product', 'quantity', 'price', 'cost'))):
            out.append(f"Sale {quote} does not match its frozen quote")
        if (not payment or payment.get('cents') != sale['price'] * sale['quantity']
                or (payment['status'] != 'succeeded' and quote not in st.write_offs
                    and not (payment['status'] == 'returned' and payment.get('previous') == 'succeeded'))):
            out.append(f"Sale {quote} has no confirmed payment")
        found = [m for m in st.moves if m['kind'] == 'sold' and m.get('reference') == quote]
        if len(found) != 1 or found[0]['product'] != sale['product'] or found[0]['quantity'] != -sale['quantity']:
            out.append(f"Sale {quote} does not match one stock sale")
    for quote, payment in st.payments.items():
        if (payment['status'] == 'succeeded' or (payment['status'] == 'returned' and payment.get('previous') == 'succeeded')) and quote not in st.sales and quote not in st.cancelled:
            out.append(f"Payment for {quote} has no sale posting")
    for count in st.counts.values():
        found = [m for m in st.moves if m['kind'] == 'count variance' and m.get('reference') == count['id']]
        if len(found) > 1:
            out.append(f"Count {count['id']} has more than one stock adjustment")
    for m in st.moves:
        kind, ref = m['kind'], m.get('reference')
        supported = (kind == 'received' and ref in st.receipts or kind == 'sold' and ref in st.sales
                     or kind == 'return' and ref in st.returns or kind == 'give-away' and ref in st.handovers
                     or kind == 'count variance' and ref in st.counts or kind == 'damage')
        if not supported:
            out.append(f"Stock movement {m['id']} has no supporting record")
        if kind == 'count variance' and ref in st.counts:
            count = st.counts[ref]
            if not count.get('review') or count['product'] != m['product'] or count['variance'] != m['quantity']:
                out.append(f"Stock movement {m['id']} does not match its reviewed count")
        if (m.get('product') not in st.versions or type(m.get('quantity')) is not int or not m['quantity']
                or not isinstance(m.get('reason'), str) or not m['reason'].strip()
                or (kind == 'damage' and m['quantity'] >= 0)):
            out.append(f"Stock movement {m['id']} has invalid details")
    for product in st.versions:
        if st.quantity(product) < 0:
            out.append(f"Stock for {product} is negative")
        if st.available(product) < 0:
            out.append(f"Reservations for {product} exceed stock")
    return sorted(set(out))


def sweep(store: Store, stream: str, now: datetime) -> int:
    from . import work
    with store.write():
        books = store.books(stream)
        st = state(books)
        day, found = local(now).date().isoformat(), differences(st)
        last = st.checks[-1] if st.checks else None
        changed = not last or last['day'] != day or last['differences'] != found
        if changed:
            led = Ledger(books)
            led.emit('shop.checked', now, {'day': day, 'differences': found}, f'shop:check:{day}:{len(st.checks)}', source='code')
            store.append(stream, led.pending)
    return int(changed) + work.sweep(store, stream, now)
