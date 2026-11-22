"""Tilden's reading of two open work orders, wired into the front desk the way an investigation is: the visitor's daily count and the
day's budget checked before any call, the worst cost reserved and settled, "Tilden is off on this copy" with no client. The only record
written is the reading; joining the orders is a staff step. A stand-in client returns fixed answers, so nothing here touches the network."""
import pytest
from fastapi import HTTPException

from core import walk as walks
from core import walk_reading
from core.billing import Ledger
from tests.test_front_desk_walk import A, FRONT, NOW, make, records, usage
from tests.walk_client import report_ids, same_answer


def answer(content):
    ids = report_ids(content)
    return same_answer(ids["First"] + ids["Second"])


def two(fd, visitor=A):
    store, stream = fd.ready(), fd.stream(visitor)
    books = store.books(stream)
    taken = {o.unit for o in walks.load(store, stream)[1].orders.values()}
    unit = next(u for u in sorted(books.units) if u not in taken)
    made = [walks.run(store, stream, walks.Act(f"r{n}", "report_work_order", FRONT, unit=unit, text=text), NOW).made
            for n, text in ((1, "Broken door spring"), (2, "Door spring is broken"))]
    return made


def test_a_kept_reading_is_the_only_record_written_and_its_cost_is_counted():
    fd = make(answer)
    a, b = two(fd)
    before = records(fd)
    out = fd.compare_orders(A, a, b)
    assert (out["off"], out["dropped"], out["reading"]["verdict"]) == (False, None, "same") and out["cents"] > 0
    assert records(fd) == before                                        # no order, lease, block, overlock or money moved
    wk = walks.load(fd.ready(), fd.stream(A))[1]
    assert [r["reading"]["verdict"] for r in wk.comparisons[f"{a}:{b}"]] == ["same"]
    assert wk.orders[b].state == "reported" and wk.orders[b].joined_into is None
    assert usage(fd) == [("investigate", pytest.approx(out["cents"]))] and fd.reserved == 0


def test_the_same_two_orders_are_not_read_again_until_a_report_changes():
    fd = make(answer, answer)
    a, b = two(fd)
    fd.compare_orders(A, a, b)
    with pytest.raises(HTTPException) as e:
        fd.compare_orders(A, a, b)
    assert e.value.status_code == 409 and len(usage(fd)) == 1


def test_orders_that_are_no_longer_a_pair_are_refused_before_any_call():
    fd = make(answer)
    a, b = two(fd)
    d = walks.run(fd.ready(), fd.stream(A), walks.Act("j1", "join_work_order", FRONT, about=b, into=a), NOW)
    assert d.allowed, d.check
    with pytest.raises(HTTPException) as e:
        fd.compare_orders(A, a, b)
    assert e.value.status_code == 404 and usage(fd) == []


def test_with_no_client_tilden_is_off_and_nothing_is_written():
    fd = make(client=False)
    a, b = two(fd)
    out = fd.compare_orders(A, a, b)
    assert out["off"] and "off on this copy" in out["reason"] and usage(fd) == []
    assert walks.load(fd.ready(), fd.stream(A))[1].comparisons == {}


def test_a_reading_kept_by_another_request_meanwhile_stands_and_no_second_is_written():
    fd = make(answer)
    a, b = two(fd)
    def other_request_finishes_first():         # runs while this request's call is out, outside the desk's lock
        store, stream = fd.ready(), fd.stream(A)
        first, second = (walks.load(store, stream)[1].orders[x] for x in (a, b))
        recs = walk_reading.order_records(first, second, "zz")
        with store.write():
            led = Ledger(store.books(stream))
            led.emit("work_order.compared", NOW, {"pair": f"{a}:{b}", "unit": first.unit, "run": "zz", "records": recs,
                                                   "reading": {"verdict": "same", "summary": "Kept first.", "records": [recs[0]["id"], recs[1]["id"]]},
                                                   "checks": {"kept": True, "dropped": None}, "call": None}, "compare:zz", source="operator")
            store.append(stream, led.pending)
    fd.client.on_call = other_request_finishes_first
    out = fd.compare_orders(A, a, b)
    assert out["reading"]["summary"] == "Kept first."
    assert len(walks.load(fd.ready(), fd.stream(A))[1].comparisons[f"{a}:{b}"]) == 1
