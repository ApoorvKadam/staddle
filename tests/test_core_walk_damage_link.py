"""A damaged door stays a finding until a repair of that door is dealt with: an unrelated work order on the unit hides nothing, and a
closed order counts only when its staff check came after the damage was seen."""
from datetime import timedelta

from core import walk
from tests.test_core_walk_compare import EMPTY, NOW, STREAM, WALKED, World

MANAGER, FRONT = "staff:office_manager", "staff:front_desk"


def order(w, text, at):
    w.n += 1
    d = walk.run(w.store, STREAM, walk.Act(f"r{w.n}", "report_work_order", FRONT, unit=EMPTY, text=text), at)
    assert d.allowed, d.check
    return d.made


def move(w, oid, steps, at):
    for i, (action, kw) in enumerate(steps):
        w.n += 1
        d = walk.run(w.store, STREAM, walk.Act(f"m{w.n}", action, MANAGER, about=oid, **kw), at + timedelta(minutes=i))
        assert d.allowed, d.check


def slugs(w):
    return {f.slug for f in w.found() if f.unit == EMPTY}


def test_an_unrelated_cleaning_order_does_not_hide_a_damaged_door():
    w = World()
    order(w, "Needs cleaning after the move-out", WALKED - timedelta(hours=1))
    w.walk(EMPTY, lock="absent", whose="unknown", door="damaged")
    assert "door_damaged" in slugs(w)


def test_an_order_for_the_door_hides_the_finding_while_it_is_open():
    w = World()
    order(w, "Door track bent", WALKED - timedelta(hours=1))
    w.walk(EMPTY, lock="absent", whose="unknown", door="damaged")
    assert "door_damaged" not in slugs(w)


def test_a_door_order_checked_before_the_damage_was_seen_and_closed_after_still_leaves_the_finding():
    w = World()
    oid = order(w, "Door track bent", WALKED - timedelta(days=2))
    move(w, oid, [("triage_work_order", {}), ("assign_work_order", {"to": "front_desk"}), ("record_order_work", {"text": "Fixed"}),
                  ("check_order_work", {"text": "Checked"})], WALKED - timedelta(hours=2))
    w.walk(EMPTY, lock="absent", whose="unknown", door="damaged")     # damage seen after the check
    move(w, oid, [("close_work_order", {})], WALKED + timedelta(hours=2))
    assert "door_damaged" in slugs(w)
