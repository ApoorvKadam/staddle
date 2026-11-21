"""What staff saw on the floor, read against what the FMS records.

Each unit's standing observation (core/walk.py: the newest walk that checked it) is compared with the collections overlock record, whether
the lease has anything past due, the unit's renting status and its move-out. Every contradiction is one finding with a stable key per unit
and kind, naming the records that disagree in plain words. An "unknown" in the observation is never agreement and never a contradiction on
its own: it is one finding asking staff to look again.

This module only reads. A finding never lifts or records an overlock, ends a lease, blocks or frees a unit, posts a charge or moves money;
core/work.py turns each finding into an item of work, and staff act on the records through the commands that already exist."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import delinquency as col
from . import liens, renting
from .books import Books, local
from .evidence import clock, day, money

OFFICE, FRONT = col.OWNER_ROLE, "front_desk"
UNKNOWN_WORDS = {"lock": "whether a lock was on the door", "whose": "whose lock it was", "tag": "whether the overlock tag was there",
                 "door": "the state of the door"}


@dataclass
class Finding:
    unit: str
    slug: str                   # overlock_on_paid, overlock_not_found, lock_on_vacant, door_open, look_again
    kind: str                   # the same in words
    what: str                   # the headline
    why: str                    # the records that disagree, and what staff saw and when
    closes: str
    role: str
    due: date
    ref: str                    # the observation it is about
    tenant: str | None = None   # the tenant's name, for the words
    lease: str | None = None
    tenant_id: str | None = None

    @property
    def key(self) -> str:
        return f"walk:{self.unit}:{self.slug}"


def _ledger(books: Books, lease, now: datetime) -> str:
    name, late = books.tenants[lease.tenant].name, col.days_past_due(books, lease.id, now)
    if late is None:
        return f"The ledger shows nothing past due for {name}."
    return f"The ledger shows {money(books.past_due(lease.tenant, now))} past due for {name}, the oldest {late} days."


def _overlock(rec: dict | None, unit: str) -> str:
    return f"The FMS has an overlock on unit {unit} recorded {day(rec['at'])}." if rec else "The FMS has no overlock recorded on it."


def for_unit(books: Books, rent, unit: str, now: datetime) -> list[Finding]:
    """The findings on one unit: its contradictions, or the one look-again finding when it has none and something was unknown."""
    o = rent.floor.standing(unit)
    if o is None or unit not in books.units:
        return []
    status = rent.status(unit, now)
    lease = next((x for x in books.leases.values() if x.unit == unit and x.ended is None), None)
    rec = col.state(books).overlocks.get(lease.id) if lease else None
    late = col.days_past_due(books, lease.id, now) if lease else None
    holder = rent.holder(unit, now)
    moving = bool(holder and holder[0] == "rental" and (rent.rentals[holder[1]].state == "ending" or rent.rentals[holder[1]].moveout))
    seen, due = f"On {clock(o.at)}", local(o.at).date() + timedelta(days=1)
    name = books.tenants[lease.tenant].name if lease else None
    out: list[Finding] = []

    def found(slug: str, kind: str, what: str, why: str, closes: str, role: str) -> None:
        out.append(Finding(unit, slug, kind, what, why, closes, role, due, o.id, name, lease.id if lease else None, lease.tenant if lease else None))
    overlock_on_door = o.tag == "present" or (o.lock == "present" and o.whose == "facility overlock")
    nothing_on_door = o.lock == "absent" and o.tag == "absent"
    if status == "occupied" and lease:
        if overlock_on_door and late is None:
            found("overlock_on_paid", "overlock seen on a paid-up unit", f"Facility overlock on the door of unit {unit}, and {name} owes nothing past due",
                  f"{seen}, staff saw the facility overlock on the door of unit {unit}. {_ledger(books, lease, now)} {_overlock(rec, unit)}",
                  "A later walk finds the overlock gone, the ledger shows what is owed, or staff say what they checked. Staff take any lock off, "
                  "then record it.", OFFICE)
        if rec and nothing_on_door:
            found("overlock_not_found", "overlock recorded, none on the door", f"Overlock recorded on unit {unit}, but none found on the door",
                  f"{_overlock(rec, unit)} {seen}, staff found no lock or tag on the door. {_ledger(books, lease, now)}",
                  "A later walk finds the overlock on the door, the overlock record is lifted, or staff say what they checked.", OFFICE)
        if (o.door == "open" or (o.lock == "absent" and o.tag != "present" and not rec)) and not moving:
            found("door_open", "rented unit with an open door or no lock", f"Rented unit {unit} seen with its door open or no lock",
                  f"{seen}, staff saw the door of unit {unit} {'open' if o.door == 'open' else 'with no lock on it'}. The unit is rented to {name} "
                  "and no move-out is recorded.", "A later walk finds the door shut and locked, a move-out is recorded, or staff say what they checked.", FRONT)
    elif status == "vacant" and o.lock == "present" and o.whose in ("tenant", "unknown"):
        last = max((x.ended for x in books.leases.values() if x.unit == unit and x.ended), default=None)
        found("lock_on_vacant", "lock on a vacant unit", f"A lock on the door of unit {unit}, which renting lists as vacant",
              f"{seen}, staff saw a lock ({'the tenant\'s' if o.whose == 'tenant' else 'owner unknown'}) on the door of unit {unit}, which renting lists as "
              f"vacant{f', last rented until {day(last)}' if last else ''}.",
              "A later walk finds the unit clear, the unit is rented to the lock's owner, or staff say what they checked.", FRONT)
    if not out:
        unknown = [k for k in ("lock", "tag", "door") if getattr(o, k) == "unknown"] + (["whose"] if o.lock == "present" and o.whose == "unknown" else [])
        if unknown:
            words = [UNKNOWN_WORDS[k] for k in unknown]
            found("look_again", "unknown on the walk", f"Look again at unit {unit}: staff could not tell everything",
                  f"{seen}, staff could not tell {', '.join(words[:-1]) + ' or ' if len(words) > 1 else ''}{words[-1]} on unit {unit}; it was recorded "
                  "as unknown, which agrees with nothing in the records and contradicts nothing.",
                  "A later walk records what was seen, or staff say what they checked. Staff look again.", FRONT)
    return out


def compare(books: Books, rent, now: datetime) -> list[Finding]:
    """Every finding, by unit."""
    return [f for u in sorted(books.units) for f in for_unit(books, rent, u, now)]


def readiness(books: Books, rent, unit: str, now: datetime, claimed: bool = False) -> list[str]:
    """Why the unit cannot be offered right now, every reason in plain words and in this order, or [] when it is ready: renting does not
    call it vacant; a walk contradiction stands on it; a work order on it is open; a lease on it is still active; a legal stop or an open
    lien file covers it; its gate state is not clear. Worked out each time it is asked from the records as they stand, and stored
    nowhere, so closing a work order removes only that order's reason. A look-again finding is not a contradiction and does not stop an
    offer on its own. (A staff word that an item was checked closes the item, not the contradiction: only a later look or a record does.)
    `claimed` asks the question for a renter who already holds the unit, about to move in: what the hold itself explains (that the unit is
    reserved, that its new lease is signed) is left out, and an open work order or a legal stop still stops the move-in."""
    out: list[str] = []
    status = rent.status(unit, now)
    if status != "vacant" and not claimed:
        out.append(f"the unit is {status}")
    out += [f"a walk finding stands on the unit: {f.what[0].lower()}{f.what[1:]}" for f in for_unit(books, rent, unit, now) if f.slug != "look_again"]
    out += [f"work order {o.id} is still open ({o.problem})" for o in rent.floor.orders.values() if o.unit == unit and o.state != "closed"]
    leases = [x for x in books.leases.values() if x.unit == unit]
    if status != "occupied" and not claimed and any(x.ended is None for x in leases):
        out.append("a lease on the unit is still active")
    for x in leases:
        out += [f"a legal stop covers the unit ({liens.STOP_WORDS[s['kind']]})" for s in liens.open_stops(books, x.id) if s["lease"] == x.id]
        if liens.active_case(books, x.id):
            out.append("a lien file is open on the unit")
    holder = rent.holder(unit, now)
    if holder and holder[0] == "rental" and rent.rentals[holder[1]].state == "ended" and not claimed:
        out.append("the gate system has not been checked for a code from before Staddle" if renting.needs_clearance(rent.rentals[holder[1]])
                   else "the old gate code is not revoked yet")
    return out
