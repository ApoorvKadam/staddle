from core import renting, street_rates
from tests.test_fms import seen, sign_in, site
from tests.test_fms_renting_pages import desk_of, vacant


def test_each_size_proposes_from_its_own_row_and_a_varied_size_says_why(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        page = client.get('/pricing').text
        assert 'Propose a rate for' not in seen(page)
        assert all(f'name="size" value="{size}"' in page for size in ('5x10', '10x10', '10x20'))
        unit = next(u for u in vacant(fd, ident) if fd.rent_view(ident).units[u].size == '5x10')
        rent = fd.rent_view(ident).units[unit].rent_cents
        assert renting.run(fd.ready(), fd.stream(ident), renting.Act('vary-' + unit, 'price_unit', 'staff:office_manager', unit,
                           args={'size': '5x10', 'rent_cents': rent + 500}), fd.clock()).allowed
        page = client.get('/pricing').text
        assert 'name="size" value="5x10"' not in page and 'name="size" value="10x10"' in page
        assert 'one rate cannot be proposed for the whole size' in seen(page)


def test_rows_show_prices_limits_and_vacancy(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        rent = fd.rent_view(ident)
        unit = next(u for u in rent.units.values() if u.size and u.rent_cents)
        limits = street_rates.Limits(500, 700, 100, 50000)
        assert street_rates.set_limits(fd.ready(), fd.stream(ident), fd.clock(), size=unit.size, limits=limits, by='owner').allowed
        text = seen(client.get('/pricing').text)
        assert unit.size in text
        assert '$5.00' in text and '$7.00' in text and '$500.00' in text
        assert 'Vacant' in text and 'Last change' in text


def test_waiting_proposal_shows_recorded_checks(fms_url, sim):
    from core import renting
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        store, stream, now = fd.ready(), fd.stream(ident), fd.clock()
        unit = next(u for u in fd.rent_view(ident).units.values() if u.size and u.rent_cents)
        for other in fd.rent_view(ident).units.values():
            if other.size == unit.size:
                assert renting.run(store, stream, renting.Act('row-setup-' + other.id, 'price_unit', 'staff:office_manager', other.id,
                                    args={'size': unit.size, 'rent_cents': unit.rent_cents}), now).allowed
        limits = street_rates.Limits(1000, 1000, 100, 50000)
        assert street_rates.set_limits(store, stream, now, size=unit.size, limits=limits, by='owner').allowed
        proposal = street_rates.propose(store, stream, now, size=unit.size, current_cents=unit.rent_cents,
                                        new_cents=unit.rent_cents - 500, reason='Seasonal price', by='staff:office_manager', limits=limits)
        assert proposal.allowed
        text = seen(client.get('/pricing').text)
        assert 'Waiting for approval' in text and 'Seasonal price' in text
        assert 'checks allowed' in text
        assert proposal.made not in text
