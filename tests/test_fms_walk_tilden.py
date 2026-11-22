"""Tilden's walk work stays on the signed-in copy and reads in staff's words."""
import re

from fastapi.testclient import TestClient

from core import reconcile, walk
from desk.fms import Facility
from tests.test_fms import seen, sign_in, site
from tests.test_fms_renting_pages import desk_of, rent_of
from tests.walk_client import StandIn, history_ids, note_answer, packet


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
