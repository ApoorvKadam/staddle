"""Record reads and gated commands available to the unit readiness playbook."""
from dataclasses import asdict, dataclass
from uuid import uuid4

from core import reconcile, renting, walk
from core.store import Event
from core.tilden import authority, cases


@dataclass(frozen=True)
class Tool:
    authority: str | None


TOOLS = {name: Tool(None) for name in ('lease', 'walk_notes', 'work_orders', 'readiness', 'availability')}
TOOLS.update({name: Tool(name) for name in ('open_work_order', 'link_reports', 'hold_unit', 'release_unit',
                                             'request_inspection', 'hand_to_staff')})


def read(name, store, stream, case, now):
    if name not in TOOLS or TOOLS[name].authority is not None:
        raise ValueError('unknown read tool')
    books, rent = renting.load(store, stream)
    unit = case.unit
    if unit not in books.units:
        raise ValueError('there is no such unit')
    if name == 'lease':
        return [asdict(lease) for lease in books.leases.values() if lease.unit == unit]
    if name == 'walk_notes':
        return [asdict(note) for note in rent.floor.observations.values() if note.unit == unit]
    if name == 'work_orders':
        return [asdict(order) for order in rent.floor.orders.values() if order.unit == unit]
    if name == 'readiness':
        holder = rent.holder(unit, now)
        if holder and holder[0] == 'block' and any(
            e.kind == 'unit.blocked' and e.payload['block'] == holder[1]
            and e.payload.get('case_id') == case.id for e in store.events(stream)
        ):
            # Judge all readiness rules as if this case's own hold were absent.
            rent.units[unit].holder = None
        return {'id': unit, 'reasons': reconcile.readiness(books, rent, unit, now)}
    holder = rent.holder(unit, now)
    held_by = None
    if holder and holder[0] == 'block':
        placed = next((e for e in store.events(stream) if e.kind == 'unit.blocked' and e.payload.get('block') == holder[1]), None)
        # the worker starts each run fresh from the records, so it must be told which hold is its own
        held_by = ('this case' if placed and placed.payload.get('case_id') == case.id else
                   'another case' if placed and placed.payload.get('case_id') else 'staff')
        held_by = {'by': held_by, 'reason': placed.payload.get('reason') if placed else None}
    return {'id': unit, 'status': rent.status(unit, now), 'holder': holder, 'held_by': held_by}


def act(name, store, stream, case, now, **args):
    if name not in TOOLS or TOOLS[name].authority is None:
        raise ValueError('unknown action tool')
    ident = f'tilden:{uuid4().hex}'
    if name == 'hand_to_staff':
        with store.write():
            authority.use_approval(store, stream, args.get('approval_id'), now)
            store.append(stream, [Event('tilden.case.updated', now, {'id': case.id, 'state': 'needs staff',
                'steps': case.steps + [{'reason': args['reason'], 'evidence': args.get('evidence', [])}], 'wake': []},
                f'tilden:case:{uuid4().hex}', source='agent:tilden')])
        return cases.load(store, stream)[case.id]
    if name in ('hold_unit', 'release_unit'):
        command = renting.Act(ident, 'block_unit' if name == 'hold_unit' else 'unblock_unit', 'agent:tilden', case.unit,
                              args={'reason': args['reason']} if name == 'hold_unit' else {}, case_id=case.id, approval_id=args.get('approval_id'))
        return renting.run(store, stream, command, now)
    if name == 'link_reports':
        command = walk.Act(ident, 'join_work_order', 'agent:tilden', about=args['order'], into=args['into'], case_id=case.id, approval_id=args.get('approval_id'))
    else:
        text = f'Inspect unit {case.unit} after repair' if name == 'request_inspection' else args['text']
        command = walk.Act(ident, 'report_work_order', 'agent:tilden', unit=case.unit, text=text,
                           case_id=case.id, authority_action=name, approval_id=args.get('approval_id'), answers_record=args.get('answers_record'))
    return walk.run(store, stream, command, now)
