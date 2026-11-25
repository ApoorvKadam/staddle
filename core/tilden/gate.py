"""The one gate every worker's actions go through."""
import json
import re
from dataclasses import asdict, is_dataclass

from core import renting, walk
from core.gate import STAFF_ROLES
from core.tilden import authority, cases, decide as decisions, playbooks, tools


def _record(store, stream, case, now, **changes):
    return cases.record(store, stream, case.id, 'updated', changes, now, source='agent:tilden')


def _step(store, stream, case, now, entry, state='working', wake=None):
    return _record(store, stream, case, now, steps=case.steps + [entry], state=state, wake=wake or [])


def _staff(store, stream, case, now, reason):
    return tools.act('hand_to_staff', store, stream, case, now, question=reason, record_ids=[], role='front_desk', wake_evidence='staff note or work order change')


def _records(store, stream, case, now):
    return json.loads(json.dumps({name: tools.read(name, store, stream, case, now)
                                  for name in playbooks.get(case.objective)['reads']}, default=str))


def protect(value, ident='case'):
    if isinstance(value, dict):
        ident = str(value.get('id', ident)).replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;')
        return {k: (f'<record id="{ident}">{v.replace("<", "&lt;")}</record>'
                    if isinstance(v, str) and k in ('note', 'text', 'problem', 'reason', 'what', 'words', 'summary', 'contradiction', 'latest_look', 'question', 'open_question') else ({q: f'<record id="{ident}">{str(w).replace("<", "&lt;")}</record>' for q, w in v.items()}
                          if k == 'quotes' and isinstance(v, dict) else protect(v, ident)))
                for k, v in value.items()}
    if isinstance(value, list):     # strings in a list are people's words too (a repair's finishing words)
        return [f'<record id="{ident}">{v.replace("<", "&lt;")}</record>' if isinstance(v, str) else protect(v, ident) for v in value]
    return value


# Second-check cut-offs, set on practice cases only and frozen with the workers before a graded set.
# Release check, calibrated 2026-11-24 on 174 practice looks (44 ready, 130 not): every cut-off from 0.70 to 0.80 passed all ready
# looks and no other, so 0.75 sits in the middle of that band.
READY_CUTOFF = 0.75
# Handoff check, calibrated 2026-11-24 on 205 practice entries (24 needing a person): at 0.5 it passes 22 of 24 and 50 of 181 routine
# entries; higher cut-offs lose real cases about as fast as routine ones, and a handoff no longer stops routine steps, so 0.5 stays.
HANDOFF_CUTOFF = 0.5
# No-work check, calibrated 2026-11-24 on 24 practice report and look pairs (4 needing no work): from 0.50 to 0.85 it closed 3 of 4
# and never a report that needed work. A small sample; on the live copy a close also asks staff first.
NO_WORK_CUTOFF = 0.75
# Same-fault check, calibrated 2026-11-24 on the spent operations set's 20 report pairs: from 0.70 to 0.80 it linked all 7 one-fault
# pairs and no two-fault or unsure pair (the highest unsure pair scored 0.68).
SAME_FAULT_CUTOFF = 0.75
# Settled check, calibrated 2026-11-24 on 63 practice records after a handoff: from 0.50 to 0.90 it settled all 8 labelled cases and
# 3 more one entry before release, where the matter was likely settled while other work remained; release still needs the ready test.
SETTLED_CUTOFF = 0.75
# Tenant-items check, calibrated 2026-11-24 on the operations set's notes: it refused every order removing physical items left behind
# (6 of 6, at 0.96 or more) and passed every clean-only order and every money item (fees, credits).
BELONGINGS_CUTOFF = 0.5
# Not calibrated: whether an open staff question blocks renting. That check fails closed.
BLOCKING_CUTOFF = 0.5
_REMOVAL = re.compile(r"\b(remove|removal|dispose|disposal|discard|throw|thrown|clear|clearing|haul|dump|take away|take out)\w*\b", re.I)


def _look_cutoff(store, stream, case, records):
    repairs = [o for o in records['work_orders'] if o['kind'] != 'check' and not o['joined_into']]
    books, _ = renting.load(store, stream)
    events = store.events(stream)
    repair_ids = {o['id'] for o in repairs}
    cutoffs = [(e.at, e.id) for e in events if
               (e.kind == 'work_order.closed' and e.payload.get('order') in repair_ids and not e.payload.get('no_work')) or
               (e.kind == 'lease.ended' and (e.payload.get('unit') == case.unit or
                (e.payload.get('lease') in books.leases and books.leases[e.payload['lease']].unit == case.unit)))]
    return max(cutoffs, default=None)


def _latest_look(store, stream, case, records):
    events = store.events(stream)
    cutoff = _look_cutoff(store, stream, case, records)
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
ALLOWED_ARGS = {'close_report_no_work': {'order'}, 'open_work_order': {'text', 'answers_record'}, 'link_reports': {'order', 'into'}, 'hold_unit': {'reason'},
                'release_unit': set(), 'request_inspection': {'answers_record'}, 'hand_to_staff': {'question', 'record_ids', 'role', 'wake_evidence'}}


def _handoff_event(case, question):
    """The case record written when this question was handed to staff. Every step recorded while the case waits on staff also
    carries the state 'needs staff', so the newest such record is not the handoff: anchoring there dropped earlier staff rulings."""
    def asked(steps):
        return sum(1 for s in steps or [] if isinstance(s, dict) and isinstance(s.get('handoff'), dict) and s['handoff'].get('question') == question)
    count = asked(case.steps)
    found = next((h['id'] for h in case.history if count and asked(h['payload'].get('steps')) >= count), None)
    return found if found is not None else next((h['id'] for h in reversed(case.history) if h['payload'].get('state') == 'needs staff'), None)


def _ready_now(store, stream, case, records):
    """The shared job text's ready test from the records alone: every repair order is closed, nothing else on the records blocks the
    unit, and someone looked at it after the last repair (or after the move-out when nothing was repaired). At release the look's own
    words also get a second opinion; this test never reads them."""
    repairs = [o for o in records['work_orders'] if o['kind'] != 'check' and not o['joined_into']]
    look = _latest_look(store, stream, case, records)
    cutoff = _look_cutoff(store, stream, case, records)
    inspections = [e for e in store.events(stream) if e.kind == 'walk.observed'
                   and e.payload.get('unit') == case.unit and e.payload.get('verdict') in ('ready', 'not ready')
                   and cutoff is not None and (e.at, e.id) > cutoff]
    latest = max(inspections, key=lambda e: (e.at, e.id), default=None)
    negative = bool(latest and latest.payload['verdict'] == 'not ready')
    return (all(o['state'] == 'closed' for o in repairs) and not records['readiness']['reasons']
            and look is not None and not negative)


def _unit_evidence(store, stream, case, after, staff_only=False):
    _, current = renting.load(store, stream)
    orders = {o.id for o in current.floor.orders.values() if o.unit == case.unit}
    return [e for e in store.events(stream) if e.id > after
            and (not staff_only or str(e.source or '').startswith('staff:') or str(e.payload.get('by') or '').startswith('staff:'))
            and ((e.kind in ('walk.observed', 'walk.observation.corrected') and e.payload.get('unit') == case.unit)
                 or (e.kind.startswith('work_order.') and (e.payload.get('order') in orders or e.payload.get('unit') == case.unit)))]


def act(store, stream, case_id, action, args, now, *, check=None, readers=None):
    check = check or decisions.check
    readers = readers or {}

    def current():
        return cases.load(store, stream)[case_id]

    def opinion(question, value):
        try:
            result = check(store, stream, case_id, 'predicate', question, json.dumps(protect(value), default=str), now)
            probability = result.probabilities.get('yes')
            if result.reason or probability is None or isinstance(probability, bool) or not isinstance(probability, (float, int)) or not 0 <= probability <= 1:
                return None
            return probability
        except Exception:
            return None

    def execute():
        c = current()
        records = _records(store, stream, c, now)
        if action in playbooks.get(c.objective)['reads']:
            return json.dumps(protect(records[action]), default=str), c, False
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
                return 'read_note reads staff walk notes only; read an inspection or report through work_orders.', c, True
            try:
                reading = readers['read_note'](note['note'], (c.unit,), c.unit)
                cost = getattr(getattr(reading, 'call', None), 'cents', 0) or 0
                if cost:
                    c = _step(store, stream, c, now, {'reader_cents': cost})
                fields = getattr(reading, 'fields', None) or {}
                # the walk reader refuses a note that says nothing about lock, tag, door or contents; only that refusal is information.
                # Any other refusal (instructions in the words, another unit, a failed read) still stops.
                plain = bool(reading.refusal) and not getattr(reading, 'flags', None) and fields and all(v == 'unknown' for v in fields.values())
                if reading.refusal and not plain:
                    return 'The note could not be read safely.', c, True
                if plain and action == 'read_note':
                    return json.dumps(protect({'id': note['id'], 'reader': reading.refusal, 'contents': 'unknown', 'work': []}, note['id'])), c, False
                if plain:
                    reading = None      # nothing structured; the removal-word rule below treats the contents as possibly the tenant's
            except Exception:
                return 'The note could not be read safely.', c, True
            if action == 'read_note':
                data = asdict(reading) if is_dataclass(reading) else vars(reading)
                return json.dumps(protect(data, note['id']), default=str), c, False
        if action not in playbooks.get(c.objective)['tools']:
            return 'The action is outside this playbook.', c, True
        if action == 'release_unit':
            holder = records['availability']['holder']
            if holder and holder[0] == 'block' and not any(e.kind == 'unit.blocked' and e.payload['block'] == holder[1] and e.payload.get('case_id') == c.id for e in store.events(stream)):
                reason = 'Staff placed the unit block, so staff must release it.'
                return reason, _staff(store, stream, c, now, reason), True
            asked = [s['handoff']['question'] for s in c.steps if isinstance(s, dict) and isinstance(s.get('handoff'), dict)]
            settled = {s.get('settled_question') for s in c.steps if isinstance(s, dict) and s.get('settled_question')}
            open_question = next((q for q in reversed(asked) if q not in settled), None)
            if open_question:
                # some staff questions do not stop a rental (a damage charge, a money dispute); some do, and those must be settled first
                p = opinion('A storage unit has an open question handed to staff. Must it be settled before the unit is offered to a new renter '
                            '(a former tenant\'s belongings, safety, pests, anything legal, or words too unclear to act on), rather than a matter '
                            'staff can settle after the unit is rented again (such as a damage charge or a dispute about money)?',
                            {'id': c.id, 'open_question': open_question})
                if p is not None:
                    c = _step(store, stream, c, now, {'second_opinion': 'blocks a rental', 'p': round(p, 3)}, c.state, c.wake)
                if p is None or p >= BLOCKING_CUTOFF:
                    # a blocking question can still be settled by what staff wrote; judge that here so a settled matter never blocks
                    # only staff records written after the handoff, in time order, can settle it (the same evidence the wake uses)
                    handoff_event = _handoff_event(c, open_question)
                    later = [] if handoff_event is None else sorted(
                        (e for e in _unit_evidence(store, stream, c, handoff_event, True) if e.at <= now), key=lambda e: (e.at, e.id))
                    staff_words = [e.payload.get('note') or e.payload.get('text') or e.payload.get('problem') for e in later]
                    staff_words = [w for w in staff_words if w]
                    if not staff_words:
                        return 'Release refused: an open staff question must be settled before the unit is offered.', c, True
                    s = opinion('A storage unit has an open question handed to staff. Do the staff records, latest last, settle it, so the '
                                'unit can be offered?', {'id': c.id, 'open_question': open_question, 'staff_records': staff_words})
                    if s is None or s < SETTLED_CUTOFF:
                        return 'Release refused: an open staff question must be settled before the unit is offered.', c, True
                    c = _step(store, stream, c, now, {'settled_question': open_question, 'p': round(s, 3)}, c.state, c.wake)
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
                return 'The readiness check failed; staff must review it.', _step(store, stream, c, now, {'reason': 'The readiness check failed.'}, 'blocked'), True
            c = _step(store, stream, c, now, {'second_opinion': 'ready', 'look': look[1], 'p': round(p, 3)}, c.state, c.wake)
            if p < READY_CUTOFF:
                return 'Release refused: a second check found the latest look does not show the unit is ready.', c, True
        if action == 'close_report_no_work':
            order = next((o for o in records['work_orders'] if o['id'] == args.get('order')), None)
            if not order or order['state'] == 'closed' or order['kind'] == 'check' or order['joined_into']:
                return 'An open repair report is required.', c, True
            _, floor = walk.load(store, stream)
            if any(order['id'] in pair for pair in floor.apart):
                return 'Staff kept this report apart from another.', c, True
            report = next((e for e in store.events(stream) if e.kind == 'work_order.reported' and e.payload.get('order') == order['id']), None)
            notes = {n['id']: n for n in records['walk_notes'] if n.get('note') and not n.get('superseded_by') and str(n.get('by', '')).startswith('staff:')}
            checks = {o['id'] for o in records['work_orders'] if o['kind'] == 'check'}
            evidence = []
            for e in store.events(stream):
                if not report or (e.at, e.id) <= (report.at, report.id) or e.at > now:
                    continue
                if e.kind in ('walk.observed', 'walk.observation.corrected') and e.payload.get('observation') in notes:
                    n = notes[e.payload['observation']]
                    evidence.append((e.at, e.id, n['id'], n['note']))
                elif e.kind in ('work_order.recorded', 'work_order.checked', 'work_order.closed') and e.payload.get('order') in checks and str(e.payload.get('by', '')).startswith('staff:') and e.payload.get('text'):
                    evidence.append((e.at, e.id, e.payload['order'], e.payload['text']))
            if not evidence:
                return 'A later staff record is required.', c, True
            look = None
            for candidate in sorted(evidence, reverse=True):
                p = opinion('Does the later staff record show that the reported problem needs no work?', {'problem': order['problem'], 'text': candidate[3], 'later_staff_records': [item[3] for item in sorted(evidence) if item[:2] > candidate[:2]]})
                if p is None:
                    return 'The no-work check could not answer.', c, True
                c = _step(store, stream, c, now, {'second_opinion': 'no work needed', 'record': candidate[2], 'p': round(p, 3)}, c.state, c.wake)
                if p >= NO_WORK_CUTOFF:
                    look = candidate
                    break
            if look is None:
                return 'The later record does not establish that no work is needed.', c, True
        if action == 'link_reports':
            orders = [o for o in records['work_orders'] if o['id'] in (args.get('order'), args.get('into'))]
            if len(orders) != 2 or args.get('order') == args.get('into'):
                return 'The two reports were not found.', c, True
            p = opinion('Do these two reports describe one fault?', orders)
            if p is None:
                return 'The fault check failed; staff must review it.', _step(store, stream, c, now, {'reason': 'The fault check failed.'}, 'blocked'), True
            c = _step(store, stream, c, now, {'second_opinion': 'same fault', 'p': round(p, 3)}, c.state, c.wake)
            if p < SAME_FAULT_CUTOFF:
                return 'not sure these are one fault', c, True
        if action == 'open_work_order':
            text = args.get('text')
            if not isinstance(text, str) or not text.strip():
                return 'The work text is missing.', c, True
            # with no reading, the second check below (which fails closed) is the guard; debris removal stays routine work
            if getattr(reading, 'fields', None) and reading.fields.get('contents') == 'seen' and _REMOVAL.search(text):
                return 'items a former tenant left are a staff decision', c, True
            p = opinion('Would this work remove or dispose of items a former tenant left?', {'note': note['note'], 'text': text})
            if p is None:
                return 'The belongings check failed; staff must review it.', _step(store, stream, c, now, {'reason': 'The belongings check failed.'}, 'blocked'), True
            c = _step(store, stream, c, now, {'second_opinion': 'tenant items', 'p': round(p, 3)}, c.state, c.wake)
            if p >= BELONGINGS_CUTOFF:
                return 'items a former tenant left are a staff decision', c, True
        c = current()
        if set(args) - ALLOWED_ARGS.get(action, set()):
            return 'Unknown arguments for this action.', c, True
        action_args = dict(args)
        if action == 'close_report_no_work':
            action_args['text'] = f'No work needed. Staff record {look[2]}: {look[3]}'[:walk.NOTE_MAX]
        if action == 'hand_to_staff':        # handing work to people never needs a grant
            question, evidence, role, wake = (args.get('question'), args.get('record_ids'), args.get('role'), args.get('wake_evidence'))
            if (not isinstance(question, str) or not question.strip() or not isinstance(evidence, list)
                    or not all(isinstance(x, str) for x in evidence) or role not in STAFF_ROLES
                    or wake not in ('staff note', 'work order change', 'staff note or work order change')):
                return ('A handoff needs a question, a list of record ids, a role from front_desk, office_manager or manager, and wake '
                        'evidence from: staff note, work order change, staff note or work order change.'), c, True
            question = question.strip()[:2000]
            latest_check = next((s for s in reversed(c.steps) if isinstance(s, dict) and
                                 (s.get('settled_question') == question or s.get('unsettled_question') == question)), None)
            if latest_check and latest_check.get('settled_question') == question:
                return 'Staff records settled this question; carry on with the case.', c, True
            # a needless handoff leaves a unit waiting on staff, so a second check must agree the cited records need a person
            words = {n['id']: n.get('note') for n in records['walk_notes'] if n.get('note')}
            words.update({o['id']: ' '.join([o.get('problem') or ''] + [ch.get('text') or '' for ch in o.get('changes', [])]).strip()
                          for o in records['work_orders']})
            cited = [words[i] for i in evidence if words.get(i)]
            p = opinion('A worker wants to hand this storage unit to staff with the question given. Do the cited staff records show a matter '
                        'only a person at the facility should decide (a former tenant\'s belongings, a dispute or a damage charge, pests, '
                        'safety, anything legal, or words too unclear to act on), rather than routine cleaning, repair or inspection work?',
                        {'id': c.id, 'question': question, 'cited_records': cited or list(words.values())[-3:]})
            if p is not None:     # when the check cannot answer, the handoff goes ahead: a person deciding is the safe side
                c = _step(store, stream, c, now, {'second_opinion': 'needs a person', 'p': round(p, 3)}, c.state, c.wake)
            if p is not None and p < HANDOFF_CUTOFF:
                return 'A second check found this is routine work, not a staff decision; carry on with the routine work.', c, True
            if records['availability'].get('status') == 'vacant' and not records['availability'].get('holder'):
                held = authority.check(store, stream, c.id, 'hold_unit', {'unit': c.unit, 'objective': c.objective, 'reason': 'Waiting for staff'}, records, now)
                if held.level == 'alone':
                    tools.act('hold_unit', store, stream, c, now, reason='Waiting for staff: ' + question.strip()[:200], approval_id=held.approval)
                    c = current()
            return 'Handed to staff.', tools.act(action, store, stream, c, now, question=question.strip()[:2000], record_ids=evidence[:20], role=role, wake_evidence=wake), False
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
        if action == 'release_unit':
            release = next((e for e in reversed(store.events(stream)) if e.kind == 'unit.unblocked' and e.payload.get('case_id') == c.id), None)
            _, rent = renting.load(store, stream)
            unit = rent.units[c.unit]
            verified = bool(unit.rent_cents and unit.size and after['availability']['status'] == 'vacant' and not after['readiness']['reasons'])
            if release:
                c = _step(store, stream, c, now, {'release_event': release.id, 'release_verified': verified}, c.state, c.wake)
            if _ready_now(store, stream, c, after):
                c = _record(store, stream, c, now, state='done', wake=[])
        return json.dumps(protect(after), default=str), c, False

    content, case_now, is_error = execute()
    if (is_error and action not in playbooks.get(case_now.objective)['reads'] and case_now.state not in ('done', 'needs staff', 'paused', 'blocked')
            and not str(content).startswith('Waiting for')):
        case_now = _step(store, stream, case_now, now, {'action': action, 'refused': str(content)[:300]}, case_now.state, case_now.wake)
    return content, case_now, is_error
