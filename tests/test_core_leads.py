from datetime import date, datetime, timezone

from core import leads
from core.billing import Ledger
from core.books import Books


NOW = datetime(2026, 11, 23, 15, tzinfo=timezone.utc)


def test_lead_record_survives_replay_with_normalised_contact_details():
    ledger = Ledger()
    ident = leads.capture(ledger, NOW, source="website inquiry", name="  Robin  Example ", phone="(407) 555-0123",
                          email=" Robin@Example.COM ", size="10x10", start="2026-12-01", words="Room for boxes",
                          permissions=["email"], campaign="autumn")
    replay = Books()
    for event in ledger.pending:
        replay.apply(event.kind, event.at, event.payload)
    lead = replay.leads.records[ident]
    assert (lead.name, lead.phone, lead.email) == ("Robin Example", "4075550123", "robin@example.com")
    assert (lead.source, lead.arrived, lead.size, lead.start, lead.words) == ("website inquiry", NOW, "10x10", date(2026, 12, 1), "Room for boxes")
    assert lead.permissions == ["email"] and not lead.stopped and lead.campaign == "autumn" and lead.status == "new"


def test_capture_refuses_invalid_data_without_writing_or_using_an_id():
    import pytest
    from core.billing import Refused

    ledger = Ledger()
    for args in ({"source": "website inquiry", "name": "Robin", "email": "r@example.com", "start": "2026-02-30"},
                 {"source": "website inquiry", "name": "Robin"},
                 {"source": "unknown", "name": "Robin", "phone": "4075550123"}):
        with pytest.raises(Refused):
            leads.capture(ledger, NOW, **args)
    assert not ledger.pending and not ledger.books.counters
    assert leads.capture(ledger, NOW, source="call", name="Robin", phone="4075550123") == "LEAD0001"


def test_matching_leads_are_linked_without_merging_their_records():
    ledger = Ledger()
    first = leads.capture(ledger, NOW, source="call", name="Robin", phone="407-555-0123")
    second = leads.capture(ledger, NOW, source="message", name="Robin E", phone="4075550123")
    assert ledger.books.leads.records[second].matches == [("lead", first)]
    assert ledger.books.leads.records[first].status == "new"
    assert ledger.books.leads.records[second].status == "new"
    assert len(ledger.books.leads.records) == 2


def test_staff_commands_keep_first_decision_and_refuse_unsafe_sends():
    ledger = Ledger()
    ident = leads.capture(ledger, NOW, source="website inquiry", name="Robin", email="r@example.com", permissions=["email"])
    act = leads.Act("one", "send", "staff:front_desk", ident, {"channel": "text", "text": "Hello"})
    denied = leads.command(ledger, act, NOW)
    assert not denied.allowed and denied.why == "NO_CHANNEL_PERMISSION"
    assert leads.command(ledger, act, NOW).repeated
    sent = leads.command(ledger, leads.Act("two", "send", "staff:front_desk", ident, {"channel": "email", "text": "Hello"}), NOW)
    assert sent.allowed and ledger.books.leads.records[ident].history[-1]["kind"] == "sent"
    assert "simulated" in sent.check.lower()


def test_due_steps_and_duplicate_decision():
    from datetime import timedelta
    ledger = Ledger()
    first = leads.capture(ledger, NOW, source="website inquiry", name="Robin", phone="4075550123")
    second = leads.capture(ledger, NOW, source="call", name="Robin", phone="4075550123")
    assert len(leads.due(ledger.books.leads.records[first], NOW + timedelta(minutes=5))) == 1
    result = leads.command(ledger, leads.Act("merge", "confirm_duplicate", "staff:manager", second, {"other": first}), NOW)
    assert result.allowed and ledger.books.leads.records[second].duplicate_of == first
    assert ledger.books.leads.records[first].status == "new"


def test_conversion_requires_matching_signed_rental_and_confirmed_payment():
    from types import SimpleNamespace
    from datetime import timedelta
    ledger = Ledger()
    ident = leads.capture(ledger, NOW, source="website inquiry", name="Robin", phone="4075550123")
    ledger.books.tenants["T1"] = SimpleNamespace(id="T1", phone="4075550123")
    payment = SimpleNamespace(known_at=NOW + timedelta(minutes=2), effective_at=NOW + timedelta(minutes=2), returned=False)
    ledger.books.payments["P1"] = payment
    agreement = SimpleNamespace(state="accepted", at=NOW + timedelta(minutes=1))
    rental = SimpleNamespace(tenant="T1", accepted=True, agreement="A1", payments=["P1"])
    rent = SimpleNamespace(rentals={"R1": rental}, agreements={"A1": agreement})
    ledger.emit("lead.checkout_started", NOW, {"rental": "R1", "tenant": "T1", "lead": ident}, "checkout:linked")
    assert leads.conversion_evidence(ledger.books, rent, ledger.books.leads.records[ident]) == "R1"
    agreement.at = NOW - timedelta(minutes=1)
    assert leads.conversion_evidence(ledger.books, rent, ledger.books.leads.records[ident]) is None


def test_checkout_is_only_captured_after_it_expires_without_payment():
    from types import SimpleNamespace
    from datetime import timedelta
    ledger = Ledger()
    ledger.books.tenants["T1"] = SimpleNamespace(id="T1", name="Robin", phone="4075550123")
    ledger.emit("lead.checkout_started", NOW, {"rental": "R1", "tenant": "T1", "size": "10x10"}, "checkout:R1")
    rental = SimpleNamespace(tenant="T1", expires_at=NOW + timedelta(minutes=30), payments=[], state="pending")
    rent = SimpleNamespace(rentals={"R1": rental}, uncertain=lambda record: False)
    leads.capture_abandoned(ledger, rent, NOW + timedelta(minutes=10))
    assert not leads.state(ledger.books).records
    leads.capture_abandoned(ledger, rent, NOW + timedelta(minutes=31))
    assert [x.source for x in leads.state(ledger.books).records.values()] == ["abandoned checkout"]
    leads.capture_abandoned(ledger, rent, NOW + timedelta(minutes=32))
    assert len(leads.state(ledger.books).records) == 1


def test_reservation_reminder_is_due_the_day_before_move_in():
    from types import SimpleNamespace
    from datetime import timedelta
    from core import work
    ledger = Ledger()
    ident = leads.capture(ledger, NOW, source="call", name="Robin", phone="4075550123")
    ledger.books.tenants["T1"] = SimpleNamespace(id="T1", phone="4075550123")
    reservation = SimpleNamespace(id="RS1", tenant="T1", state="held", start=(NOW + timedelta(days=1)).date(), expires_at=NOW + timedelta(hours=1))
    rent = SimpleNamespace(rentals={}, agreements={}, reservations={"RS1": reservation})
    items = work._leads(ledger.books, rent, NOW)
    assert any(item.ref == ident and item.kind == "reservation reminder" for item in items)


def test_stop_and_missing_lead_refuse_without_a_send():
    ledger = Ledger()
    ident = leads.capture(ledger, NOW, source="message", name="Robin", email="r@example.com", permissions=["email"])
    assert leads.command(ledger, leads.Act("stop", "stop", "staff:manager", ident), NOW).allowed
    denied = leads.command(ledger, leads.Act("send-after-stop", "send", "staff:manager", ident, {"channel": "email", "text": "Hello"}), NOW)
    missing = leads.command(ledger, leads.Act("send-missing", "send", "staff:manager", "LEAD9999", {"channel": "email", "text": "Hello"}), NOW)
    assert not denied.allowed and denied.why == "PERSON_STOPPED"
    assert not missing.allowed
    assert not any(event.kind == "lead.sent" for event in ledger.pending)


def test_duplicate_phone_with_country_code_is_linked():
    ledger = Ledger()
    first = leads.capture(ledger, NOW, source="call", name="Robin", phone="+1 407 555 0123")
    second = leads.capture(ledger, NOW, source="message", name="Robin", phone="4075550123")
    assert ("lead", first) in ledger.books.leads.records[second].matches


def test_stop_applies_to_another_inquiry_from_the_same_person():
    ledger = Ledger()
    first = leads.capture(ledger, NOW, source="message", name="Robin", email="r@example.com", permissions=["email"])
    second = leads.capture(ledger, NOW, source="website inquiry", name="Robin", email="R@example.com", permissions=["email"])
    leads.command(ledger, leads.Act("stop-first", "stop", "staff:manager", first), NOW)
    denied = leads.command(ledger, leads.Act("send-second", "send", "staff:manager", second, {"channel": "email", "text": "Hello"}), NOW)
    assert not denied.allowed and denied.why == "PERSON_STOPPED"


def test_contact_with_due_step_closes_only_that_step():
    from datetime import timedelta
    ledger = Ledger()
    ident = leads.capture(ledger, NOW, source="abandoned checkout", name="Robin", phone="4075550123")
    now = NOW + timedelta(hours=1)
    assert leads.due(ledger.books.leads.records[ident], now)
    leads.command(ledger, leads.Act("call", "contact", "staff:manager", ident, {"outcome": "Left a message", "step": "checkout follow up"}), now)
    assert not leads.due(ledger.books.leads.records[ident], now)


def test_contact_cannot_close_an_unchecked_follow_up():
    ledger = Ledger()
    ident = leads.capture(ledger, NOW, source="website inquiry", name="Robin", phone="4075550123")
    result = leads.command(ledger, leads.Act("bad-step", "contact", "staff:manager", ident, {"outcome": "Called", "step": "follow up 48 hours"}), NOW)
    assert not result.allowed
    assert not ledger.books.leads.records[ident].completed


def test_daily_status_reflects_a_due_follow_up():
    from datetime import timedelta
    from types import SimpleNamespace
    ledger = Ledger()
    ident = leads.capture(ledger, NOW, source="website inquiry", name="Robin", phone="4075550123")
    rent = SimpleNamespace(reservations={})
    leads.refresh_status(ledger, rent, NOW + timedelta(minutes=6))
    assert ledger.books.leads.records[ident].status == "follow-up due"
    count = len(ledger.pending)
    leads.refresh_status(ledger, rent, NOW + timedelta(minutes=7))
    assert len(ledger.pending) == count


def test_confirming_duplicate_records_the_link_on_both_timelines():
    ledger = Ledger()
    first = leads.capture(ledger, NOW, source="message", name="Robin", email="r@example.com")
    second = leads.capture(ledger, NOW, source="call", name="Robin", email="r@example.com")
    leads.command(ledger, leads.Act("confirm", "confirm_duplicate", "staff:manager", second, {"other": first}), NOW)
    assert any(h["kind"] == "duplicate_linked" for h in ledger.books.leads.records[first].history)
    assert ledger.books.leads.records[second].duplicate_of == first


def test_converted_lead_cannot_be_reopened_by_a_contact_attempt():
    ledger = Ledger()
    ident = leads.capture(ledger, NOW, source="website inquiry", name="Robin", phone="4075550123")
    ledger.emit("lead.converted", NOW, {"lead": ident}, "conversion")
    result = leads.command(ledger, leads.Act("late-call", "contact", "staff:manager", ident, {"outcome": "Called"}), NOW)
    assert not result.allowed and result.why == "LEAD_ALREADY_CONVERTED"
    assert ledger.books.leads.records[ident].status == "converted"


def test_conversion_cancels_an_existing_work_item():
    from types import SimpleNamespace
    from core import work
    ledger = Ledger()
    ident = leads.capture(ledger, NOW, source="call", name="Robin", phone="4075550123")
    ledger.emit("lead.converted", NOW, {"lead": ident}, "conversion")
    how, explanation = work._closing(ledger.books, SimpleNamespace(rentals={}), {"key": f"lead:{ident}:first contact", "kind": "first contact", "ref": ident, "closed_by": "Done"})
    assert how == "cancelled" and "marked converted" in explanation


def test_staff_can_record_conversion_only_with_rental_and_payment():
    from types import SimpleNamespace
    from datetime import timedelta
    ledger = Ledger()
    ident = leads.capture(ledger, NOW, source="call", name="Robin", phone="4075550123")
    ledger.books.tenants["T1"] = SimpleNamespace(id="T1", phone="4075550123")
    ledger.books.payments["P1"] = SimpleNamespace(known_at=NOW + timedelta(minutes=2), effective_at=NOW + timedelta(minutes=2), returned=False)
    rent = SimpleNamespace(rentals={"R1": SimpleNamespace(tenant="T1", accepted=True, agreement="A1", payments=["P1"])}, agreements={"A1": SimpleNamespace(state="accepted", at=NOW + timedelta(minutes=1))})
    result = leads.command(ledger, leads.Act("convert", "convert", "staff:manager", ident), NOW + timedelta(minutes=3), rent)
    assert result.allowed and ledger.books.leads.records[ident].status == "converted"


def test_checkout_waiting_for_payment_answer_is_not_abandoned():
    from types import SimpleNamespace
    from datetime import timedelta
    ledger = Ledger()
    ledger.books.tenants["T1"] = SimpleNamespace(id="T1", name="Robin", phone="4075550123")
    ledger.emit("lead.checkout_started", NOW, {"rental": "R1", "tenant": "T1"}, "checkout:R1")
    rental = SimpleNamespace(tenant="T1", expires_at=NOW + timedelta(minutes=30), payments=[], state="pending")
    rent = SimpleNamespace(rentals={"R1": rental}, uncertain=lambda record: True)
    leads.capture_abandoned(ledger, rent, NOW + timedelta(hours=1))
    assert not leads.state(ledger.books).records


def test_unanswered_attempt_does_not_suppress_escalation():
    from datetime import timedelta
    ledger = Ledger()
    ident = leads.capture(ledger, NOW, source="website inquiry", name="Robin", phone="4075550123")
    leads.command(ledger, leads.Act("unanswered", "contact", "staff:manager", ident, {"outcome": "no answer", "step": "first contact"}), NOW + timedelta(minutes=5))
    assert [step for step, _ in leads.due(ledger.books.leads.records[ident], NOW + timedelta(minutes=15))] == ["escalation"]


def test_cancelled_follow_up_does_not_claim_unproved_rental():
    from types import SimpleNamespace
    from core import work
    ledger = Ledger()
    ident = leads.capture(ledger, NOW, source="call", name="Robin", phone="4075550123")
    ledger.emit("lead.converted", NOW, {"lead": ident}, "wrong-conversion")
    rent = SimpleNamespace(rentals={}, agreements={})
    _, explanation = work._closing(ledger.books, rent, {"key": f"lead:{ident}:first contact", "kind": "first contact", "ref": ident, "closed_by": "Done"})
    assert "marked converted" in explanation and "person rented" not in explanation


def test_lead_matches_existing_tenant_by_normalised_email():
    ledger = Ledger()
    tenant = ledger.add_tenant(NOW, "Robin", "", "ROBIN@EXAMPLE.COM")
    ident = leads.capture(ledger, NOW, source="message", name="Robin", email=" robin@example.com ")
    assert ("tenant", tenant) in ledger.books.leads.records[ident].matches


def test_shared_phone_needs_staff_review_and_keeps_followups():
    from datetime import timedelta
    from types import SimpleNamespace
    from core import work
    ledger = Ledger()
    ident = leads.capture(ledger, NOW, source="call", name="Kemi", phone="4075550123")
    ledger.books.tenants["T1"] = SimpleNamespace(id="T1", name="Femi", phone="4075550123")
    ledger.books.payments["P1"] = SimpleNamespace(known_at=NOW + timedelta(minutes=2), effective_at=NOW + timedelta(minutes=2), returned=False)
    rent = SimpleNamespace(rentals={"R1": SimpleNamespace(tenant="T1", accepted=True, agreement="A1", payments=["P1"])}, agreements={"A1": SimpleNamespace(state="accepted", at=NOW + timedelta(minutes=1))}, reservations={})
    now = NOW + timedelta(minutes=6)
    leads.convert(ledger, rent, now)
    assert ledger.books.leads.records[ident].status != "converted"
    items = work._leads(ledger.books, rent, now)
    assert any(item.kind == "rental needs review" for item in items)
    assert any(item.kind == "first contact" for item in items)
