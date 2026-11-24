"""Run the unit readiness playbook against the copy's records."""
import json
import os
from datetime import datetime
from functools import partial
from itertools import combinations

from core import renting, walk, walk_reading
from core.store import Event
from core.tilden import authority, cases, decide as decisions, gate, planner, playbooks, tools

# Bound each wake so one case cannot occupy the worker indefinitely.
MAX_DECISIONS = 16
MAX_TOOL_CALLS = 40
CASE_CENTS = 25
DAILY_CENTS = 100


def _record(store, stream, case, now, **changes):
    return cases.record(store, stream, case.id, 'updated', changes, now, source='agent:tilden')


def _step(store, stream, case, now, entry, state='working', wake=None):
    return _record(store, stream, case, now, steps=case.steps + [entry], state=state, wake=wake or [])


def _staff(store, stream, case, now, reason):
    return tools.act('hand_to_staff', store, stream, case, now, question=reason, record_ids=[], role='front_desk', wake_evidence='staff note or work order change')


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


_ready_now = gate._ready_now
_latest_look = gate._latest_look
SETTLED_CUTOFF = gate.SETTLED_CUTOFF
READY_CUTOFF = gate.READY_CUTOFF
SAME_FAULT_CUTOFF = gate.SAME_FAULT_CUTOFF
BELONGINGS_CUTOFF = gate.BELONGINGS_CUTOFF


def _spend(store, stream, c, now):
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
    return case_cost, day_cost


def run_case(store, stream, case_id, now, readers=None, *, chooser=None, client=None, check=None):
    case = cases.load(store, stream)[case_id]
    if case.state in ('done', 'needs staff', 'blocked', 'paused'):
        return case
    chooser = chooser or 'planner'
    if chooser not in ('planner', 'fixed', 'hybrid'):
        raise ValueError('unknown chooser')
    check = check or decisions.check
    readers = readers or {'read_note': partial(walk_reading.read_note, client,
        os.getenv('TILDEN_PLANNER_MODEL', 'claude-haiku-5-5'), os.getenv('TILDEN_PLANNER_EFFORT', 'medium')),
        'same_fault': partial(walk_reading.same_fault, client, os.getenv('TILDEN_PLANNER_MODEL', 'claude-haiku-5-5'),
                              os.getenv('TILDEN_PLANNER_EFFORT', 'medium'))}

    def current():
        return cases.load(store, stream)[case_id]

    def limit(c, stop=False, reserve=0, keep=None):
        entries = [s for s in c.steps if isinstance(s, dict)]
        case_cost, day_cost = _spend(store, stream, c, now)
        start = max((i for i, entry in enumerate(entries) if entry.get('wake_start')), default=0)
        wake_entries = entries[start:]
        exceeded = ((chooser in ('fixed', 'planner') and (sum(s.get('decisions', 0) for s in wake_entries) >= MAX_DECISIONS or
                    sum(s.get('tool_calls', 0) for s in wake_entries) >= MAX_TOOL_CALLS)) or case_cost + reserve >= CASE_CENTS or day_cost + reserve >= DAILY_CENTS)
        return _step(store, stream, c, now, {'reason': 'The case reached its decision, tool, or spending limit.', **(keep or {})}, 'paused') if stop else exceeded

    def execute(action, args, pending=None):
        return _execute(action, args, pending)

    routine = chooser in ('hybrid', 'planner')
    seen = []       # the planner's records, marked judged once its first model call has been made

    def unjudged():
        """Staff records on the unit the model has not judged yet; the same list feeds the per-record turns and the release gate."""
        c = current()
        judged = {s['judged_event'] for s in c.steps if isinstance(s, dict) and 'judged_event' in s}
        # Event ids distinguish a report from later words on the same order.
        found = [e for e in _unit_evidence(store, stream, c, c.source_event - 1, True)
                 if e.at <= now and
                 ((e.kind in ('walk.observed', 'walk.observation.corrected') and e.payload.get('note')) or
                  (e.kind == 'work_order.reported' and e.payload.get('problem')) or
                  (e.kind == 'work_order.closed' and e.payload.get('text')))]
        # A check order can be reported and closed with the same staff words: one record, judged once (through its close).
        duplicates = {e.id for e in found if e.kind == 'work_order.reported' and e.payload.get('kind') == 'check'
                      and any(o.kind == 'work_order.closed' and o.payload.get('order') == e.payload.get('order')
                              and o.payload.get('text') == e.payload.get('problem') and o.at == e.at for o in found)}
        return [e for e in found if e.id not in judged and e.id not in duplicates]

    def _execute(action, args, pending=None):
        c = current()
        if action == 'decision':
            c = _step(store, stream, c, now, {'decisions': 1})
            if chooser == 'planner':
                # the planner's one pass reads every record in its first message: a model call that was made has judged what existed then
                for event in seen:
                    c = _step(store, stream, c, now, {'judged_event': event.id}, c.state, c.wake)
                seen.clear()
            return '', c, False
        if action == 'staff':
            return args['reason'], _step(store, stream, c, now, {'reason': args['reason']}, 'blocked'), False
        if not routine and limit(c):
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
        holder = _records(store, stream, c, now)['availability']['holder'] if action == 'release_unit' else None
        staff_block = holder and holder[0] == 'block' and not any(
            e.kind == 'unit.blocked' and e.payload['block'] == holder[1] and e.payload.get('case_id') == c.id
            for e in store.events(stream))
        if action == 'release_unit' and not staff_block and chooser in ('hybrid', 'planner') and unjudged():
            content = 'Release refused: a staff record on this unit has not been judged yet.'
            return content, _step(store, stream, c, now, {'action': action, 'refused': content}, c.state, c.wake), True
        return gate.act(store, stream, case_id, action, args, now, check=check, readers=readers)

    just_settled = bool(case.steps and case.steps[-1].get('settled_question'))
    previous_event = case.history[-1]['id']
    prior = case.steps[-1] if case.steps and isinstance(case.steps[-1], dict) else {}
    rewind = bool(prior.get('resumed_wake'))       # a saved wake picked up again starts the fixed order over, as the wake it replaces would have
    case = _step(store, stream, case, now, {'wake_start': True, 'tool_calls': 1}, case.state, case.wake)
    if chooser == 'fixed' and limit(case):
        keep = None
        if _spend(store, stream, case, now)[0] < CASE_CENTS:
            # the daily budget renews, so the records that woke the case are kept for the first sweep that can use them;
            # a case at its lifetime cap stays paused with nothing saved
            woke = {*prior.get('evidence_ids', []), *(e.id for e in _unit_evidence(store, stream, case, previous_event, True) if e.at <= now)}
            keep = {'pending_evidence': sorted(woke)} if woke else None
        return limit(case, stop=True, keep=keep)

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
        if case.state in ('waiting', 'needs staff', 'paused', 'blocked', 'done'):
            return case
    acts = [s for s in current().steps if isinstance(s, dict) and s.get('allowed') is True]
    last_hold = max((i for i, s in enumerate(acts) if s.get('action') == 'hold_unit'), default=-1)
    last_release = max((i for i, s in enumerate(acts) if s.get('action') == 'release_unit'), default=-1)
    if last_hold < 0 or last_release > last_hold:      # a new case, or one reopened after its unit was released
        # the job text's first line is always right: a vacated unit nobody has looked at stays off the list, for every chooser
        first = _records(store, stream, current(), now)
        if first['availability'].get('status') == 'vacant' and not first['availability'].get('holder'):
            _, held, _ = execute('hold_unit', {'reason': 'Vacated: kept off the rentable list until someone looks at it.'})
            if held.state in ('waiting', 'needs staff', 'paused', 'blocked', 'done'):
                return held
    if chooser == 'fixed' and just_settled and _ready_now(store, stream, current(), _records(store, stream, current(), now)):
        _, result, _ = execute('release_unit', {})
        if result.state in ('waiting', 'needs staff', 'paused', 'blocked', 'done'):
            return result
    resumed = current()
    fixed_index = max((s.get('fixed_next', 0) for s in resumed.steps if isinstance(s, dict)), default=0)
    if resumed.state == 'waiting' and any(e.id > previous_event and
            (e.kind == 'walk.observed' and e.payload.get('unit') == resumed.unit or
             e.kind.startswith('work_order.') and (e.payload.get('unit') == resumed.unit or
             e.payload.get('order') in {o['id'] for o in _records(store, stream, resumed, now)['work_orders']}))
            for e in store.events(stream)):
        fixed_index = 0
    if rewind:
        fixed_index = 0
    if current().state == 'waiting':
        _record(store, stream, current(), now, state='working', wake=[])
    if chooser in ('hybrid', 'planner'):
        routine = False
        evidence = unjudged()
        capped = False

        def record_execute(action, args):
            if action not in ('decision', 'staff') and action not in {tool['name'] for tool in planner.HYBRID_TOOLS}:
                return 'The action is not available.', current(), True
            return execute(action, args)

        if chooser == 'planner':
            # the planner reads the whole case and picks any tool; a handoff or a wait ends its pass, never the case's routine steps
            seen[:] = evidence
            result = planner.run(store, stream, current(), now, client, _records(store, stream, current(), now), execute, limit)
            if result.state == 'blocked':
                return result
            if result.state == 'paused':
                capped = True
            elif result.state == 'done' or any(isinstance(w, dict) and w.get('approval') for w in result.wake):
                return result
            elif result.state == 'needs staff':
                _record(store, stream, result, now, state='working', wake=[])
        for event in ([] if chooser == 'planner' else evidence):
            if limit(current()):
                capped = True
                break
            # one model turn for this record, at most 2 tool calls; the wake has no shared decision or tool budget
            result = planner.run(store, stream, current(), now, client, _records(store, stream, current(), now),
                                 record_execute, limit, record={'id': event.payload.get('observation') or event.payload.get('order'),
                                                        'event': event.id, 'kind': event.kind, 'words': event.payload.get('note') or event.payload.get('problem') or event.payload.get('text')})
            if result.state == 'blocked':
                return result
            if result.state == 'paused':
                capped = True
                break
            result = _step(store, stream, result, now, {'judged_event': event.id}, result.state, result.wake)
            if result.state == 'needs staff':
                _record(store, stream, result, now, state='working', wake=[])
            if result.state == 'done' or any(isinstance(w, dict) and w.get('approval') for w in result.wake):
                return result
        routine = True
        if current().state == 'paused':
            _record(store, stream, current(), now, state='working', wake=[])
        # the model finishing a record by waiting must not stop the routine steps code owns; only its own wait is cleared
        if current().state == 'waiting' and all(isinstance(w, dict) and 'record' in w and not w.get('approval') for w in current().wake):
            _record(store, stream, current(), now, state='working', wake=[])

        records = _records(store, stream, current(), now)
        orders = [o for o in records['work_orders'] if o['state'] != 'closed' and o['kind'] != 'check' and not o['joined_into']]
        for first, second in combinations(orders, 2):
            active = {o['id'] for o in _records(store, stream, current(), now)['work_orders'] if o['state'] != 'closed' and not o['joined_into']}
            if first['id'] not in active or second['id'] not in active:
                continue
            _, result, _ = execute('link_reports', {'order': second['id'], 'into': first['id']})
            if result.state in ('waiting', 'needs staff', 'paused', 'blocked', 'done'):
                return result
        records = _records(store, stream, current(), now)
        for order in records['work_orders']:
            if order['state'] != 'closed' and order['kind'] != 'check' and not order['joined_into']:
                _, result, _ = execute('close_report_no_work', {'order': order['id']})
                if result.state in ('waiting', 'needs staff', 'paused', 'blocked', 'done'):
                    return result
        records = _records(store, stream, current(), now)
        if not capped and _ready_now(store, stream, current(), records):
            _, result, _ = execute('release_unit', {})
            if result.state in ('waiting', 'needs staff', 'paused', 'blocked', 'done'):
                return result
        else:
            repairs = [o for o in records['work_orders'] if o['kind'] != 'check' and not o['joined_into']]
            checks = [o for o in records['work_orders'] if o['kind'] == 'check' and o['state'] != 'closed']
            if all(o['state'] == 'closed' for o in repairs) and not checks and _latest_look(store, stream, current(), records) is None:
                _, result, _ = execute('request_inspection', {})
                if result.state in ('waiting', 'needs staff', 'paused', 'blocked', 'done'):
                    return result
        steps = current().steps
        last_hand = max((i for i, s in enumerate(steps) if s.get('handoff')), default=-1)
        last_settled = max((i for i, s in enumerate(steps) if s.get('settled_question')), default=-1)
        if capped:
            return _step(store, stream, current(), now, {'reason': 'The case reached its decision, tool, or spending limit.',
                'pending_handoff': last_hand > last_settled,
                'pending_evidence': [e.id for e in evidence if e.id not in {s.get('judged_event') for s in steps if isinstance(s, dict)}]}, 'paused')
        return _record(store, stream, current(), now, state='needs staff' if last_hand > last_settled else 'waiting', wake=[{'work_orders': True}])
    records = _records(store, stream, current(), now)
    for order in records['work_orders']:
        if order['state'] != 'closed' and order['kind'] != 'check' and not order['joined_into']:
            _, result, _ = execute('close_report_no_work', {'order': order['id']})
            if result.state in ('waiting', 'needs staff', 'paused', 'blocked', 'done'):
                return result
    fixed_actions = ['link_reports', 'open_work_order', 'hold_unit', 'request_inspection', 'wait']
    while True:
        case = current()
        if limit(case):
            return limit(case, stop=True)
        records = _records(store, stream, case, now)
        action = fixed_actions[fixed_index] if fixed_index < len(fixed_actions) else 'wait'
        fixed_index += 1
        _step(store, stream, current(), now, {'fixed_next': fixed_index})
        if _ready_now(store, stream, case, records):
            action = 'release_unit'
        execute('decision', {})
        if not action or action == 'wait':
            return _record(store, stream, current(), now, state='waiting', wake=[{'work_orders': True}])
        if chooser == 'fixed' and action == 'open_work_order':
            note = next((n for n in reversed(records['walk_notes']) if n.get('note')), None)
            if note and any(e.kind == 'work_order.reported' and e.payload.get('answers_record') == note['id'] for e in store.events(stream)):
                continue
        try:
            args, cost = _args(action, records, readers, store, stream, current())
        except Exception:
            return _step(store, stream, current(), now, {'reason': 'The records could not be read safely.'}, 'blocked')
        if cost:
            _step(store, stream, current(), now, {'reader_cents': cost})
        if action == 'open_work_order' and args:
            args['answers_record'] = next((n['id'] for n in reversed(records['walk_notes']) if n.get('note')), '')
        if args is None:
            if chooser == 'fixed':
                continue
            return _staff(store, stream, current(), now, 'The records do not identify the work needed for this action.')
        result, case, error = execute(action, args)
        if case.state in ('waiting', 'needs staff', 'paused', 'blocked', 'done'):
            return case
        if error:
            return _step(store, stream, case, now, {'reason': result}, 'waiting', [{'work_orders': True}])

_unit_evidence = gate._unit_evidence


def sweep(store, stream, now, readers=None, *, chooser=None, client=None, check=None):
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
        if case.state in ('needs staff', 'paused'):
            pending_handoff = case.state == 'needs staff' or bool(case.steps and case.steps[-1].get('pending_handoff'))
            pending_evidence = case.steps[-1].get('pending_evidence', []) if case.steps else []
            after = case.history[-1]['id']
            if pending_handoff:
                after = next(h['id'] for h in reversed(case.history) if h['payload'].get('state') == 'needs staff')
            evidence = _unit_evidence(store, stream, case, after, pending_handoff)
            resumed_wake = False
            if pending_evidence:
                case_cost, day_cost = _spend(store, stream, case, now)
                # the saved wake is reopened only when the budget can pay for it; until then the evidence stays saved
                if case_cost < CASE_CENTS and day_cost < DAILY_CENTS:
                    saved = [e for e in _unit_evidence(store, stream, case, case.source_event - 1, True)
                             if e.id in pending_evidence and e.at <= now and e not in evidence]
                    evidence += saved
                    resumed_wake = bool(saved)
            if not evidence:
                continue
            if pending_handoff:
                handoff = next((s['handoff'] for s in reversed(case.steps) if isinstance(s, dict) and 'handoff' in s), None)
                if handoff:
                    handoff_event = gate._handoff_event(case, handoff['question'])
                    evidence = [e for e in _unit_evidence(store, stream, case, handoff_event, True) if e.at <= now]
                    case_cost, day_cost = _spend(store, stream, case, now)
                    wake = handoff['wake_evidence']
                    woke = [e for e in evidence if wake == 'staff note or work order change' or (wake == 'staff note' and e.kind in ('walk.observed', 'walk.observation.corrected')) or (wake == 'work order change' and e.kind.startswith('work_order.'))]
                    if not woke and chooser not in ('hybrid', 'planner', None):
                        continue
                    # the wake evidence decides when the case wakes; every staff record since the handoff can settle the question
                    if case_cost >= CASE_CENTS or day_cost >= DAILY_CENTS:
                        # over the cap the case pauses with its handoff still open; it is not picked up on a guess
                        _step(store, stream, case, now, {'reason': 'The case reached its spending limit.', 'pending_handoff': True}, 'paused')
                        continue
                    checker = check or decisions.check
                    answer = None
                    try:
                        answer = checker(store, stream, case.id, 'predicate',
                            'Do the new staff records settle the question so routine work can continue?',
                            json.dumps(planner.protect({'question': handoff['question'], 'records': [e.payload for e in evidence]}), default=str), now)
                        probability = answer.probabilities.get('yes')
                    except Exception:
                        probability = None
                    if (getattr(answer, 'reason', None) or isinstance(probability, bool) or
                            not isinstance(probability, (int, float)) or not 0 <= probability <= 1):
                        if case_cost < CASE_CENTS and day_cost < DAILY_CENTS:
                            _step(store, stream, case, now, {'reason': 'The handoff settlement check failed.'}, 'blocked')
                            continue
                        probability = 0
                    ids = [e.id for e in evidence]
                    if probability < SETTLED_CUTOFF:
                        case = _step(store, stream, case, now, {'unsettled_question': handoff['question'], 'evidence_ids': ids, 'p': probability}, case.state, case.wake)
            entry = {'reason': 'New records picked up again the case.', 'evidence_ids': [e.id for e in evidence]}
            if resumed_wake:
                entry['resumed_wake'] = True
            if pending_handoff and handoff and probability >= SETTLED_CUTOFF:
                entry.update({'settled_question': handoff['question'], 'p': probability})
            case = _step(store, stream, case, now, entry, 'working', [])
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
                if not any(e.id > last and ((e.kind in ('walk.observed', 'walk.observation.corrected') and e.payload.get('unit') == case.unit)
                                            or (e.kind.startswith('work_order.') and (e.payload.get('order') in unit_orders or e.payload.get('unit') == case.unit)))
                           for e in events):
                    continue
        if case.state in ('open', 'working', 'waiting'):
            try:
                run_case(store, stream, case.id, now, readers, chooser=chooser, client=client, check=check)
            except Exception as exc:      # one broken case goes to staff; the sweep goes on with the others
                _step(store, stream, cases.load(store, stream)[case.id], now, {'reason': f'The case stopped on an error ({type(exc).__name__}).'}, 'blocked')
    return cases.load(store, stream)
