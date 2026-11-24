"""Work orders: a repair from reported to closed, each change with who and when. Only the transitions are recorded here; when a
closed order makes a unit rentable again is a later rule and is not decided by this record."""
from datetime import timedelta

from core import renting, walk
from tests.test_core_walk import AT, FRONT, put, tiny

STREAM = "visitor:a"
MANAGER = "staff:manager"


def reported(store, n=1, unit="A102", problem="Door will not latch", by=FRONT, at=AT):
    put(store, STREAM, n, "work_order.reported", {"order": "WO0001", "unit": unit, "problem": problem, "by": by}, at)


def test_a_work_order_names_the_unit_what_is_wrong_and_who_reported_it():
    store = tiny()
    reported(store)
    _, wk = walk.load(store, STREAM)
    o = wk.orders["WO0001"]
    assert (o.unit, o.problem, o.reported_by, o.reported, o.state) == ("A102", "Door will not latch", FRONT, AT, "reported")
    assert [(c.state, c.by, c.at) for c in o.changes] == [("reported", FRONT, AT)]


def test_each_change_of_state_is_kept_with_who_and_when():
    store = tiny()
    reported(store)
    put(store, STREAM, 2, "work_order.triaged", {"order": "WO0001", "by": MANAGER, "text": "Needs a new latch"}, AT + timedelta(hours=1))
    put(store, STREAM, 3, "work_order.assigned", {"order": "WO0001", "by": MANAGER, "to": "front_desk", "text": ""}, AT + timedelta(hours=2))
    put(store, STREAM, 4, "work_order.recorded", {"order": "WO0001", "by": FRONT, "text": "Fitted a new latch"}, AT + timedelta(days=1))
    put(store, STREAM, 5, "work_order.checked", {"order": "WO0001", "by": MANAGER, "text": "Door shuts and latches"}, AT + timedelta(days=1, hours=1))
    put(store, STREAM, 6, "work_order.closed", {"order": "WO0001", "by": MANAGER, "text": ""}, AT + timedelta(days=1, hours=2))
    _, wk = walk.load(store, STREAM)
    o = wk.orders["WO0001"]
    assert o.state == "closed" and o.assigned_to == "front_desk"
    assert [(c.state, c.by) for c in o.changes] == [("reported", FRONT), ("triaged", MANAGER), ("assigned", MANAGER), ("work recorded", FRONT),
                                                     ("staff checked", MANAGER), ("closed", MANAGER)]
    assert [c.at for c in o.changes] == [AT, AT + timedelta(hours=1), AT + timedelta(hours=2), AT + timedelta(days=1),
                                         AT + timedelta(days=1, hours=1), AT + timedelta(days=1, hours=2)]
    assert o.changes[1].text == "Needs a new latch"


def test_closing_a_work_order_only_records_the_state_and_changes_no_unit_or_money():
    store = tiny()
    reported(store)
    put(store, STREAM, 2, "work_order.closed", {"order": "WO0001", "by": MANAGER, "text": ""})
    books, wk = walk.load(store, STREAM)
    assert wk.orders["WO0001"].state == "closed"
    assert store.journal(STREAM) == []
    rent = renting.Rent(books)
    for e in store.events(STREAM):
        rent.apply(e.kind, e.at, e.payload)
    assert rent.blocks == {} and rent.status("A102", AT) == "vacant"        # a closed order does not block, release or rent a unit


def test_agent_closes_a_report_as_needing_no_work_only_while_nobody_has_worked_on_it():
    store = tiny()
    reported(store)
    seen = {}
    steps = [None, "work_order.triaged", "work_order.assigned", "work_order.recorded", "work_order.checked", "work_order.closed"]
    for n, kind in enumerate(steps, start=1):
        if kind:
            put(store, STREAM, n, kind, {"order": "WO0001", "by": MANAGER, "text": "x"}, AT + timedelta(minutes=n))
        books, wk = walk.load(store, STREAM)
        act = walk.Act("c1", "close_report_no_work", "agent:tilden", about="WO0001", text="Staff note O1 says it is dry")
        seen[wk.orders["WO0001"].state] = walk.evaluate(books, wk, act, AT + timedelta(hours=1), agent_allowed=True)
    assert {s: ok for s, (ok, _) in seen.items()} == {"reported": True, "triaged": True, "assigned": True, "work recorded": False, "staff checked": False, "closed": False}
    assert all("nobody has worked on" in c for s, (ok, c) in seen.items() if not ok)
