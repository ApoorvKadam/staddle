"""Available deal descriptions for an online rental."""
from core import promotions
from core.evidence import money
from .wording import plain


def offers(f, unit):
    result = []
    for key in promotions.state(f.b).versions:
        try:
            version = promotions.version_in_force(f.b, key, f.today)
            terms = version.terms
            promotions.price(f.b, f.rent, [key], unit=unit.id, tenant='', start=f.today, now=f.now)
        except promotions.PromotionRefused:
            continue
        amount = money(terms['price_cents']) if terms.get('price_cents') is not None else f"{terms['percent']}% off"
        covered = 'the rest of the first month, prorated by day' if terms['covers'] == 'rest_of_first_month' else 'the first full month'
        customers = 'new customers' if terms['customers'] == 'new' else 'all customers'
        result.append({'id': key, 'name': plain(terms['name']), 'description': plain(f'{amount} for {covered}; {customers}')})
    return result
