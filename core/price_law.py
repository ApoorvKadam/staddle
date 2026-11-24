"""The legal rules a price change must pass: notice length and how notice is served, disclosed maximums, and price limits during a
declared emergency, which differ by place.

Everything here is code, by design: no model reads or decides any of it. A rule set is an immutable, versioned record with its
citation, its dates, the place it covers and whether anyone has reviewed it. The records a decision rests on (the facility's legal
profile, the lease terms taken from the signed agreement, disclosed maximums, emergency restrictions and the staff check that they
were looked up, notice service evidence) are folded from their own `price_law.*` events into Books.price_law, like the lien and
lead records. A decision is recorded with the reasons it gave, the records it read and the rule versions in force, and a posted
invoice keeps the snapshot it was decided on. Like the lien workflow, anything the records cannot establish fails closed."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from typing import TYPE_CHECKING

from .billing import Ledger, Refused

if TYPE_CHECKING:
    from .books import Books

# how a notice can reach a tenant; a text message and a dashboard banner are named so they can be refused in words
CHANNELS = ("hand", "first_class_mail", "email", "text", "dashboard")
CHANNEL_WORDS = {"hand": "hand delivery", "first_class_mail": "first-class mail", "email": "e-mail", "text": "a text message",
                 "dashboard": "a notice in the tenant's online account"}
# where in a price change's life the law is checked; each runs the same evaluator over the records as they stand then
POINTS = ("proposal", "approval", "publication", "notice_authorization", "notice_completion", "quote_acceptance", "activation", "invoice")
COMMITMENT_KINDS = ("first_year", "anniversary")
REVIEWS = ("reviewed", "demonstration", "pending")
# the statutes below predate this system's records; their enactment dates are not needed, so the window opens at a floor
LONG_IN_FORCE = date(2000, 1, 1)
NYC_NAMES = ("new york", "new york city", "manhattan", "brooklyn", "queens", "bronx", "the bronx", "staten island")
SHA = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class RuleSet:
    """One version of one place's rule on one topic. Never edited: a change of law is a new version that names a new
    effective date, and a decision names the version it read."""
    id: str
    version: int
    place: str                          # CA, UT, NYC, FL
    topic: str                          # notice, disclosure, emergency
    name: str
    citation: str
    effective_from: date
    effective_to: date | None
    review: str                         # reviewed: counsel or a named reviewer checked it; demonstration: modeled for tests and the
                                        # evaluation only; pending: not reviewed, so it only holds
    reviewer: str
    reviewed_on: date | None
    source: str                         # where the rule's text was read
    notice_days: int | None = None
    agreement_specific: bool = False    # the days and methods come from the signed agreement, not from a statute
    methods: tuple[str, ...] = ()
    email_needs_agreement: bool = False
    designated_method: bool = False     # the notice goes the way the tenant designated
    first_year_max: bool = False        # a first-year maximum must be disclosed and then holds
    first_page: bool = False            # the disclosure must be prominent, on the agreement's first page
    annual_max_notice_days: int | None = None
    recurring_fee: bool = False         # the disclosed maximum is the whole recurring fee, not the base rent alone
    emergency_cap_percent: int | None = None
    emergency_hold: bool = False        # no numeric limit is modeled: a price change under the declaration waits for documented review

    @property
    def approval_hash(self) -> str:
        body = json.dumps(asdict(self), sort_keys=True, default=lambda v: v.isoformat() if isinstance(v, date) else str(v))
        return hashlib.sha256(body.encode()).hexdigest()

    def in_force(self, on: date) -> bool:
        return self.effective_from <= on and (self.effective_to is None or on <= self.effective_to)


_REVIEWED_BY = "Staddle design review, 2026-11-23"
_PENDING = "none: counsel review pending"
RULE_SETS: tuple[RuleSet, ...] = (
    RuleSet("CA-notice", 1, "CA", "notice", "California notice of a change in rental terms", "California Business and Professions Code 21715.5",
            LONG_IN_FORCE, None, "demonstration", _PENDING, None, "advisor research, 2026-11-23",
            notice_days=30, methods=("hand", "first_class_mail", "email"), email_needs_agreement=True),
    RuleSet("CA-disclosure", 1, "CA", "disclosure", "California first-page disclosure for new agreements",
            "California Business and Professions Code 21715.2 (SB 709)", date(2026, 1, 1), None, "demonstration", _PENDING, None,
            "advisor research, 2026-11-23", first_year_max=True, first_page=True),
    RuleSet("CA-emergency", 1, "CA", "emergency", "California price limit during a declared emergency", "California Penal Code 396(b)",
            LONG_IN_FORCE, None, "demonstration", _PENDING, None, "advisor research, 2026-11-23", emergency_cap_percent=10),
    RuleSet("UT-notice", 1, "UT", "notice", "Utah notice of a change in rent", "Utah Code 38-8-6",
            LONG_IN_FORCE, None, "demonstration", _PENDING, None, "advisor research, 2026-11-23",
            notice_days=30, methods=("first_class_mail", "email")),
    RuleSet("NYC-notice", 1, "NYC", "notice", "New York City notice before an occupancy fee increase",
            "New York City Administrative Code 20-566.4", LONG_IN_FORCE, None, "demonstration", _PENDING, None, "advisor research, 2026-11-23",
            notice_days=60, methods=("hand", "first_class_mail", "email"), designated_method=True),
    RuleSet("NYC-disclosure", 1, "NYC", "disclosure", "New York City disclosed maximum occupancy fee",
            "New York City Administrative Code 20-566.4 and the city's rules under it", LONG_IN_FORCE, None, "demonstration", _PENDING, None,
            "advisor research, 2026-11-23", first_year_max=True, annual_max_notice_days=60, recurring_fee=True),
    RuleSet("FL-emergency", 1, "FL", "emergency", "Florida price gouging during a declared emergency", "Florida Statutes 501.160",
            LONG_IN_FORCE, None, "reviewed", _REVIEWED_BY, date(2026, 11, 23), "advisor research, 2026-11-23", emergency_hold=True),
)


def rules_in_force(place: str | None, on: date, rules: tuple[RuleSet, ...] | None = None) -> list[RuleSet]:
    """The rule sets of this place whose dates cover `on`. Two versions of one rule never both cover a day: the later one is the
    one in force."""
    found: dict[str, RuleSet] = {}
    for rs in (RULE_SETS if rules is None else rules):
        if rs.place == place and rs.in_force(on) and (rs.id not in found or rs.version > found[rs.id].version):
            found[rs.id] = rs
    return list(found.values())


def place_of(city: str) -> str | None:
    """The place whose rules a facility falls under, from its city text ("Orlando, FL"): a state code, or NYC for the city, since
    New York City's rules are the city's and not the state's. None when the text names no state."""
    match = re.search(r"^(.*?),?\s*([A-Z]{2})\s*$", (city or "").strip())
    if not match:
        return None
    name, state = match.group(1).strip().lower(), match.group(2)
    return "NYC" if state == "NY" and name in NYC_NAMES else state


@dataclass(frozen=True)
class FacilityProfile:
    facility: str
    city: str
    place: str | None
    synthetic: bool = False             # a made-up facility for the tests and the evaluation; the live facility is never synthetic
    reviewed_by: str = ""


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())


def _date(value, label: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise Refused(f"enter a valid {label}")
    return value


def _sha(value, label: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise Refused(f"the {label} needs the document's fingerprint")
    return value


@dataclass(frozen=True)
class Terms:
    """What the signed agreement says about changing the price, frozen at the version taken from it."""
    lease: str
    version: int
    signed_on: date
    agreement_sha256: str
    notice_methods: tuple[str, ...]
    notice_days: int | None             # the agreement's own notice period, where it states one
    email_notice_authorized: bool
    last_known_address: str
    last_known_email: str
    designated_method: str | None       # the way the tenant asked to be notified (New York City)
    price_promise_until: date | None
    price_promise_max_cents: int | None     # None with a promise date means the price is promised not to rise at all
    rent_change_allowed: bool           # the agreement lets the operator change rent during the term
    at: datetime


@dataclass(frozen=True)
class Commitment:
    """A disclosed maximum the facility is held to: stated for a covered period, with the document that stated it."""
    id: str
    lease: str
    kind: str
    max_cents: int
    covers_from: date
    covers_to: date
    disclosed_on: date
    evidence_sha256: str
    at: datetime


@dataclass(frozen=True)
class Restriction:
    id: str
    place: str
    name: str
    source: str
    starts: date
    ends: date | None
    products: tuple[str, ...]
    scope: str
    at: datetime


@dataclass(frozen=True)
class EmergencyCheck:
    """Staff looked up whether a declaration covers the facility's place. It is good until `valid_until`; after that the status is
    unknown again."""
    id: str
    place: str
    checked_on: date
    valid_until: date
    by: str
    source: str
    active: tuple[str, ...]
    at: datetime


@dataclass(frozen=True)
class Baseline:
    id: str
    restriction: str
    subject: str                        # a lease id or a unit class
    price_cents: int
    observed_on: date
    source: str
    review: str                         # reviewed or pending
    by: str
    at: datetime


@dataclass(frozen=True)
class Notice:
    """The stages of one notice, kept apart: attempted, dispatch accepted, qualifying service (decided by the evaluator against the
    place's rule), delivered, acknowledged."""
    id: str
    change: str
    lease: str
    document_sha256: str
    recipient: str
    channel: str
    sent_to: str
    attempted_on: date
    dispatch_accepted: bool
    certificate_of_mailing: bool
    postage_prepaid: bool
    confirmation: str | None
    delivered_on: date | None
    acknowledged_on: date | None
    by: str
    at: datetime


@dataclass(frozen=True)
class Authorization:
    """A documented legal exception, recorded by the owner for one change at one price. Never an agent's or a click's judgment."""
    id: str
    change: str
    basis: str
    covers_cents: int
    document_sha256: str
    by: str
    at: datetime


@dataclass(frozen=True)
class Reason:
    code: str
    kind: str                           # refuse, hold, owner, note
    words: str
    cite: str                           # the rule set id, or "owner rule"
    version: int | None = None


@dataclass(frozen=True)
class Recorded:
    id: str
    change: str
    point: str
    outcome: str
    reasons: tuple[Reason, ...]
    records: tuple[str, ...]
    versions: dict
    lease: str | None
    invoice: str | None
    snapshot: dict
    at: datetime


@dataclass(frozen=True)
class Approval:
    change: str
    versions: dict
    by: str
    at: datetime


class State:
    def __init__(self):
        self.profile: FacilityProfile | None = None
        self.terms: dict[str, list[Terms]] = {}
        self.commitments: dict[str, list[Commitment]] = {}
        self.emergencies: dict[str, Restriction] = {}
        self.checks: list[EmergencyCheck] = []
        self.baselines: list[Baseline] = []
        self.notices: dict[str, list[Notice]] = {}
        self.authorizations: dict[str, list[Authorization]] = {}
        self.approvals: dict[str, Approval] = {}
        self.decisions: list[Recorded] = []

    def apply(self, kind: str, at: datetime, p: dict) -> tuple[str, str] | None:
        """Fold one `price_law.*` event. Returns the (prefix, id) the books count, or None."""
        day = lambda key: date.fromisoformat(p[key]) if p.get(key) else None
        if kind == "price_law.profile_set":
            self.profile = FacilityProfile(p["facility"], p["city"], p["place"], False, p["reviewed_by"])
        elif kind == "price_law.terms_snapshotted":
            self.terms.setdefault(p["lease"], []).append(Terms(
                p["lease"], p["version"], day("signed_on"), p["agreement_sha256"], tuple(p["notice_methods"]), p["notice_days"],
                p["email_notice_authorized"], p["last_known_address"], p["last_known_email"], p["designated_method"], day("price_promise_until"),
                p["price_promise_max_cents"], p["rent_change_allowed"], at))
        elif kind == "price_law.commitment_recorded":
            self.commitments.setdefault(p["lease"], []).append(Commitment(
                p["id"], p["lease"], p["kind"], p["max_cents"], day("covers_from"), day("covers_to"), day("disclosed_on"), p["evidence_sha256"], at))
            return "PLC", p["id"]
        elif kind == "price_law.emergency_declared":
            self.emergencies[p["id"]] = Restriction(p["id"], p["place"], p["name"], p["source"], day("starts"), day("ends"),
                                                    tuple(p["products"]), p["scope"], at)
            return "PLE", p["id"]
        elif kind == "price_law.emergency_extended":
            self.emergencies[p["id"]] = replace(self.emergencies[p["id"]], ends=day("ends"), source=p["source"])
        elif kind == "price_law.emergency_checked":
            self.checks.append(EmergencyCheck(p["id"], p["place"], day("checked_on"), day("valid_until"), p["by"], p["source"], tuple(p["active"]), at))
            return "PLK", p["id"]
        elif kind == "price_law.baseline_recorded":
            self.baselines.append(Baseline(p["id"], p["restriction"], p["subject"], p["price_cents"], day("observed_on"), p["source"], p["review"], p["by"], at))
            return "PLB", p["id"]
        elif kind == "price_law.notice_served":
            self.notices.setdefault(p["change"], []).append(Notice(
                p["id"], p["change"], p["lease"], p["document_sha256"], p["recipient"], p["channel"], p["sent_to"], day("attempted_on"),
                p["dispatch_accepted"], p["certificate_of_mailing"], p["postage_prepaid"], p["confirmation"], day("delivered_on"),
                day("acknowledged_on"), p["by"], at))
            return "PLN", p["id"]
        elif kind == "price_law.notice_updated":
            notices = self.notices[p["change"]]
            i = next(i for i, n in enumerate(notices) if n.id == p["notice"])
            notices[i] = replace(notices[i], confirmation=p["confirmation"] or notices[i].confirmation,
                                 delivered_on=day("delivered_on") or notices[i].delivered_on,
                                 acknowledged_on=day("acknowledged_on") or notices[i].acknowledged_on)
        elif kind == "price_law.exception_authorized":
            self.authorizations.setdefault(p["change"], []).append(Authorization(
                p["id"], p["change"], p["basis"], p["covers_cents"], p["document_sha256"], p["by"], at))
            return "PLX", p["id"]
        elif kind == "price_law.approved":
            self.approvals[p["change"]] = Approval(p["change"], dict(p["versions"]), p["by"], at)
        elif kind == "price_law.decided":
            self.decisions.append(Recorded(
                p["id"], p["change"], p["point"], p["outcome"], tuple(Reason(**r) for r in p["reasons"]), tuple(p["records"]),
                dict(p["versions"]), p["lease"], p["invoice"], dict(p["snapshot"]), at))
            return "PLD", p["id"]
        return None


def state(books: Books) -> State:
    """The price-law view of the books; an empty State, never None, before any record exists."""
    return books.price_law or State()


# the facility -----------------------------------------------------------------------------------------------------------------
def profile_of(books: Books) -> FacilityProfile:
    """The facility's legal profile: the one staff recorded, else the one its city text gives (Northstar is Orlando, Florida)."""
    recorded = state(books).profile
    if recorded:
        return recorded
    city = books.facility.get("city", "")
    return FacilityProfile(books.facility.get("name", ""), city, place_of(city))


def set_profile(led: Ledger, at: datetime, *, place: str, reviewed_by: str) -> None:
    if not _clean(place) or not _clean(reviewed_by):
        raise Refused("give the place and who reviewed it")
    city = led.books.facility.get("city", "")
    led.emit("price_law.profile_set", at, {"facility": led.books.facility.get("name", ""), "city": city, "place": _clean(place),
                                           "reviewed_by": _clean(reviewed_by)}, f"price_law:profile:{_clean(place)}:{at.isoformat()}")


# the agreement ----------------------------------------------------------------------------------------------------------------
def terms_of(books: Books, lease: str) -> Terms | None:
    history = state(books).terms.get(lease)
    return history[-1] if history else None


def record_terms(led: Ledger, at: datetime, *, lease: str, signed_on, agreement_sha256: str, notice_methods, notice_days: int | None = None,
                 email_notice_authorized: bool = False, last_known_address: str = "", last_known_email: str = "", designated_method: str | None = None,
                 price_promise_until=None, price_promise_max_cents: int | None = None, rent_change_allowed: bool = True) -> int:
    """Take the terms from the signed agreement as a new version. The earlier versions stay, and a decision names the version it read."""
    methods = tuple(notice_methods)
    if not _clean(lease) or any(m not in CHANNELS for m in methods):
        raise Refused("name the lease and use only the ways of giving notice the system knows")
    if notice_days is not None and (not isinstance(notice_days, int) or isinstance(notice_days, bool) or notice_days < 0):
        raise Refused("the agreement's notice days must be a whole number")
    if designated_method is not None and designated_method not in CHANNELS:
        raise Refused("the designated way of giving notice is not one the system knows")
    if price_promise_max_cents is not None and (not isinstance(price_promise_max_cents, int) or price_promise_max_cents < 0):
        raise Refused("the promised maximum must be an amount in cents")
    signed, promise = _date(signed_on, "signing date"), _date(price_promise_until, "promise date") if price_promise_until else None
    if price_promise_max_cents is not None and promise is None:
        raise Refused("a promised maximum needs the date it lasts until")
    sha = _sha(agreement_sha256, "agreement")
    version = len(state(led.books).terms.get(lease, ())) + 1
    led.emit("price_law.terms_snapshotted", at, {
        "lease": lease, "version": version, "signed_on": signed.isoformat(), "agreement_sha256": sha, "notice_methods": list(methods),
        "notice_days": notice_days, "email_notice_authorized": bool(email_notice_authorized), "last_known_address": _clean(last_known_address),
        "last_known_email": _clean(last_known_email).lower(), "designated_method": designated_method,
        "price_promise_until": promise.isoformat() if promise else None, "price_promise_max_cents": price_promise_max_cents,
        "rent_change_allowed": bool(rent_change_allowed)}, f"price_law:terms:{lease}:{version}")
    return version


def record_commitment(led: Ledger, at: datetime, *, lease: str, kind: str, max_cents: int, covers_from, covers_to, disclosed_on,
                      evidence_sha256: str) -> str:
    if kind not in COMMITMENT_KINDS or not isinstance(max_cents, int) or isinstance(max_cents, bool) or max_cents <= 0:
        raise Refused("a disclosed maximum is first-year or anniversary and a positive amount")
    start, end, told = _date(covers_from, "start date"), _date(covers_to, "end date"), _date(disclosed_on, "disclosure date")
    if end < start:
        raise Refused("the covered period ends before it starts")
    sha = _sha(evidence_sha256, "disclosure")
    ident = led.books.next_id("PLC")
    led.emit("price_law.commitment_recorded", at, {"id": ident, "lease": lease, "kind": kind, "max_cents": max_cents, "covers_from": start.isoformat(),
                                                   "covers_to": end.isoformat(), "disclosed_on": told.isoformat(), "evidence_sha256": sha}, f"price_law:{ident}")
    return ident


# emergencies ------------------------------------------------------------------------------------------------------------------
def record_emergency(led: Ledger, at: datetime, *, place: str, name: str, source: str, starts, ends=None, products=("storage",), scope: str = "") -> str:
    begin = _date(starts, "start date")
    end = _date(ends, "end date") if ends is not None else None
    if not _clean(place) or not _clean(name) or not _clean(source) or not products or (end and end < begin):
        raise Refused("a declaration needs its place, name, source, products and a period that does not end before it starts")
    ident = led.books.next_id("PLE")
    led.emit("price_law.emergency_declared", at, {"id": ident, "place": _clean(place), "name": _clean(name), "source": _clean(source),
                                                  "starts": begin.isoformat(), "ends": end.isoformat() if end else None, "products": list(products),
                                                  "scope": _clean(scope)}, f"price_law:{ident}")
    return ident


def record_emergency_extension(led: Ledger, at: datetime, *, restriction: str, ends, source: str) -> None:
    held = state(led.books).emergencies.get(restriction)
    end = _date(ends, "end date")
    if held is None or not _clean(source) or (held.ends and end <= held.ends):
        raise Refused("an extension names a declaration on file, its source, and a date later than its current end")
    led.emit("price_law.emergency_extended", at, {"id": restriction, "ends": end.isoformat(), "source": _clean(source)},
             f"price_law:{restriction}:extended:{end.isoformat()}")


def record_emergency_check(led: Ledger, at: datetime, *, place: str, checked_on, valid_until, by: str, source: str, active) -> str:
    seen, until = _date(checked_on, "check date"), _date(valid_until, "expiry date")
    if until < seen or not _clean(source) or not by.startswith("staff:") and by != "owner":
        raise Refused("a check names who looked, where, and a date it stays good until that is not before the day it was made")
    if any(rid not in state(led.books).emergencies for rid in active):
        raise Refused("a check can only name declarations on file")
    ident = led.books.next_id("PLK")
    led.emit("price_law.emergency_checked", at, {"id": ident, "place": _clean(place), "checked_on": seen.isoformat(), "valid_until": until.isoformat(),
                                                 "by": by, "source": _clean(source), "active": list(active)}, f"price_law:{ident}")
    return ident


def record_baseline(led: Ledger, at: datetime, *, restriction: str, subject: str, price_cents: int, observed_on, source: str, review: str, by: str) -> str:
    if restriction not in state(led.books).emergencies or not _clean(subject) or not _clean(source) or review not in ("reviewed", "pending") \
            or not isinstance(price_cents, int) or isinstance(price_cents, bool) or price_cents <= 0:
        raise Refused("a baseline names a declaration on file, what it prices, an amount, its source and its review status")
    seen = _date(observed_on, "observation date")
    ident = led.books.next_id("PLB")
    led.emit("price_law.baseline_recorded", at, {"id": ident, "restriction": restriction, "subject": _clean(subject), "price_cents": price_cents,
                                                 "observed_on": seen.isoformat(), "source": _clean(source), "review": review, "by": by}, f"price_law:{ident}")
    return ident


# notices ----------------------------------------------------------------------------------------------------------------------
def record_notice(led: Ledger, at: datetime, *, change: str, lease: str, document_sha256: str, recipient: str, channel: str, sent_to: str,
                  attempted_on, dispatch_accepted: bool, by: str, certificate_of_mailing: bool = False, postage_prepaid: bool = False,
                  confirmation: str | None = None, delivered_on=None, acknowledged_on=None) -> str:
    if channel not in CHANNELS or not _clean(recipient) or not _clean(change):
        raise Refused("a notice names its change, its recipient and a way of giving it the system knows")
    tried = _date(attempted_on, "date it was sent")
    sha = _sha(document_sha256, "notice")
    out = [(_date(d, "date").isoformat() if d else None) for d in (delivered_on, acknowledged_on)]
    ident = led.books.next_id("PLN")
    led.emit("price_law.notice_served", at, {
        "id": ident, "change": change, "lease": lease, "document_sha256": sha, "recipient": _clean(recipient), "channel": channel, "sent_to": _clean(sent_to),
        "attempted_on": tried.isoformat(), "dispatch_accepted": bool(dispatch_accepted), "certificate_of_mailing": bool(certificate_of_mailing),
        "postage_prepaid": bool(postage_prepaid), "confirmation": confirmation, "delivered_on": out[0], "acknowledged_on": out[1], "by": by}, f"price_law:{ident}")
    return ident


def update_notice(led: Ledger, at: datetime, *, change: str, notice: str, confirmation: str | None = None, delivered_on=None, acknowledged_on=None) -> None:
    """A later stage of the same notice: a confirmation, the delivery, the tenant's acknowledgment. The notice record is never rewritten."""
    if not any(n.id == notice for n in state(led.books).notices.get(change, ())):
        raise Refused("that notice is not on file for this change")
    out = [(_date(d, "date").isoformat() if d else None) for d in (delivered_on, acknowledged_on)]
    led.emit("price_law.notice_updated", at, {"change": change, "notice": notice, "confirmation": confirmation, "delivered_on": out[0], "acknowledged_on": out[1]},
             f"price_law:{notice}:update:{at.isoformat()}")


def record_exception(led: Ledger, at: datetime, *, change: str, basis: str, covers_cents: int, document_sha256: str, by: str) -> str:
    if by != "owner":
        raise Refused("only the owner records a legal exception")
    if not _clean(basis) or not isinstance(covers_cents, int) or isinstance(covers_cents, bool) or covers_cents <= 0:
        raise Refused("an exception names its basis and the price it covers")
    sha = _sha(document_sha256, "exception")
    ident = led.books.next_id("PLX")
    led.emit("price_law.exception_authorized", at, {"id": ident, "change": change, "basis": _clean(basis), "covers_cents": covers_cents,
                                                    "document_sha256": sha, "by": by}, f"price_law:{ident}", source=by)
    return ident


# decisions --------------------------------------------------------------------------------------------------------------------
def record_approval(led: Ledger, at: datetime, *, change: str, versions: dict, by: str) -> None:
    led.emit("price_law.approved", at, {"change": change, "versions": dict(versions), "by": by}, f"price_law:approved:{change}:{at.isoformat()}", source=by)


def record_decision(led: Ledger, at: datetime, *, change: str, point: str, outcome: str, reasons, records, versions: dict, lease: str | None,
                    invoice: str | None, snapshot: dict | None = None) -> str:
    if point not in POINTS or outcome not in ("allow", "refuse", "hold"):
        raise Refused("a decision is recorded at one of the known points as allow, refuse or hold")
    ident = led.books.next_id("PLD")
    led.emit("price_law.decided", at, {"id": ident, "change": change, "point": point, "outcome": outcome, "reasons": [asdict(r) for r in reasons],
                                       "records": list(records), "versions": dict(versions), "lease": lease, "invoice": invoice,
                                       "snapshot": dict(snapshot or {})}, f"price_law:{ident}")
    return ident
