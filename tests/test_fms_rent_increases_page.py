import itertools
import re
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import desk
from core import price_law
from core.billing import Ledger
from core.books import at_local
from core.evidence import day, money
from tests.test_fms import seen, sign_in, site
from tests.test_fms_renting_pages import desk_of

ICONS = Path(desk.__file__).parent / 'templates' / 'icons'


def test_rent_increases_is_in_the_facility_menu_with_its_own_icon_and_needs_sign_in(fms_url, sim):
    app = site(fms_url)
    with TestClient(app) as client:
        assert client.get('/rent-increases', follow_redirects=False).headers['location'] == '/sign-in'
    with sign_in(app) as client:
        menu = client.get('/home').text.split('aria-label="Facility"', 1)[1]
        link = re.search(r'<a href="/rent-increases"[^>]*>(.*?)</a>', menu, re.S)
        assert link and 'Rent increases' in seen(link.group(1))
        icon = re.search(r'<svg.*?</svg>', link.group(1), re.S).group(0).strip()
        same = [path.name for path in ICONS.glob('*.svg') if path.read_text().strip() == icon]
        assert same == ['trending-up.svg']              # no other menu or page icon looks like it
        page = client.get('/rent-increases')
        assert page.status_code == 200 and 'Rent increases' in seen(page.text)


# the way a test reaches the copy and gives the core's own staff commands ------------------------------------------------------------------
SHA = 'e' * 64
STEPS = itertools.count(1)


def core_step(fd, ident, action, about=None, at=None, **form):
    act = price_law.Act(f'test-step-{next(STEPS)}', action, 'staff:office_manager', about, form)
    return price_law.run(fd.ready(), fd.stream(ident), act, at or fd.clock(), None)


def a_lease(fd, ident):
    books = fd.view(ident)[0]
    return min((x for x in books.leases.values() if x.ended is None), key=lambda x: x.id)


def take_terms(fd, ident, lease):
    assert core_step(fd, ident, 'record_terms', lease=lease.id, signed_on=lease.start.isoformat(), agreement_sha256=SHA,
                     notice_methods=['first_class_mail', 'email'], notice_days=30, email_notice_authorized=True,
                     last_known_address='1 Main St, Orlando, FL', last_known_email='tenant@example.com').allowed


def change_form(lease, effective, new=None, **more):
    return {'kind': 'lease_increase', 'current_cents': lease.rent_cents, 'new_cents': new or lease.rent_cents + 500, 'effective_on': effective.isoformat(),
            'lease': lease.id, 'other_recurring_cents': 0, **more}


def expected_rows(fd, ident):
    books, rent = fd.view(ident)[0], fd.rent_view(ident)
    rates = {}
    for u in rent.units.values():
        if u.size and u.rent_cents:
            rates.setdefault(u.size, set()).add(u.rent_cents)
    out = []
    for x in books.leases.values():
        if x.ended is None:
            size = rent.units[x.unit].size
            street = next(iter(rates[size])) if len(rates.get(size, ())) == 1 else None
            out.append((None if street is None else street - x.rent_cents, books.tenants[x.tenant].name, x.unit))
    return sorted(out, key=lambda r: (r[0] is None, -(r[0] or 0), r[1], r[2]))


ROW = re.compile(r'<tr>\s*<td><a href="/tenants/[^"]+">([^<]+)</a></td>\s*<td>([A-Z]\d+)</td><td>')


def listed(page):
    section = page.split('id="h-leases"', 1)[1]
    return ROW.findall(section)


def test_active_leases_are_listed_with_the_street_rate_and_the_largest_gap_first(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        want = expected_rows(fd, ident)
        assert len(want) > 25 and len({gap for gap, _, _ in want}) > 1
        page = client.get('/rent-increases').text
        text = seen(page)
        assert [(n, u) for _, n, u in want[:25]] == listed(page)
        for header in ('Tenant', 'Unit', 'Size', 'Rent a month', 'Rent last changed', 'Street rate', 'Gap'):
            assert header in text
        assert f'Leases 1 to 25 of {len(want)}' in text and 'Not changed since move-in' in text
        assert 'below' in text
        second = client.get('/rent-increases', params={'page': '2'}).text
        assert [(n, u) for _, n, u in want[25:50]] == listed(second) and 'Leases 26 to 50' in seen(second)
        far = client.get('/rent-increases', params={'page': '9999'}).text           # a page past the end is the last one
        assert [(n, u) for _, n, u in want[-(len(want) % 25 or 25):]] == listed(far)
        assert listed(client.get('/rent-increases', params={'page': 'x'}).text) == listed(page)


def test_an_increase_in_progress_shows_each_stage_in_words_until_it_is_billed(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease = a_lease(fd, ident)
        name = fd.view(ident)[0].tenants[lease.tenant].name
        take_terms(fd, ident, lease)
        effective = fd.today() + timedelta(days=31)
        form = change_form(lease, effective)
        assert core_step(fd, ident, 'check_price_change', about='RI-test', point='proposal', **form).allowed
        text = seen(client.get('/rent-increases').text)
        assert 'Increases in progress' in text and name in text and 'Proposed, waiting for approval' in text
        assert 'The price-change rules allow it.' in text
        assert core_step(fd, ident, 'approve_price_change', about='RI-test', **form).allowed
        assert 'Approved, notice not sent yet' in seen(client.get('/rent-increases').text)
        notice = dict(change='RI-test', lease=lease.id, document_sha256=SHA, recipient=name, channel='first_class_mail', sent_to='1 Main St, Orlando, FL',
                      attempted_on=fd.today().isoformat(), dispatch_accepted=True, certificate_of_mailing=True, postage_prepaid=True,
                      new_cents=form['new_cents'], effective_on=effective.isoformat())
        assert core_step(fd, ident, 'record_notice', **notice).allowed
        assert core_step(fd, ident, 'check_price_change', about='RI-test', point='notice_completion', **form).allowed
        shown = f'Notice sent, takes effect {day(effective)}'
        assert shown in seen(client.get('/rent-increases').text)
        # on the day it takes effect the bill is posted at the new rent, and the page says so
        on = at_local(effective, 10)
        invoice = f'{lease.id}:{effective:%Y-%m}'
        assert core_step(fd, ident, 'check_price_change', about='RI-test', at=on, point='activation', **form).allowed
        decision = core_step(fd, ident, 'check_price_change', about='RI-test', at=on, point='invoice', invoice=invoice, **form)
        assert decision.allowed, decision.check
        store, stream = fd.ready(), fd.stream(ident)
        with store.write():
            led = Ledger(store.books(stream))
            price_law.post_invoice(led, on, decision=decision.made)
            store.append(stream, led.pending)
        text = seen(client.get('/rent-increases').text)
        assert f'Billed, {money(form["new_cents"])} from {day(effective)}' in text
        assert 'Proposed, waiting' not in text and 'Approved, notice' not in text and 'Notice sent' not in text


# proposing from the page --------------------------------------------------------------------------------------------------------------
def token_of(client):
    return re.search(r'name="token" value="([0-9a-f]{12})"', client.get('/rent-increases').text).group(1)


def propose(client, target, rent_cents, on, **over):
    data = {'token': token_of(client), 'action': 'propose', 'lease': target.id, 'rent': f'{rent_cents / 100:.2f}', 'effective': on.isoformat(), **over}
    response = client.post('/rent-increases', data=data, follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/rent-increases'
    return seen(client.get('/rent-increases').text)


def first_listed(fd, ident):
    """The lease on the first row of the page: its form is on the page the test loads."""
    _, _, unit = expected_rows(fd, ident)[0]
    return next(x for x in fd.view(ident)[0].leases.values() if x.ended is None and x.unit == unit)


def written(fd, ident, before):
    return [e.kind for e in fd.ready().events(fd.stream(ident))[before:]]


def count(fd, ident):
    return len(fd.ready().events(fd.stream(ident)))


def decisions_of(fd, ident, lease):
    return [d for d in price_law.state(fd.view(ident)[0]).decisions if d.lease == lease.id]


def test_each_lease_row_says_what_the_rules_ask_and_has_a_form_to_propose(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        page = client.get('/rent-increases').text
        assert page.count('name="action" value="propose"') == 25 and 'name="lease"' in page
        text = seen(page)
        assert "30 days' notice" in text and 'first-class mail or email' in text
        assert 'Some price rules are modeled for demonstration only.' in text


def test_a_reasonable_increase_is_proposed_and_the_core_records_its_checks(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease = first_listed(fd, ident)
        name = fd.view(ident)[0].tenants[lease.tenant].name
        effective = fd.today() + timedelta(days=31)
        before = count(fd, ident)
        text = propose(client, lease, lease.rent_cents + 500, effective)
        assert 'Proposed, waiting for approval' in text and name in text and money(lease.rent_cents + 500) in text
        assert 'The price-change rules allow it.' in text and day(effective) in text
        assert sorted(written(fd, ident, before)) == ['gate.decided', 'price_law.decided']
        [recorded] = decisions_of(fd, ident, lease)
        assert (recorded.point, recorded.outcome) == ('proposal', 'allow') and not price_law.state(fd.view(ident)[0]).approvals
        assert 'An increase is in progress.' in text                  # that lease takes no second proposal until this one is done
        again = propose(client, lease, lease.rent_cents + 600, effective)
        assert 'already in progress' in again and len(decisions_of(fd, ident, lease)) == 1


def test_a_notice_period_that_is_too_short_stops_with_the_cores_words_and_writes_only_its_records(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease = first_listed(fd, ident)
        before = count(fd, ident)
        text = propose(client, lease, lease.rent_cents + 500, fd.today() + timedelta(days=10))
        earliest = fd.today() + timedelta(days=30)
        assert "requires 30 days' written notice" in text and f'{earliest:%b} {earliest.day}' in text
        assert 'Proposed, waiting' not in text and 'Increases in progress' not in text
        assert sorted(written(fd, ident, before)) == ['gate.decided', 'price_law.decided']
        [recorded] = decisions_of(fd, ident, lease)
        assert recorded.outcome == 'refuse' and not price_law.state(fd.view(ident)[0]).approvals


def test_a_hold_is_shown_as_held_and_the_owners_rule_and_disclosed_maximum_are_said(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease = first_listed(fd, ident)
        effective = fd.today() + timedelta(days=31)
        # the owner's rule: at most 5 percent in one step
        store, stream = fd.ready(), fd.stream(ident)
        with store.write():
            led = Ledger(store.books(stream))
            price_law.adopt_owner_rule(led, fd.clock(), version=1, sentence='Raise rents by no more than 5 percent at a time.', typed={'max_increase_percent': 5})
            store.append(stream, led.pending)
        text = propose(client, lease, lease.rent_cents * 2, effective)
        assert 'Raise rents by no more than 5 percent at a time.' in text and 'allows at most 5 percent' in text
        # a maximum the facility disclosed to this tenant
        assert core_step(fd, ident, 'record_commitment', lease=lease.id, kind='first_year', max_cents=lease.rent_cents + 300, covers_from=lease.start.isoformat(),
                         covers_to=(effective + timedelta(days=400)).isoformat(), disclosed_on=lease.start.isoformat(), evidence_sha256=SHA, first_page=True).allowed
        text = propose(client, lease, lease.rent_cents + 400, effective)
        assert 'maximum disclosed for this lease' in text and money(lease.rent_cents + 300) in text
        # staff have not looked up emergencies lately: the increase is held, not refused
        assert core_step(fd, ident, 'record_emergency_check', place='FL', checked_on='2026-11-01', valid_until='2026-11-10', source='state page', active=[]).allowed
        before = count(fd, ident)
        held = propose(client, lease, lease.rent_cents + 100, effective)
        assert 'Held for review' in held and 'expired' in held and 'Proposed, waiting' not in held
        assert sorted(written(fd, ident, before)) == ['gate.decided', 'price_law.decided']
        assert decisions_of(fd, ident, lease)[-1].outcome == 'hold'


def test_an_entry_that_is_not_an_increase_is_refused_before_the_core_and_writes_nothing(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease = first_listed(fd, ident)
        effective = fd.today() + timedelta(days=31)
        before = count(fd, ident)
        assert 'higher than the current rent' in propose(client, lease, lease.rent_cents, effective)
        assert 'higher than the current rent' in propose(client, lease, lease.rent_cents - 100, effective)
        assert 'valid rent in dollars and cents' in propose(client, lease, 0, effective, rent='abc')
        assert 'date the new rent takes effect' in propose(client, lease, lease.rent_cents + 500, effective, effective='next week')
        assert 'tenant who is renting' in propose(client, lease, lease.rent_cents + 500, effective, lease='nobody')
        assert count(fd, ident) == before
        assert client.post('/rent-increases', data={'action': 'propose'}, follow_redirects=False).status_code == 422


# approving, recording the notice, and the date it takes effect -------------------------------------------------------------------------
def step(client, action, about, **over):
    data = {'token': token_of(client), 'action': action, 'about': about, **over}
    response = client.post('/rent-increases', data=data, follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/rent-increases'
    return seen(client.get('/rent-increases').text)


def proposed(client, fd, ident, days=31):
    lease = first_listed(fd, ident)
    effective = fd.today() + timedelta(days=days)
    propose(client, lease, lease.rent_cents + 500, effective)
    change = next(d.change for d in decisions_of(fd, ident, lease) if d.point == 'proposal')
    return lease, effective, change


def test_an_approved_increase_gets_its_notice_by_the_leases_own_method_and_shows_when_it_takes_effect(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease, effective, change = proposed(client, fd, ident)
        page = client.get('/rent-increases').text
        assert 'value="approve"' in page and 'value="notice"' not in page               # nothing to send before it is approved
        before = count(fd, ident)
        text = step(client, 'approve', change)
        assert 'Approved, notice not sent yet' in text
        assert sorted(written(fd, ident, before)) == ['gate.decided', 'price_law.approved', 'price_law.decided']
        books = fd.view(ident)[0]
        assert change in price_law.state(books).approvals
        page = client.get('/rent-increases').text
        assert 'value="notice"' in page and 'value="approve"' not in page
        terms = price_law.terms_of(books, lease.id)
        assert set(re.findall(r'name="method" value="([a-z_]+)"', page)) == set(terms.notice_methods)       # the lease's own methods, no others
        before = count(fd, ident)
        text = step(client, 'notice', change, method='first_class_mail', sent_on=fd.today().isoformat(), certificate='yes', postage='yes')
        assert f'Notice sent, takes effect {day(effective)}' in text
        assert sorted(written(fd, ident, before)) == ['gate.decided'] * 3 + ['price_law.decided'] * 2 + ['price_law.notice_served']
        [notice] = price_law.state(fd.view(ident)[0]).notices[change]
        assert notice.channel == 'first_class_mail' and notice.sent_to == terms.last_known_address and notice.effective_on == effective
        assert notice.new_cents == lease.rent_cents + 500 and notice.certificate_of_mailing and notice.postage_prepaid
        assert 'value="notice"' not in client.get('/rent-increases').text


def test_a_notice_that_does_not_count_says_why_and_a_second_method_can_be_recorded(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease, effective, change = proposed(client, fd, ident)
        step(client, 'approve', change)
        text = step(client, 'notice', change, method='first_class_mail', sent_on=fd.today().isoformat())      # no certificate of mailing, no postage
        assert 'Notice sent, but it does not count yet' in text and 'certificate of mailing' in text and 'prepaid postage' in text
        assert 'value="notice"' in client.get('/rent-increases').text                   # another notice can be recorded
        text = step(client, 'notice', change, method='email', sent_on=fd.today().isoformat(), accepted='yes')
        assert f'Notice sent, takes effect {day(effective)}' in text
        assert [n.channel for n in price_law.state(fd.view(ident)[0]).notices[change]] == ['first_class_mail', 'email']


def test_a_hand_delivered_notice_needs_its_delivery_day(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        assert core_step(fd, ident, 'record_terms', lease=first_listed(fd, ident).id, signed_on='2026-03-02', agreement_sha256=SHA, notice_methods=['hand'],
                         notice_days=30).allowed
        lease, effective, change = proposed(client, fd, ident)
        step(client, 'approve', change)
        before = count(fd, ident)
        assert 'Enter the day it was delivered' in step(client, 'notice', change, method='hand', sent_on=fd.today().isoformat())
        assert count(fd, ident) == before
        text = step(client, 'notice', change, method='hand', sent_on=fd.today().isoformat(), delivered_on=fd.today().isoformat())
        assert f'Notice sent, takes effect {day(effective)}' in text


def test_the_steps_are_taken_in_order_and_a_hold_is_not_cleared_by_approving(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease, effective, change = proposed(client, fd, ident)
        before = count(fd, ident)
        assert 'Approve the increase before recording its notice' in step(client, 'notice', change, method='email', sent_on=fd.today().isoformat(), accepted='yes')
        assert 'not waiting for approval' in step(client, 'approve', 'RI-000000000000')
        assert count(fd, ident) == before
        # staff have not looked up emergencies lately: approving is held and nothing is approved
        assert core_step(fd, ident, 'record_emergency_check', place='FL', checked_on='2026-11-01', valid_until='2026-11-10', source='state page', active=[]).allowed
        before = count(fd, ident)
        text = step(client, 'approve', change)
        assert 'Held for review' in text and 'Proposed, waiting for approval' in text
        assert sorted(written(fd, ident, before)) == ['gate.decided', 'price_law.decided'] and not price_law.state(fd.view(ident)[0]).approvals
        # staff look again; now it can be approved, and a second approval is turned back
        assert core_step(fd, ident, 'record_emergency_check', place='FL', checked_on=fd.today().isoformat(), valid_until=(fd.today() + timedelta(days=300)).isoformat(),
                         source='state page', active=[]).allowed
        assert 'Approved, notice not sent yet' in step(client, 'approve', change)
        before = count(fd, ident)
        assert 'not waiting for approval' in step(client, 'approve', change)
        assert count(fd, ident) == before


def test_a_posted_bill_ends_the_steps_and_the_page_shows_it_billed(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease, effective, change = proposed(client, fd, ident)
        step(client, 'approve', change)
        step(client, 'notice', change, method='email', sent_on=fd.today().isoformat(), accepted='yes')
        unchanged = seen(client.get('/rent-increases').text).count('Not changed since move-in')
        snap = next(d.snapshot for d in decisions_of(fd, ident, lease) if d.point == 'proposal')
        form = {k: snap[k] for k in ('kind', 'current_cents', 'new_cents', 'effective_on', 'lease', 'other_recurring_cents', 'last_increase_on')}
        on = at_local(effective, 10)
        assert core_step(fd, ident, 'check_price_change', about=change, at=on, point='activation', **form).allowed
        decision = core_step(fd, ident, 'check_price_change', about=change, at=on, point='invoice', invoice=f'{lease.id}:{effective:%Y-%m}', **form)
        assert decision.allowed, decision.check
        store, stream = fd.ready(), fd.stream(ident)
        with store.write():
            led = Ledger(store.books(stream))
            price_law.post_invoice(led, on, decision=decision.made)
            store.append(stream, led.pending)
        page = client.get('/rent-increases').text
        assert f'Billed, {money(lease.rent_cents + 500)} from {day(effective)}' in seen(page)
        assert 'value="approve"' not in page and 'value="notice"' not in page
        assert seen(page).count('Not changed since move-in') == unchanged - 1               # the lease's rent now shows when it last changed


# the tenant's own page ---------------------------------------------------------------------------------------------------------------
def test_the_tenant_page_shows_a_scheduled_increase_and_only_for_that_tenant(fms_url, sim):
    from tests.test_fms import record_of
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease, effective, change = proposed(client, fd, ident)
        books = fd.view(ident)[0]
        name = books.tenants[lease.tenant].name
        other = next(t.name for t in books.tenants.values() if t.id != lease.tenant and t.name != name)
        page = client.get(record_of(client, name)).text
        text = seen(page)
        assert 'id="h-increase"' in page and 'Proposed, waiting for approval' in text and money(lease.rent_cents + 500) in text and day(effective) in text
        step(client, 'approve', change)
        step(client, 'notice', change, method='email', sent_on=fd.today().isoformat(), accepted='yes')
        text = seen(client.get(record_of(client, name)).text)
        assert f'Notice sent, takes effect {day(effective)}' in text
        assert 'id="h-increase"' not in client.get(record_of(client, other)).text


# the whole path on the live copy, and what the page must never show -------------------------------------------------------------------
IDS = re.compile(r'\b(?:RI-[0-9a-f]{6,}|L\d{4,}|P[A-Z]{2}\d{3,}|rent-increase:\S+|price_law\S*|gate:\S+|test-step\S*)')
SHOUTED = re.compile(r'\b[A-Z]{3,}(?:_[A-Z]{2,})+\b')
PROVIDER = re.compile(r'\b(?:payment|listing|lease|facility)_[A-Za-z0-9]{4,}\b')
VENDOR = re.compile(r'(?i)\b(?:claude|anthropic|openai|gpt|codex|gemini|haiku|sonnet|opus|fable|kepler|grok)\b')
STAMP = re.compile(r'\d{4}-\d{2}-\d{2}')


def clean(page, where):
    from tests.test_fms_shop_pages import CODES
    text = seen(page)
    assert not CODES.search(text) and not IDS.search(text) and not SHOUTED.search(text) and not PROVIDER.search(text), where
    assert not re.search(r'\b(?:SR\d{4,}|SRS\d{4,}|PLD\d{4,}|CA|UT|NYC)\b', text), where           # a place is named, never its code (the city line reads "Orlando, FL" as everywhere)
    assert not STAMP.search(text) and not re.search(r'\d{2}:\d{2}:\d{2}', text), where                # dates read as on the other pages
    assert not VENDOR.search(text) and 'legally compliant' not in text.lower(), where
    body = page.split('<main', 1)[1]
    assert not re.search(r'<(?:em|i)[ >]|font-style|style=', body), where                         # no italic line, no colour of its own
    return text


def test_a_demo_increase_goes_all_the_way_through_on_the_live_copy_and_the_page_stays_plain(fms_url, sim):
    from tests.test_fms import record_of
    app = site(fms_url)
    with TestClient(app) as stranger:
        assert stranger.post('/rent-increases', data={'action': 'propose'}, follow_redirects=False).headers['location'] == '/sign-in'
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease = first_listed(fd, ident)
        name = fd.view(ident)[0].tenants[lease.tenant].name
        text = clean(client.get('/rent-increases').text, 'list')
        assert 'Some price rules are modeled for demonstration only.' in text and 'Held for review' not in text
        # a stop first, in words
        stopped = propose(client, lease, lease.rent_cents + 500, fd.today() + timedelta(days=5))
        assert "30 days' written notice" in stopped
        clean(client.get('/rent-increases').text, 'stopped')
        # then a reasonable one, through the page's own forms
        effective = fd.today() + timedelta(days=31)
        propose(client, lease, lease.rent_cents + 500, effective)
        change = next(d.change for d in decisions_of(fd, ident, lease) if d.point == 'proposal' and d.outcome == 'allow')
        clean(client.get('/rent-increases').text, 'proposed')
        step(client, 'approve', change)
        clean(client.get('/rent-increases').text, 'approved')
        text = step(client, 'notice', change, method='first_class_mail', sent_on=fd.today().isoformat(), certificate='yes', postage='yes')
        assert f'Notice sent, takes effect {day(effective)}' in text and 'Held for review' not in text
        clean(client.get('/rent-increases').text, 'noticed')
        clean(client.get(record_of(client, name)).text, 'tenant page')
        decisions = [d.outcome for d in price_law.state(fd.view(ident)[0]).decisions if d.change == change]
        assert decisions and set(decisions) == {'allow'}, decisions


# the bill: the page says billed only from the posted bill, and the lease's rent follows ----------------------------------------------------
def rent_run(fd, ident, month):
    """The monthly rent run on this copy, the core's own: what it posted, as charge ids."""
    store, stream = fd.ready(), fd.stream(ident)
    with store.write():
        led = Ledger(store.books(stream))
        made = led.post_rent(month)
        store.append(stream, led.pending)
    return made


def first_on_or_after(d):
    first = d.replace(day=1)
    if first >= d:
        return first
    return first.replace(year=first.year + 1, month=1) if first.month == 12 else first.replace(month=first.month + 1)


def shown(d, today):
    return day(d) if d.year == today.year else f'{day(d)}, {d.year}'


def rent_cell(client, unit):
    """What the lease list says this unit's lease pays a month, read from whichever page it is on."""
    for number in range(1, 8):
        page = client.get('/rent-increases', params={'page': str(number)}).text
        found = re.search(rf'<td>{unit}</td><td>[^<]*</td><td class="num">([^<]+)</td>', page.split('id="h-leases"', 1)[1])
        if found:
            return found.group(1)
    raise AssertionError(unit)


def noticed_increase(client, fd, ident, **over):
    lease, effective, change = proposed(client, fd, ident, **over)
    step(client, 'approve', change)
    step(client, 'notice', change, method='email', sent_on=fd.today().isoformat(), accepted='yes')
    return lease, effective, change


def test_billed_shows_only_from_the_posted_bill_and_the_lease_rent_follows_it(fms_url, sim):
    from tests.test_fms import record_of
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease, effective, change = noticed_increase(client, fd, ident)
        new = lease.rent_cents + 500
        text = seen(client.get('/rent-increases').text)
        assert f'Notice sent, takes effect {day(effective)}' in text and 'Billed,' not in text and 'Rent before' in text
        assert rent_cell(client, lease.unit) == money(lease.rent_cents)
        first = first_on_or_after(effective)
        rent_run(fd, ident, f'{first:%Y-%m}')
        text = seen(client.get('/rent-increases').text)
        assert f'Billed, {money(new)} from {shown(first, fd.today())}' in text and 'Notice sent' not in text
        assert rent_cell(client, lease.unit) == money(new)                           # the lease's rent a month is the new amount
        name = fd.view(ident)[0].tenants[lease.tenant].name
        tenant = seen(client.get(record_of(client, name)).text)
        assert f'Billed, {money(new)} from {shown(first, fd.today())}' in tenant and money(new) in tenant


def test_an_increase_whose_day_has_come_says_it_waits_for_the_monthly_bill(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease = first_listed(fd, ident)
        assert core_step(fd, ident, 'record_terms', lease=lease.id, signed_on=lease.start.isoformat(), agreement_sha256=SHA, notice_methods=['email'],
                         notice_days=0, email_notice_authorized=True, last_known_email='tenant@example.com').allowed
        propose(client, lease, lease.rent_cents + 500, fd.today())
        change = next(d.change for d in decisions_of(fd, ident, lease) if d.point == 'proposal')
        step(client, 'approve', change)
        text = step(client, 'notice', change, method='email', sent_on=fd.today().isoformat(), accepted='yes')
        assert f'Notice sent, takes effect {day(fd.today())}, waiting for the monthly bill' in text and 'Billed,' not in text
        assert rent_cell(client, lease.unit) == money(lease.rent_cents)               # nothing is billed, so the rent has not changed


def test_an_increase_the_rent_run_could_not_post_says_it_is_held_and_the_old_rent_stands(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease, effective, change = noticed_increase(client, fd, ident)
        assert core_step(fd, ident, 'record_emergency_check', place='FL', checked_on='2026-11-01', valid_until='2026-11-10', source='state page', active=[]).allowed
        first = first_on_or_after(effective)
        rent_run(fd, ident, f'{first:%Y-%m}')
        text = seen(client.get('/rent-increases').text)
        assert 'Held at the monthly bill, the old rent is still billed' in text and 'Held for review' in text and 'Billed,' not in text
        assert rent_cell(client, lease.unit) == money(lease.rent_cents)


# the whole road to the tenant's bill ---------------------------------------------------------------------------------------------------------
def bill_of(fd, ident, lease, month):
    books = fd.view(ident)[0]
    return [c for c in books.charges.values() if c.lease == lease.id and c.obligation == f'rent:{lease.id}:{month}']


def before(first):
    return first.replace(year=first.year - 1, month=12) if first.month == 1 else first.replace(month=first.month - 1)


def test_a_northstar_increase_goes_from_proposal_to_the_next_months_bill_at_the_new_rent(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease = first_listed(fd, ident)
        neighbour = next(x for x in fd.view(ident)[0].leases.values() if x.ended is None and x.id != lease.id)
        old, new = lease.rent_cents, lease.rent_cents + 500
        # through the page: proposal, approval, notice
        effective = fd.today() + timedelta(days=31)
        propose(client, lease, new, effective)
        change = next(d.change for d in decisions_of(fd, ident, lease) if d.point == 'proposal')
        assert 'Proposed, waiting for approval' in seen(client.get('/rent-increases').text)
        assert 'Approved, notice not sent yet' in step(client, 'approve', change)
        text = step(client, 'notice', change, method='first_class_mail', sent_on=fd.today().isoformat(), certificate='yes', postage='yes')
        assert f'Notice sent, takes effect {day(effective)}' in text
        # the rent run for each month: before the effective day the old rent, from it the new one
        first = first_on_or_after(effective)
        if first > effective:
            month = f'{before(first):%Y-%m}'
            assert rent_run(fd, ident, month)
            [early] = bill_of(fd, ident, lease, month)
            assert early.cents == old and fd.view(ident)[0].leases[lease.id].rent_cents == old
            assert not price_law.state(fd.view(ident)[0]).postings
            assert 'Billed,' not in seen(client.get('/rent-increases').text)
        month = f'{first:%Y-%m}'
        assert rent_run(fd, ident, month)
        [bill] = bill_of(fd, ident, lease, month)
        assert bill.cents == new and bill.due == first and fd.view(ident)[0].leases[lease.id].rent_cents == new
        [other] = bill_of(fd, ident, neighbour, month)
        assert other.cents == neighbour.rent_cents                       # nobody else's bill moved
        assert f'Billed, {money(new)} from {shown(first, fd.today())}' in seen(client.get('/rent-increases').text)
        assert rent_cell(client, lease.unit) == money(new)
        assert rent_run(fd, ident, month) == []                          # the same month again posts nothing more
        assert len(bill_of(fd, ident, lease, month)) == 1
        # the month after: the lease's own rent, now the new one
        after = first.replace(year=first.year + 1, month=1) if first.month == 12 else first.replace(month=first.month + 1)
        assert rent_run(fd, ident, f'{after:%Y-%m}')
        [next_bill] = bill_of(fd, ident, lease, f'{after:%Y-%m}')
        assert next_bill.cents == new and len(price_law.state(fd.view(ident)[0]).postings) == 1


@pytest.mark.parametrize('reached', ['proposed', 'approved', 'notice not counted', 'held'])
def test_an_increase_that_is_held_or_not_through_never_changes_the_bill(fms_url, sim, reached):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease, effective, change = proposed(client, fd, ident)
        if reached != 'proposed':
            step(client, 'approve', change)
        if reached == 'notice not counted':
            step(client, 'notice', change, method='first_class_mail', sent_on=fd.today().isoformat())       # no certificate, no postage
        if reached == 'held':
            step(client, 'notice', change, method='email', sent_on=fd.today().isoformat(), accepted='yes')
            assert core_step(fd, ident, 'record_emergency_check', place='FL', checked_on='2026-11-01', valid_until='2026-11-10', source='state page', active=[]).allowed
        first = first_on_or_after(effective)
        for month in (first, first.replace(year=first.year + 1, month=1) if first.month == 12 else first.replace(month=first.month + 1)):
            assert rent_run(fd, ident, f'{month:%Y-%m}')
            [bill] = bill_of(fd, ident, lease, f'{month:%Y-%m}')
            assert bill.cents == lease.rent_cents and fd.view(ident)[0].leases[lease.id].rent_cents == lease.rent_cents
        assert not price_law.state(fd.view(ident)[0]).postings
        text = seen(client.get('/rent-increases').text)
        assert 'Billed,' not in text and rent_cell(client, lease.unit) == money(lease.rent_cents)
        if reached == 'held':
            assert 'Held at the monthly bill' in text
            # staff look again: the next run posts it, over a month not yet billed
            assert core_step(fd, ident, 'record_emergency_check', place='FL', checked_on=fd.today().isoformat(), valid_until=(fd.today() + timedelta(days=300)).isoformat(),
                             source='state page', active=[]).allowed
            later = first.replace(year=first.year + 1, month=first.month + 2 - 12) if first.month > 10 else first.replace(month=first.month + 2)
            assert rent_run(fd, ident, f'{later:%Y-%m}')
            [bill] = bill_of(fd, ident, lease, f'{later:%Y-%m}')
            assert bill.cents == lease.rent_cents + 500 and fd.view(ident)[0].leases[lease.id].rent_cents == lease.rent_cents + 500


# every form on a page is its own operation ---------------------------------------------------------------------------------------------------
def test_forms_from_one_page_load_each_complete_on_their_own_and_an_identical_resend_does_nothing(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        books = fd.view(ident)[0]
        units = [unit for _, _, unit in expected_rows(fd, ident)[:2]]
        one, two = (next(x for x in books.leases.values() if x.ended is None and x.unit == unit) for unit in units)
        effective = (fd.today() + timedelta(days=31)).isoformat()

        def send(data):
            assert client.post('/rent-increases', data=data, follow_redirects=False).status_code == 303

        token = token_of(client)                    # one page load, several forms: the token is the page's, never an operation's
        proposal = {one.id: dict(token=token, action='propose', lease=one.id, rent=f'{(one.rent_cents + 500) / 100:.2f}', effective=effective),
                    two.id: dict(token=token, action='propose', lease=two.id, rent=f'{(two.rent_cents + 700) / 100:.2f}', effective=effective)}
        send(proposal[one.id])
        send(proposal[two.id])
        changes = {x.id: [d.change for d in decisions_of(fd, ident, x) if d.point == 'proposal' and d.outcome == 'allow'] for x in (one, two)}
        assert all(len(found) == 1 for found in changes.values()) and changes[one.id] != changes[two.id]
        text = seen(client.get('/rent-increases').text)
        assert text.count('Proposed, waiting for approval') == 2 and money(one.rent_cents + 500) in text and money(two.rent_cents + 700) in text
        marker = count(fd, ident)
        send(proposal[one.id])                      # the same form sent again
        send(proposal[two.id])
        assert count(fd, ident) == marker
        # approvals and notices from one page load, each its own operation
        token = token_of(client)
        for x in (one, two):
            send(dict(token=token, action='approve', about=changes[x.id][0]))
        assert set(price_law.state(fd.view(ident)[0]).approvals) == {changes[one.id][0], changes[two.id][0]}
        marker = count(fd, ident)
        send(dict(token=token, action='approve', about=changes[one.id][0]))
        assert count(fd, ident) == marker
        token = token_of(client)
        for x in (one, two):
            send(dict(token=token, action='notice', about=changes[x.id][0], method='email', sent_on=fd.today().isoformat(), accepted='yes'))
        assert set(price_law.state(fd.view(ident)[0]).notices) == {changes[one.id][0], changes[two.id][0]}
        marker = count(fd, ident)
        send(dict(token=token, action='notice', about=changes[two.id][0], method='email', sent_on=fd.today().isoformat(), accepted='yes'))
        assert count(fd, ident) == marker
        assert seen(client.get('/rent-increases').text).count('Notice sent, takes effect') == 2


# when an increase cannot go on as it is -------------------------------------------------------------------------------------------------------
def month_after(first):
    return first.replace(year=first.year + 1, month=1) if first.month == 12 else first.replace(month=first.month + 1)


def retake_terms(fd, ident, lease):
    """The agreement recorded again with the same notice terms: an approval given before it rests on the older record."""
    terms = price_law.terms_of(fd.view(ident)[0], lease.id)
    assert core_step(fd, ident, 'record_terms', lease=lease.id, signed_on=terms.signed_on.isoformat(), agreement_sha256='f' * 64,
                     notice_methods=list(terms.notice_methods), notice_days=terms.notice_days, email_notice_authorized=terms.email_notice_authorized,
                     last_known_address=terms.last_known_address, last_known_email=terms.last_known_email, designated_method=terms.designated_method).allowed


def test_a_held_increase_can_be_checked_and_approved_again_and_then_bills(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease, effective, change = noticed_increase(client, fd, ident)
        retake_terms(fd, ident, lease)
        first = first_on_or_after(effective)
        rent_run(fd, ident, f'{first:%Y-%m}')
        page = client.get('/rent-increases').text
        assert 'Held at the monthly bill, the old rent is still billed' in seen(page) and 'approved again' in seen(page)
        assert 'Check and approve again' in page and 'value="approve"' in page
        text = step(client, 'approve', change)
        assert 'Held at the monthly bill' not in text and 'Notice sent, takes effect' in text
        assert 'value="approve"' not in client.get('/rent-increases').text
        later = month_after(first)
        assert rent_run(fd, ident, f'{later:%Y-%m}')
        [bill] = bill_of(fd, ident, lease, f'{later:%Y-%m}')
        assert bill.cents == lease.rent_cents + 500 and fd.view(ident)[0].leases[lease.id].rent_cents == lease.rent_cents + 500


def test_an_increase_can_be_withdrawn_before_it_is_billed_and_another_proposed(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease, effective, change = noticed_increase(client, fd, ident)
        assert 'value="withdraw"' in client.get('/rent-increases').text
        before = count(fd, ident)
        text = step(client, 'withdraw', change)
        assert sorted(written(fd, ident, before)) == ['gate.decided', 'price_law.withdrawn']
        assert 'Increases in progress' not in text and 'An increase is in progress' not in text
        first = first_on_or_after(effective)
        assert rent_run(fd, ident, f'{first:%Y-%m}')
        [bill] = bill_of(fd, ident, lease, f'{first:%Y-%m}')
        assert bill.cents == lease.rent_cents and not price_law.state(fd.view(ident)[0]).postings
        # the lease is free for a new proposal
        assert 'Proposed, waiting for approval' in propose(client, lease, lease.rent_cents + 300, fd.today() + timedelta(days=45))
        again = next(d.change for d in decisions_of(fd, ident, lease) if d.point == 'proposal' and d.change != change)
        before = count(fd, ident)
        assert 'not in progress' in step(client, 'withdraw', change)           # the withdrawn one is gone; the new one is untouched
        assert count(fd, ident) == before and again not in price_law.state(fd.view(ident)[0]).withdrawn


def test_a_billed_increase_cannot_be_withdrawn(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease, effective, change = noticed_increase(client, fd, ident)
        rent_run(fd, ident, f'{first_on_or_after(effective):%Y-%m}')
        assert 'value="withdraw"' not in client.get('/rent-increases').text
        before = count(fd, ident)
        assert 'not in progress' in step(client, 'withdraw', change)
        assert count(fd, ident) == before


def approved_earlier(fd, ident, days_ago=10, days_ahead=21, proposal=True):
    """An increase proposed and approved some days ago, taking effect a full notice period after that day."""
    lease = first_listed(fd, ident)
    then, effective = fd.clock() - timedelta(days=days_ago), fd.today() + timedelta(days=days_ahead)
    change = f'RI-earlier-{lease.id}'
    if proposal:
        assert core_step(fd, ident, 'check_price_change', change, at=then, point='proposal', **change_form(lease, effective)).allowed
    assert core_step(fd, ident, 'approve_price_change', change, at=then, **change_form(lease, effective)).allowed
    return lease, effective, change


def test_a_notice_sent_on_an_earlier_day_is_judged_from_that_day(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease, effective, change = approved_earlier(fd, ident)
        sent = fd.today() - timedelta(days=10)          # 31 days before the effective day, recorded only today
        text = step(client, 'notice', change, method='email', sent_on=sent.isoformat(), accepted='yes')
        assert f'Notice sent, takes effect {day(effective)}' in text
        [notice] = price_law.state(fd.view(ident)[0]).notices[change]
        assert notice.attempted_on == sent


def test_a_notice_given_today_must_still_leave_the_full_period(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease, effective, change = approved_earlier(fd, ident)
        text = step(client, 'notice', change, method='email', sent_on=fd.today().isoformat(), accepted='yes')
        assert 'If notice is given today the earliest' in text and 'Approved, notice not sent yet' in text
        assert change not in price_law.state(fd.view(ident)[0]).notices


def test_an_approval_that_went_stale_before_the_notice_can_be_approved_again(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease, effective, change = proposed(client, fd, ident)
        step(client, 'approve', change)
        retake_terms(fd, ident, lease)
        text = step(client, 'notice', change, method='email', sent_on=fd.today().isoformat(), accepted='yes')
        assert 'approved again' in text and change not in price_law.state(fd.view(ident)[0]).notices
        page = client.get('/rent-increases').text
        assert 'Check and approve again' in page and 'value="notice"' not in page
        assert 'Approved, notice not sent yet' in step(client, 'approve', change)
        page = client.get('/rent-increases').text
        assert 'value="notice"' in page and 'Check and approve again' not in page
        text = step(client, 'notice', change, method='email', sent_on=fd.today().isoformat(), accepted='yes')
        assert f'Notice sent, takes effect {day(effective)}' in text


def test_an_earlier_notice_recorded_under_a_stale_approval_counts_once_it_is_approved_again(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease, effective, change = approved_earlier(fd, ident)
        retake_terms(fd, ident, lease)
        sent = fd.today() - timedelta(days=10)
        text = step(client, 'notice', change, method='email', sent_on=sent.isoformat(), accepted='yes')
        assert 'Notice sent, but it does not count yet' in text and 'approved again' in text
        page = client.get('/rent-increases').text
        assert 'Check and approve again' in page
        text = step(client, 'approve', change)                       # the notice already on file is checked again with the renewed approval
        assert f'Notice sent, takes effect {day(effective)}' in text and 'does not count yet' not in text


def changed_cell(client, unit):
    """What the lease list says about when this unit's rent last changed."""
    for number in range(1, 8):
        page = client.get('/rent-increases', params={'page': str(number)}).text
        found = re.search(rf'<td>{unit}</td><td>[^<]*</td><td class="num">[^<]+</td><td>([^<]+)</td>', page.split('id="h-leases"', 1)[1])
        if found:
            return found.group(1)
    raise AssertionError(unit)


def test_an_increase_billed_without_a_proposal_still_dates_the_leases_last_change(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = desk_of(app, client)
        lease, effective, change = approved_earlier(fd, ident, proposal=False)       # approved straight through the core, never proposed on the page
        sent = fd.today() - timedelta(days=10)
        terms = price_law.terms_of(fd.view(ident)[0], lease.id)
        assert core_step(fd, ident, 'record_notice', change=change, lease=lease.id, document_sha256=SHA, recipient='Tenant', channel='email',
                         sent_to=terms.last_known_email, attempted_on=sent.isoformat(), dispatch_accepted=True, new_cents=lease.rent_cents + 500,
                         effective_on=effective.isoformat()).allowed
        assert changed_cell(client, lease.unit) == 'Not changed since move-in'
        first = first_on_or_after(effective)
        rent_run(fd, ident, f'{first:%Y-%m}')
        [bill] = bill_of(fd, ident, lease, f'{first:%Y-%m}')
        assert bill.cents == lease.rent_cents + 500
        assert changed_cell(client, lease.unit) == shown(first, fd.today())

