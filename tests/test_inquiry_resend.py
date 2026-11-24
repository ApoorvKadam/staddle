"""The online inquiry form sent twice (the browser's back and resend) makes one inquiry, and the second press shows the first result. A
refused form can be corrected and sent again. Each test fails on the code before the fix."""
import re

from core import leads

from tests.test_fms import sign_in, site
from tests.test_fms_renting_pages import desk_of


def form_token(client):
    return re.search(r'action="/rent/inquiry"><input type="hidden" name="token" value="([0-9a-f]{12})"', client.get("/rent").text).group(1)


def inquiries(app, client, name):
    fd, ident = desk_of(app, client)
    return [x for x in leads.state(fd.view(ident)[0]).records.values() if x.name == name]


def test_the_same_inquiry_form_sent_twice_makes_one_inquiry_and_shows_the_first_result(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        data = {"token": "0123456789ab", "name": "Morgan Twice", "phone": "4075550141", "message": "Boxes", "consent": "yes"}
        first = c.post("/rent/inquiry", data=data, follow_redirects=False)
        second = c.post("/rent/inquiry", data=data, follow_redirects=False)                # back and resend
        assert first.status_code == second.status_code == 303 and first.headers["location"] == second.headers["location"] == "/rent?inquiry=received"
        assert len(inquiries(app, c, "Morgan Twice")) == 1


def test_the_inquiry_page_gives_each_visit_its_own_token(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        assert form_token(c) != form_token(c)


def test_a_new_page_makes_a_new_inquiry_even_from_the_same_person(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        for _ in range(2):
            c.post("/rent/inquiry", data={"token": form_token(c), "name": "Morgan Again", "phone": "4075550142", "consent": "yes"})
        assert len(inquiries(app, c, "Morgan Again")) == 2                                 # each is a message of its own, linked by the core


def test_a_refused_inquiry_does_not_use_up_its_page(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        token = form_token(c)
        refused = c.post("/rent/inquiry", data={"token": token, "name": "Morgan Fixed"}, follow_redirects=False)      # no phone or email
        assert refused.status_code == 303 and refused.headers["location"] == "/rent#ask" and not inquiries(app, c, "Morgan Fixed")
        page = c.get("/rent").text
        assert 'role="alert"' in page and "phone" in page.lower()                         # why, beside the form, not an error page
        assert 'role="alert"' not in c.get("/rent").text                                 # shown once
        fixed = c.post("/rent/inquiry", data={"token": token, "name": "Morgan Fixed", "phone": "4075550143"}, follow_redirects=False)
        assert fixed.status_code == 303 and len(inquiries(app, c, "Morgan Fixed")) == 1


def test_a_token_that_is_not_one_the_page_made_is_refused(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        refused = c.post("/rent/inquiry", data={"token": "not-a-token", "name": "Morgan Odd", "phone": "4075550144"}, follow_redirects=False)
        assert refused.status_code == 422 and not inquiries(app, c, "Morgan Odd")
