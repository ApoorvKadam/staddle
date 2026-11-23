"""The Shop page: staff do every shop job from a page, every shop item on Today links to where it is closed, and each form goes through the
shop's staff command gate. Only page forms are used to change anything."""
import html
import re

from fastapi.testclient import TestClient

from core import shop
from tests.test_fms import seen, sign_in, site
from tests.test_fms_renting_pages import desk_of, token

CODES = re.compile(r"form:[0-9a-f]{12}|seed:shop|\b(?:NEEDS_STAFF|OUTCOME_UNKNOWN|PENDING_AT_FMS)\b|shop:payment")


def post(client, action, **fields):
    page = client.get("/shop").text
    if action == "sell" and "shown" not in fields:          # the counter form sends back the prices it shows
        fields["shown"] = html.unescape(re.search(r'name="shown" value="([^"]+)"', page).group(1))
    if action == "resume_payment" and "key" not in fields:    # the resume form names the attempt it shows
        fields["key"] = html.unescape(re.search(r'name="about" value="%s"><input type="hidden" name="key" value="([^"]+)"' % re.escape(fields["about"]), page).group(1))
    return client.post("/shop", data={"token": token(page), "action": action, **fields}, follow_redirects=False)


def part(page, name):
    """The one section of the Shop page with that anchor."""
    return page.split(f'id="{name}"', 1)[1].split("</section>", 1)[0]


def hidden(html, name):
    return re.search(rf'name="{name}" value="([^"]+)"', html).group(1)


def state_of(app, client):
    fd, ident = desk_of(app, client)
    return shop.state(fd.ready().books(fd.stream(ident))), fd, ident


def today_item(client, words, view="all"):
    page = client.get("/today", params={"view": view}).text
    return next((a for a in page.split("<article") if words in seen(a) and 'class="fms-line"' in a), None)


def merchandise(app, client):
    fd, ident = desk_of(app, client)
    totals = {}
    for _, account, cents, _c in fd.ready().journal(fd.stream(ident)):
        if "merchandise" in account:
            totals[account] = totals.get(account, 0) + cents
    return totals


def test_sign_in_required(fms_url, sim):
    with TestClient(site(fms_url)) as client:
        assert client.get("/shop", follow_redirects=False).headers["location"] == "/sign-in"
        assert client.post("/shop", follow_redirects=False).headers["location"] == "/sign-in"


def test_shop_sits_beside_walkthrough_and_reads_in_words(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        home = client.get("/home").text
        menu = re.findall(r'<a href="/(\w+)" ', home.split('aria-label="Facility"', 1)[1])
        assert menu[menu.index("walkthrough") + 1] == "shop"
        response = client.get("/shop")
        assert response.status_code == 200
        text = seen(response.text)
        assert "disc lock" in text and "$15.00" in text and "$5.00" in text
        assert "the books said 12, staff counted 9, a difference of -3" in text
        assert "Opening delivery" in text and "Staff counted the delivery" in text
        assert "Goodwill" in text and "Manager approved" in text
        assert not CODES.search(text)


def test_each_part_of_the_shop_is_done_from_its_page_forms(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        # the seeded count is on Today and links to the Shop page
        item = today_item(c, "Stock count needs review")
        assert item and "disc lock: book quantity 12, counted 9" in seen(item)
        link = re.search(r'href="(/shop#counts)"', item).group(1)
        page = c.get(link.split("#")[0])
        assert 'id="counts"' in page.text and "Waiting for review" in seen(part(page.text, "counts"))
        count = hidden(part(page.text, "counts"), "about")
        # a review with no findings is refused in words and nothing changes
        refused = post(c, "review_count", about=count, reason="")
        assert refused.headers["location"] == "/shop?at=counts#counts"
        assert "Nothing changed. Give an existing count and the review findings." in seen(c.get(refused.headers["location"]).text)
        st, *_ = state_of(app, c)
        assert st.quantity("disc lock") == 12 and not st.counts[count]["review"]
        # accepting it posts the count variance move and closes the item on Today
        assert post(c, "review_count", about=count, reason="Counted twice, three locks are missing").status_code == 303
        st, *_ = state_of(app, c)
        assert st.quantity("disc lock") == 9 and st.counts[count]["review"]
        assert today_item(c, "Stock count needs review") is None
        assert today_item(c, "Stock count needs review", "done")
        assert "Reviewed, difference posted to stock" in seen(part(c.get("/shop").text, "counts"))

        # the unmatched delivery is recorded as received: stock goes up by what arrived, once
        deliveries = part(c.get("/shop").text, "deliveries")
        delivery = hidden(deliveries, "about")
        assert "4 not received yet" in seen(deliveries)
        assert post(c, "receive_delivery", about=delivery, quantity="4", reason="Staff counted four boxes").status_code == 303
        assert post(c, "receive_delivery", about=delivery, quantity="4", reason="Staff counted four boxes").status_code == 303
        st, *_ = state_of(app, c)
        assert st.quantity("disc lock") == 13
        assert "Received in full" in seen(part(c.get("/shop").text, "deliveries"))
        assert "more than" not in seen(part(c.get("/shop").text, "deliveries"))

        # the seeded give-away is handed over but not taken out of stock; Today's records item names it and closes with the step
        item = today_item(c, "Shop records need review")
        assert item and 'href="/shop#records"' in item
        giveaways = part(c.get("/shop").text, "giveaways")
        assert "Not taken out of stock yet" in seen(giveaways)
        assert post(c, "post_give_away", about=hidden(giveaways, "about")).status_code == 303
        st, *_ = state_of(app, c)
        assert st.quantity("disc lock") == 12
        assert today_item(c, "Shop records need review") is None
        assert "The shop records agree" in seen(part(c.get("/shop").text, "records"))
        # a new give-away needs its approval and handover
        assert "Nothing changed" in seen(c.get(post(c, "give_away", product="disc lock", quantity="1", reason="Goodwill", approval="", handover="").headers["location"]).text)
        assert post(c, "give_away", product="disc lock", quantity="1", reason="Lost key", approval="Manager approved", handover="Handed to the tenant").status_code == 303
        st, *_ = state_of(app, c)
        assert st.quantity("disc lock") == 11 and len(st.handovers) == 2

        # a counter sale with the accepted test card: stock and the books change once, even when the form is sent twice
        before = merchandise(app, c)
        page = c.get("/shop").text
        form = {"token": token(page), "action": "sell", "product": "disc lock", "quantity": "1", "card": "card_ok",
                "shown": html.unescape(re.search(r'name="shown" value="([^"]+)"', page).group(1))}
        assert c.post("/shop", data=form, follow_redirects=False).status_code == 303
        assert c.post("/shop", data=form, follow_redirects=False).status_code == 303
        st, *_ = state_of(app, c)
        assert st.quantity("disc lock") == 10 and len(st.sales) == 1
        after = merchandise(app, c)
        assert after["income:merchandise"] - before.get("income:merchandise", 0) == -1500
        assert after["cost:merchandise"] - before.get("cost:merchandise", 0) == 500
        sales = seen(part(c.get("/shop").text, "sales"))
        assert "1 disc lock, $15.00" in sales and "Paid" in sales and "Sale posted" in sales

        # a recorded return puts the lock back and the cost back
        sale = hidden(part(c.get("/shop").text, "sales"), "about")
        assert "Nothing changed" in seen(c.get(post(c, "record_return", about=sale, quantity="1", condition="damaged", reason="Broken latch").headers["location"]).text)
        assert post(c, "record_return", about=sale, quantity="1", condition="resalable", reason="Wrong size").status_code == 303
        st, *_ = state_of(app, c)
        assert st.quantity("disc lock") == 11 and len(st.returns) == 1
        assert merchandise(app, c)["cost:merchandise"] == before.get("cost:merchandise", 0)
        assert "Back in stock" in seen(part(c.get("/shop").text, "returns"))
        assert not CODES.search(seen(c.get("/shop").text))


def test_price_change_keeps_history_and_a_recount_posts_nothing(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        assert post(c, "change_price", product="disc lock", price="17.50", cost="6", active="yes").status_code == 303
        st, *_ = state_of(app, c)
        assert [v["price"] for v in st.versions["disc lock"]][-2:] == [1500, 1750] and st.versions["disc lock"][-1]["cost"] == 600
        products = seen(part(c.get("/shop").text, "products"))
        assert "$17.50" in products and "price $15.00, cost $5.00" in products
        assert "Nothing changed" in seen(c.get(post(c, "change_price", product="disc lock", price="free", cost="6", active="yes").headers["location"]).text)
        count = hidden(part(c.get("/shop").text, "counts"), "about")
        assert post(c, "ask_recount", about=count, reason="Shelf was mid-restock").status_code == 303
        st, *_ = state_of(app, c)
        assert st.quantity("disc lock") == 12 and st.counts[count]["review"]["reason"] == "Shelf was mid-restock" and st.counts[count]["review"]["recount"]
        counts = seen(part(c.get("/shop").text, "counts"))
        assert "Recount asked for. Waiting for the new count" in counts and "Accept the difference" not in counts
        assert today_item(c, "Stock count needs review")            # asking is not an answer: the item stays until a newer count exists
        assert post(c, "record_count", product="disc lock", quantity="8").status_code == 303
        item = today_item(c, "Stock count needs review")
        assert item and "counted 8" in seen(item) and "counted 9" not in seen(item)
        assert "a newer count stands in for this one" in seen(part(c.get("/shop").text, "counts"))
        newer = hidden(part(c.get("/shop").text, "counts"), "about")
        assert post(c, "review_count", about=newer, reason="Counted twice").status_code == 303
        st, *_ = state_of(app, c)
        assert st.quantity("disc lock") == 8
        assert today_item(c, "Stock count needs review") is None


def declined_sale(c, quantity="1"):
    response = post(c, "sell", product="disc lock", quantity=quantity, card="card_declined")
    assert response.headers["location"] == "/shop?at=counter#counter"
    assert "The sale was started and its stock is held, but it is not posted: the card was declined" in seen(c.get(response.headers["location"]).text)


def sale_row(c, words):
    """The sale article of the Sales part that has those words in it."""
    return next(a for a in part(c.get("/shop").text, "sales").split("<article") if words in seen(a))


def test_a_declined_card_sells_nothing_and_a_word_does_not_close_its_item(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        declined_sale(c)
        st, *_ = state_of(app, c)
        assert st.quantity("disc lock") == 12 and not st.sales
        item = today_item(c, "Shop payment needs review")
        assert item and 'href="/shop#sales"' in item
        assert "Done: I checked it" not in seen(item) and "A new payment is collected, the unpaid sale is cancelled" in seen(item)
        row = sale_row(c, "Needs a look")
        assert "Try the payment again" in row and "Cancel the sale and release the stock" in row and "Write off" not in row
        # a word, sent straight to the work gate, is refused in plain words and the item stays
        key = re.search(r'name="key" value="([^"]+)"', item).group(1)
        page = c.get("/today").text
        response = c.post("/work/step", data={"token": token(page), "action": "finish", "key": key, "text": "I checked it"}, follow_redirects=False)
        assert response.status_code == 303
        assert "the record closes this one, not a word" in seen(c.get("/today").text).lower()
        assert today_item(c, "Shop payment needs review")


def test_trying_the_payment_again_posts_the_sale_once_and_closes_the_item(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        declined_sale(c)
        before = merchandise(app, c)
        row = sale_row(c, "Needs a look")
        quote = hidden(row, "about")
        page = c.get("/shop").text
        form = {"token": token(page), "action": "retry_payment", "about": quote, "card": "card_ok"}
        assert c.post("/shop", data=form, follow_redirects=False).status_code == 303
        assert c.post("/shop", data=form, follow_redirects=False).status_code == 303       # the same page sent twice takes nothing again
        st, *_ = state_of(app, c)
        assert st.quantity("disc lock") == 11 and len(st.sales) == 1
        after = merchandise(app, c)
        assert after["income:merchandise"] - before.get("income:merchandise", 0) == -1500
        assert today_item(c, "Shop payment needs review") is None
        assert "Needs a look" not in seen(part(c.get("/shop").text, "sales")) and "Sale posted" in seen(part(c.get("/shop").text, "sales"))
        assert "Nothing changed" in seen(c.get(post(c, "retry_payment", about=quote, card="card_ok").headers["location"]).text)


def test_cancelling_an_unpaid_sale_releases_the_stock_and_is_refused_without_a_reason(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        declined_sale(c, "12")
        assert "Choose a positive quantity available in stock" in seen(c.get(post(c, "sell", product="disc lock", quantity="1", card="card_ok").headers["location"]).text)
        quote = hidden(sale_row(c, "Needs a look"), "about")
        refused = post(c, "cancel_sale", about=quote, reason="")
        assert "Nothing changed. Say why the sale is cancelled." in seen(c.get(refused.headers["location"]).text)
        assert today_item(c, "Shop payment needs review")
        assert post(c, "cancel_sale", about=quote, reason="The buyer left").status_code == 303
        st, *_ = state_of(app, c)
        assert st.available("disc lock") == 12 and st.quantity("disc lock") == 12 and not st.sales
        assert today_item(c, "Shop payment needs review") is None
        row = seen(part(c.get("/shop").text, "sales"))
        assert "Cancelled" in row and "The buyer left" in row and "Try the payment again" not in row
        assert post(c, "sell", product="disc lock", quantity="12", card="card_ok").status_code == 303
        assert state_of(app, c)[0].quantity("disc lock") == 0


def test_a_returned_payment_is_written_off_into_the_books_or_paid_again(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        for n in (1, 2):
            assert post(c, "sell", product="disc lock", quantity="1", card="card_ok").status_code == 303
            st, *_ = state_of(app, c)
            quote, payment = list(st.payments.items())[-1]
            fd.processor.settle(payment["outside"], "returned", fd.clock())
            fd.sweep()
            assert state_of(app, c)[0].payments[quote]["status"] == "returned"
        assert len(re.findall("Needs a look", seen(part(c.get("/shop").text, "sales")))) == 2
        item = today_item(c, "Shop payment needs review")
        assert item and "was returned" in seen(item)
        first, second = [hidden(a, "about") for a in part(c.get("/shop").text, "sales").split("<article") if "Needs a look" in a]
        row = sale_row(c, "Needs a look")
        assert "Write off the payment" in row and "Try the payment again" in row and "Cancel the sale" not in row
        # one is paid again: no second sale, the money is back in the books
        assert post(c, "retry_payment", about=first, card="card_ok").status_code == 303
        st, *_ = state_of(app, c)
        assert st.payments[first]["status"] == "succeeded" and len(st.sales) == 2 and len([m for m in st.moves if m["kind"] == "sold"]) == 2
        # the other is written off: its own entry in the books, and a reason is required
        assert "Say why the money is written off." in seen(c.get(post(c, "write_off_payment", about=second, reason="").headers["location"]).text)
        assert today_item(c, "Shop payment needs review")
        assert post(c, "write_off_payment", about=second, reason="The buyer cannot be reached").status_code == 303
        books = merchandise(app, c)
        assert books["income:merchandise"] == -3000 and books["loss:merchandise-write-off"] == 1500
        assert today_item(c, "Shop payment needs review") is None
        text = seen(part(c.get("/shop").text, "sales"))
        assert "Written off" in text and "The buyer cannot be reached" in text and "Needs a look" not in text
        assert not CODES.search(seen(c.get("/shop").text))
