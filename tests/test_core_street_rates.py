from datetime import date

from core import renting, street_rates
from core.billing import Ledger
from core.books import at_local
from core.store import BASE, Store

NOW = at_local(date(2026, 11, 23), 10)


def facility(name="Northstar Storage"):
    store = Store()
    led = Ledger()
    led.set_up(NOW, name, "Orlando, FL", ["A1", "A2"])
    with store.write():
        store.append(BASE, led.pending)
    for unit in ("A1", "A2"):
        assert renting.run(store, BASE, renting.Act("price-" + unit, "price_unit", "staff:manager", unit,
                           args={"size": "5x10", "rent_cents": 10000}), NOW).allowed
    return store


def test_proposal_approval_and_publication_keep_versions():
    store = facility()
    limits = street_rates.Limits(2000, 2000, 8000, 12000)
    assert street_rates.set_limits(store, BASE, NOW, size="5x10", limits=limits, by="owner").allowed
    proposal = street_rates.propose(store, BASE, NOW, size="5x10", current_cents=10000, new_cents=9000,
                                    reason="Seasonal rate", by="staff:manager", limits=limits)
    assert proposal.allowed
    assert street_rates.approve(store, BASE, NOW, proposal.made, by="staff:manager").allowed
    assert street_rates.publish(store, BASE, NOW, proposal.made, by="staff:manager").allowed
    history = street_rates.history(store, BASE)
    assert [version.status for version in history] == ["proposed", "approved", "published"]
    assert [version.version for version in history] == [1, 2, 3]
    assert all(unit.rent_cents == 9000 for unit in renting.load(store, BASE)[1].units.values())


def test_owner_limits_and_legal_hold_are_enforced_before_a_proposal():
    store = facility()
    limits = street_rates.Limits(500, 500, 9500, 10500)
    assert street_rates.set_limits(store, BASE, NOW, size="5x10", limits=limits, by="owner").allowed
    denied = street_rates.propose(store, BASE, NOW, size="5x10", current_cents=10000, new_cents=9000,
                                  reason="Test", by="staff:manager", limits=street_rates.Limits(5000, 5000, 1, 20000))
    assert not denied.allowed and "owner" in denied.check.lower()
    held = street_rates.propose(store, BASE, NOW, size="5x10", current_cents=10000, new_cents=10400,
                                reason="Test", by="staff:manager", limits=limits)
    assert not held.allowed and "emergency" in held.check.lower()
    assert street_rates.history(store, BASE) == []


def test_non_demo_rules_hold_and_publication_rechecks_rules():
    store = facility("Example Storage")
    limits = street_rates.Limits(2000, 2000, 8000, 12000)
    assert street_rates.set_limits(store, BASE, NOW, size="5x10", limits=limits, by="owner").allowed
    result = street_rates.propose(store, BASE, NOW, size="5x10", current_cents=10000, new_cents=9000,
                                  reason="Test", by="staff:manager", limits=limits)
    assert not result.allowed and "not reviewed" in result.check
    assert not street_rates.history(store, BASE)


def test_publication_rechecks_emergency_status():
    store = facility()
    limits = street_rates.Limits(2000, 2000, 8000, 12000)
    assert street_rates.set_limits(store, BASE, NOW, size="5x10", limits=limits, by="owner").allowed
    from core import price_law
    with store.write():
        led = Ledger(store.books(BASE))
        price_law.record_emergency_check(led, NOW, place="FL", checked_on=NOW.date(), valid_until=NOW.date(),
                                         by="staff:manager", source="Example", active=[])
        store.append(BASE, led.pending)
    from datetime import timedelta
    p = street_rates.propose(store, BASE, NOW, size="5x10", current_cents=10000, new_cents=10500,
                             reason="Test", by="staff:manager", limits=limits, effective_on=NOW.date() + timedelta(days=1))
    assert p.allowed
    assert street_rates.approve(store, BASE, NOW, p.made, by="staff:manager").allowed
    denied = street_rates.publish(store, BASE, NOW + timedelta(days=1), p.made, by="staff:manager")
    assert not denied.allowed and "expired" in denied.check
    assert [v.status for v in street_rates.history(store, BASE)] == ["proposed", "approved"]
    assert all(u.rent_cents == 10000 for u in renting.load(store, BASE)[1].units.values())


def test_publication_preserves_accepted_quote_and_existing_lease():
    from tests.test_core_renting import accept, checkout, rented
    store = Store()
    led = Ledger()
    led.set_up(NOW, "Northstar Storage", "Orlando, FL", ["A1", "A2", "A3"])
    tenants = [led.add_tenant(NOW, name, phone) for name, phone in
               (("Dana", "+14075550101"), ("Eli", "+14075550102"), ("Fay", "+14075550103"))]
    with store.write():
        store.append(BASE, led.pending)
    for unit in ("A1", "A2", "A3"):
        assert renting.run(store, BASE, renting.Act("price-" + unit, "price_unit", "staff:manager", unit,
                           args={"size": "5x10", "rent_cents": 10000}), NOW).allowed
    active = rented(store, tenants[0], "A1", NOW)
    pending = checkout(store, tenants[1], "A2", NOW)
    assert accept(store, tenants[1], pending, NOW).allowed
    before = renting.load(store, BASE)[1]
    old_quote = before.rentals[pending].quote.copy()
    lease = before.rentals[active].lease
    old_rent = store.books(BASE).leases[lease].rent_cents
    limits = street_rates.Limits(2000, 2000, 8000, 12000)
    assert street_rates.set_limits(store, BASE, NOW, size="5x10", limits=limits, by="owner").allowed
    p = street_rates.propose(store, BASE, NOW, size="5x10", current_cents=10000, new_cents=9000,
                             reason="Seasonal", by="staff:manager", limits=limits)
    assert p.allowed
    assert street_rates.approve(store, BASE, NOW, p.made, by="staff:manager").allowed
    assert street_rates.publish(store, BASE, NOW, p.made, by="staff:manager").allowed
    after = renting.load(store, BASE)[1]
    assert after.rentals[pending].accepted and after.rentals[pending].quote == old_quote
    assert store.books(BASE).leases[lease].rent_cents == old_rent == 10000
    new = checkout(store, tenants[2], "A3", NOW)
    assert renting.load(store, BASE)[1].rentals[new].quote["rent_cents"] == 9000


def test_held_and_refused_proposals_open_today_items_with_reasons():
    from core import work
    store = facility("Example Storage")
    limits = street_rates.Limits(2000, 2000, 8000, 12000)
    assert street_rates.set_limits(store, BASE, NOW, size="5x10", limits=limits, by="owner").allowed
    held = street_rates.propose(store, BASE, NOW, size="5x10", current_cents=10000, new_cents=9000,
                                reason="Test", by="staff:manager", limits=limits)
    refused = street_rates.propose(store, BASE, NOW, size="5x10", current_cents=10000, new_cents=7000,
                                   reason="Test", by="staff:manager", limits=limits)
    books, rent, wk = work.load(store, BASE)
    items = [i for i in work.open_items(work.queue(books, rent, wk, NOW)) if i.source == "street_rate"]
    assert len(items) == 2
    assert {item.why for item in items} == {held.check, refused.check}
    assert all(item.attest for item in items)


def test_rate_can_only_be_published_on_approved_effective_day():
    from datetime import timedelta
    store = facility()
    limits = street_rates.Limits(2000, 2000, 8000, 12000)
    assert street_rates.set_limits(store, BASE, NOW, size="5x10", limits=limits, by="owner").allowed
    effective = NOW.date() + timedelta(days=1)
    p = street_rates.propose(store, BASE, NOW, size="5x10", current_cents=10000, new_cents=9000,
                             reason="Seasonal", by="staff:manager", limits=limits, effective_on=effective)
    assert p.allowed
    assert street_rates.approve(store, BASE, NOW, p.made, by="staff:manager").allowed
    assert not street_rates.publish(store, BASE, NOW, p.made, by="staff:manager").allowed
    assert street_rates.publish(store, BASE, NOW + timedelta(days=1), p.made, by="staff:manager").allowed


def test_limits_and_staff_approval_are_rechecked():
    store = facility()
    limits = street_rates.Limits(500, 500, 9000, 11000)
    assert not street_rates.set_limits(store, BASE, NOW, size="5x10", limits=limits, by="staff:manager").allowed
    assert street_rates.set_limits(store, BASE, NOW, size="5x10", limits=limits, by="owner").allowed
    for rate in (8999, 11001, 9499, 10501):
        result = street_rates.propose(store, BASE, NOW, size="5x10", current_cents=10000, new_cents=rate,
                                      reason="Test", by="staff:manager", limits=limits)
        assert not result.allowed
    p = street_rates.propose(store, BASE, NOW, size="5x10", current_cents=10000, new_cents=9500,
                             reason="Test", by="staff:manager", limits=limits)
    assert p.allowed
    assert not street_rates.publish(store, BASE, NOW, p.made, by="staff:manager").allowed
    assert not street_rates.approve(store, BASE, NOW, p.made, by="renter").allowed
    stricter = street_rates.Limits(200, 200, 9000, 11000)
    assert street_rates.set_limits(store, BASE, NOW, size="5x10", limits=stricter, by="owner").allowed
    assert not street_rates.approve(store, BASE, NOW, p.made, by="staff:manager").allowed
    assert [v.status for v in street_rates.history(store, BASE)] == ["proposed"]


def test_limit_rejection_still_shows_legal_hold():
    store = facility("Example Storage")
    limits = street_rates.Limits(200, 200, 9000, 11000)
    assert street_rates.set_limits(store, BASE, NOW, size="5x10", limits=limits, by="owner").allowed
    result = street_rates.propose(store, BASE, NOW, size="5x10", current_cents=10000, new_cents=9500,
                                  reason="Test", by="staff:manager", limits=limits)
    assert not result.allowed and "step limit" in result.check and "not reviewed" in result.check
