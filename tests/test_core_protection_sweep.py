"""The daily check: policy evidence past its expiry date moves to expired and keeps its history; the plan charges are read against enrolments,
covered periods and lease end dates by the work queue's own check, which the desk runs right after this one."""
from datetime import date

from core import protection, work
from core.billing import Ledger
from core.books import PROTECTION, at_local
from desk.front_desk import FrontDesk
from tests.protection_world import FRONT, MANAGER, STREAM, World, day

DOC = {"carrier": "Sunrise Mutual", "number": "8A-44521", "effective": date(2025, 11, 1), "expires": date(2026, 12, 31), "document": "declarations page.pdf"}
REVIEW = ["carrier", "policy number", "dates"]


def put(w, k, at, **over):
    return w.act(lambda led: protection.record_evidence(led, w.leases[k], FRONT, at, **{**DOC, **over}))


def test_a_policy_past_its_expiry_becomes_expired_with_its_history_and_a_second_pass_writes_nothing():
    w = World()
    ev = put(w, 0, day(10, 1))
    w.act(lambda led: protection.review_evidence(led, ev, MANAGER, day(10, 2), "accepted", "Read the declarations page for all three.", REVIEW))
    assert protection.sweep(w.store, STREAM, day(12, 30)) == 0 and w.state().evidence[ev].status == "reviewed"          # still in force on its last day
    assert protection.sweep(w.store, STREAM, day(1, 2, year=2027)) == 1
    rec = w.state().evidence[ev]
    assert rec.status == "expired" and [h["status"] for h in rec.history] == ["received", "reviewed", "expired"] and rec.review["by"] == MANAGER
    assert protection.sweep(w.store, STREAM, day(1, 3, year=2027)) == 0


def test_an_unreviewed_document_past_its_expiry_expires_too_and_a_statement_or_rejected_record_never_does():
    w = World()
    unreviewed = put(w, 0, day(10, 1))
    statement = w.act(lambda led: protection.record_evidence(led, w.leases[1], FRONT, day(10, 1), statement="I am covered through my car policy."))
    rejected = put(w, 2, day(10, 1), number="TEMP")
    w.act(lambda led: protection.review_evidence(led, rejected, MANAGER, day(10, 2), "rejected", "", [], "Placeholder number."))
    assert protection.sweep(w.store, STREAM, day(1, 2, year=2027)) == 1
    st = w.state().evidence
    assert (st[unreviewed].status, st[statement].status, st[rejected].status) == ("expired", "reported", "rejected")


def test_the_check_touches_no_enrolment_charge_or_money_and_the_stale_plan_is_found_by_the_queue():
    w = World()
    e = w.accepted_and_active(0, day(11, 1), starts=date(2026, 11, 1))
    w.act(lambda led: led.end_lease(day(11, 10), w.leases[0]))
    stray = w.act(lambda led: led.charge_by_hand(day(11, 12), w.leases[1], PROTECTION, 1200, f"protection:{w.leases[1]}:2026-11", date(2026, 11, 12)))
    before = (w.store.journal(STREAM), w.state().enrolments[e].state, len(w.books().charges))
    protection.sweep(w.store, STREAM, day(11, 13))
    work.sweep(w.store, STREAM, day(11, 13))
    assert (w.store.journal(STREAM), w.state().enrolments[e].state, len(w.books().charges)) == before
    books, rent, wk = work.load(w.store, STREAM)
    keys = {i.key for i in work.open_items(work.queue(books, rent, wk, day(11, 13)))}
    assert {f"protection:ended-lease:{e}", f"protection:charge:{stray}"} <= keys                 # each a dated item, found against the records


def test_the_desks_daily_pass_runs_the_protection_check_before_the_work_queue_check(tmp_path):
    today = date(2026, 12, 3)
    desk = FrontDesk(str(tmp_path / "core.db"), clock=lambda: at_local(today, 10))
    store = desk.ready()
    lease = desk.cast["maria"]["leases"][0]
    with store.write():
        led = Ledger(store.books("visitor:v1"))
        ev = protection.record_evidence(led, lease, FRONT, at_local(date(2026, 10, 1), 10), **{**DOC, "expires": date(2026, 11, 30)})
        store.append("visitor:v1", led.pending)
    assert desk.sweep() == ["visitor:v1"]
    books = store.books("visitor:v1")
    assert protection.state(books).evidence[ev].status == "expired"
    count = len(store.stream_events("visitor:v1"))
    desk.sweep()
    assert len(store.stream_events("visitor:v1")) == count
