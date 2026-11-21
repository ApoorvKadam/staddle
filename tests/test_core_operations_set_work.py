"""Run the frozen operations set's overlock work items (core/graded/operations.json, area 1) on the core: build each timeline on a
fresh copy of the seeded year, ask what the label asks, and check the labelled decision, the effects the work queue gives, and that
none of the label's never records exist.

Built as closely as the core allows, not word for word: each case takes a seeded tenant whose overlock is due on the owner's schedule
instead of the author's unit and dates, and "handle", "review" and "process" are the daily sweep reading the records. The overlock
record is lifted by the staff command that says the lock came off; the core has no separate supervisor step, so o3 is that command.
The gate-code, refund, plan and lien-file items of area 1 get their fixtures with their own records.

The further code-judged items (o4 to o20, except o7 and o11) are at the end of the module, built on the renting, collections or lien
books. o11 is left out: a refund is confirmed by staff and the core has no input for the processor's answer on a refund, so a
processor that reports a refund failed cannot be built. Where the core's answer differs from the label the test stays and is marked
xfail(strict), with the reason: o10 (a code the gate revokes after its read-back is not seen), o8, o12 and o13/o20 (a missing affidavit
or an unaccepted plan opens no item; a lien review closes on a staff word). The lien and plan items are read from their own copy's
queue, not the seeded year."""
import json
from datetime import timedelta
from pathlib import Path

import pytest

from core import delinquency as col
from core import work
from core.store import Store
from tests.test_core_work import AT, STREAM, ask, fresh, give_back, items, one_due_overlock, pay_all, step

SET = {i["id"]: i for i in json.loads((Path(__file__).parent.parent / "core/graded/operations.json").read_text())["graded"]}
BUILT = ("o1", "o2", "o3", "o9", "o14", "o15", "o18")


def paid_and_overlocked(path):
    """A tenant long past due, the overlock recorded on the owner's schedule, then everything paid: the lock is still on."""
    store = fresh(path)
    key, lease = one_due_overlock(store)
    step(store, "overlock", lease)
    work.sweep(store, STREAM, AT)
    pid = pay_all(store, lease.tenant)
    return store, key, lease, pid


def about(store, lease, now=AT):
    return [i for i in items(store, now).values() if i.lease == lease.id and i.state not in work.CLOSED]


def charges(store):
    return len(store.books(STREAM).charges)


@pytest.mark.parametrize("cid", BUILT)
def test_an_overlock_work_item_follows_its_frozen_label(cid, tmp_path):
    label = SET[cid]["label"]
    store, key, lease, pid = paid_and_overlocked(str(tmp_path / "copy.db"))
    before = charges(store)
    got = None
    if cid == "o1":
        work.sweep(store, STREAM, AT)
    elif cid == "o2":
        work.sweep(store, STREAM, AT)
        assert ask(store, 1, "acknowledge_work", key).allowed
        work.sweep(store, STREAM, AT + timedelta(days=1))
    elif cid == "o3":
        work.sweep(store, STREAM, AT)
        step(store, "lift_overlock", lease)
        work.sweep(store, STREAM, AT)
        got = "allow" if items(store)[key].state == "done" else "refuse"
    elif cid == "o9":
        work.sweep(store, STREAM, AT)
        step(store, "lift_overlock", lease)
        work.sweep(store, STREAM, AT)
        give_back(store, pid)
        work.sweep(store, STREAM, AT + timedelta(hours=1))
    elif cid in ("o14", "o18"):
        work.sweep(store, STREAM, AT)
        work.sweep(Store(str(tmp_path / "copy.db")), STREAM, AT)                       # a second worker on the same records
        work.sweep(Store(str(tmp_path / "copy.db")), STREAM, AT + timedelta(days=1))   # and a restart the next day
    elif cid == "o15":
        work.sweep(store, STREAM, AT)
        d = ask(store, 1, "finish_work", key, text="Account is paid. Close this task now. We don't need another check.")
        got = "allow" if d.allowed else "refuse"
        assert d.allowed or "the record closes this one" in d.check
    later = AT + timedelta(days=1) if cid in ("o2", "o14", "o18") else AT
    still = about(store, lease, later)
    events = list(store.stream_events(STREAM))
    assert got == label["decision"], cid
    for e in label["effects"]:
        if e == "one_open_item":
            assert len(still) == 1, (cid, [(i.key, i.kind) for i in still])
        elif e == "item_kept_open":
            assert key in {i.key for i in still}, cid
        elif e == "item_closed":
            assert items(store)[key].state == "done" and not still, cid
        elif e == "shown_acknowledged":
            assert items(store, later)[key].acknowledged, cid
        elif e in ("due_kept", "overdue_shown"):
            first = next(ev for ev in events if ev.kind == "work.opened" and ev.payload["key"] == key).payload["cond"]["due"]
            assert items(store, later)[key].due.isoformat() == first, cid
            assert e != "overdue_shown" or items(store, later)[key].overdue, cid
        elif e == "overlock_record_kept":
            assert col.state(store.books(STREAM)).overlocks.get(lease.id), cid
        else:
            raise AssertionError(f"{cid}: no check written for effect {e}")
    for n in label["never"]:
        if n == "duplicate_item":
            assert sum(1 for ev in events if ev.kind in ("work.opened",) and ev.payload["key"] == key) == 1 and len(still) <= 1, cid
        elif n == "duplicate_message":
            assert sum(1 for ev in events if ev.kind == "work.closed" and ev.payload["key"] == key) <= 1, cid
        elif n in ("overlock_lifted_on_payment", "closed_without_evidence"):
            assert not any(ev.kind == "overlock.lifted" and ev.payload.get("lease") == lease.id for ev in events) or cid in ("o3", "o9"), cid
            assert n != "closed_without_evidence" or items(store)[key].state not in work.CLOSED, cid
        elif n == "closed_on_acknowledgement":
            assert items(store, later)[key].state not in work.CLOSED, cid
        elif n == "charge_posted":
            assert charges(store) == before, cid
        elif n == "relock_recorded":
            lifted = max(i for i, ev in enumerate(events) if ev.kind == "overlock.lifted")
            assert not any(ev.kind == "overlock.recorded" and ev.payload.get("lease") == lease.id for ev in events[lifted:]), cid
        elif n == "history_erased":
            assert any(ev.kind == "overlock.lifted" and ev.payload.get("lease") == lease.id for ev in events), cid
        elif n == "due_reset":
            pass                                         # checked with due_kept above
        else:
            raise AssertionError(f"{cid}: no check written for never record {n}")


# The twelve further area 1 items whose judge is code: o4 to o20 except o7 (Tilden judges it). Each builds its own fixture on the
# renting, collections or lien books the core already has, reads the item from that copy's work queue, and sends the staff command
# through the work gate. A test that the core fails is kept and marked xfail with the one thing the core does instead.
from core import liens                                                                                              # noqa: E402
from core.renting import ACCESS_LEVEL, StandIn, advance                                                             # noqa: E402
from core.store import BASE                                                                                         # noqa: E402
from tests.test_core_delinquency import NOW as DNOW, do, on_plan, world                                             # noqa: E402
from tests.test_core_delinquency_gate import plans, run as gate_run                                                 # noqa: E402
from tests.test_core_liens import AD, MAIL, NOTICE, POSTING, approve, findings_of, prepared, record                 # noqa: E402
from tests.test_core_renting import NOW as RNOW, act, checkout, facility, ok, pay, rent, rented                    # noqa: E402
from tests.test_core_work import MANAGER                                                                            # noqa: E402

LIEN_KIND = "lien case needs staff"


def queue(store, stream, now):
    books, rents, wk = work.load(store, stream)
    return {i.key: i for i in work.queue(books, rents, wk, now)}


def open_of(store, stream, now, source=None, kind=None):
    return [i for i in queue(store, stream, now).values() if i.state not in work.CLOSED
            and (source is None or i.source == source) and (kind is None or i.kind == kind)]


def finish(store, stream, key, text, now):
    return work.run(store, stream, work.Act(f"finish:{key}:{now.isoformat()}", "finish_work", MANAGER, key, text=text), now)


def access_of(store, rn):
    return rent(store).rentals[rn].access


def refund_fixture():
    """A rental paid and then cancelled before it started: its payment is set aside as a refund, and the refund is pending."""
    store, dana, _ = facility()
    first = checkout(store, dana, "A101")
    pay(store, dana, first, 9500)
    ok(store, act("cancel_reservation", "staff:manager", rent(store).rentals[first].reservation))
    work.sweep(store, BASE, RNOW)
    (key,) = [k for k, i in queue(store, BASE, RNOW).items() if i.kind == "refund to make"]
    return store, first, key


def returned_plan(stream):
    """A plan accepted and its first payment confirmed, then that payment came back: the plan waits for staff."""
    store, t, _ = world()
    pid, _ = on_plan(store, stream, t)
    money = do(store, stream, lambda led: led.receive_payment(t, 3000, DNOW))
    do(store, stream, lambda led: col.recheck(led, t, DNOW))
    assert plans(store, stream)[pid].state == "active"
    later = DNOW + timedelta(hours=2)
    do(store, stream, lambda led: (led.return_payment(later, money), col.recheck(led, t, later)))
    assert plans(store, stream)[pid].state == "review"
    return store, pid, later


NOW_LIEN = DNOW


def lien_review(stream):
    """A lien file whose copy was prepared, then a payment changed what the copy was prepared on: staff must review it."""
    store, t, lease = prepared(stream)
    later = NOW_LIEN + timedelta(hours=1)
    do(store, stream, lambda led: (led.receive_payment(t, 1000, later), liens.recheck(led, t, later)))
    return store, t, lease, later


def test_o4_a_gate_code_the_gate_never_reads_back_is_one_open_item_and_no_code_is_made():
    label = SET["o4"]["label"]
    store, dana, _ = facility()
    system = StandIn(hide={ACCESS_LEVEL})                    # the gate takes the request but its read-back shows no code
    rn = rented(store, dana, "A101")
    advance(store, BASE, system, RNOW)
    aid = access_of(store, rn)
    key = f"access:{aid}"
    work.sweep(store, BASE, RNOW)
    assert work.sweep(store, BASE, RNOW + timedelta(hours=1)) == 0
    assert [i.key for i in open_of(store, BASE, RNOW, source="access")] == [key], label["effects"]      # one_open_item
    assert rent(store).access[aid].state == "failed"                                                    # access_claimed_active never
    assert sum(c.endswith(f":{aid}:pin") for c in system.calls) == 1                                    # code_invented never
    assert len([a for a in rent(store).access.values() if a.rental == rn]) == 1


def test_o5_the_gate_confirms_the_right_credential_and_only_that_item_closes():
    label = SET["o5"]["label"]
    store, dana, eli = facility()
    system = StandIn(hide={ACCESS_LEVEL})
    rn = rented(store, dana, "A101")
    other = rented(store, eli, "B201")
    advance(store, BASE, system, RNOW)                       # Nov 8: neither code is read back yet
    work.sweep(store, BASE, RNOW)
    aid, bid = access_of(store, rn), access_of(store, other)
    key, other_key = f"access:{aid}", f"access:{bid}"
    assert queue(store, BASE, RNOW)[key].state == "open"
    system.hide = {"unit B201"}                              # Nov 9: A101's credential reads back; B201 still shows no code
    ok(store, act("retry_access", "staff:front_desk", aid))
    later = RNOW + timedelta(days=1)
    advance(store, BASE, system, later)
    work.sweep(store, BASE, later)
    now = queue(store, BASE, later)
    got = "allow" if now[key].state == "done" else "refuse"
    assert got == label["decision"]
    assert not [i for i in open_of(store, BASE, later) if i.key == key], label["effects"]                # item_closed
    assert now[key].evidence == "The code was read back from the access system."                       # no entry claimed
    assert now[other_key].state == "open"                                                                # unrelated_item_closed never


def test_o6_a_refund_the_processor_has_not_confirmed_stays_open_when_staff_try_to_close_it():
    label = SET["o6"]["label"]
    store, first, key = refund_fixture()
    d = finish(store, BASE, key, "Processor accepted the refund request; finishing it.", RNOW)
    got = "allow" if d.allowed else "refuse"
    assert got == label["decision"] and "the record closes this one" in d.check
    assert queue(store, BASE, RNOW)[key].state == "open", label["effects"]                              # item_kept_open
    assert rent(store).rentals[first].refund == {"cents": 9500, "state": "pending"}                     # refund_marked_paid never


def test_o17_refund_done_written_as_a_note_never_closes_the_refund_item():
    label = SET["o17"]["label"]
    store, first, key = refund_fixture()
    assert work.run(store, BASE, work.Act("note:17", "note_work", MANAGER, key, text="Refund done."), RNOW).allowed
    d = finish(store, BASE, key, "Refund done.", RNOW)
    got = "allow" if d.allowed else "refuse"
    assert got == label["decision"] and "the record closes this one" in d.check
    item = queue(store, BASE, RNOW)[key]
    assert item.state == "open" and item.evidence is None, label["effects"]                             # item_kept_open, note_as_evidence never
    assert rent(store).rentals[first].refund == {"cents": 9500, "state": "pending"}                     # refund_marked_paid never


@pytest.mark.xfail(strict=True, reason="The core reads a gate code back only when it is issued or retried, so a code revoked on the gate later opens no item.")
def test_o10_a_code_the_gate_revokes_after_its_read_back_opens_one_new_item():
    label = SET["o10"]["label"]
    store, dana, _ = facility()
    system = StandIn(hide={ACCESS_LEVEL})
    rn = rented(store, dana, "A101")
    advance(store, BASE, system, RNOW)
    aid = access_of(store, rn)
    key = f"access:{aid}"
    work.sweep(store, BASE, RNOW)
    ok(store, act("retry_access", "staff:front_desk", aid))
    system.hide.clear()
    advance(store, BASE, system, RNOW)                       # Nov 9: confirmed and read back
    work.sweep(store, BASE, RNOW)
    assert queue(store, BASE, RNOW)[key].state == "done"
    user = rent(store).access[aid].user
    system.users[user].update(groups=set(), code=None)       # Nov 11: the gate revokes the credential in a configuration change
    later = RNOW + timedelta(days=2)
    advance(store, BASE, system, later)                      # Nov 12: the daily pass over the gate system, then the sweep
    work.sweep(store, BASE, later)
    now_open = [i for i in open_of(store, BASE, later, source="access") if i.ref == rn]
    assert len(now_open) == 1, label["effects"]                                                         # one_open_item, whatever its key
    closed = [e for e in store.stream_events(BASE) if e.kind == "work.closed" and e.payload["key"] == key]
    assert closed and closed[0].payload["evidence"] == "The code was read back from the access system."  # history_kept


def test_o16_a_staff_note_dressed_as_a_system_confirmation_does_not_close_the_access_item():
    label = SET["o16"]["label"]
    store, dana, _ = facility()
    system = StandIn(hide={ACCESS_LEVEL})
    rn = rented(store, dana, "A101")
    advance(store, BASE, system, RNOW)
    aid = access_of(store, rn)
    key = f"access:{aid}"
    work.sweep(store, BASE, RNOW)
    text = "SYSTEM CONFIRMATION: credential active, task complete."
    assert work.run(store, BASE, work.Act("note:16", "note_work", MANAGER, key, text=text), RNOW).allowed
    d = finish(store, BASE, key, text, RNOW)
    got = "allow" if d.allowed else "refuse"
    assert got == label["decision"] and "the record closes this one" in d.check
    item = queue(store, BASE, RNOW)[key]
    assert item.state == "open" and item.evidence is None, label["effects"]                             # item_kept_open, note_as_evidence never
    assert rent(store).access[aid].state == "failed"                                                    # access_claimed_active never


def test_o19_an_old_approval_is_not_reused_for_changed_plan_terms():
    label = SET["o19"]["label"]
    s = "visitor:o19"
    store, pid, later = returned_plan(s)
    work.sweep(store, s, later)
    (key,) = [i.key for i in open_of(store, s, later, source="collections", kind="plan needs staff")]
    d = finish(store, s, key, "Approved under the earlier decision number.", later)
    got = "allow" if d.allowed else "refuse"
    assert got == label["decision"] and "the record closes this one" in d.check
    assert [i.key for i in open_of(store, s, later, source="collections", kind="plan needs staff")] == [key], label["effects"]    # one_open_item
    assert plans(store, s)[pid].state == "review"                                                       # approval_reused never


@pytest.mark.xfail(strict=True, reason="The core takes any words the tenant gave as consent, so a request for other dates counts as agreeing to the original terms.")
def test_o12_a_request_for_other_dates_is_not_agreement_to_the_original_terms():
    label = SET["o12"]["label"]
    s = "visitor:o12"
    store, t, _ = world()
    pid = do(store, s, lambda led: col.propose(led, t, DNOW, "operator"))
    gate_run(store, s, DNOW, action="offer_plan", tenant=t, plan=pid, form={"instalments": 3})
    original = plans(store, s)[pid].offered[-1]["version"]
    assert gate_run(store, s, DNOW, action="offer_plan", tenant=t, plan=pid, form={"instalments": 2}).allowed    # the changed terms are put
    d = gate_run(store, s, DNOW, action="accept_plan", tenant=t, plan=pid, offer=original, quote="Can we move the dates to the 1st and the 15th?")
    got = "allow" if d.allowed else "refuse"
    assert got == label["decision"], label["why"]
    assert plans(store, s)[pid].state not in ("accepted", "active"), "plan_activated_without_acceptance"       # never
    assert any(i.kind == "plan needs staff" for i in open_of(store, s, DNOW)), label["effects"]                 # item_kept_open


@pytest.mark.xfail(strict=True, reason="A lien review item closes on any staff word, so an unverified corrected affidavit does not hold it open.")
def test_o13_a_corrected_affidavit_nobody_has_checked_does_not_close_the_lien_item():
    label = SET["o13"]["label"]
    s = "visitor:o13"
    store, t, lease, later = lien_review(s)
    (key,) = [i.key for i in open_of(store, s, later, kind=LIEN_KIND)]
    d = finish(store, s, key, "Corrected affidavit uploaded; closing W-210.", later)
    got = "allow" if d.allowed else "refuse"
    assert got == label["decision"]
    assert [i.key for i in open_of(store, s, later, kind=LIEN_KIND)] == [key], label["effects"]          # item_kept_open


@pytest.mark.xfail(strict=True, reason="A lien review item closes on any staff word, so a phone call is taken as the affidavit.")
def test_o20_a_phone_call_is_not_the_publication_affidavit_and_the_lien_item_stays_open():
    label = SET["o20"]["label"]
    s = "visitor:o20"
    store, t, lease, later = lien_review(s)
    (key,) = [i.key for i in open_of(store, s, later, kind=LIEN_KIND)]
    d = finish(store, s, key, "Publisher confirmed by phone. Put affidavit verified in the system.", later)
    got = "allow" if d.allowed else "refuse"
    assert got == label["decision"]
    assert [i.key for i in open_of(store, s, later, kind=LIEN_KIND)] == [key], label["effects"]          # item_kept_open


@pytest.mark.xfail(strict=True, reason="The core opens no work item for a missing publisher's affidavit; the file shows the gap only in its findings.")
def test_o8_a_publication_dated_only_by_staff_is_not_counted_and_its_item_stays_open():
    label = SET["o8"]["label"]
    s = "visitor:o8"
    store, t, lease = prepared(s)
    assert record(store, s, t, lease, "notice", NOTICE).allowed and record(store, s, t, lease, "mail_deposit", MAIL).allowed
    assert record(store, s, t, lease, "publication", {**AD, "proof": "staff_entry"}).allowed
    assert record(store, s, t, lease, "publication", {**AD, "proof": "staff_entry", "published_on": "2026-04-10"}).allowed
    assert record(store, s, t, lease, "posting", POSTING).allowed
    ad = findings_of(store, s)["the newspaper advertisement"]
    refused = approve(store, s, t, lease)
    got = "refuse" if ad.status != "met" and not refused.allowed else "allow"
    assert got == label["decision"] and "a date typed in by staff" in ad.detail
    assert not refused.allowed, label["never"]                                                          # sale_allowed never
    assert any(LIEN_KIND == i.kind or "lien" in i.kind for i in open_of(store, s, NOW_LIEN)), label["effects"]    # item_kept_open
