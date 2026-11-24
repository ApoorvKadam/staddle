"""A product refusal names only the field that is wrong, not every field. Each test fails on the code before the fix."""
from .test_core_operations_set_shop import World
from .test_fms import seen, sign_in, site
from .test_fms_shop_pages import post


def refusal(w, **form):
    decision = w.do('publish_product', 'padlock', **{'price': 500, 'cost': 200, 'tax_class': 'standard', 'active': True, **form})
    assert not decision.allowed
    return decision.check


def test_a_product_refusal_names_only_the_field_that_is_wrong():
    w = World()
    price = refusal(w, price=None)
    assert 'price' in price and 'dollars' in price and 'cost' not in price and 'tax class' not in price and 'name' not in price
    cost = refusal(w, cost=-5)
    assert 'cost' in cost and 'price' not in cost and 'tax class' not in cost
    assert refusal(w, tax_class='  ') == 'Give the tax class.'
    assert refusal(w, active=None) == 'Say whether the product is on sale.'
    nameless = w.do('publish_product', '  ', price=500, cost=200, tax_class='standard', active=True)
    assert not nameless.allowed and nameless.check == 'Give the product a name.'
    both = refusal(w, price=None, tax_class='')
    assert 'price' in both and 'tax class' in both and 'cost' not in both                  # each wrong field once, the right ones not named


def test_the_product_page_names_the_price_when_only_the_price_is_wrong(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        refused = post(c, "add_product", product="Padlock", price="five", cost="2", tax_class="standard")
        note = seen(c.get(refused.headers["location"]).text)
        assert "Nothing changed. The price is not an amount of money; write it in dollars, like 12 or 12.50." in note
        assert "Give the tax class" not in note and "The cost is not" not in note and "Give the product a name" not in note      # nothing else is named
