"""Review fix on counter payment attempts: a refusal is resolved against the attempt that was sent, never a newer one that replaced it
while the request was out. The first and third tests fail on the code before the fix; the others keep what already worked."""
from core import shop
from core.delinquency import ProcessorTimeout, SimulatedProcessor

from .test_core_operations_set_shop import NOW, World
from .test_shop_review_fixes import Dropping, LATER, quote_and_ask


class NoLookup(SimulatedProcessor):
    """The processor's lookup by key times out."""
    def find(self, key, since=None):
        raise ProcessorTimeout(key)


def replaced_by_a_newer_attempt():
    """A sale whose first attempt was declined and a newer attempt then started: the first one's refusal is resolved after that."""
    w = World()
    w.stock(3)
    processor = SimulatedProcessor()
    quote = w.do('quote_sale', 'lock', quantity=1).made
    first = shop.pay(w.store, 'shop', processor, quote, 'card_declined', NOW)
    assert w.do('retry_payment', quote, card='card_ok').allowed
    return w, processor, quote, first


def test_a_refusal_is_resolved_against_the_attempt_that_was_sent_when_the_lookup_times_out():
    w, _, quote, first = replaced_by_a_newer_attempt()
    newer = w.state().payments[quote]
    got = shop._confirm_none(w.store, 'shop', NoLookup(), quote, first['key'], NOW)
    assert got['key'] == first['key'] and got['key'] != newer['key']            # the original attempt, never the newer one's result


def test_a_refusal_found_paid_for_the_original_key_is_credited_to_that_attempt():
    w, processor, quote, first = replaced_by_a_newer_attempt()
    processor.settle(first['outside'], 'succeeded', NOW)
    got = shop._confirm_none(w.store, 'shop', processor, quote, first['key'], NOW)
    assert got['key'] == first['key'] and got['status'] == 'succeeded'
    assert w.state().payments[quote]['status'] == 'open'                        # the newer attempt is untouched


def test_no_payment_for_an_old_key_is_not_written_against_the_newer_attempt():
    w, _, quote, first = replaced_by_a_newer_attempt()
    processor = SimulatedProcessor()
    processor.by_key.clear()                                                    # the processor holds nothing for any key
    got = shop._confirm_none(w.store, 'shop', processor, quote, first['key'], LATER)
    assert got['key'] == first['key'] and not w.state().payments[quote].get('problem')


def test_a_current_attempt_the_processor_holds_nothing_for_is_marked_and_returned():
    w = World()
    w.stock(3)
    processor = Dropping()
    quote, payment = quote_and_ask(w, processor)
    got = shop._confirm_none(w.store, 'shop', SimulatedProcessor(), quote, payment['key'], LATER)
    assert got['key'] == payment['key'] and got['problem'] and w.state().payments[quote]['problem']

