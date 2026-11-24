from datetime import datetime, timedelta, timezone

from core.store import Store
from core.tilden import authority

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


def test_default_approval_is_bound_to_effect_and_current_records():
    store = Store()
    effect = {'unit': 'A1', 'action': 'hold_unit'}
    pending = authority.check(store, 'copy', 'case1', 'hold_unit', effect, {'version': 1}, NOW)
    assert pending.level == 'ask'
    authority.approve(store, 'copy', pending.approval, 'staff:office_manager', NOW)
    assert authority.check(store, 'copy', 'case1', 'hold_unit', effect, {'version': 1}, NOW).level == 'alone'
    assert authority.check(store, 'copy', 'case1', 'hold_unit', effect, {'version': 2}, NOW).level == 'ask'


def test_owner_grant_expires_and_limits_effect():
    store = Store()
    authority.grant(store, 'copy', 'hold_unit', {'unit': 'A1'}, NOW + timedelta(hours=1), 'owner', NOW)
    assert authority.check(store, 'copy', 'case1', 'hold_unit', {'unit': 'A1'}, {}, NOW).level == 'alone'
    assert authority.check(store, 'copy', 'case1', 'hold_unit', {'unit': 'A2'}, {}, NOW).level == 'ask'
    assert authority.check(store, 'copy', 'case1', 'hold_unit', {'unit': 'A1'}, {}, NOW + timedelta(hours=1)).level == 'ask'
