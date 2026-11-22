"""Walkthrough reads each copy and sends staff steps through the walk gate."""
import re

from fastapi.testclient import TestClient

from core import reconcile
from desk.fms import Facility
from desk.wording import plain
from tests.test_fms import seen, sign_in, site
from tests.test_fms_renting_pages import desk_of, rent_of, token


def step(client, action, **fields):
    page = client.get('/walkthrough').text
    return client.post('/walkthrough', data={'token': token(page), 'action': action, **fields}, follow_redirects=False)


def test_sign_in_required(fms_url, sim):
    with TestClient(site(fms_url)) as client:
        assert client.get('/walkthrough', follow_redirects=False).headers['location'] == '/sign-in'
        assert client.post('/walkthrough', follow_redirects=False).headers['location'] == '/sign-in'


def test_walk_and_findings_come_from_the_copy(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        f = Facility(fd, ident)
        latest = max(f.rent.floor.walks.values(), key=lambda w: (w.started, w.id))
        page = client.get('/walkthrough').text
        route = page.split('id="latest-walk"', 1)[1].split('</section>', 1)[0]
        assert len(latest.route) == 8
        for unit in latest.route:
            assert f'<td>{unit}</td>' in route
        findings = reconcile.compare(f.b, f.rent, f.now)
        section = page.split('id="findings"', 1)[1].split('</section>', 1)[0]
        assert section.count('View this item on Today') == len(findings)
        for finding in findings:
            text = seen(section)
            assert plain(finding.kind) in text and plain(finding.why) in text and plain(finding.closes) in text
        text = seen(page)
        assert not re.search(r'\b(?:W|O|WO|PL|PR|EX|PI|TK|RT|L|T|C)\d{4}\b', text)
        assert not re.search(r'\b(?:NEEDS_STAFF|OUTCOME_UNKNOWN|PENDING_AT_FMS)\b', text)


def test_observation_and_copies(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client, sign_in(app) as other:
        fd, ident = desk_of(app, client)
        unit = fd.ready().books(fd.stream(ident)).units[0]
        assert step(client, 'start_walk', **{f'unit_{unit}': '1'}).status_code == 303
        floor = rent_of(fd, ident).floor
        started = max(floor.walks.values(), key=lambda w: (w.started, w.id))
        step(client, 'observe_unit', about=started.id, unit=unit, lock='present', whose='tenant', tag='absent', door='closed', text='Latch looks sound')
        assert 'Latch looks sound' in seen(client.get('/walkthrough').text)
        assert 'Latch looks sound' not in seen(other.get('/walkthrough').text)


def test_refused_order_move_is_shown_once(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        before = rent_of(fd, ident).floor
        order = next(o for o in before.orders.values() if o.state != 'closed' and o.state != 'staff checked')
        state, changes = order.state, len(order.changes)
        response = step(client, 'close_work_order', about=order.id)
        assert response.headers['location'] == '/walkthrough?at=orders#orders'
        text = seen(client.get(response.headers['location']).text)
        assert text.count('Nothing changed.') == 1
        assert f'that work order is {state}; the next change is' in text.lower()
        assert 'Nothing changed.' not in seen(client.get('/walkthrough').text)
        after = rent_of(fd, ident).floor.orders[order.id]
        assert (after.state, len(after.changes)) == (state, changes)


def test_the_same_report_sent_twice_from_one_page_makes_one_work_order(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        unit = fd.ready().books(fd.stream(ident)).units[0]
        page = client.get('/walkthrough').text
        form = {'token': token(page), 'action': 'report_work_order', 'unit': unit, 'text': 'Hinge squeaks on the roll-up door'}
        before = len(rent_of(fd, ident).floor.orders)
        for _ in range(2):                                       # a double click sends the same form twice
            assert client.post('/walkthrough', data=form, follow_redirects=False).status_code == 303
        assert len(rent_of(fd, ident).floor.orders) == before + 1
        other = {**form, 'text': 'Light out above the door'}     # another report from the same page is another order
        assert client.post('/walkthrough', data=other, follow_redirects=False).status_code == 303
        assert len(rent_of(fd, ident).floor.orders) == before + 2


def test_the_observe_form_makes_staff_choose_every_field_and_offers_belongings(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        unit = fd.ready().books(fd.stream(ident)).units[0]
        assert step(client, 'start_walk', **{f'unit_{unit}': '1'}).status_code == 303
        page = client.get('/walkthrough').text
        for name in ('lock', 'whose', 'tag', 'door', 'contents'):
            assert re.search(rf'<select name="{name}" required><option value="" selected disabled>Choose</option>', page), name
        floor = rent_of(fd, ident).floor
        started = max(floor.walks.values(), key=lambda w: (w.started, w.id))
        step(client, 'observe_unit', about=started.id, unit=unit, lock='absent', whose='unknown', tag='absent', door='closed', contents='seen', text='Boxes inside')
        assert rent_of(fd, ident).floor.walks[started.id].current(unit).contents == 'seen'


def test_each_observation_in_the_correction_list_names_its_walk_and_unit_for_the_note_reader(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        unit = fd.ready().books(fd.stream(ident)).units[0]
        assert step(client, 'start_walk', **{f'unit_{unit}': '1'}).status_code == 303
        wid = max(rent_of(fd, ident).floor.walks)
        assert step(client, 'observe_unit', about=wid, unit=unit, lock='absent', whose='unknown', tag='absent', door='closed').status_code == 303
        html = client.get('/walkthrough').text
        oid = rent_of(fd, ident).floor.walks[wid].current(unit).id
        assert f'<option value="{oid}" data-walk="{wid}" data-unit="{unit}">' in html
