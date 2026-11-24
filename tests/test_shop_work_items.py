"""Only a record closes a shop item on Today: staff words never do."""
from core import shop, work
from tests.test_core_operations_set_shop import NOW, World


def open_items(w, kind):
    shop.sweep(w.store, 'shop', NOW)
    b, rent, wk = work.load(w.store, 'shop')
    return [i for i in work.queue(b, rent, wk, NOW) if i.source == 'shop' and i.kind == kind and i.state not in work.CLOSED]


def counted(w, quantity):
    d = w.do('record_count', 'lock', quantity=quantity)
    assert d.allowed
    return d.made


def test_asking_for_a_recount_leaves_the_count_item_open_until_a_newer_count():
    w = World()
    w.stock(12)
    first = counted(w, 9)
    assert len(open_items(w, 'stock count variance')) == 1
    assert w.do('review_count', first, reason='Shelf looked short', recount=True).allowed
    assert len(open_items(w, 'stock count variance')) == 1
    assert w.state().quantity('lock') == 12
    refused = w.do('adjust_stock', 'lock', kind='count variance', quantity=-3, count=first, reason='Fix it')
    assert not refused.allowed and refused.why == 'recount_asked'
    counted(w, 12)                      # the recount agrees with the books: nothing is left to settle
    assert open_items(w, 'stock count variance') == []
    assert w.state().quantity('lock') == 12


def test_a_newer_count_that_differs_carries_its_own_item_and_can_be_reviewed():
    w = World()
    w.stock(12)
    first = counted(w, 9)
    assert w.do('review_count', first, reason='Shelf looked short', recount=True).allowed
    second = counted(w, 8)
    items = open_items(w, 'stock count variance')
    assert len(items) == 1 and 'counted 8' in items[0].why
    assert w.do('review_count', second, reason='Counted twice').allowed
    assert w.do('adjust_stock', 'lock', kind='count variance', quantity=-4, count=second, reason='Counted twice').allowed
    assert w.state().quantity('lock') == 8
    assert open_items(w, 'stock count variance') == []


def test_a_reviewed_count_stays_open_until_its_difference_is_posted():
    w = World()
    w.stock(12)
    first = counted(w, 9)
    assert w.do('review_count', first, reason='Counted twice').allowed
    assert len(open_items(w, 'stock count variance')) == 1
    assert w.do('adjust_stock', 'lock', kind='count variance', quantity=-3, count=first, reason='Counted twice').allowed
    assert open_items(w, 'stock count variance') == []
