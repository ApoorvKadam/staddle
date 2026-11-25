"""The live copy takes the terms of every active lease from its (made-up) signed agreement, so a rent increase on a Northstar lease can go
from proposal to a posted bill on the demo. A plain build is unchanged."""
import itertools
from datetime import timedelta

from core import price_law, seed
from core.billing import Ledger
from core.books import at_local
from core.store import BASE, Store

STEPS = itertools.count(1)


def live_store():
    store = Store()
    with store.write():
        store.append(BASE, seed.build(standing=True, today=seed.TODAY, cutoff=at_local(seed.TODAY)).led.pending)
    return store


def test_a_plain_build_takes_no_lease_terms():
    assert not [e for e in seed.build().led.pending if e.kind == 'price_law.terms_snapshotted']


def test_the_live_copy_has_terms_for_every_active_lease_and_none_for_the_rest():
    year = seed.build(standing=True, today=seed.TODAY, cutoff=at_local(seed.TODAY))
    books = year.led.books
    active = {x.id for x in books.leases.values() if x.ended is None}
    terms = {e.payload['lease']: e.payload for e in year.led.pending if e.kind == 'price_law.terms_snapshotted'}
    assert active and set(terms) == active
    for lease, p in terms.items():
        assert p['version'] == 1 and p['signed_on'] == books.leases[lease].start.isoformat()
        assert p['notice_days'] == 30 and p['notice_methods'] == ['first_class_mail', 'email'] and p['email_notice_authorized']
        assert p['last_known_address'] and p['last_known_email'].endswith('@example.com') and p['rent_change_allowed']
    assert len({p['agreement_sha256'] for p in terms.values()}) == len(terms)         # each lease has its own fingerprint


def test_a_northstar_increase_goes_from_proposal_to_a_posted_bill_on_the_live_copy():
    store = live_store()
    now = at_local(seed.TODAY, 10)
    books = store.books(BASE)
    lease = min((x for x in books.leases.values() if x.ended is None), key=lambda x: x.id)
    terms = price_law.terms_of(books, lease.id)
    effective = seed.TODAY + timedelta(days=31)
    form = {'kind': 'lease_increase', 'current_cents': lease.rent_cents, 'new_cents': lease.rent_cents + 500, 'effective_on': effective.isoformat(),
            'lease': lease.id, 'other_recurring_cents': 0}

    def go(action, about='RI-live', at=now, **fields):
        act = price_law.Act(f'live-step-{next(STEPS)}', action, 'staff:office_manager', about, fields)
        return price_law.run(store, BASE, act, at)
    for point in ('proposal',):
        assert go('check_price_change', point=point, **form).allowed
    assert go('approve_price_change', **form).allowed
    assert go('check_price_change', point='notice_authorization', **form).allowed
    assert go('record_notice', change='RI-live', lease=lease.id, document_sha256='a' * 64, recipient='Tenant', channel='first_class_mail',
              sent_to=terms.last_known_address, attempted_on=seed.TODAY.isoformat(), dispatch_accepted=True, certificate_of_mailing=True,
              postage_prepaid=True, new_cents=form['new_cents'], effective_on=effective.isoformat()).allowed
    assert go('check_price_change', point='notice_completion', **form).allowed
    on = at_local(effective, 10)
    assert go('check_price_change', at=on, point='activation', **form).allowed
    invoice = go('check_price_change', at=on, point='invoice', invoice=f'{lease.id}:{effective:%Y-%m}', **form)
    assert invoice.allowed, invoice.check
    with store.write():
        led = Ledger(store.books(BASE))
        charge = price_law.post_invoice(led, on, decision=invoice.made)
        store.append(BASE, led.pending)
    assert store.books(BASE).charges[charge].cents == form['new_cents']
