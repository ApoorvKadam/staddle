"""Review fixes on the move-in checkout: a form without the confirmed total is never accepted or charged, and the add-on rules around an
attempt that may still settle. Each test fails on the code before the fix."""
import re

import pytest

from core import renting

from .test_fms import seen, sign_in, site
from .test_fms_renting_pages import desk_of, new_tenant, start_rental, token, vacant
from .test_shop_review2_money import rent_of_desk
from .test_shop_review_fixes import STAFF


@pytest.mark.parametrize("total", [None, "", "abc", "-5", "12.5"])
def test_a_checkout_form_without_its_confirmed_total_accepts_and_charges_nothing(fms_url, sim, total):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        unit, = vacant(fd, ident)
        tenant = new_tenant(fd, ident)
        _, rn = start_rental(fd, ident, tenant, unit, accept=False)
        renting.add_shop_items(fd.ready(), fd.stream(ident), rn, [("disc lock", 1)], STAFF, fd.clock())      # the price moved while the page was open
        page = c.get(f"/rent/checkout/{rn}").text
        data = {"token": token(page), "card": "card_ok", "accept": "yes", "sha256": re.search(r'name="sha256" value="([^"]+)"', page).group(1)}
        if total is not None:
            data["total"] = total
        refused = c.post(f"/rent/checkout/{rn}", data=data, follow_redirects=False)
        assert refused.status_code == 303 and refused.headers["location"] == f"/rent/checkout/{rn}?changed=1"       # sent back to see the current total
        assert not fd.processor.payments and not rent_of_desk(fd, ident).rentals[rn].accepted
        due = re.search(r'name="total" value="(\d+)"', page).group(1)
        paid = c.post(f"/rent/checkout/{rn}", data={**data, "total": due}, follow_redirects=False)                # the same page with its total is taken
        assert paid.headers["location"] == f"/rent/result/{rn}" and len(fd.processor.payments) == 1
