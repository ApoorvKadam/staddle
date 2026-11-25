"""The shift pages read this visitor's records and accept staff submissions once."""
from fastapi.testclient import TestClient
from core import walk
from core.tilden import cases
from desk.tilden_screen import screen_text, second_words, timeline
from tests.test_fms import sign_in, seen
from tests.test_fms_renting_pages import desk_of, token
from tests.test_fms_tilden_shift import shift_app, seeded, orders_on


def test_shift_pages_and_compact_repair(fms_url, sim):
    app, fd, model = shift_app(fms_url)
    with sign_in(app) as client:
        _, ident = desk_of(app, client)
        unit, _ = seeded(fd, ident)
        page = client.get('/tilden')
        assert page.status_code == 200 and 'Start the morning shift' in page.text
        start_token = token(page.text)
        assert client.post('/tilden/start', data={'token': start_token}, follow_redirects=False).status_code == 303
        fd.join_shift(ident)
        before = len(fd.ready().stream_events(fd.stream(ident)))
        client.post('/tilden/start', data={'token': start_token}, follow_redirects=False)
        assert fd.start_shift_async(ident, start_token, None, True) is True
        fd.join_shift(ident)
        assert len(fd.ready().stream_events(fd.stream(ident))) == before
        case = client.get('/tilden/case/' + unit)
        assert case.status_code == 200 and 'Get unit ' + unit in case.text
        order = orders_on(fd, ident, unit)[0]
        data = {'token': token(case.text), 'action': 'record_repair', 'order': order.id,
                'scope': 'Repair the track', 'person': 'Dana', 'work': 'Straightened the track at WO12'}
        url = '/tilden/case/' + unit + '/step'
        assert client.post(url, data=data, follow_redirects=False).status_code == 303
        before = len(fd.ready().stream_events(fd.stream(ident)))
        client.post(url, data=data, follow_redirects=False)
        assert len(fd.ready().stream_events(fd.stream(ident))) == before
        page = client.get('/tilden/case/' + unit).text
        assert 'Straightened the track' in page and 'Staff' in page and 'Tilden' in page
        assert walk.load(fd.ready(), fd.stream(ident))[1].orders[order.id].state == 'work recorded'
        case_record = next(c for c in cases.load(fd.ready(), fd.stream(ident)).values() if c.unit == unit)
        activity = timeline(fd.ready(), fd.stream(ident), case_record, walk.load(fd.ready(), fd.stream(ident))[1], False)
        assert {'Staff', 'Tilden'} <= {entry['actor'] for entry in activity}
        assert order.id not in seen(page) and 'NEEDS_STAFF' not in seen(page)
        assert 'WO12' not in seen(page)
        assert walk.load(fd.ready(), fd.stream(ident))[1].orders[order.id].changes[-1].text.endswith('WO12')
        check_data = {'token': token(page), 'action': 'check_repair', 'order': order.id,
                      'decision': 'accept', 'words': 'Checked the track'}
        assert client.post(url, data=check_data, follow_redirects=False).status_code == 303
        fd.join_shift(ident)
        inspection = orders_on(fd, ident, unit, 'check')[0]
        page = client.get('/tilden/case/' + unit).text
        inspect_data = {'token': token(page), 'action': 'inspect', 'order': inspection.id,
                        'scope': 'Inspect the unit', 'person': 'Dana', 'verdict': 'ready',
                        'observations': 'The door rolls smoothly and the unit is clean and fine to rent'}
        assert client.post(url, data=inspect_data, follow_redirects=False).status_code == 303
        fd.join_shift(ident)
        page = client.get('/tilden/case/' + unit).text
        assert 'The door rolls smoothly' in page
        accept_data = {'token': token(page), 'action': 'check_inspection', 'order': inspection.id,
                       'decision': 'accept', 'words': 'Checked the inspection record'}
        assert client.post(url, data=accept_data, follow_redirects=False).status_code == 303
        fd.join_shift(ident)
        page = seen(client.get('/tilden/case/' + unit).text)
        assert 'offered there again' in page
        store, stream = fd.ready(), fd.stream(ident)
        case_record = next(c for c in cases.load(store, stream).values() if c.unit == unit)
        entries = timeline(store, stream, case_record, walk.load(store, stream)[1], False)
        assert any(e['title'] == 'Released the unit' for e in entries)
        assert any(e['title'] == 'Read back from the online rentals page' and 'offered there again' in e['detail'] for e in entries)
    with TestClient(app) as visitor:
        assert visitor.get('/tilden', follow_redirects=False).status_code in (302, 303, 401, 403)
        assert visitor.get('/tilden/case/' + unit, follow_redirects=False).status_code in (302, 303, 401, 403)


def test_visible_wording_hides_short_ids_and_describes_only_known_check_results():
    assert all(value not in screen_text('Records WO12, O3 and W4 were read') for value in ('WO12', 'O3', 'W4'))
    assert second_words({'second_opinion': 'ready', 'p': 0.9}).startswith('Agreed:')
    assert second_words({'second_opinion': 'ready', 'p': 0.1}).startswith('Did not agree:')
    assert second_words({'second_opinion': 'ready', 'p': 0.75}).startswith('Second check recorded:')
    assert second_words({'second_opinion': 'tenant items', 'p': 0.1}).startswith('Agreed:')


def test_running_case_refresh_keeps_staff_forms_in_place(fms_url, sim, monkeypatch):
    app, fd, _ = shift_app(fms_url)
    with sign_in(app) as client:
        _, ident = desk_of(app, client)
        unit, _ = seeded(fd, ident)
        page = client.get('/tilden')
        client.post('/tilden/start', data={'token': token(page.text)}, follow_redirects=False)
        fd.join_shift(ident)
        monkeypatch.setattr(fd, 'shift_running', lambda ident: True)
        page = client.get('/tilden/case/' + unit).text
        assert 'name="work"' in page
        assert 'location.reload()' not in page
        assert 'id="case-activity"' in page
