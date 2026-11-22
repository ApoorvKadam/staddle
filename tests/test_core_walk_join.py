"""Two staff reports of one fault: code finds two open work orders on one unit, staff join the later one into the first, and one order
stays open carrying both reports. Code never decides two reports are the same fault; joining changes nothing about the repair itself."""
from datetime import timedelta

from core import walk, work
from tests.test_core_walk_compare import EMPTY, NOW, SECOND, STREAM, WALKED
from tests.test_core_walk_ready import Floor

MANAGER = "staff:office_manager"


def join(w, dup, keep, n=1, text="Same spring", source=MANAGER):
    return walk.run(w.store, STREAM, walk.Act(f"j{n}", "join_work_order", source, about=dup, into=keep, text=text), WALKED + timedelta(hours=1))


def state(w):
    return walk.load(w.store, STREAM)[1]


def open_items(w):
    books, rent, wk = work.load(w.store, STREAM)
    return {i.key: i for i in work.queue(books, rent, wk, NOW) if i.state not in work.CLOSED}


def two(w):
    return w.report(SECOND, "Broken door spring"), w.report(SECOND, "Door spring is broken")


def test_two_open_orders_on_one_unit_are_a_pair_and_nothing_joins_them_by_itself():
    w = Floor()
    a, b = two(w)
    w.report(EMPTY, "Light out above the door")
    s = state(w)
    assert [(x.id, y.id) for x, y in walk.pairs(s)] == [(a, b)]
    assert (s.orders[b].state, s.orders[b].joined_into) == ("reported", None)
    assert {k for k, i in open_items(w).items() if i.unit == SECOND} == {f"maintenance:{a}", f"maintenance:{b}"}


def test_staff_join_the_later_order_and_one_order_stays_open_with_both_reports():
    w = Floor()
    a, b = two(w)
    d = join(w, b, a)
    assert d.allowed and d.made == a, d.check
    s = state(w)
    assert (s.orders[b].state, s.orders[b].joined_into, s.orders[b].changes[-1].state) == ("closed", a, "joined")
    assert [r.text for r in s.orders[a].reports] == ["Broken door spring", "Door spring is broken"]
    assert s.orders[a].state == "reported"                                   # the repair itself has not moved
    assert walk.pairs(s) == []
    assert [k for k, i in open_items(w).items() if i.unit == SECOND] == [f"maintenance:{a}"]       # one open item
    assert w.ready(SECOND) == [f"work order {a} is still open (Broken door spring)"]               # still out of renting


def test_a_join_is_refused_in_plain_words():
    w = Floor()
    a, b = two(w)
    other = w.report(EMPTY, "Door spring is broken")
    assert join(w, b, b, 1).check == "a work order cannot join itself"
    assert join(w, other, a, 2).check == f"those work orders are on different units ({EMPTY} and {SECOND})"
    assert join(w, "WO9999", a, 3).check == "there is no such work order"
    assert "only staff may" in join(w, b, a, 4, source="renter").check
    w.move(b, ("triage_work_order", {}), ("assign_work_order", {"to": "front_desk"}), ("record_order_work", {"text": "Spring replaced"}))
    assert join(w, b, a, 5).check == "work is already recorded on that order; finish it instead of joining it"
    w.close(a)
    late = w.report(SECOND, "Spring still broken")
    assert join(w, late, a, 6).check == "both work orders must still be open"


def test_a_repeated_join_returns_its_first_decision_and_writes_nothing():
    w = Floor()
    a, b = two(w)
    first = join(w, b, a, 7)
    before = len(list(w.store.events(STREAM)))
    again = join(w, b, a, 7)
    assert first.allowed and again.allowed and again.repeated
    assert len(list(w.store.events(STREAM))) == before


def test_the_joined_orders_item_ends_as_the_same_fault_never_as_checked_work():
    w = Floor()
    a, b = two(w)
    work.sweep(w.store, STREAM, NOW)                       # the daily check opened an item for each order
    join(w, b, a)
    books, rent, wk = work.load(w.store, STREAM)
    item = next(i for i in work.queue(books, rent, wk, NOW) if i.key == f"maintenance:{b}")
    assert item.state == "cancelled" and "same fault as the order reported" in item.evidence and "Broken door spring" in item.evidence
    work.sweep(w.store, STREAM, NOW)
    books, rent, wk = work.load(w.store, STREAM)
    item = next(i for i in work.queue(books, rent, wk, NOW) if i.key == f"maintenance:{b}")
    assert item.state == "cancelled" and "staff checked" not in item.evidence.lower()


def test_staff_keep_two_orders_apart_and_that_pair_is_not_offered_again():
    w = Floor()
    a, b, c = (w.report(SECOND, t) for t in ("Broken door spring", "Light out above the door", "Door spring is broken"))
    assert [(x.id, y.id) for x, y in walk.pairs(state(w))] == [(a, b), (a, c)]
    keep = walk.Act("k1", "keep_work_orders_apart", MANAGER, about=b, into=a, text="The light is separate")
    assert walk.run(w.store, STREAM, keep, WALKED + timedelta(hours=1)).allowed
    s = state(w)
    assert [(x.id, y.id) for x, y in walk.pairs(s)] == [(a, c)]
    assert (s.orders[a].state, s.orders[b].state, s.orders[b].changes[-1].state) == ("reported", "reported", "kept apart")
    again = walk.run(w.store, STREAM, walk.Act("k2", "keep_work_orders_apart", MANAGER, about=a, into=b), WALKED + timedelta(hours=2))
    assert again.check == "those two work orders are already kept apart"
