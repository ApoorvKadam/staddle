"""Run the unit readiness playbook against the copy's records."""
import json
import os
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime
from functools import partial

from core import renting, walk_reading
from core.store import Event
from core.tilden import authority, cases, decide as decisions, planner, playbooks, tools

# over a case's whole life, which spans several wakes (move-out, notes, repairs, the look); each model turn is one decision and each
# tool the worker calls is one tool call, the read-back after an action included in it
MAX_DECISIONS = 16
MAX_TOOL_CALLS = 40
CASE_CENTS = 25
DAILY_CENTS = 100


def _record(store, stream, case, now, **changes):
    return cases.record(store, stream, case.id, 'updated', changes, now, source='agent:tilden')


def _step(store, stream, case, now, entry, state='working', wake=None):
    return _record(store, stream, case, now, steps=case.steps + [entry], state=state, wake=wake or [])


def _staff(store, stream, case, now, reason):
    return _step(store, stream, case, now, {'reason': reason}, 'needs staff')


def _records(store, stream, case, now):
    return json.loads(json.dumps({name: tools.read(name, store, stream, case, now)
                                  for name in playbooks.get(case.objective)['reads']}, default=str))


def _args(action, records, readers, store, stream, case):
    if action == 'hold_unit':
        return {'reason': 'Work and inspection are needed before this unit is ready.'}, 0
    if action == 'open_work_order':
        notes = [n['note'] for n in records['walk_notes'] if n.get('note')]
        if not notes:
            return None, 0
        reading = readers['read_note'](notes[-1], (case.unit,), case.unit)
        work = [] if reading.refusal else [item['words'] for item in reading.work if item.get('words') in notes[-1]]
        cost = getattr(getattr(reading, 'call', None), 'cents', 0) or 0
        return ({'text': work[0]} if len(work) == 1 else None), cost
    if action == 'link_reports':
        _, rent = renting.load(store, stream)
        orders = [o for o in rent.floor.orders.values() if o.unit == case.unit and o.state != 'closed' and o.kind != 'check']
        if len(orders) != 2:
            return None, 0
        return {'order': orders[1].id, 'into': orders[0].id}, 0
    return {}, 0


# Set on practice cases only (2026-11-24, before any frozen run): a passing look after a repair scored 0.86 and a same-fault pair
# 0.89 to 0.90, while a look saying the roof still leaks and unrelated work scored near 0. Frozen with the workers before a graded set.
READY_CUTOFF = 0.75
SAME_FAULT_CUTOFF = 0.75
SETTLED_CUTOFF = 0.75     # after a handoff: later staff records must settle what was handed over (same practice basis as the others)
BELONGINGS_CUTOFF = 0.5
_REMOVAL = re.compile(r"\b(remove|removal|dispose|disposal|discard|throw|thrown|clear|clearing|haul|dump|take away|take out)\w*\b", re.I)


def _latest_look(store, stream, case, records):
    repairs = [o for o in records['work_orders'] if o['kind'] != 'check' and not o['joined_into']]
    books, _ = renting.load(store, stream)
    events = store.events(stream)
    repair_ids = {o['id'] for o in repairs}
    cutoffs = [(e.at, e.id) for e in events if
               (e.kind == 'work_order.closed' and e.payload.get('order') in repair_ids) or
               (e.kind == 'lease.ended' and (e.payload.get('unit') == case.unit or
                (e.payload.get('lease') in books.leases and books.leases[e.payload['lease']].unit == case.unit)))]
    cutoff = max(cutoffs, default=None)
    notes = {n['id']: n for n in records['walk_notes'] if n.get('note') and not n.get('superseded_by') and str(n.get('by', '')).startswith('staff:')}
    checks = {o['id'] for o in records['work_orders'] if o['kind'] == 'check'}
    looks = []
    for e in events:
        if cutoff is None or (e.at, e.id) <= cutoff:
            continue
        if e.kind in ('walk.observed', 'walk.observation.corrected') and e.payload.get('observation') in notes:
            note = notes[e.payload['observation']]
            looks.append((e.at, e.id, note['id'], note['note']))
        elif e.kind == 'work_order.closed' and e.payload.get('order') in checks and e.payload.get('by', '').startswith('staff:') and e.payload.get('text'):
            looks.append((e.at, e.id, e.payload['order'], e.payload['text']))
    latest = max(looks, default=None)
    return (latest[0], latest[2], latest[3]) if latest else None


# the only arguments each action takes; anything else from a model is refused, never forwarded
ALLOWED_ARGS = {'open_work_order': {'text', 'answers_record'}, 'link_reports': {'order', 'into'}, 'hold_unit': {'reason'},
                'release_unit': set(), 'request_inspection': {'answers_record'}, 'hand_to_staff': {'reason', 'evidence'}}


def _ready_now(store, stream, case, records):
    """The shared job text's ready test from the records alone: every repair order is closed, nothing else on the records blocks the
    unit, and someone looked at it after the last repair (or after the move-out when nothing was repaired). At release the look's own
    words also get a second opinion; this test never reads them."""
    repairs = [o for o in records['work_orders'] if o['kind'] != 'check' and not o['joined_into']]
    return (all(o['state'] == 'closed' for o in repairs) and not records['readiness']['reasons']
            and _latest_look(store, stream, case, records) is not None)


def run_case(store, stream, case_id, now, decide=None, readers=None, *, chooser=None, client=None, check=None):
    case = cases.load(store, stream)[case_id]
    if case.state in ('done', 'needs staff', 'blocked'):
        return case
    chooser = chooser or ('picker' if decide is not None else 'planner')
    if chooser not in ('planner', 'picker', 'fixed'):
        raise ValueError('unknown chooser')
    decide = decide or decisions.choose
    check = check or decisions.check
    readers = readers or {'read_note': partial(walk_reading.read_note, client,
        os.getenv('TILDEN_PLANNER_MODEL', 'claude-haiku-5-5'), os.getenv('TILDEN_PLANNER_EFFORT', 'medium')),
        'same_fault': partial(walk_reading.same_fault, client, os.getenv('TILDEN_PLANNER_MODEL', 'claude-haiku-5-5'),
                              os.getenv('TILDEN_PLANNER_EFFORT', 'medium'))}

    def current():
        return cases.load(store, stream)[case_id]

    def limit(c, stop=False):
        entries = [s for s in c.steps if isinstance(s, dict)]
        events = store.stream_events(stream)
        costs = [e for e in events if e.kind in ('tilden.decision', 'tilden.planner.call')]
        case_cost = sum(e.payload.get('cost_cents', 0) for e in costs if e.payload.get('case') == c.id) + sum(s.get('reader_cents', 0) for s in entries)
        day_cost = sum(e.payload.get('cost_cents', 0) for e in costs if e.at.date() == now.date())
        previous = {}
        for event in events:
            if event.kind != 'tilden.case.updated' or 'steps' not in event.payload:
                continue
            ident = event.payload['id']
            total = sum(s.get('reader_cents', 0) for s in event.payload['steps'] if isinstance(s, dict))
            if event.at.date() == now.date():
                day_cost += max(0, total - previous.get(ident, 0))
            previous[ident] = total
        exceeded = (sum(s.get('decisions', 0) for s in entries) >= MAX_DECISIONS or
                    sum(s.get('tool_calls', 0) for s in entries) >= MAX_TOOL_CALLS or case_cost >= CASE_CENTS or day_cost >= DAILY_CENTS)
        return _staff(store, stream, c, now, 'The case reached its decision, tool, or spending limit.') if stop else exceeded

    def opinion(question, value):
        try:
            result = check(store, stream, case_id, 'predicate', question, json.dumps(planner.protect(value), default=str), now)
            probability = result.probabilities.get('yes')
            if result.reason or probability is None or isinstance(probability, bool) or not isinstance(probability, (float, int)) or not 0 <= probability <= 1:
                return None
            return probability
        except Exception:
            return None

    def execute(action, args, pending=None):
        content, case_now, is_error = _execute(action, args, pending)
        if (is_error and action not in playbooks.get(case_now.objective)['reads'] and case_now.state not in ('done', 'needs staff')
                and not str(content).startswith('Waiting for')):     # waiting on an approval is not a refusal
            case_now = _step(store, stream, case_now, now, {'action': action, 'refused': str(content)[:300]}, case_now.state, case_now.wake)
        return content, case_now, is_error

    def _execute(action, args, pending=None):
        c = current()
        if action == 'decision':
            return '', _step(store, stream, c, now, {'decisions': 1}), False
        if action == 'staff':
            return args['reason'], _staff(store, stream, c, now, args['reason']), False
        if limit(c):
            return 'The case reached its limit.', limit(c, stop=True), True
        c = _step(store, stream, c, now, {'tool_calls': 1})
        if action == 'note_plan':
            if not isinstance(args.get('steps'), list) or not all(isinstance(x, str) for x in args['steps']):
                return 'Invalid plan.', c, True
            return 'Plan recorded.', _record(store, stream, c, now, plan=args['steps']), False
        if action == 'wait_for':
            record, what = args.get('record'), args.get('what')
            if not isinstance(record, str) or not isinstance(what, str):
                return 'Invalid wake condition.', c, True
            return 'Waiting for records.', _record(store, stream, c, now, state='waiting', wake=[{'record': record, 'what': what}]), False
        records = _records(store, stream, c, now)
        if action in playbooks.get(c.objective)['reads']:
            return json.dumps(planner.protect(records[action]), default=str), c, False
        note_id = args.get('record') if action == 'read_note' else args.get('answers_record')
        note = next((n for n in records['walk_notes'] if n['id'] == note_id and n.get('note') and not n.get('superseded_by')), None)
        reading = None
        if action == 'open_work_order' and not note:
            # work can also answer a report or an inspection that found a fault; its words stand in for a note's
            order = next((o for o in records['work_orders'] if o['id'] == note_id), None)
            if order:
                words = ' '.join([order.get('problem') or ''] + [ch.get('text') or '' for ch in order.get('changes', [])]).strip()
                note = {'id': order['id'], 'note': words, 'from_order': True}
        if action == 'open_work_order' and (note or {}).get('from_order'):
            # work that answers an order still runs the fixed tenant-items rule, on the unit's latest staff note when there is one
            latest = max((n for n in records['walk_notes'] if n.get('note') and not n.get('superseded_by')), key=lambda n: n['at'], default=None)
            if latest:
                try:
                    reading = readers['read_note'](latest['note'], (c.unit,), c.unit)
                    cost = getattr(getattr(reading, 'call', None), 'cents', 0) or 0
                    if cost:
                        c = _step(store, stream, c, now, {'reader_cents': cost})
                except Exception:
                    return 'The note could not be read safely.', c, True
        if action == 'read_note' or (action == 'open_work_order' and not (note or {}).get('from_order')):
            if not note:
                return 'The note record was not found.', c, True
            try:
                reading = readers['read_note'](note['note'], (c.unit,), c.unit)
                cost = getattr(getattr(reading, 'call', None), 'cents', 0) or 0
                if cost:
                    c = _step(store, stream, c, now, {'reader_cents': cost})
                if reading.refusal:
                    return 'The note could not be read safely.', c, True
            except Exception:
                return 'The note could not be read safely.', c, True
            if action == 'read_note':
                data = asdict(reading) if is_dataclass(reading) else vars(reading)
                return json.dumps(planner.protect(data, note['id']), default=str), c, False
        if action not in playbooks.get(c.objective)['tools']:
            return 'The action is outside this playbook.', c, True
        if action == 'release_unit':
            holder = records['availability']['holder']
            if holder and holder[0] == 'block' and not any(e.kind == 'unit.blocked' and e.payload['block'] == holder[1] and e.payload.get('case_id') == c.id for e in store.events(stream)):
                reason = 'Staff placed the unit block, so staff must release it.'
                return reason, _staff(store, stream, c, now, reason), True
            handed = [h for h in c.history if h.get('payload', {}).get('state') == 'needs staff']
            if handed:
                # after a handoff, only staff can settle what was handed over: a later staff record must be judged to settle it
                at = handed[-1]['at']
                why = next((s.get('reason') for s in reversed(c.steps) if isinstance(s, dict) and s.get('reason') and 'picked up again' not in s.get('reason', '')), '')
                later = [n['note'] for n in records['walk_notes'] if n.get('note') and str(n.get('by', '')).startswith('staff:') and str(n['at']) > str(at)]
                later += [ch.get('text') for o in records['work_orders'] for ch in o.get('changes', [])
                          if ch.get('text') and str(ch.get('by', '')).startswith('staff:') and str(ch.get('at')) > str(at)]
                if not later:
                    return 'Release refused: nothing staff wrote after the handoff settles it.', c, True
                p = opinion('A worker handed this storage unit\'s case to staff for the reason given. Staff then wrote the later records. '
                            'Do the later records settle that matter, so routine work on the unit can go on?',
                            {'id': c.id, 'handed_over_because': why, 'later_staff_records': later})
                if p is None:
                    reason = 'The settlement check failed; staff must review it.'
                    return reason, _staff(store, stream, c, now, reason), True
                c = _step(store, stream, c, now, {'second_opinion': 'handoff settled', 'p': round(p, 3)}, c.state, c.wake)
                if p < SETTLED_CUTOFF:
                    return 'Release refused: the records do not show staff settled what was handed to them.', c, True
            if not _ready_now(store, stream, c, records):
                return 'Release refused: work is open, something blocks the unit, or nobody has looked at it since the move-out or the last repair.', c, True
            look = _latest_look(store, stream, c, records)
            if not look:
                return 'Release refused: no recent look proves the unit is ready.', c, True
            # the look is judged with the repairs it follows, so "Door works" after a latch repair reads as the pass it is
            fixed = [{'problem': o.get('problem'), 'finished': [ch.get('text') for ch in o.get('changes', []) if ch.get('state') == 'closed']}
                     for o in records['work_orders'] if o['kind'] != 'check' and not o['joined_into']]
            p = opinion('A tenant moved out of this storage unit. The repairs done since are listed, then the latest look staff wrote. '
                        'Does the latest look say the unit is fine to rent again, with nothing still wrong?',
                        {'id': look[1], 'repairs': fixed, 'latest_look': look[2]})
            if p is None:
                return 'The readiness check failed; staff must review it.', _staff(store, stream, c, now, 'The readiness check failed; staff must review it.'), True
            c = _step(store, stream, c, now, {'second_opinion': 'ready', 'look': look[1], 'p': round(p, 3)}, c.state, c.wake)
            if p < READY_CUTOFF:
                return 'Release refused: a second check found the latest look does not show the unit is ready.', c, True
        if action == 'link_reports':
            orders = [o for o in records['work_orders'] if o['id'] in (args.get('order'), args.get('into'))]
            if len(orders) != 2 or args.get('order') == args.get('into'):
                return 'The two reports were not found.', c, True
            p = opinion('Do these two reports describe one fault?', orders)
            if p is None:
                return 'The fault check failed; staff must review it.', _staff(store, stream, c, now, 'The fault check failed; staff must review it.'), True
            c = _step(store, stream, c, now, {'second_opinion': 'same fault', 'p': round(p, 3)}, c.state, c.wake)
            if p < SAME_FAULT_CUTOFF:
                return 'not sure these are one fault', c, True
        if action == 'open_work_order':
            text = args.get('text')
            if not isinstance(text, str) or not text.strip():
                return 'The work text is missing.', c, True
            if getattr(reading, 'fields', None) and reading.fields.get('contents') == 'seen' and _REMOVAL.search(text):
                return 'items a former tenant left are a staff decision', c, True
            p = opinion('Would this work remove or dispose of items a former tenant left?', {'note': note['note'], 'text': text})
            if p is None:
                return 'The belongings check failed; staff must review it.', _staff(store, stream, c, now, 'The belongings check failed; staff must review it.'), True
            c = _step(store, stream, c, now, {'second_opinion': 'tenant items', 'p': round(p, 3)}, c.state, c.wake)
            if p >= BELONGINGS_CUTOFF:
                return 'items a former tenant left are a staff decision', c, True
        c = current()
        if limit(c):
            return 'The case reached its limit.', limit(c, stop=True), True
        used = sum(s.get('tool_calls', 0) for s in c.steps if isinstance(s, dict))
        if used + 1 > MAX_TOOL_CALLS:
            return 'The case reached its tool call limit.', _staff(store, stream, c, now, 'The case reached its tool call limit.'), True
        if set(args) - ALLOWED_ARGS.get(action, set()):
            return 'Unknown arguments for this action.', c, True
        action_args = dict(args)
        if action == 'hand_to_staff':        # handing work to people never needs a grant
            reason, evidence = args.get('reason'), args.get('evidence', [])
            if not isinstance(reason, str) or not reason.strip() or not isinstance(evidence, list) or not all(isinstance(x, str) for x in evidence):
                return 'A handoff needs a reason and a list of record ids.', c, True
            # a case handed to staff never leaves the unit on the rentable list: hold it first if nothing holds it yet
            if records['availability'].get('status') == 'vacant' and not records['availability'].get('holder'):
                held = authority.check(store, stream, c.id, 'hold_unit', {'unit': c.unit, 'objective': c.objective, 'reason': 'Waiting for staff'}, records, now)
                if held.level == 'alone':
                    tools.act('hold_unit', store, stream, c, now, reason='Waiting for staff: ' + reason.strip()[:200], approval_id=held.approval)
                    c = current()
            return 'Handed to staff.', tools.act(action, store, stream, c, now, reason=reason.strip()[:2000], evidence=evidence[:20]), False
        effect = {'unit': c.unit, 'objective': c.objective, **args}
        result = authority.check(store, stream, c.id, action, effect, records, now)
        if result.level == 'never':
            return 'The action is outside the available authority.', _staff(store, stream, c, now, 'The action is outside the available authority.'), True
        if result.level == 'ask':
            return 'Waiting for approval.', _step(store, stream, c, now, {'action': action, 'reason': 'Waiting for approval.'}, 'waiting',
                [{'approval': result.approval, 'action': action, 'effect': effect}]), True
        try:
            result_action = tools.act(action, store, stream, c, now, **action_args, approval_id=result.approval)
        except (KeyError, TypeError, ValueError):
            return 'Invalid action arguments.', current(), True
        c = _step(store, stream, current(), now, {'action': action, 'allowed': result_action.allowed, 'reason': result_action.check})
        if not result_action.allowed:
            return result_action.check, c, True
        after = _records(store, stream, c, now)
        if action == 'release_unit' and _ready_now(store, stream, c, after):
            c = _record(store, stream, c, now, state='done', wake=[])
        return json.dumps(planner.protect(after), default=str), c, False

    pending = next((w for w in case.wake if isinstance(w, dict) and w.get('approval')), None)
    if pending:
        records = _records(store, stream, case, now)
        request = next(e for e in store.stream_events(stream) if e.kind == 'tilden.approval.requested' and e.payload['id'] == pending['approval'])
        if authority._digest({'case': case.id, 'action': pending['action'], 'effect': pending['effect'], 'records': records}) != request.payload['fingerprint']:
            with store.write():
                store.append(stream, [Event('tilden.approval.stale', now, {'id': pending['approval']}, f"tilden:stale:{pending['approval']}", source='code')])
            case = _step(store, stream, case, now, {'reason': 'Approval refused because the records changed.'})
            result = authority.check(store, stream, case.id, pending['action'], pending['effect'], records, now, force_ask=True)
            return _step(store, stream, case, now, {'reason': 'Waiting for a new approval.'}, 'waiting', [{'approval': result.approval, 'action': pending['action'], 'effect': pending['effect']}])
        args = {k: v for k, v in pending['effect'].items() if k not in ('unit', 'objective')}
        _, case, _ = execute(pending['action'], args, pending)
        if case.state in ('waiting', 'needs staff', 'done'):
            return case
    acts = [s for s in current().steps if isinstance(s, dict) and s.get('allowed') is True]
    last_hold = max((i for i, s in enumerate(acts) if s.get('action') == 'hold_unit'), default=-1)
    last_release = max((i for i, s in enumerate(acts) if s.get('action') == 'release_unit'), default=-1)
    if last_hold < 0 or last_release > last_hold:      # a new case, or one reopened after its unit was released
        # the job text's first line is always right: a vacated unit nobody has looked at stays off the list, for every chooser
        first = _records(store, stream, current(), now)
        if first['availability'].get('status') == 'vacant' and not first['availability'].get('holder'):
            _, held, _ = execute('hold_unit', {'reason': 'Vacated: kept off the rentable list until someone looks at it.'})
            if held.state in ('waiting', 'needs staff', 'done'):
                return held
    resumed = current()
    fixed_index = max((s.get('fixed_next', 0) for s in resumed.steps if isinstance(s, dict)), default=0)
    if resumed.state == 'waiting' and any(e.id > resumed.history[-1]['id'] and
            (e.kind == 'walk.observed' and e.payload.get('unit') == resumed.unit or
             e.kind.startswith('work_order.') and (e.payload.get('unit') == resumed.unit or
             e.payload.get('order') in {o['id'] for o in _records(store, stream, resumed, now)['work_orders']}))
            for e in store.events(stream)):
        fixed_index = 0
    if current().state == 'waiting':
        _record(store, stream, current(), now, state='working', wake=[])
    if chooser == 'planner':
        return planner.run(store, stream, current(), now, client, _records(store, stream, current(), now), execute, limit)
    fixed_actions = ['link_reports', 'open_work_order', 'hold_unit', 'request_inspection', 'wait']
    while True:
        case = current()
        if limit(case):
            return limit(case, stop=True)
        records = _records(store, stream, case, now)
        if chooser == 'picker':
            choice = decide(store, stream, case.id, json.dumps(planner.protect({'records': records, 'steps': case.steps}), default=str),
                            list(dict.fromkeys(playbooks.get(case.objective)['tools'] + ['wait'])), now)
            action = choice.choice
        else:
            action = fixed_actions[fixed_index] if fixed_index < len(fixed_actions) else 'wait'
            fixed_index += 1
            _step(store, stream, current(), now, {'fixed_next': fixed_index})
            if _ready_now(store, stream, case, records):
                action = 'release_unit'
        execute('decision', {})
        if not action or action == 'wait':
            return _record(store, stream, current(), now, state='waiting', wake=[{'work_orders': True}])
        if action == 'hand_to_staff':
            return execute('hand_to_staff', {'reason': 'The case needs staff review.', 'evidence': []})[1]
        if chooser == 'fixed' and action == 'open_work_order':
            note = next((n for n in reversed(records['walk_notes']) if n.get('note')), None)
            if note and any(e.kind == 'work_order.reported' and e.payload.get('answers_record') == note['id'] for e in store.events(stream)):
                continue
        try:
            args, cost = _args(action, records, readers, store, stream, current())
        except Exception:
            return _staff(store, stream, current(), now, 'The records could not be read safely.')
        if cost:
            _step(store, stream, current(), now, {'reader_cents': cost})
        if action == 'open_work_order' and args:
            args['answers_record'] = next((n['id'] for n in reversed(records['walk_notes']) if n.get('note')), '')
        if args is None:
            if chooser == 'fixed':
                continue
            return _staff(store, stream, current(), now, 'The records do not identify the work needed for this action.')
        result, case, error = execute(action, args)
        if case.state in ('waiting', 'needs staff', 'done'):
            return case
        if error:
            return _step(store, stream, case, now, {'reason': result}, 'waiting', [{'work_orders': True}])

def sweep(store, stream, now, decide=None, readers=None, *, chooser=None, client=None, check=None):
    events = store.events(stream)
    latest = {}
    for event in events:
        if (event.kind == 'tilden.authority.granted' and event.at <= now
                and event.payload['action'] in playbooks.get('make-unit-ready')['tools']):
            latest[event.payload['action']] = event
    grants = [e for e in latest.values() if datetime.fromisoformat(e.payload['expires']) > now]
    if grants:
        earliest = min(e.at for e in grants)
        books, rent = renting.load(store, stream)
        for event in events:
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
            # one case per unit at a time: a later note about a unit already being worked on wakes that case instead
            if unit in books.units and not any(c.unit == unit for c in cases.open_cases(store, stream)):
                finished = [c for c in cases.load(store, stream).values() if c.unit == unit and c.state == 'done']
                if event.kind == 'walk.observed' and finished and finished[-1].history[-1]['id'] < event.id:
                    # a note after the unit was released reopens its case without a new hold: the worker reads the note and holds
                    # the unit again only if the note shows a problem
                    _step(store, stream, finished[-1], now, {'reason': 'A staff note arrived after the unit was released.'}, 'working', [])
                elif not (event.kind == 'walk.observed' and finished):
                    cases.open_case(store, stream, event, unit, now)
    for case in cases.open_cases(store, stream):
        if case.state == 'needs staff':
            # staff answer a handoff through the records: a new staff note or work order change on the unit picks the case up again
            last = case.history[-1]['id']
            _, current = renting.load(store, stream)
            unit_orders = {o.id for o in current.floor.orders.values() if o.unit == case.unit}
            if not any(e.id > last and (str(e.source or '').startswith('staff:') or str(e.payload.get('by') or '').startswith('staff:'))
                       and ((e.kind == 'walk.observed' and e.payload.get('unit') == case.unit)
                            or (e.kind.startswith('work_order.') and (e.payload.get('order') in unit_orders or e.payload.get('unit') == case.unit)))
                       for e in events):
                continue
            case = _step(store, stream, case, now, {'reason': 'Staff added records after the handoff; the case is picked up again.'}, 'working', [])
        if case.state == 'waiting':
            last = case.history[-1]['id']
            wake = case.wake
            _, current = renting.load(store, stream)
            unit_orders = {o.id for o in current.floor.orders.values() if o.unit == case.unit}
            if not any((w.get('approval') and any(e.kind == 'tilden.approval.approved' and e.payload['id'] == w['approval'] for e in events))
                       or ((w.get('work_orders') or w.get('record') == 'work_orders') and any(e.kind == 'work_order.closed' and e.payload.get('order') in unit_orders and e.id > last for e in events))
                       or (w.get('record') and w.get('record') != 'work_orders' and any(e.id > last and w['record'] in (e.kind, str(e.payload.get('order', '')), str(e.payload.get('observation', ''))) for e in events))
                       for w in wake if isinstance(w, dict)):
                # a wait the planner named loosely must not strand the case: any new note or work order change on the unit wakes it
                if not any(e.id > last and ((e.kind == 'walk.observed' and e.payload.get('unit') == case.unit)
                                            or (e.kind.startswith('work_order.') and (e.payload.get('order') in unit_orders or e.payload.get('unit') == case.unit)))
                           for e in events):
                    continue
        if case.state in ('open', 'working', 'waiting'):
            try:
                run_case(store, stream, case.id, now, decide, readers, chooser=chooser, client=client, check=check)
            except Exception as exc:      # one broken case goes to staff; the sweep goes on with the others
                _staff(store, stream, cases.load(store, stream)[case.id], now, f'The case stopped on an error ({type(exc).__name__}); staff review it.')
    return cases.load(store, stream)
