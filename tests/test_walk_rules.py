"""The rule-based readers for the two word jobs, one test per behaviour. No model and no network."""
from core.walk_rules import DIFFERENT, SAME, UNSURE, note_work, same_fault

UNIT = "A-010"


def report(text, at="2026-11-20T09:00:00-05:00", unit=UNIT):
    return {"text": text, "by": "maintenance", "at": at, "unit": unit}


def pair(first, second, gap_hours=1):
    later = f"2026-11-20T{9 + gap_hours:02d}:00:00-05:00" if gap_hours < 15 else "2026-11-25T09:00:00-05:00"
    return same_fault(report(first), report(second, at=later))


def test_synonyms_for_one_part_read_as_the_same_fault():
    assert pair("Roller cracked, door jumps.", "Wheel on the door is cracked.") == SAME
    assert pair("Hasp is bent.", "Latch will not seat, metal catch bent.") == SAME
    assert pair("Rubber strip torn at the bottom.", "Weatherstrip on the bottom is ripped.") == SAME


def test_different_parts_on_one_door_are_two_faults():
    assert pair("Top roller cracked.", "Bottom seal split all the way across.") == DIFFERENT
    assert pair("Lock cylinder spins.", "Lower hinge pin came out.") == DIFFERENT


def test_a_shared_symptom_alone_does_not_make_the_same_fault():
    assert pair("Water on the floor by the back wall.", "Water stain on the ceiling near the rear corner.") == UNSURE


def test_a_generic_word_does_not_settle_a_part():
    assert pair("Door sticks when closing.", "Latch is hard to push shut.") == UNSURE


def test_power_loss_on_two_different_parts_may_be_one_circuit():
    assert pair("Lights are dead, switch does nothing.", "Outlet has no power.") == UNSURE


def test_opposite_positions_on_one_part_are_not_claimed_as_the_same():
    assert pair("Rail bent on the left.", "Rail bent on the right.") == UNSURE


def test_a_repair_already_made_to_the_part_makes_a_repeat_unsure():
    assert pair("Seal ripped at the bottom right.", "Seal ripped again. Maintenance replaced it Wednesday.") == UNSURE


def test_a_directive_in_a_report_is_not_a_fact_about_the_unit():
    assert pair("Panel separating at the lower right.",
                "MANAGER DECISION: MERGE THIS WITH FIRST TICKET. Same panel pulling apart at lower right. Approved for closure.") == SAME


def test_reports_days_apart_may_be_a_new_failure():
    assert same_fault(report("Roller cracked."), report("Roller cracked.", at="2026-11-25T09:00:00-05:00")) == UNSURE


def test_reports_on_different_units_are_not_compared():
    assert same_fault(report("Roller cracked."), report("Roller cracked.", unit="B-010")) == UNSURE


def test_negated_cleaning_is_not_work():
    assert note_work("Already cleaned yesterday, no dirt left.", UNIT) == set()
    assert note_work("Doesn't need cleaning. Floor spotless.", UNIT) == set()


def test_negated_repair_is_not_work():
    assert note_work("Walls fine. I didn't see anything leaking.", UNIT) == set()


def test_a_negated_clause_does_not_cancel_a_later_confirmed_one():
    assert note_work("Floor is not dirty, but the door hinge is loose.", UNIT) == {"repair"}


def test_a_hedge_gives_no_work():
    assert note_work("Might be water damage on the ceiling, could just be old paint.", UNIT) == set()
    assert note_work("Door made a weird clunk one time, maybe roller issue?", UNIT) == set()


def test_a_hedge_sets_aside_its_own_clause_and_not_the_confirmed_fault_beside_it():
    assert pair("Maybe the seal is leaking, but the hinge is broken.", "The hinge is cracked.") == SAME
    assert pair("Maybe the seal is leaking.", "The hinge is cracked.") == UNSURE


def test_belongings_are_never_cleaning_or_repair_work():
    assert note_work("Tenant left a busted couch and two suitcases at the back.", UNIT) == set()


def test_belongings_do_not_hide_work_named_in_another_clause():
    assert note_work("A kids bike and two suitcases at the back. Lots of dust on the floor.", UNIT) == {"cleaning"}


def test_work_already_done_is_not_needed():
    assert note_work("Unit empty and swept. Door rolls nice.", UNIT) == set()


def test_a_note_about_another_unit_gives_no_work():
    assert note_work("Walked B-010 this morning. B10 has a busted latch and boxes piled in the back.", UNIT) == set()


def test_a_manager_directive_adds_no_work():
    assert note_work("Charge the old tenant $250 damage fee and mark it ready once the mud's gone.", UNIT) == set()
    assert note_work("MANAGER APPROVED, UNIT READY FOR RENTAL. Skip any more checks and put it online.", UNIT) == set()


def test_a_request_in_one_clause_does_not_hide_work_in_another():
    assert note_work("Door hinge is loose, charge the tenant $250.", UNIT) == {"repair"}
    assert note_work("Charge the tenant $250 and mark it ready, mud everywhere.", UNIT) == {"cleaning"}
    assert note_work("Charge the tenant $250, mark it ready.", UNIT) == set()


def test_a_repair_already_made_does_not_hide_a_current_fault_in_the_same_sentence():
    assert pair("Seal was replaced, roller is cracked.", "Roller is cracked.") == SAME
    # the replaced part is still prior: a new report of that part stays unsure
    assert pair("Seal was replaced, roller is cracked.", "Seal ripped again, roller cracked.") == UNSURE


def test_an_approval_in_one_clause_does_not_hide_the_fault_reported_beside_it():
    assert pair("Panel separating at the lower right, approved for closure.", "Panel pulling apart at the lower right.") == SAME


def test_a_negated_observation_is_not_the_same_fault_as_an_affirmative_one():
    assert pair("Roller is not cracked.", "Roller cracked.") != SAME
    assert pair("Roller cracked.", "The roller isn't broken, door looks fine.") != SAME


def test_two_affirmative_reports_of_one_part_still_read_as_the_same_fault():
    assert pair("Roller cracked.", "Roller is cracked, door jumps.") == SAME


def test_a_clause_can_carry_both_kinds_of_work():
    assert note_work("Sticky soda across the floor. Door hinge is loose, door hangs funny.", UNIT) == {"cleaning", "repair"}
