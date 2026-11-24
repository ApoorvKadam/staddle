"""The check that finds a give-away not taken out of stock says so in plain words, not in the words of a code message. Each test fails on
the code before the fix."""
from core import shop

from .test_core_operations_set_shop import World
from .test_fms import seen, sign_in, site
from .test_fms_shop_pages import part, post


def test_a_give_away_handed_over_but_not_taken_out_of_stock_says_so_in_plain_words():
    w = World()
    w.stock(5)
    assert w.do('record_handover', product='lock', quantity=2, reason='Goodwill', approval='Manager', handover='Handed over').allowed
    assert shop.differences(w.state()) == ['A give-away was handed over but is not taken out of stock yet']


def test_the_records_check_on_the_shop_page_uses_the_plain_words(fms_url, sim):
    with sign_in(site(fms_url)) as c:                       # the seeded give-away is handed over and not yet taken out of stock
        assert post(c, "record_count", product="disc lock", quantity="0").status_code == 303       # any shop step runs the daily check
        records = seen(part(c.get("/shop").text, "records"))
        assert "A give-away was handed over but is not taken out of stock yet" in records
        assert "does not match one stock adjustment" not in records
