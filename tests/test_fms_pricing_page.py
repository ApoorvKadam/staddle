import re

from fastapi.testclient import TestClient

from core import renting, street_rates
from tests.test_fms import seen
from tests.test_fms_renting_pages import desk_of, token, vacant

from tests.test_fms import sign_in, site


def test_pricing_in_facility_menu_and_requires_sign_in(fms_url, sim):
    app = site(fms_url)
    with TestClient(app) as client:
        assert client.get('/pricing', follow_redirects=False).headers['location'] == '/sign-in'
        assert client.post('/pricing', follow_redirects=False).headers['location'] == '/sign-in'
    with sign_in(app) as client:
        menu = client.get('/home').text.split('aria-label="Facility"', 1)[1]
        assert re.search(r'href="/pricing"[^>]*>.*?Pricing', menu, re.S)
        assert client.get('/pricing').status_code == 200


def test_pricing_calls_demonstration_rules_by_name_and_hides_codes(fms_url, sim):
    from tests.test_fms import seen
    from tests.test_fms_shop_pages import CODES
    with sign_in(site(fms_url)) as client:
        text = seen(client.get('/pricing').text)
        assert 'modeled for demonstration only' in text
        assert not CODES.search(text)
        assert 'Orlando, FL' in text                       # the city reads as on every other page
        assert not re.search(r'\b(?:SR\d{4,}|SRS\d{4,}|PLD\d{4,}|CA|UT|NYC)\b', text)
        assert not re.search(r'\b(?:payment|listing|lease|facility)_[A-Za-z0-9]{4,}\b', text)
        assert 'legally compliant' not in text.lower()


def test_propose_and_stop_from_page(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        unit = next(u for u in fd.rent_view(ident).units.values() if u.size and u.rent_cents)
        for other in fd.rent_view(ident).units.values():
            if other.size == unit.size:
                result = renting.run(fd.ready(), fd.stream(ident), renting.Act('pricing-setup-' + other.id, 'price_unit', 'staff:office_manager', other.id, args={'size': unit.size, 'rent_cents': unit.rent_cents}), fd.clock())
                assert result.allowed
        limits = street_rates.Limits(1000, 1000, 100, 50000)
        assert street_rates.set_limits(fd.ready(), fd.stream(ident), fd.clock(), size=unit.size, limits=limits, by='owner').allowed
        page = client.get('/pricing').text
        fields = {'token': token(page), 'size': unit.size, 'rate': f'{(unit.rent_cents - 2000) / 100:.2f}', 'reason': 'Seasonal price', 'action': 'propose'}
        assert client.post('/pricing', data=fields, follow_redirects=False).status_code == 303
        assert 'step limit' in seen(client.get('/pricing').text)
        assert not street_rates.history(fd.ready(), fd.stream(ident))
        today = client.get('/today')          # a stopped proposal is staff work on Today, linked to Pricing
        assert today.status_code == 200 and 'Open it in Pricing' in seen(today.text)
        fields['rate'] = f'{(unit.rent_cents - 500) / 100:.2f}'
        fields['token'] = token(client.get('/pricing').text)
        assert client.post('/pricing', data=fields, follow_redirects=False).status_code == 303
        assert 'Waiting for approval' in seen(client.get('/pricing').text)


def test_rule_hold_is_shown_in_words_without_making_a_proposal(fms_url, sim):
    from core import price_law
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        unit = next(u for u in fd.rent_view(ident).units.values() if u.size and u.rent_cents)
        # Florida's last check ran out before today, so an increase waits until staff check again
        assert price_law.run(fd.ready(), fd.stream(ident), price_law.Act('expired-check', 'record_emergency_check', 'staff:office_manager',
                             form={'place': 'FL', 'checked_on': '2026-11-01', 'valid_until': '2026-11-10', 'source': 'state page', 'active': []}),
                             fd.clock()).allowed
        page = client.get('/pricing').text
        response = client.post('/pricing', data={'token': token(page), 'action': 'propose', 'size': unit.size,
                                                'rate': f'{(unit.rent_cents + 100) / 100:.2f}', 'reason': 'Seasonal price'}, follow_redirects=False)
        assert response.status_code == 303
        text = seen(client.get('/pricing').text)
        assert 'emergency' in text.lower() and 'Florida' in text
        assert not re.search(r'\bSR\d{4,}\b', text)
        assert not street_rates.history(fd.ready(), fd.stream(ident))


def test_a_size_rate_is_proposed_approved_published_and_shown_on_rent(fms_url, sim):
    """One whole path on a fresh live copy, through the page's own forms: the seeded size has one rate and its owner limits, Florida's
    rules allow the increase, and the rent pages show the new rate once it is published."""
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        view = fd.rent_view(ident)
        size = '5x10'
        uid = next(u for u in vacant(fd, ident) if view.units[u].size == size)
        current = view.units[uid].rent_cents
        assert all(u.rent_cents == current for u in view.units.values() if u.size == size)
        amount = f'{(current + 500) / 100:.2f}'
        page = client.get('/pricing').text
        assert client.post('/pricing', data={'token': token(page), 'action': 'propose', 'size': size, 'rate': amount,
                                             'reason': 'Seasonal price'}, follow_redirects=False).status_code == 303
        page = client.get('/pricing').text
        proposal = re.search(r'name="about" value="([^"]+)"', page).group(1)
        assert 'Waiting for approval' in seen(page)
        for action in ('approve', 'publish'):
            page = client.get('/pricing').text
            assert client.post('/pricing', data={'token': token(page), 'action': action, 'about': proposal},
                               follow_redirects=False).status_code == 303
        text = seen(client.get('/pricing').text)
        assert 'Held for review' not in text and 'Waiting for approval' not in text and 'waiting to publish' not in text
        history = street_rates.history(fd.ready(), fd.stream(ident))
        assert [v.status for v in history if v.id == proposal] == ['proposed', 'approved', 'published']
        decisions = [e.payload for e in fd.ready().events(fd.stream(ident)) if e.kind == 'price_law.decided' and e.payload['change'] == proposal]
        assert decisions and all(d['outcome'] == 'allow' for d in decisions), decisions
        shown = '$' + amount.removesuffix('.00')       # the storefront writes whole dollars without cents (site_money)
        assert shown in seen(client.get('/rent', params={'size': size}).text)
        assert shown in seen(client.get('/rent/' + uid).text)


def test_approve_publish_reaches_website(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        uid = vacant(fd, ident)[0]
        unit = fd.rent_view(ident).units[uid]
        for other in fd.rent_view(ident).units.values():
            if other.size == unit.size:
                result = renting.run(fd.ready(), fd.stream(ident), renting.Act('publish-setup-' + other.id, 'price_unit', 'staff:office_manager', other.id, args={'size': unit.size, 'rent_cents': unit.rent_cents}), fd.clock())
                assert result.allowed
        assert street_rates.set_limits(fd.ready(), fd.stream(ident), fd.clock(), size=unit.size,
                                       limits=street_rates.Limits(1000, 1000, 100, 50000), by='owner').allowed
        amount = f'{(unit.rent_cents - 525) / 100:.2f}'
        page = client.get('/pricing').text
        assert client.post('/pricing', data={'token': token(page), 'action': 'propose', 'size': unit.size,
                                             'rate': amount, 'reason': 'Seasonal price'}).status_code == 200
        page = client.get('/pricing').text
        proposal = re.search(r'name="about" value="([^"]+)"', page).group(1)
        for action in ('approve', 'publish'):
            assert client.post('/pricing', data={'token': token(page), 'action': action, 'about': proposal}, follow_redirects=False).status_code == 303
            page = client.get('/pricing').text
            if action == 'approve':
                assert 'Approved, waiting to publish' in seen(page), seen(page)
            else:
                assert 'Waiting for approval' not in seen(page) and 'waiting to publish' not in seen(page), seen(page)
        assert '$' + amount in seen(client.get('/rent', params={'size': unit.size}).text)
        assert '$' + amount in seen(client.get('/rent/' + uid).text)


def test_core_role_refusal_is_shown(fms_url, sim, monkeypatch):
    import desk.fms
    monkeypatch.setattr(desk.fms, 'STAFF', 'visitor')
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        store, stream, now = fd.ready(), fd.stream(ident), fd.clock()
        unit = next(u for u in fd.rent_view(ident).units.values() if u.size and u.rent_cents)
        for other in fd.rent_view(ident).units.values():
            if other.size == unit.size:
                assert renting.run(store, stream, renting.Act('role-setup-' + other.id, 'price_unit', 'staff:office_manager', other.id,
                                    args={'size': unit.size, 'rent_cents': unit.rent_cents}), now).allowed
        limits = street_rates.Limits(1000, 1000, 100, 50000)
        assert street_rates.set_limits(store, stream, now, size=unit.size, limits=limits, by='owner').allowed
        proposal = street_rates.propose(store, stream, now, size=unit.size, current_cents=unit.rent_cents,
                                        new_cents=unit.rent_cents - 100, reason='Seasonal price', by='staff:office_manager', limits=limits)
        assert proposal.allowed
        page = client.get('/pricing').text
        response = client.post('/pricing', data={'token': token(page), 'action': 'approve', 'about': proposal.made}, follow_redirects=False)
        assert response.status_code == 303
        assert 'Only staff may do this.' in seen(client.get('/pricing').text)
        assert street_rates.history(store, stream)[-1].status == 'proposed'
