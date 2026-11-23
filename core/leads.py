"""Inquiry records, folded from their own events."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .evidence import clock
from .gate import Decision, STAFF_ROLES
from .store import Store

from .billing import Ledger, Refused
from .books import Books

SOURCES = ("website inquiry", "call", "message", "abandoned checkout", "walk-in")
CHANNELS = ("phone", "email", "text")


@dataclass
class Lead:
    id: str
    source: str
    arrived: datetime
    name: str
    phone: str
    email: str
    size: str
    start: date | None
    words: str
    permissions: list[str]
    campaign: str
    matches: list[tuple[str, str]] = field(default_factory=list)
    stopped: bool = False
    status: str = "new"
    reason: str = ""
    duplicate_of: str | None = None
    history: list[dict] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    scheduled: dict[str, datetime] = field(default_factory=dict)


class State:
    def __init__(self):
        self.records: dict[str, Lead] = {}
        self.checkouts: dict[str, dict] = {}

    def apply(self, kind: str, at: datetime, p: dict) -> None:
        if kind == "lead.checkout_started":
            self.checkouts[p["rental"]] = {**p, "at": at, "captured": False}
        elif kind == "lead.checkout_abandoned":
            self.checkouts[p["rental"]]["captured"] = True
            self.checkouts[p["rental"]]["lead"] = p["lead"]
        elif kind == "lead.captured":
            lead = Lead(p["lead"], p["source"], at, p["name"], p["phone"], p["email"], p["size"],
                        date.fromisoformat(p["start"]) if p["start"] else None, p["words"], list(p["permissions"]), p["campaign"])
            lead.matches = [tuple(match) for match in p.get("matches", [])]
            lead.history.append({"kind": "captured", "at": at})
            self.records[lead.id] = lead
        elif p.get("lead") in self.records:
            lead = self.records[p["lead"]]
            entry = {**p, "kind": kind.removeprefix("lead."), "at": at}
            lead.history.append(entry)
            if kind == "lead.contacted":
                if p["outcome"].strip().lower() == "reached":
                    lead.status = "contacted"
                if p.get("step"):
                    lead.completed.append(p["step"])
            elif kind == "lead.scheduled":
                lead.scheduled[p["step"]] = datetime.fromisoformat(p["due"])
            elif kind == "lead.duplicate_confirmed":
                lead.status, lead.duplicate_of = "duplicate", p["other"]
                self.records[p["other"]].history.append({"kind": "duplicate_linked", "lead": lead.id, "at": at})
            elif kind == "lead.duplicate_rejected":
                lead.matches = [m for m in lead.matches if m != ("lead", p["other"])]
            elif kind == "lead.unqualified":
                lead.status, lead.reason = "unqualified", p["reason"]
            elif kind == "lead.stopped":
                lead.stopped = True
            elif kind == "lead.converted":
                lead.status = "converted"
            elif kind == "lead.status_changed":
                lead.status = p["status"]


def state(books: Books) -> State:
    return books.leads or State()


def normal_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def capture(led: Ledger, at: datetime, *, source: str, name: str, phone: str = "", email: str = "", size: str = "",
            start: str = "", words: str = "", permissions: list[str] | None = None, campaign: str = "") -> str:
    name = " ".join((name or "").split())
    phone = normal_phone(phone)
    email = (email or "").strip().lower()
    permissions = list(permissions or [])
    if source not in SOURCES or not name or not (phone or email):
        raise Refused("give a source, name and phone or email")
    if any(channel not in CHANNELS for channel in permissions):
        raise Refused("choose a contact channel")
    try:
        start_date = date.fromisoformat(start) if start else None
    except (TypeError, ValueError):
        raise Refused("enter a valid start date") from None
    matches = [("lead", other.id) for other in state(led.books).records.values()
               if other.status not in ("converted", "unqualified", "duplicate")
               and ((phone and phone == other.phone) or (email and email == other.email))]
    matches += [("tenant", tenant.id) for tenant in led.books.tenants.values()
                if (phone and phone == normal_phone(tenant.phone)) or (email and email == getattr(tenant, "email", "").strip().lower())]
    ident = led.books.next_id("LEAD")
    p = {"lead": ident, "source": source, "name": name, "phone": phone, "email": email, "size": size.strip(),
         "start": start_date.isoformat() if start_date else "", "words": words.strip(), "permissions": permissions,
         "campaign": campaign.strip(), "matches": matches}
    led.emit("lead.captured", at, p, f"lead:{ident}:captured")
    return ident


def label(step: str) -> str:
    """A follow-up step in staff words."""
    if step.startswith("follow up ") and step.endswith(" hours"):
        return f"Follow-up after {step[len('follow up '):-len(' hours')]} hours"
    return {"checkout follow up": "Checkout follow-up"}.get(step, step.capitalize())


OUTCOME_WORDS = {"reached": "reached the person", "no answer": "no answer", "left message": "left a message"}


def said(event: dict) -> str:
    """One timeline entry in staff words."""
    kind = event["kind"]
    if kind == "contacted":
        outcome = str(event.get("outcome", "")).strip().lower()
        return f"Contact attempt: {OUTCOME_WORDS.get(outcome, outcome)}"
    if kind == "scheduled":
        return f"Follow-up scheduled for {clock(datetime.fromisoformat(event['due']))}"
    if kind == "sent":
        return f"Simulated {event.get('channel', '')} message: “{event.get('text', '')}”"
    if kind == "stopped":
        return "They asked not to be contacted"
    return kind.replace("_", " ").capitalize()


# Owner's follow-up timings, measured from the inquiry.
FIRST_MINUTES = 5
ESCALATE_MINUTES = 15
LATER_HOURS = (24, 48)


def due(lead: Lead, now: datetime) -> list[tuple[str, datetime]]:
    if lead.status in ("converted", "unqualified", "duplicate"):
        return []
    arrived = lead.arrived.astimezone(timezone.utc)      # elapsed time, so a clock change never moves a deadline
    if lead.source == "website inquiry":
        steps = [("first contact", arrived + timedelta(minutes=FIRST_MINUTES)),
                 ("escalation", arrived + timedelta(minutes=ESCALATE_MINUTES))]
        steps += [(f"follow up {hours} hours", arrived + timedelta(hours=hours)) for hours in LATER_HOURS]
    elif lead.source == "abandoned checkout":
        steps = [("checkout follow up", arrived + timedelta(minutes=FIRST_MINUTES))]
    else:
        steps = [("first contact", arrived + timedelta(minutes=FIRST_MINUTES))]
    if any(h["kind"] == "contacted" and h.get("outcome", "").strip().lower() == "reached" for h in lead.history):
        steps = [(key, when) for key, when in steps if key not in ("first contact", "escalation")]
    # Today shows only the latest step due, so recording that one settles the earlier fixed steps too.
    done = [when for key, when in steps if key in lead.completed]
    steps = [(key, when) for key, when in steps if not done or when > max(done)]
    steps += list(lead.scheduled.items())
    return [(key, when) for key, when in steps if key not in lead.completed and when <= now]


@dataclass
class Act:
    id: str
    action: str
    source: str
    about: str | None = None
    form: dict | None = None


def command(led: Ledger, act: Act, now: datetime, rent=None) -> Decision:
    books = led.books
    if act.id in books.decisions:
        p = books.decisions[act.id]
        return Decision(p["allowed"], p["action"], p["check"], command=act.id, repeated=True, made=p.get("made"), why=p.get("why"))
    f = act.form or {}
    lead = state(books).records.get(act.about or "")
    ok, why, check, made = False, "refused", "That action is not available.", None
    try:
        if not act.source.startswith("staff:") or act.source[6:] not in STAFF_ROLES:
            raise Refused("Only staff may do that.")
        if act.action in ("record_call", "record_walk_in"):
            made = capture(led, now, source="call" if act.action == "record_call" else "walk-in", name=f.get("name", ""), phone=f.get("phone", ""), email=f.get("email", ""), size=f.get("size", ""), start=f.get("start", ""), words=f.get("words", ""), permissions=f.get("permissions", []))
            ok, why, check = True, None, "Inquiry recorded."
        else:
            if lead is None:
                raise Refused("There is no such inquiry.")
            if act.action not in ("send", "stop", "convert") and (lead.status == "converted" or (rent is not None and conversion_evidence(books, rent, lead))):
                why = "LEAD_ALREADY_CONVERTED"
                raise Refused("This inquiry is marked converted or has a matching rental; no follow-up was recorded.")
            payload = {"lead": lead.id}
            if act.action == "convert":
                candidates = list(conversion_candidates(books, rent, lead)) if rent is not None else []
                rental = conversion_evidence(books, rent, lead) if rent is not None else None
                chosen = str(f.get("tenant", "")).strip()
                if lead.status == "converted":
                    why = "LEAD_ALREADY_CONVERTED"
                    raise Refused("This inquiry is already converted.")
                if not rental and rent is not None:
                    renters = matching_renters(books, lead)
                    if chosen and chosen not in renters:
                        raise Refused("That renter does not share this inquiry's phone or email.")
                    if len(renters) > 1 and not chosen:
                        raise Refused("More than one renter matches this inquiry. Choose which renter it is for.")
                    candidates = [ident for ident in candidates if not chosen or rent.rentals[ident].tenant == chosen]
                    rental = candidates[0] if candidates else None
                if not rental:
                    raise Refused("A signed rental and verified payment for this person are needed first.")
                payload["rental"] = rental
                if rent is not None:
                    payload["tenant"] = rent.rentals[rental].tenant
                ok, why, check, kind = True, None, "Conversion recorded from the rental and payment.", "converted"
            elif act.action == "send":
                if lead.status == "converted" or (rent is not None and conversion_evidence(books, rent, lead)):
                    why, check = "LEAD_ALREADY_CONVERTED", "This inquiry is marked converted or has a matching rental; no follow-up was sent."
                elif any(other.stopped and (other.id == lead.id or (lead.phone and lead.phone == other.phone) or (lead.email and lead.email == other.email)) for other in state(books).records.values()):
                    why, check = "PERSON_STOPPED", "This person asked us to stop contacting them."
                elif f.get("channel") not in lead.permissions:
                    why, check = "NO_CHANNEL_PERMISSION", "This person has not given permission for that channel."
                elif not str(f.get("text", "")).strip():
                    raise Refused("Write the message first.")
                else:
                    payload.update(channel=f["channel"], text=f["text"], simulated=True)
                    ok, why, check = True, None, "Simulated message recorded."
                    kind = "sent"
            elif act.action == "contact":
                if not str(f.get("outcome", "")).strip():
                    raise Refused("Record the outcome of the contact attempt.")
                step = f.get("step", "")
                available = {key for key, _ in due(lead, now)}
                if rent is not None:
                    from . import work
                    available.update(c.key.removeprefix(f"lead:{lead.id}:") for c in work._leads(books, rent, now) if c.ref == lead.id and c.kind == "reservation reminder")
                if step and step not in available:
                    raise Refused("That follow-up is not due for this inquiry.")
                payload.update(outcome=f["outcome"], step=step)
                ok, why, check, kind = True, None, "Contact attempt recorded.", "contacted"
            elif act.action == "schedule":
                try:
                    when = datetime.fromisoformat(f.get("due", ""))
                    if when.tzinfo is None:
                        from zoneinfo import ZoneInfo
                        when = when.replace(tzinfo=ZoneInfo("America/New_York"))
                except (ValueError, TypeError):
                    raise Refused("Enter a valid follow-up date and time with a time zone.") from None
                payload.update(step=f"scheduled {act.id}", due=when.isoformat())
                ok, why, check, kind = True, None, "Follow-up scheduled.", "scheduled"
            elif act.action == "unqualify":
                if not str(f.get("reason", "")).strip():
                    raise Refused("Give a reason.")
                payload["reason"] = f["reason"]
                ok, why, check, kind = True, None, "Inquiry closed with a reason.", "unqualified"
            elif act.action in ("confirm_duplicate", "reject_duplicate"):
                other = f.get("other", "")
                if ("lead", other) not in lead.matches or other not in state(books).records or other == lead.id:
                    raise Refused("That matching inquiry is not on record.")
                if act.action == "confirm_duplicate" and state(books).records[other].status == "duplicate":
                    raise Refused("Choose the original inquiry, not another duplicate.")
                payload["other"] = other
                kind = "duplicate_confirmed" if act.action == "confirm_duplicate" else "duplicate_rejected"
                ok, why, check = True, None, "Matching inquiry reviewed."
            elif act.action == "stop":
                ok, why, check, kind = True, None, "Request to stop recorded.", "stopped"
            if ok:
                led.emit(f"lead.{kind}", now, payload, f"lead:{act.id}", source=act.source, command=act.id)
                made = lead.id
    except Refused as exc:
        check = str(exc)
    led.emit("gate.decided", now, {"id": act.id, "action": act.action, "source": act.source, "about": act.about, "command": act.id,
                                 "allowed": ok, "check": check, "why": why, "made": made}, f"gate:{act.id}", source=act.source, command=act.id)
    return Decision(ok, act.action, check, command=act.id, made=made, why=why)


def run(store: Store, stream: str, act: Act, now: datetime) -> Decision:
    with store.write():
        from . import renting
        books, rent = renting.load(store, stream)
        led = Ledger(books)
        decision = command(led, act, now, rent)
        if not decision.repeated:
            decision.events = [event.id for event in store.append(stream, led.pending) if event.id is not None]
        return decision


def matching_renters(books: Books, lead: Lead) -> list[str]:
    """Renters, including ones created after the inquiry, whose phone or email is the inquiry's."""
    return [ident for ident, tenant in books.tenants.items()
            if (lead.phone and normal_phone(tenant.phone) == lead.phone) or (lead.email and (getattr(tenant, "email", "") or "").strip().lower() == lead.email)]


def conversion_candidates(books: Books, rent, lead: Lead, *, require_match: bool = True):
    """Find rentals with matching contact details and qualifying payment evidence."""
    for ident, rental in rent.rentals.items():
        tenant = books.tenants.get(rental.tenant)
        if not tenant or (require_match and not ((lead.phone and normal_phone(tenant.phone) == lead.phone) or
                              (lead.email and getattr(tenant, "email", "").strip().lower() == lead.email))):
            continue
        if getattr(rental, "provenance", None):
            lease = books.leases.get(rental.lease or "")
            if lease and lease.signed_at and lease.signed_at >= lead.arrived and any(
                payment.tenant == rental.tenant and not payment.returned and payment.known_at >= lead.arrived and payment.effective_at >= lead.arrived
                for payment in books.payments.values()
            ):
                yield ident
            continue
        agreement = rent.agreements.get(rental.agreement or "")
        if not rental.accepted or not agreement or agreement.state != "accepted" or not agreement.at or agreement.at < lead.arrived:
            continue
        if any((payment := books.payments.get(pid)) and not payment.returned and payment.known_at >= lead.arrived and payment.effective_at >= lead.arrived for pid in rental.payments):
            yield ident


def conversion_evidence(books: Books, rent, lead: Lead) -> str | None:
    linked = {checkout["rental"] for checkout in state(books).checkouts.values() if checkout.get("lead") == lead.id}
    linked.update(h["rental"] for h in lead.history if h["kind"] == "converted" and h.get("rental"))
    return next((ident for ident in conversion_candidates(books, rent, lead, require_match=False) if ident in linked), None)


def convert(led: Ledger, rent, now: datetime) -> None:
    for lead in list(state(led.books).records.values()):
        if lead.status not in ("converted", "unqualified", "duplicate") and (rental := conversion_evidence(led.books, rent, lead)):
            led.emit("lead.converted", now, {"lead": lead.id, "rental": rental}, f"lead:{lead.id}:converted")


def capture_abandoned(led: Ledger, rent, now: datetime) -> None:
    for checkout in list(state(led.books).checkouts.values()):
        rental = rent.rentals.get(checkout["rental"])
        if checkout["captured"] or not rental or not rental.expires_at or rental.expires_at > now or rental.payments or rent.uncertain(rental) or rental.state in ("active", "ending", "ended"):
            continue
        tenant = led.books.tenants.get(checkout["tenant"])
        if not tenant or not tenant.phone:
            continue
        ident = capture(led, checkout["at"], source="abandoned checkout", name=tenant.name, phone=tenant.phone, size=checkout.get("size", ""))
        led.emit("lead.checkout_abandoned", now, {"rental": checkout["rental"], "lead": ident}, f"lead:abandoned:{checkout['rental']}")


def refresh_status(led: Ledger, rent, now: datetime) -> None:
    for lead in state(led.books).records.values():
        if lead.status in ("converted", "unqualified", "duplicate"):
            continue
        reserved = any((tenant := led.books.tenants.get(res.tenant)) and ((lead.phone and normal_phone(tenant.phone) == lead.phone) or
                       (lead.email and getattr(tenant, "email", "").strip().lower() == lead.email)) and res.state in ("held", "awaiting_checkout", "confirmed") and res.expires_at > now
                       for res in rent.reservations.values())
        status = "reserved" if reserved else "follow-up due" if due(lead, now) else "contacted" if any(h["kind"] == "contacted" and h.get("outcome", "").strip().lower() == "reached" for h in lead.history) else "new"
        if status != lead.status:
            led.emit("lead.status_changed", now, {"lead": lead.id, "status": status}, f"lead:{lead.id}:status:{len(lead.history)}")
