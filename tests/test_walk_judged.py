"""The walkthrough grading runner on a stand-in client: every fixture builds on the generated year, Tilden's answers are scored against
core/graded/operations_judged.json, and the fixed-rule arms score as their rules say. No network."""
import re

from core import evaluate
from tests.walk_client import StandIn, history_ids, note_answer, packet, report_ids, same_answer


def answer(content):
    if "<history>" in content:                    # an investigation: the first open step, every record cited
        step = re.search(r"Next steps open for this finding: (\w+)", content).group(1)
        return packet(history_ids(content), step=step, reason="needs_a_person")
    if "<reports>" in content:                    # two work orders: Tilden says same only when the reports share the word spring
        ids = report_ids(content)
        first, second = (re.search(rf"^{ids[k][0]} .*?: (.*)$", content, re.M).group(1).lower() for k in ("First", "Second"))
        verdict = "same" if ("spring" in first) == ("spring" in second) and ("latch" in first) == ("latch" in second) else "different"
        return same_answer(ids["First"] + ids["Second"], verdict=verdict, summary="The two reports are compared by the part they name.")
    if "Padlock" in content:
        return note_answer(unit=None, lock=("present", "Padlock"), tag=("absent", "no facility tag"), door=("closed", "on the door"))
    note = content.split("<note>")[1].split("</note>")[0].strip()       # a move-out note: the stand-in names work by one word each
    work = [(k, w) for k, w in (("cleaning", "trash"), ("cleaning", "dust"), ("repair", "broken"), ("repair", "flickers"), ("repair", "cracked"), ("cleaning", "mop"))
            if w in note]
    return note_answer(unit=None, work=work)


def test_the_runner_builds_every_fixture_and_scores_both_arms():
    report = evaluate.walk_judged(StandIn(*[answer] * 60), 1, "claude-haiku-5-5", "low")
    t = report["table"]
    assert report["rubric"] == evaluate.judged_hash() and report["outside"] == evaluate.outside_hash() and report["runs"] == 1
    assert set(report["sets"]) == {"outside investigate", "practice reading", "outside move_out_note", "practice move_out_note",
                                   "outside same_fault", "practice same_fault"}
    assert report["sets"]["outside same_fault"]["tilden"]["runs"] == 21 and report["sets"]["outside move_out_note"]["tilden"]["runs"] == 21
    for item in ("o21", "o22", "o38"):
        assert t[f"{item} investigate"]["fixed"]["right"] == 1
        assert t[f"{item} investigate"]["tilden"] == {"right": 1, "dropped": 0, "critical": 0, "runs": 1}
    assert t["o38 reading"]["fixed"] == {"right": 0, "dropped": 0, "critical": 1, "runs": 1}       # the word "overlock" decides it
    assert t["o38 reading"]["tilden"]["right"] == 1
    words = {k.split()[0]: v["fixed_words"]["right"] for k, v in t.items() if k.endswith("same_fault") and k[0] in "od"}
    assert words == {"o36": 0, "d1": 1, "d2": 0, "d3": 1, "d4": 0, "d5": 1}
    unit = {k.split()[0]: (v["fixed_unit"]["right"], v["fixed_unit"]["critical"]) for k, v in t.items() if k.endswith("same_fault") and k[0] in "od"}
    assert unit == {"o36": (1, 0), "d1": (0, 1), "d2": (1, 0), "d3": (0, 1), "d4": (1, 0), "d5": (0, 1)}
    notes = {k.split()[0]: (v["fixed"]["right"], v["tilden"]["right"]) for k, v in t.items() if k.endswith("move_out_note") and k[0] in "on"}
    assert notes == {"o25": (1, 1), "n1": (0, 1), "n2": (1, 1), "n3": (0, 1), "n4": (1, 1), "n5": (1, 1)}
    assert all(r["raw"] is not None for r in report["rows"] if r["arm"] == "tilden" and r["part"] in ("investigate", "same_fault"))


def test_the_outside_unsure_pairs_count_a_confident_fixed_verdict_as_serious():
    report = evaluate.walk_judged(StandIn(*[answer] * 60), 1, "claude-haiku-5-5", "low")
    t = report["table"]
    for item in ("A15", "A16", "A17", "A20"):                       # the author says the words cannot settle these
        assert t[f"{item} same_fault"]["fixed_unit"] == {"right": 0, "dropped": 0, "critical": 1, "runs": 1}
