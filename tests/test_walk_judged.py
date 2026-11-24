"""The walkthrough grading runner on a stand-in client: every fixture builds on the generated year, Tilden's answers are scored against
core/graded/operations_judged.json, and the fixed-rule arms score as their rules say. No network."""
import json
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


def test_a_set_with_check_labels_scores_check_and_not_work_words(tmp_path):
    # a stand-in in the third set's layout, written for this test (the real third set is never read by a test)
    pair = {"id": "A1", "unit": "B12", "first": "Door spring snapped, door will not lift.", "first_at": "2026-11-20T09:00:00", "first_by": "Ana",
            "second": "Spring on the door is broken, cannot open it.", "second_at": "2026-11-20T11:00:00", "second_by": "Raj", "label": "same", "why": "one spring"}
    def note(nid, text, cleaning, repair, check, not_work=None):
        return {"id": nid, "unit": "B12", "note": text, "note_at": "2026-11-20", "note_by": "Ana", "cleaning": cleaning, "repair": repair,
                "check": check, "not_work": not_work, "other": None, "why": "stand-in"}
    path = tmp_path / "set3.json"
    path.write_text(json.dumps({"about": "stand-in", "mix": {}, "pairs": [pair], "notes": [
        note("B1", "The door might be bent near the bottom, hard to tell.", False, False, True),
        note("B2", "Lots of trash all over the floor.", True, False, False, "none"),
        note("B3", "Couch to remove at the back.", False, False, False, '"Couch to remove"')]}))

    def answer3(content):
        if "<note>" in content:
            text = content.split("<note>")[1].split("</note>")[0]
            if "might be bent" in text:
                return note_answer(unit=None, work=[("check", "might be bent")])
            if "Couch" in text:
                return note_answer(unit=None, work=[("cleaning", "Couch to remove")])
        return answer(content)

    report = evaluate.walk_judged(StandIn(*[answer3] * 200), 1, "claude-haiku-5-5", "low", str(path))
    assert report["outside_file"] == "set3.json" and report["outside"] == evaluate.outside_hash(str(path))
    rows = {(r["item"], r["arm"]): r for r in report["rows"] if r["part"] == "move_out_note" and r["set"] == "outside"}
    assert rows[("B1", "tilden")]["kinds"] == ["check"] and rows[("B1", "tilden")]["right"]
    assert rows[("B1", "strong_rule")]["kinds"] == ["check"] and rows[("B1", "strong_rule")]["right"]
    assert not rows[("B1", "fixed")]["right"]                       # a word list never says check
    assert rows[("B2", "tilden")]["right"] and rows[("B2", "strong_rule")]["right"]
    assert rows[("B3", "tilden_unchecked")]["critical"] == ["not-work words turned into work"] and rows[("B3", "tilden_unchecked")]["not_work_in_words"]
    assert rows[("B3", "strong_rule")]["right"] and not rows[("B3", "strong_rule")]["critical"]
    same = {r["arm"]: r for r in report["rows"] if r["part"] == "same_fault" and r["item"] == "A1"}
    assert same["tilden"]["right"] and same["fixed_unit"]["right"]
