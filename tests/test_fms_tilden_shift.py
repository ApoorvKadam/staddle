"""Tilden's morning shift through the pages: the switch, the seeded copy, the wake after a staff form on the Walkthrough page, and the
shared daily budget. The visitor acts as staff only through the Walkthrough forms; the model and the second check are stand-ins."""
from core import renting, walk
from core.tilden import cases
from desk.config import Settings
from tests.shift_world import Model, check, readers
from tests.test_fms import sign_in, site
from tests.test_fms_renting_pages import desk_of, token

STAFF_WORDS = 'Straightened the bent track; the door rolls up smoothly.'


def shift_app(fms_url, model=None, on=True):
    model = model or Model()
    app = site(fms_url, model, tilden_shift=on)
    fd = app.state.front_desk
    fd.shift_hooks = {'readers': readers(), 'check': check}
    return app, fd, model


def post(client, action, **fields):
    page = client.get('/walkthrough').text
    response = client.post('/walkthrough', data={'token': token(page), 'action': action, **fields}, follow_redirects=False)
    assert response.status_code == 303
    return response


def orders_on(fd, ident, unit, kind=None):
    orders = walk.load(fd.ready(), fd.stream(ident))[1].orders.values()
    return [o for o in orders if o.unit == unit and o.state != 'closed' and (kind is None or (o.kind == 'check') == (kind == 'check'))]


def finish(client, order, words=STAFF_WORDS, after=None):
    """The five ordinary forms on one work order; `after` observes each accepted step."""
    for action, fields in (('triage_work_order', {'text': 'Looked at it.'}), ('assign_work_order', {'to': 'office_manager'}),
                           ('record_order_work', {'text': words}), ('check_order_work', {'text': words}), ('close_work_order', {})):
        post(client, action, about=order, **fields)
        if after:
            after(action)


def note(client, fd, ident, unit, words):
    """A walk note through the forms: start a walk on the unit, then record what was seen."""
    post(client, 'start_walk', **{f'unit_{unit}': '1'})
    wk = max(walk.load(fd.ready(), fd.stream(ident))[1].walks.values(), key=lambda w: (w.started, w.id))
    post(client, 'observe_unit', about=wk.id, unit=unit, lock='absent', whose='unknown', tag='absent', door='closed', contents='not seen', text=words)


def seeded(fd, ident):
    events = fd.ready().events(fd.stream(ident))
    repair = next(e.payload['unit'] for e in events if e.kind == 'work_order.reported' and 'Roll-up' in e.payload['problem'])
    unclear = next(e.payload['unit'] for e in events if e.kind == 'walk.observed' and 'boxes' in e.payload['note'])
    return repair, unclear


def test_with_the_switch_off_nothing_changes_and_nothing_is_called(fms_url, sim):
    app, fd, model = shift_app(fms_url, on=False)
    with sign_in(app) as client:
        _, ident = desk_of(app, client)
        events = fd.ready().events(fd.stream(ident))
        assert not any(e.kind == 'moveout.requested' and 'Roll-up' in repr(e.payload) for e in events)
        assert not any(e.kind == 'work_order.reported' and 'Roll-up' in e.payload['problem'] for e in events)
        assert fd.run_shift(ident, None, True) is None
        assert 'href="/tilden"' not in client.get('/today').text
        assert client.get('/tilden', follow_redirects=False).status_code == 404
        post(client, 'report_work_order', unit='A101', text='A light is out above the door')
        assert not model.calls and not cases.load(fd.ready(), fd.stream(ident))


def test_a_visitor_as_staff_takes_the_repair_unit_to_a_verified_release_and_each_wake_is_the_right_one(fms_url, sim):
    app, fd, model = shift_app(fms_url)
    with sign_in(app) as client, sign_in(app) as other:
        _, ident = desk_of(app, client)
        _, other_ident = desk_of(app, other)
        repair, unclear = seeded(fd, ident)
        shifted = fd.run_shift(ident, None, True)
        assert len(shifted.admitted) == 2
        stream = fd.stream(ident)
        assert len(orders_on(fd, ident, repair)) == 1          # the two reports were linked into one order by the first step
        steps, calls = [], [len(model.calls)]

        def counted(action):
            steps.append(action)
            if action != 'close_work_order':
                assert len(model.calls) == calls[0]          # a half finished order wakes nothing
        before = len(fd.ready().stream_events(stream))
        finish(client, orders_on(fd, ident, repair)[0].id, after=counted)
        # the repair is closed: Tilden asked for the inspection, and nothing else yet
        inspection = orders_on(fd, ident, repair, 'check')
        assert len(inspection) == 1 and len(fd.ready().stream_events(stream)) > before
        note(client, fd, ident, repair, 'The door rolls up smoothly and the unit is clean and fine to rent.')
        calls[0] = len(model.calls)
        finish(client, inspection[0].id, after=counted)
        case = next(c for c in cases.load(fd.ready(), stream).values() if c.unit == repair)
        books, rent = renting.load(fd.ready(), stream)
        assert case.state == 'done' and rent.status(repair, fd.clock()) == 'vacant'
        assert any(e.kind == 'unit.unblocked' and e.payload.get('case_id') == case.id for e in fd.ready().stream_events(stream))
        assert not orders_on(fd, ident, repair)              # no second inspection was asked for
        # the other visitor's copy was never touched
        assert not cases.load(fd.ready(), fd.stream(other_ident))
        assert not [o for o in walk.load(fd.ready(), fd.stream(other_ident))[1].orders.values() if o.state == 'closed' and o.unit == repair]
        assert len(steps) == 10                              # five forms for the repair and five for the inspection


def test_a_walk_note_after_the_repair_is_a_look_too(fms_url, sim):
    app, fd, model = shift_app(fms_url)
    with sign_in(app) as client:
        _, ident = desk_of(app, client)
        repair, _ = seeded(fd, ident)
        fd.run_shift(ident, None, True)
        finish(client, orders_on(fd, ident, repair)[0].id)
        note(client, fd, ident, repair, 'The door rolls up smoothly and the unit is clean and fine to rent.')
        finish(client, orders_on(fd, ident, repair, 'check')[0].id)
        case = next(c for c in cases.load(fd.ready(), fd.stream(ident)).values() if c.unit == repair)
        assert case.state == 'done'


def test_closing_an_inspection_without_a_walk_does_not_release_the_unit(fms_url, sim):
    app, fd, model = shift_app(fms_url)
    with sign_in(app) as client:
        _, ident = desk_of(app, client)
        repair, _ = seeded(fd, ident)
        fd.run_shift(ident, None, True)
        finish(client, orders_on(fd, ident, repair)[0].id)
        finish(client, orders_on(fd, ident, repair, 'check')[0].id)
        case = next(c for c in cases.load(fd.ready(), fd.stream(ident)).values() if c.unit == repair)
        assert case.state != 'done'
        assert renting.load(fd.ready(), fd.stream(ident))[1].status(repair, fd.clock()) == 'blocked'


def test_a_negative_walk_does_not_release_the_unit(fms_url, sim):
    app, fd, model = shift_app(fms_url)
    with sign_in(app) as client:
        _, ident = desk_of(app, client)
        repair, _ = seeded(fd, ident)
        fd.run_shift(ident, None, True)
        finish(client, orders_on(fd, ident, repair)[0].id)
        note(client, fd, ident, repair, 'The door still catches on the track; not ready to rent.')
        finish(client, orders_on(fd, ident, repair, 'check')[0].id)
        case = next(c for c in cases.load(fd.ready(), fd.stream(ident)).values() if c.unit == repair)
        books, rent = renting.load(fd.ready(), fd.stream(ident))
        assert case.state != 'done' and rent.status(repair, fd.clock()) == 'blocked'


def test_the_shared_budget_sees_the_reservation_and_gets_the_unused_part_back(fms_url, sim):
    app, fd, model = shift_app(fms_url)
    with sign_in(app) as client:
        _, ident = desk_of(app, client)
        repair, _ = seeded(fd, ident)
        fd.run_shift(ident, None, True)
        rows = fd.ready().db.execute("SELECT cents, run FROM usage WHERE visitor = ? AND shift = 'shift'", (ident,)).fetchall()
        assert len(rows) == 2 and all(r[0] == 2.0 for r in rows)
        assert fd.spent_dollars(fd.clock().replace(hour=0, minute=0)) >= 0.04
        fd.run_shift(ident, None, True)                       # a repeated press reserves nothing more
        assert fd.ready().db.execute("SELECT COUNT(*) FROM usage WHERE shift = 'shift'").fetchone()[0] == 2


def test_a_used_up_day_admits_nothing_and_still_keeps_the_units_off_the_list(fms_url, sim):
    app, fd, model = shift_app(fms_url)
    with sign_in(app) as client:
        _, ident = desk_of(app, client)
        repair, unclear = seeded(fd, ident)
        shifted = fd.run_shift(ident, lambda: 0.0, True)
        assert not shifted.admitted and len(shifted.waiting) == 2 and not model.calls
        books, rent = renting.load(fd.ready(), fd.stream(ident))
        assert rent.status(repair, fd.clock()) == 'blocked' and rent.status(unclear, fd.clock()) == 'blocked'
        assert fd.ready().db.execute("SELECT COUNT(*) FROM usage WHERE shift = 'shift'").fetchone()[0] == 0


def test_two_visitors_have_their_own_reservations(fms_url, sim):
    app, fd, model = shift_app(fms_url)
    with sign_in(app) as one, sign_in(app) as two:
        _, first = desk_of(app, one)
        _, second = desk_of(app, two)
        fd.run_shift(first, None, True)
        assert fd.ready().db.execute("SELECT COUNT(*) FROM usage WHERE visitor = ? AND shift = 'shift'", (second,)).fetchone()[0] == 0
        fd.run_shift(second, None, True)
        assert fd.ready().db.execute("SELECT COUNT(*) FROM usage WHERE visitor = ? AND shift = 'shift'", (first,)).fetchone()[0] == 2
        # a visitor's own daily limit is the same for each
        assert fd.ready().db.execute("SELECT COUNT(*) FROM usage WHERE visitor = ? AND shift = 'shift'", (second,)).fetchone()[0] == 2


def test_the_settings_switch_is_off_unless_set_to_one(monkeypatch):
    monkeypatch.delenv('DESK_TILDEN_SHIFT', raising=False)
    assert Settings.from_env().tilden_shift is False
    monkeypatch.setenv('DESK_TILDEN_SHIFT', '1')
    assert Settings.from_env().tilden_shift is True
    monkeypatch.setenv('DESK_TILDEN_SHIFT', 'true')
    assert Settings.from_env().tilden_shift is False
