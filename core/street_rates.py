"""Versioned staff proposals for the prices shown to new renters."""
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime

from . import price_law, renting
from .billing import Ledger
from .books import local
from .store import Store


@dataclass(frozen=True)
class Limits:
    max_up_cents: int
    max_down_cents: int
    floor_cents: int
    ceiling_cents: int


@dataclass(frozen=True)
class Version:
    id: str
    version: int
    status: str
    size: str
    current_cents: int
    new_cents: int
    reason: str
    proposed_by: str
    limits: dict
    by: str
    effective_on: str
    disclosure: dict | None = None


@dataclass(frozen=True)
class Result:
    allowed: bool
    check: str
    made: str | None = None


def history(store: Store, stream: str) -> list[Version]:
    return [Version(**e.payload) for e in store.events(stream) if e.kind == "street_rate.versioned"]


def _write(led, now, version):
    led.emit("street_rate.versioned", now, asdict(version), f"street-rate:{version.id}:{version.version}", source=version.by)


def _valid_limits(limits):
    values = (limits.max_up_cents, limits.max_down_cents, limits.floor_cents, limits.ceiling_cents)
    return all(type(v) is int and v >= 0 for v in values) and 0 < limits.floor_cents <= limits.ceiling_cents



def set_limits(store: Store, stream: str, now: datetime, *, size: str, limits: Limits, by: str) -> Result:
    with store.write():
        books, rent = renting.load(store, stream)
        if by != "owner" or not size or not _valid_limits(limits):
            return Result(False, "The owner must set valid limits for a size.")
        if not any(u.size == size for u in rent.units.values()):
            return Result(False, "There is no unit of that size.")
        events = [e for e in store.events(stream) if e.kind == "street_rate.limits" and e.payload["size"] == size]
        led = Ledger(books)
        led.emit("street_rate.limits", now, {"size": size, "limits": asdict(limits)},
                 f"street-rate:limits:{size}:{len(events) + 1}", source=by)
        store.append(stream, led.pending)
        return Result(True, "Owner limits saved.")


def _limits(store, stream, size):
    events = [e for e in store.events(stream) if e.kind == "street_rate.limits" and e.payload["size"] == size]
    return Limits(**events[-1].payload["limits"]) if events else None


def _change(version):
    return price_law.PriceChange(version.id, "street_rate", version.current_cents, version.new_cents,
                                 date.fromisoformat(version.effective_on), unit_class=version.size, disclosure=version.disclosure)


def _legal(books, version, now, point):
    return price_law.evaluate(books, _change(version), now, point=point)

def _check_rate(rent, size, current, new, limits):
    units = [u for u in rent.units.values() if u.size == size]
    if not units or any(u.rent_cents != current for u in units):
        return "The current rate must match every unit of this size."
    if type(new) is not int or new <= 0 or not _valid_limits(limits):
        return "Enter valid rates and owner limits."
    if not limits.floor_cents <= new <= limits.ceiling_cents:
        return "The proposed rate is outside the owner's floor or ceiling."
    if new - current > limits.max_up_cents or current - new > limits.max_down_cents:
        return "The proposed rate exceeds the owner's step limit."
    return None



def _stopped(store, stream, books, now, size, by, reason):
    ident = f"SRS{len(books.street_rate_stops) + 1:04d}"
    led = Ledger(books)
    led.emit("street_rate.stopped", now, {"id": ident, "size": size, "reason": reason},
             f"street-rate:stopped:{ident}", source=by)
    store.append(stream, led.pending)
    return Result(False, reason)

def propose(store: Store, stream: str, now: datetime, *, size: str, current_cents: int, new_cents: int,
            reason: str, by: str, limits: Limits, disclosure: dict | None = None,
            effective_on: date | None = None) -> Result:
    with store.write():
        books, rent = renting.load(store, stream)
        if not by.startswith("staff:") or not by[6:].strip() or not reason.strip():
            return Result(False, "Staff and a reason are required.")
        if effective_on is not None and (type(effective_on) is not date or effective_on < local(now).date()):
            return _stopped(store, stream, books, now, size, by, "Enter a valid effective date that has not passed.")
        configured = _limits(store, stream, size)
        if configured is None or limits != configured:
            return _stopped(store, stream, books, now, size, by, "Use the owner's current limits for this size.")
        bad = _check_rate(rent, size, current_cents, new_cents, configured)
        if type(current_cents) is not int or type(new_cents) is not int or current_cents <= 0 or new_cents <= 0:
            return _stopped(store, stream, books, now, size, by, bad or "Enter valid rates.")
        versions = history(store, stream)
        ident = f"SR{len({v.id for v in versions}) + 1:04d}"
        version = Version(ident, 1, "proposed", size, current_cents, new_cents, reason.strip(), by, asdict(configured), by, (effective_on or local(now).date()).isoformat(), disclosure)
        result = _legal(books, version, now, "proposal")
        if bad or not result.allowed:
            return _stopped(store, stream, books, now, size, by, " ".join(x for x in (bad, result.words if not result.allowed else "") if x))
        led = Ledger(books)
        price_law.decide(led, now, _change(version), point="proposal")
        _write(led, now, version)
        store.append(stream, led.pending)
        return Result(True, "Proposed.", ident)


def _advance(store, stream, now, ident, by, expected, status):
    with store.write():
        books, rent = renting.load(store, stream)
        versions = [v for v in history(store, stream) if v.id == ident]
        if not by.startswith("staff:") or not by[6:].strip():
            return Result(False, "Only staff may do this.")
        if not versions or versions[-1].status != expected:
            return Result(False, "The proposal is not at the required stage.")
        version = versions[-1]
        if status == "published" and date.fromisoformat(version.effective_on) != local(now).date():
            return Result(False, "Publish on the approved effective date.")
        configured = _limits(store, stream, version.size)
        if configured is None:
            return Result(False, "The owner must set limits for this size.")
        bad = _check_rate(rent, version.size, version.current_cents, version.new_cents, configured)
        point = "approval" if status == "approved" else "publication"
        result = _legal(books, version, now, point)
        if bad or not result.allowed:
            return Result(False, " ".join(x for x in (bad, result.words if not result.allowed else "") if x))
        led = Ledger(books)
        price_law.decide(led, now, _change(version), point=point)
        if status == "approved":
            price_law.record_approval(led, now, change=ident, versions=result.versions, by=by, snapshot=price_law._revision(_change(version)))
        _write(led, now, replace(version, version=version.version + 1, status=status, by=by))
        if status == "published":
            for unit in rent.units.values():
                if unit.size == version.size:
                    led.emit("unit.priced", now, {"unit": unit.id, "size": unit.size, "rent_cents": version.new_cents},
                             f"street-rate:{ident}:unit:{unit.id}", source=by)
        store.append(stream, led.pending)
        return Result(True, status.capitalize() + ".", ident)


def approve(store: Store, stream: str, now: datetime, ident: str, *, by: str) -> Result:
    return _advance(store, stream, now, ident, by, "proposed", "approved")


def publish(store: Store, stream: str, now: datetime, ident: str, *, by: str) -> Result:
    return _advance(store, stream, now, ident, by, "approved", "published")
