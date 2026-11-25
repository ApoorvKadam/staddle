"""One visitor's morning shift: which open unit-ready cases Tilden works today, what each may cost, and the wake when staff write records.

The shift never decides anything about a unit. It lists the cases the records have opened, admits at most two of them a day for model
work, reserves each one's worst cost before any call, and hands the admitted ones to runtime.sweep. A case it did not admit is paused
with the reason on its record, so the sweep does not run it. Staff records wake an admitted case through the sweep's own wake rules."""
import threading
from dataclasses import dataclass, field
from datetime import datetime

from core import renting
from core.agent import worst
from core.books import local
from core.store import Event
from core.tilden import cases, gate, playbooks, runtime

ADMIT_PER_DAY = 2
# The most one case may cost in a day, second checks and continuations included: about twice the 1.2 cents measured for a case.
CASE_RESERVE_CENTS = 2.0
# What one visitor's shift work may cost in a day, and what all shifts together may cost (the rest of the $1 is for message reading).
VISITOR_DAY_CENTS = 4.0
ALL_SHIFTS_DAY_CENTS = 80.0
# What one more model turn and one more second check can cost at most: a planner call's prompt is bounded by its tools and the case's
# records (counted at 24,000 characters), its output by 4,000 tokens; a check reads about 6,000 characters and writes nothing.
TURN_CENTS = worst('claude-haiku-5-5', 24000, 4000)
CHECK_CENTS = 0.1
# Second checks one wake can ask for: linking, release, a handoff settled, a no-work close.
CHECKS_PER_WAKE = 3
HOLD_REASON = 'Vacated: kept off the rentable list until someone looks at it.'
PAUSED = 'shift_wait'
_locks: dict = {}
_running: set = set()
_guard = threading.Lock()


@dataclass
class Shift:
    admitted: list = field(default_factory=list)     # case ids admitted today
    newly: list = field(default_factory=list)        # admitted by this call
    waiting: list = field(default_factory=list)      # case ids held back, with the reason on the case's record
    busy: bool = False                               # another step of this copy's shift was already running


class Allowance:
    """What the shift asks of the day's shared budget. The desk implements it over its usage table; a reservation for a key that was
    already made counts once."""

    def reserve(self, key: str, cents: float) -> bool:
        raise NotImplementedError

    def settle(self, case_id: str, cents: float) -> None:
        raise NotImplementedError


def _day(now: datetime) -> str:
    return local(now).date().isoformat()


def _lock(store, stream):
    with _guard:
        return _locks.setdefault((id(store), stream), threading.Lock())


def running(store, stream) -> bool:
    """Whether a shift step of this copy is going on now."""
    with _guard:
        return (id(store), stream) in _running


def started(store, stream, day: str) -> bool:
    """Whether the shift has admitted a case on this copy today."""
    return bool(_admitted(store, stream, day))


def _grant_start(store, stream, now):
    """When the earliest live grant for this playbook's tools was made, or None; the sweep opens cases only from records after it."""
    latest = {}
    for event in store.events(stream):
        if event.kind == 'tilden.authority.granted' and event.at <= now and event.payload['action'] in playbooks.get('make-unit-ready')['tools']:
            latest[event.payload['action']] = event
    live = [e.at for e in latest.values() if datetime.fromisoformat(e.payload['expires']) > now]
    return min(live) if live else None


def open_from_records(store, stream, now) -> None:
    """Open the cases the sweep would open, so they exist before it runs and the shift can admit or hold each one: a move-out, or a note
    on a vacant unit, after the owner's grants, for a unit with no open case. A note on a unit already released is the sweep's own
    reopening and is left to it. The case is keyed by its source record, so opening twice makes one."""
    earliest = _grant_start(store, stream, now)
    if earliest is None:
        return
    books, rent = renting.load(store, stream)
    for event in store.events(stream):
        if not earliest <= event.at <= now:
            continue
        unit = None
        if event.kind == 'lease.ended':
            lease = books.leases.get(event.payload.get('lease'))
            unit = lease.unit if lease else event.payload.get('unit')
        elif event.kind == 'walk.observed':
            candidate = event.payload.get('unit')
            if event.payload.get('note') and candidate in books.units and rent.status(candidate, now) == 'vacant':
                unit = candidate
        if unit not in books.units:
            continue
        everything = list(cases.load(store, stream).values())
        if any(c.unit == unit and c.state != 'done' for c in everything):
            continue
        if event.kind == 'walk.observed' and any(c.unit == unit and c.state == 'done' for c in everything):
            continue
        cases.open_case(store, stream, event, unit, now)


def _admitted(store, stream, day: str) -> dict:
    """Case id -> its admission record, for this day."""
    return {e.payload['case']: e for e in store.stream_events(stream) if e.kind == 'tilden.shift.admitted' and e.payload.get('day') == day}


def _reserved(store, stream, case_id: str) -> float:
    """What is still reserved for the case: every reservation, or what it really cost once it was settled, and reservations after that."""
    total = 0.0
    for e in store.stream_events(stream):
        if e.payload.get('case') != case_id:
            continue
        if e.kind == 'tilden.shift.reserved':
            total += e.payload['cents']
        elif e.kind == 'tilden.shift.settled':
            total = e.payload['cents']
    return total


def _new_records(store, stream, case, now) -> int:
    """Staff records on the unit the model has not judged yet: what the next wake will pay a turn for."""
    judged = {s['judged_event'] for s in case.steps if isinstance(s, dict) and 'judged_event' in s}
    found = [e for e in gate._unit_evidence(store, stream, case, case.source_event - 1, True)
             if e.at <= now and e.id not in judged and
             ((e.kind in ('walk.observed', 'walk.observation.corrected') and e.payload.get('note')) or
              (e.kind == 'work_order.reported' and e.payload.get('problem')) or
              (e.kind == 'work_order.closed' and e.payload.get('text')))]
    return len(found)


def _mark(store, stream, case, now, reason: str, why: str):
    """Pause a case with the reason on its record, once: a repeated command writes nothing more."""
    last = case.steps[-1] if case.steps and isinstance(case.steps[-1], dict) else {}
    fresh = gate._unit_evidence(store, stream, case, case.history[-1]['id'], True) if case.history else []
    if case.state == 'paused' and last.get(PAUSED) == why and not fresh:
        return case
    return cases.record(store, stream, case.id, 'updated', {'steps': case.steps + [{'reason': reason, PAUSED: why}], 'state': 'paused', 'wake': []},
                        now, source='agent:tilden')


def _hold(store, stream, case, now, check, readers) -> None:
    """The one step that needs no model: a vacated unit nobody has looked at stays off the rentable list."""
    reads = gate._records(store, stream, case, now)
    held = reads['availability']
    if held.get('status') == 'vacant' and not held.get('holder'):
        gate.act(store, stream, case.id, 'hold_unit', {'reason': HOLD_REASON}, now, check=check, readers=readers)


def _spent(store, stream, case, now) -> float:
    return runtime._spend(store, stream, case, now)[0]


def start_shift(store, stream, now, *, allowance: Allowance, client=None, readers=None, check=None, chooser='hybrid', wait=False) -> Shift:
    """One shift step for a copy. Cases come from the records; at most two are admitted for model work in a day, and an admitted case
    that has new records comes first, then the longest waiting, then the unit's name. A case is admitted only when its worst cost can be
    reserved in the shared daily budget; one that cannot is held back, its unit is still kept off the rentable list, and its record says
    why. Before an admitted case's wake the shift checks that what is still reserved covers the turns and second checks the wake can
    ask for, and tops the reservation up when it does not; a case that cannot afford them waits, with that said on its record. A
    repeated command, a reload or a second tab changes nothing: a step already running is not started again, a case is admitted once,
    and the sweep skips a case with no new records."""
    lock = _lock(store, stream)
    if not lock.acquire(blocking=wait):
        return Shift(busy=True)
    with _guard:
        _running.add((id(store), stream))
    try:
        return _step(store, stream, now, allowance, client, readers, check, chooser)
    finally:
        with _guard:
            _running.discard((id(store), stream))
        lock.release()


def _step(store, stream, now, allowance, client, readers, check, chooser) -> Shift:
    day = _day(now)
    open_from_records(store, stream, now)
    admitted = _admitted(store, stream, day)
    live = cases.open_cases(store, stream)
    at = {e.id: e.at for e in store.events(stream)}
    source = {c.id: at.get(c.source_event, now) for c in live}

    def order(c):
        fresh = c.id in admitted and bool(gate._unit_evidence(store, stream, c, c.history[-1]['id'], True))
        return (0 if fresh else 1 if c.id in admitted else 2, source[c.id], c.unit, c.id)
    result = Shift()
    for case in sorted(live, key=order):
        if case.id in admitted:
            continue
        if client is None or len(admitted) >= ADMIT_PER_DAY:
            reason = ('Tilden is off on this copy, so no model work is started.' if client is None
                      else 'Tilden works two units a morning; this one waits for a later shift.')
            why = 'off' if client is None else 'limit'
        elif allowance.reserve(f'{day}:{case.id}:0', CASE_RESERVE_CENTS):
            with store.write():
                store.append(stream, [Event('tilden.shift.reserved', now, {'case': case.id, 'cents': CASE_RESERVE_CENTS, 'key': f'{day}:{case.id}:0'},
                                            f'tilden:shift:reserved:{day}:{case.id}:0', source='code'),
                                      Event('tilden.shift.admitted', now, {'case': case.id, 'unit': case.unit, 'day': day},
                                            f'tilden:shift:admitted:{day}:{case.id}', source='code')])
            admitted[case.id] = True
            result.newly.append(case.id)
            continue
        else:
            reason = 'Tilden cannot start this unit: the day\'s allowance for model work cannot cover it. The unit stays off the rentable list.'
            why = 'allowance'
        case = cases.load(store, stream)[case.id]
        try:
            _hold(store, stream, case, now, check, readers)
        except Exception:
            pass
        _mark(store, stream, cases.load(store, stream)[case.id], now, reason, why)
        result.waiting.append(case.id)
    # admitted cases: a case that cannot pay for what its wake can ask of it waits, and says so
    for case in [c for c in cases.open_cases(store, stream) if c.id in admitted]:
        case = cases.load(store, stream)[case.id]
        remaining = _reserved(store, stream, case.id) - _spent(store, stream, case, now)
        need = (_new_records(store, stream, case, now) or 1) * TURN_CENTS + CHECKS_PER_WAKE * CHECK_CENTS
        topups = len([1 for e in store.stream_events(stream) if e.kind == 'tilden.shift.reserved' and e.payload.get('case') == case.id])
        while remaining < need and topups < 4:
            key = f'{day}:{case.id}:{topups}'
            if not allowance.reserve(key, CASE_RESERVE_CENTS):
                break
            with store.write():
                store.append(stream, [Event('tilden.shift.reserved', now, {'case': case.id, 'cents': CASE_RESERVE_CENTS, 'key': key},
                                            f'tilden:shift:reserved:{key}', source='code')])
            remaining += CASE_RESERVE_CENTS
            topups += 1
        waiting = case.state == 'paused' and case.steps and isinstance(case.steps[-1], dict) and case.steps[-1].get(PAUSED)
        if remaining < need:
            if case.state != 'done':
                try:
                    _hold(store, stream, case, now, check, readers)
                except Exception:
                    pass
                _mark(store, stream, cases.load(store, stream)[case.id], now,
                      'Tilden is waiting: the day\'s allowance for model work cannot pay for the next check yet. The unit stays off the rentable list.', 'allowance')
                result.waiting.append(case.id)
        elif waiting:
            # the allowance came back: the case carries on from the records that woke it
            cases.record(store, stream, case.id, 'updated', {'state': 'working', 'wake': []}, now, source='agent:tilden')
    runtime.sweep(store, stream, now, readers, chooser=chooser, client=client, check=check)
    for case_id in admitted:
        case = cases.load(store, stream).get(case_id)
        if case is None:
            continue
        spent, held = _spent(store, stream, case, now), _reserved(store, stream, case_id)
        if (case.state == 'done' or spent > held) and abs(spent - held) > 1e-9:
            allowance.settle(f'{day}:{case_id}', spent)
            with store.write():
                store.append(stream, [Event('tilden.shift.settled', now, {'case': case_id, 'cents': spent}, f'tilden:shift:settled:{day}:{case_id}:{spent:.6f}', source='code')])
    result.admitted = sorted(admitted)
    return result
