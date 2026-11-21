"""No charge from a finding: a walk record, an observation or a work order never posts a charge. A damaged door, a lock left behind or an
unfinished repair is something staff see; any money question about it is a separate step staff take through billing."""
from datetime import timedelta

from core import renting, walk, work
from core.books import at_local, lines
from core.seed import NOW, build
from core.store import Store

STREAM = "visitor:a"
FRONT, MANAGER = "staff:front_desk", "staff:office_manager"
KINDS = ("walk.started", "walk.observed", "walk.finished", "work_order.reported", "work_order.reported_again", "work_order.triaged",
         "work_order.assigned", "work_order.recorded", "work_order.checked", "work_order.closed")


def test_no_walk_or_work_order_event_has_a_journal_line():
    p = {"walk": "W1", "observation": "O1", "unit": "A101", "lock": "present", "whose": "tenant", "tag": "absent", "door": "damaged", "note": "Door bent",
         "by": FRONT, "supersedes": None, "route": ["A101"], "order": "WO1", "problem": "Door bent", "text": "Door bent", "to": "front_desk"}
    assert all(lines(kind, p) == [] for kind in KINDS)


def test_every_walk_and_work_order_command_leaves_the_journal_as_it_was():
    y = build(today=NOW.date(), cutoff=at_local(NOW.date()), standing=True)
    store = Store()
    with store.write():
        store.append("base", y.led.pending)
    base_lines = store.db.execute("SELECT COUNT(*) FROM journal_lines").fetchone()[0]
    unit, now = y.walked["repair"], at_local(NOW.date(), 10)

    def ask(n, action, source=FRONT, **kw):
        d = walk.run(store, STREAM, walk.Act(f"n{n}", action, source, **kw), now + timedelta(minutes=n))
        assert d.allowed, d.check
        return d.made
    wid = ask(1, "start_walk", route=[unit, y.walked["lock_on_vacant"]])
    damaged = {"lock": "absent", "whose": "unknown", "tag": "absent", "door": "damaged"}
    first = ask(2, "observe_unit", about=wid, unit=unit, form=damaged, text="Door bent out of its frame, a damage to charge for")
    ask(3, "correct_observation", about=first, form={**damaged, "door": "open"})
    ask(4, "finish_walk", about=wid)
    order = ask(5, "report_work_order", unit=unit, text="Door bent out of its frame")
    ask(6, "report_work_order", unit=unit, about=order, text="Same door, tenant says they did not do it")
    for n, (action, kw) in enumerate((("triage_work_order", {}), ("assign_work_order", {"to": "office_manager"}), ("record_order_work", {"text": "Door replaced"}),
                                      ("check_order_work", {"text": "Closes"}), ("close_work_order", {})), start=7):
        ask(n, action, MANAGER, about=order, **kw)
    assert work.sweep(store, STREAM, now) > 0                       # the daily check opens the items the findings make, and charges nothing
    books, rent = renting.load(store, STREAM)
    assert store.journal(STREAM) == []
    assert store.db.execute("SELECT COUNT(*) FROM journal_lines").fetchone()[0] == base_lines
    assert store.db.execute("SELECT COUNT(*) FROM events WHERE stream = ? AND kind IN ('charge.posted', 'charge.reversed', 'payment.received', "
                            "'payment.returned', 'credit.applied')", (STREAM,)).fetchone()[0] == 0
    assert {t: books.past_due(t, now) for t in books.tenants} == {t: store.books("base").past_due(t, now) for t in books.tenants}
