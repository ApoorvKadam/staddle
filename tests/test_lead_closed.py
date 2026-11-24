"""A closed inquiry (not a fit, or a duplicate) is not reopened through the contact, schedule or close steps: the core refuses them with a
plain reason and the inquiry's page does not offer them. Stopping contact stays possible. Each test fails on the code before the fix."""
from datetime import datetime, timezone

import pytest

from core import leads
from core.billing import Ledger

from tests.test_fms import seen, sign_in, site
from tests.test_fms_renting_pages import desk_of, token

NOW = datetime(2026, 11, 23, 15, tzinfo=timezone.utc)
STEPS = (("contact", {"outcome": "reached"}), ("schedule", {"due": "2026-12-01T10:00"}), ("unqualify", {"reason": "Changed their mind"}))


def run(ledger, ident, action, form, name):
    return leads.command(ledger, leads.Act(name, action, "staff:front_desk", ident, form), NOW)


def closed_lead(how):
    ledger = Ledger()
    first = leads.capture(ledger, NOW, source="website inquiry", name="Robin", phone="4075550123")
    if how == "unqualified":
        assert run(ledger, first, "unqualify", {"reason": "Wants another city"}, "close").allowed
        return ledger, first
    second = leads.capture(ledger, NOW, source="call", name="Robin", phone="4075550123")
    assert run(ledger, second, "confirm_duplicate", {"other": first}, "merge").allowed
    return ledger, second


@pytest.mark.parametrize("how", ["unqualified", "duplicate"])
def test_a_closed_inquiry_takes_no_contact_schedule_or_close_step(how):
    ledger, ident = closed_lead(how)
    before = len(ledger.books.leads.records[ident].history)
    for action, form in STEPS:
        refused = run(ledger, ident, action, form, f"again-{action}")
        assert not refused.allowed and refused.why == "LEAD_CLOSED" and "not reopened" in refused.check
    assert len(ledger.books.leads.records[ident].history) == before                   # nothing was recorded for any of them
    assert ledger.books.leads.records[ident].status == how


@pytest.mark.parametrize("how", ["unqualified", "duplicate"])
def test_a_closed_inquiry_can_still_record_a_request_to_stop(how):
    ledger, ident = closed_lead(how)
    assert run(ledger, ident, "stop", {}, "stop").allowed and ledger.books.leads.records[ident].stopped


def test_an_open_inquiry_still_takes_contact_schedule_and_close_steps():
    ledger = Ledger()
    ident = leads.capture(ledger, NOW, source="website inquiry", name="Robin", phone="4075550123")
    for action, form in STEPS:
        assert run(ledger, ident, action, form, f"open-{action}").allowed, action


def test_the_page_of_a_closed_inquiry_does_not_offer_the_contact_schedule_or_close_forms(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        client.post("/rent/inquiry", data={"name": "Pat Closed", "phone": "4075550166", "consent": "yes"})
        fd, ident = desk_of(app, client)
        lead = next(x for x in leads.state(fd.view(ident)[0]).records.values() if x.name == "Pat Closed")
        page = client.get(f"/leads/{lead.id}").text
        for value in ("contact", "schedule", "unqualify"):
            assert f'name="action" value="{value}"' in page                          # an open inquiry offers them
        sent = client.post(f"/leads/{lead.id}", data={"token": token(page), "action": "unqualify", "reason": "Wants another city"}, follow_redirects=False)
        assert sent.status_code == 303
        page = client.get(f"/leads/{lead.id}").text
        for value in ("contact", "schedule", "unqualify"):
            assert f'name="action" value="{value}"' not in page
        assert "not reopened" in seen(page) and 'value="stop"' in page                # it says why, and a request to stop can still be recorded
        refused = client.post(f"/leads/{lead.id}", data={"token": token(page), "action": "contact", "outcome": "reached"}, follow_redirects=False)
        assert "Nothing changed. This inquiry was closed as not a fit" in seen(client.get(refused.headers["location"]).text)
