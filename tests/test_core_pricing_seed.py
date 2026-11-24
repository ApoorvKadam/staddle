"""The seeded year's pricing: a plain build is unchanged, and the live copy gives every unit of a size one asking rate."""
import hashlib
import json
from collections import defaultdict

from core import seed
from core.books import at_local


def digest(year):
    rows = [json.dumps([e.kind, str(e.at), e.payload, e.source], sort_keys=True, default=str) for e in year.led.pending]
    return len(rows), hashlib.sha256("\n".join(rows).encode()).hexdigest()[:16]


def test_plain_build_is_unchanged_by_the_live_copy_pricing():
    # measured before the live copy's pricing changed (2026-11-24): the plain build's events, count and digest
    assert digest(seed.build()) == (3241, "eec4e9c635f3f438")


def test_live_copy_owner_limits_cover_every_size_around_its_rate():
    year = seed.build(standing=True, today=seed.TODAY, cutoff=at_local(seed.TODAY))
    limits = {e.payload["size"]: e for e in year.led.pending if e.kind == "street_rate.limits"}
    assert set(limits) == set(seed.SIZES.values())
    for row, size in seed.SIZES.items():
        rate, event = seed.SIZE_RATES[row], limits[size]
        assert event.source == "owner"
        band = event.payload["limits"]
        assert band["floor_cents"] < rate < band["ceiling_cents"], size
        assert 0 < band["max_up_cents"] < rate and 0 < band["max_down_cents"] < rate, size


def test_live_copy_size_rate_is_allowed_by_florida_rules_from_proposal_to_publication():
    from core import renting, street_rates
    from core.store import BASE, Store
    now = at_local(seed.TODAY, 10)
    store = Store()
    with store.write():
        store.append(BASE, seed.build(standing=True, today=seed.TODAY, cutoff=at_local(seed.TODAY)).led.pending)
    _, rent = renting.load(store, BASE)
    current = next(u.rent_cents for u in rent.units.values() if u.size == "5x10")
    limits = street_rates._limits(store, BASE, "5x10")
    assert limits is not None
    staff = "staff:office_manager"
    proposal = street_rates.propose(store, BASE, now, size="5x10", current_cents=current, new_cents=current + 500,
                                    reason="Seasonal price", by=staff, limits=limits)
    assert proposal.allowed, proposal.check
    assert street_rates.approve(store, BASE, now, proposal.made, by=staff).allowed
    assert street_rates.publish(store, BASE, now, proposal.made, by=staff).allowed
    decisions = [e.payload["outcome"] for e in store.events(BASE)
                 if e.kind == "price_law.decided" and e.payload["change"] == proposal.made]
    assert len(decisions) == 3 and set(decisions) == {"allow"}, decisions
    _, rent = renting.load(store, BASE)
    assert {u.rent_cents for u in rent.units.values() if u.size == "5x10"} == {current + 500}


def test_live_copy_gives_every_unit_of_a_size_the_same_asking_rate():
    year = seed.build(standing=True, today=seed.TODAY, cutoff=at_local(seed.TODAY))
    rents = defaultdict(set)
    for event in year.led.pending:
        if event.kind == "unit.priced":
            rents[event.payload["size"]].add(event.payload["rent_cents"])
    assert set(rents) == set(seed.SIZES.values())
    for row, size in seed.SIZES.items():
        assert rents[size] == {seed.SIZE_RATES[row]}, size
