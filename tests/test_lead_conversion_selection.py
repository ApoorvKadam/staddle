from core import leads
from tests.test_fms import seen, sign_in, site
from tests.test_fms_renting_pages import desk_of, new_tenant, start_rental, token, vacant


def test_staff_selects_matching_renter_before_conversion(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        client.post('/rent/inquiry', data={'name': 'Jordan Rivera', 'phone': '4075550177', 'consent': 'yes'})
        fd, ident = desk_of(app, client)
        lead = next(x for x in leads.state(fd.view(ident)[0]).records.values() if x.name == 'Jordan Rivera')
        first = new_tenant(fd, ident, name='Sam Rivera', phone='4075550177')
        chosen = new_tenant(fd, ident, name='Jordan Rivera', phone='4075550177')
        units = vacant(fd, ident, 2)
        _, first_rental = start_rental(fd, ident, first, units[0], card='card_ok')
        _, chosen_rental = start_rental(fd, ident, chosen, units[1], card='card_ok')
        assert first_rental != chosen_rental
        assert 'Confirm the match on the inquiry' in seen(client.get('/today').text)
        page = client.get(f'/leads/{lead.id}').text
        assert 'Sam Rivera' in page and 'Jordan Rivera' in page
        reply = client.post(f'/leads/{lead.id}', data={'token': token(page), 'action': 'convert'}, follow_redirects=True)
        assert 'Choose which renter' in seen(reply.text)
        assert leads.state(fd.view(ident)[0]).records[lead.id].status != 'converted'
        page = reply.text
        reply = client.post(f'/leads/{lead.id}', data={'token': token(page), 'action': 'convert', 'tenant': 'T-nobody'}, follow_redirects=True)
        assert 'does not share' in seen(reply.text)
        assert leads.state(fd.view(ident)[0]).records[lead.id].status != 'converted'
        page = reply.text
        client.post(f'/leads/{lead.id}', data={'token': token(page), 'action': 'convert', 'tenant': chosen})
        converted = leads.state(fd.view(ident)[0]).records[lead.id]
        assert converted.status == 'converted'
        assert converted.history[-1]['rental'] == chosen_rental
