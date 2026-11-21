"""The outside-written operations set is frozen: its hash is pinned before any part's decision code is written, and every label uses
words the set itself defines, so a later runner never has to guess what a label means."""
import hashlib
import json
from pathlib import Path

SET = Path(__file__).parent.parent / "core" / "graded" / "operations.json"


def test_the_operations_set_is_frozen_and_every_label_uses_its_own_words():
    assert hashlib.sha256(SET.read_bytes()).hexdigest() == "e26696c40b4c339bd7725bd919334f421d707fe208a66ae552befad3789b9c40"
    s = json.loads(SET.read_text())
    items = s["graded"]
    assert len(items) == 80 and len({i["id"] for i in items}) == 80
    for area in (1, 2, 3, 4):
        mine = [i for i in items if i["area"] == area]
        assert [sum(i["group"] == g for i in mine) for g in ("ordinary", "changing_records", "adversarial")] == [8, 6, 6], area
    for i in items:
        label = i["label"]
        assert label["ask"] in s["asks"] and label["decision"] in ("allow", "refuse", None), i["id"]
        assert (label["why"] is None) == (label["decision"] != "refuse"), i["id"]
        assert label["why"] is None or label["why"] in s["why"], i["id"]
        assert set(label["effects"]) <= set(s["effects"]) and set(label["never"]) <= set(s["never"]), i["id"]
        assert str(label["part"]) in s["parts"] and label["judge"] in ("agent", "code"), i["id"]
        assert i["timeline"] and i["decision_point"] and i["author_expected"] and i["author_must_not"], i["id"]
