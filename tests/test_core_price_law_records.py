"""The records the price-law gate reads: versioned rule sets, the facility's legal profile, lease terms taken from the signed agreement,
disclosed maximums, emergency restrictions and their verification, notice service evidence and recorded decisions."""
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone

import pytest

from core import price_law
from core.billing import Ledger, Refused
from core.books import Books

NOW = datetime(2026, 11, 23, 15, tzinfo=timezone.utc)
D = date


def replayed(led: Ledger) -> Books:
    books = Books()
    for event in led.pending:
        books.apply(event.kind, event.at, event.payload)
    return books


def test_every_rule_set_carries_its_citation_dates_and_review_status():
    assert price_law.RULE_SETS
    for rs in price_law.RULE_SETS:
        assert rs.citation and rs.name and rs.source
        assert isinstance(rs.effective_from, date) and rs.version >= 1
        assert rs.review in ("reviewed", "demonstration", "pending")
        assert rs.topic in ("notice", "disclosure", "emergency")
    keys = [(rs.id, rs.version) for rs in price_law.RULE_SETS]
    assert len(keys) == len(set(keys))


def test_a_rule_set_is_immutable_and_its_hash_follows_its_content():
    rs = price_law.RULE_SETS[0]
    with pytest.raises(FrozenInstanceError):
        rs.notice_days = 1
    from dataclasses import replace
    assert replace(rs, version=rs.version + 1).approval_hash != rs.approval_hash
    assert replace(rs).approval_hash == rs.approval_hash


def test_the_modeled_places_and_what_each_covers():
    places = {(rs.place, rs.topic) for rs in price_law.RULE_SETS}
    assert {("CA", "notice"), ("CA", "disclosure"), ("CA", "emergency"), ("UT", "notice"), ("NYC", "notice"), ("NYC", "disclosure"),
            ("FL", "emergency")} <= places
    ca = price_law.rules_in_force("CA", D(2026, 11, 23))
    assert {rs.topic for rs in ca} == {"notice", "disclosure", "emergency"}
    # California's disclosure rule for new agreements starts on January 1, 2026
    assert not [rs for rs in price_law.rules_in_force("CA", D(2025, 12, 31)) if rs.topic == "disclosure"]
    assert [rs for rs in price_law.rules_in_force("CA", D(2026, 1, 1)) if rs.topic == "disclosure"]
    assert price_law.rules_in_force("TX", D(2026, 11, 23)) == []


@pytest.mark.parametrize("city, place", [("Orlando, FL", "FL"), ("Sacramento, CA", "CA"), ("Salt Lake City, UT", "UT"), ("New York, NY", "NYC"),
                                         ("Brooklyn, NY", "NYC"), ("Buffalo, NY", "NY"), ("Austin, TX", "TX"), ("Orlando", None), ("", None)])
def test_a_facility_city_gives_its_place_and_new_york_state_is_not_the_city(city, place):
    assert price_law.place_of(city) == place


def test_the_live_facility_is_orlando_and_a_stated_profile_replaces_the_derived_one():
    led = Ledger()
    led.set_up(NOW, "Northstar Storage", "Orlando, FL", ["A1"])
    derived = price_law.profile_of(led.books)
    assert (derived.place, derived.synthetic, derived.city) == ("FL", False, "Orlando, FL")
    price_law.set_profile(led, NOW, place="FL", reviewed_by="staff:manager")
    assert price_law.profile_of(replayed(led)).place == "FL"
    with pytest.raises(Refused):
        price_law.set_profile(led, NOW, place="", reviewed_by="staff:manager")


def test_lease_terms_are_a_snapshot_of_the_signed_agreement_and_a_change_is_a_new_version():
    led = Ledger()
    first = price_law.record_terms(led, NOW, lease="L0001", signed_on=D(2026, 3, 2), agreement_sha256="a" * 64, notice_methods=["first_class_mail"],
                                   notice_days=None, email_notice_authorized=False, last_known_address="1 Main St, Orlando, FL")
    second = price_law.record_terms(led, NOW, lease="L0001", signed_on=D(2026, 3, 2), agreement_sha256="b" * 64, notice_methods=["first_class_mail", "email"],
                                    notice_days=30, email_notice_authorized=True, last_known_email="t@example.com", price_promise_until=D(2026, 9, 1))
    books = replayed(led)
    history = books.price_law.terms["L0001"]
    assert [t.version for t in history] == [1, 2] and (first, second) == (1, 2)
    assert price_law.terms_of(books, "L0001").agreement_sha256 == "b" * 64
    assert history[0].agreement_sha256 == "a" * 64
    with pytest.raises(FrozenInstanceError):
        history[0].notice_days = 5


def test_terms_refuse_what_is_not_an_agreement_without_writing():
    led = Ledger()
    for args in ({"agreement_sha256": "short"}, {"notice_methods": ["carrier pigeon"]}, {"notice_days": -1}, {"signed_on": "yesterday"}):
        with pytest.raises(Refused):
            price_law.record_terms(led, NOW, **{"lease": "L0001", "signed_on": D(2026, 3, 2), "agreement_sha256": "a" * 64, "notice_methods": [], **args})
    assert not led.pending and not led.books.price_law


def test_a_disclosed_maximum_names_its_period_and_evidence():
    led = Ledger()
    ident = price_law.record_commitment(led, NOW, lease="L0001", kind="first_year", max_cents=15000, covers_from=D(2026, 3, 2), covers_to=D(2027, 3, 1),
                                        disclosed_on=D(2026, 3, 2), evidence_sha256="c" * 64)
    com = replayed(led).price_law.commitments["L0001"][0]
    assert (com.id, com.kind, com.max_cents, com.covers_to) == (ident, "first_year", 15000, D(2027, 3, 1))
    for args in ({"max_cents": 0}, {"kind": "yearly"}, {"covers_to": D(2026, 3, 1)}, {"evidence_sha256": ""}):
        with pytest.raises(Refused):
            price_law.record_commitment(led, NOW, **{"lease": "L0001", "kind": "first_year", "max_cents": 15000, "covers_from": D(2026, 3, 2),
                                                     "covers_to": D(2027, 3, 1), "disclosed_on": D(2026, 3, 2), "evidence_sha256": "c" * 64, **args})
    assert len(led.pending) == 1


def test_an_emergency_restriction_and_its_verification_are_separate_records_and_the_verification_expires():
    led = Ledger()
    rid = price_law.record_emergency(led, NOW, place="CA", name="Declared emergency, example county", source="Governor's proclamation, read 2026-11-20",
                                     starts=D(2026, 11, 10), ends=D(2026, 12, 10), products=["storage"], scope="Example County")
    chk = price_law.record_emergency_check(led, NOW, place="CA", checked_on=D(2026, 11, 22), valid_until=D(2026, 11, 29), by="staff:manager",
                                           source="state emergency page", active=[rid])
    state = replayed(led).price_law
    assert state.emergencies[rid].ends == D(2026, 12, 10) and state.checks[-1].id == chk
    assert state.checks[-1].valid_until == D(2026, 11, 29)
    with pytest.raises(Refused):
        price_law.record_emergency_check(led, NOW, place="CA", checked_on=D(2026, 11, 22), valid_until=D(2026, 11, 21), by="staff:manager",
                                         source="state emergency page", active=[])
    with pytest.raises(Refused):
        price_law.record_emergency_check(led, NOW, place="CA", checked_on=D(2026, 11, 22), valid_until=D(2026, 11, 29), by="staff:manager",
                                         source="state emergency page", active=["ER9999"])


def test_notice_service_keeps_each_stage_apart():
    led = Ledger()
    ident = price_law.record_notice(led, NOW, change="PC1", lease="L0001", document_sha256="d" * 64, recipient="Pat Tenant", channel="email",
                                    sent_to="t@example.com", attempted_on=D(2026, 11, 20), dispatch_accepted=True, by="staff:front_desk")
    notice = replayed(led).price_law.notices["PC1"][0]
    assert (notice.id, notice.channel, notice.dispatch_accepted) == (ident, "email", True)
    assert (notice.delivered_on, notice.acknowledged_on, notice.confirmation) == (None, None, None)
    with pytest.raises(Refused):
        price_law.record_notice(led, NOW, change="PC1", lease="L0001", document_sha256="x", recipient="Pat", channel="email", sent_to="t@example.com",
                                attempted_on=D(2026, 11, 20), dispatch_accepted=True, by="staff:front_desk")


def test_a_recorded_decision_names_its_reasons_records_and_rule_version_and_replays():
    led = Ledger()
    reason = price_law.Reason("NOTICE_PERIOD_TOO_SHORT", "refuse", "California requires 30 days' written notice.", "CA-notice", 1)
    ident = price_law.record_decision(led, NOW, change="PC1", point="proposal", outcome="refuse", reasons=[reason], records=["terms:L0001#1"],
                                      versions={"CA-notice": 1}, lease="L0001", invoice=None)
    rec = replayed(led).price_law.decisions[-1]
    assert (rec.id, rec.outcome, rec.versions, rec.records) == (ident, "refuse", {"CA-notice": 1}, ("terms:L0001#1",))
    assert rec.reasons[0].code == "NOTICE_PERIOD_TOO_SHORT" and rec.reasons[0].cite == "CA-notice"
    with pytest.raises(Refused):
        price_law.record_decision(led, NOW, change="PC1", point="whenever", outcome="allow", reasons=[], records=[], versions={}, lease=None, invoice=None)
