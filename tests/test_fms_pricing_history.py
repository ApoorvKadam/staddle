from core import renting, street_rates
from tests.test_fms import seen, sign_in, site
from tests.test_fms_renting_pages import desk_of


def test_history_shows_five_latest_changes_in_order(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        store, stream, now = fd.ready(), fd.stream(ident), fd.clock()
        unit = next(u for u in fd.rent_view(ident).units.values() if u.size and u.rent_cents)
        for other in fd.rent_view(ident).units.values():
            if other.size == unit.size:
                assert renting.run(store, stream, renting.Act('history-setup-' + other.id, 'price_unit', 'staff:office_manager', other.id,
                                    args={'size': unit.size, 'rent_cents': unit.rent_cents}), now).allowed
        limits = street_rates.Limits(1000, 1000, 100, 50000)
        assert street_rates.set_limits(store, stream, now, size=unit.size, limits=limits, by='owner').allowed
        for number in range(6):
            proposal = street_rates.propose(store, stream, now, size=unit.size, current_cents=unit.rent_cents,
                                            new_cents=unit.rent_cents - 100, reason=f'Change number {number}',
                                            by='staff:office_manager', limits=limits)
            assert proposal.allowed
        page = client.get('/pricing').text
        row = page.split(f'<tr><td>{unit.size}</td>', 1)[1].split('</tr>', 1)[0]
        history = seen(row.split('<summary>Price history</summary>', 1)[1].split('</details>', 1)[0])
        assert 'Change number 0' not in history
        assert all(f'Change number {n}' in history for n in range(1, 6))
        assert history.index('Change number 5') < history.index('Change number 1')
        assert 'Office manager' in history
