"""Staff-facing readings of the street rate records."""
import re
import secrets

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from core import price_law, street_rates
from core.evidence import clock

from . import pricing_actions
from .wording import plain


def page_words(text):
    text = re.sub(r"\b(?:SR|SRS|PLD)\d{4,}\b", "", plain(text or ""))
    for code, name in price_law.PLACE_NAMES.items():
        text = re.sub(r"\b" + re.escape(code) + r"\b", name, text)
    return re.sub(r"(?<=for )[A-Z]{2}\b", "this place", text)


def size_area(size):
    numbers = [int(n) for n in re.findall(r'\d+', size or '')]
    return numbers[0] * numbers[1] if len(numbers) >= 2 else float('inf')


def rows(f):
    events = f.fd.ready().events(f.fd.stream(f.ident))
    limits = {}
    latest = {}
    published = {}
    decisions = {}
    for event in events:
        p = event.payload
        if event.kind == 'street_rate.limits':
            limits[p['size']] = street_rates.Limits(**p['limits'])
        elif event.kind == 'unit.priced' and event.source.startswith('staff:') and p.get('size'):
            published[p['size']] = event
        elif event.kind == 'street_rate.versioned':
            latest[p['id']] = event
            if p['status'] == 'published':
                published[p['size']] = event
        elif event.kind == 'price_law.decided':
            decisions[p['change']] = p
    # smallest unit first, as every FMS lists sizes (a text sort would put 10x10 before 5x10)
    sizes = sorted({u.size for u in f.rent.units.values() if u.size}, key=lambda s: (size_area(s), s))
    result = []
    for size in sizes:
        units = [u for u in f.rent.units.values() if u.size == size]
        rates = {u.rent_cents for u in units}
        last = published.get(size)
        waiting = []
        for event in latest.values():
            p = event.payload
            if p['size'] != size or p['status'] not in ('proposed', 'approved'):
                continue
            check = decisions.get(p['id'], {})
            reasons = [page_words(r['words']) for r in check.get('reasons', []) if r.get('words')]
            waiting.append({'id': p['id'], 'status': p['status'], 'new': p['new_cents'],
                            'reason': page_words(p['reason']), 'checks': reasons})
        changes = sorted((event for event in latest.values() if event.payload['size'] == size),
                         key=lambda event: event.id, reverse=True)[:5]
        history = [{'at': clock(event.at), 'by': event.payload['by'].removeprefix('staff:').replace('_', ' ').capitalize(),
                    'old': event.payload['current_cents'], 'new': event.payload['new_cents'],
                    'status': event.payload['status'], 'reason': page_words(event.payload['reason'])} for event in changes]
        result.append({'size': size, 'rate': next(iter(rates)) if len(rates) == 1 else None,
                       'varied': len(rates) > 1, 'rates': sorted(x for x in rates if x is not None), 'limits': limits.get(size),
                       'vacant': sum(f.rent.status(u.id, f.now) == 'vacant' for u in units),
                       'last': {'at': clock(last.at), 'by': last.payload.get('by', last.source).removeprefix('staff:').replace('_', ' ').capitalize()} if last else None,
                       'waiting': waiting, 'history': history})
    return result


def mount(app, fd, need, same_site, form, show, facility, staff):
    @app.get('/pricing', include_in_schema=False)
    def pricing_page(request: Request):
        f = facility(fd, need(request))
        profile = price_law.profile_of(f.b)
        reviews = {rule.review for rule in price_law.rules_in_force(profile.place, f.today)} if profile.place else set()
        return show(request, 'fms_pricing.html', 'pricing', f, rows=rows(f), reviews=reviews,
                    token=secrets.token_hex(6), note=page_words(fd.take_step_note(f.ident)))

    @app.post('/pricing', include_in_schema=False)
    async def pricing_step(request: Request):
        same_site(request)
        ident = need(request)
        data = await form(request)
        token = data.get('token', '')
        if not re.fullmatch(r'[0-9a-f]{12}', token) or data.get('action') not in ('propose', 'approve', 'publish'):
            raise HTTPException(422, 'Reload the page and try again.')

        def apply():
            with fd.lock:
                key = (ident, 'pricing:' + token)
                if key in fd.site_forms:
                    return
                why = (pricing_actions.propose(fd, ident, data, staff) if data.get('action') == 'propose'
                       else pricing_actions.advance(fd, ident, data, staff))
                if why:
                    fd.keep_step_note(ident, why)
                else:
                    fd._step_notes.pop(ident, None)
                    fd.site_forms.add(key)

        await run_in_threadpool(apply)
        return RedirectResponse('/pricing', status_code=303)
