"""Small shop page defects: a product is named exactly as it is stored when it is chosen, and a follow-up step that fails after a record
was saved says what was saved. Each test fails on the code before the fix."""
from core import shop

from .test_fms import seen, sign_in, site
from .test_fms_renting_pages import desk_of, token
from .test_fms_shop_pages import part, post, state_of

STAFF = "staff:office_manager"


def test_a_product_chosen_on_a_form_is_looked_up_exactly_as_it_is_stored(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        assert fd.staff_shop(ident, shop.Act("odd-name", "publish_product", STAFF, "Heavy  lock", {"price": 1500, "cost": 500, "tax_class": "standard", "active": True}))
        page = c.get("/shop").text
        assert 'name="product" value="Heavy  lock"' in page                                      # the page sends the stored name, double space and all
        changed = c.post("/shop", data={"token": token(page), "action": "change_price", "product": "Heavy  lock", "price": "20", "cost": "6", "active": "yes"},
                         follow_redirects=False)
        assert changed.status_code == 303
        st, *_ = state_of(app, c)
        assert [v["price"] for v in st.versions["Heavy  lock"]] == [1500, 2000] and "Heavy lock" not in st.versions      # one product, a new version of it
        assert post(c, "record_count", product="Heavy  lock", quantity="0").status_code == 303
        st, *_ = state_of(app, c)
        assert [x["product"] for x in st.counts.values()].count("Heavy  lock") == 1               # counted against the stored name, not refused
        assert post(c, "add_product", product="  New   box ", price="5", cost="2", tax_class="standard").status_code == 303
        assert "New box" in state_of(app, c)[0].versions                                          # only a name being created is tidied


def test_a_give_away_whose_stock_step_fails_says_the_handover_was_saved(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        refused = post(c, "give_away", product="disc lock", quantity="50", reason="Goodwill", approval="Manager", handover="Handed to a neighbour")
        note = seen(c.get(refused.headers["location"]).text)
        assert "Nothing changed" not in note
        assert "The give-away was saved, but it is not taken out of stock yet" in note and "make stock negative" in note
        st, *_ = state_of(app, c)
        [mine] = [h for h in st.handovers.values() if h["quantity"] == 50]                         # the record is there, waiting to be posted
        assert not any(m.get("reference") == mine["id"] for m in st.moves)
        assert "Not taken out of stock yet" in seen(part(c.get("/shop").text, "giveaways"))
        plain = post(c, "post_give_away", about=mine["id"])                                        # a single step refused still changes nothing, and says so
        assert "Nothing changed. The adjustment would make stock negative" in seen(c.get(plain.headers["location"]).text)


def test_a_counter_sale_whose_card_is_declined_says_the_sale_was_started(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        refused = post(c, "sell", product="disc lock", quantity="1", card="card_declined")
        note = seen(c.get(refused.headers["location"]).text)
        assert "Nothing changed" not in note
        assert "The sale was started and its stock is held, but it is not posted: the card was declined" in note
        st, *_ = state_of(app, c)
        assert len(st.quotes) == 1 and not st.sales
