"""Publish deal terms using the core's validation and transaction."""
from datetime import date
import re
from decimal import Decimal, InvalidOperation

from core import promotions
from core.billing import Ledger, Refused


def cents(value):
    if not value:
        return None
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount * 100 != (amount * 100).to_integral_value() or abs(amount) > 99999999:
            raise ValueError
        return int(amount * 100)
    except (InvalidOperation, ValueError):
        raise ValueError('Enter dollars and cents as an amount with at most two decimal places.') from None


def whole(value):
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise ValueError('Enter whole numbers for the percent and redemption limit.') from None


def publish(fd, ident, data, staff):
    store, stream = fd.ready(), fd.stream(ident)
    try:
        effective = date.fromisoformat(data.get('effective', ''))
    except ValueError:
        return 'Enter a real effective date.'
    try:
        price = cents(data.get('price', ''))
        percent = whole(data.get('percent', ''))
        budget = cents(data.get('budget', ''))
        limit = whole(data.get('max_redemptions', ''))
    except ValueError as exc:
        return str(exc)
    with store.write():
        led = Ledger(store.books(stream))
        versions = promotions.state(led.books).versions
        key = data.get('promotion', '')
        if key not in versions:
            return 'The deal could not be found. Reload the page and try again.'
        stacks = [value for name, value in data.items() if name.startswith('stack_')]
        if any(other not in versions or other == key for other in stacks) or len(set(stacks)) != len(stacks):
            return 'Choose existing deals in the stacking rule.'
        previous = versions[key][-1].terms['stacks_with']
        stacks = [other for other in previous if other in stacks] + [other for other in stacks if other not in previous]
        covers = data.get('covers', '')
        terms = {**versions[key][-1].terms, 'name': data.get('name', '').strip(), 'unit_types': [s.strip() for s in data.get('unit_types', '').split(',') if s.strip()],
                 'customers': data.get('customers', ''), 'price_cents': price, 'percent': percent, 'covers': covers,
                 'proration': promotions.COVERS.get(covers), 'expires': data.get('expires', ''),
                 'stacks_with': stacks, 'transfer': data.get('transfer', ''),
                 'max_redemptions': limit, 'max_concession_cents': budget}
        # The core distinguishes an omitted deal kind from a value, and the latest terms may omit the unused key.
        latest = versions[key][-1].terms
        if price is None and 'price_cents' not in latest:
            terms.pop('price_cents')
        if percent is None and 'percent' not in latest:
            terms.pop('percent')
        try:
            promotions.publish_version(led, key, terms, effective, staff, fd.clock())
        except Refused as exc:
            def formatted(match):
                try:
                    d = date.fromisoformat(match.group())
                    return f'{d:%b} {d.day}, {d.year}'
                except ValueError:
                    return match.group()
            message = re.sub(r'\b\d{4}-\d{2}-\d{2}\b', formatted, str(exc))
            for code, words in {'rest_of_first_month': 'the rest of the first month', 'first_full_month': 'the first full month', 'by_days': 'by day'}.items():
                message = message.replace(code, words)
            return message
        store.append(stream, led.pending)
    return None


def mount(app, fd, need, same_site, form, staff):
    import re
    from fastapi import HTTPException, Request
    from fastapi.responses import RedirectResponse
    from starlette.concurrency import run_in_threadpool

    @app.post('/deals', include_in_schema=False)
    async def deals_publish(request: Request):
        same_site(request)
        ident = need(request)
        data = await form(request)
        token = data.get('token', '')
        if not re.fullmatch(r'[0-9a-f]{12}', token):
            raise HTTPException(422, 'Reload the page and try again.')

        def apply():
            with fd.lock:
                key = (ident, 'deals:' + token)
                if key in fd.site_forms:
                    return
                why = publish(fd, ident, data, staff)
                if why:
                    fd.keep_step_note(ident, why)
                else:
                    fd._step_notes.pop(ident, None)
                    fd.site_forms.add(key)

        await run_in_threadpool(apply)
        return RedirectResponse('/deals', status_code=303)
