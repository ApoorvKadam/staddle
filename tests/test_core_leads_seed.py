from core import leads
from core.books import at_local
from core.seed import TODAY, build
from tests.test_core_protection_seed import money


def test_seeded_inquiries_have_no_effect_on_money():
    year = build(today=TODAY, cutoff=at_local(TODAY), standing=True)
    records = list(leads.state(year.led.books).records.values())
    assert any(x.source == "website inquiry" and x.status == "new" for x in records)
    assert any(x.matches for x in records)
    assert any(x.source == "abandoned checkout" and x.status == "converted" for x in records)
    baseline = build(today=TODAY, cutoff=at_local(TODAY), standing=True, seed_leads=False)
    assert money(year) == money(baseline)


def test_seeded_inquiry_names_use_the_tenant_name_lists():
    from core.seed import FIRST, LAST

    year = build(today=TODAY, cutoff=at_local(TODAY), standing=True)
    records = [lead for lead in leads.state(year.led.books).records.values() if lead.source != "abandoned checkout"]
    assert len(records) == 3
    assert all(len(parts := lead.name.split()) == 2 and parts[0] in FIRST and parts[1] in LAST for lead in records)
    assert all(lead.email.endswith("@example.com") or lead.phone.startswith("407555") for lead in records)
    assert not {lead.name for lead in records} & {tenant.name for tenant in year.led.books.tenants.values()}
