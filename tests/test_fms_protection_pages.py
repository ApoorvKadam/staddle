"""Protection on the FMS pages: a tenant's page tells the tenant's own policy and the facility's plan in words and takes every staff step through
the one protection gate; each copy is its own, a refusal is said once at the section, and no page shows a record id or calls the plan insurance."""
import re
from datetime import date

from fastapi.testclient import TestClient

from core import protection
from core.billing import Ledger
from core.books import PROTECTION
from desk.fms import SESSION
from tests.test_fms import record_of, seen, sign_in, site
from tests.test_fms_collections import token

IDS = re.compile(r"\b(?:W|O|WO|PL|PR|EX|PI|TK|RT|L|T|C|PE|PV|PA|PN|PC)\d{4}\b")
INSURANCE = re.compile(r"insur\w*|premium\w*|underwrit\w*|reimburs\w*|indemni\w*|coverage", re.I)
HOW = "Read the declarations page: carrier, policy number and dates match what the tenant typed in."


def person(client, name: str, unit: str = "") -> str:
    """A tenant's page by name; two tenants can share a name, so a unit they rent picks the right one."""
    if not unit:
        return record_of(client, name)
    return re.search(rf'href="(/tenants/{name.split()[0].lower()}-[a-z0-9-]+)"', client.get("/tenants", params={"q": unit}).text).group(1)


def section(page: str) -> str:
    """The text of the Protection section alone."""
    return seen(page.split('id="protection"', 1)[1].split("</section>", 1)[0])


def step(client, url, page, **fields):
    return client.post(url + "/protection", data={"token": token(page), **fields}, follow_redirects=False)


def hidden(page: str, name: str) -> str:
    """The first hidden value of that name inside the Protection section."""
    return re.search(rf'name="{name}" value="([A-Z0-9]+)"', page.split('id="protection"', 1)[1]).group(1)


def clean(text: str) -> str:
    """The section's words with the two lines that say what the plan is not, so a word like insurance can be searched for."""
    return text.replace("no insurance policy issued", "").replace("not confirmed with the insurer", "")


def test_the_seeded_policies_are_told_in_words(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        yara = section(client.get(record_of(client, "Yara Quinn")).text)
        assert "Harbor Mutual" in yara and "TEMP" in yara and "Needs review" in yara and "The policy number looks like a placeholder." in yara
        luis = section(client.get(record_of(client, "Luis Ortiz")).text)
        assert "SM-5521077" in luis and "Expired" in luis and "SM-6630411" in luis and "Document received, not reviewed" in luis
        dev = section(client.get(person(client, "Dev Patel", "A131")).text)
        assert "Reviewed by Office manager" in dev
        assert "Read the declarations page" in dev and "Checked: carrier, policy number, dates" in dev
        assert "Staff review of the document; not confirmed with the insurer" in dev and "Expired" in dev


def test_the_seeded_plans_are_told_in_words_and_never_called_insurance(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        tara = section(client.get(record_of(client, "Tara Singh")).text)
        assert "Version 1" in tara and "$12.00 a month" in tara and "Active" in tara
        assert "Simulated protection-plan enrolment, no insurance policy issued" in tara
        assert "Yes, add the protection plan for $12 a month." in tara and "signed agreement" in tara and "November 2026" in tara
        assert "Offer version 2 at $15.00" in tara          # a newer version is in force and she is on the first
        olga = section(client.get(record_of(client, "Olga Rivera")).text)
        assert "Active" in olga and "End the plan" in olga
        for text in (tara, olga, section(client.get(record_of(client, "Yara Quinn")).text)):
            assert not IDS.search(text) and not INSURANCE.search(clean(text)), text


def test_a_policy_is_recorded_and_reviewed_through_the_gate(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client, sign_in(app) as other:
        url = record_of(client, "Cole Jensen")
        page = client.get(url).text
        assert "No policy of the tenant's own is on file" in section(page)
        lease = hidden(page, "lease")
        step(client, url, page, action="record_policy", lease=lease, carrier="Sunrise Mutual", number="SM-7788123", effective="2026-11-01", expires="2027-10-31",
             document="declarations page.pdf", statement="")
        text = section(client.get(url).text)
        assert "Sunrise Mutual" in text and "SM-7788123" in text and "Document received, not reviewed" in text
        assert "Sunrise Mutual" not in seen(other.get(url).text)                      # each copy is its own
        page = client.get(url).text
        policy = hidden(page, "policy")
        step(client, url, page, action="review_policy", policy=policy, outcome="accepted", how="Looks fine.")
        refused = seen(client.get(url + "?at=protection").text)
        assert "Nothing changed. No staff review of the document is recorded" in refused
        assert "Nothing changed." not in seen(client.get(url).text)                    # said once
        page = client.get(url).text
        step(client, url, page, action="review_policy", policy=policy, outcome="accepted", how=HOW, check_carrier="1", check_policy_number="1", check_dates="1")
        text = section(client.get(url).text)
        assert "Reviewed by staff" in text and "Staff review of the document; not confirmed with the insurer" in text and HOW in text
        assert "Review this policy" not in text                                     # nothing left to review


def test_a_statement_alone_is_recorded_as_the_tenants_words(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        url = record_of(client, "Cole Jensen")
        page = client.get(url).text
        step(client, url, page, action="record_policy", lease=hidden(page, "lease"), statement="I am covered through my car policy.")
        text = section(client.get(url).text)
        assert "The tenant's words only, no document" in text and "I am covered through my car policy." in text


def test_the_plan_is_offered_accepted_enrolled_and_ended_through_the_gate(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        url = record_of(client, "Cole Jensen")
        page = client.get(url).text
        lease = hidden(page, "lease")
        step(client, url, page, action="offer_plan", lease=lease)
        page = client.get(url).text
        assert "Offered, waiting for the tenant's yes" in section(page)
        plan, version, cents = hidden(page, "plan"), hidden(page, "version"), hidden(page, "cents")
        yes = dict(action="record_acceptance", plan=plan, version=version, cents=cents, words="Yes, add the protection plan.", starts="2026-11-14")
        step(client, url, page, how="staff tick", **yes)
        assert "Nothing changed. Acceptance must be explicit" in seen(client.get(url + "?at=protection").text)
        page = client.get(url).text
        step(client, url, page, how="signed agreement", **yes)
        text = section(client.get(url).text)
        assert "Accepted, not active yet" in text and "Yes, add the protection plan." in text and "signed agreement" in text
        page = client.get(url).text
        step(client, url, page, action="enroll_plan", plan=plan)
        text = section(client.get(url).text)
        assert "Active" in text and "November 2026" in text and "Simulated protection-plan enrolment, no insurance policy issued" in text
        page = client.get(url).text
        step(client, url, page, action="end_plan", plan=plan, on="2026-12-01", reason="move-out")
        text = section(client.get(url).text)
        assert "Ended" in text and "Move-out" in text and "Dec 1" in text
        today = seen(client.get("/today").text)
        assert "Offered the protection plan for Cole Jensen" in today and "Ended a protection plan for Cole Jensen" in today
        assert "offer_plan" not in today and "end_plan" not in today
        assert not IDS.search(seen(client.get(url).text)), "no record id on the tenant's page"


def test_plan_charges_show_their_month_and_whether_a_hold_stands_on_them(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client:
        fd, ident = app.state.front_desk, app.state.guard.valid(client.cookies.get(SESSION), "session")
        url = person(client, "Tara Singh")
        lease, store, stream, now = hidden(client.get(url).text, "lease"), fd.ready(), fd.stream(ident), fd.clock()
        assert "Plan charges" not in section(client.get(url).text)                       # none posted yet
        ok = protection.run(store, stream, protection.Act("charge-1", "post_plan_charge", "staff:front_desk", lease=lease, period="2026-11", cents=1200), now)
        assert ok.allowed, ok.check
        with store.write():                                                              # a charge posted by hand at a price nobody accepted
            led = Ledger(store.books(stream))
            led.charge_by_hand(now, lease, PROTECTION, 1500, f"protection:{lease}:2026-12", date(2026, 12, 1), "Protection plan, 2026-12", "front_desk")
            store.append(stream, led.pending)
        text = section(client.get(url).text)
        assert "Plan charges" in text and "November 2026 B226 $12.00 Owed No hold" in text
        assert "December 2026 B226 $15.00 Owed Held The plan was accepted at $12.00, not $15.00." in text
        assert not IDS.search(text) and not INSURANCE.search(clean(text))


def test_a_refusal_names_no_record(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        url = person(client, "Cole Jensen")
        lease = hidden(client.get(url).text, "lease")
        step(client, url, client.get(url).text, action="offer_plan", lease=lease)
        step(client, url, client.get(url).text, action="offer_plan", lease=lease)
        text = seen(client.get(url + "?at=protection").text)
        assert "Nothing changed. An offer is already open for this lease." in text and not IDS.search(text)
        policy = dict(action="record_policy", lease=lease, carrier="Sunrise Mutual", number="SM-7788123", effective="2026-11-01", expires="2027-10-31",
                      document="declarations page.pdf")
        step(client, url, client.get(url).text, **policy)
        step(client, url, client.get(url).text, **policy)
        text = seen(client.get(url + "?at=protection").text)
        assert "Nothing changed. That document is already on record." in text and not IDS.search(text)


def test_protection_items_on_today_open_the_protection_section(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        page = client.get("/today").text
        work = page.split('id="h-work"', 1)[1]
        items = [a for a in re.findall(r'(?s)<article class="fms-line".*?</article>', work) if re.search(r"protection plan|policy", seen(a), re.I)]
        assert len(items) == 5, [seen(a)[:80] for a in items]
        for a in items:
            assert re.search(r'<a href="/tenants/[a-z0-9-]+#protection">', a), seen(a)[:120]
        assert not IDS.search(seen(page))


def test_a_step_on_another_tenants_record_is_refused(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        tara = client.get(record_of(client, "Tara Singh")).text
        plan = hidden(tara, "plan")
        cole = record_of(client, "Cole Jensen")
        page = client.get(cole).text
        assert step(client, cole, page, action="end_plan", plan=plan, on="2026-12-01", reason="move-out").status_code == 404
        assert step(client, cole, page, action="mark_covered").status_code == 422
        assert client.post(cole + "/protection", data={"action": "offer_plan"}, follow_redirects=False).status_code == 422     # no page token


def test_protection_steps_need_a_session(fms_url, sim):
    with TestClient(site(fms_url)) as client:
        assert client.post("/tenants/unknown/protection", follow_redirects=False).headers["location"] == "/sign-in"


def panel(page: str) -> str:
    """The text of the Protection plan panel on Settings."""
    return seen(page.split('id="protection-plan"', 1)[1].split("</section>", 1)[0])


def publish(client, page, **fields):
    data = {"token": token(page), "price": "18.00", "terms": "Simulated protection plan, version 3: a monthly fee for the plan's own listed benefits.",
            "effective": "2026-12-01", **fields}
    return client.post("/rules/protection-plan", data=data, follow_redirects=False)


def test_settings_list_every_version_of_the_plan(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        text = panel(client.get("/rules").text)
        assert "Protection plan" in text and "Plan version 1" in text and "Plan version 2 In force" in text
        assert "$12.00 a month" in text and "$15.00 a month" in text and "Takes effect" in text and "Published by Office manager" in text
        assert not IDS.search(text) and not INSURANCE.search(text.lower().replace("no insurance policy is issued", ""))


def test_a_new_version_is_published_through_the_gate_and_the_old_one_stays(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client, sign_in(app) as other:
        page = client.get("/rules").text
        done = publish(client, page)
        assert done.status_code == 303 and done.headers["location"] == "/rules#protection-plan"
        text = panel(client.get("/rules").text)
        assert "Plan version 3" in text and "$18.00 a month" in text and "Waiting for its day" in text and "Plan version 2 In force" in text and "Plan version 1" in text
        assert "Plan version 3" not in panel(other.get("/rules").text)                               # each copy is its own
        assert "Nothing changed" not in seen(client.get("/rules").text)
        text = panel(client.get("/rules").text)
        assert not INSURANCE.search(text.lower().replace("no insurance policy is issued", ""))


def test_a_version_that_cannot_stand_is_refused_once_above_the_panels(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        for fields, words in (({"price": "soon"}, "above zero"), ({"terms": ""}, "terms in words"),
                              ({"effective": "2026-09-01"}, "cannot take effect before"), ({"effective": ""}, "day it takes effect")):
            done = publish(client, client.get("/rules").text, **fields)
            assert done.headers["location"] == "/rules#in-force"
            text = seen(client.get("/rules").text)
            assert "Nothing changed. " in text and words in text, (fields, text[:400])
            assert "Nothing changed." not in seen(client.get("/rules").text)             # said once
        assert publish(client, client.get("/rules").text, price="18.00", terms="Terms").headers["location"] == "/rules#protection-plan"
        again = publish(client, client.get("/rules").text, price="18.00", terms="Terms")
        assert again.headers["location"] == "/rules#in-force"
        assert "Nothing changed. The latest version already has this price and these terms" in seen(client.get("/rules").text)


def test_publishing_needs_a_session(fms_url, sim):
    with TestClient(site(fms_url)) as client:
        assert client.post("/rules/protection-plan", follow_redirects=False).headers["location"] == "/sign-in"


def row_of(page: str, link: str, unit: str) -> str:
    """The words of one Rentals row: the tenant's link and the unit name it."""
    return next(seen(tr).strip() for tr in re.findall(r"(?s)<tr>.*?</tr>", page) if f'href="{link}' in tr and f"<td>{unit}</td>" in tr)


def test_rentals_say_what_protection_each_rental_has_from_the_records(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        page = client.get("/rentals").text
        assert seen(re.search(r"(?s)<thead>.*?</thead>", page).group(0)).strip().endswith("Protection")
        widths = {len(re.findall(r"<td\b", tr)) for tr in re.findall(r"(?s)<tr>.*?</tr>", page.split("<tbody>", 1)[1])}
        assert widths == {8}                     # every row has the column, so the table stays even
        tara, luis, yara, dev = person(client, "Tara Singh"), person(client, "Luis Ortiz"), person(client, "Yara Quinn"), person(client, "Dev Patel", "A131")
        assert row_of(page, tara, "B226").endswith("Plan active")
        assert row_of(page, luis, "C317").endswith("Own policy needs review")      # the renewal came in and nobody has read it
        assert row_of(page, yara, "C319").endswith("Own policy needs review")
        assert row_of(page, dev, "A131").endswith("Own policy expired")
        assert row_of(page, dev, "B204").endswith("None")
        assert not IDS.search(seen(page))


def test_a_reviewed_policy_reads_reviewed(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        url = person(client, "Cole Jensen")
        page = client.get(url).text
        step(client, url, page, action="record_policy", lease=hidden(page, "lease"), carrier="Sunrise Mutual", number="SM-7788123", effective="2026-11-01",
             expires="2027-10-31", document="declarations page.pdf")
        page = client.get(url).text
        policy = hidden(page, "policy")
        assert row_of(client.get("/rentals").text, url, "B218").endswith("Own policy needs review")
        step(client, url, page, action="review_policy", policy=policy, outcome="accepted", how=HOW, check_carrier="1", check_policy_number="1", check_dates="1")
        assert row_of(client.get("/rentals").text, url, "B218").endswith("Own policy reviewed")


# the five protection items the live copy opens on Today, each closed the way staff would: from Today to the tenant's page, through its forms ----
def today_item(client, view: str, what: str, who: str) -> str:
    """The Today article for one tenant's item of that kind, in that view, or an empty string when it is not there."""
    work = client.get(f"/today?view={view}").text.split('id="h-work"', 1)[1]
    arts = re.findall(r'(?s)<article class="fms-line".*?</article>', work)
    return next((a for a in arts if what in seen(a) and who in seen(a)), "")


def open_from_today(client, what: str, who: str) -> tuple[str, str]:
    """Follow the item's own link from Today to the tenant's page: its address and the page."""
    art = today_item(client, "all", what, who)
    assert art, f"{what} for {who} is not on Today"
    url = re.search(r'<a href="(/tenants/[a-z0-9-]+)#protection">', art).group(1)
    return url, client.get(url).text


def finished(client, what: str, who: str) -> str:
    """The words of the item once it is finished: gone from the open list, and on Done today with the evidence it shows."""
    assert not today_item(client, "all", what, who), f"{what} for {who} is still open"
    return seen(today_item(client, "done", what, who))


def review(client, url, page, policy, **fields):
    return step(client, url, page, action="review_policy", policy=policy, how=HOW, check_carrier="1", check_policy_number="1", check_dates="1", **fields)


def test_item_plan_still_active_after_move_out_closes_by_ending_the_plan(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        url, page = open_from_today(client, "Plan still active after move-out", "Olga Rivera")
        assert "The lease on unit B210 ended Sep 30, 2026, so the plan stops covering the day after." in section(page)
        plan = hidden(page, "plan")
        step(client, url, page, action="end_plan", plan=plan, on="2026-11-14", reason="move-out")                      # a day long after she left
        refused = seen(client.get(url + "?at=protection").text)
        assert "Nothing changed. The lease ended Sep 30, so the plan cannot cover days after that" in refused
        assert today_item(client, "all", "Plan still active after move-out", "Olga Rivera")
        page = client.get(url).text
        day_after = re.search(r'name="on" required value="([\d-]+)"', page).group(1)                                  # the form offers the day after her last day
        assert day_after == "2026-10-01"
        step(client, url, page, action="end_plan", plan=plan, on=day_after, reason="move-out")
        assert "Ended" in section(client.get(url).text) and "Move-out" in section(client.get(url).text)
        assert "The plan was ended." in finished(client, "Plan still active after move-out", "Olga Rivera")


def test_item_luis_policy_to_review_closes_when_the_renewal_is_reviewed(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        url, page = open_from_today(client, "Policy to review", "Luis Ortiz")
        review(client, url, page, hidden(page, "policy"), outcome="accepted")
        assert "Reviewed by staff" in section(client.get(url).text)
        assert "Staff reviewed the document and recorded the result." in finished(client, "Policy to review", "Luis Ortiz")


def test_item_luis_policy_expired_closes_when_the_renewal_is_reviewed(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        url, page = open_from_today(client, "Policy expired", "Luis Ortiz")
        review(client, url, page, hidden(page, "policy"), outcome="accepted")
        assert "A renewal was reviewed." in finished(client, "Policy expired", "Luis Ortiz")


def test_item_yara_placeholder_number_closes_when_staff_reject_the_document_with_a_reason(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        url, page = open_from_today(client, "Policy number looks like a placeholder", "Yara Quinn")
        review(client, url, page, hidden(page, "policy"), outcome="rejected", note="TEMP is a placeholder, asked her for the real declarations page")
        assert "Rejected" in section(client.get(url).text)
        assert "Staff reviewed the document and recorded the result." in finished(client, "Policy number looks like a placeholder", "Yara Quinn")


def test_item_dev_policy_expired_closes_when_a_renewal_is_recorded_and_reviewed(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        url, page = open_from_today(client, "Policy expired", "Dev Patel")
        lease = re.search(r'<option value="(L\d+)">Unit A131</option>', page).group(1)
        step(client, url, page, action="record_policy", lease=lease, carrier="Coastal Mutual", number="CM-7710999", effective="2026-11-08", expires="2027-11-07",
             document="declarations page.pdf", statement="")
        page = client.get(url).text
        assert "Document received, not reviewed" in section(page)
        review(client, url, page, re.search(r'name="policy" value="(PV\d{4})"', page).group(1), outcome="accepted")
        assert "A renewal was reviewed." in finished(client, "Policy expired", "Dev Patel")


def test_item_dev_policy_expired_also_closes_the_same_day_when_staff_say_they_asked_for_a_renewal(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        art = today_item(client, "all", "Policy expired", "Dev Patel")
        assert "A renewal is reviewed, or staff say they asked the tenant." in seen(art)
        key = re.search(r'id="item-([^"]+)"', art).group(1)
        today = client.get("/today").text
        client.post("/work/step", data={"token": token(today), "key": key, "view": "all", "action": "finish", "text": "Called Dev, he will bring the new declarations page Monday."},
                    follow_redirects=False)
        done = finished(client, "Policy expired", "Dev Patel")
        assert "Staff checked: Called Dev, he will bring the new declarations page Monday." in done and "A renewal was reviewed." not in done      # said, not a review
