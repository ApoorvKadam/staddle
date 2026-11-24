from dataclasses import replace
"""The made-up facilities the tests and the evaluation use for California, Utah and New York City, and Florida's ordinary increase, which
is the signed agreement's own rule and not a statute."""
from datetime import date

import pytest

from core import price_law as pl
from core.billing import Ledger
from core.books import at_local

SHA = "e" * 64
TODAY = date(2026, 11, 23)
NOW = at_local(TODAY, 10)
ADDRESS = "7 Orange Ave, Orlando, FL 32801"


def northstar() -> Ledger:
    led = Ledger()
    led.set_up(NOW, "Northstar Storage", "Orlando, FL", ["A1"])
    pl.record_emergency_check(led, NOW, place="FL", checked_on=TODAY, valid_until=date(2027, 3, 31), by="staff:manager", source="state emergency page", active=[])
    return led


def terms(led, **over):
    args = {"lease": "L0001", "signed_on": date(2026, 3, 2), "agreement_sha256": SHA, "notice_methods": ["first_class_mail"], "notice_days": 30,
            "last_known_address": ADDRESS}
    return pl.record_terms(led, NOW, **{**args, **over})


def change(**over):
    base = {"id": "PC1", "kind": "lease_increase", "current_cents": 10000, "new_cents": 10500, "effective_on": date(2026, 12, 23), "lease": "L0001"}
    return pl.PriceChange(**{**base, **over})


def notice(led, at=NOW, **over):
    args = {"new_cents": 10500, "effective_on": date(2026, 12, 23), "change": "PC1", "lease": "L0001", "document_sha256": SHA, "recipient": "Pat Tenant", "channel": "first_class_mail", "sent_to": ADDRESS,
            "attempted_on": TODAY, "dispatch_accepted": True, "certificate_of_mailing": True, "postage_prepaid": True, "by": "staff:front_desk"}
    return pl.record_notice(led, at, **{**args, **over})


def check(led, point="activation", at=at_local(date(2026, 12, 24), 10), **over):
    return pl.evaluate(led.books, change(**over), at, point=point)


def test_the_made_up_facilities_are_flagged_and_the_live_one_is_orlando():
    assert set(pl.SYNTHETIC_PROFILES) == {"CA", "UT", "NYC"}
    for place, profile in pl.SYNTHETIC_PROFILES.items():
        assert profile.synthetic and profile.place == place and "made up" in profile.reviewed_by
        assert pl.place_of(profile.city) == place
        assert pl.synthetic_profile(place) is profile
    with pytest.raises(KeyError):
        pl.synthetic_profile("FL")
    live = pl.profile_of(northstar().books)
    assert (live.place, live.synthetic, live.facility) == ("FL", True, "Northstar Storage")


def test_only_the_recorded_demo_profile_is_synthetic():
    led = northstar()
    pl.set_profile(led, NOW, place="FL", reviewed_by="staff:manager")
    assert pl.profile_of(led.books).synthetic
    other = Ledger()
    other.set_up(NOW, "Real Storage", "Orlando, FL", ["A1"])
    pl.set_profile(other, NOW, place="FL", reviewed_by="staff:manager")
    assert not pl.profile_of(other.books).synthetic
    pl.set_profile(led, NOW, place="CA", reviewed_by="staff:manager")
    assert not pl.profile_of(led.books).synthetic


def test_florida_rule_defers_to_the_agreement_and_states_no_statewide_period():
    rule = next(rs for rs in pl.rules_in_force("FL", TODAY) if rs.topic == "notice")
    assert (rule.review, rule.agreement_specific, rule.notice_days) == ("demonstration", True, None)
    assert "30" not in rule.name and "statute" not in rule.name.lower()
    assert pl.required_notice_days(Ledger().books, [rule], None) is None


def test_a_florida_increase_follows_the_notice_the_agreement_states():
    led = northstar()
    terms(led)
    notice(led)
    res = check(led)
    assert res.outcome == "allow" and res.versions["FL-notice"] == 1
    short_ledger = northstar()
    terms(short_ledger)
    notice(short_ledger, at=at_local(date(2026, 11, 24), 10), attempted_on=date(2026, 11, 24))
    short = check(short_ledger, at=at_local(date(2026, 12, 23), 10))
    assert [r.code for r in short.reasons] == ["NOTICE_PERIOD_TOO_SHORT"]


def test_florida_says_the_agreement_requires_the_days_and_never_names_a_statute_for_them():
    led = northstar()
    terms(led, notice_days=45)
    notice(led)
    res = check(led)
    words = res.reasons[0].words
    assert res.reasons[0].code == "NOTICE_PERIOD_TOO_SHORT" and "signed agreement requires 45 days" in words and "30 days" not in words
    assert "Florida requires" not in words


def test_a_florida_agreement_that_states_no_notice_period_means_no_activated_increase():
    led = northstar()
    terms(led, notice_days=None)
    notice(led)
    res = check(led)
    assert (res.outcome, [r.code for r in res.reasons]) == ("hold", ["LEASE_TERMS_MISSING"])
    for point in ("proposal", "activation"):
        assert pl.evaluate(led.books, change(lease="L9999"), NOW, point=point).outcome == "hold"


def test_a_florida_agreement_with_no_way_of_giving_notice_listed_holds_and_one_that_lists_others_refuses():
    led = northstar()
    terms(led, notice_methods=[])
    notice(led)
    assert [r.code for r in check(led).reasons] == ["LEASE_TERMS_MISSING"]
    led = northstar()
    terms(led, notice_methods=["email"], email_notice_authorized=False, last_known_email="pat@example.com")
    notice(led)
    res = check(led)
    assert [r.code for r in res.reasons] == ["NOTICE_METHOD_NOT_PERMITTED"] and "agreement" in res.reasons[0].words
    led = northstar()
    terms(led, notice_methods=["email"], email_notice_authorized=True, last_known_email="pat@example.com")
    notice(led, channel="email", sent_to="pat@example.com", certificate_of_mailing=False, postage_prepaid=False)
    assert check(led).outcome == "allow"


def test_rules_are_demonstrations_with_no_claim_of_counsel_review():
    assert {r.id for r in pl.RULE_SETS if r.place == "FL"} == {"FL-notice", "FL-emergency"}
    for rule in pl.RULE_SETS:
        assert rule.review == "demonstration" and rule.reviewed_on is None
        assert "counsel review pending" in rule.reviewer.lower()
        assert "compliant" not in str(rule).lower()


def test_non_demo_florida_facility_holds_for_unreviewed_rules():
    led = Ledger()
    led.set_up(NOW, "Real Storage", "Orlando, FL", ["A1"])
    pl.set_profile(led, NOW, place="FL", reviewed_by="staff:manager")
    pl.record_emergency_check(led, NOW, place="FL", checked_on=TODAY, valid_until=date(2027, 3, 31), by="staff:manager", source="state page", active=[])
    result = pl.evaluate(led.books, change(kind="street_rate", lease=None), NOW)
    reasons = [r for r in result.reasons if r.code == "RULE_NOT_REVIEWED"]
    assert result.outcome == "hold" and reasons and all("not reviewed" in r.words for r in reasons)


def test_unrepresentable_agreement_notice_period_is_rejected_and_legacy_evidence_holds():
    led = northstar()
    with pytest.raises(pl.Refused):
        terms(led, notice_days=1000000000)
    assert not led.books.price_law.terms
    terms(led, notice_days=365)
    history = led.books.price_law.terms["L0001"]
    history[-1] = replace(history[-1], notice_days=1000000000)
    result = pl.evaluate(led.books, change(), NOW, point="proposal")
    assert result.outcome == "hold"
    assert "LEASE_TERMS_MISSING" in [reason.code for reason in result.reasons]
