"""Staff-facing readings of first-month deal records."""
from datetime import date

from core import promotions
from core.books import local
from core.evidence import day, money

from .wording import plain


def rows(f):
    state = promotions.state(f.b)
    result = []
    for key, versions in state.versions.items():
        try:
            version = promotions.version_in_force(f.b, key, f.today)
        except promotions.PromotionRefused:
            version = None
        terms = version.terms if version else None
        redeemed = [r for r in state.redemptions.values() if r.promotion == key and r.carried_from is None]
        given = sum(c['cents'] for r in state.redemptions.values() if r.promotion == key for c in r.concessions
                    if not f.b.charges[c['charge']].closed)
        stacks = []
        if terms:
            for other in terms['stacks_with']:
                try:
                    stacks.append(promotions.version_in_force(f.b, other, f.today).terms['name'])
                except promotions.PromotionRefused:
                    stacks.append('A deal not yet in force')
        def date_words(d):
            return f'{day(d)}, {d.year}'
        result.append({'id': key, 'name': plain((terms or versions[-1].terms)['name']), 'terms': terms,
                       'offer': (money(terms['price_cents']) if terms.get('price_cents') is not None else f"{terms['percent']}% off") if terms else '',
                       'covers': ('the rest of the first month, prorated by day' if terms['covers'] == 'rest_of_first_month' else 'the first full month') if terms else '',
                       'customers': ('New customers' if terms['customers'] == 'new' else 'All customers') if terms else '',
                       'expires': date_words(date.fromisoformat(terms['expires'])) if terms else '',
                       'stacks': ', '.join(plain(s) for s in stacks) or 'None', 'used': len(redeemed), 'given': given,
                       'latest': versions[-1].terms, 'effective': max(f.today, versions[-1].effective).isoformat(),
                       'stack_choices': [{'id': k, 'name': plain(vs[-1].terms['name']), 'selected': k in versions[-1].terms['stacks_with']} for k, vs in state.versions.items() if k != key],
                       'history': [{'when': date_words(local(v.at).date()), 'effective': date_words(v.effective),
                                    'by': v.by.removeprefix('staff:').replace('_', ' ').capitalize()} for v in reversed(versions[-5:])]})
    return result
