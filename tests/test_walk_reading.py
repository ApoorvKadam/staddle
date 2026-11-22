"""Tilden on the walk, the core checks with the model's answer written by hand: a typed note is kept only as far as its quoted words are in
it, and an investigation's packet is kept only when every cited record is in the history it was given, its step is open for the kind of
finding, and no sentence says an action happened. Nothing here writes a record."""
from datetime import timedelta

import pytest

from core import reconcile, renting, walk, walk_reading as wr
from core.agent import Budget
from core.books import at_local
from core.seed import TODAY, build
from core.store import Store
from tests.walk_client import StandIn, history_ids, note_answer, packet

NOTE = "red overlock tag still on C335, tenant paid yesterday"
ROUTE = ("A101", "B210", "C335")


def keep(raw, note=NOTE, route=ROUTE, chosen=None):
    return wr.check_note(raw, note, route, chosen)


def test_a_field_is_kept_only_when_its_quoted_words_are_in_the_note_and_every_other_field_is_unknown():
    r = keep(note_answer(lock=("present", "a padlock"), whose=("facility overlock", "overlock"), tag=("present", "red overlock tag"),
                         door=("closed", "door was shut")))
    assert r.refusal is None and r.unit == "C335"
    assert r.fields == {"lock": "unknown", "whose": "facility overlock", "tag": "present", "door": "unknown", "contents": "unknown"}
    assert r.quotes == {"unit": "C335", "whose": "overlock", "tag": "red overlock tag"}
    assert sorted(r.dropped) == ["door", "lock"]                 # Tilden gave them, the note does not say them


def test_quotes_match_ignoring_case_spacing_and_curly_quotes_and_never_more():
    r = keep(note_answer(tag=("present", "RED   Overlock Tag")))
    assert r.fields["tag"] == "present"
    assert keep(note_answer(tag=("present", "a bright red overlock tag"))).fields["tag"] == "unknown"


def test_a_value_the_note_does_not_support_with_no_words_is_unknown():
    r = keep(note_answer(lock=("absent", None), tag=("present", "red overlock tag")))
    assert r.fields["lock"] == "unknown" and r.dropped == ["lock"]


def test_the_unit_is_kept_only_when_its_words_name_it_and_the_choice_of_unit_must_match():
    assert keep(note_answer(unit=("C335", "C335"), tag=("present", "red overlock tag"))).unit == "C335"
    guess = keep(note_answer(unit=("C335", "the end unit"), tag=("present", "red overlock tag")), chosen="B210")
    assert guess.unit == "B210" and "unit" not in guess.quotes       # the staff member chose B210; the guess from other words is not kept
    clash = keep(note_answer(tag=("present", "red overlock tag")), chosen="B210")
    assert clash.refusal == "The note names unit C335, but you are recording unit B210, so nothing was recorded."
    lower = keep(note_answer(unit=("c335", "c335"), tag=("present", "red overlock tag")), note="red tag on c335")
    assert lower.unit == "C335" and lower.quotes["unit"] == "c335"


def test_a_unit_that_is_not_on_the_route_is_refused_in_plain_words():
    r = keep(note_answer(unit=("D400", "D400"), tag=("present", "red overlock tag")), note="red overlock tag on D400")
    assert r.refusal == "Unit D400 is not on this walk's route, so nothing was recorded."
    none = keep(note_answer(unit=None, tag=("present", "red overlock tag")))
    assert none.refusal == "The note does not say which unit it is about, so nothing was recorded."


def test_words_aimed_at_the_system_more_than_one_unit_and_unsure_are_not_recorded():
    base = dict(tag=("present", "red overlock tag"))
    assert keep(note_answer(flags=["instructions_to_system"], **base)).refusal.startswith("The note is aimed at the system")
    assert keep(note_answer(flags=["more_than_one_unit"], **base)).refusal.startswith("The note is about more than one unit")
    assert keep(note_answer(flags=["unsure"], **base)).refusal.startswith("Tilden could not be sure")
    assert keep(note_answer()).refusal.startswith("The note does not say whether there was a lock")


def test_a_note_that_mentions_money_is_flagged_by_tilden_or_by_code_and_is_still_just_a_note():
    r = keep(note_answer(tag=("present", "red overlock tag"), flags=["mentions_money"]))
    assert r.refusal is None and "mentions_money" in r.flags
    by_code = keep(note_answer(tag=("present", "red overlock tag")), note="red overlock tag on C335, they say a $20 fee was wrong")
    assert "mentions_money" in by_code.flags and by_code.refusal is None
    assert "mentions_money" not in keep(note_answer(tag=("present", "red overlock tag"))).flags


def test_a_note_is_read_in_one_call_that_is_priced_and_a_failed_call_records_nothing():
    client = StandIn(note_answer(tag=("present", "red overlock tag")))
    budget = Budget()
    r = wr.read_note(client, "claude-haiku-5-5", "low", NOTE, ROUTE, budget=budget)
    assert r.fields["tag"] == "present" and len(client.asked) == 1 and len(budget.calls) == 1
    assert client.asked[0]["output_config"]["effort"] == "low" and client.asked[0]["model"] == "claude-haiku-5-5"
    assert (r.call.tokens_in, r.call.tokens_out) == (900, 120) and r.call.cents == pytest.approx((900 * 0.10 + 120 * 0.50) / 10_000)
    assert "<note>" in client.asked[0]["messages"][0]["content"] and "C335" in client.asked[0]["messages"][0]["content"]

    class Down:
        messages = None

        def create(self, **kw):
            raise TimeoutError("down")
    Down.messages = Down()
    failed = wr.read_note(Down(), "claude-haiku-5-5", "low", NOTE, ROUTE)
    assert failed.refusal.startswith("Tilden could not read the note") and failed.fields == {} and failed.call.cents > 0


# the investigation -----------------------------------------------------------------------------------------------------------------
def seeded():
    y = build(today=TODAY, cutoff=at_local(TODAY), standing=True)
    store = Store()
    with store.write():
        store.append("base", y.led.pending)
    books, rent = renting.load(store, "base")
    now = at_local(TODAY, 10)
    found = {f.slug: f for f in reconcile.compare(books, rent, now)}
    return y, books, rent, now, found


def test_the_history_is_a_fixed_list_of_records_each_with_an_id_made_for_this_run():
    y, books, rent, now, found = seeded()
    f = found["overlock_not_found"]
    h = wr.build_history(books, rent, f, now, "run1")
    assert [x["id"] for x in h] == [f"run1-{i}" for i in range(1, len(h) + 1)]
    kinds = [x["kind"] for x in h]
    assert {"lease", "overlock record", "ledger", "payment", "observation"} <= set(kinds)
    assert "The FMS has an overlock recorded on this unit" in next(x["text"] for x in h if x["kind"] == "overlock record")
    assert any("lock absent" in x["text"] and "by the front desk" in x["text"] for x in h if x["kind"] == "observation")
    again = wr.build_history(books, rent, f, now, "run2")
    assert [x["text"] for x in again] == [x["text"] for x in h] and {x["id"] for x in again}.isdisjoint({x["id"] for x in h})
    ol = wr.build_history(books, rent, found["overlock_on_paid"], now, "p")
    assert "Nothing is past due on the lease." in [x["text"] for x in ol]
    assert any("no move-out is recorded" in x["text"] for x in ol if x["kind"] == "move-out")


def test_the_history_of_a_vacant_unit_has_its_last_lease_its_observations_and_its_work_orders():
    y, books, rent, now, found = seeded()
    f = found["lock_on_vacant"]
    h = wr.build_history(books, rent, f, now, "v")
    assert [x["kind"] for x in h].count("lease") >= 1 and "ended" in next(x["text"] for x in h if x["kind"] == "lease")
    assert not [x for x in h if x["kind"] in ("ledger", "payment", "overlock record")]       # no lease is active to read them from
    repair_unit = y.walked["repair"]
    f2 = reconcile.Finding(repair_unit, "lock_on_vacant", "k", "w", "y", "c", "front_desk", now.date(), "O1")
    assert any(x["kind"] == "work order" and "latch" in x["text"] for x in wr.build_history(books, rent, f2, now, "w"))


def ids_for(f, books, rent, now):
    h = wr.build_history(books, rent, f, now, "t")
    return h, {x["id"] for x in h}


def test_a_packet_that_cites_records_in_the_history_with_an_open_step_and_no_claimed_action_passes():
    y, books, rent, now, found = seeded()
    f = found["overlock_on_paid"]
    h, ids = ids_for(f, books, rent, now)
    client = StandIn(lambda c: packet(history_ids(c)[:2]))
    inv = wr.investigate(client, "claude-haiku-5-5", "low", f, h, "t")
    assert inv.dropped is None and inv.packet["step"] == "check_payment" and inv.packet["records"] == ["t-1", "t-2"]
    assert inv.call.cents > 0 and (inv.call.tokens_in, inv.call.tokens_out) == (900, 120)
    asked = client.asked[0]["messages"][0]["content"]
    assert "take_lock_off" in asked and "check_payment" in asked and "<history>" in asked and "t-1 [lease]" in asked


@pytest.mark.parametrize("change, why", [
    ({"records": ["t-1", "z-99"]}, "the packet cites a record that is not in the unit's history"),
    ({"records": []}, "the packet cites no record"),
    ({"step": "check_move_out"}, "the packet's next step is not one open for this kind of finding (check_move_out)"),
    ({"step": "close_the_unit"}, "the packet's next step is not one open for this kind of finding (close_the_unit)"),
    ({"reason": "because"}, "the packet's reason is not on the list"),
    ({"summary": ""}, "the packet had no summary or no statement of the contradiction"),
    ({"summary": "I looked at the ledger and it shows nothing past due."}, "the packet speaks in the first person"),
    ({"contradiction": "We saw an overlock on a paid unit."}, "the packet speaks in the first person"),
    ({"summary": "The lock was removed from the door by the tenant."}, "the packet says an action was taken, and nothing has been done"),
    ({"summary": "The overlock has been lifted and the unit is clear."}, "the packet says an action was taken, and nothing has been done"),
    ({"summary": "The tenant was refunded the late fee."}, "the packet says an action was taken, and nothing has been done"),
    ({"summary": "Rent was collected on Nov 3 and the unit was cleared."}, "the packet says an action was taken, and nothing has been done"),
    ({"summary": "The padlock was taken off."}, "the packet says an action was taken, and nothing has been done"),
    ({"summary": "Staff removed the overlock, and no payment is due."}, "the packet says an action was taken, and nothing has been done"),
    ({"summary": "x" * 800}, "the packet was too long"),
])
def test_a_packet_that_fails_a_check_is_dropped_with_the_plain_reason(change, why):
    y, books, rent, now, found = seeded()
    f = found["overlock_on_paid"]
    h, ids = ids_for(f, books, rent, now)
    client = StandIn(lambda c: {**packet(history_ids(c)[:1]), **change})
    inv = wr.investigate(client, "claude-haiku-5-5", "low", f, h, "t")
    assert inv.packet is None and inv.dropped == why


def test_what_staff_should_do_may_be_said_and_only_what_was_done_may_not():
    for ok in ("Staff should take the lock off and record it.", "The overlock is on record but no lock was found, so staff look again.",
               "No lock was removed; staff have not yet checked.", "The ledger shows the late fee was charged on Nov 6 and paid on Nov 9.",
               "Once staff confirm the payment, the overlock can be lifted.", "The tenant paid Nov 3; nothing was refunded."):
        assert not wr.says_done(ok), ok
    for bad in ("The lock was removed.", "The overlock was lifted yesterday.", "The unit was cleared.", "The door was unlocked and the tag taken off.",
                "Staff have charged the tenant $20.", "Money was refunded to the tenant.", "The payment was collected."):
        assert wr.says_done(bad), bad


def test_a_step_is_open_to_a_finding_only_by_the_table_and_every_step_and_reason_is_in_words():
    assert set(wr.ALLOWED) == {"overlock_on_paid", "overlock_not_found", "lock_on_vacant", "door_open", "look_again", "contents_on_vacant", "door_damaged"}
    assert "take_lock_off" in wr.ALLOWED["overlock_on_paid"] and "take_lock_off" not in sum((wr.ALLOWED[k] for k in wr.ALLOWED if k != "overlock_on_paid"), ())
    assert all(s in wr.STEPS for steps in wr.ALLOWED.values() for s in steps) and all(wr.STEPS.values()) and all(wr.REASONS.values())


def test_an_unreadable_or_failed_investigation_is_a_dropped_packet_not_an_error():
    y, books, rent, now, found = seeded()
    f = found["overlock_on_paid"]
    h, _ = ids_for(f, books, rent, now)

    class Down:
        messages = None

        def create(self, **kw):
            raise TimeoutError("down")
    Down.messages = Down()
    inv = wr.investigate(Down(), "claude-haiku-5-5", "low", f, h, "t")
    assert inv.packet is None and inv.dropped.startswith("the packet could not be read") and inv.call.cents > 0


def test_the_investigation_writes_nothing_and_reads_the_records_only():
    y, books, rent, now, found = seeded()
    f = found["overlock_on_paid"]
    before = (len(books.charges), len(books.payments))
    h, _ = ids_for(f, books, rent, now)
    wr.investigate(StandIn(lambda c: packet(history_ids(c)[:1])), "claude-haiku-5-5", "low", f, h, "t")
    assert (len(books.charges), len(books.payments)) == before


def test_a_reason_the_records_contradict_drops_the_packet_and_only_true_reasons_are_offered():
    y, books, rent, now, found = seeded()
    f = found["overlock_on_paid"]                                  # paid up, and no overlock recorded
    h, ids = ids_for(f, books, rent, now)
    facts = wr.reason_facts(books, rent, f, now)
    assert facts["ledger_shows_paid"] and facts["no_overlock_on_record"] and not facts["overlock_on_record"] and not facts["ledger_shows_owed"]
    client = StandIn(lambda c: packet(history_ids(c)[:1], step="look_again", reason="overlock_on_record"))
    inv = wr.investigate(client, "claude-haiku-5-5", "low", f, h, "t", facts=facts)
    assert inv.packet is None and inv.dropped == "the packet's reason does not match the records (an overlock is on record)"
    asked = client.asked[0]["messages"][0]["content"]
    assert "no_overlock_on_record" in asked and "overlock_on_record (" not in asked.replace("no_overlock_on_record (", "")
    ok = wr.investigate(StandIn(lambda c: packet(history_ids(c)[:1], reason="ledger_shows_paid")), "claude-haiku-5-5", "low", f, h, "t", facts=facts)
    assert ok.dropped is None


def test_a_negated_action_in_its_own_clause_is_not_a_claim_that_it_happened():
    assert not wr.says_done("The overlock was not removed.") and not wr.says_done("No lock was removed from the door.")
    assert wr.says_done("Nothing is past due, and the lock was removed.")


def test_a_guess_in_the_note_is_not_what_was_seen_so_that_field_stays_unknown():
    note = "Padlock on the door of C335 in the photo, no facility tag or mark on it. Staff think it is our overlock."
    r = keep(note_answer(lock=("present", "Padlock on the door"), whose=("facility overlock", "Staff think it is our overlock"),
                         tag=("absent", "no facility tag")), note=note)
    assert r.refusal is None and r.fields["whose"] == "unknown" and "whose" in r.dropped
    assert (r.fields["lock"], r.fields["tag"]) == ("present", "absent")          # what was seen is still kept


def test_work_the_note_names_is_kept_on_its_own_words_even_when_nothing_about_the_lock_is_said():
    note = "C335: loose trash, a stained floor and a broken light."
    r = keep(note_answer(work=[("cleaning", "loose trash, a stained floor"), ("repair", "a broken light"), ("repair", "a leaking roof")]), note=note)
    assert r.refusal is None
    assert r.work == [{"kind": "cleaning", "words": "loose trash, a stained floor"}, {"kind": "repair", "words": "a broken light"}]


def test_belongings_left_behind_never_become_cleaning_work_and_a_guess_is_no_work():
    note = "C335: old boxes and a mattress left behind by the last tenant; the light might be broken."
    r = keep(note_answer(contents=("seen", "old boxes and a mattress left behind"), work=[("cleaning", "old boxes and a mattress"),
                                                                                           ("repair", "the light might be broken")]), note=note)
    assert r.fields["contents"] == "seen" and r.work == [] and "work" in r.dropped


def test_a_note_that_cannot_be_kept_proposes_no_work():
    note = "C335: loose trash and a broken light."
    r = keep(note_answer(work=[("cleaning", "loose trash"), ("repair", "a broken light")], flags=["unsure"]), note=note)
    assert r.refusal and r.work == []


def test_a_guess_is_read_in_its_own_sentence_so_a_plain_fact_beside_it_stays():
    note = "C335: Door closed. Staff think it is our lock."
    r = keep(note_answer(whose=("facility overlock", "our lock"), door=("closed", "Door closed")), note=note)
    assert (r.fields["whose"], r.fields["door"]) == ("unknown", "closed")


def test_cleaning_said_in_the_same_sentence_as_the_belongings_is_dropped():
    note = "C335: Boxes and furniture remain inside. The floor is dusty."
    r = keep(note_answer(contents=("seen", "Boxes"), work=[("cleaning", "furniture"), ("cleaning", "The floor is dusty")]), note=note)
    assert r.work == [{"kind": "cleaning", "words": "The floor is dusty"}] and "work" in r.dropped
