"""Durable case records folded from the copy's event stream."""
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from core.store import Event, Store

STATES = {'open', 'working', 'waiting', 'needs staff', 'done', 'blocked'}


@dataclass
class Case:
    id: str
    objective: str
    unit: str
    source_event: int
    state: str = 'open'
    links: list = field(default_factory=list)
    plan: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    wake: list = field(default_factory=list)
    history: list = field(default_factory=list)


def load(store: Store, stream: str) -> dict[str, Case]:
    found = {}
    for event in store.stream_events(stream):
        if not event.kind.startswith('tilden.case.'):
            continue
        p = event.payload
        if event.kind == 'tilden.case.opened':
            found[p['id']] = Case(p['id'], p['objective'], p['unit'], p['source_event'])
        case = found.get(p['id'])
        if case is None:
            continue
        if event.kind == 'tilden.case.updated':
            for name in ('state', 'links', 'plan', 'steps', 'wake'):
                if name in p:
                    setattr(case, name, p[name])
        case.history.append({'id': event.id, 'kind': event.kind, 'at': event.at.isoformat(), 'payload': p})
    return found


def open_cases(store: Store, stream: str) -> list[Case]:
    return [case for case in load(store, stream).values() if case.state != 'done']


def open_case(store: Store, stream: str, source: Event, unit: str, now: datetime) -> Case:
    if source.id is None:
        raise ValueError('source event must be stored')
    with store.write():
        events = store.events(stream)
        if not any(e.id == source.id for e in events):
            raise ValueError('source event is not visible in this copy')
        existing = next((c for c in load(store, stream).values() if c.source_event == source.id), None)
        if existing:
            return existing
        case_id = uuid4().hex
        p = {'id': case_id, 'objective': 'make-unit-ready', 'unit': unit, 'source_event': source.id}
        store.append(stream, [Event('tilden.case.opened', now, p, f'tilden:source:{source.id}', source='code', caused_by=source.id)])
    return load(store, stream)[case_id]


def record(store: Store, stream: str, case_id: str, kind: str, changes: dict, now: datetime, source: str = "code") -> Case:
    if kind != 'updated' or not changes.keys() <= {'state', 'links', 'plan', 'steps', 'wake'}:
        raise ValueError('unknown case change')
    if 'state' in changes and changes['state'] not in STATES:
        raise ValueError('unknown case state')
    with store.write():
        if case_id not in load(store, stream):
            raise KeyError(case_id)
        store.append(stream, [Event('tilden.case.updated', now, {'id': case_id, **changes},
                                    f'tilden:case:{uuid4().hex}', source=source)])
    return load(store, stream)[case_id]
