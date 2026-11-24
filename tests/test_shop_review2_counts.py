"""Counts: a recount can be asked for any count that disagrees with another live count, and for a reviewed count the shop would no longer
post. Each test fails on the code before the fix."""
from core import shop

from .test_fms import seen, sign_in, site
from .test_fms_renting_pages import desk_of
from .test_fms_shop_pages import hidden, part, post, state_of, today_item


def row(page, count):
    """The one line of the Stock counts section that belongs to that count."""
    return next(a for a in part(page, "counts").split("<article") if f'name="about" value="{count}"' in a)


def test_a_count_that_matches_the_books_but_disagrees_with_another_count_can_be_recounted(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        st, fd, ident = state_of(app, c)
        [seeded] = st.counts                                          # the books say 12 and staff counted 9
        assert post(c, "record_count", product="disc lock", quantity="12").status_code == 303
        st, *_ = state_of(app, c)
        matching = next(i for i in st.counts if i != seeded)
        assert st.counts[matching]["variance"] == 0 and st.in_conflict(st.counts[matching])
        page = c.get("/shop").text
        line = seen(row(page, matching))
        assert "The count matches the books" in line and "disagrees with this one; a recount is needed" in line
        assert 'value="ask_recount"' in row(page, matching) and 'value="review_count"' not in row(page, matching)      # a recount, never a posting
        assert post(c, "ask_recount", about=matching).status_code == 303
        st, *_ = state_of(app, c)
        assert st.counts[matching]["review"]["recount"] is True
        asked = part(c.get("/shop").text, "counts")
        assert "Recount asked for. Waiting for the new count" in seen(asked)            # asked once; the page waits for the new count
        assert asked.count(f'name="about" value="{matching}"') == 0


def test_a_reviewed_count_the_shop_will_no_longer_post_can_be_recounted_and_its_item_closes(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        count = hidden(part(c.get("/shop").text, "counts"), "about")
        assert fd.staff_shop(ident, shop.Act("early-review", "review_count", "staff:office_manager", count, {"reason": "Looked at the shelf"}))
        delivery = hidden(part(c.get("/shop").text, "deliveries"), "about")
        assert post(c, "receive_delivery", about=delivery, quantity="4", reason="Staff counted four boxes").status_code == 303       # stock moved after the review
        page = c.get("/shop").text
        line = row(page, count)
        assert "Post the difference to stock" not in seen(line) and "has not been reviewed against the current stock" in seen(line)
        assert 'value="ask_recount"' in line                                             # the way forward is offered
        assert today_item(c, "Stock count needs review")                                 # still open: no record has settled it
        assert post(c, "ask_recount", about=count).status_code == 303
        assert shop.state(fd.ready().books(fd.stream(ident))).counts[count]["review"]["recount"] is True
        assert post(c, "record_count", product="disc lock", quantity="16").status_code == 303      # the new count, matching the books
        assert today_item(c, "Stock count needs review") is None                         # closed on the record of the newer count
