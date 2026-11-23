"""Tilden reads two open work orders on one unit and says whether they are one fault, two, or cannot tell. Code keeps the reading only
when it cites a report of each order, speaks as staff would read it, and says nothing was joined or repaired. A stand-in client
returns fixed answers, so nothing here touches the network."""
import pytest

from core import walk, walk_reading
from tests.test_core_walk_compare import SECOND, STREAM
from tests.test_core_walk_ready import Floor
from tests.walk_client import StandIn, report_ids, same_answer


def two():
    w = Floor()
    a, b = w.report(SECOND, "Broken door spring"), w.report(SECOND, "Door spring is broken")
    s = walk.load(w.store, STREAM)[1]
    return s.orders[a], s.orders[b]


def both(content):
    ids = report_ids(content)
    return ids["First"] + ids["Second"]


def test_a_reading_that_cites_both_orders_is_kept_and_the_prompt_holds_no_record_ids():
    first, second = two()
    client = StandIn(lambda c: same_answer(both(c)))
    res = walk_reading.same_fault(client, "claude-haiku-5-5", "low", first, second, "r1")
    assert res.dropped is None and res.pair == f"{first.id}:{second.id}"
    assert res.reading == {"verdict": "same", "records": ["r1-1", "r1-2"],
                           "summary": "Both reports describe a broken spring on the unit's door, so one repair fixes both."}
    content = client.asked[0]["messages"][0]["content"]
    assert f"First work order on unit {SECOND}" in content and "Door spring is broken" in content
    assert first.id not in content and second.id not in content


@pytest.mark.parametrize("answer, why", [
    (lambda c: same_answer(report_ids(c)["First"]), "the answer does not cite both orders"),
    (lambda c: same_answer(both(c) + ["r1-9"]), "the answer cites a record that is not one of the two orders' reports"),
    (lambda c: same_answer([]), "the answer cites no record"),
    (lambda c: same_answer(both(c), summary="We think both reports are about one spring."), "the answer speaks in the first person"),
    (lambda c: same_answer(both(c), summary="The second report was joined into the first one."), "the answer says something was done, and nothing has been"),
    (lambda c: same_answer(both(c), summary="Both reports describe the spring on unit B200."), "the answer names what the reports do not show (B200)"),
    (lambda c: same_answer(both(c), summary="The spring that could not lift the door was repaired."), "the answer says something was done, and nothing has been"),
    (lambda c: same_answer(both(c), summary="The reports agree, and the spring was replaced."), "the answer says something was done, and nothing has been"),
    (lambda c: same_answer(both(c), summary="The second report says it was replaced, and the spring was repaired."), "the answer says something was done, and nothing has been"),
    (lambda c: same_answer(both(c), summary="The spring was repaired as the second report says."), "the answer says something was done, and nothing has been"),
    (lambda c: same_answer(both(c), summary="The orders were merged since the first report mentions it."), "the answer says something was done, and nothing has been"),
    (lambda c: same_answer(both(c), verdict="probably"), "the answer gave no verdict from the list"),
    (lambda c: same_answer(both(c), summary="The second report says the seal was replaced: the orders were merged."), "the answer says something was done, and nothing has been"),
])
def test_a_reading_code_cannot_check_is_dropped_with_the_reason(answer, why):
    first, second = two()
    res = walk_reading.same_fault(StandIn(answer), "claude-haiku-5-5", "low", first, second, "r1")
    assert (res.reading, res.dropped) == (None, why)


def test_a_conditional_suggestion_is_not_a_claim_that_anything_was_done():
    first, second = two()
    summary = "Both reports describe one broken spring, so the second order should be joined into the first."
    res = walk_reading.same_fault(StandIn(lambda c: same_answer(both(c), summary=summary)), "claude-haiku-5-5", "low", first, second, "r1")
    assert res.dropped is None and res.reading["summary"] == summary


def test_different_and_unsure_are_kept_as_readings_too():
    first, second = two()
    for verdict in ("different", "unsure"):
        res = walk_reading.same_fault(StandIn(lambda c: same_answer(both(c), verdict=verdict, summary="The words alone do not show one part.")),
                                      "claude-haiku-5-5", "low", first, second, "r1")
        assert res.reading["verdict"] == verdict


def test_a_bare_attributed_claim_passes_with_or_without_a_colon():
    first, second = two()
    for summary in ("The second report says the seal was replaced.", "The second report says: the seal was replaced."):
        res = walk_reading.same_fault(StandIn(lambda c: same_answer(both(c), verdict="unsure", summary=summary)), "claude-haiku-5-5", "low", first, second, "r1")
        assert res.dropped is None and res.reading["summary"] == summary


def test_a_claim_a_report_makes_may_be_quoted_as_its_claim():
    first, second = two()
    summary = "The second report says maintenance replaced the spring already, so the words alone do not settle whether this is a new failure."
    res = walk_reading.same_fault(StandIn(lambda c: same_answer(both(c), verdict="unsure", summary=summary)), "claude-haiku-5-5", "low", first, second, "r1")
    assert res.dropped is None and res.reading["verdict"] == "unsure"
