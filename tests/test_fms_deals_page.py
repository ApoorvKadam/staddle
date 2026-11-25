import re

from fastapi.testclient import TestClient

from core import promotions
from core.evidence import money
from desk.fms import Facility
from tests.test_fms import seen, sign_in, site
from tests.test_fms_renting_pages import desk_of, token, vacant
from tests.test_fms_shop_pages import CODES


def test_deals_in_facility_menu_and_requires_sign_in(fms_url, sim):
    app = site(fms_url)
    with TestClient(app) as client:
        assert client.get('/deals', follow_redirects=False).headers['location'] == '/sign-in'
        assert client.post('/deals', follow_redirects=False).headers['location'] == '/sign-in'
    with sign_in(app) as client:
        menu = client.get('/home').text.split('aria-label="Facility"', 1)[1]
        assert re.search(r'href="/deals"[^>]*>.*?Deals', menu, re.S)
        assert client.get('/deals').status_code == 200


def test_deals_show_seeded_terms_and_history_in_words(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        text = seen(client.get('/deals').text)
        for words in ('$1 first month', '$1.00', '5x10', '10x10', 'New customers', 'Deal ends', '1 of 10', '$1,500.00', 'Office manager'):
            assert words in text
        assert not CODES.search(text)
        assert not re.search(r'\b(?:PR\d{4,}|first-month|payment_[A-Za-z0-9]{4,}|rest_of_first_month)\b', text)


def test_only_last_five_versions_are_shown_newest_first(fms_url, sim):
    from datetime import timedelta
    from core import promotions
    from core.billing import Ledger
    from core.books import local
    from core.evidence import day
    from tests.test_fms_renting_pages import desk_of

    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        store, stream, now = fd.ready(), fd.stream(ident), fd.clock()
        with store.write():
            led = Ledger(store.books(stream))
            terms = dict(promotions.state(led.books).versions['first-month'][-1].terms)
            for n in range(6):
                terms['price_cents'] = 200 + n
                at = now - timedelta(days=6 - n)
                promotions.publish_version(led, 'first-month', dict(terms), local(at).date(), 'staff:front_desk', at)
            store.append(stream, led.pending)
        text = seen(client.get('/deals').text)
        dates = [f'{day(local(now - timedelta(days=n)).date())}, {local(now).year}' for n in range(1, 6)]
        assert all(d in text for d in dates)
        assert [text.index(d) for d in dates] == sorted(text.index(d) for d in dates)
        assert f'{day(local(now - timedelta(days=6)).date())}, {local(now).year}' not in text


def test_publish_and_core_refusals_leave_the_books_unchanged(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        state = promotions.state(fd.ready().books(fd.stream(ident)))
        terms = state.versions['first-month'][-1].terms
        fields = {'promotion': 'first-month', 'name': terms['name'], 'unit_types': ', '.join(terms['unit_types']),
                  'customers': terms['customers'], 'price': '1.00', 'percent': '', 'covers': terms['covers'],
                  'expires': terms['expires'], 'stacks_with': '', 'transfer': terms['transfer'],
                  'max_redemptions': str(terms['max_redemptions']), 'budget': '1500.00',
                  'effective': fd.today().isoformat()}
        def post():
            return client.post('/deals', data={**fields, 'token': token(client.get('/deals').text)}, follow_redirects=False)
        assert post().status_code == 303
        assert 'latest version already has these terms' in seen(client.get('/deals').text)
        assert len(promotions.state(fd.ready().books(fd.stream(ident))).versions['first-month']) == 1
        fields['price'] = '2.00'
        assert post().status_code == 303
        assert len(promotions.state(fd.ready().books(fd.stream(ident))).versions['first-month']) == 2
        fields['budget'] = '0.00'
        assert post().status_code == 303
        assert 'budget must be at least one cent' in seen(client.get('/deals').text)
        assert len(promotions.state(fd.ready().books(fd.stream(ident))).versions['first-month']) == 2


def test_stacking_selection_keeps_distinct_deals_with_the_same_name(fms_url, sim):
    import re
    from core.billing import Ledger
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        store, stream, now = fd.ready(), fd.stream(ident), fd.clock()
        with store.write():
            led = Ledger(store.books(stream))
            terms = dict(promotions.state(led.books).versions['first-month'][-1].terms)
            for key in ('another-one', 'another-two'):
                promotions.publish_version(led, key, dict(terms), fd.today(), 'staff:office_manager', now)
            store.append(stream, led.pending)
        page = client.get('/deals').text
        form = page.split('name="promotion" value="first-month"', 1)[1].split('</form>', 1)[0]
        field = re.search(r'name="(stack_\d+)" value="another-two"', form).group(1)
        fields = {'token': token(page), 'promotion': 'first-month', 'name': terms['name'],
                  'unit_types': ', '.join(terms['unit_types']), 'customers': terms['customers'],
                  'price': '2.00', 'covers': terms['covers'], 'expires': terms['expires'],
                  'transfer': terms['transfer'], 'max_redemptions': str(terms['max_redemptions']),
                  'budget': '1500.00', 'effective': fd.today().isoformat(), field: 'another-two'}
        assert client.post('/deals', data=fields, follow_redirects=False).status_code == 303
        latest = promotions.state(store.books(stream)).versions['first-month'][-1]
        assert latest.terms['stacks_with'] == ['another-two']
        with store.write():
            led = Ledger(store.books(stream))
            reordered = dict(promotions.state(led.books).versions['first-month'][-1].terms)
            reordered['stacks_with'] = ['another-two', 'another-one']
            promotions.publish_version(led, 'first-month', reordered, fd.today(), 'staff:office_manager', now)
            store.append(stream, led.pending)
        page = client.get('/deals').text
        form = page.split('name="promotion" value="first-month"', 1)[1].split('</form>', 1)[0]
        fields['token'] = token(page)
        fields = {k: v for k, v in fields.items() if not k.startswith('stack_')}
        for key in ('another-one', 'another-two'):
            field = re.search(r'name="(stack_\d+)" value="' + key + '"', form).group(1)
            fields[field] = key
        assert client.post('/deals', data=fields, follow_redirects=False).status_code == 303
        assert 'latest version already has these terms' in seen(client.get('/deals').text)
        assert len(promotions.state(store.books(stream)).versions['first-month']) == 3


def test_percent_offer_with_an_explicit_empty_price_can_be_saved_unchanged(fms_url, sim):
    from core.billing import Ledger
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        store, stream, now = fd.ready(), fd.stream(ident), fd.clock()
        with store.write():
            led = Ledger(store.books(stream))
            terms = dict(promotions.state(led.books).versions['first-month'][-1].terms)
            terms.update(price_cents=None, percent=25, note='Existing term')
            promotions.publish_version(led, 'first-month', terms, fd.today(), 'staff:office_manager', now)
            store.append(stream, led.pending)
        page = client.get('/deals')
        assert page.status_code == 200 and '25% off' in seen(page.text)
        fields = {'token': token(page.text), 'promotion': 'first-month', 'name': terms['name'],
                  'unit_types': ', '.join(terms['unit_types']), 'customers': terms['customers'],
                  'percent': '25', 'covers': terms['covers'], 'expires': terms['expires'],
                  'transfer': terms['transfer'], 'max_redemptions': str(terms['max_redemptions']),
                  'budget': '1500.00', 'effective': fd.today().isoformat()}
        assert client.post('/deals', data=fields, follow_redirects=False).status_code == 303
        assert 'latest version already has these terms' in seen(client.get('/deals').text)
        assert len(promotions.state(store.books(stream)).versions['first-month']) == 2


def test_mayas_lease_shows_its_deal_and_first_full_price(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        f = Facility(fd, ident, everything=True)
        person = next(p for p in f.people.values() if p['name'] == 'Maya Okafor')
        red = next(r for r in promotions.state(f.b).redemptions.values() if r.tenant == person['id'])
        lease = f.b.leases[red.lease]
        text = seen(client.get('/tenants/' + person['slug']).text)
        assert '$1 first month' in text
        assert red.first_full_price.strftime('%B %Y') in text
        assert money(lease.rent_cents) in text
        assert money(red.planned_cents) in text
        assert red.start.strftime('%B %Y') in text


def test_online_rental_shows_and_applies_the_deal(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        unit = next(u for u in vacant(fd, ident, 20) if fd.rent_view(ident).units[u].size == '5x10')
        assert '$1 first month' in seen(client.get('/rent?size=5x10').text)
        page = client.get('/rent/' + unit).text
        assert '$1 first month' in seen(page)
        response = client.post('/rent/' + unit, data={'token': token(page), 'name': 'Test Renter', 'phone': '+14075550199', 'promotion': 'first-month'}, follow_redirects=False)
        assert response.status_code == 303 and '/rent/checkout/' in response.headers['location']
        assert '$1 first month' in seen(client.get(response.headers['location']).text)
