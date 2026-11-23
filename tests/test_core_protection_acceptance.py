"""Offers and acceptance. An offer names the version, price, lease and unit. Acceptance is its own record and must be explicit and
attributable: who accepted, how, their words, the version, price, lease and unit, and the date coverage starts. An offer, a price-list
change, a staff tick, a statement line or a note is never acceptance."""
from datetime import date

import pytest

from core import protection
from core.billing import Refused
from tests.protection_world import FRONT, MANAGER, TERMS_V2, World, day


def test_an_offer_names_the_version_price_lease_and_unit_and_is_not_acceptance():
    w = World()
    e = w.offer(0, day(11, 2))
    rec = w.state().enrolments[e]
    assert (rec.state, rec.version, rec.cents, rec.lease, rec.unit, rec.tenant) == ("offered", 1, 1200, w.leases[0], "A101", w.tenants[0])
    assert (rec.offered_by, rec.offered_at) == (FRONT, day(11, 2)) and rec.acceptance is None and w.state().acceptances == {}


def test_acceptance_records_who_how_their_words_and_the_exact_terms():
    w = World()
    e = w.offer(0, day(11, 2))
    a = w.accept(e, day(11, 3), starts=date(2026, 11, 3), how="tenant message", words="Yes, add the protection plan for $12 a month.")
    acc, rec = w.state().acceptances[a], w.state().enrolments[e]
    assert (acc.by, acc.how, acc.words, acc.version, acc.cents, acc.lease, acc.unit, acc.starts, acc.at) == (
        w.tenants[0], "tenant message", "Yes, add the protection plan for $12 a month.", 1, 1200, w.leases[0], "A101", date(2026, 11, 3), day(11, 3))
    assert (rec.state, rec.acceptance) == ("accepted", a)


def test_a_price_list_change_and_a_staff_tick_and_a_note_are_never_acceptance():
    w = World()
    e = w.offer(0, day(11, 2))
    w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1500, TERMS_V2, date(2026, 11, 6), FRONT, day(11, 6)))
    w.act(lambda led: protection.note(led, e, "staff tick", "customer verbally agreed", FRONT, day(11, 3)))
    w.act(lambda led: protection.note(led, e, "note", "tenant seemed happy with it", FRONT, day(11, 4)))
    rec = w.state().enrolments[e]
    assert rec.state == "offered" and rec.acceptance is None and w.state().acceptances == {}
    assert [(n["kind"], n["text"]) for n in rec.notes] == [("staff tick", "customer verbally agreed"), ("note", "tenant seemed happy with it")]


@pytest.mark.parametrize("over, why", [
    ({"how": "staff tick"}, "explicit"),
    ({"how": "verbal"}, "explicit"),
    ({"words": "  "}, "words"),
    ({"by": "staff:front_desk"}, "tenant"),
    ({"by": "T0003"}, "tenant"),
    ({"version": 2}, "other terms"),
    ({"cents": 1500}, "other terms"),
    ({"starts": date(2026, 10, 1)}, "before the lease"),
])
def test_acceptance_that_is_not_explicit_attributable_or_for_these_terms_is_refused(over, why):
    w = World()
    e = w.offer(0, day(11, 2))
    w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1500, TERMS_V2, date(2026, 11, 6), FRONT, day(11, 6)))
    before = len(w.store.stream_events("visitor:a"))
    with pytest.raises(Refused, match=why):
        w.accept(e, day(11, 3), **over)
    assert len(w.store.stream_events("visitor:a")) == before and w.state().enrolments[e].state == "offered"


def test_a_second_acceptance_of_the_same_offer_is_refused():
    w = World()
    e = w.offer(0, day(11, 2))
    w.accept(e, day(11, 3))
    with pytest.raises(Refused, match="already"):
        w.accept(e, day(11, 4))


def test_an_offer_is_for_a_lease_that_is_active_and_has_no_other_open_offer():
    w = World()
    e = w.offer(0, day(11, 2))
    with pytest.raises(Refused, match="offer is already open") as refused:
        w.offer(0, day(11, 3))
    assert e not in str(refused.value)      # the words name no record
    with pytest.raises(Refused, match="no lease"):
        w.act(lambda led: protection.offer(led, "L9999", FRONT, day(11, 3)))
    w.act(lambda led: led.end_lease(day(11, 4), w.leases[1]))
    with pytest.raises(Refused, match="ended"):
        w.offer(1, day(11, 5))


def test_an_offer_defaults_to_the_version_in_force_and_may_name_an_earlier_one():
    w = World()
    w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1500, TERMS_V2, date(2026, 11, 6), FRONT, day(11, 6)))
    early, late, named = w.offer(0, day(11, 5)), w.offer(1, day(11, 7)), w.offer(2, day(11, 7), version=1)
    enrolments = w.state().enrolments
    assert enrolments[early].version == 1                                     # version 2 is not in force yet
    assert enrolments[late].version == 2
    assert enrolments[named].cents == 1200


def test_the_offer_and_acceptance_are_read_back_after_a_restart():
    w = World()
    e = w.offer(0, day(11, 2))
    a = w.accept(e, day(11, 3))
    again = protection.state(w.store.books("visitor:a"))
    assert again.enrolments[e].acceptance == a and again.acceptances[a].by == w.tenants[0]
    assert MANAGER not in str(again.acceptances[a])
