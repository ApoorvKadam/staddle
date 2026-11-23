"""The three steps a tenant's page and Settings take that had no staff command: offer the plan in force, record the tenant's own acceptance,
and publish a new version of the plan. Each goes through one run, as staff only, and a refusal comes back in plain words with a named reason."""
from datetime import date

import pytest

from core import protection
from tests.protection_world import FRONT, MANAGER, TERMS_V2, World, day

YES = {"how": "signed agreement", "words": "I agree to the protection plan at this price.", "starts": "2026-11-05"}


def test_offer_plan_offers_the_version_in_force_and_enrols_nobody():
    w = World()
    d = w.cmd("offer_plan", day(11, 2), FRONT, lease=w.leases[0])
    e = w.state().enrolments[d.made]
    assert d.allowed and (e.state, e.version, e.cents) == ("offered", 1, 1200) and "not accepted" in d.check
    again = w.cmd("offer_plan", day(11, 3), FRONT, lease=w.leases[0])
    assert (again.allowed, again.why) == (False, "refused") and "already open" in again.check


def test_offer_plan_is_staff_only_and_needs_a_lease():
    w = World()
    assert not w.cmd("offer_plan", day(11, 2), "operator", lease=w.leases[0]).allowed
    d = w.cmd("offer_plan", day(11, 2), FRONT, lease="L9999")
    assert not d.allowed and w.state().enrolments == {}


def test_record_acceptance_keeps_the_tenants_own_words_and_the_terms_they_saw():
    w = World()
    e = w.cmd("offer_plan", day(11, 2), FRONT, lease=w.leases[0]).made
    d = w.cmd("record_acceptance", day(11, 3), FRONT, about=e, version=1, cents=1200, form=YES)
    acc = w.state().acceptances[d.made]
    assert d.allowed and (acc.how, acc.by, acc.starts) == ("signed agreement", w.tenants[0], date(2026, 11, 5)) and w.state().enrolments[e].state == "accepted"
    assert acc.words == YES["words"] and "not active" in d.check


@pytest.mark.parametrize("over, words", [({"how": "staff tick"}, "not acceptance"), ({"words": "ok"}, "own words"), ({"starts": ""}, "day coverage starts"),
                                         ({"starts": "soon"}, "year-month-day")])
def test_record_acceptance_refuses_what_is_not_acceptance(over, words):
    w = World()
    e = w.cmd("offer_plan", day(11, 2), FRONT, lease=w.leases[0]).made
    d = w.cmd("record_acceptance", day(11, 3), FRONT, about=e, version=1, cents=1200, form={**YES, **over})
    assert not d.allowed and words in d.check and w.state().acceptances == {}


def test_record_acceptance_for_other_terms_than_the_page_showed_is_refused():
    w = World()
    e = w.cmd("offer_plan", day(11, 2), FRONT, lease=w.leases[0]).made
    d = w.cmd("record_acceptance", day(11, 3), FRONT, about=e, version=1, cents=1500, form=YES)
    assert not d.allowed and "other terms" in d.check
    d = w.cmd("record_acceptance", day(11, 3), FRONT, about=e, form=YES)
    assert not d.allowed and "version and the price" in d.check


def test_publish_plan_adds_the_next_version_and_keeps_the_old_one():
    w = World()
    d = w.cmd("publish_plan", day(11, 2), MANAGER, form={"cents": 1500, "terms": TERMS_V2, "effective": "2026-11-20"})
    versions = w.state().versions[protection.PRODUCT]
    assert d.allowed and d.made == "2" and [(v.version, v.cents) for v in versions] == [(1, 1200), (2, 1500)] and versions[1].effective == date(2026, 11, 20)
    same = w.cmd("publish_plan", day(11, 3), MANAGER, form={"cents": 1500, "terms": TERMS_V2, "effective": "2026-11-21"})
    assert not same.allowed and "already has this price" in same.check


@pytest.mark.parametrize("form, words", [({"cents": 0, "terms": "x", "effective": "2026-11-20"}, "above zero"),
                                         ({"cents": 1500, "terms": "", "effective": "2026-11-20"}, "terms in words"),
                                         ({"cents": 1500, "terms": "Terms", "effective": ""}, "day it takes effect"),
                                         ({"cents": 1500, "terms": "Terms", "effective": "2026-09-01"}, "cannot take effect before")])
def test_publish_plan_refuses_a_plan_that_cannot_stand(form, words):
    w = World()
    d = w.cmd("publish_plan", day(11, 2), MANAGER, form=form)
    assert not d.allowed and words in d.check and len(w.state().versions[protection.PRODUCT]) == 1
