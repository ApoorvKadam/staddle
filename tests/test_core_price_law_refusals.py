from dataclasses import replace
"""Every refusal and hold of the price-law gate, each with words a staff member can read and a rule to point at, and the staff commands
that carry them: a refusal never comes after a write."""
from datetime import date

import pytest

from core import price_law as pl
from core.billing import Ledger, Refused
from core.books import at_local

SHA = "e" * 64
TODAY = date(2026, 11, 23)
NOW = at_local(TODAY, 10)
LATER = at_local(date(2026, 12, 24), 10)
UT = pl.FacilityProfile("Synthetic Utah facility", "Salt Lake City, UT", "UT", synthetic=True)
CA = pl.FacilityProfile("Synthetic California facility", "Sacramento, CA", "CA", synthetic=True)
NYC = pl.FacilityProfile("Synthetic New York City facility", "New York, NY", "NYC", synthetic=True)
ADDRESS = "1 Main St, Salt Lake City, UT 84101"
EMAIL = "pat@example.com"


def book(profile) -> Ledger:
    led = Ledger()
    led.set_up(NOW, "Test Storage", profile.city, ["A1"])
    pl.record_emergency_check(led, NOW, place=profile.place, checked_on=TODAY, valid_until=date(2027, 3, 31), by="staff:manager", source="state emergency page", active=[])
    return led


def terms(led, **over):
    args = {"lease": "L0001", "signed_on": date(2026, 3, 2), "agreement_sha256": SHA, "notice_methods": ["first_class_mail", "email", "hand"],
            "email_notice_authorized": True, "last_known_address": ADDRESS, "last_known_email": EMAIL, "designated_method": "email"}
    return pl.record_terms(led, NOW, **{**args, **over})


def notice(led, change="PC1", at=NOW, **over):
    args = {"new_cents": 10500, "effective_on": date(2026, 12, 23), "change": change, "lease": "L0001", "document_sha256": SHA, "recipient": "Pat Tenant", "channel": "first_class_mail", "sent_to": ADDRESS,
            "attempted_on": TODAY, "dispatch_accepted": True, "certificate_of_mailing": True, "postage_prepaid": True, "by": "staff:front_desk"}
    return pl.record_notice(led, at, **{**args, **over})


def change(**over):
    base = {"id": "PC1", "kind": "lease_increase", "current_cents": 10000, "new_cents": 10500, "effective_on": date(2026, 12, 23), "lease": "L0001", "other_recurring_cents": 0}
    return pl.PriceChange(**{**base, **over})


def verdict(led, c, profile, point="activation", at=LATER):
    res = pl.evaluate(led.books, c, at, point=point, profile=profile)
    return res.outcome, [r.code for r in res.reasons if r.kind != "note"], res


@pytest.fixture
def hypothetical_florida_rules(monkeypatch):
    # Exercise the modeled rule mechanics under a hypothetical future review.
    rules = tuple(replace(r, review="reviewed", reviewer="test fixture") if r.place == "FL" else r for r in pl.RULE_SETS)
    monkeypatch.setattr(pl, "RULE_SETS", rules)


def test_a_complete_utah_increase_is_allowed_only_once_the_notice_is_served_and_its_days_have_run():
    led = book(UT)
    terms(led)
    notice(led)
    assert verdict(led, change(), UT)[:2] == ("allow", [])
    assert verdict(led, change(), UT, "notice_completion", NOW)[:2] == ("allow", [])
    early = verdict(led, change(), UT, "activation", at_local(date(2026, 12, 22), 10))
    assert early[:2] == ("refuse", ["NOTICE_PERIOD_TOO_SHORT"]) and "takes effect Dec 23" in early[2].words


def test_utah_does_not_count_a_text_message_and_says_so():
    led = book(UT)
    terms(led, notice_methods=["first_class_mail", "email", "text"])
    notice(led, channel="text", sent_to="+1 801 555 0100", certificate_of_mailing=False, postage_prepaid=False)
    out, codes, res = verdict(led, change(), UT)
    assert (out, codes) == ("refuse", ["NOTICE_METHOD_NOT_PERMITTED"])
    assert res.reasons[0].words == "Utah requires 30 days' written notice by first-class mail or email; a text message does not count."
    assert res.reasons[0].cite == "UT-notice"


def test_a_notice_in_the_online_account_never_counts():
    led = book(UT)
    terms(led)
    notice(led, channel="dashboard", sent_to="pat")
    assert verdict(led, change(), UT)[:2] == ("refuse", ["NOTICE_METHOD_NOT_PERMITTED"])


def test_the_agreement_can_narrow_the_ways_notice_may_go():
    led = book(UT)
    terms(led, notice_methods=["first_class_mail"])
    notice(led, channel="email", sent_to=EMAIL, certificate_of_mailing=False, postage_prepaid=False)
    out, codes, res = verdict(led, change(), UT)
    assert (out, codes) == ("refuse", ["NOTICE_METHOD_NOT_PERMITTED"]) and "agreement" in res.reasons[0].words


def test_california_counts_email_only_when_the_agreement_authorizes_it():
    led = book(CA)
    terms(led, email_notice_authorized=False)
    notice(led, channel="email", sent_to=EMAIL, certificate_of_mailing=False, postage_prepaid=False)
    commit(led)
    out, codes, res = verdict(led, change(), CA)
    assert (out, codes) == ("refuse", ["NOTICE_METHOD_NOT_PERMITTED"]) and "does not authorize" in res.reasons[0].words
    led = book(CA)
    terms(led)
    notice(led, channel="email", sent_to=EMAIL, certificate_of_mailing=False, postage_prepaid=False)
    commit(led)
    assert verdict(led, change(), CA)[:2] == ("allow", [])


def commit(led, **over):
    args = {"lease": "L0001", "kind": "first_year", "max_cents": 12000, "covers_from": date(2026, 3, 2), "covers_to": date(2027, 3, 1),
            "disclosed_on": date(2026, 3, 2), "evidence_sha256": SHA, "first_page": True}
    return pl.record_commitment(led, NOW, **{**args, **over})


@pytest.mark.parametrize("mistake, over, words", [
    ("no certificate", {"certificate_of_mailing": False}, "certificate of mailing"),
    ("postage not prepaid", {"postage_prepaid": False}, "prepaid postage"),
    ("another address", {"sent_to": "9 Other Rd, Provo, UT"}, "last known address"),
])
def test_a_mailing_counts_only_with_its_proof_and_at_the_last_known_address(mistake, over, words):
    led = book(UT)
    terms(led)
    notice(led, **over)
    out, codes, res = verdict(led, change(), UT)
    assert (out, codes) == ("refuse", ["NOTICE_SERVICE_UNPROVEN"]) and words in res.reasons[0].words


def test_an_email_the_service_accepted_is_dispatch_not_service_unless_it_went_to_the_address_on_file():
    led = book(UT)
    terms(led)
    notice(led, channel="email", sent_to="someone.else@example.com", certificate_of_mailing=False, postage_prepaid=False)
    assert verdict(led, change(), UT)[:2] == ("refuse", ["NOTICE_SERVICE_UNPROVEN"])
    led = book(UT)
    terms(led)
    notice(led, channel="email", sent_to=EMAIL, dispatch_accepted=False, certificate_of_mailing=False, postage_prepaid=False)
    res = verdict(led, change(), UT)
    assert res[:2] == ("refuse", ["NOTICE_SERVICE_UNPROVEN"]) and "accepted" in res[2].words


def test_a_hand_delivery_counts_from_the_day_it_was_delivered_and_not_before_and_utah_does_not_list_it():
    led = book(CA)
    terms(led)
    commit(led)
    n = notice(led, channel="hand", sent_to="in person", certificate_of_mailing=False, postage_prepaid=False)
    assert verdict(led, change(), CA)[:2] == ("refuse", ["NOTICE_SERVICE_UNPROVEN"])
    pl.update_notice(led, at_local(date(2026, 11, 25), 10), change="PC1", notice=n, delivered_on=date(2026, 11, 25))
    out, codes, res = verdict(led, change(), CA)
    assert (out, codes) == ("refuse", ["NOTICE_PERIOD_TOO_SHORT"]) and "Nov 25" in res.words
    led = book(UT)
    terms(led)
    notice(led, channel="hand", sent_to="in person", certificate_of_mailing=False, postage_prepaid=False, delivered_on=TODAY)
    assert verdict(led, change(), UT)[1] == ["NOTICE_METHOD_NOT_PERMITTED"]


def test_no_notice_at_all_is_unproven_for_a_rent_increase_and_its_own_missing_notice_for_a_fee():
    led = book(UT)
    terms(led)
    assert verdict(led, change(), UT)[:2] == ("refuse", ["NOTICE_SERVICE_UNPROVEN"])
    fee = verdict(led, change(kind="fee_change"), UT)
    assert fee[:2] == ("refuse", ["FEE_CHANGE_NOTICE_MISSING"]) and "fee" in fee[2].words
    assert verdict(led, change(kind="fee_change", new_cents=9000), UT)[:2] == ("allow", [])


def test_the_days_are_counted_from_proposal_so_a_date_too_soon_is_refused_before_any_notice():
    led = book(UT)
    terms(led)
    res = verdict(led, change(effective_on=date(2026, 12, 10)), UT, "proposal", NOW)
    assert res[:2] == ("refuse", ["NOTICE_PERIOD_TOO_SHORT"]) and "Dec 23" in res[2].words
    assert verdict(led, change(effective_on=date(2026, 12, 23)), UT, "proposal", NOW)[:2] == ("allow", [])


def test_the_owner_may_ask_for_longer_notice_than_the_law():
    led = book(UT)
    terms(led)
    pl.adopt_owner_rule(led, NOW, version=1, sentence="Sixty days' notice before any increase.", typed={"notice_days": 60})
    res = verdict(led, change(effective_on=date(2026, 12, 23)), UT, "proposal", NOW)
    assert res[:2] == ("refuse", ["OWNER_RULE"]) and "60" in res[2].words


def test_new_york_city_needs_60_days_and_the_way_the_tenant_designated():
    led = book(NYC)
    terms(led, designated_method="email", last_known_address="5 Broad St, New York, NY 10004")
    commit(led, kind="first_year", max_cents=12000)
    notice(led, channel="email", sent_to=EMAIL, certificate_of_mailing=False, postage_prepaid=False, attempted_on=TODAY, effective_on=date(2027, 1, 22))
    notice(led, channel="email", sent_to=EMAIL, certificate_of_mailing=False, postage_prepaid=False, attempted_on=TODAY)
    short = verdict(led, change(effective_on=date(2026, 12, 23)), NYC)
    assert short[:2] == ("refuse", ["NOTICE_PERIOD_TOO_SHORT"]) and "60 days" in short[2].words
    assert verdict(led, change(effective_on=date(2027, 1, 22)), NYC, "activation", at_local(date(2027, 1, 22), 10))[:2] == ("allow", [])
    led = book(NYC)
    terms(led, designated_method="email", last_known_address="5 Broad St, New York, NY 10004")
    commit(led, kind="first_year", max_cents=12000)
    notice(led, sent_to="5 Broad St, New York, NY 10004", effective_on=date(2027, 1, 22))
    out, codes, res = verdict(led, change(effective_on=date(2027, 1, 22)), NYC, "activation", at_local(date(2027, 1, 22), 10))
    assert (out, codes) == ("refuse", ["NOTICE_METHOD_NOT_PERMITTED"]) and "asked" in res.reasons[0].words
    led = book(NYC)
    terms(led, designated_method=None)
    commit(led, kind="first_year", max_cents=12000)
    notice(led, effective_on=date(2027, 1, 22))
    assert verdict(led, change(effective_on=date(2027, 1, 22)), NYC, "activation", at_local(date(2027, 1, 22), 10))[:2] == ("hold", ["LEASE_TERMS_MISSING"])


def test_a_first_year_maximum_must_have_been_disclosed_on_a_new_california_agreement_and_holds_afterwards():
    led = book(CA)
    terms(led)
    notice(led)
    out, codes, res = verdict(led, change(), CA)
    assert (out, codes) == ("refuse", ["DISCLOSURE_MISSING_OR_INVALID"]) and "first-year maximum" in res.reasons[0].words
    commit(led, max_cents=10400)
    over = verdict(led, change(), CA)
    assert over[:2] == ("refuse", ["DISCLOSED_MAX_EXCEEDED"]) and "$104.00" in over[2].words and "$105.00" in over[2].words
    led = book(CA)
    terms(led)
    notice(led)
    commit(led, max_cents=10500)
    assert verdict(led, change(), CA)[:2] == ("allow", [])


def test_california_requires_first_page_evidence_for_first_year_maximum():
    led = book(CA)
    terms(led)
    notice(led)
    commit(led, first_page=False)
    assert "DISCLOSURE_MISSING_OR_INVALID" in verdict(led, change(), CA)[1]


def test_an_agreement_signed_before_the_california_disclosure_rule_began_needs_no_first_year_maximum():
    led = book(CA)
    terms(led, signed_on=date(2025, 12, 1))
    effective = date(2026, 11, 30)
    notice(led, attempted_on=date(2026, 10, 25), effective_on=effective)
    assert verdict(led, change(effective_on=effective), CA, at=at_local(effective, 10))[:2] == ("allow", [])


def test_a_disclosed_maximum_binds_in_any_place_where_it_was_stated():
    led = book(UT)
    terms(led)
    notice(led)
    commit(led, max_cents=10200)
    assert verdict(led, change(), UT)[:2] == ("refuse", ["DISCLOSED_MAX_EXCEEDED"])
    assert verdict(led, change(effective_on=date(2027, 3, 2), new_cents=11000), UT, "activation", at_local(date(2027, 3, 2), 10))[1] != ["DISCLOSED_MAX_EXCEEDED"]


def test_later_disclosure_does_not_erase_a_lower_maximum():
    led = book(UT)
    terms(led)
    notice(led)
    commit(led, max_cents=10400)
    commit(led, max_cents=12000)
    assert verdict(led, change(), UT)[1] == ["DISCLOSED_MAX_EXCEEDED"]


def test_new_york_city_needs_a_yearly_maximum_notice_at_least_60_days_before_each_anniversary():
    led = book(NYC)
    terms(led, designated_method="first_class_mail", last_known_address="5 Broad St, New York, NY 10004")
    commit(led, kind="first_year", max_cents=12000)
    notice(led, at=at_local(date(2027, 1, 5), 10), sent_to="5 Broad St, New York, NY 10004", attempted_on=date(2027, 1, 5), effective_on=date(2027, 4, 5))
    c = change(effective_on=date(2027, 4, 5))
    at = at_local(date(2027, 4, 5), 10)
    pl.record_emergency_check(led, at_local(date(2027, 4, 1), 10), place="NYC", checked_on=date(2027, 4, 1), valid_until=date(2027, 4, 30), by="staff:manager", source="city page", active=[])
    out, codes, res = verdict(led, c, NYC, "activation", at)
    assert (out, codes) == ("refuse", ["ANNUAL_MAX_NOTICE_MISSING"]) and "60 days" in res.reasons[0].words
    commit(led, kind="anniversary", max_cents=12500, covers_from=date(2027, 3, 2), covers_to=date(2028, 3, 1), disclosed_on=date(2027, 1, 20))
    late = verdict(led, c, NYC, "activation", at)
    assert late[:2] == ("refuse", ["ANNUAL_MAX_NOTICE_MISSING"]) and "Jan 20" in late[2].words
    commit(led, kind="anniversary", max_cents=12500, covers_from=date(2027, 3, 2), covers_to=date(2028, 3, 1), disclosed_on=date(2026, 12, 20))
    assert verdict(led, c, NYC, "activation", at)[:2] == ("allow", [])
    notice(led, at=at_local(date(2027, 1, 5), 10), sent_to="5 Broad St, New York, NY 10004", attempted_on=date(2027, 1, 5), effective_on=date(2027, 4, 5), new_cents=12600)
    assert verdict(led, change(effective_on=date(2027, 4, 5), new_cents=12600), NYC, "activation", at)[1] == ["DISCLOSED_MAX_EXCEEDED"]


@pytest.mark.parametrize("kind,current,new,other", [
    ("lease_increase", 10000, 11000, 1500),
    ("fee_change", 1000, 1500, 11000),
])
def test_nyc_compares_complete_recurring_total_to_maximum(kind, current, new, other):
    led = book(NYC)
    terms(led, designated_method="first_class_mail")
    commit(led, max_cents=12000)
    c = change(kind=kind, current_cents=current, new_cents=new, other_recurring_cents=other)
    assert "DISCLOSED_MAX_EXCEEDED" in verdict(led, c, NYC, "proposal", NOW)[1]
    at_cap = change(kind=kind, current_cents=current, new_cents=new, other_recurring_cents=12000 - new)
    assert "DISCLOSED_MAX_EXCEEDED" not in verdict(led, at_cap, NYC, "proposal", NOW)[1]
    for invalid in (None, -1, True, "1500"):
        unknown = change(kind=kind, current_cents=current, new_cents=new, other_recurring_cents=invalid)
        assert "RECURRING_TOTAL_UNKNOWN" in verdict(led, unknown, NYC, "proposal", NOW)[1]


def test_nyc_annual_notice_deadline_uses_anniversary_not_record_start():
    led = book(NYC)
    terms(led, designated_method="first_class_mail")
    commit(led, kind="anniversary", covers_from=date(2027, 4, 1), covers_to=date(2028, 3, 1), disclosed_on=date(2027, 1, 20))
    notice(led, at=at_local(date(2027, 1, 5), 10), attempted_on=date(2027, 1, 5), effective_on=date(2027, 4, 5))
    pl.record_emergency_check(led, at_local(date(2027, 4, 1), 10), place="NYC", checked_on=date(2027, 4, 1), valid_until=date(2027, 4, 30), by="staff:manager", source="city page", active=[])
    assert "ANNUAL_MAX_NOTICE_MISSING" in verdict(led, change(effective_on=date(2027, 4, 5)), NYC, at=at_local(date(2027, 4, 5), 10))[1]


def street(**disclosure):
    base = {"max_cents": 12000, "months": 12, "first_page": True, "recurring_cents": 11500}
    return change(kind="street_rate", lease=None, unit_class="10x10", current_cents=10000, new_cents=11000, effective_on=date(2026, 12, 1),
                  disclosure={**base, **disclosure})


@pytest.mark.parametrize("profile, disclosure, words", [
    (CA, {"first_page": False}, "first page"),
    (CA, {"months": 6}, "12 months"),
    (CA, {"max_cents": 0}, "maximum"),
    (NYC, {"recurring_cents": None}, "recurring"),
])
def test_a_street_rate_in_california_or_new_york_city_needs_its_disclosure_in_full(profile, disclosure, words):
    led = book(profile)
    res = verdict(led, street(**disclosure), profile, "publication", NOW)
    assert (res[0], res[1]) == ("refuse", ["DISCLOSURE_MISSING_OR_INVALID"]) and words in res[2].words
    none = verdict(led, pl.PriceChange(**{**street().__dict__, "disclosure": None}), profile, "publication", NOW)
    assert none[1] == ["DISCLOSURE_MISSING_OR_INVALID"]
    assert verdict(led, street(), profile, "publication", NOW)[:2] == ("allow", [])


def test_a_street_rate_or_its_recurring_fee_above_the_disclosed_maximum_is_refused():
    led = book(NYC)
    assert verdict(led, street(max_cents=10900), NYC, "quote_acceptance", NOW)[1] == ["DISCLOSED_MAX_EXCEEDED"]
    assert verdict(led, street(recurring_cents=12100), NYC, "quote_acceptance", NOW)[1] == ["DISCLOSED_MAX_EXCEEDED"]


def test_a_utah_street_rate_needs_no_disclosure():
    led = book(UT)
    assert verdict(led, pl.PriceChange(**{**street().__dict__, "disclosure": None}), UT, "publication", NOW)[:2] == ("allow", [])


@pytest.mark.parametrize("over, words", [
    ({"price_promise_until": date(2027, 6, 1)}, "no increase"),
    ({"price_promise_until": date(2027, 6, 1), "price_promise_max_cents": 10400}, "$104.00"),
    ({"rent_change_allowed": False}, "does not let"),
])
def test_a_price_promise_or_an_agreement_that_bars_changes_stops_an_increase(over, words):
    led = book(UT)
    terms(led, **over)
    notice(led)
    out, codes, res = verdict(led, change(), UT)
    assert (out, codes) == ("refuse", ["CONTRACTUAL_PRICE_PROMISE"]) and words in res.reasons[0].words
    over_ok = {"price_promise_until": date(2026, 12, 1), "price_promise_max_cents": 10000}
    led = book(UT)
    terms(led, **over_ok)
    notice(led)
    assert verdict(led, change(), UT)[:2] == ("allow", [])


def _ca_emergency(led, new=11500):
    rid = pl.record_emergency(led, NOW, place="CA", name="Declared emergency, example county", source="governor's proclamation", starts=date(2026, 11, 10), ends=date(2027, 1, 10))
    pl.record_emergency_check(led, NOW, place="CA", checked_on=TODAY, valid_until=date(2027, 3, 31), by="staff:manager", source="state emergency page", active=[rid])
    pl.record_baseline(led, NOW, restriction=rid, subject="10x10", price_cents=10000, observed_on=date(2026, 11, 1), source="rate sheet", review="reviewed", by="staff:manager")
    return rid


def test_a_claimed_exception_counts_only_once_the_owner_has_recorded_it_with_its_document():
    led = book(CA)
    _ca_emergency(led)
    claimed = pl.PriceChange(**{**street().__dict__, "new_cents": 11500, "claimed_exception": "costs rose after the storm"})
    res = verdict(led, claimed, CA, "publication", NOW)
    assert set(res[1]) == {"EMERGENCY_PRICE_RESTRICTED", "LEGAL_EXCEPTION_UNVERIFIED"}
    for who in ("staff:manager", "staff:front_desk"):
        with pytest.raises(Refused):
            pl.record_exception(led, NOW, change="PC1", basis="documented cost increase", covers_cents=11500, document_sha256=SHA, by=who)
    with pytest.raises(Refused):
        pl.record_exception(led, NOW, change="PC1", basis="documented cost increase", covers_cents=11500, document_sha256="", by="owner")
    pl.record_exception(led, NOW, change="PC1", basis="documented cost increase", covers_cents=11400, document_sha256=SHA, by="owner")
    assert "LEGAL_EXCEPTION_UNVERIFIED" in verdict(led, claimed, CA, "publication", NOW)[1]
    pl.record_exception(led, NOW, change="PC1", basis="documented cost increase", covers_cents=11500, document_sha256=SHA, by="owner")
    ok = verdict(led, claimed, CA, "publication", NOW)
    assert ok[:2] == ("allow", []) and any(r.code == "EMERGENCY_EXCEPTION_RECORDED" and r.kind == "note" for r in ok[2].reasons)


def test_a_recorded_exception_lifts_the_florida_hold_but_never_a_notice_refusal(hypothetical_florida_rules):
    led = Ledger()
    led.set_up(NOW, "Northstar Storage", "Orlando, FL", ["A1"])
    rid = pl.record_emergency(led, NOW, place="FL", name="Declared emergency, example county", source="state order", starts=date(2026, 11, 20), ends=date(2026, 12, 20))
    pl.record_emergency_check(led, NOW, place="FL", checked_on=TODAY, valid_until=date(2027, 3, 31), by="staff:manager", source="state page", active=[rid])
    fl = pl.profile_of(led.books)
    c = pl.PriceChange(id="PC1", kind="street_rate", current_cents=10000, new_cents=10500, effective_on=date(2026, 12, 1), unit_class="10x10")
    assert pl.evaluate(led.books, c, NOW, point="publication").outcome == "hold"
    pl.record_exception(led, NOW, change="PC1", basis="documented legal review of this price", covers_cents=10500, document_sha256=SHA, by="owner")
    assert pl.evaluate(led.books, c, NOW, point="publication").outcome == "allow"
    led2 = book(UT)
    terms(led2)
    pl.record_exception(led2, NOW, change="PC1", basis="documented legal review of this price", covers_cents=10500, document_sha256=SHA, by="owner")
    assert verdict(led2, change(), UT)[1] == ["NOTICE_SERVICE_UNPROVEN"]


def act(i, action, form=None, about=None, source="staff:manager"):
    return pl.Act(i, action, source, about, form or {})


def test_a_refused_command_writes_nothing_but_the_gate_record():
    led = book(UT)
    before = len(led.pending)
    for a in (act("a1", "record_notice", {"change": "PC1"}, source="renter"),
              act("a2", "record_notice", {"change": "PC1", "lease": "L0001", "document_sha256": "short", "recipient": "Pat", "channel": "email", "sent_to": EMAIL,
                                          "attempted_on": "2026-11-23", "dispatch_accepted": True}),
              act("a3", "record_exception", {"change": "PC1", "basis": "x", "covers_cents": 1, "document_sha256": SHA}, source="staff:manager"),
              act("a4", "melt_the_price", {})):
        res = pl.command(led, a, NOW, profile=UT)
        assert not res.allowed
    kinds = [e.kind for e in led.pending[before:]]
    assert kinds == ["gate.decided"] * 4
    assert not led.books.price_law.notices and not led.books.price_law.terms and not led.books.price_law.authorizations
    assert pl.command(led, act("a1", "record_notice", {}), NOW, profile=UT).repeated


def test_a_staff_command_records_the_terms_and_then_the_gate_answers_a_proposal_in_words():
    led = book(UT)
    ok = pl.command(led, act("t1", "record_terms", {"lease": "L0001", "signed_on": "2026-03-02", "agreement_sha256": SHA, "notice_methods": ["first_class_mail", "email"],
                                                   "email_notice_authorized": True, "last_known_address": ADDRESS, "last_known_email": EMAIL}), NOW, profile=UT)
    assert ok.allowed and led.books.price_law.terms["L0001"][0].version == 1
    form = {"kind": "lease_increase", "current_cents": 10000, "new_cents": 10500, "effective_on": "2026-12-10", "lease": "L0001", "point": "proposal"}
    short = pl.command(led, act("c1", "check_price_change", form, about="PC1"), NOW, profile=UT)
    assert not short.allowed and short.why == "NOTICE_PERIOD_TOO_SHORT" and "30 days" in short.check and short.made.startswith("PLD")
    fine = pl.command(led, act("c2", "check_price_change", {**form, "effective_on": "2026-12-23"}, about="PC1"), NOW, profile=UT)
    assert fine.allowed and led.books.price_law.decisions[-1].outcome == "allow"
    approved = pl.command(led, act("c3", "approve_price_change", {**form, "effective_on": "2026-12-23"}, about="PC1"), NOW, profile=UT)
    assert approved.allowed and led.books.price_law.approvals["PC1"].versions["UT-notice"] == 1
    refused = pl.command(led, act("c4", "approve_price_change", form, about="PC9"), NOW, profile=UT)
    assert not refused.allowed and "PC9" not in led.books.price_law.approvals


def test_a_hold_is_never_cleared_by_approving_it():
    led = Ledger()
    led.set_up(NOW, "Test Storage", "Salt Lake City, UT", ["A1"])
    form = {"kind": "street_rate", "current_cents": 10000, "new_cents": 10500, "effective_on": "2026-12-10", "unit_class": "10x10", "point": "approval"}
    res = pl.command(led, act("h1", "approve_price_change", form, about="PC1"), NOW, profile=UT)
    assert not res.allowed and res.why == "EMERGENCY_STATUS_UNKNOWN" and res.check.startswith("Held for review")
    assert "PC1" not in led.books.price_law.approvals
    again = pl.command(led, act("h2", "approve_price_change", form, about="PC1", source="owner"), NOW, profile=UT)
    assert not again.allowed and "PC1" not in led.books.price_law.approvals


def test_first_year_disclosure_must_cover_the_whole_first_year():
    led = book(CA)
    terms(led)
    notice(led)
    commit(led, covers_from=date(2026, 12, 23), covers_to=date(2026, 12, 23))
    outcome, codes, _ = verdict(led, change(), CA)
    assert outcome == 'refuse' and 'DISCLOSURE_MISSING_OR_INVALID' in codes
    commit(led)
    assert verdict(led, change(), CA)[:2] == ('allow', [])
