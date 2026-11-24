"""Promotion versions: an explicit schedule, each change a new version, the old ones kept, and terms that are not complete refused."""
from datetime import date

import pytest

from core import promotions
from core.billing import Ledger, Refused
from core.books import at_local
from core.promotions import PromotionRefused

BY = "staff:office_manager"
WHEN = at_local(date(2026, 10, 1), 9)


def terms(**over):
    base = {"name": "$1 first month", "unit_types": ["10x10"], "customers": "new", "price_cents": 100, "covers": "rest_of_first_month",
            "proration": "by_days", "expires": "2026-12-31", "stacks_with": [], "transfer": "end", "max_redemptions": 5, "max_concession_cents": 60000}
    base.update(over)
    return base


def test_a_complete_promotion_is_version_one_and_keeps_its_schedule_as_written():
    led = Ledger()
    assert promotions.publish_version(led, "first-month", terms(), date(2026, 10, 1), BY, WHEN) == 1
    (v,) = promotions.state(led.books).versions["first-month"]
    assert (v.version, v.effective, v.by) == (1, date(2026, 10, 1), BY)
    assert v.terms == terms() and v.terms["covers"] == "rest_of_first_month" and v.terms["expires"] == "2026-12-31"


def test_each_change_is_a_new_version_and_the_old_one_stays_in_the_history():
    led = Ledger()
    promotions.publish_version(led, "first-month", terms(), date(2026, 10, 1), BY, WHEN)
    n = promotions.publish_version(led, "first-month", terms(max_redemptions=8), date(2026, 11, 1), BY, WHEN)
    first, second = promotions.state(led.books).versions["first-month"]
    assert n == 2 and first.terms["max_redemptions"] == 5 and second.terms["max_redemptions"] == 8
    assert promotions.version_in_force(led.books, "first-month", date(2026, 10, 15)).version == 1
    assert promotions.version_in_force(led.books, "first-month", date(2026, 11, 2)).version == 2


@pytest.mark.parametrize("missing", ["name", "unit_types", "customers", "covers", "proration", "expires", "stacks_with", "transfer",
                                     "max_redemptions", "max_concession_cents"])
def test_terms_with_a_part_missing_are_refused_as_incomplete_and_nothing_is_written(missing):
    led = Ledger()
    bad = terms()
    del bad[missing]
    with pytest.raises(PromotionRefused) as r:
        promotions.publish_version(led, "first-month", bad, date(2026, 10, 1), BY, WHEN)
    assert r.value.code == "PROMOTION_TERMS_INCOMPLETE" and missing.replace("_", " ") in str(r.value)
    assert led.pending == [] and led.books.promotions is None


@pytest.mark.parametrize("bad", [{"price_cents": None, "percent": None}, {"price_cents": 100, "percent": 50}, {"unit_types": []}, {"percent": 0, "price_cents": None},
                                 {"covers": "whenever"}, {"covers": "first_full_month", "proration": "by_days"}, {"covers": "rest_of_first_month", "proration": "none"},
                                 {"customers": "everyone"}, {"transfer": "maybe"}, {"max_redemptions": 0}, {"max_concession_cents": 0}, {"expires": "soon"}])
def test_terms_that_contradict_themselves_or_name_no_deal_are_refused(bad):
    led = Ledger()
    t = {k: v for k, v in terms(**bad).items() if v is not None}
    with pytest.raises(PromotionRefused) as r:
        promotions.publish_version(led, "first-month", t, date(2026, 10, 1), BY, WHEN)
    assert r.value.code == "PROMOTION_TERMS_INCOMPLETE" and led.pending == []


def test_a_version_that_changes_nothing_or_starts_before_the_last_is_refused():
    led = Ledger()
    promotions.publish_version(led, "first-month", terms(), date(2026, 10, 1), BY, WHEN)
    with pytest.raises(Refused, match="needs different terms"):
        promotions.publish_version(led, "first-month", terms(), date(2026, 11, 1), BY, WHEN)
    with pytest.raises(Refused, match="before the version it follows"):
        promotions.publish_version(led, "first-month", terms(max_redemptions=9), date(2026, 9, 1), BY, WHEN)
    assert len(promotions.state(led.books).versions["first-month"]) == 1


def test_only_staff_or_the_owner_publish_a_promotion():
    led = Ledger()
    for who in ("renter", "operator", "code", "staff:janitor"):
        with pytest.raises(Refused, match="only staff or the owner"):
            promotions.publish_version(led, "first-month", terms(), date(2026, 10, 1), who, WHEN)
    assert led.pending == []
