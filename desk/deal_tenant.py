"""Deal details retained on a tenant's leases."""
from datetime import date

from core import promotions
from core.evidence import money
from .wording import plain


def for_lease(books, lease):
    state = promotions.state(books)
    result = []
    for red in state.redemptions.values():
        if red.lease != lease.id:
            continue
        terms = state.versions[red.promotion][red.version - 1].terms
        given = [c for c in red.concessions if not books.charges[c['charge']].closed]
        periods = [date.fromisoformat(p + '-01').strftime('%B %Y') for p in red.periods]
        result.append({'name': plain(terms['name']), 'months': ', '.join(periods),
                       'amount': money(sum(c['cents'] for c in given) if given else red.planned_cents),
                       'expected': not given, 'first': red.first_full_price.strftime('%B %Y'), 'rent': money(lease.rent_cents)})
    return result
