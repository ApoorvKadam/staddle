"""Owner grants and staff approvals kept in the copy's event stream."""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from core.gate import STAFF_ROLES
from core.store import Event, Store

ACTIONS = {'open_work_order', 'link_reports', 'hold_unit', 'release_unit', 'request_inspection', 'hand_to_staff'}


@dataclass
class Result:
    level: str
    approval: str | None = None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def grant(store: Store, stream: str, action: str, limits: dict, expires: datetime, by: str, now: datetime, level: str = 'alone'):
    if by != 'owner' or action not in ACTIONS or level not in ('alone', 'ask'):
        raise ValueError('only the owner may grant a known action')
    if not limits or expires <= now:
        raise ValueError('a grant needs limits and a future expiry')
    with store.write():
        store.append(stream, [Event('tilden.authority.granted', now,
            {'action': action, 'limits': limits, 'expires': expires.isoformat(), 'level': level, 'by': by},
            f'tilden:grant:{uuid4().hex}', source=by)])


def approve(store: Store, stream: str, approval: str, by: str, now: datetime):
    if not by.startswith('staff:') or by.removeprefix('staff:') not in STAFF_ROLES:
        raise ValueError('approval requires staff')
    with store.write():
        events = store.stream_events(stream)
        if not any(e.kind == 'tilden.approval.requested' and e.payload['id'] == approval for e in events):
            raise ValueError('unknown approval')
        if not any(e.kind == 'tilden.approval.approved' and e.payload['id'] == approval for e in events):
            store.append(stream, [Event('tilden.approval.approved', now, {'id': approval, 'by': by},
                                        f'tilden:approved:{approval}', source=by)])


def check(store: Store, stream: str, case_id: str, action: str, effect: dict, records: dict, now: datetime, force_ask: bool = False) -> Result:
    if action not in ACTIONS:
        return Result('never')
    with store.write():
        events = store.stream_events(stream)
        grants = [e for e in store.events(stream) if e.kind == 'tilden.authority.granted' and e.payload['action'] == action and e.at <= now]
        grant_event = grants[-1] if grants else None
        if grant_event:
            p = grant_event.payload
            if (not force_ask and datetime.fromisoformat(p['expires']) > now and p['level'] == 'alone'
                    and all(effect.get(k) == v for k, v in p['limits'].items())):
                return Result('alone')
        fingerprint = _digest({'case': case_id, 'action': action, 'effect': effect, 'records': records})
        requests = [e for e in events if e.kind == 'tilden.approval.requested' and e.payload['fingerprint'] == fingerprint
                    and not any(u.kind in ('tilden.approval.used', 'tilden.approval.stale') and u.payload['id'] == e.payload['id'] for u in events)]
        if requests and not force_ask:
            approval = requests[-1].payload['id']
            if any(e.kind == 'tilden.approval.approved' and e.payload['id'] == approval for e in events):
                return Result('alone', approval)
            return Result('ask', approval)
        approval = uuid4().hex
        store.append(stream, [Event('tilden.approval.requested', now,
            {'id': approval, 'case': case_id, 'action': action, 'effect': effect, 'records': records, 'fingerprint': fingerprint},
            f'tilden:approval:{approval}', source='code')])
        return Result('ask', approval)


def live_grant(store: Store, stream: str, case_id: str | None, action: str, unit: str | None, now: datetime, approval: str | None = None) -> bool:
    """Check the latest grant and case inside the command's write transaction."""
    from core.tilden import cases

    case = cases.load(store, stream).get(case_id or '')
    if case is None or case.unit != unit or case.state == 'done':
        return False
    if action == 'release_unit':
        from core import renting
        _, rent = renting.load(store, stream)
        holder = rent.holder(unit, now)
        if not holder or holder[0] != 'block' or not any(
            e.kind == 'unit.blocked' and e.payload['block'] == holder[1]
            and e.payload.get('case_id') == case_id for e in store.events(stream)
        ):
            return False
    events = store.stream_events(stream)
    if approval:
        request = next((e for e in events if e.kind == 'tilden.approval.requested' and e.payload['id'] == approval), None)
        return bool(request and request.payload['case'] == case_id and request.payload['action'] == action
                    and request.payload['effect'].get('unit') == unit
                    and any(e.kind == 'tilden.approval.approved' and e.payload['id'] == approval for e in events)
                    and not any(e.kind in ('tilden.approval.used', 'tilden.approval.stale') and e.payload['id'] == approval for e in events))
    grants = [e for e in store.events(stream) if e.kind == 'tilden.authority.granted' and e.payload['action'] == action and e.at <= now]
    if not grants:
        return False
    grant = grants[-1].payload
    effect = {'unit': unit, 'objective': case.objective}
    return (grant['level'] == 'alone' and datetime.fromisoformat(grant['expires']) > now
            and all(effect.get(k) == v for k, v in grant['limits'].items()))


def use_approval(store, stream, approval, now):
    if approval:
        store.append(stream, [Event('tilden.approval.used', now, {'id': approval},
                                    f'tilden:used:{approval}', source='code')])
