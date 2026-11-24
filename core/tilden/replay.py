"""Replay authored unit-readiness cases against a supplied worker."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import re
from uuid import uuid4

from core import reconcile, renting, walk
from core.billing import Ledger
from core.books import at_local, local
from core.store import Event, Store
from core.tilden import authority, cases


@dataclass
class Entry:
    id: str
    at: datetime
    kind: str
    who: str
    text: str
    answers: str | None = None


@dataclass
class Case:
    id: str
    unit: str
    size: str
    entries: list[Entry]
    rentable_after: str | None
    staff_decide: str | None
    staff_reason: str
    must_not: list[str]
    why: str


@dataclass
class Step:
    entry: str
    rentable: bool
    needs_staff: bool
    paused: bool = False
    actions: list[dict] = field(default_factory=list)


def _actions(events):
    actions = []
    for event in events:
        if event.source != 'agent:tilden':
            continue
        p = event.payload
        if event.kind == 'work_order.reported':
            actions.append({'action': 'request_inspection' if p.get('kind') == 'check' else 'open_work_order',
                            'order': p['order'], 'text': p['problem'], 'answers_record': p.get('answers_record')})
        elif event.kind == 'work_order.closed' and p.get('no_work'):
            actions.append({'action': 'close_report_no_work', 'order': p['order'], 'text': p['text']})
        elif event.kind == 'work_order.joined':
            actions.append({'action': 'link_reports', 'order': p['order'], 'into': p['into']})
        elif event.kind == 'unit.blocked':
            actions.append({'action': 'hold_unit', 'block': p['block'], 'reason': p['reason']})
        elif event.kind == 'unit.unblocked':
            actions.append({'action': 'release_unit', 'block': p['block']})
        elif event.kind == 'tilden.case.updated' and p.get('state') == 'needs staff':
            steps = p.get('steps', [])
            if steps and 'handoff' in steps[-1]:
                actions.append({'action': 'hand_to_staff', **steps[-1]['handoff']})
    return actions


@dataclass
class Result:
    case: str
    released_at: str | None
    handed_at: str | None
    release_right: bool
    released_early: bool
    released_late_or_missed: bool
    handoff_right: bool
    premature_handoff: bool     # handed before the entry that needs a person: a worker that hands everything over is not right
    missed_handoff: bool
    needless_handoff: bool
    must_not: list[str]
    paused: bool = False
    released_on_due_entry: bool | None = None
    kept_off_when_never_offered: bool | None = None
    post_handoff_recovery: bool | None = None


def _date(value):
    value = ' '.join(value.strip().split())
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %I:%M %p',
                '%b %d, %I:%M %p', '%B %d, %I:%M %p', '%b %d %I:%M %p',
                '%B %d %I:%M %p', '%b %d, %Y %I:%M %p', '%B %d, %Y %I:%M %p',
                '%b %d, %Y, %I:%M %p', '%B %d, %Y, %I:%M %p'):
        try:
            parsed = datetime.strptime(value, fmt)
            if '%Y' not in fmt:
                parsed = parsed.replace(year=2026)
            return at_local(parsed.date(), parsed.hour, parsed.minute).replace(second=parsed.second)
        except ValueError:
            pass
    raise ValueError(f'cannot read date and time: {value}')


def parse(text, skipped=None):
    """Read the case format; a missing year uses 2026 for reproducible runs. With `skipped`, a case that cannot be read is
    listed there with its reason and the rest are returned; without it the first such case stops the read."""
    blocks = re.split(r'(?m)^\s*(?=U\d+\.\s*$)', text.strip())
    # an author's reply can open with a line about what follows, and can arrive in parts that repeat cases; the text before the
    # first case is not a case, and when a case number comes again the author's last version is the one kept
    if blocks and not re.match(r'\s*U\d+\.\s*\n', blocks[0]):
        blocks = blocks[1:]
    latest = {}
    for block in filter(str.strip, blocks):
        head = re.match(r'\s*(U\d+)\.', block)
        latest.pop(head.group(1) if head else None, None)
        latest[head.group(1) if head else None] = block
    blocks = sorted(latest.values(), key=lambda b: int(re.match(r'\s*U(\d+)', b).group(1)) if re.match(r'\s*U\d+', b) else 0)
    out = []
    for block in blocks:
        try:
            match = re.fullmatch(r'\s*(U\d+)\.\s*\n\s*UNIT:\s*(.*?)\s*\n(.*)', block, re.S)
            if not match:
                raise ValueError('invalid case header')
            ident, unit_line, body = match.groups()
            unit_match = re.fullmatch(r'(\S+)\s+(.+)', unit_line.strip())
            if not unit_match:
                raise ValueError(f'{ident}: unit and size required')
            unit, size = unit_match.groups()
            labels = re.search(r'(?m)^\s*RENTABLE AFTER:\s*(.*?)\s*\n\s*STAFF DECIDE:\s*(.*?)\s*\n\s*MUST NOT:\s*(.*?)\s*\n\s*WHY:\s*(.*)\Z', body, re.S)
            if not labels:
                raise ValueError(f'{ident}: missing labels')
            rentable, staff, forbidden, why = labels.groups()
            rentable = rentable.strip()
            rentable = None if rentable.lower() == 'never in this record' else rentable
            staff = ' '.join(staff.split())
            staff_match = re.fullmatch(r'(E\d+):\s*(.+)', staff)
            if staff.lower() == 'none':
                staff_id, reason = None, ''
            elif staff_match:
                staff_id, reason = staff_match.groups()
            else:
                raise ValueError(f'{ident}: invalid staff label')
            entries_text = body[:labels.start()].strip()
            pattern = re.compile(r'(?ms)^\s*(E\d+):\s*(.*?),\s*(MOVE-OUT|NOTE|REPORT|WORK DONE|INSPECTION),\s*(.*?):\s*"(.*?)"\s*(?=^\s*E\d+:|\Z)')
            entries = []
            end = 0
            for item in pattern.finditer(entries_text):
                if entries_text[end:item.start()].strip():
                    raise ValueError(f'{ident}: invalid entry')
                eid, when, kind, author, words = item.groups()
                answer = re.fullmatch(r'(.*?),\s*answers\s+(E\d+)', author.strip(), re.I)
                who, ref = (answer.group(1).strip(), answer.group(2)) if answer else (author.strip(), None)
                entries.append(Entry(eid, _date(when), kind, who, ' '.join(words.split()), ref))
                end = item.end()
            if not entries or entries_text[end:].strip():
                raise ValueError(f'{ident}: invalid entries')
            ids = [e.id for e in entries]
            if len(set(ids)) != len(ids) or entries[0].kind != 'MOVE-OUT':
                raise ValueError(f'{ident}: invalid entry sequence')
            for i, entry in enumerate(entries):
                if entry.answers and entry.answers not in ids[:i]:
                    raise ValueError(f'{ident}: answer must name an earlier entry')
                if entry.kind == 'WORK DONE' and (not entry.answers or entries[ids.index(entry.answers)].kind not in ('NOTE', 'REPORT', 'INSPECTION')):
                    raise ValueError(f'{ident}: work must answer a note, report or inspection')
                if i and entry.at < entries[i - 1].at:
                    raise ValueError(f'{ident}: entries are out of time order')
            if rentable is not None and rentable not in ids or staff_id is not None and staff_id not in ids:
                raise ValueError(f'{ident}: label names an unknown entry')
            must_not = [line.strip().removeprefix('- ').strip() for line in forbidden.splitlines() if line.strip()]
            if len(must_not) == 1 and must_not[0].lower() == 'none':
                must_not = []
            out.append(Case(ident, unit, size, entries, rentable, staff_id, reason, must_not, ' '.join(why.split())))
        except ValueError as exc:
            # a case the loader cannot read is left out of every worker's score and listed, never fixed by editing the set
            if skipped is None:
                raise
            skipped.append(str(exc))
    if not out:
        raise ValueError('no cases')
    return out


def replay(case, arm, readers=None):
    """Run an arm once per entry. Readers may be captured by the supplied arm."""
    store, stream = Store(), 'visitor:case'
    first = case.entries[0].at
    before = first - timedelta(days=2)
    led = Ledger()
    led.set_up(before, 'Practice facility', 'Orlando, FL', [case.unit])
    tenant = led.add_tenant(before, 'Practice tenant', '+14075550101')
    lease = led.sign_lease(before, tenant, case.unit, 10000, [])
    with store.write():
        store.append('base', led.pending)
    priced = renting.run(store, stream, renting.Act('replay:price', 'price_unit', 'staff:manager', case.unit,
                         args={'rent_cents': 10000, 'size': case.size}), before)
    if not priced.allowed:
        raise ValueError(f'cannot price unit: {priced.check}')
    expires = max(e.at for e in case.entries) + timedelta(days=1)
    for action in ('open_work_order', 'link_reports', 'close_report_no_work', 'hold_unit', 'release_unit', 'request_inspection'):
        authority.grant(store, stream, action, {'objective': 'make-unit-ready'}, expires, 'owner', before)
    orders, note_events, inspections, trace = {}, {}, {}, []

    def command(action, entry, **kwargs):
        act = walk.Act(uuid4().hex, action, 'staff:front_desk', **kwargs)
        result = walk.run(store, stream, act, entry.at)
        if not result.allowed:
            raise ValueError(f'{entry.id}: {result.check}')
        return result.made

    def finish(order, entry):
        while True:
            current = walk.load(store, stream)[1].orders[order]
            if current.joined_into:
                order = current.joined_into
                continue
            if current.state == 'closed':
                return
            action = {'reported': 'triage_work_order', 'triaged': 'assign_work_order',
                      'assigned': 'record_order_work', 'work recorded': 'check_order_work',
                      'staff checked': 'close_work_order'}[current.state]
            command(action, entry, about=order, text=entry.text, **({'to': 'front_desk'} if action == 'assign_work_order' else {}))

    for entry in case.entries:
        with store.write():
            recorded = store.append(stream, [Event('replay.entry', entry.at,
                {'entry': entry.id, 'kind': entry.kind, 'unit': case.unit, 'who': entry.who,
                 'text': entry.text, 'answers': entry.answers}, f'replay:{entry.id}', source='staff:front_desk')])[0]
        if entry.kind == 'MOVE-OUT':
            with store.write():
                led = Ledger(store.books(stream))
                renting.end_lease(led, lease, local(entry.at).date(), entry.at, 'staff:front_desk')
                store.append(stream, led.pending)
        elif entry.kind == 'NOTE':
            wid = command('start_walk', entry, route=[case.unit])
            command('observe_unit', entry, about=wid, unit=case.unit, text=entry.text,
                    form={'lock': 'unknown', 'whose': 'unknown', 'tag': 'unknown', 'door': 'unknown'})
            note_events[entry.id] = next(e.payload['observation'] for e in reversed(store.stream_events(stream)) if e.kind == 'walk.observed')
            # a worker asked staff to look; the note staff wrote next is that look, so it closes a plain inspection request
            asked = {e.payload['order'] for e in store.stream_events(stream) if e.kind == 'work_order.reported' and not e.payload.get('answers_record')}
            plain = next((o.id for o in walk.load(store, stream)[1].orders.values()
                          if o.unit == case.unit and o.kind == 'check' and o.state != 'closed' and o.id in asked), None)
            if plain:
                finish(plain, entry)
        elif entry.kind == 'REPORT':
            orders[entry.id] = command('report_work_order', entry, unit=case.unit, text=entry.text)
        elif entry.kind == 'WORK DONE':
            if entry.answers in orders:
                order = orders[entry.answers]
            elif entry.answers in inspections:
                # work after a look that found a fault: the first open repair a worker opened after that look, else staff report it
                after = inspections[entry.answers][1]
                made = [e.payload['order'] for e in store.stream_events(stream)
                        if e.kind == 'work_order.reported' and e.id > after and e.payload['unit'] == case.unit and e.payload.get('kind') != 'check']
                current = walk.load(store, stream)[1].orders
                order = next((oid for oid in made if current[oid].state != 'closed'), None)
                if order is None:
                    original = next(e for e in case.entries if e.id == entry.answers)
                    order = command('report_work_order', entry, unit=case.unit, text=original.text)
            else:
                note_id = note_events[entry.answers]
                made = [e.payload['order'] for e in store.stream_events(stream)
                        if e.kind == 'work_order.reported' and e.payload.get('answers_record') == note_id and e.payload['unit'] == case.unit]
                current = walk.load(store, stream)[1].orders
                order = next((oid for oid in made if current[oid].state != 'closed'), None)
                if order is None:
                    original = next(e for e in case.entries if e.id == entry.answers)
                    order = command('report_work_order', entry, unit=case.unit, text=original.text)
            finish(order, entry)
            orders[entry.id] = order
        elif entry.kind == 'INSPECTION':
            current = walk.load(store, stream)[1].orders.values()
            evidence = {e.payload['order']: e.payload.get('answers_record') for e in store.stream_events(stream)
                        if e.kind == 'work_order.reported'}
            target = orders.get(entry.answers) or note_events.get(entry.answers)
            order = next((o.id for o in current if o.unit == case.unit and o.kind == 'check' and o.state != 'closed'
                          and (not entry.answers or target is not None and evidence.get(o.id) == target)), None)
            if order is None:      # a plain inspection request (no record named) is what staff answer when nothing more specific is open
                order = next((o.id for o in current if o.unit == case.unit and o.kind == 'check' and o.state != 'closed'
                              and not evidence.get(o.id)), None)
            if order is None:
                # The staff command cannot yet label a report as a check.
                with store.write():
                    books, floor = walk.load(store, stream)
                    led = Ledger(books)
                    act = walk.Act(uuid4().hex, 'report_work_order', 'staff:front_desk', unit=case.unit, text=entry.text)
                    decision = walk.command(led, floor, act, entry.at)
                    if not decision.allowed:
                        raise ValueError(f'{entry.id}: {decision.check}')
                    next(e for e in led.pending if e.kind == 'work_order.reported').payload['kind'] = 'check'
                    store.append(stream, led.pending)
                    order = decision.made
            finish(order, entry)
            inspections[entry.id] = (order, max(e.id for e in store.stream_events(stream)))
        before_arm = len(store.stream_events(stream))
        arm(store, stream, entry.at)
        actions = _actions(store.stream_events(stream)[before_arm:])
        books, rent = renting.load(store, stream)
        rentable = not reconcile.readiness(books, rent, case.unit, entry.at)
        needs = any(c.unit == case.unit and c.state == 'needs staff' for c in cases.load(store, stream).values())
        trace.append(Step(entry.id, rentable, needs, any(c.unit == case.unit and c.state == 'paused' for c in cases.load(store, stream).values()), actions))
    return trace


def score(case, trace):
    released = next((s.entry for s in trace if s.rentable), None)
    handed = next((s.entry for s in trace if s.needs_staff), None)
    order = {e.id: i for i, e in enumerate(case.entries)}
    early = released is not None and (case.rentable_after is None or order[released] < order[case.rentable_after])
    late = case.rentable_after is not None and (released is None or order[released] > order[case.rentable_after])
    hand_right = case.staff_decide is not None and handed == case.staff_decide
    premature = case.staff_decide is not None and handed is not None and order[handed] < order[case.staff_decide]
    missed = case.staff_decide is not None and (handed is None or order[handed] > order[case.staff_decide])
    return Result(case.id, released, handed, released == case.rentable_after, early, late, hand_right, premature,
                  missed, case.staff_decide is None and handed is not None, list(case.must_not), any(s.paused for s in trace),
                  released == case.rentable_after if case.rentable_after else None,
                  released is None if case.rentable_after is None else None,
                  # recovery needs a handoff seen by the deciding entry, then the release on the due entry
                  (released == case.rentable_after and handed is not None and order[handed] <= order[case.staff_decide])
                  if case.staff_decide and case.rentable_after and order[case.staff_decide] < order[case.rentable_after] else None)


def totals(results):
    """Count each scoring outcome across one arm's cases."""
    names = ('release_right', 'released_early', 'released_late_or_missed', 'handoff_right', 'premature_handoff', 'missed_handoff',
             'needless_handoff', 'paused')
    counts = {name: sum(bool(getattr(result, name)) for result in results) for name in names}
    for name in ('released_on_due_entry', 'kept_off_when_never_offered', 'post_handoff_recovery'):
        values = [getattr(result, name) for result in results if getattr(result, name) is not None]
        counts[name] = {'right': sum(values), 'of': len(values)}
    return counts
