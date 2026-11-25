"""The live copy's two move-outs for Tilden's shift: made through the staff commands after the owner's grants, on leases no other story
uses, as starting records only. A plain build, and a live copy with the shift switch off, are unchanged."""
from datetime import timedelta

from core import renting, walk
from core.seed import NOW, TODAY, build
from core.store import BASE, Store
from tests.shift_world import year


def totals(y):
    b = y.led.books
    return (len(y.led.pending), len(b.units), len(b.tenants), len(b.leases), len(b.charges), len(b.payments), sum(c.cents for c in b.charges.values()))


def test_a_plain_build_is_the_same_with_or_without_the_shift():
    plain = build()
    assert totals(build(shift=True)) == totals(plain)
    assert [e.kind for e in build(shift=True).led.pending] == [e.kind for e in plain.led.pending]


def test_a_live_copy_with_the_switch_off_is_the_copy_it_was():
    assert totals(build(standing=True, shift=False)) == totals(build(standing=True))
    assert not any(e.kind == 'moveout.requested' and e.at > NOW for e in build(standing=True).led.pending)


def test_the_live_copy_with_the_switch_on_only_adds_records_after_the_grants():
    off, on = build(standing=True).led.pending, year().led.pending
    assert [e.kind for e in on[:len(off)]] == [e.kind for e in off]
    grants = [e for e in off if e.kind == 'tilden.authority.granted']
    assert grants and all(e.at == grants[0].at for e in grants)
    added = on[len(off):]
    assert added and all(e.at > grants[0].at for e in added) and all(e.at.date() == TODAY for e in added)
    # nothing was decided in advance: no hold, no case, no finished repair, no inspection
    for e in added:
        assert not e.kind.startswith('tilden.') and e.kind not in ('unit.blocked', 'unit.unblocked', 'work_order.triaged', 'work_order.assigned',
                                                                  'work_order.recorded', 'work_order.checked', 'work_order.closed', 'work_order.joined')
        assert e.payload.get('kind') != 'check'


def test_two_move_outs_through_the_staff_commands_on_leases_nothing_else_uses():
    on = year().led.pending
    asked = [e for e in on if e.kind == 'moveout.requested' and e.at > NOW]
    assert len(asked) == 2 and all(e.source == 'staff:front_desk' and e.payload['on'] == TODAY.isoformat() for e in asked)
    ended = [e for e in on if e.kind == 'lease.ended' and e.at > NOW]
    assert len(ended) == 2
    y = year()
    b = y.led.books
    leases = {e.payload['lease'] for e in ended}
    tenants = {b.leases[x].tenant for x in leases}
    units = {b.leases[x].unit for x in leases}
    cast = {t for c in y.cast.values() for t in (c['tenant'], *c['leases'])}
    assert not (leases | tenants) & cast
    assert not units & {u for v in y.walked.values() for u in (v if isinstance(v, list) else [v])}
    elsewhere = {'lien_case.opened', 'plan.accepted', 'protection.accepted', 'lead.converted', 'promotion.redeemed', 'dispute.opened', 'overlock.recorded', 'card.saved',
                 'payment.returned', 'charge.waived', 'notice.recorded', 'lead.checkout_started'}
    others = [e for e in on if e.kind in elsewhere]
    assert others
    for e in others:
        text = repr(e.payload)
        assert not any(x in text for x in leases | tenants | units)
    # the account is square before and after: nothing owed, no credit left that could be spent twice
    assert all(not b.receivable(x) for x in leases)


def test_the_repair_unit_has_two_open_reports_and_the_other_two_unclear_notes():
    store = Store()
    with store.write():
        store.append(BASE, list(year().led.pending))
    wk = walk.load(store, BASE)[1]
    fresh = [o for o in wk.orders.values() if o.reported > NOW]
    assert len(fresh) == 2 and {o.state for o in fresh} == {'reported'} and len({o.unit for o in fresh}) == 1
    unit = fresh[0].unit
    seen = [e for e in store.events(BASE) if e.kind == 'walk.observed' and e.at > NOW]
    assert len(seen) == 2 and len({e.payload['unit'] for e in seen}) == 1 and seen[0].payload['unit'] != unit
    assert all(e.payload['note'] for e in seen)
    # both units are the ones whose lease ended this morning
    books, rent = renting.load(store, BASE)
    assert {unit, seen[0].payload['unit']} == {books.leases[e.payload['lease']].unit for e in store.events(BASE) if e.kind == 'lease.ended' and e.at > NOW}
    assert all(r.state == 'ended' for r in rent.rentals.values() if r.unit in (unit, seen[0].payload['unit']))
