"""Shop page forms after review: each button is offered only where the shop would take it, and a form is a replay only when every field matches."""
import re

from core import shop

from .test_fms import seen, sign_in, site
from .test_fms_renting_pages import desk_of, token
from .test_fms_shop_pages import hidden, part, post, state_of
from .test_shop_review_fixes import Dropping


def test_the_return_form_offers_only_the_condition_the_shop_can_take(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        assert post(c, "sell", product="disc lock", quantity="1", card="card_ok").status_code == 303
        sales = part(c.get("/shop").text, "sales")
        assert 'value="resalable"' in sales and "damaged" not in sales.lower()      # a damaged return has no flow yet: it is not offered


def test_a_form_whose_fields_run_together_is_not_taken_for_a_replay(fms_url, sim):
    """Product A with quantity 1 and note 2|Opening has the same fields end to end as product A|1 with quantity 2 and note Opening."""
    app = site(fms_url)
    with sign_in(app) as c:
        for name in ("A", "A|1"):
            assert post(c, "add_product", product=name, price="5", cost="2", tax_class="standard").status_code == 303
        same = token(c.get("/shop").text)

        def send(product, quantity, reason):
            return c.post("/shop", data={"token": same, "action": "expect_delivery", "product": product, "quantity": quantity, "reason": reason}, follow_redirects=False)
        assert send("A", "1", "2|Opening").status_code == send("A|1", "2", "Opening").status_code == 303
        st, *_ = state_of(app, c)
        assert sorted((d["product"], d["quantity"]) for d in st.deliveries.values() if d["product"] in ("A", "A|1")) == [("A", 1), ("A|1", 2)]
        assert send("A", "1", "2|Opening").status_code == 303
        assert len(state_of(app, c)[0].deliveries) == len(st.deliveries)            # the exact same form is still a replay


def test_a_payment_that_never_got_an_answer_is_asked_about_again_from_the_page(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        fd.processor = down = Dropping()
        assert post(c, "sell", product="disc lock", quantity="1", card="card_ok").status_code == 303
        st, *_ = state_of(app, c)
        [quote] = st.payments
        sales = part(c.get("/shop").text, "sales")
        assert "Ask the card processor again" in seen(sales) and "Try the payment again" not in seen(sales) and "Cancel the sale" not in seen(sales)
        key = st.payments[quote]["key"]
        down.down = False
        assert post(c, "resume_payment", about=quote).status_code == 303
        st, *_ = state_of(app, c)
        assert st.payments[quote]["key"] == key and st.payments[quote]["status"] == "succeeded" and quote in st.sales and len(down.payments) == 1
        assert "Ask the card processor again" not in seen(part(c.get("/shop").text, "sales"))
        assert not [d for d in shop.differences(st) if quote in d]              # the seeded give-away is the one open difference; this sale adds none


def test_a_difference_is_offered_for_posting_only_when_the_shop_would_take_it(fms_url, sim):
    """Stock moved after a count: the shop refuses to post that count's difference, so the page does not offer the button."""
    app = site(fms_url)
    with sign_in(app) as c:
        counts = part(c.get("/shop").text, "counts")
        count = hidden(counts, "about")
        assert "Accept the difference and post it to stock" in seen(counts)               # nothing has moved since the count: it is offered
        fd, ident = desk_of(app, c)
        assert fd.staff_shop(ident, shop.Act("early-review", "review_count", "staff:office_manager", count, {"reason": "Looked at the shelf"}))
        assert "Post the difference to stock" in seen(part(c.get("/shop").text, "counts"))      # reviewed, not posted yet, stock unchanged
        delivery = hidden(part(c.get("/shop").text, "deliveries"), "about")
        assert post(c, "receive_delivery", about=delivery, quantity="4", reason="Staff counted four boxes").status_code == 303
        counts = seen(part(c.get("/shop").text, "counts"))
        assert "Post the difference to stock" not in counts and "has not been reviewed against the current stock" in counts
        refused = post(c, "post_difference", about=count)
        assert "Nothing changed. The count difference has not been reviewed against the current stock" in seen(c.get(refused.headers["location"]).text)


def test_a_count_that_cannot_be_accepted_does_not_offer_accept_but_still_offers_a_recount(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        delivery = hidden(part(c.get("/shop").text, "deliveries"), "about")
        assert post(c, "receive_delivery", about=delivery, quantity="4", reason="Staff counted four boxes").status_code == 303
        counts = part(c.get("/shop").text, "counts")
        assert "Accept the difference and post it to stock" not in seen(counts) and "Ask for a recount" in seen(counts)
        assert "has not been reviewed against the current stock" in seen(counts)


def test_the_add_product_fields_are_required_before_the_browser_posts(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        form = part(c.get("/shop").text, "products").split("<summary>Add a product</summary>", 1)[1].split("</form>", 1)[0]
        for field in ("product", "price", "cost", "tax_class"):
            tag = re.search(rf'<(?:input|select)[^>]*name="{field}"[^>]*>', form)
            assert tag and "required" in tag.group(0), field
        refused = post(c, "add_product", product="Padlock", price="5", cost="2")
        assert "Nothing changed. Give a product, price, cost, tax class" in seen(c.get(refused.headers["location"]).text)


def test_a_recount_asked_without_findings_clears_two_counts_that_disagree(fms_url, sim):
    """Found by pressing the forms on a local copy: the recount button skips the findings box, and the request was refused for it."""
    app = site(fms_url)
    with sign_in(app) as c:
        assert post(c, "record_count", product="disc lock", quantity="0").status_code == 303
        st, *_ = state_of(app, c)
        doubted = [i for i, x in st.counts.items() if x["product"] == "disc lock" and not x.get("review")]
        assert len(doubted) >= 2 and st.in_conflict(st.counts[doubted[-1]])
        for about in doubted:
            assert post(c, "ask_recount", about=about).status_code == 303
        st, *_ = state_of(app, c)
        assert all((st.counts[i].get("review") or {}).get("recount") for i in doubted)
        assert "Give an existing count" not in seen(c.get("/shop").text)
        assert post(c, "record_count", product="disc lock", quantity="10").status_code == 303
        assert 'value="review_count"' in part(c.get("/shop").text, "counts")      # the new count can now be accepted


def test_shop_steps_stay_off_the_today_decision_list(fms_url, sim):
    """Found by pressing the forms on a local copy: Today listed "complete_sale: . The sale was posted.." after a counter sale."""
    with sign_in(site(fms_url)) as c:
        assert post(c, "sell", product="disc lock", quantity="1", card="card_ok").status_code == 303
        today = c.get("/today").text
        assert not any(name in today for name in shop.ACTIONS)
        assert "Nothing in the books changed" not in seen(today)


def test_every_shop_command_is_in_the_action_list():
    import inspect
    names = set(re.findall(r"act\.action == '([a-z_]+)'", inspect.getsource(shop)))
    for group in re.findall(r"act\.action in \(([^)]*)\)", inspect.getsource(shop)):
        names |= set(re.findall(r"'([a-z_]+)'", group))
    assert names <= shop.ACTIONS
