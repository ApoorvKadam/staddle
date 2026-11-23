"""Today shows one step per lead, the latest one due; recording it settles the earlier fixed steps, so they never come back."""
from datetime import datetime, timedelta, timezone

from core import leads

ARRIVED = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)


def lead(**kw):
    base = dict(id="LEAD0009", source="website inquiry", arrived=ARRIVED, name="Ana Ruiz", phone="4075550199", email="", size="",
                start=None, words="", permissions=[], campaign="")
    return leads.Lead(**(base | kw))


def test_recording_the_escalation_settles_the_first_contact():
    now = ARRIVED + timedelta(minutes=20)
    assert [k for k, _ in leads.due(lead(), now)] == ["first contact", "escalation"]
    done = lead(completed=["escalation"], history=[{"kind": "contacted", "outcome": "no answer", "at": now}])
    assert leads.due(done, now) == []
    later = ARRIVED + timedelta(hours=25)
    assert [k for k, _ in leads.due(done, later)] == ["follow up 24 hours"]


def test_step_labels_read_as_words():
    assert leads.label("follow up 24 hours") == "Follow-up after 24 hours"
    assert leads.label("escalation") == "Escalation"
    assert leads.label("checkout follow up") == "Checkout follow-up"


def test_a_clock_change_does_not_move_a_deadline():
    from zoneinfo import ZoneInfo
    arrived = datetime(2025, 11, 1, 8, 30, tzinfo=ZoneInfo("America/New_York"))      # clocks went back on Nov 2
    steps = dict(leads.due(lead(arrived=arrived), arrived + timedelta(days=5)))
    assert steps["follow up 48 hours"] == arrived.astimezone(timezone.utc) + timedelta(hours=48)


def test_timeline_entries_read_as_words():
    assert leads.said({"kind": "contacted", "outcome": "left message"}) == "Contact attempt: left a message"
    assert leads.said({"kind": "scheduled", "due": "2026-11-25T10:00:00-05:00"}).startswith("Follow-up scheduled for Nov 25 at 10:00")
    assert leads.said({"kind": "stopped"}) == "They asked not to be contacted"
