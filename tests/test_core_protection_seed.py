"""The seeded protection records on the live copy's year: a renewal not yet reviewed, a placeholder policy number, an expired policy, a moved-out
tenant with a stale enrolment, and a proposed charge the gate refuses. All written through the real commands as synthetic records. They carry
no money, so every figure of the year is the same without them."""
from collections import Counter
from datetime import date, timedelta

import pytest

from core import protection, work
from core.books import PROTECTION, at_local, lines
from core.seed import TODAY, build
from core.store import Store

STREAM = "visitor:a"


def world(today=TODAY, **kw):
    y = build(today=today, cutoff=at_local(today), standing=True, **kw)
    store = Store()
    with store.write():
        store.append("base", y.led.pending)
    return y, store


def money(y):
    """Everything money in the year: each journal line, and each charge with what was paid on it."""
    b = y.led.books
    return (Counter((a, c, ch) for e in y.led.pending for a, c, ch in lines(e.kind, e.payload)),
            {c.id: (c.lease, c.kind, c.cents, c.due, c.paid, c.closed) for c in b.charges.values()},
            {p.id: (p.tenant, p.cents, p.returned) for p in b.payments.values()}, dict(b.credit), dict(b.refunds))


def test_the_protection_records_change_no_money_figure_of_the_year():
    with_it, without = build(today=TODAY, cutoff=at_local(TODAY), standing=True), build(today=TODAY, cutoff=at_local(TODAY), standing=True, protected=False)
    assert money(with_it) == money(without)
    assert protection.state(with_it.led.books).enrolments and protection.state(without.led.books).enrolments == {}


def test_the_fixed_books_have_no_protection_records():
    y = build(today=TODAY, cutoff=at_local(TODAY))
    assert not [e for e in y.led.pending if e.kind.startswith("protection.")] and y.led.books.protection is None


def test_a_renewal_not_yet_reviewed_stands_beside_the_policy_that_ended():
    y, store = world()
    st = protection.state(store.books("base"))
    old, renewal = [e for e in st.evidence.values() if e.lease == y.lease("luis")]
    assert (old.status, [h["status"] for h in old.history], old.review["by"]) == ("expired", ["received", "reviewed", "expired"], "staff:office_manager")
    assert (renewal.status, renewal.renews, renewal.review) == ("received", old.id, None) and renewal.effective == old.expires + timedelta(days=1)


@pytest.mark.parametrize("today", [TODAY, date(2026, 12, 3), date(2027, 1, 20), date(2026, 11, 2)])
def test_every_seeded_policy_record_and_review_comes_after_the_move_in_and_before_the_policy_ended(today):
    from core.books import local
    y, store = world(today)
    b = store.books("base")
    for e in protection.state(b).evidence.values():
        start = b.leases[e.lease].start
        assert local(e.recorded_at).date() >= start, (e.id, e.lease)                           # the document cannot arrive before the lease it is about
        if e.review:
            assert e.recorded_at <= e.review["at"] and local(e.review["at"]).date() <= e.expires, (e.id, "reviewed while the policy still ran")


def test_a_policy_document_with_a_placeholder_number_needs_review_and_is_flagged():
    y, store = world()
    (e,) = [e for e in protection.state(store.books("base")).evidence.values() if e.lease == y.lease("yara")]
    assert (e.number, e.status, e.flags) == ("TEMP", "needs review", ("placeholder number",))


def test_a_reviewed_policy_that_ended_with_no_renewal_is_expired():
    y, store = world()
    (e,) = [e for e in protection.state(store.books("base")).evidence.values() if e.lease == y.lease("dev")]
    assert e.status == "expired" and e.review["by"] == "staff:office_manager" and e.expires == TODAY - timedelta(days=7)
    assert protection.standing(protection.state(store.books("base")), y.lease("dev")).id == e.id


def test_a_moved_out_tenant_still_has_an_active_plan_that_no_charge_was_ever_posted_under():
    y, store = world()
    b = store.books("base")
    (e,) = [e for e in protection.state(b).enrolments.values() if e.lease == y.lease("olga")]
    assert e.state == "active" and b.leases[e.lease].ended is not None and e.acceptance and protection.state(b).acceptances[e.acceptance].by == b.leases[e.lease].tenant
    assert not [c for c in b.charges.values() if c.kind == PROTECTION]


def test_the_price_change_after_an_acceptance_and_the_proposed_charge_the_gate_refused():
    y, store = world()
    b = store.books("base")
    st = protection.state(b)
    assert [(v.version, v.cents) for v in st.versions[protection.PRODUCT]] == [(1, 1200), (2, 1500)]
    (e,) = [e for e in st.enrolments.values() if e.lease == y.lease("tara")]
    assert (e.state, e.version, e.cents) == ("active", 1, 1200)
    refused = b.decisions["seed:protection:charge:tara"]
    assert (refused["allowed"], refused["why"], refused["action"]) == (False, "acceptance_other_terms", "post_plan_charge")
    assert not [c for c in b.charges.values() if c.kind == PROTECTION]


def test_every_seeded_record_was_made_by_a_staff_command_and_only_that_one_was_refused():
    y, store = world()
    decided = {k: d for k, d in store.books("base").decisions.items() if k.startswith("seed:protection:")}
    assert len(decided) >= 8 and {k for k, d in decided.items() if not d["allowed"]} == {"seed:protection:charge:tara"}
    assert all(d["source"].startswith("staff:") for d in decided.values())


def test_the_work_queue_reads_five_protection_items_from_the_seed_and_the_daily_check_has_nothing_to_add():
    y, store = world()
    assert protection.sweep(store, STREAM, at_local(TODAY, 9)) == 0
    work.sweep(store, STREAM, at_local(TODAY, 9))
    books, rent, wk = work.load(store, STREAM)
    open_ = {i.key: i for i in work.open_items(work.queue(books, rent, wk, at_local(TODAY, 9))) if i.source == "protection"}
    st = protection.state(books)
    policies = lambda role: [e.id for e in st.evidence.values() if e.lease == y.lease(role)]
    luis_old, luis_new = policies("luis")
    (yara,), (dev,) = policies("yara"), policies("dev")
    (olga,) = [e.id for e in st.enrolments.values() if e.lease == y.lease("olga")]
    assert set(open_) == {f"protection:review:{luis_new}", f"protection:expiring:{luis_old}", f"protection:review:{yara}", f"protection:expiring:{dev}",
                          f"protection:ended-lease:{olga}"}
    assert open_[f"protection:expiring:{dev}"].what == "Policy expired" and open_[f"protection:review:{yara}"].what == "Policy number looks like a placeholder"


@pytest.mark.parametrize("today", [date(2026, 12, 3), date(2027, 1, 20), date(2026, 11, 2)])
def test_the_seed_holds_on_any_day_the_live_copy_can_open(today):
    y, store = world(today)
    assert len(protection.state(store.books("base")).evidence) == 4 and len(protection.state(store.books("base")).enrolments) == 2


def test_the_same_seed_gives_the_same_protection_records():
    shape = lambda y: [(e.id, e.lease, e.state, e.version) for e in protection.state(y.led.books).enrolments.values()] + [
        (e.id, e.lease, e.status, e.number) for e in protection.state(y.led.books).evidence.values()]
    assert shape(build(today=TODAY, cutoff=at_local(TODAY), standing=True)) == shape(build(today=TODAY, cutoff=at_local(TODAY), standing=True))
