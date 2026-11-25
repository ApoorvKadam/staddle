"""A live copy with this morning's two move-outs, and stand-ins for everything the shift calls: the model, the second check and the day's
budget. No network. The year is built once per process; each test gets its own store."""
import json
from datetime import timedelta
from functools import lru_cache
from types import SimpleNamespace as NS

from core import renting, walk
from core.seed import NOW, build
from core.store import BASE, Store
from core.tilden import cases, shift

STAFF = 'staff:front_desk'
LATER = NOW + timedelta(minutes=10)


@lru_cache(maxsize=1)
def year():
    return build(standing=True, shift=True)


def world(stream='visitor:t'):
    """A store with the base year and the old gate codes of the two move-outs already revoked, and the two units' names:
    (store, repair unit, unclear unit)."""
    store = Store()
    with store.write():
        store.append(BASE, list(year().led.pending))
    access = renting.StandIn()
    access.know(store.events(BASE))
    renting.advance(store, stream, access, LATER)
    events = store.events(BASE)
    repair = next(e.payload['unit'] for e in events if e.kind == 'work_order.reported' and e.at > NOW)
    unclear = next(e.payload['unit'] for e in events if e.kind == 'walk.observed' and e.at > NOW)
    return store, repair, unclear


class Allow(shift.Allowance):
    """The day's budget as a plain dict of reservations, with a cap."""

    def __init__(self, cap=100.0):
        self.rows, self.cap, self.asked = {}, cap, []

    def reserve(self, key, cents):
        self.asked.append(key)
        if key in self.rows:
            return True
        if sum(self.rows.values()) + cents > self.cap:
            return False
        self.rows[key] = cents
        return True

    def settle(self, key, cents):
        mine = [k for k in self.rows if k.startswith(key + ':')]
        for i, k in enumerate(mine):
            self.rows[k] = cents if i == 0 else 0


class Model:
    """The planning model: every record gets one turn. A note that mentions boxes is handed to staff, anything else ends the turn."""

    def __init__(self, hand_boxes=True):
        self.calls, self.hand_boxes, self.messages = [], hand_boxes, self

    def create(self, **kw):
        self.calls.append(kw)
        usage = NS(input_tokens=600, output_tokens=30, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        content = kw['messages'][0]['content']
        asked = len([m for m in kw['messages'] if m['role'] == 'user']) > 1
        if self.hand_boxes and 'cardboard boxes' in content.split('Judge this new record:')[-1] and not asked:
            block = NS(type='tool_use', id='h', name='hand_to_staff', input={
                'question': 'Are the boxes the former tenant\'s belongings?', 'record_ids': [], 'role': 'front_desk', 'wake_evidence': 'staff note'})
            return NS(stop_reason='tool_use', content=[block], usage=usage)
        return NS(stop_reason='end_turn', content=[], usage=usage)


def check(store, stream, case_id, kind, question, text, now, choices=None):
    """The second check: sure of a same-fault pair and of a look that says the unit is fine; unsure of a look that names a problem."""
    p = 0.9
    if 'Must it be settled' in question:
        p = 0.9
    if 'needs no work' in question:
        p = 0.05
    if 'fine to rent again' in question:
        look = json.loads(text).get('latest_look', '').lower()
        p = 0.05 if any(w in look for w in ('not ready', 'still', 'catches', 'broken')) else 0.95
    return NS(probabilities={'yes': p}, reason=None, choice='yes')


def readers():
    return {'read_note': lambda *a: NS(refusal=None, fields={'contents': 'not seen'}, quotes={}, work=[]), 'same_fault': lambda *a: None}


def step(store, stream, now, allow, model, **kw):
    return shift.start_shift(store, stream, now, allowance=allow, client=model, readers=readers(), check=check, **kw)


def case_of(store, stream, unit):
    return next(c for c in cases.load(store, stream).values() if c.unit == unit)


def staff(store, stream, at, action, name, **kw):
    d = walk.run(store, stream, walk.Act(f'test:{name}:{action}', action, STAFF, **kw), at)
    assert d.allowed, d.check
    return d.made


def finish_order(store, stream, start, order, words):
    """The five staff steps on one work order, the last with the words that close it. Returns the time after."""
    for i, (action, extra) in enumerate((('triage_work_order', {'text': 'Looked at it.'}), ('assign_work_order', {'to': 'office_manager'}),
                                         ('record_order_work', {'text': words}), ('check_order_work', {'text': words}),
                                         ('close_work_order', {'text': words}))):
        staff(store, stream, start + timedelta(minutes=i), action, order, about=order, **extra)
    return start + timedelta(minutes=5)


def open_orders(store, stream, unit):
    return [o for o in walk.load(store, stream)[1].orders.values() if o.unit == unit and o.state != 'closed']
