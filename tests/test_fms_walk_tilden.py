"""Tilden's walk work stays on the signed-in copy and reads in staff's words."""
import re

from fastapi.testclient import TestClient

from core import reconcile, walk
from desk.fms import Facility
from tests.test_fms import seen, sign_in, site
from tests.test_fms_renting_pages import desk_of, rent_of
from tests.walk_client import StandIn, history_ids, note_answer, packet, report_ids, same_answer


def finding(app, client):
    fd, ident = desk_of(app, client)
    f = Facility(fd, ident)
    return next(x for x in reconcile.compare(f.b, f.rent, f.now) if x.slug == 'overlock_on_paid')


def start(app, client):
    fd, ident = desk_of(app, client)
    unit = fd.ready().books(fd.stream(ident)).units[0]
    result = walk.run(fd.ready(), fd.stream(ident), walk.Act('test-note-walk', 'start_walk', 'staff:office_manager', route=[unit]), fd.clock())
    return result.made, unit


def test_signed_out_is_refused(fms_url, sim):
    with TestClient(site(fms_url)) as client:
        assert client.post('/api/fms/walk/investigate', json={'key': 'x'}).status_code == 401
        assert client.post('/api/fms/walk/note', json={'walk': 'x', 'unit': 'x', 'text': 'x'}).status_code == 401


def test_kept_report_and_copy_isolation(fms_url, sim):
    app = site(fms_url, StandIn(lambda c: packet(history_ids(c)[:2])))
    with sign_in(app) as client, sign_in(app) as other:
        f = finding(app, client)
        response = client.post('/api/fms/walk/investigate', json={'key': f.key})
        assert response.status_code == 200
        report = response.json()
        assert report['summary'] and report['contradiction'] and report['records']
        assert report['step'] == 'Check the payment with the card processor'
        page = seen(client.get('/walkthrough').text)
        assert report['summary'] in page and report['step'] in page
        assert all(record in page for record in report['records'])
        assert report['summary'] not in seen(other.get('/walkthrough').text)
        assert not re.search(r'\b(?:W|O|WO|PL|PR|EX|PI|TK|RT|L|T|C)\d{4}\b|\b[0-9a-f]{8}-\d+\b', page)


def test_dropped_and_gone_finding(fms_url, sim):
    app = site(fms_url, StandIn(lambda c: packet(['missing-record'])))
    with sign_in(app) as client:
        f = finding(app, client)
        response = client.post('/api/fms/walk/investigate', json={'key': f.key})
        assert 'not in the unit' in response.json()['dropped']
        page = seen(client.get('/walkthrough').text)
        assert 'Not shown:' in page and f.kind in page
        gone = client.post('/api/fms/walk/investigate', json={'key': 'gone'})
        assert gone.status_code == 404 and 'not on the list any more' in gone.json()['detail']


def test_a_note_is_read_into_a_draft_staff_record_and_an_unsure_note_says_why(fms_url, sim):
    app = site(fms_url, StandIn(note_answer(unit=None, lock=('present', 'a lock'), door=('open', 'door open')),
                                note_answer(unit=None, flags=('unsure',))))
    with sign_in(app) as client:
        wid, unit = start(app, client)
        result = client.post('/api/fms/walk/note', json={'walk': wid, 'unit': unit, 'text': 'a lock and door open'}).json()
        assert not result['recorded'] and result['unit'] == unit                  # a draft: staff check the fields and record it
        assert result['fields']['lock'] == {'value': 'present', 'raw': 'present', 'words': 'a lock'}
        assert result['fields']['door']['words'] == 'door open' and result['fields']['tag']['value'] == 'not said'
        fd, ident = desk_of(app, client)
        assert rent_of(fd, ident).floor.walks[wid].history(unit) == []           # nothing recorded until staff press Record
        result = client.post('/api/fms/walk/note', json={'walk': wid, 'unit': unit, 'text': 'maybe something'}).json()
        assert not result['recorded'] and 'could not be sure' in result['reason']
        assert rent_of(fd, ident).floor.walks[wid].history(unit) == []


def test_off_is_said(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        f = finding(app, client)
        assert 'off on this copy' in client.post('/api/fms/walk/investigate', json={'key': f.key}).json()['reason']
        wid, unit = start(app, client)
        assert 'off on this copy' in client.post('/api/fms/walk/note', json={'walk': wid, 'unit': unit, 'text': 'door open'}).json()['reason']


def test_two_orders_on_one_unit_are_shown_read_by_tilden_and_joined_by_staff(fms_url, sim):
    app = site(fms_url, StandIn(lambda c: same_answer(report_ids(c)['First'] + report_ids(c)['Second'])))
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        store, stream = fd.ready(), fd.stream(ident)
        taken = {o.unit for o in rent_of(fd, ident).floor.orders.values()}
        unit = next(u for u in sorted(store.books(stream).units) if u not in taken)
        for n, text in ((1, 'Broken door spring'), (2, 'Door spring is broken')):
            assert walk.run(store, stream, walk.Act(f'page-report-{n}', 'report_work_order', 'staff:front_desk', unit=unit, text=text), fd.clock()).allowed
        (a, b), = [(x.id, y.id) for x, y in walk.pairs(rent_of(fd, ident).floor) if x.unit == unit]
        page = seen(client.get('/walkthrough').text)
        assert f'Unit {unit}: two open work orders' in page and 'Ask Tilden whether this is one fault' in page
        reading = client.post('/api/fms/walk/compare', json={'first': a, 'second': b}).json()
        assert reading['verdict'] == 'The same fault' and 'broken spring' in reading['summary']
        assert 'Tilden reads: The same fault.' in seen(client.get('/walkthrough').text)
        html = client.get('/walkthrough').text
        tok = re.search(r'name="token" value="([0-9a-f]{12})"', html).group(1)
        assert client.post('/walkthrough', data={'token': tok, 'action': 'join_work_order', 'about': b, 'into': a, 'text': 'Same spring'},
                           follow_redirects=False).status_code == 303
        page = seen(client.get('/walkthrough').text)
        assert f'Unit {unit}: two open work orders' not in page
        assert 'Closed as the same fault as the order reported' in page and 'Broken door spring' in page
        assert not re.search(r'\bWO\d{4}\b', page)


def test_a_note_naming_work_returns_it_for_staff_to_open_as_work_orders(fms_url, sim):
    app = site(fms_url, StandIn(note_answer(unit=None, work=[('cleaning', 'loose trash'), ('repair', 'a broken light')])))
    with sign_in(app) as client:
        wid, unit = start(app, client)
        result = client.post('/api/fms/walk/note', json={'walk': wid, 'unit': unit, 'text': 'loose trash and a broken light'}).json()
        assert result['work'] == [{'kind': 'Cleaning', 'words': 'loose trash'}, {'kind': 'Repair', 'words': 'a broken light'}]
        assert not result['recorded']                   # a draft: staff record the observation and open each order themselves


def test_opening_proposed_work_says_which_order_was_refused_and_stops(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        unit = fd.ready().books(fd.stream(ident)).units[0]
        ok = client.post('/api/fms/walk/orders', json={'unit': unit, 'texts': ['Cleaning: loose trash', 'Repair: a broken light'], 'token': 'a1b2c3d4e5f6'}).json()
        assert [r['allowed'] for r in ok['results']] == [True, True]
        assert not [p for p in walk.pairs(rent_of(fd, ident).floor) if p[0].unit == unit]     # opened together as separate work: not a duplicate pair
        again = client.post('/api/fms/walk/orders', json={'unit': unit, 'texts': ['Cleaning: loose trash'], 'token': 'a1b2c3d4e5f6'}).json()
        assert again['results'][0]['allowed']                           # the same text and page token: its first decision, no new order
        bad = client.post('/api/fms/walk/orders', json={'unit': unit, 'texts': ['Repair: ' + 'x' * 400, 'Cleaning: dust'], 'token': 'a1b2c3d4e5f6'}).json()
        assert len(bad['results']) == 1 and not bad['results'][0]['allowed'] and 'characters' in bad['results'][0]['check']
        problems = sorted(o.problem for o in rent_of(fd, ident).floor.orders.values() if o.unit == unit)
        assert problems.count('Cleaning: loose trash') == 1 and problems.count('Repair: a broken light') == 1 and 'Cleaning: dust' not in problems
