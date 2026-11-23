from datetime import timedelta
from types import SimpleNamespace

from core import leads, work
from core.billing import Ledger
from core.seed import NOW


def test_lead_today_chips_describe_steps_without_repeating_name():
    ledger = Ledger()
    lead_id = leads.capture(ledger, NOW, source='website inquiry', name='Jordan Rivera', phone='4075550177')
    rent = SimpleNamespace(rentals={}, agreements={}, reservations={})
    first, = [item for item in work._leads(ledger.books, rent, NOW + timedelta(minutes=6)) if item.ref == lead_id]
    escalation, = [item for item in work._leads(ledger.books, rent, NOW + timedelta(minutes=16)) if item.ref == lead_id]
    later, = [item for item in work._leads(ledger.books, rent, NOW + timedelta(hours=25)) if item.ref == lead_id]
    assert (first.what, escalation.what, later.what) == ('First contact due', 'Escalation due', 'Follow-up due')


def test_move_in_reminder_has_short_chip():
    ledger = Ledger()
    lead_id = leads.capture(ledger, NOW, source='call', name='Jordan Rivera', phone='4075550177')
    ledger.books.tenants['T1'] = SimpleNamespace(id='T1', phone='4075550177')
    reservation = SimpleNamespace(id='RS1', tenant='T1', state='held', start=(NOW + timedelta(days=1)).date(), expires_at=NOW + timedelta(hours=1))
    rent = SimpleNamespace(rentals={}, agreements={}, reservations={'RS1': reservation})
    reminder, = [item for item in work._leads(ledger.books, rent, NOW) if item.ref == lead_id and item.kind == 'reservation reminder']
    assert reminder.what == 'Move-in reminder due'
