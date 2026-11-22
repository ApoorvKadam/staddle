"""A damaged door: a door seen damaged is a finding on any unit. On a vacant unit it stops readiness; on a rented unit it is one item for
staff to decide what is done and who is responsible, and it never posts a charge. A repair already tracked by an open work order, or
verified (staff checked, order closed) after staff saw the damage, is not reported again; a repair closed before the sighting is not
proof the new damage was mended. It closes when a later observation shows the door is no longer damaged, or staff say what they checked."""
from datetime import timedelta

from core import walk_reading as wr
from core import work
from tests.test_core_walk_compare import EMPTY, NOW, PAID, SECOND, STREAM, WALKED, World, slugs
from tests.test_core_walk_ready import Floor

MANAGER = "staff:office_manager"


def test_a_damaged_door_on_a_rented_unit_is_one_item_for_staff_to_decide_and_charges_nothing():
    w = World()
    w.walk(PAID, door="damaged", note="Scratches and a dent on the door.")
    (f,) = w.found()
    assert (f.unit, f.slug, f.role, f.tenant) == (PAID, "door_damaged", "office_manager", "Ana Alvarez")
    assert "staff saw the door of unit A101 damaged" in f.why and "rented to Ana Alvarez" in f.why and "who is responsible" in f.why
    assert "nothing is charged from this finding" in f.closes
    assert w.store.journal(STREAM) == []
    work.sweep(w.store, STREAM, NOW)
    books, rent, wk = work.load(w.store, STREAM)
    item = next(i for i in work.queue(books, rent, wk, NOW) if i.key == f"walk:{PAID}:door_damaged")
    assert (item.source, item.state, item.role, item.attest, item.unit) == ("walk", "open", "office_manager", True, PAID)
    assert w.store.journal(STREAM) == [] and not [e for e in w.store.stream_events(STREAM) if e.kind.startswith("charge.")]


def test_a_damaged_door_on_a_vacant_unit_is_a_finding_that_stops_an_offer_until_a_later_look_finds_it_sound():
    w = Floor()
    w.walk(EMPTY, lock="absent", whose="unknown", door="damaged", note="Door will not close securely.")
    (f,) = w.found()
    assert (f.slug, f.tenant, f.lease) == ("door_damaged", None, None) and "The unit is vacant" in f.why
    (why,) = w.ready(EMPTY)
    assert why.startswith("a walk finding stands on the unit: damaged door on unit A103")
    assert w.reserve(EMPTY).check == why
    w.walk(EMPTY, at=WALKED + timedelta(days=1), lock="absent", whose="unknown", door="closed")
    assert w.ready(EMPTY, NOW + timedelta(days=2)) == [] and w.reserve(EMPTY).allowed


def test_the_door_finding_closes_when_a_later_observation_shows_the_door_sound_or_staff_say_what_they_checked():
    w = World()
    w.walk(PAID, door="damaged")
    key = f"walk:{PAID}:door_damaged"
    work.sweep(w.store, STREAM, NOW)
    d = work.run(w.store, STREAM, work.Act("f1", "finish_work", MANAGER, key, text="Looked again: it is an old scuff, not damage to charge for"), NOW)
    assert d.allowed
    w2 = World()
    w2.walk(PAID, door="damaged")
    work.sweep(w2.store, STREAM, NOW)
    w2.walk(PAID, at=WALKED + timedelta(days=1))
    books, rent, wk = work.load(w2.store, STREAM)
    assert next(i for i in work.queue(books, rent, wk, NOW + timedelta(days=1)) if i.key == key).state == "done"


def test_a_repair_already_open_is_not_reported_again_and_one_closed_after_the_sighting_settles_it():
    w = Floor()
    w.walk(EMPTY, lock="absent", whose="unknown", door="damaged")
    oid = w.report(EMPTY, "Door bent")
    assert w.found() == [] and w.ready(EMPTY)[0].startswith("work order")             # the order tracks it and stops the offer
    w.close(oid)
    assert w.found(now=NOW + timedelta(days=1)) == [] and w.ready(EMPTY, NOW + timedelta(days=1)) == []


def test_a_repair_closed_before_the_damage_was_seen_does_not_settle_it():
    w = Floor()
    w.close(w.report(EMPTY, "Door repair"))
    w.walk(EMPTY, at=WALKED + timedelta(hours=10), lock="absent", whose="unknown", door="damaged")
    assert slugs(w.found()) == [(EMPTY, "door_damaged")]
    assert w.ready(EMPTY, NOW + timedelta(days=1))[0].startswith("a walk finding stands")


def test_a_damaged_door_is_a_finding_whatever_the_unit_is_and_a_damaged_look_is_not_an_open_door():
    w = Floor()
    assert w.reserve(SECOND).allowed
    w.walk(SECOND, lock="absent", whose="unknown", door="damaged")
    assert slugs(w.found()) == [(SECOND, "door_damaged")]
    assert "The unit is reserved" in w.found()[0].why


def test_the_investigation_table_knows_the_damaged_door_and_never_offers_to_take_a_lock_off_for_it():
    assert set(wr.ALLOWED["door_damaged"]) == {"look_again", "work_order", "hand_to_manager"} and set(wr.ALLOWED["door_damaged"]) <= set(wr.STEPS)
