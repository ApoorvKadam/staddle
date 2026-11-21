"""The seeded walk: on the live copy's year, one finished walk of eight units, written as synthetic staff observations. Four of the
units carry a planted situation (a paid tenant whose unit still has the facility overlock, an FMS overlock with no lock on the door, a
tenant lock on a vacant unit, a finished move-out with a repair unfinished) and four are ordinary and agree with the records. Which
unit carries which is known only to the seed (`Year.walked`) and these tests, never marked in a record. The seed moves no money."""
from collections import Counter
from datetime import date, timedelta

from core import delinquency as col
from core import reconcile, renting, walk, work
from core.books import at_local
from core.seed import NOW, TODAY, build
from core.store import Store

STREAM = "visitor:a"
EVENT_FIELDS = {"walk", "observation", "unit", "lock", "whose", "tag", "door", "note", "by", "supersedes"}


def world(today=TODAY, **kw):
    y = build(today=today, cutoff=at_local(today), standing=True, **kw)
    store = Store()
    with store.write():
        store.append("base", y.led.pending)
    return y, store


def paid_up(b, lease, now):
    """Nothing is past due on the lease: all that was due has been paid (on the 1st the new month's rent is not yet due)."""
    return col.days_past_due(b, lease.id, now) is None


def check_planted(y, store, today=TODAY):
    """Every planted situation is true of the records, and the four ordinary units agree with them."""
    books, rent = renting.load(store, "base")
    wk = walk.load(store, "base")[1]
    now = at_local(today)
    assert list(wk.walks) == ["W0001"]
    w = wk.walks["W0001"]
    assert w.finished and w.not_checked == () and len(w.route) == 8 and list(w.route) == sorted(w.route)
    assert set(w.route) == {y.walked["paid_overlocked"], y.walked["overlock_no_lock"], y.walked["lock_on_vacant"], y.walked["repair"],
                            *y.walked["ordinary"]}
    seen = {u: w.current(u) for u in w.route}
    assert all(o.evidence == "staff attested" and o.by == "staff:front_desk" and o.at < now for o in seen.values())
    assert w.started < now and w.finished[1] < now
    lease_of = {x.unit: x for x in books.leases.values() if x.ended is None}
    ended_of = {x.unit: x for x in books.leases.values() if x.ended}
    overlocks = col.state(books).overlocks

    # a tenant who paid in full, and the facility overlock and its tag still on the door
    u = y.walked["paid_overlocked"]
    assert paid_up(books, lease_of[u], now) and rent.status(u, now) == "occupied"
    assert (seen[u].lock, seen[u].whose, seen[u].tag) == ("present", "facility overlock", "present")
    # the FMS records an overlock, and no lock was found on the door
    u = y.walked["overlock_no_lock"]
    assert lease_of[u].id in overlocks and not paid_up(books, lease_of[u], now)
    assert overlocks[lease_of[u].id]["at"] < w.started
    assert (seen[u].lock, seen[u].tag) == ("absent", "absent")
    # a tenant's own lock on a unit the FMS lists as vacant
    u = y.walked["lock_on_vacant"]
    assert rent.status(u, now) == "vacant" and u not in lease_of
    assert (seen[u].lock, seen[u].whose) == ("present", "tenant")
    # a move-out that is finished (the lease ended, the unit vacant) and a repair not finished, with its open order
    u = y.walked["repair"]
    assert u in ended_of and u not in lease_of and rent.status(u, now) == "vacant" and seen[u].door == "damaged"
    orders = [o for o in wk.orders.values() if o.unit == u]
    assert len(orders) == 1 and orders[0].state not in ("work recorded", "staff checked", "closed") and orders[0].reported < w.started
    assert orders[0].reported > at_local(ended_of[u].ended)
    assert [o.unit for o in wk.orders.values()] == [u]
    # the other four agree with the records
    for u in y.walked["ordinary"]:
        occupied = u in lease_of
        assert rent.status(u, now) == ("occupied" if occupied else "vacant")
        assert not occupied or paid_up(books, lease_of[u], now)
        assert (seen[u].lock, seen[u].tag, seen[u].door) == (("present", "absent", "closed") if occupied else ("absent", "absent", "closed"))
        assert not occupied or seen[u].whose == "tenant"
        assert lease_of.get(u) is None or lease_of[u].id not in overlocks
    assert len(y.walked["ordinary"]) == 4 and sum(u in lease_of for u in y.walked["ordinary"]) == 3
    return w


def test_the_live_copy_opens_with_one_finished_walk_of_eight_units_and_each_planted_situation_is_true_of_the_records():
    y, store = world()
    w = check_planted(y, store)
    assert w.started.date() == TODAY - timedelta(days=1)            # the year ends at midnight, so the walk is yesterday's


def test_each_planted_situation_holds_on_other_days_the_live_copy_can_open_on():
    for today in (date(2026, 11, 1), date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 30), date(2026, 12, 5), date(2027, 1, 20)):
        y, store = world(today)
        check_planted(y, store, today)


def test_nothing_in_a_record_says_which_unit_carries_which_situation():
    y, store = world()
    events = [e for e in store.stream_events("base") if e.kind.startswith(("walk.", "work_order."))]
    assert Counter(e.kind for e in events) == {"walk.started": 1, "walk.observed": 8, "walk.finished": 1, "work_order.reported": 1,
                                               "work_order.triaged": 1, "work_order.assigned": 1}
    assert all(set(e.payload) == EVENT_FIELDS for e in events if e.kind == "walk.observed")
    words = " ".join(str(e.payload) for e in events).lower()
    for giveaway in ("plant", "seed", "synthetic", "test", "situation", "drift", "disagree", "mismatch"):
        assert giveaway not in words


def test_the_seed_changes_no_money_figure_of_the_year():
    y, store = world()
    journal = store.journal("base")
    totals = Counter()
    for _, account, cents, _ in journal:
        totals[account.split(":")[0]] += cents
    books = store.books("base")
    figures = (len(journal), dict(totals), len(books.charges), len(books.payments), sum(books.credit.values()), len(books.tenants), len(books.leases),
               sum(books.past_due(t, NOW) for t in books.tenants), sum(1 for t in books.tenants if books.past_due(t, NOW)))
    # read off the year before the walk was added
    assert figures == (6317, {"receivable": 302100, "income": -21888188, "clearing": 21595088, "contra": 2000, "credit": -11000}, 1675, 1286,
                       11000, 124, 125, 299600, 23)
    assert sum(totals.values()) == 0
    walked = [e.id for e in store.stream_events("base") if e.kind.startswith(("walk.", "work_order.", "overlock."))]
    lines = store.db.execute("SELECT COUNT(*) FROM journal_lines WHERE event_id IN (%s)" % ",".join(map(str, walked))).fetchone()[0]
    assert walked and lines == 0


def test_the_fixed_books_the_graded_sets_read_have_no_walk():
    y = build()
    assert not [e for e in y.led.pending if e.kind.startswith(("walk.", "work_order.", "overlock."))] and y.walked == {}


def seeded_items(store, now):
    books, rent, wk = work.load(store, STREAM)
    return {i.key: i for i in work.queue(books, rent, wk, now) if i.source in ("walk", "maintenance")}


def test_the_four_planted_situations_each_show_as_exactly_one_item_and_the_ordinary_units_show_none():
    y, store = world()
    now = at_local(TODAY, 10)
    got = seeded_items(store, now)
    w = y.walked
    assert sorted(got) == sorted([f"walk:{w['paid_overlocked']}:overlock_on_paid", f"walk:{w['overlock_no_lock']}:overlock_not_found",
                                  f"walk:{w['lock_on_vacant']}:lock_on_vacant", "maintenance:WO0001"])
    assert got["maintenance:WO0001"].unit == w["repair"]
    assert not {i.unit for i in got.values()} & set(w["ordinary"])
    assert [i.source for i in got.values() if i.unit == w["repair"]] == ["maintenance"]       # the repair unit's door and lock agree with the records
    assert all(i.why and i.closes for i in got.values())


def test_a_repeated_sweep_and_a_restart_make_no_second_item_of_the_seeded_walk(tmp_path):
    y, _ = world()
    path = str(tmp_path / "copy.db")
    store = Store(path)
    with store.write():
        store.append("base", y.led.pending)
    now = at_local(TODAY, 10)
    first = work.sweep(store, STREAM, now)
    assert first >= 4 and work.sweep(store, STREAM, now) == 0 and work.sweep(store, STREAM, now + timedelta(minutes=5)) == 0
    keys = sorted(seeded_items(store, now))
    restarted = Store(path)
    assert work.sweep(restarted, STREAM, now) == 0 and sorted(seeded_items(restarted, now)) == keys
    opened = [e.payload["key"] for e in restarted.stream_events(STREAM) if e.kind == "work.opened"]
    assert len(opened) == len(set(opened))


def test_the_repair_unit_is_not_offered_while_its_order_is_open_and_is_when_it_closes():
    y, store = world()
    unit, now = y.walked["repair"], at_local(TODAY, 10)
    books, rent = renting.load(store, STREAM)
    why = "work order WO0001 is still open (Door latch bent after the move-out; the door will not stay shut.)"
    assert reconcile.readiness(books, rent, unit, now) == [why]
    tenant = next(iter(books.tenants))
    d = renting.run(store, STREAM, renting.Act("r1", "reserve", "renter", unit, tenant), now)
    assert (d.allowed, d.check) == (False, why)
    for n, (action, kw) in enumerate((("record_order_work", {"text": "New latch fitted"}), ("check_order_work", {"text": "Door shuts and locks"}),
                                      ("close_work_order", {}))):
        assert walk.run(store, STREAM, walk.Act(f"c{n}", action, "staff:office_manager", about="WO0001", **kw), now).allowed
    books, rent = renting.load(store, STREAM)
    assert reconcile.readiness(books, rent, unit, now) == []
    assert renting.run(store, STREAM, renting.Act("r2", "reserve", "renter", unit, tenant), now).allowed
    other = y.walked["lock_on_vacant"]                           # the lock on a vacant unit keeps that one back until a later look agrees
    assert reconcile.readiness(books, rent, other, now)[0].startswith("a walk finding stands on the unit")
    assert reconcile.readiness(books, rent, y.walked["ordinary"][-1], now) == []         # the ordinary vacant unit is ready


def test_a_copy_sees_the_seeded_walk_and_numbers_its_own_after_it():
    y, store = world()
    d = walk.run(store, STREAM, walk.Act("n1", "start_walk", "staff:front_desk", route=[y.walked["repair"]]), at_local(TODAY, 10))
    assert d.allowed and d.made == "W0002"
    assert sorted(walk.load(store, STREAM)[1].walks) == ["W0001", "W0002"]
    assert sorted(walk.load(store, "visitor:b")[1].walks) == ["W0001"]
    other = walk.run(store, "visitor:b", walk.Act("n2", "report_work_order", "staff:manager", unit=y.walked["lock_on_vacant"], text="Lock to cut off"), at_local(TODAY, 10))
    assert other.made == "WO0002" and "WO0002" not in walk.load(store, STREAM)[1].orders
