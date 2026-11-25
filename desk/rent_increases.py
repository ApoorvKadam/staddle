"""Staff-facing rent increases for existing tenants, through the core's price-change gate (core/price_law.py).

The page reads the price-law records the core keeps (decisions, approvals, notices, posted bills) and never decides anything itself: a
proposal, an approval and a notice are staff commands through price_law.run, and the words a stop or hold gives are the core's own."""
import hashlib
import re
import secrets
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from core import price_law
from core.evidence import day, money

from .pricing import page_words

PAGE_SIZE = 25
GIVEN = ('hand', 'first_class_mail', 'email')            # the ways of giving notice a page can record; a text or a dashboard banner never counts
STAGE_TONE = {'proposed': 'plain', 'approved': 'wait', 'noticed': 'wait', 'unproven': 'act', 'held': 'act', 'billed': 'done'}


def dated(d: date, year: int) -> str:
    """A day as the other FMS pages show it, with its year when it is not this year."""
    return day(d) if d.year == year else f'{day(d)}, {d.year}'


def said(decision) -> str:
    """What one recorded check said, in the core's words: a stop or hold gives its reasons, an allow gives its notes."""
    blocking = [r.words for r in decision.reasons if r.kind != 'note']
    notes = [r.words for r in decision.reasons if r.kind == 'note']
    lead = ('Held for review: ' if decision.outcome == 'hold' else '') + ' '.join(blocking) if blocking else 'The price-change rules allow it.'
    return page_words(' '.join([lead, *notes]))


def streets(f) -> dict:
    """Each size's asking rate (what a new renter pays): the one rate its units share, None where they differ."""
    rates: dict[str, set] = {}
    for unit in f.rent.units.values():
        if unit.size and unit.rent_cents:
            rates.setdefault(unit.size, set()).add(unit.rent_cents)
    return {size: next(iter(cents)) if len(cents) == 1 else None for size, cents in rates.items()}


def stages(f) -> list[dict]:
    """Every rent increase the core has on record for an active lease, with where it stands: proposed, approved, notice sent (and
    whether the notice counts), or billed. A change whose proposal was stopped or held, or that staff withdrew, is not in progress."""
    st = price_law.state(f.b)
    by_id = {d.id: d for d in st.decisions}
    changes: dict[str, list] = {}
    for d in st.decisions:
        if d.snapshot.get('kind') == 'lease_increase' and d.lease in f.b.leases and f.b.leases[d.lease].ended is None:
            changes.setdefault(d.change, []).append(d)
    billed = {}
    for invoice, (decision, charge) in st.postings.items():
        if decision in by_id:
            billed[by_id[decision].change] = (invoice, charge)
    result = []
    for change, decisions in changes.items():
        proposals = [d for d in decisions if d.point == 'proposal' and d.outcome == 'allow']
        if not proposals or change in st.withdrawn:
            continue
        snap = proposals[-1].snapshot
        lease = f.b.leases[snap['lease']]
        person = f.people[lease.tenant]
        effective = date.fromisoformat(snap['effective_on'])
        notices = st.notices.get(change, [])
        billed_on = None
        counted = [d for d in decisions if d.point == 'notice_completion']
        last = decisions[-1]
        tries = [i for i, d in enumerate(decisions) if d.point == 'invoice']
        # the rent run checked it on its day and could not post it, and no approval since then has passed
        stuck = bool(tries) and decisions[tries[-1]].outcome != 'allow' and not any(
            d.point == 'approval' and d.outcome == 'allow' for d in decisions[tries[-1]:])
        if change in billed:
            charge = f.b.charges[billed[change][1]]          # said from the posted bill itself: its amount and the day it is due
            stage, words, billed_on = 'billed', f'Billed, {money(charge.cents)} from {dated(charge.due, f.today.year)}', charge.due
        elif notices:
            if counted and counted[-1].outcome == 'allow':
                if stuck:
                    stage, words = 'held', 'Held at the monthly bill, the old rent is still billed'
                elif effective <= f.today:
                    stage, words = 'noticed', f'Notice sent, takes effect {dated(effective, f.today.year)}, waiting for the monthly bill'
                else:
                    stage, words = 'noticed', f'Notice sent, takes effect {dated(effective, f.today.year)}'
            else:
                stage, words = 'unproven', 'Notice sent, but it does not count yet'
        elif change in st.approvals:
            stage, words = 'approved', 'Approved, notice not sent yet'
        else:
            stage, words = 'proposed', 'Proposed, waiting for approval'
        terms = price_law.terms_of(f.b, lease.id)
        ways_open = [(m, price_law.CHANNEL_WORDS[m]) for m in (terms.notice_methods if terms else ()) if m in GIVEN] if stage in ('approved', 'unproven') else []
        result.append({'id': change, 'lease': lease.id, 'name': person['name'], 'slug': person['slug'], 'unit': lease.unit,
                       'current': snap['current_cents'], 'new': snap['new_cents'], 'effective': effective, 'stage': stage, 'words': words,
                       'tone': STAGE_TONE[stage], 'checks': said(last), 'snap': snap, 'ways': ways_open, 'billed_on': billed_on,
                       # the approval rests on records that have changed since: it is checked and approved again, at any stage before the bill
                       'again': stage == 'held' or (stage != 'billed' and any(r.code == 'LEGAL_RULE_VERSION_STALE' for r in last.reasons)),
                       'noticed': bool(notices)})
    order = ('proposed', 'approved', 'unproven', 'held', 'noticed', 'billed')
    return sorted(result, key=lambda r: (order.index(r['stage']), r['effective'], r['name']))


def last_changes(f) -> dict:
    """Each lease's latest billed increase: the day its bill was due, since the rent changed with the bill and not on the day the notice
    named. Read from the posted bills themselves, so an increase that was billed without a proposal on this page still counts."""
    st = price_law.state(f.b)
    lease_of = {d.id: d.lease for d in st.decisions}
    out = {}
    for decision, charge in st.postings.values():
        lease = lease_of.get(decision)
        if lease and charge in f.b.charges and f.b.charges[charge].due >= out.get(lease, date.min):
            out[lease] = f.b.charges[charge].due
    return out


def leases(f, open_leases: set) -> list[dict]:
    """The active leases, the largest gap to the street rate first."""
    rates, changed = streets(f), last_changes(f)
    result = []
    for lease in f.b.leases.values():
        if lease.ended is not None:
            continue
        unit = f.rent.units.get(lease.unit)
        size = (unit.size if unit else '') or ''
        street = rates.get(size)
        gap = None if street is None else street - lease.rent_cents
        person = f.people[lease.tenant]
        when = changed.get(lease.id)
        result.append({'id': lease.id, 'name': person['name'], 'slug': person['slug'], 'unit': lease.unit, 'size': size, 'rent': lease.rent_cents,
                       'changed': dated(when, f.today.year) if when else 'Not changed since move-in', 'street': street, 'gap': gap,
                       'open': lease.id in open_leases})
    return sorted(result, key=lambda r: (r['gap'] is None, -(r['gap'] or 0), r['name'], r['unit']))


def ways(methods) -> str:
    words = [price_law.CHANNEL_WORDS[m] for m in methods]
    return words[0] if len(words) == 1 else ', '.join(words[:-1]) + ' or ' + words[-1]


def facts(f, lease, sets) -> list[str]:
    """What the rules ask for this lease, before anything is proposed: the notice it needs, the owner's rule, a maximum disclosed to the
    tenant, and what the agreement promises. The core's checks say the rest when a proposal is made."""
    terms = price_law.terms_of(f.b, lease.id)
    if terms is None:
        return ["The signed agreement's terms are not on file for this lease, so an increase would be held."]
    out = []
    days = price_law.required_notice_days(f.b, sets, terms)
    if days is not None and terms.notice_methods:
        out.append(f"{days} days' notice, by {ways(terms.notice_methods)}.")
    elif days is not None:
        out.append(f"{days} days' notice.")
    if owner := price_law.owner_rule(f.b):
        out.append(f"The owner's rule: {owner['sentence']}")
    for m in price_law.state(f.b).commitments.get(lease.id, ()):
        if m.covers_to >= f.today:
            out.append(f'A maximum of {money(m.max_cents)} a month was disclosed to this tenant through {dated(m.covers_to, f.today.year)}.')
    if not terms.rent_change_allowed:
        out.append('The agreement does not let the rent change during its term.')
    elif terms.price_promise_until and terms.price_promise_until >= f.today:
        cap = 'no increase' if terms.price_promise_max_cents is None else f'no more than {money(terms.price_promise_max_cents)}'
        out.append(f'The agreement promises {cap} through {dated(terms.price_promise_until, f.today.year)}.')
    return out


def cents_of(text) -> int | None:
    """Dollars and cents typed on a form, as cents; None when it is not an amount."""
    text = (text or '').strip()
    if not re.fullmatch(r'\d{1,8}(?:\.\d{1,2})?', text):
        return None
    return int(Decimal(text) * 100)


def operation(data) -> str:
    """What a form is about, so two forms of one page load never share a completion: the lease for a proposal, the increase for an
    approval, the increase and the way for a notice."""
    return ':'.join(data.get(key, '')[:80] for key in ('lease', 'about', 'method'))


def propose(fd, facility, ident, data, staff, token) -> str:
    """A proposed increase for one lease, through the core's staff command. Empty when it passed; else why nothing was proposed. An entry
    that is not an increase is turned back here, before the core, so it leaves no record."""
    f = facility(fd, ident)
    lease = f.b.leases.get(data.get('lease', '')[:40])
    if lease is None or lease.ended is not None:
        return 'Choose a tenant who is renting a unit.'
    if any(r['lease'] == lease.id and r['stage'] != 'billed' for r in stages(f)):
        return 'An increase is already in progress for this lease.'
    cents = cents_of(data.get('rent'))
    if cents is None or cents <= 0:
        return 'Enter a valid rent in dollars and cents.'
    if cents <= lease.rent_cents:
        return f'Enter a rent higher than the current rent of {money(lease.rent_cents)}.'
    try:
        on = date.fromisoformat(data.get('effective', ''))
    except ValueError:
        return 'Enter the date the new rent takes effect.'
    profile = price_law.profile_of(f.b)
    sets = price_law.rules_in_force(profile.place, f.today) if profile.place else []
    recurring = any(rs.recurring_fee for rs in sets if rs.topic == 'disclosure')
    other = cents_of(data.get('other')) if recurring else 0           # only a rule that counts the whole monthly fee asks for it
    last = last_changes(f).get(lease.id)
    form = {'kind': 'lease_increase', 'current_cents': lease.rent_cents, 'new_cents': cents, 'effective_on': on.isoformat(), 'lease': lease.id,
            'other_recurring_cents': other, 'last_increase_on': last.isoformat() if last else None, 'point': 'proposal'}
    act = price_law.Act(f'rent-increase:{token}:{lease.id}:propose', 'check_price_change', staff, f'RI-{token}-{lease.id}', form)       # one proposal per form, so per lease
    decision = price_law.run(fd.ready(), fd.stream(ident), act, fd.clock())
    return '' if decision.allowed else decision.check


SNAPSHOT_KEYS = ('kind', 'current_cents', 'new_cents', 'effective_on', 'lease', 'unit_class', 'invoice', 'disclosure', 'claimed_exception',
                 'last_increase_on', 'other_recurring_cents')


def same_change(row, point) -> dict:
    """The change as it was proposed, to be checked again at another point of its life. Every field is the proposal's own, so the
    approval, the notice and the bill all rest on the one set of terms."""
    return {**{key: row['snap'].get(key) for key in SNAPSHOT_KEYS}, 'point': point}


def command(fd, ident, staff, token, name, action, about, form):
    return price_law.run(fd.ready(), fd.stream(ident), price_law.Act(f'rent-increase:{token}:{name}', action, staff, about, form), fd.clock())


def approve(fd, facility, ident, data, staff, token) -> str:
    row = next((r for r in stages(facility(fd, ident)) if r['id'] == data.get('about', '')[:80]), None)
    if row is None or not (row['stage'] == 'proposed' or row['again']):
        return 'That increase is not waiting for approval.'
    decision = command(fd, ident, staff, f"{token}:{row['id']}", 'approve', 'approve_price_change', row['id'], same_change(row, 'approval'))
    if decision.allowed and row['noticed']:
        # a notice is already on file: with the approval renewed it is checked again, so the row says at once whether it counts
        decision = command(fd, ident, staff, f"{token}:{row['id']}", 'complete', 'check_price_change', row['id'], same_change(row, 'notice_completion'))
    return '' if decision.allowed else decision.check


def day_typed(text) -> date | None:
    try:
        return date.fromisoformat(text or '')
    except ValueError:
        return None


def notice(fd, facility, ident, data, staff, token) -> str:
    """Record that the notice was given, by one of the ways this lease's terms allow, then check whether it counts. The core decides
    whether the way and the evidence qualify; this only takes down what staff did."""
    f = facility(fd, ident)
    row = next((r for r in stages(f) if r['id'] == data.get('about', '')[:80]), None)
    if row is None:
        return 'That increase is not on file.'
    if row['stage'] == 'proposed':
        return 'Approve the increase before recording its notice.'
    if row['stage'] not in ('approved', 'unproven'):
        return 'A notice for this increase has already been recorded and counts.'
    method = data.get('method')
    if method not in dict(row['ways']):
        return 'Choose one of the ways the lease allows.'
    sent = day_typed(data.get('sent_on')) or (f.today if not data.get('sent_on') else None)
    if sent is None or sent > f.today:
        return 'Enter the day the notice was sent: today or an earlier day.'
    delivered = None
    if method == 'hand':
        delivered = day_typed(data.get('delivered_on'))
        if delivered is None or delivered > f.today:
            return 'Enter the day it was delivered: today or an earlier day.'
    terms = price_law.terms_of(f.b, row['lease'])
    sent_to = {'first_class_mail': terms.last_known_address, 'email': terms.last_known_email}.get(method, f"Handed to {row['name']}")
    token = f"{token}:{row['id']}:{method}"            # this form's own operation: a notice of this change by this way
    if (delivered or sent) == f.today:
        # a notice given today must still leave the full notice period; one given on an earlier day is judged from that day when it is checked
        decision = command(fd, ident, staff, token, 'authorize', 'check_price_change', row['id'], same_change(row, 'notice_authorization'))
        if not decision.allowed:
            return decision.check
    letter = (f"Notice of a change in rent for {row['name']}, unit {row['unit']}: from {money(row['current'])} to {money(row['new'])} a month, "
              f"taking effect {row['snap']['effective_on']}.")
    recorded = command(fd, ident, staff, token, 'record', 'record_notice', row['id'], {
        'change': row['id'], 'lease': row['lease'], 'document_sha256': hashlib.sha256(letter.encode()).hexdigest(), 'recipient': row['name'],
        'channel': method, 'sent_to': sent_to, 'attempted_on': sent.isoformat(), 'dispatch_accepted': method == 'email' and data.get('accepted') == 'yes',
        'certificate_of_mailing': method == 'first_class_mail' and data.get('certificate') == 'yes',
        'postage_prepaid': method == 'first_class_mail' and data.get('postage') == 'yes',
        'delivered_on': delivered.isoformat() if delivered else None, 'new_cents': row['new'], 'effective_on': row['snap']['effective_on']})
    if not recorded.allowed:
        return recorded.check
    decision = command(fd, ident, staff, token, 'complete', 'check_price_change', row['id'], same_change(row, 'notice_completion'))
    return '' if decision.allowed else decision.check


def withdraw(fd, facility, ident, data, staff, token) -> str:
    """Take an increase back before it is billed, so a new one can be proposed for the lease. The core refuses one that is billed."""
    row = next((r for r in stages(facility(fd, ident)) if r['id'] == data.get('about', '')[:80]), None)
    if row is None or row['stage'] == 'billed':
        return 'That increase is not in progress.'
    decision = command(fd, ident, staff, f"{token}:{row['id']}", 'withdraw', 'withdraw_price_change', row['id'], {})
    return '' if decision.allowed else decision.check


def gap_words(gap) -> str:
    if gap is None:
        return ''
    return 'Same as the street rate' if gap == 0 else f'{money(abs(gap))} {"below" if gap > 0 else "above"}'


def mount(app, fd, need, same_site, form, show, facility, staff):
    @app.get('/rent-increases', include_in_schema=False)
    def rent_increases_page(request: Request, page: str = '1'):
        ident = need(request)
        f = facility(fd, ident)
        profile = price_law.profile_of(f.b)
        reviews = {rule.review for rule in price_law.rules_in_force(profile.place, f.today)} if profile.place else set()
        progress = stages(f)
        rows = leases(f, {r['lease'] for r in progress if r['stage'] != 'billed'})
        last = max(1, -(-len(rows) // PAGE_SIZE))
        number = min(max(int(page) if re.fullmatch(r'\d{1,6}', page) else 1, 1), last)
        shown = rows[(number - 1) * PAGE_SIZE: number * PAGE_SIZE]
        sets = price_law.rules_in_force(profile.place, f.today) if profile.place else []
        for row in shown:
            lease = f.b.leases[row['id']]
            row['facts'] = facts(f, lease, sets)
            terms = price_law.terms_of(f.b, lease.id)
            days = price_law.required_notice_days(f.b, sets, terms) if terms else None
            row['default_date'] = (f.today + timedelta(days=days)).isoformat() if days is not None else ''
        return show(request, 'fms_rent_increases.html', 'rent-increases', f, reviews=reviews, progress=progress, rows=shown, total=len(rows),
                    number=number, last=last, first_shown=(number - 1) * PAGE_SIZE + 1, gap_words=gap_words, token=secrets.token_hex(6),
                    today_iso=f.today.isoformat(), asks_other=any(rs.recurring_fee for rs in sets if rs.topic == 'disclosure'),
                    note=page_words(fd.take_step_note(ident)))

    @app.post('/rent-increases', include_in_schema=False)
    async def rent_increases_step(request: Request):
        same_site(request)
        ident = need(request)
        data = await form(request)
        token = data.get('token', '')
        action = data.get('action')
        if not re.fullmatch(r'[0-9a-f]{12}', token) or action not in ('propose', 'approve', 'notice', 'withdraw'):
            raise HTTPException(422, 'Reload the page and try again.')

        def apply():
            with fd.lock:
                key = (ident, f"rent-increase:{token}:{action}:{operation(data)}")       # the page's token plus the form's own operation
                if key in fd.site_forms:
                    return
                why = {'propose': propose, 'approve': approve, 'notice': notice, 'withdraw': withdraw}[action](fd, facility, ident, data, staff, token)
                if why:
                    fd.keep_step_note(ident, why)
                else:
                    fd._step_notes.pop(ident, None)
                    fd.site_forms.add(key)

        await run_in_threadpool(apply)
        return RedirectResponse('/rent-increases', status_code=303)
