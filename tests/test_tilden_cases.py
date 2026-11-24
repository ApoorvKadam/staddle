from datetime import datetime, timezone

from core.store import Event, Store
from core.tilden import cases

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


def test_case_events_resume_and_deduplicate(tmp_path):
    path = str(tmp_path / 'events.db')
    store = Store(path)
    with store.write():
        source = store.append('visit', [Event('lease.ended', NOW, {'unit': 'A1'}, 'end:1')])[0]
    first = cases.open_case(store, 'visit', source, 'A1', NOW)
    assert cases.open_case(store, 'visit', source, 'A1', NOW).id == first.id
    cases.record(store, 'visit', first.id, 'updated', {
        'state': 'waiting', 'links': ['lease:1'], 'steps': ['inspect'],
        'wake': ['inspection.recorded']}, NOW)
    resumed = cases.load(Store(path), 'visit')[first.id]
    assert resumed.objective == 'make-unit-ready'
    assert resumed.unit == 'A1'
    assert resumed.state == 'waiting'
    assert resumed.links == ['lease:1']
    assert resumed.steps == ['inspect']
    assert resumed.wake == ['inspection.recorded']
    assert len(resumed.history) == 2
    assert [case.id for case in cases.open_cases(Store(path), 'visit')] == [first.id]
