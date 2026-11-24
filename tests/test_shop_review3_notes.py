"""Review fixes on the shop's refusal notes: the Shop page keeps its own note and its partial-save flag, so another page
cannot take them. Each test fails on the code before the fix."""
from .test_fms import seen, sign_in, site
from .test_fms_shop_pages import post


def test_a_shop_note_is_not_taken_by_opening_today_in_another_tab(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        refused = post(c, "give_away", product="disc lock", quantity="50", reason="Goodwill", approval="Manager", handover="Handed to a neighbour")
        assert "make stock negative" not in seen(c.get("/today").text)               # another tab opens Today before the Shop redirect lands
        assert c.get("/leads").status_code == 200
        note = seen(c.get(refused.headers["location"]).text)
        assert "The give-away was saved, but it is not taken out of stock yet" in note and "Nothing changed" not in note
