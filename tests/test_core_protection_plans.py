"""Plan products: versioned (product, version, monthly price, terms text, effective date). A new price or new terms is a new version; the
old one stays in the history."""
from datetime import date

import pytest

from core import protection
from core.billing import Refused
from tests.protection_world import FRONT, TERMS_V1, TERMS_V2, World, day


def test_the_first_version_is_on_record_with_its_price_terms_and_date():
    w = World()
    (v,) = w.state().versions[protection.PRODUCT]
    assert (v.version, v.cents, v.terms, v.effective, v.by) == (1, 1200, TERMS_V1, date(2026, 10, 1), FRONT)


def test_a_new_price_is_a_new_version_and_the_old_one_stays_in_the_history():
    w = World()
    v = w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1500, TERMS_V1, date(2026, 11, 6), FRONT, day(11, 6)))
    assert v == 2
    old, new = w.state().versions[protection.PRODUCT]
    assert (old.version, old.cents, old.effective) == (1, 1200, date(2026, 10, 1))
    assert (new.version, new.cents, new.effective) == (2, 1500, date(2026, 11, 6))


def test_new_terms_at_the_same_price_are_a_new_version_too():
    w = World()
    assert w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1200, TERMS_V2, date(2026, 11, 6), FRONT, day(11, 6))) == 2
    assert [v.terms for v in w.state().versions[protection.PRODUCT]] == [TERMS_V1, TERMS_V2]


@pytest.mark.parametrize("cents, terms, effective, why", [
    (1200, TERMS_V1, date(2026, 11, 6), "already has this price"),
    (0, TERMS_V2, date(2026, 11, 6), "price"),
    (1500, "  ", date(2026, 11, 6), "terms"),
    (1500, TERMS_V2, date(2026, 9, 1), "before"),
])
def test_a_version_that_changes_nothing_or_is_malformed_is_refused_and_writes_nothing(cents, terms, effective, why):
    w = World()
    before = len(w.store.stream_events("visitor:a"))
    with pytest.raises(Refused, match=why):
        w.act(lambda led: protection.publish_version(led, protection.PRODUCT, cents, terms, effective, FRONT, day(11, 6)))
    assert len(w.store.stream_events("visitor:a")) == before and len(w.state().versions[protection.PRODUCT]) == 1


def test_the_versions_are_read_back_after_a_restart():
    w = World()
    w.act(lambda led: protection.publish_version(led, protection.PRODUCT, 1500, TERMS_V2, date(2026, 11, 6), FRONT, day(11, 6)))
    again = World.__new__(World)
    again.store = w.store
    assert [v.version for v in protection.state(again.store.books("visitor:a")).versions[protection.PRODUCT]] == [1, 2]
