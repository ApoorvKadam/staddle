"""Belongings: an observation says whether contents were seen in the unit (seen, not seen, unknown; unknown unless said, so older
observations and forms still read). Contents seen on a unit the FMS calls vacant is a finding of its own, naming the records that
disagree, and it stops readiness until a later look finds the unit empty. Tilden reads contents from the staff member's own words."""
from datetime import timedelta

from core import walk, work
from tests.test_core_walk_compare import EMPTY, NOW, PAID, STREAM, WALKED, World, slugs
from tests.test_core_walk_ready import Floor
from tests.test_walk_reading import keep
from tests.walk_client import note_answer

MANAGER = "staff:office_manager"


def test_an_observation_carries_contents_and_an_older_one_reads_unknown():
    w = World()
    w.walk(EMPTY, lock="absent", whose="unknown", contents="seen", note="Boxes stacked inside")
    w.walk(PAID, at=WALKED + timedelta(hours=1))
    _, wk = walk.load(w.store, STREAM)
    assert wk.standing(EMPTY).contents == "seen" and wk.standing(PAID).contents == "unknown"
    events = {e.payload["unit"]: e.payload for e in w.store.stream_events(STREAM) if e.kind == "walk.observed"}
    assert events[EMPTY]["contents"] == "seen" and "contents" not in events[PAID]      # the older shape is unchanged when nothing was said


def test_contents_must_be_one_of_the_three_values():
    w = World()
    d = walk.run(w.store, STREAM, walk.Act("s", "start_walk", "staff:front_desk", route=[EMPTY]), WALKED)
    bad = walk.run(w.store, STREAM, walk.Act("o", "observe_unit", "staff:front_desk", about=d.made, unit=EMPTY,
                                              form={"lock": "absent", "tag": "absent", "door": "closed", "contents": "lots"}), WALKED)
    assert (bad.allowed, bad.check) == (False, "contents must be one of: seen, not seen, unknown")


def test_contents_seen_on_a_vacant_unit_is_one_finding_naming_what_staff_saw_and_the_records():
    w = World()
    w.walk(EMPTY, lock="absent", whose="unknown", contents="seen")
    (f,) = w.found()
    assert (f.unit, f.slug, f.role) == (EMPTY, "contents_on_vacant", "office_manager")
    assert "staff saw belongings inside unit A103, which renting lists as vacant" in f.why and "nothing is disposed of" in f.closes
    assert f.key == f"walk:{EMPTY}:contents_on_vacant" and f.lease is None


def test_a_lock_and_contents_on_a_vacant_unit_are_two_findings():
    w = World()
    w.walk(EMPTY, whose="tenant", contents="seen")
    assert slugs(w.found()) == [(EMPTY, "contents_on_vacant"), (EMPTY, "lock_on_vacant")]


def test_contents_not_seen_unknown_or_on_a_rented_unit_are_no_finding_and_unknown_asks_nothing():
    for unit, seen in ((EMPTY, {"lock": "absent", "whose": "unknown", "contents": "not seen"}), (EMPTY, {"lock": "absent", "whose": "unknown"}),
                       (PAID, {"contents": "seen"})):
        w = World()
        w.walk(unit, **seen)
        assert w.found() == [], (unit, seen)


def test_contents_seen_stop_an_offer_until_a_later_look_finds_the_unit_empty():
    w = Floor()
    w.close(w.report(EMPTY, "Broken latch"))
    w.walk(EMPTY, at=WALKED + timedelta(hours=10), lock="absent", whose="unknown", contents="seen")
    (why,) = w.ready(EMPTY, NOW + timedelta(days=1))
    assert why.startswith("a walk finding stands on the unit: belongings seen inside unit A103")
    assert w.reserve(EMPTY).check == why
    w.walk(EMPTY, at=WALKED + timedelta(days=1), lock="absent", whose="unknown", contents="not seen")
    assert w.ready(EMPTY, NOW + timedelta(days=2)) == [] and w.reserve(EMPTY).allowed


def test_the_finding_is_one_item_of_work_that_closes_when_a_later_look_finds_the_unit_empty():
    w = World()
    w.walk(EMPTY, lock="absent", whose="unknown", contents="seen")
    key = f"walk:{EMPTY}:contents_on_vacant"
    work.sweep(w.store, STREAM, NOW)
    books, rent, wk = work.load(w.store, STREAM)
    item = next(i for i in work.queue(books, rent, wk, NOW) if i.key == key)
    assert (item.source, item.state, item.role, item.attest) == ("walk", "open", "office_manager", True)
    w.walk(EMPTY, at=WALKED + timedelta(days=1), lock="absent", whose="unknown", contents="not seen")
    later = NOW + timedelta(days=1)
    books, rent, wk = work.load(w.store, STREAM)
    assert next(i for i in work.queue(books, rent, wk, later) if i.key == key).state == "done"


def test_tilden_keeps_contents_only_when_its_quoted_words_are_in_the_note():
    r = keep(note_answer(unit=("C335", "C335"), contents=("seen", "boxes inside"), lock=("absent", "no lock")),
             note="C335: no lock, boxes inside, door shut")
    assert r.fields["contents"] == "seen" and r.quotes["contents"] == "boxes inside" and r.refusal is None
    made_up = keep(note_answer(unit=("C335", "C335"), contents=("seen", "furniture stacked"), lock=("absent", "no lock")), note="C335: no lock, door shut")
    assert made_up.fields["contents"] == "unknown" and made_up.dropped == ["contents"]
    only = keep(note_answer(unit=("C335", "C335"), contents=("not seen", "empty")), note="C335 is empty")
    assert only.fields["contents"] == "not seen" and only.refusal is None             # a note about contents alone is a note
    assert keep(note_answer(unit=("C335", "C335"))).refusal.startswith("The note does not say whether there was a lock")


def test_tilden_s_contents_reading_goes_into_the_observation_and_the_investigation_table_knows_the_kind():
    from core import walk_reading as wr
    assert "contents" in wr.READ_SCHEMA["properties"] and "contents" in wr.READ_SCHEMA["required"]
    assert "contents_on_vacant" in wr.ALLOWED and set(wr.ALLOWED["contents_on_vacant"]) <= set(wr.STEPS)
    assert "take_lock_off" not in wr.ALLOWED["contents_on_vacant"]
