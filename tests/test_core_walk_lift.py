"""Recording an overlock as lifted while a lock nobody has identified is still seen. The collections command (lift_overlock) is refused when
the unit's standing observation, taken after the overlock was recorded, shows a lock whose owner is unknown: whose lock it is must be
identified first. A facility overlock seen on the door does not stop it (staff take it off, then record it), and neither does a tenant's
own lock, no lock, or an unknown lock. The refusal is in plain words and nothing else changes."""
from datetime import timedelta

from core import delinquency as col
from core import walk
from core.billing import Ledger
from core.books import at_local
from core.gate import Command, execute
from tests.test_core_walk_compare import FRONT, LATE, NOW, STREAM, WALKED, World

MANAGER = "staff:office_manager"
REFUSAL = ("a lock is still seen on this unit and nobody has said whose it is; identify the lock first, "
           "then the overlock can be recorded as lifted")


def paid_after_the_overlock(w):
    """The tenant of LATE pays in full two days after the overlock was recorded: the lift's own conditions are met."""
    with w.store.write():
        led = Ledger(w.store.books("base"))
        led.receive_payment(w.tenants[LATE], 12000, WALKED - timedelta(days=1), WALKED - timedelta(days=1))
        w.store.append("base", led.pending)


def lift(w, n=1):
    cmd = Command(f"lift{n}", "lift_overlock", w.tenants[LATE], MANAGER, lease=w.leases[LATE])
    return execute(w.store, STREAM, cmd, NOW)


def ready_world():
    w = World()
    w.overlock(LATE)
    paid_after_the_overlock(w)
    return w


def recorded(w):
    return bool(col.state(w.store.books(STREAM)).overlocks.get(w.leases[LATE]))


def test_with_no_walk_since_the_overlock_the_lift_is_allowed_as_before():
    w = ready_world()
    d = lift(w)
    assert d.allowed and d.check == "lifted after a confirmed payment" and not recorded(w)


def test_a_lock_whose_owner_is_unknown_seen_after_the_overlock_was_recorded_stops_the_lift():
    w = ready_world()
    w.walk(LATE, whose="unknown")                              # a padlock, and nobody could tell whose
    d = lift(w)
    assert (d.allowed, d.check) == (False, REFUSAL)
    assert recorded(w)                                         # the overlock record stands
    assert not [e for e in w.store.stream_events(STREAM) if e.kind == "overlock.lifted"]


def test_a_facility_overlock_a_tenants_lock_no_lock_or_an_unknown_lock_do_not_stop_the_lift():
    for seen in ({"whose": "facility overlock", "tag": "present"}, {"whose": "tenant"}, {"lock": "absent", "whose": "unknown"},
                 {"lock": "unknown", "whose": "unknown"}):
        w = ready_world()
        w.walk(LATE, **seen)
        d = lift(w)
        assert d.allowed, (seen, d.check)


def test_a_sighting_from_before_the_overlock_was_recorded_does_not_stop_the_lift():
    w = ready_world()
    w.walk(LATE, at=WALKED - timedelta(days=5), whose="unknown")      # the overlock was recorded on Nov 10; this look was Nov 8
    assert lift(w).allowed


def test_identifying_the_lock_in_a_later_look_or_a_correction_lets_the_lift_go_ahead():
    w = ready_world()
    w.walk(LATE, whose="unknown")
    assert not lift(w, 1).allowed
    w.walk(LATE, at=WALKED + timedelta(hours=3), whose="tenant")      # the newest walk says whose it is
    assert lift(w, 2).allowed
    w2 = ready_world()
    first = w2.walk(LATE, whose="unknown")
    _, wk = walk.load(w2.store, STREAM)
    fix = walk.run(w2.store, STREAM, walk.Act("fix", "correct_observation", FRONT, about=wk.standing(LATE).id,
                                              form={"lock": "present", "whose": "facility overlock", "tag": "present", "door": "closed"}),
                   WALKED + timedelta(hours=2))
    assert fix.allowed and first and lift(w2).allowed


def test_the_refusal_is_recorded_as_the_decision_and_a_repeat_returns_it():
    w = ready_world()
    w.walk(LATE, whose="unknown")
    first = lift(w)
    again = lift(w)
    assert not first.allowed and again.repeated and again.check == REFUSAL
    assert w.store.books(STREAM).decisions["lift1"]["check"] == REFUSAL


def test_an_older_stream_without_any_walk_reads_as_before():
    w = ready_world()
    assert w.store.books(STREAM).floor is None and lift(w).allowed
