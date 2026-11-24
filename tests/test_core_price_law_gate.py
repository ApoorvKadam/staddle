"""A scenario per profile, run the way the product runs it: staff commands through one gate onto a real store, no command line. Each
profile walks an increase from proposal to a posted invoice, then meets the refusal or hold that is its own. Also: a posted invoice keeps
the rule snapshot it was decided on, a pending approval goes stale when the law changes, and an emergency check that expires holds."""
from dataclasses import replace
from datetime import date

import pytest

from core import price_law as pl
from core.billing import Ledger
from core.books import at_local
from core.store import Store

STREAM = "visitor:price-law"
SHA = "e" * 64
TODAY = date(2026, 11, 23)
NOW = at_local(TODAY, 10)


class Desk:
    """One facility on a real store; every write goes through `pl.run`."""

    def __init__(self, name: str, city: str, profile=None):
        led = Ledger()
        led.set_up(NOW, name, city, ["A1"])
        tenant = led.add_tenant(NOW, "Pat Tenant", "5550100")
        led.sign_lease(NOW, tenant, "A1", 10000, [])
        self.store, self.profile, self.n = Store(":memory:"), profile, 0
        with self.store.write():
            self.store.append("base", led.pending)

    def go(self, action, at=NOW, who="staff:manager", about=None, **form):
        self.n += 1
        return pl.run(self.store, STREAM, pl.Act(f"g{self.n}", action, who, about, form), at, self.profile)

    def books(self):
        return self.store.books(STREAM)

    def events(self, kind):
        return [e for e in self.store._rows(STREAM) if e.kind == kind] if hasattr(self.store, "_rows") else []

    def checked(self, place, valid_until="2027-03-31", **over):
        return self.go("record_emergency_check", place=place, checked_on="2026-11-23", valid_until=valid_until, source="state emergency page", active=[], **over)

    def terms(self, **over):
        form = {"lease": "L0001", "signed_on": "2026-03-02", "agreement_sha256": SHA, "notice_methods": ["first_class_mail", "email"], "email_notice_authorized": True,
                "last_known_address": "1 Main St, Anytown", "last_known_email": "pat@example.com"}
        return self.go("record_terms", **{**form, **over})

    def notice(self, change="PC1", **over):
        form = {"new_cents": 10500, "effective_on": "2026-12-23", "change": change, "lease": "L0001", "document_sha256": SHA, "recipient": "Pat Tenant", "channel": "first_class_mail", "sent_to": "1 Main St, Anytown",
                "attempted_on": "2026-11-23", "dispatch_accepted": True, "certificate_of_mailing": True, "postage_prepaid": True}
        return self.go("record_notice", **{**form, **over})

    def change(self, action, point, at, about="PC1", **over):
        form = {"kind": "lease_increase", "current_cents": 10000, "new_cents": 10500, "effective_on": "2026-12-23", "lease": "L0001", "point": point}
        return self.go(action, at=at, about=about, **{**form, **over})

    def walk(self, effective=date(2026, 12, 23), days=30, **over):
        """The whole road: proposal, approval, notice, completion, activation, invoice. Returns every decision."""
        eff = effective.isoformat()
        out = {"proposal": self.change("check_price_change", "proposal", NOW, effective_on=eff, **over)}
        out["approval"] = self.change("approve_price_change", "approval", NOW, effective_on=eff, **over)
        out["notice_authorization"] = self.change("check_price_change", "notice_authorization", NOW, effective_on=eff, **over)
        out["notice"] = self.notice(effective_on=eff, new_cents=over.get("new_cents", 10500))
        out["notice_completion"] = self.change("check_price_change", "notice_completion", NOW, effective_on=eff, **over)
        on = at_local(effective, 10)
        out["activation"] = self.change("check_price_change", "activation", on, effective_on=eff, **over)
        out["invoice"] = self.change("check_price_change", "invoice", on, effective_on=eff, invoice="L0001:2026-12", **over)
        return out


def allowed(walk):
    return {point: d.allowed for point, d in walk.items()}


@pytest.fixture
def hypothetical_florida_rules(monkeypatch):
    # Exercise the modeled rule mechanics under a hypothetical future review.
    rules = tuple(replace(r, review="reviewed", reviewer="test fixture") if r.place == "FL" else r for r in pl.RULE_SETS)
    monkeypatch.setattr(pl, "RULE_SETS", rules)


def test_california_walks_an_increase_to_a_posted_invoice_and_stops_at_its_disclosed_maximum():
    d = Desk("Synthetic California facility", "Sacramento, CA", pl.SYNTHETIC_PROFILES["CA"])
    assert d.checked("CA").allowed and d.terms().allowed
    assert d.go("record_commitment", lease="L0001", kind="first_year", max_cents=11000, covers_from="2026-03-02", covers_to="2027-03-01",
                disclosed_on="2026-03-02", evidence_sha256=SHA).allowed
    walk = d.walk()
    assert all(allowed(walk).values()), {k: v.check for k, v in walk.items() if not v.allowed}
    books = d.books()
    assert books.price_law.approvals["PC1"].versions["CA-notice"] == 1
    assert books.price_law.decisions[-1].invoice == "L0001:2026-12"
    over = d.change("check_price_change", "proposal", NOW, about="PC2", new_cents=11100)
    assert (over.allowed, over.why) == (False, "DISCLOSED_MAX_EXCEEDED") and "$110.00" in over.check and "$111.00" in over.check
    assert d.change("check_price_change", "proposal", NOW, about="PC3", new_cents=11000).allowed


def test_utah_walks_an_increase_and_meets_its_text_message_rule():
    d = Desk("Synthetic Utah facility", "Salt Lake City, UT", pl.SYNTHETIC_PROFILES["UT"])
    assert d.checked("UT").allowed and d.terms(notice_methods=["first_class_mail", "email", "text"]).allowed
    walk = d.walk()
    assert all(allowed(walk).values()), {k: v.check for k, v in walk.items() if not v.allowed}
    d.notice(change="PC2", channel="text", sent_to="+1 801 555 0100", certificate_of_mailing=False, postage_prepaid=False)
    text = d.change("check_price_change", "notice_completion", NOW, about="PC2")
    assert (text.allowed, text.why) == (False, "NOTICE_METHOD_NOT_PERMITTED")
    assert text.check == "Utah requires 30 days' written notice by first-class mail or email; a text message does not count."


def test_new_york_city_walks_an_increase_with_sixty_days_and_refuses_thirty():
    d = Desk("Synthetic New York City facility", "New York, NY", pl.SYNTHETIC_PROFILES["NYC"])
    assert d.checked("NYC").allowed and d.terms(designated_method="first_class_mail").allowed
    assert d.go("record_commitment", lease="L0001", kind="first_year", max_cents=11000, covers_from="2026-03-02", covers_to="2027-03-01",
                disclosed_on="2026-03-02", evidence_sha256=SHA).allowed
    walk = d.walk(effective=date(2027, 1, 22))
    assert all(allowed(walk).values()), {k: v.check for k, v in walk.items() if not v.allowed}
    short = d.change("check_price_change", "proposal", NOW, about="PC2", effective_on="2026-12-23")
    assert (short.allowed, short.why) == (False, "NOTICE_PERIOD_TOO_SHORT") and "60 days" in short.check


def test_florida_northstar_follows_its_agreement_and_holds_any_increase_under_a_declaration(hypothetical_florida_rules):
    d = Desk("Northstar Storage", "Orlando, FL")
    assert d.checked("FL").allowed
    assert d.terms(notice_days=30, notice_methods=["first_class_mail"], email_notice_authorized=False).allowed
    walk = d.walk()
    assert all(allowed(walk).values()), {k: v.check for k, v in walk.items() if not v.allowed}
    rid = d.go("record_emergency", place="FL", name="Declared emergency, example county", source="state order", starts="2026-11-20", ends="2026-12-20").made
    assert d.go("record_emergency_check", place="FL", checked_on="2026-11-23", valid_until="2027-03-31", source="state emergency page", active=[rid]).allowed
    held = d.change("check_price_change", "proposal", NOW, about="PC2", kind="street_rate", lease=None, unit_class="10x10", effective_on="2026-12-01")
    assert (held.allowed, held.why) == (False, "EMERGENCY_PRICE_RESTRICTED") and held.check.startswith("Held for review") and "no percentage" in held.check
    approved = d.change("approve_price_change", "approval", NOW, about="PC2", kind="street_rate", lease=None, unit_class="10x10", effective_on="2026-12-01")
    assert not approved.allowed and "PC2" not in d.books().price_law.approvals
    assert d.go("record_exception", who="staff:manager", change="PC2", basis="documented legal review", covers_cents=10500, document_sha256=SHA).allowed is False
    assert d.go("record_exception", who="owner", change="PC2", basis="documented legal review", covers_cents=10500, document_sha256=SHA).allowed
    assert d.change("approve_price_change", "approval", NOW, about="PC2", kind="street_rate", lease=None, unit_class="10x10", effective_on="2026-12-01").allowed


def test_a_posted_invoice_keeps_the_rule_snapshot_it_was_decided_on(monkeypatch):
    d = Desk("Synthetic Utah facility", "Salt Lake City, UT", pl.SYNTHETIC_PROFILES["UT"])
    d.checked("UT")
    d.terms()
    walk = d.walk()
    first = walk["invoice"]
    assert first.allowed
    with d.store.write():
        led = Ledger(d.books())
        pl.post_invoice(led, at_local(date(2026, 12, 23), 10), decision=first.made)
        d.store.append(STREAM, led.pending)
    basis = pl.posted_basis(d.books(), "L0001:2026-12")
    assert basis.id == first.made and basis.versions["UT-notice"] == 1 and basis.outcome == "allow"
    recorded = len(d.books().price_law.decisions)
    updated = tuple(replace(rs, version=2, notice_days=45) if rs.id == "UT-notice" else rs for rs in pl.RULE_SETS)
    monkeypatch.setattr(pl, "RULE_SETS", updated)
    on = at_local(date(2026, 12, 23), 10)
    again = d.change("check_price_change", "invoice", on, invoice="L0001:2026-12")
    assert again.allowed and again.made == first.made
    assert len(d.books().price_law.decisions) == recorded
    assert pl.posted_basis(d.books(), "L0001:2026-12").versions["UT-notice"] == 1
    fresh = d.change("check_price_change", "invoice", at_local(date(2027, 1, 23), 10), about="PC5", invoice="L0001:2027-01", effective_on="2027-01-23")
    assert fresh.made != first.made and d.books().price_law.decisions[-1].versions["UT-notice"] == 2
    assert pl.posted_basis(d.books(), "L0001:2027-02") is None


def test_a_pending_approval_goes_stale_when_the_law_changes_and_a_new_approval_clears_it(monkeypatch):
    d = Desk("Synthetic Utah facility", "Salt Lake City, UT", pl.SYNTHETIC_PROFILES["UT"])
    d.checked("UT")
    d.terms()
    assert d.change("approve_price_change", "approval", NOW).allowed
    updated = tuple(replace(rs, version=2) if rs.id == "UT-notice" else rs for rs in pl.RULE_SETS)
    monkeypatch.setattr(pl, "RULE_SETS", updated)
    stale = d.change("check_price_change", "notice_authorization", NOW)
    assert (stale.allowed, stale.why) == (False, "LEGAL_RULE_VERSION_STALE") and "UT-notice" in stale.check
    assert d.change("approve_price_change", "approval", NOW).allowed
    assert d.change("check_price_change", "notice_authorization", NOW).allowed


def test_an_emergency_check_that_expires_holds_the_next_increase_until_staff_check_again():
    d = Desk("Synthetic Utah facility", "Salt Lake City, UT", pl.SYNTHETIC_PROFILES["UT"])
    assert d.checked("UT", valid_until="2026-12-31").allowed
    d.terms()
    d.notice()
    on = at_local(date(2026, 12, 23), 10)
    assert d.change("check_price_change", "activation", on).allowed
    late = at_local(date(2027, 1, 5), 10)
    held = d.change("check_price_change", "activation", late, about="PC2")
    assert (held.allowed, held.why) == (False, "EMERGENCY_STATUS_UNKNOWN") and "expired on Dec 31" in held.check
    assert d.go("record_emergency_check", at=late, place="UT", checked_on="2027-01-05", valid_until="2027-04-05", source="state emergency page", active=[]).allowed
    assert d.change("check_price_change", "activation", late, about="PC3", new_cents=10600).why != "EMERGENCY_STATUS_UNKNOWN"


def test_nothing_is_decided_for_a_place_no_rule_covers_and_the_decision_is_still_recorded():
    d = Desk("A Texas facility", "Austin, TX")
    res = d.change("check_price_change", "proposal", NOW)
    assert (res.allowed, res.why) == (False, "JURISDICTION_UNSUPPORTED") and "cannot take effect" in res.check
    rec = d.books().price_law.decisions[-1]
    assert (rec.outcome, rec.reasons[0].code) == ("refuse", "JURISDICTION_UNSUPPORTED")


def test_the_whole_record_survives_the_store_and_replays_to_the_same_decisions():
    d = Desk("Synthetic Utah facility", "Salt Lake City, UT", pl.SYNTHETIC_PROFILES["UT"])
    d.checked("UT")
    d.terms()
    d.walk()
    live = d.books().price_law
    from core.store import Store
    again = d.store.books(STREAM).price_law
    assert [(r.id, r.outcome, r.versions) for r in again.decisions] == [(r.id, r.outcome, r.versions) for r in live.decisions]
    assert again.notices["PC1"][0].channel == "first_class_mail" and again.terms["L0001"][0].agreement_sha256 == SHA


def test_every_refusal_code_the_design_names_is_one_the_gate_can_give():
    import inspect
    source = inspect.getsource(pl)
    for code in ("JURISDICTION_UNSUPPORTED", "LEGAL_RULE_VERSION_STALE", "NOTICE_METHOD_NOT_PERMITTED", "NOTICE_SERVICE_UNPROVEN", "DISCLOSURE_MISSING_OR_INVALID",
                 "DISCLOSED_MAX_EXCEEDED", "ANNUAL_MAX_NOTICE_MISSING", "FEE_CHANGE_NOTICE_MISSING", "EMERGENCY_STATUS_UNKNOWN", "EMERGENCY_PRICE_RESTRICTED",
                 "PRICE_BASELINE_UNPROVEN", "LEGAL_EXCEPTION_UNVERIFIED", "CONTRACTUAL_PRICE_PROMISE"):
        assert f'"{code}"' in source


def test_approval_is_bound_to_amount_and_effective_date():
    d = Desk('Synthetic Utah facility', 'Salt Lake City, UT', pl.SYNTHETIC_PROFILES['UT'])
    assert d.checked('UT').allowed and d.terms().allowed
    assert d.change('approve_price_change', 'approval', NOW).allowed
    assert d.change('check_price_change', 'notice_authorization', NOW).allowed
    for terms in ({'new_cents': 12000}, {'effective_on': '2026-12-24'}):
        result = d.change('check_price_change', 'notice_authorization', NOW, **terms)
        assert not result.allowed and result.why == 'LEGAL_RULE_VERSION_STALE'


def test_notice_requires_the_served_amount_date_and_lease():
    d = Desk('Synthetic Utah facility', 'Salt Lake City, UT', pl.SYNTHETIC_PROFILES['UT'])
    assert d.checked('UT').allowed and d.terms().allowed
    assert d.notice(new_cents=10500, effective_on='2026-12-23').allowed
    assert d.change('check_price_change', 'notice_completion', NOW).allowed
    for terms in ({'new_cents': 12000}, {'effective_on': '2026-12-24'}):
        assert not d.change('check_price_change', 'notice_completion', NOW, **terms).allowed


def test_notice_for_another_lease_does_not_prove_service():
    d = Desk('Synthetic Utah facility', 'Salt Lake City, UT', pl.SYNTHETIC_PROFILES['UT'])
    assert d.checked('UT').allowed and d.terms().allowed
    assert d.terms(lease='L0002').allowed
    assert d.notice(lease='L0002').allowed
    result = d.change('check_price_change', 'notice_completion', NOW)
    assert not result.allowed and result.why == 'NOTICE_SERVICE_UNPROVEN'


def test_unposted_invoice_check_is_not_reused_for_new_terms():
    d = Desk('Synthetic Utah facility', 'Salt Lake City, UT', pl.SYNTHETIC_PROFILES['UT'])
    assert d.checked('UT').allowed and d.terms().allowed and d.notice().allowed
    on = at_local(date(2026, 12, 23), 10)
    first = d.change('check_price_change', 'invoice', on, invoice='L0001:2026-12')
    assert first.allowed
    assert pl.posted_basis(d.books(), 'L0001:2026-12') is None
    other = d.change('check_price_change', 'invoice', on, invoice='L0001:2026-12', new_cents=12000)
    assert not other.allowed and other.made != first.made


def test_posted_basis_is_not_reused_for_different_changes_or_bad_invoices():
    d = Desk('Synthetic Utah facility', 'Salt Lake City, UT', pl.SYNTHETIC_PROFILES['UT'])
    assert d.checked('UT').allowed and d.terms().allowed and d.notice().allowed
    on = at_local(date(2026, 12, 23), 10)
    first = d.change('check_price_change', 'invoice', on, invoice='L0001:2026-12')
    with d.store.write():
        led = Ledger(d.books())
        pl.post_invoice(led, on, decision=first.made)
        d.store.append(STREAM, led.pending)
    for args in ({'about': 'PC2'}, {'new_cents': 12000}, {'lease': 'L0002'}):
        result = d.change('check_price_change', 'invoice', on, invoice='L0001:2026-12', **args)
        assert not result.allowed and result.made != first.made
    for invoice in ('L0002:2026-12', 'L0001:2026-13', 'L0001:December', 'L0001:2026-12:extra'):
        assert not d.change('check_price_change', 'invoice', on, invoice=invoice).allowed


def test_new_fee_with_zero_previous_amount_reaches_the_gate():
    d = Desk('Synthetic Utah facility', 'Salt Lake City, UT', pl.SYNTHETIC_PROFILES['UT'])
    assert d.checked('UT').allowed and d.terms().allowed
    result = d.change('check_price_change', 'notice_authorization', NOW, kind='fee_change', current_cents=0, new_cents=500)
    assert result.allowed
    with d.store.write():
        led = Ledger(d.books())
        pl.adopt_owner_rule(led, NOW, version=1, sentence='At most ten percent.', typed={'max_increase_percent': 10})
        d.store.append(STREAM, led.pending)
    result = d.change('check_price_change', 'notice_authorization', NOW, kind='fee_change', current_cents=0, new_cents=500)
    assert not result.allowed and result.why == 'OWNER_RULE'


def test_null_notice_methods_get_an_audited_refusal():
    d = Desk('Synthetic Utah facility', 'Salt Lake City, UT', pl.SYNTHETIC_PROFILES['UT'])
    result = d.terms(notice_methods=None)
    assert not result.allowed
    assert not d.books().price_law or not d.books().price_law.terms
    assert any(e.kind == 'gate.decided' for e in d.store._rows(STREAM))


@pytest.mark.parametrize('field', ['dispatch_accepted', 'certificate_of_mailing', 'postage_prepaid'])
def test_notice_evidence_requires_actual_booleans(field):
    d = Desk('Synthetic Utah facility', 'Salt Lake City, UT', pl.SYNTHETIC_PROFILES['UT'])
    result = d.notice(**{field: 'false'})
    assert not result.allowed
    assert not d.books().price_law or not d.books().price_law.notices
    assert any(e.kind == 'gate.decided' and not e.payload['allowed'] for e in d.store._rows(STREAM))


@pytest.mark.parametrize('field', ['attempted_on', 'delivered_on', 'acknowledged_on'])
def test_notice_rejects_future_service_evidence(field):
    d = Desk('Synthetic Utah facility', 'Salt Lake City, UT', pl.SYNTHETIC_PROFILES['UT'])
    assert not d.notice(**{field: '2026-12-01'}).allowed
    assert not d.books().price_law or not d.books().price_law.notices


def test_notice_update_rejects_future_delivery_and_acknowledgment():
    d = Desk('Synthetic Utah facility', 'Salt Lake City, UT', pl.SYNTHETIC_PROFILES['UT'])
    notice = d.notice().made
    for field in ('delivered_on', 'acknowledged_on'):
        assert not d.go('update_notice', change='PC1', notice=notice, **{field: '2026-12-01'}).allowed
    assert d.books().price_law.notices['PC1'][0].delivered_on is None


def test_service_after_the_check_does_not_count():
    d = Desk('Synthetic Utah facility', 'Salt Lake City, UT', pl.SYNTHETIC_PROFILES['UT'])
    assert d.checked('UT').allowed and d.terms().allowed
    later = at_local(date(2026, 12, 1), 10)
    assert d.notice(at=later, attempted_on='2026-12-01', effective_on='2027-01-01').allowed
    result = d.change('check_price_change', 'notice_completion', NOW, effective_on='2027-01-01')
    assert not result.allowed


def test_emergency_lookup_cannot_be_recorded_before_it_happens():
    d = Desk('Synthetic Utah facility', 'Salt Lake City, UT', pl.SYNTHETIC_PROFILES['UT'])
    result = d.go('record_emergency_check', place='UT', checked_on='2026-12-01', valid_until='2026-12-31', source='state page', active=[])
    assert not result.allowed
    assert not d.books().price_law or not d.books().price_law.checks
    assert any(e.kind == 'gate.decided' and not e.payload['allowed'] for e in d.store._rows(STREAM))
    assert d.checked('UT').allowed


def test_an_open_ended_emergency_cannot_be_shortened_by_extension():
    d = Desk('Synthetic Utah facility', 'Salt Lake City, UT', pl.SYNTHETIC_PROFILES['UT'])
    restriction = d.go('record_emergency', place='UT', name='Emergency', source='order', starts='2026-11-20').made
    for end in ('2026-11-19', '2026-12-20'):
        assert not d.go('extend_emergency', restriction=restriction, ends=end, source='order').allowed
    assert d.books().price_law.emergencies[restriction].ends is None
