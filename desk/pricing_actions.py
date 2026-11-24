"""Staff forms for proposing street rates."""
from decimal import Decimal, InvalidOperation
import re

from core import renting, street_rates


def propose(fd, ident, data, staff):
    size = data.get('size', '')[:80]
    amount = data.get('rate', '').strip()
    try:
        if not re.fullmatch(r'\d{1,8}(?:\.\d{1,2})?', amount):
            raise InvalidOperation
        value = Decimal(amount)
        if value <= 0:
            raise InvalidOperation
        cents = int(value * 100)
    except InvalidOperation:
        return 'Enter a valid rate in dollars and cents.'
    store, stream = fd.ready(), fd.stream(ident)
    _, rent = renting.load(store, stream)
    units = [u for u in rent.units.values() if u.size == size]
    current = units[0].rent_cents if units and units[0].rent_cents else 0
    limits = street_rates._limits(store, stream, size) or street_rates.Limits(0, 0, 0, 0)
    result = street_rates.propose(store, stream, fd.clock(), size=size, current_cents=current, new_cents=cents,
                                  reason=data.get('reason', '')[:200], by=staff, limits=limits)
    return '' if result.allowed else result.check


def advance(fd, ident, data, staff):
    action = data.get('action')
    operation = street_rates.approve if action == 'approve' else street_rates.publish
    result = operation(fd.ready(), fd.stream(ident), fd.clock(), data.get('about', '')[:80], by=staff)
    return '' if result.allowed else result.check
