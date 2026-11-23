from tests.test_fms import seen, sign_in, site


def test_storefront_inquiry_appears_on_leads(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        response = client.post("/rent/inquiry", data={"name": "Morgan Prospect", "phone": "4075550123", "size": "10x10", "message": "Boxes", "consent": "yes"}, follow_redirects=False)
        assert response.status_code == 303
        page = seen(client.get("/leads").text)
        assert "Morgan Prospect" in page and "Boxes" in page


def test_rental_converts_inquiry_and_refuses_follow_up(fms_url, sim):
    from core import leads
    from tests.test_fms_renting_pages import desk_of, new_tenant, start_rental, vacant
    app = site(fms_url)
    with sign_in(app) as client:
        client.post("/rent/inquiry", data={"name": "Morgan Prospect", "phone": "4075550123", "consent": "yes"})
        fd, ident = desk_of(app, client)
        lead = next(x for x in leads.state(fd.view(ident)[0]).records.values() if x.name == "Morgan Prospect")
        tenant = new_tenant(fd, ident, name="Morgan Prospect", phone="4075550123")
        start_rental(fd, ident, tenant, vacant(fd, ident)[0], card="card_ok")
        lead = leads.state(fd.view(ident)[0]).records[lead.id]
        assert lead.status != "converted"
        decision = leads.run(fd.ready(), fd.stream(ident), leads.Act("confirm-conversion", "convert", "staff:front_desk", lead.id), fd.clock())
        assert decision.allowed
        decision = leads.run(fd.ready(), fd.stream(ident), leads.Act("after-conversion", "send", "staff:front_desk", lead.id, {"channel": "phone", "text": "Hello"}), fd.clock())
        assert not decision.allowed and decision.why == "LEAD_ALREADY_CONVERTED"
        assert "Converted" in seen(client.get("/leads").text)


def test_today_links_due_inquiry_and_conversion_review(fms_url, sim):
    from core import leads
    from core.billing import Ledger
    from tests.test_fms_renting_pages import desk_of
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        store, stream = fd.ready(), fd.stream(ident)
        with store.write():
            ledger = Ledger(store.books(stream))
            lead = leads.capture(ledger, fd.clock(), source="call", name="Review Example", phone="4075550198")
            ledger.emit("lead.converted", fd.clock(), {"lead": lead}, "test:wrong-conversion")
            store.append(stream, ledger.pending)
        page = client.get("/today")
        assert page.status_code == 200
        assert "Conversion needs review" in seen(page.text)
        assert f'/leads/{lead}' in page.text
        lead_record = next(x for x in leads.state(fd.view(ident)[0]).records.values() if x.phone == "4075550101")
        assert lead_record.name in seen(page.text)
        detail = client.get(f"/leads/{lead_record.id}")
        assert 'name="step"' in detail.text and "escalation" in detail.text


def test_staff_can_record_a_walk_in(fms_url, sim):
    import re
    with sign_in(site(fms_url)) as client:
        page = client.get("/leads")
        token = re.search(r'name="token" value="([0-9a-f]{12})"', page.text).group(1)
        response = client.post("/leads/new", data={"token": token, "action": "record_walk_in", "name": "Casey Example", "phone": "4075550188"}, follow_redirects=False)
        assert response.status_code == 303
        assert "Casey Example" in seen(client.get("/leads").text)


def test_lead_pages_present_readable_details_and_matches(fms_url, sim):
    from datetime import datetime, timezone
    from core import leads
    from core.billing import Ledger
    from tests.test_fms_renting_pages import desk_of

    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        store, stream = fd.ready(), fd.stream(ident)
        with store.write():
            ledger = Ledger(store.books(stream))
            lead = leads.capture(ledger, datetime(2025, 11, 1, 12, 30, tzinfo=timezone.utc), source="website inquiry", name="Pat Example", phone="4075550101", words="Do you have space?")
            store.append(stream, ledger.pending)
        listing = seen(client.get('/leads').text)
        detail = seen(client.get(f'/leads/{lead}').text)
        assert 'Nov 1, 2025 8:30 am' in listing
        assert 'Follow-up after 48 hours, overdue since Nov 3 at 7:30 am' in listing      # clocks went back on Nov 2
        assert '(407) 555-0101' in detail
        assert '?.' not in listing and '. .' not in listing
        assert '?.' not in detail and '. .' not in detail
        seeded = next(x for x in leads.state(fd.view(ident)[0]).records.values() if x.phone == "4075550101" and x.id != lead)
        assert 'Matching records' in detail and seeded.name in detail
        assert 'matching phone' in detail
        assert 'href="/leads/' in client.get(f'/leads/{lead}').text
        assert 'LEAD0001' not in listing and 'LEAD0001' not in detail


def test_storefront_units_precede_inquiry_form(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        page = client.get('/rent').text
        assert page.index('website-grid') < page.index('Ask about storage')


def test_lead_writes_run_in_worker_threads(fms_url, sim, monkeypatch):
    import re
    import threading
    from desk import fms
    from core import leads
    app = site(fms_url)
    calls = []
    original = fms.run_in_threadpool
    async def tracked(func, *args, **kwargs):
        event_thread = threading.get_ident()
        def check_thread():
            assert threading.get_ident() != event_thread
            calls.append(True)
            return func(*args, **kwargs)
        return await original(check_thread)
    monkeypatch.setattr(fms, "run_in_threadpool", tracked)
    with sign_in(app) as client:
        client.post("/rent/inquiry", data={"name": "Thread Test", "phone": "4075550189"})
        assert len(calls) == 1
        page = client.get("/leads")
        token = re.search(r'name="token" value="([0-9a-f]{12})"', page.text).group(1)
        client.post("/leads/new", data={"token": token, "action": "record_call", "name": "Thread Call", "phone": "4075550190"})
        assert len(calls) == 2
        from tests.test_fms_renting_pages import desk_of
        fd, ident = desk_of(app, client)
        lead = next(x for x in leads.state(fd.view(ident)[0]).records.values() if x.name == "Thread Call")
        client.post(f"/leads/{lead.id}", data={"token": token, "action": "stop"})
        assert len(calls) == 3


def test_a_converted_inquiry_offers_only_what_can_still_be_recorded(fms_url, sim):
    from core import leads
    from tests.test_fms_renting_pages import desk_of

    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        converted = next(x for x in leads.state(fd.view(ident)[0]).records.values() if x.status == "converted")
        page = seen(client.get(f"/leads/{converted.id}").text)
        assert "Converted. Follow-ups have stopped." in page
        assert "Record request to stop" in page
        for refused in ("Record attempt", "Schedule a follow-up", "Record simulated message", "Mark unqualified", "Record conversion", "Confirm duplicate"):
            assert refused not in page
        open_one = next(x for x in leads.state(fd.view(ident)[0]).records.values() if x.status != "converted")
        assert "Record attempt" in seen(client.get(f"/leads/{open_one.id}").text)


def test_after_a_stop_request_the_page_offers_no_message(fms_url, sim):
    from core import leads
    from tests.test_fms_renting_pages import desk_of

    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lead = next(x for x in leads.state(fd.view(ident)[0]).records.values() if x.status != "converted")
        assert "Record simulated message" in seen(client.get(f"/leads/{lead.id}").text)
        token = __import__("re").search(r'name="token" value="([^"]+)"', client.get(f"/leads/{lead.id}").text).group(1)
        client.post(f"/leads/{lead.id}", data={"token": token, "action": "stop"})
        page = seen(client.get(f"/leads/{lead.id}").text)
        assert "Record simulated message" not in page and "Record request to stop" not in page
        assert "They asked not to be contacted." in page


def test_the_message_form_lists_only_allowed_channels(fms_url, sim):
    from datetime import datetime, timezone
    from core import leads
    from core.billing import Ledger
    from tests.test_fms_renting_pages import desk_of

    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        store, stream = fd.ready(), fd.stream(ident)
        with store.write():
            ledger = Ledger(store.books(stream))
            texts = leads.capture(ledger, datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc), source="call", name="Ana Ruiz", phone="4075550177", permissions=["text"])
            none = leads.capture(ledger, datetime(2026, 11, 20, 16, 0, tzinfo=timezone.utc), source="call", name="Ben Ruiz", phone="4075550178")
            store.append(stream, ledger.pending)
        html = client.get(f"/leads/{texts}").text
        assert 'value="text"' in html and 'value="email"' not in html and 'option value="phone"' not in html
        page = seen(client.get(f"/leads/{none}").text)
        assert "Record simulated message" not in page and "not given permission to be messaged" in page
