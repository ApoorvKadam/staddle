"""The one evaluator: which rules cover a price change, what holds it, what the owner may add, and what an earlier approval is worth
after the law, the agreement or an emergency changes."""
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from core import price_law as pl
from core.billing import Ledger, Refused
from core.books import at_local

NOW = at_local(date(2026, 11, 23), 10)
TODAY = date(2026, 11, 23)
SHA = "e" * 64
CA = pl.FacilityProfile("Synthetic California facility", "Sacramento, CA", "CA", synthetic=True)
UT = pl.FacilityProfile("Synthetic Utah facility", "Salt Lake City, UT", "UT", synthetic=True)
TX = pl.FacilityProfile("A Texas facility", "Austin, TX", "TX")


def book(city="Sacramento, CA") -> Ledger:
    led = Ledger()
    led.set_up(NOW, "Test Storage", city, ["A1"])
    return led


def lease_terms(led, lease="L0001", **over):
    args = {"lease": lease, "signed_on": date(2026, 3, 2), "agreement_sha256": SHA, "notice_methods": ["first_class_mail", "email"], "email_notice_authorized": True}
    return pl.record_terms(led, NOW, **{**args, **over})


def checked(led, place, active=(), valid_until=date(2026, 11, 30), checked_on=TODAY):
    return pl.record_emergency_check(led, NOW, place=place, checked_on=checked_on, valid_until=valid_until, by="staff:manager", source="state emergency page",
                                     active=list(active))


def change(**over):
    base = {"id": "PC1", "kind": "lease_increase", "current_cents": 10000, "new_cents": 10500, "effective_on": date(2027, 1, 15), "lease": "L0001"}
    return pl.PriceChange(**{**base, **over})


def codes(result):
    return [r.code for r in result.reasons]


def test_a_place_no_rule_covers_gets_no_activation_and_only_that_reason():
    led = book("Austin, TX")
    for kind in ("lease_increase", "street_rate", "fee_change"):
        res = pl.evaluate(led.books, change(kind=kind), NOW, profile=TX)
        assert (res.outcome, codes(res)) == ("refuse", ["JURISDICTION_UNSUPPORTED"])
    nowhere = pl.evaluate(led.books, change(), NOW, profile=pl.FacilityProfile("x", "Austin", None))
    assert codes(nowhere) == ["JURISDICTION_UNSUPPORTED"] and "place" in nowhere.reasons[0].words


def test_a_place_with_only_an_emergency_rule_cannot_activate_a_lease_increase():
    led = book("Orlando, FL")
    only_emergency = tuple(rs for rs in pl.RULE_SETS if rs.place == "FL" and rs.topic == "emergency")
    res = pl.evaluate(led.books, change(), NOW, profile=pl.profile_of(led.books), rules=only_emergency)
    assert codes(res) == ["JURISDICTION_UNSUPPORTED"] and "notice" in res.reasons[0].words


def test_an_unreviewed_rule_set_holds_and_a_demonstration_set_works_only_for_a_made_up_facility():
    led = book()
    lease_terms(led)
    checked(led, "CA")
    real_ca = replace(CA, synthetic=False)
    street = change(kind="street_rate", lease=None, disclosure={"max_cents": 20000, "months": 12, "first_page": True})
    res = pl.evaluate(led.books, street, NOW, profile=real_ca)
    assert "RULE_NOT_REVIEWED" in codes(res) and res.outcome == "hold"
    assert pl.evaluate(led.books, street, NOW, profile=CA).outcome == "allow"
    pending = tuple(replace(rs, review="pending") for rs in pl.RULE_SETS)
    assert "RULE_NOT_REVIEWED" in codes(pl.evaluate(led.books, street, NOW, profile=CA, rules=pending))


def test_two_rule_sets_that_disagree_on_one_topic_hold_instead_of_choosing():
    led = book("Salt Lake City, UT")
    lease_terms(led)
    checked(led, "UT")
    other = replace(next(rs for rs in pl.RULE_SETS if rs.id == "UT-notice"), id="UT-notice-b", notice_days=45)
    res = pl.evaluate(led.books, change(), NOW, profile=UT, rules=pl.RULE_SETS + (other,))
    assert "LEGAL_RULES_CONFLICT" in codes(res) and res.outcome == "hold"
    same = replace(other, notice_days=30)
    assert "LEGAL_RULES_CONFLICT" not in codes(pl.evaluate(led.books, change(), NOW, profile=UT, rules=pl.RULE_SETS + (same,)))


def test_a_lease_change_with_no_terms_taken_from_the_agreement_holds():
    led = book()
    checked(led, "CA")
    res = pl.evaluate(led.books, change(), NOW, profile=CA)
    assert "LEASE_TERMS_MISSING" in codes(res) and res.outcome == "hold"
    assert "LEASE_TERMS_MISSING" not in codes(pl.evaluate(led.books, change(kind="street_rate", lease=None), NOW, profile=CA))


def test_no_emergency_check_and_an_expired_one_hold_every_increase_but_not_a_decrease():
    led = book("Salt Lake City, UT")
    street = change(kind="street_rate", lease=None)
    res = pl.evaluate(led.books, street, NOW, profile=UT)
    assert (res.outcome, codes(res)) == ("hold", ["EMERGENCY_STATUS_UNKNOWN"])
    assert "no one has checked" in res.reasons[0].words.lower()
    checked(led, "UT", valid_until=date(2026, 11, 22), checked_on=date(2026, 11, 15))
    expired = pl.evaluate(led.books, street, NOW, profile=UT)
    assert codes(expired) == ["EMERGENCY_STATUS_UNKNOWN"] and "expired" in expired.reasons[0].words
    assert pl.evaluate(led.books, change(kind="street_rate", lease=None, new_cents=9000), NOW, profile=UT).outcome == "allow"
    checked(led, "UT")
    assert pl.evaluate(led.books, street, NOW, profile=UT).outcome == "allow"


def test_a_declaration_recorded_after_the_last_check_holds_until_someone_checks_again():
    led = book("Salt Lake City, UT")
    checked(led, "UT")
    pl.record_emergency(led, NOW, place="UT", name="Declared emergency, example county", source="state proclamation", starts=date(2026, 11, 20), ends=date(2026, 12, 20))
    street = change(kind="street_rate", lease=None, effective_on=date(2026, 12, 1))
    res = pl.evaluate(led.books, street, NOW, profile=UT)
    assert codes(res) == ["EMERGENCY_STATUS_UNKNOWN"] and "recorded after" in res.reasons[0].words
    checked(led, "UT", active=["PLE0001"])
    assert "EMERGENCY_STATUS_UNKNOWN" not in codes(pl.evaluate(led.books, street, NOW, profile=UT))


def test_an_active_declaration_in_a_place_with_no_emergency_rule_holds():
    led = book("Salt Lake City, UT")
    rid = pl.record_emergency(led, NOW, place="UT", name="Declared emergency, example county", source="state proclamation", starts=date(2026, 11, 20), ends=date(2026, 12, 20))
    checked(led, "UT", active=[rid])
    res = pl.evaluate(led.books, change(kind="street_rate", lease=None, effective_on=date(2026, 12, 1)), NOW, profile=UT)
    assert (res.outcome, codes(res)) == ("hold", ["EMERGENCY_PRICE_RESTRICTED"]) and "no rule" in res.reasons[0].words
    after = pl.evaluate(led.books, change(kind="street_rate", lease=None, effective_on=date(2026, 12, 21)), NOW, profile=UT)
    assert after.outcome == "allow"


def test_florida_has_no_number_so_any_increase_under_a_declaration_is_held_for_review():
    led = book("Orlando, FL")
    rid = pl.record_emergency(led, NOW, place="FL", name="Declared emergency, example county", source="state order", starts=date(2026, 11, 20), ends=date(2026, 12, 20))
    checked(led, "FL", active=[rid])
    profile = pl.profile_of(led.books)
    for new in (10001, 10100, 10500):
        res = pl.evaluate(led.books, change(kind="street_rate", lease=None, new_cents=new, effective_on=date(2026, 12, 1)), NOW, profile=profile)
        assert (res.outcome, codes(res)) == ("hold", ["EMERGENCY_PRICE_RESTRICTED"])
    words = res.reasons[0].words
    assert "Florida" in words and "no percentage" in words and "10 percent" not in words and "documented" in words
    assert pl.evaluate(led.books, change(kind="street_rate", lease=None, new_cents=9900, effective_on=date(2026, 12, 1)), NOW, profile=profile).outcome == "allow"


def _ca_emergency(led):
    rid = pl.record_emergency(led, NOW, place="CA", name="Declared emergency, example county", source="governor's proclamation", starts=date(2026, 11, 10), ends=date(2026, 12, 10))
    checked(led, "CA", active=[rid])
    return rid


def street_ca(new, **over):
    disclosure = {"max_cents": 20000, "months": 12, "first_page": True}
    return change(kind="street_rate", lease=None, unit_class="10x10", current_cents=10000, new_cents=new, effective_on=date(2026, 12, 1), disclosure=disclosure, **over)


def test_california_limits_a_price_to_ten_percent_over_the_price_before_the_declaration():
    led = book()
    rid = _ca_emergency(led)
    pl.record_baseline(led, NOW, restriction=rid, subject="10x10", price_cents=10000, observed_on=date(2026, 11, 1), source="rate sheet, Nov 1", review="reviewed", by="staff:manager")
    assert pl.evaluate(led.books, street_ca(11000), NOW, profile=CA).outcome == "allow"
    over = pl.evaluate(led.books, street_ca(11001), NOW, profile=CA)
    assert (over.outcome, codes(over)) == ("refuse", ["EMERGENCY_PRICE_RESTRICTED"])
    assert "10 percent" in over.reasons[0].words and "$110.00" in over.reasons[0].words and "$110.01" in over.reasons[0].words


def test_a_baseline_that_is_missing_late_or_unreviewed_holds():
    led = book()
    rid = _ca_emergency(led)
    res = pl.evaluate(led.books, street_ca(10500), NOW, profile=CA)
    assert (res.outcome, codes(res)) == ("hold", ["PRICE_BASELINE_UNPROVEN"])
    pl.record_baseline(led, NOW, restriction=rid, subject="10x10", price_cents=10000, observed_on=date(2026, 11, 12), source="rate sheet, Nov 12", review="reviewed", by="staff:manager")
    late = pl.evaluate(led.books, street_ca(10500), NOW, profile=CA)
    assert codes(late) == ["PRICE_BASELINE_UNPROVEN"] and "after the declaration began" in late.reasons[0].words
    pl.record_baseline(led, NOW, restriction=rid, subject="10x10", price_cents=10000, observed_on=date(2026, 11, 5), source="rate sheet, Nov 5", review="pending", by="staff:manager")
    unreviewed = pl.evaluate(led.books, street_ca(10500), NOW, profile=CA)
    assert codes(unreviewed) == ["PRICE_BASELINE_UNPROVEN"] and "not been reviewed" in unreviewed.reasons[0].words


def test_the_owner_may_be_stricter_than_the_law_and_never_looser():
    led = book("Salt Lake City, UT")
    checked(led, "UT")
    lease_terms(led)
    pl.adopt_owner_rule(led, NOW, version=1, sentence="No increase over 5 percent, or $20, in one step.", typed={"max_increase_percent": 5, "max_increase_cents": 2000})
    over = pl.evaluate(led.books, change(new_cents=10600), NOW, profile=UT)
    assert "OWNER_RULE" in codes(over) and over.outcome == "refuse"
    assert "No increase over 5 percent" in next(r.words for r in over.reasons if r.code == "OWNER_RULE")
    assert "OWNER_RULE" not in codes(pl.evaluate(led.books, change(new_cents=10500), NOW, profile=UT))
    pl.adopt_owner_rule(led, NOW, version=2, sentence="Ten days' notice is enough.", typed={"notice_days": 10})
    note = pl.evaluate(led.books, change(), NOW, profile=UT)
    looser = next(r for r in note.reasons if r.code == "OWNER_RULE_LOOSER")
    assert looser.kind == "note" and "30 days" in looser.words and "10 days" in looser.words
    assert pl.required_notice_days(led.books, [rs for rs in pl.RULE_SETS if rs.id == "UT-notice"]) == 30


def test_an_owner_rule_that_asks_to_override_the_law_is_refused_at_adoption_and_never_lifts_a_hold():
    led = book("Orlando, FL")
    for typed in ({"allow_during_emergency": True}, {"notice_days": -3}, {"max_increase_percent": 0}, {"skip_notice": True}, {}):
        with pytest.raises(Refused):
            pl.adopt_owner_rule(led, NOW, version=1, sentence="Let us raise it.", typed=typed)
    assert "price_change" not in led.books.rules
    with pytest.raises(Refused):
        pl.adopt_owner_rule(led, NOW, version=1, sentence="", typed={"notice_days": 30})
    pl.adopt_owner_rule(led, NOW, version=1, sentence="Up to 50 percent in one step.", typed={"max_increase_percent": 50})
    rid = pl.record_emergency(led, NOW, place="FL", name="Declared emergency, example county", source="state order", starts=date(2026, 11, 20), ends=date(2026, 12, 20))
    checked(led, "FL", active=[rid])
    res = pl.evaluate(led.books, change(kind="street_rate", lease=None, effective_on=date(2026, 12, 1)), NOW, profile=pl.profile_of(led.books))
    assert res.outcome == "hold"


def _approved(led, profile, **over):
    res = pl.evaluate(led.books, change(**over), NOW, profile=profile, point="approval")
    pl.record_approval(led, NOW, change="PC1", versions=res.versions, by="staff:manager", snapshot=pl._revision(change(**over)))
    return res


def test_a_legal_rule_update_invalidates_a_pending_approval_and_a_fresh_one_clears_it():
    led = book("Salt Lake City, UT")
    checked(led, "UT")
    lease_terms(led)
    first = _approved(led, UT)
    assert first.versions["UT-notice"] == 1
    now_at = pl.evaluate(led.books, change(), NOW, profile=UT, point="notice_authorization")
    assert "LEGAL_RULE_VERSION_STALE" not in codes(now_at)
    updated = tuple(replace(rs, version=2, notice_days=45) if rs.id == "UT-notice" else rs for rs in pl.RULE_SETS)
    stale = pl.evaluate(led.books, change(), NOW, profile=UT, point="activation", rules=updated)
    assert "LEGAL_RULE_VERSION_STALE" in codes(stale) and stale.outcome == "refuse"
    assert "UT-notice" in next(r.words for r in stale.reasons if r.code == "LEGAL_RULE_VERSION_STALE")
    # at proposal and approval the change is read fresh, so the old approval never blocks it
    assert "LEGAL_RULE_VERSION_STALE" not in codes(pl.evaluate(led.books, change(), NOW, profile=UT, point="approval", rules=updated))
    again = pl.evaluate(led.books, change(), NOW, profile=UT, point="approval", rules=updated)
    pl.record_approval(led, NOW, change="PC1", versions=again.versions, by="staff:manager", snapshot=pl._revision(change()))
    assert "LEGAL_RULE_VERSION_STALE" not in codes(pl.evaluate(led.books, change(), NOW, profile=UT, point="activation", rules=updated))


def test_new_terms_and_a_new_declaration_invalidate_an_approval_too():
    led = book("Salt Lake City, UT")
    checked(led, "UT")
    lease_terms(led)
    _approved(led, UT)
    lease_terms(led, agreement_sha256="f" * 64)
    assert "LEGAL_RULE_VERSION_STALE" in codes(pl.evaluate(led.books, change(), NOW, profile=UT, point="activation"))
    led2 = book("Salt Lake City, UT")
    lease_terms(led2)
    rid = pl.record_emergency(led2, NOW, place="UT", name="Declared emergency, example county", source="state proclamation", starts=date(2026, 11, 20), ends=date(2026, 12, 20))
    checked(led2, "UT")
    _approved(led2, UT)
    checked(led2, "UT", active=[rid])
    res = pl.evaluate(led2.books, change(), NOW, profile=UT, point="activation")
    assert "LEGAL_RULE_VERSION_STALE" in codes(res)


def test_a_decision_is_recorded_with_its_reasons_records_and_versions():
    led = book("Salt Lake City, UT")
    checked(led, "UT")
    lease_terms(led)
    c = change(invoice="L0001:2027-02")
    res = pl.decide(led, NOW, c, point="invoice", profile=UT)
    rec = led.books.price_law.decisions[-1]
    assert rec.id == res.id and rec.versions["UT-notice"] == 1 and rec.invoice == "L0001:2027-02"
    assert any(r.startswith("terms:L0001#") for r in rec.records) and any(r.startswith("check:") for r in rec.records)
    assert rec.snapshot["new_cents"] == 10500 and rec.reasons == res.reasons


def test_posted_basis_does_not_expose_mutable_decision_data():
    led = book('Salt Lake City, UT')
    tenant = led.add_tenant(NOW, 'Pat Tenant', '5550100')
    led.sign_lease(NOW, tenant, 'A1', 10000, [])
    c = change(invoice='L0001:2027-02', disclosure={'nested': {'value': 1}})
    ident = pl.record_decision(led, NOW, change=c.id, point='invoice', outcome='allow', reasons=[], records=[],
                               versions={'rule': 1}, lease=c.lease, invoice=c.invoice, snapshot=pl._snapshot(c))
    pl.post_invoice(led, at_local(date(2027, 1, 15), 10), decision=ident)
    basis = pl.posted_basis(led.books, c.invoice)
    basis.versions['rule'] = 2
    basis.snapshot['disclosure']['nested']['value'] = 2
    again = pl.posted_basis(led.books, c.invoice)
    assert again.versions == {'rule': 1} and again.snapshot['disclosure']['nested']['value'] == 1
