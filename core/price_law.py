"""The legal rules a price change must pass: notice length and how notice is served, disclosed maximums, and price limits during a
declared emergency, which differ by place.

Everything here is code, by design: no model reads or decides any of it. A rule set is an immutable, versioned record with its
citation, its dates, the place it covers and whether anyone has reviewed it. The records a decision rests on (the facility's legal
profile, the lease terms taken from the signed agreement, disclosed maximums, emergency restrictions and the staff check that they
were looked up, notice service evidence) are folded from their own `price_law.*` events into Books.price_law, like the lien and
lead records. A decision is recorded with the reasons it gave, the records it read and the rule versions in force, and a posted
invoice keeps the snapshot it was decided on. Like the lien workflow, anything the records cannot establish fails closed."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from .billing import Ledger, Refused
from .books import local
from .evidence import money
from .gate import Decision, STAFF_ROLES
from .store import Store

if TYPE_CHECKING:
    from .books import Books

# how a notice can reach a tenant; a text message and a dashboard banner are named so they can be refused in words
CHANNELS = ("hand", "first_class_mail", "email", "text", "dashboard")
CHANNEL_WORDS = {"hand": "hand delivery", "first_class_mail": "first-class mail", "email": "email", "text": "a text message",
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
    review: str                         # reviewed: counsel checked it; demonstration: modeled for tests and the
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


_DESIGN_REVIEW = "Staddle design review, 2026-11-23; counsel review pending"
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
    RuleSet("FL-notice", 1, "FL", "notice", "Florida ordinary rent increase: the signed agreement's own terms",
            "the signed rental agreement; no statewide notice period is modeled", LONG_IN_FORCE, None, "pending", _DESIGN_REVIEW, None,
            "advisor research, 2026-11-23: no statewide statutory notice duration established", agreement_specific=True, email_needs_agreement=True),
    RuleSet("FL-emergency", 1, "FL", "emergency", "Florida price gouging during a declared emergency", "Florida Statutes 501.160",
            LONG_IN_FORCE, None, "pending", _DESIGN_REVIEW, None, "advisor research, 2026-11-23", emergency_hold=True),
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
    match = re.search(r"^(.*?)[,\s]+([A-Z]{2})\s*$", (city or "").strip())
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


# made-up facilities for the tests and the evaluation only: the live facility is Northstar in Orlando, Florida, and a recorded profile is never synthetic
SYNTHETIC_PROFILES = {
    "CA": FacilityProfile("Synthetic California facility", "Sacramento, CA", "CA", True, "made up for tests and the evaluation"),
    "UT": FacilityProfile("Synthetic Utah facility", "Salt Lake City, UT", "UT", True, "made up for tests and the evaluation"),
    "NYC": FacilityProfile("Synthetic New York City facility", "New York, NY", "NYC", True, "made up for tests and the evaluation"),
}


def synthetic_profile(place: str) -> FacilityProfile:
    return SYNTHETIC_PROFILES[place]


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
    first_page: bool = False


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
    new_cents: int | None
    effective_on: date | None
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
    snapshot: dict
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
        self.postings: dict[str, tuple[str, str]] = {}
        self.order: dict[str, int] = {}                 # declaration and check ids -> their place in the log, so "after" never reads a clock
        self.counts: dict[str, int] = {}                # profiles set, approvals given and notice updates made, so a repeat in the same instant has its own key
        self._n = 0

    def apply(self, kind: str, at: datetime, p: dict) -> tuple[str, str] | None:
        """Fold one `price_law.*` event. Returns the (prefix, id) the books count, or None."""
        day = lambda key: date.fromisoformat(p[key]) if p.get(key) else None
        self._n += 1
        if kind in ("price_law.emergency_declared", "price_law.emergency_extended", "price_law.emergency_checked"):
            self.order[p["id"]] = self._n
        for counted, key in (("price_law.profile_set", "profile"), ("price_law.approved", p.get("change")), ("price_law.notice_updated", p.get("notice"))):
            if kind == counted:
                self.counts[key] = self.counts.get(key, 0) + 1
        if kind == "price_law.profile_set":
            self.profile = FacilityProfile(p["facility"], p["city"], p["place"], False, p["reviewed_by"])
        elif kind == "price_law.terms_snapshotted":
            self.terms.setdefault(p["lease"], []).append(Terms(
                p["lease"], p["version"], day("signed_on"), p["agreement_sha256"], tuple(p["notice_methods"]), p["notice_days"],
                p["email_notice_authorized"], p["last_known_address"], p["last_known_email"], p["designated_method"], day("price_promise_until"),
                p["price_promise_max_cents"], p["rent_change_allowed"], at))
        elif kind == "price_law.commitment_recorded":
            self.commitments.setdefault(p["lease"], []).append(Commitment(
                p["id"], p["lease"], p["kind"], p["max_cents"], day("covers_from"), day("covers_to"), day("disclosed_on"), p["evidence_sha256"], at, p.get("first_page", False)))
            return "PLC", p["id"]
        elif kind == "price_law.emergency_declared":
            self.emergencies[p["id"]] = Restriction(p["id"], p["place"], p["name"], p["source"], day("starts"), day("ends"),
                                                    tuple(p["products"]), p["scope"], at)
            return "PLE", p["id"]
        elif kind == "price_law.emergency_extended":
            self.emergencies[p["id"]] = replace(self.emergencies[p["id"]], ends=day("ends"), source=p["source"], at=at)
        elif kind == "price_law.emergency_checked":
            self.checks.append(EmergencyCheck(p["id"], p["place"], day("checked_on"), day("valid_until"), p["by"], p["source"], tuple(p["active"]), at))
            return "PLK", p["id"]
        elif kind == "price_law.baseline_recorded":
            self.baselines.append(Baseline(p["id"], p["restriction"], p["subject"], p["price_cents"], day("observed_on"), p["source"], p["review"], p["by"], at))
            return "PLB", p["id"]
        elif kind == "price_law.notice_served":
            self.notices.setdefault(p["change"], []).append(Notice(
                p["id"], p["change"], p["lease"], p["document_sha256"], p.get("new_cents"), day("effective_on"), p["recipient"], p["channel"], p["sent_to"], day("attempted_on"),
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
            self.approvals[p["change"]] = Approval(p["change"], dict(p["versions"]), dict(p.get("snapshot", {})), p["by"], at)
        elif kind == "price_law.invoice_posted":
            self.postings[p["invoice"]] = (p["decision"], p["charge"])
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
                                           "reviewed_by": _clean(reviewed_by)}, f"price_law:profile:{state(led.books).counts.get('profile', 0) + 1}")


# the agreement ----------------------------------------------------------------------------------------------------------------
def terms_of(books: Books, lease: str) -> Terms | None:
    history = state(books).terms.get(lease)
    return history[-1] if history else None


def record_terms(led: Ledger, at: datetime, *, lease: str, signed_on, agreement_sha256: str, notice_methods, notice_days: int | None = None,
                 email_notice_authorized: bool = False, last_known_address: str = "", last_known_email: str = "", designated_method: str | None = None,
                 price_promise_until=None, price_promise_max_cents: int | None = None, rent_change_allowed: bool = True) -> int:
    """Take the terms from the signed agreement as a new version. The earlier versions stay, and a decision names the version it read."""
    if not isinstance(notice_methods, (list, tuple)):
        raise Refused("notice methods must be a list")
    if not isinstance(email_notice_authorized, bool) or not isinstance(rent_change_allowed, bool):
        raise Refused("agreement permissions must be booleans")
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
        "notice_days": notice_days, "email_notice_authorized": email_notice_authorized, "last_known_address": _clean(last_known_address),
        "last_known_email": _clean(last_known_email).lower(), "designated_method": designated_method,
        "price_promise_until": promise.isoformat() if promise else None, "price_promise_max_cents": price_promise_max_cents,
        "rent_change_allowed": rent_change_allowed}, f"price_law:terms:{lease}:{version}")
    return version


def record_commitment(led: Ledger, at: datetime, *, lease: str, kind: str, max_cents: int, covers_from, covers_to, disclosed_on,
                      evidence_sha256: str, first_page: bool = False) -> str:
    if not isinstance(first_page, bool):
        raise Refused("first-page evidence must be a boolean")
    if kind not in COMMITMENT_KINDS or not isinstance(max_cents, int) or isinstance(max_cents, bool) or max_cents <= 0:
        raise Refused("a disclosed maximum is first-year or anniversary and a positive amount")
    start, end, told = _date(covers_from, "start date"), _date(covers_to, "end date"), _date(disclosed_on, "disclosure date")
    if end < start:
        raise Refused("the covered period ends before it starts")
    sha = _sha(evidence_sha256, "disclosure")
    ident = led.books.next_id("PLC")
    led.emit("price_law.commitment_recorded", at, {"id": ident, "lease": lease, "kind": kind, "max_cents": max_cents, "covers_from": start.isoformat(),
                                                   "covers_to": end.isoformat(), "disclosed_on": told.isoformat(), "evidence_sha256": sha, "first_page": first_page}, f"price_law:{ident}")
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
    if held is None or not _clean(source) or held.ends is None or end <= held.ends:
        raise Refused("an extension names a declaration on file, its source, and a date later than its current end")
    led.emit("price_law.emergency_extended", at, {"id": restriction, "ends": end.isoformat(), "source": _clean(source)},
             f"price_law:{restriction}:extended:{end.isoformat()}")


def record_emergency_check(led: Ledger, at: datetime, *, place: str, checked_on, valid_until, by: str, source: str, active) -> str:
    seen, until = _date(checked_on, "check date"), _date(valid_until, "expiry date")
    if seen > local(at).date() or until < seen or not _clean(source) or not by.startswith("staff:") and by != "owner":
        raise Refused("a check names who looked, where, a past or current check date, and an expiry no earlier than that date")
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
                  confirmation: str | None = None, delivered_on=None, acknowledged_on=None, new_cents=None, effective_on=None) -> str:
    if any(type(value) is not bool for value in (dispatch_accepted, certificate_of_mailing, postage_prepaid)):
        raise Refused("notice evidence must be true or false")
    if channel not in CHANNELS or not _clean(recipient) or not _clean(change):
        raise Refused("a notice names its change, its recipient and a way of giving it the system knows")
    if not isinstance(new_cents, int) or isinstance(new_cents, bool) or new_cents <= 0:
        raise Refused("enter the amount stated in the notice")
    effective = _date(effective_on, "effective date stated in the notice")
    tried = _date(attempted_on, "date it was sent")
    if delivered_on is not None and _date(delivered_on, "delivery date") < tried:
        raise Refused("delivery cannot precede the notice attempt")
    if any(d is not None and _date(d, "service date") > local(at).date() for d in (tried, delivered_on, acknowledged_on)):
        raise Refused("notice service dates cannot be in the future")
    sha = _sha(document_sha256, "notice")
    out = [(_date(d, "date").isoformat() if d else None) for d in (delivered_on, acknowledged_on)]
    ident = led.books.next_id("PLN")
    led.emit("price_law.notice_served", at, {
        "id": ident, "change": change, "lease": lease, "document_sha256": sha, "new_cents": new_cents, "effective_on": effective.isoformat(), "recipient": _clean(recipient), "channel": channel, "sent_to": _clean(sent_to),
        "attempted_on": tried.isoformat(), "dispatch_accepted": bool(dispatch_accepted), "certificate_of_mailing": bool(certificate_of_mailing),
        "postage_prepaid": bool(postage_prepaid), "confirmation": confirmation, "delivered_on": out[0], "acknowledged_on": out[1], "by": by}, f"price_law:{ident}")
    return ident


def update_notice(led: Ledger, at: datetime, *, change: str, notice: str, confirmation: str | None = None, delivered_on=None, acknowledged_on=None) -> None:
    """A later stage of the same notice: a confirmation, the delivery, the tenant's acknowledgment. The notice record is never rewritten."""
    existing = next((n for n in state(led.books).notices.get(change, ()) if n.id == notice), None)
    if existing is None:
        raise Refused("that notice is not on file for this change")
    if delivered_on is not None and _date(delivered_on, "delivery date") < existing.attempted_on:
        raise Refused("delivery cannot precede the notice attempt")
    if any(d is not None and _date(d, "service date") > local(at).date() for d in (delivered_on, acknowledged_on)):
        raise Refused("notice service dates cannot be in the future")
    out = [(_date(d, "date").isoformat() if d else None) for d in (delivered_on, acknowledged_on)]
    led.emit("price_law.notice_updated", at, {"change": change, "notice": notice, "confirmation": confirmation, "delivered_on": out[0], "acknowledged_on": out[1]},
             f"price_law:{notice}:update:{state(led.books).counts.get(notice, 0) + 1}")


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
def record_approval(led: Ledger, at: datetime, *, change: str, versions: dict, by: str, snapshot: dict) -> None:
    led.emit("price_law.approved", at, {"change": change, "versions": dict(versions), "snapshot": dict(snapshot), "by": by}, f"price_law:approved:{change}:{state(led.books).counts.get(change, 0) + 1}", source=by)


def record_decision(led: Ledger, at: datetime, *, change: str, point: str, outcome: str, reasons, records, versions: dict, lease: str | None,
                    invoice: str | None, snapshot: dict | None = None) -> str:
    if point not in POINTS or outcome not in ("allow", "refuse", "hold"):
        raise Refused("a decision is recorded at one of the known points as allow, refuse or hold")
    ident = led.books.next_id("PLD")
    led.emit("price_law.decided", at, {"id": ident, "change": change, "point": point, "outcome": outcome, "reasons": [asdict(r) for r in reasons],
                                       "records": list(records), "versions": dict(versions), "lease": lease, "invoice": invoice,
                                       "snapshot": dict(snapshot or {})}, f"price_law:{ident}")
    return ident


# the evaluator ---------------------------------------------------------------------------------------------------------------
KINDS = ("lease_increase", "street_rate", "fee_change")
OWNER_RULE = "price_change"
# what an owner may say about price changes: only limits that add to the law's, each inside the platform's bounds
OWNER_BOUNDS = {"notice_days": (0, 365), "max_increase_percent": (1, 100), "max_increase_cents": (1, 100000), "months_between": (0, 60)}


@dataclass(frozen=True)
class PriceChange:
    """A proposed change in what someone pays: a rent increase on one lease, a street rate, or a new or higher fee."""
    id: str
    kind: str
    current_cents: int
    new_cents: int
    effective_on: date
    lease: str | None = None
    unit_class: str = ""                # what a street rate or an emergency baseline prices
    invoice: str | None = None          # the invoice this change prices, "<lease>:<month>", once it is being billed
    disclosure: dict | None = None      # a street rate's first-page disclosure: max_cents, months, first_page, recurring_cents
    claimed_exception: str = ""         # a legal exception the person proposing it relies on; it counts only once the owner has recorded it
    last_increase_on: date | None = None
    other_recurring_cents: int | None = None  # required recurring charges other than the amount being changed

    @property
    def raises(self) -> bool:
        return self.new_cents > self.current_cents


@dataclass(frozen=True)
class Result:
    outcome: str                        # allow, refuse or hold
    reasons: tuple[Reason, ...]
    records: tuple[str, ...]
    versions: dict
    id: str | None = None               # the recorded decision, once `decide` wrote it

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"

    @property
    def words(self) -> str:
        return " ".join(r.words for r in self.reasons if r.kind != "note")


def owner_rule(books: Books) -> dict | None:
    return books.rules.get(OWNER_RULE)


def adopt_owner_rule(led: Ledger, at: datetime, *, version: int, sentence: str, typed: dict) -> None:
    """The owner's limits on price changes. The typed form can only add to the law (a longer notice, a smaller step, more months between
    increases); anything else it names is refused here, so no owner rule can reach a legal stop."""
    typed = dict(typed)
    if not _clean(sentence) or len(sentence) > 400 or not typed or version <= led.books.rules.get(OWNER_RULE, {}).get("version", 0):
        raise Refused("an owner rule needs its sentence, at least one limit, and a version after the one in force")
    for key, value in typed.items():
        low, high = OWNER_BOUNDS.get(key, (None, None))
        if low is None or not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise Refused(f"an owner rule can only add a longer notice, a smaller increase or more time between increases, each inside the platform's bounds ({key} is not)")
    led.emit("rule.adopted", at, {"rule": OWNER_RULE, "version": version, "sentence": _clean(sentence), "typed": {"action": OWNER_RULE, **typed}, "by": "owner"},
             f"rule:{OWNER_RULE}:{version}", source="owner")


def required_notice_days(books: Books, sets, terms: Terms | None = None) -> int | None:
    """The notice a change needs: the law's days (the agreement's own where the rule defers to it) or the owner's, whichever is longer."""
    days = [terms.notice_days if rs.agreement_specific and terms else rs.notice_days for rs in sets if rs.topic == "notice"]
    owner = ((owner_rule(books) or {}).get("typed") or {}).get("notice_days")
    known = [d for d in days + [owner] if d is not None]
    return max(known) if known else None


def _add_months(day: date, months: int) -> date:
    y, m = divmod(day.year * 12 + day.month - 1 + months, 12)
    first_next = date(y + (m + 1) // 12, (m + 1) % 12 + 1, 1)
    return date(y, m + 1, min(day.day, (first_next - date(y, m + 1, 1)).days))


def _params(rs: RuleSet) -> tuple:
    body = asdict(rs)
    for key in ("id", "version", "name", "citation", "effective_from", "effective_to", "review", "reviewer", "reviewed_on", "source"):
        body.pop(key)
    return tuple(sorted(body.items()))


def day_words(d: date) -> str:
    return f"{d:%b} {d.day}"


class _Run:
    """The reasons, records and versions one evaluation collects."""

    def __init__(self, books: Books, change: PriceChange, today: date, point: str, sets, profile: FacilityProfile):
        self.books, self.change, self.today, self.point, self.sets, self.profile = books, change, today, point, sets, profile
        self.reasons: list[Reason] = []
        self.records: list[str] = []
        self.versions: dict = {}

    def add(self, kind: str, code: str, words: str, rs: RuleSet | None = None) -> None:
        cite = rs.id if rs else "owner rule" if kind in ("owner", "note") else "price law"
        self.reasons.append(Reason(code, kind, words, cite, rs.version if rs else None))

    def refuse(self, code, words, rs=None):
        self.add("refuse", code, words, rs)

    def hold(self, code, words, rs=None):
        self.add("hold", code, words, rs)

    def result(self) -> Result:
        kinds = {r.kind for r in self.reasons}
        outcome = "refuse" if kinds & {"refuse", "owner"} else "hold" if "hold" in kinds else "allow"
        return Result(outcome, tuple(self.reasons), tuple(self.records), dict(self.versions))


def _covers(r: Restriction, on: date) -> bool:
    return r.starts <= on and (r.ends is None or on <= r.ends)


def _emergency(run: _Run) -> None:
    """Verified emergency status, never a flag that defaults to none. An absent or expired check holds an increase; a declared
    emergency is judged by the place's own rule, and a place with no rule for it holds."""
    change, st = run.change, state(run.books)
    place = run.profile.place
    checks = [c for c in st.checks if c.place == place]
    chk = max(checks, key=lambda c: st.order[c.id]) if checks else None
    run.versions["emergency"] = ""
    if chk is None:
        if change.raises:
            run.hold("EMERGENCY_STATUS_UNKNOWN", f"No one has checked whether a declared emergency covers {place}, so a price increase waits until staff do.")
        return
    run.records.append(f"check:{chk.id}")
    if run.today > chk.valid_until:
        if change.raises:
            run.hold("EMERGENCY_STATUS_UNKNOWN", f"The emergency check made {day_words(chk.checked_on)} expired on {day_words(chk.valid_until)}, so the status of "
                                                 f"declarations for {place} is unknown again. Staff check it again and the increase can go ahead.")
        return
    run.versions["emergency"] = ",".join(sorted(chk.active))
    if not change.raises:
        return
    late = [r for r in st.emergencies.values() if r.place == place and _covers(r, change.effective_on) and
            (local(r.at).date() > chk.checked_on or
             (local(r.at).date() == chk.checked_on and
              (local(chk.at).date() != chk.checked_on or st.order[r.id] > st.order[chk.id])))]
    if late:
        run.hold("EMERGENCY_STATUS_UNKNOWN", f"A declaration or extension ({late[0].name}) was recorded after the last check, so whether it covers this facility is not known.")
        return
    rule = next((rs for rs in run.sets if rs.topic == "emergency"), None)
    for r in (st.emergencies[i] for i in chk.active if _covers(st.emergencies[i], change.effective_on)):
        run.records.append(f"emergency:{r.id}")
        if auth := _authorized(run):
            run.add("note", "EMERGENCY_EXCEPTION_RECORDED", f"The owner recorded a documented legal exception for this price ({auth.basis}); it lifts the emergency "
                                                          "restriction for this change only.")
            continue
        if rule is None:
            run.hold("EMERGENCY_PRICE_RESTRICTED", f"A declared emergency ({r.name}) covers {place}, and no rule for it is modeled here, so a price increase waits for documented legal review.")
        elif rule.emergency_hold:
            run.hold("EMERGENCY_PRICE_RESTRICTED", f"A declared emergency ({r.name}) covers this facility. Florida's price-gouging law ({rule.citation}) sets no percentage; "
                                                  "it asks whether a price is unconscionable. The increase waits for documented legal review of the declaration and this price.", rule)
        else:
            _cap(run, rule, r)


def _cap(run: _Run, rule: RuleSet, r: Restriction) -> None:
    change, st = run.change, state(run.books)
    subjects = {s for s in (change.lease, change.unit_class) if s}
    base = [b for b in st.baselines if b.restriction == r.id and b.subject in subjects]
    limit = f"{rule.emergency_cap_percent} percent limit"
    if not base:
        run.hold("PRICE_BASELINE_UNPROVEN", f"The price before {r.name} began is not on file for this {'unit' if change.unit_class else 'lease'}, so the {limit} cannot be applied.", rule)
        return
    early = [b for b in base if b.observed_on < r.starts]
    if not early:
        run.hold("PRICE_BASELINE_UNPROVEN", "The only price on file was seen after the declaration began, so it is not a price from before it.", rule)
        return
    proven = [b for b in early if b.review == "reviewed"]
    if not proven:
        run.hold("PRICE_BASELINE_UNPROVEN", "The price from before the declaration has not been reviewed yet, so the " + limit + " cannot be applied.", rule)
        return
    top = max(proven, key=lambda b: (b.observed_on, b.id))
    run.records.append(f"baseline:{top.id}")
    if change.new_cents * 100 > top.price_cents * (100 + rule.emergency_cap_percent):
        most = top.price_cents * (100 + rule.emergency_cap_percent) // 100
        run.refuse("EMERGENCY_PRICE_RESTRICTED", f"California limits a storage price to {rule.emergency_cap_percent} percent above the price before the declaration "
                                                 f"({rule.citation}). The price before was {money(top.price_cents)}, so the most allowed is {money(most)}, and this change "
                                                 f"is {money(change.new_cents)}.", rule)


def evaluate(books: Books, change: PriceChange, at: datetime, *, point: str = "proposal", profile: FacilityProfile | None = None,
             rules: tuple[RuleSet, ...] | None = None) -> Result:
    """Would this price change pass the law now? Reads the books, writes nothing. Run at every point of the change's life: the same
    rules over the records as they stand then."""
    if point not in POINTS or change.kind not in KINDS:
        raise ValueError("unknown point or kind of change")
    today = local(at).date()
    profile = profile or profile_of(books)
    sets = rules_in_force(profile.place, today, rules) if profile.place else []
    run = _Run(books, change, today, point, sets, profile)
    run.records.append(f"profile:{profile.place or 'unknown'}")
    if point == "invoice" and not _valid_invoice(change.invoice, change.lease):
        run.refuse("INVOICE_INVALID", "The invoice must name its lease and month as <lease>:<YYYY-MM>.")
        return run.result()
    if point == "invoice" and change.kind == "lease_increase" and (lease := books.leases.get(change.lease)) and change.current_cents != lease.rent_cents:
        run.refuse("CURRENT_RENT_MISMATCH", "The current rent does not match the lease's recorded rent.")
        return run.result()
    needs_notice = change.kind != "street_rate"
    if not sets or (needs_notice and not any(rs.topic == "notice" for rs in sets)):
        what = ("the facility's place is not known" if not profile.place else
                f"no notice rule is modeled for {profile.place}" if sets else f"no rule is modeled for {profile.place}")
        run.refuse("JURISDICTION_UNSUPPORTED", f"No approved rule covers this price change: {what}, so it cannot take effect.")
        return run.result()
    run.versions.update({rs.id: rs.version for rs in sets})
    for rs in sets:
        if rs.review == "pending" or (rs.review == "demonstration" and not profile.synthetic):
            run.hold("RULE_NOT_REVIEWED", f"{rs.name} ({rs.citation}) is {'not reviewed yet' if rs.review == 'pending' else 'modeled for demonstration only'}; "
                                          "counsel review is needed before it can allow a change.", rs)
    for topic in sorted({rs.topic for rs in sets}):
        group = [rs for rs in sets if rs.topic == topic]
        if len({_params(rs) for rs in group}) > 1:
            run.hold("LEGAL_RULES_CONFLICT", f"{' and '.join(rs.citation for rs in group)} both cover this {topic} and disagree, so the change waits for review.", group[0])
    terms = None
    if needs_notice:
        terms = terms_of(books, change.lease or "")
        if terms is None:
            run.hold("LEASE_TERMS_MISSING", "The terms of the signed agreement have not been taken for this lease, so what it allows is not known.")
        else:
            run.records.append(f"terms:{terms.lease}#{terms.version}")
            run.versions[f"terms:{terms.lease}"] = terms.version
    _emergency(run)
    _checks(run, terms)
    _stale(run)
    _owner(run)
    return run.result()


PLACE_NAMES = {"CA": "California", "UT": "Utah", "NYC": "New York City", "FL": "Florida"}
PRE_SERVICE = ("proposal", "approval", "publication", "notice_authorization", "quote_acceptance")


def _list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def _ways(methods) -> str:
    return _list([CHANNEL_WORDS[m] for m in methods])


def _plain(text: str) -> str:
    return " ".join(str(text).lower().replace(",", " ").replace(".", " ").split())


def _authorized(run: _Run) -> Authorization | None:
    """The owner's recorded exception for this change, covering at least this price. Nothing else lifts an emergency restriction."""
    return next((a for a in state(run.books).authorizations.get(run.change.id, ()) if a.by == "owner" and a.covers_cents >= run.change.new_cents), None)


def _checks(run: _Run, terms: Terms | None) -> None:
    """The rules about notice, disclosure, contract promises and claimed exceptions."""
    c = run.change
    if c.claimed_exception and not _authorized(run):
        run.refuse("LEGAL_EXCEPTION_UNVERIFIED", f"The change relies on a legal exception (“{c.claimed_exception}”), but the owner has not recorded one with its document "
                                                 "for this price, so it does not count.")
    if c.kind == "street_rate":
        _street_disclosure(run)
    elif terms is not None and c.raises:
        if c.kind == "lease_increase":
            _promise(run, terms)
        if c.kind == "lease_increase" or any(r.recurring_fee for r in run.sets if r.topic == "disclosure"):
            _disclosed(run, terms)
        _notice(run, terms)


def _promise(run: _Run, terms: Terms) -> None:
    c = run.change
    if not terms.rent_change_allowed:
        run.refuse("CONTRACTUAL_PRICE_PROMISE", "The signed agreement does not let rent change during the term.")
    if terms.price_promise_until and c.effective_on <= terms.price_promise_until:
        cap = terms.price_promise_max_cents if terms.price_promise_max_cents is not None else c.current_cents
        if c.new_cents > cap:
            promised = "no increase" if terms.price_promise_max_cents is None else f"a price no higher than {money(cap)}"
            run.refuse("CONTRACTUAL_PRICE_PROMISE", f"The agreement promises {promised} through {day_words(terms.price_promise_until)}; this change takes effect {day_words(c.effective_on)}.")


def _disclosed(run: _Run, terms: Terms) -> None:
    """A maximum the facility stated binds it where it stated it; where a rule requires the statement, its absence stops an increase."""
    c, st = run.change, state(run.books)
    first_end = _add_months(terms.signed_on, 12) - timedelta(days=1)
    in_first = terms.signed_on <= c.effective_on <= first_end
    covering = [m for m in st.commitments.get(c.lease or "", ()) if m.covers_from <= c.effective_on <= m.covers_to]
    run.records.extend(f"commitment:{m.id}" for m in covering)
    for rs in (r for r in run.sets if r.topic == "disclosure"):
        name = PLACE_NAMES.get(rs.place, rs.place)
        if rs.first_year_max and terms.signed_on >= rs.effective_from and in_first and not any(
                m.kind == "first_year" and m.covers_from <= terms.signed_on and m.covers_to >= first_end
                and (not rs.first_page or m.first_page) for m in covering):
            run.refuse("DISCLOSURE_MISSING_OR_INVALID", f"The agreement was signed {day_words(terms.signed_on)}, after {rs.citation} began, so it had to state a "
                                                        "first-year maximum" + (" on the first page" if rs.first_page else "") + " and none is on file.", rs)
        if rs.annual_max_notice_days and c.effective_on > first_end:
            years = c.effective_on.year - terms.signed_on.year
            anniversary = _add_months(terms.signed_on, years * 12)
            if anniversary > c.effective_on:
                years -= 1
                anniversary = _add_months(terms.signed_on, years * 12)
            period_end = _add_months(terms.signed_on, (years + 1) * 12) - timedelta(days=1)
            yearly = [m for m in covering if m.kind == "anniversary" and m.covers_from <= anniversary and m.covers_to >= period_end]
            timely = [m for m in yearly if m.disclosed_on <= anniversary - timedelta(days=rs.annual_max_notice_days)]
            if not yearly:
                run.refuse("ANNUAL_MAX_NOTICE_MISSING", f"{name} requires a yearly maximum-fee notice at least {rs.annual_max_notice_days} days before each anniversary; "
                                                        f"none covers {day_words(c.effective_on)}.", rs)
            elif not timely:
                last = max(yearly, key=lambda m: m.id)
                run.refuse("ANNUAL_MAX_NOTICE_MISSING", f"{name} requires a yearly maximum-fee notice at least {rs.annual_max_notice_days} days before each anniversary; "
                                                        f"the notice for the year starting {day_words(anniversary)} was given {day_words(last.disclosed_on)}.", rs)
    recurring = any(r.recurring_fee for r in run.sets if r.topic == "disclosure")
    other = c.other_recurring_cents
    if recurring and (not isinstance(other, int) or isinstance(other, bool) or other < 0):
        run.hold("RECURRING_TOTAL_UNKNOWN", "Enter the other required recurring charges to check the complete occupancy fee.")
        return
    total = c.new_cents + other if recurring else c.new_cents
    for kind, words in (("first_year", "first-year"), ("anniversary", "yearly")):
        binding = [m for m in covering if m.kind == kind]
        if binding:
            m = min(binding, key=lambda m: m.max_cents)
            if total > m.max_cents:
                run.refuse("DISCLOSED_MAX_EXCEEDED", f"The {words} maximum disclosed for this lease is {money(m.max_cents)} through {day_words(m.covers_to)}; this change would charge {money(total)}.")


def _street_disclosure(run: _Run) -> None:
    c = run.change
    rules = [rs for rs in run.sets if rs.topic == "disclosure"]
    if not rules:
        return
    rs = rules[0]
    name = PLACE_NAMES.get(rs.place, rs.place)
    d = c.disclosure
    if not isinstance(d, dict):
        run.refuse("DISCLOSURE_MISSING_OR_INVALID", f"{name} requires this rate's disclosure ({rs.citation}) and none is attached.", rs)
        return
    top, missing = d.get("max_cents"), []
    if not isinstance(top, int) or isinstance(top, bool) or top <= 0:
        missing.append("a maximum rental fee")
    for r in rules:
        if r.first_year_max and not (isinstance(d.get("months"), int) and d["months"] >= 12):
            missing.append("that maximum for the first 12 months")
        if r.first_page and d.get("first_page") is not True:
            missing.append("the disclosure shown prominently on the first page")
        if r.recurring_fee and not (isinstance(d.get("recurring_cents"), int) and d["recurring_cents"] >= c.new_cents):
            missing.append("the complete recurring fee, including required recurring charges")
    if missing:
        run.refuse("DISCLOSURE_MISSING_OR_INVALID", f"{name}'s disclosure ({rs.citation}) is incomplete; it needs " + ", ".join(dict.fromkeys(missing)) + ".", rs)
    elif c.new_cents > top:
        run.refuse("DISCLOSED_MAX_EXCEEDED", f"The disclosure states a maximum of {money(top)}; this rate is {money(c.new_cents)}.", rs)
    elif any(r.recurring_fee for r in rules) and d["recurring_cents"] > top:
        run.refuse("DISCLOSED_MAX_EXCEEDED", f"The disclosure states a maximum of {money(top)}; the recurring fee is {money(d['recurring_cents'])}.", rs)


def _notice(run: _Run, terms: Terms) -> None:
    c, st = run.change, state(run.books)
    rule = next((rs for rs in run.sets if rs.topic == "notice"), None)
    if rule is None:
        return
    name = PLACE_NAMES.get(rule.place, rule.place)
    src = "The signed agreement" if rule.agreement_specific else name
    law = terms.notice_days if rule.agreement_specific else rule.notice_days
    if law is None:
        run.hold("LEASE_TERMS_MISSING", f"{rule.citation} leaves the notice period to the agreement, and the agreement states none, so the increase waits for review.", rule)
        return
    owner = ((owner_rule(run.books) or {}).get("typed") or {}).get("notice_days")
    notices = [n for n in st.notices.get(c.id, ()) if n.lease == c.lease and n.new_cents == c.new_cents and n.effective_on == c.effective_on]
    served_for_reapproval = run.point == "approval" and any(
        when is not None and when <= run.today
        for when, _ in (_served(rule, terms, n, law) for n in notices))
    if run.point in PRE_SERVICE and not served_for_reapproval:
        for days, who in ((law, None), (owner or 0, "owner")):
            earliest = run.today + timedelta(days=days)
            if c.effective_on < earliest:
                if who is None:
                    run.refuse("NOTICE_PERIOD_TOO_SHORT", f"{src} requires {law} days' written notice ({rule.citation}). If notice is given today the earliest this can take "
                                                          f"effect is {day_words(earliest)}; this change takes effect {day_words(c.effective_on)}.", rule)
                else:
                    run.add("owner", "OWNER_RULE", f"The owner's rule asks for {owner} days' notice; if notice is given today the earliest is {day_words(earliest)}.")
                break
        return
    if not notices:
        if c.kind == "fee_change":
            run.refuse("FEE_CHANGE_NOTICE_MISSING", "A new or higher fee needs its own notice, and none is on file for it.", rule)
        else:
            run.refuse("NOTICE_SERVICE_UNPROVEN", f"No notice of this increase is on file, so {name}'s {law} days' notice has not been shown.", rule)
        return
    served, problems = [], []
    for n in notices:
        run.records.append(f"notice:{n.id}")
        when, problem = _served(rule, terms, n, law)
        if when and when > run.today:
            when, problem = None, ("refuse", "NOTICE_SERVICE_UNPROVEN", "Notice service has not occurred by the check date.")
        (served.append(when) if when else problems.append(problem))
    if not served:
        kind, code, words = problems[0]
        run.add(kind, code, words, rule)
        return
    first = min(served)
    for days, who in ((law, None), (owner or 0, "owner")):
        earliest = first + timedelta(days=days)
        if c.effective_on < earliest:
            if who is None:
                run.refuse("NOTICE_PERIOD_TOO_SHORT", f"{src} requires {law} days' written notice. Notice counted from {day_words(first)}, so the earliest this can take "
                                                      f"effect is {day_words(earliest)}; this change takes effect {day_words(c.effective_on)}.", rule)
            else:
                run.add("owner", "OWNER_RULE", f"The owner's rule asks for {owner} days' notice; counted from {day_words(first)} the earliest is {day_words(earliest)}.")
            return
    if run.point in ("activation", "invoice") and run.today < c.effective_on:
        run.refuse("NOTICE_PERIOD_TOO_SHORT", f"This change takes effect {day_words(c.effective_on)}; it cannot be activated or billed before then.", rule)


def _served(rule: RuleSet, terms: Terms, n: Notice, days: int) -> tuple[date | None, tuple]:
    """When a notice legally counts as served under this rule, or why it does not, as (kind, code, words). Attempted, accepted for
    delivery, served, delivered and acknowledged are different things: only what the rule names counts."""
    name = "The signed agreement" if rule.agreement_specific else PLACE_NAMES.get(rule.place, rule.place)
    methods = terms.notice_methods if rule.agreement_specific else rule.methods
    way = CHANNEL_WORDS[n.channel]
    if not methods:
        return None, ("hold", "LEASE_TERMS_MISSING", f"{rule.citation} leaves the ways of giving notice to the agreement, and the agreement lists none.")
    if n.channel not in methods or n.channel in ("text", "dashboard"):
        return None, ("refuse", "NOTICE_METHOD_NOT_PERMITTED", f"{name} requires {days} days' written notice by {_ways(methods)}; {way} does not count.")
    if terms.notice_methods and n.channel not in terms.notice_methods:
        return None, ("refuse", "NOTICE_METHOD_NOT_PERMITTED", f"The agreement allows notice only by {_ways(terms.notice_methods)}; {way} is not one of them.")
    if rule.designated_method:
        if not terms.designated_method:
            return None, ("hold", "LEASE_TERMS_MISSING", "The way the tenant asked to be notified is not on file, and the rule sends notice the way the tenant asked.")
        if n.channel != terms.designated_method:
            return None, ("refuse", "NOTICE_METHOD_NOT_PERMITTED", f"The tenant asked for notice by {CHANNEL_WORDS[terms.designated_method]}; {way} is not that.")
    if n.channel == "email":
        if rule.email_needs_agreement and not terms.email_notice_authorized:
            return None, ("refuse", "NOTICE_METHOD_NOT_PERMITTED", f"The agreement does not authorize notice by email, which {rule.citation} requires.")
        if not terms.last_known_email or n.sent_to.lower() != terms.last_known_email:
            return None, ("refuse", "NOTICE_SERVICE_UNPROVEN", f"The email went to {n.sent_to}, which is not the last known address on file ({terms.last_known_email or 'none'}).")
        if not n.dispatch_accepted:
            return None, ("refuse", "NOTICE_SERVICE_UNPROVEN", "The email was never accepted for delivery by the mail service, so it was not sent.")
        return n.attempted_on, ()
    if n.channel == "first_class_mail":
        lacks = [w for ok, w in ((n.certificate_of_mailing, "a certificate of mailing"), (n.postage_prepaid, "prepaid postage")) if not ok]
        if lacks:
            return None, ("refuse", "NOTICE_SERVICE_UNPROVEN", "The mailing has no " + " and no ".join(lacks) + ", so first-class mailing is not proven.")
        if not terms.last_known_address or _plain(n.sent_to) != _plain(terms.last_known_address):
            return None, ("refuse", "NOTICE_SERVICE_UNPROVEN", f"The mailing went to {n.sent_to}, but the last known address on file is {terms.last_known_address or 'none'}.")
        return n.attempted_on, ()
    if not n.delivered_on:
        return None, ("refuse", "NOTICE_SERVICE_UNPROVEN", "The hand delivery has not been recorded as delivered.")
    if n.delivered_on < n.attempted_on:
        return None, ("refuse", "NOTICE_SERVICE_UNPROVEN", "The delivery date precedes the notice attempt.")
    return n.delivered_on, ()


def _stale(run: _Run) -> None:
    approval = state(run.books).approvals.get(run.change.id)
    if approval is None or run.point in ("proposal", "approval") or (approval.versions == run.versions and approval.snapshot == _revision(run.change)):
        return
    differs = []
    if approval.snapshot != _revision(run.change):
        differs.append("the change terms changed")
    for key in sorted(set(approval.versions) | set(run.versions)):
        old, new = approval.versions.get(key), run.versions.get(key)
        if old == new:
            continue
        if key == "emergency":
            differs.append("the declarations on file for this place changed")
        elif key.startswith("terms:"):
            differs.append("the lease terms changed")
        else:
            differs.append(f"{key} went from version {old} to version {new}")
    run.refuse("LEGAL_RULE_VERSION_STALE", "The approval no longer holds: " + "; ".join(differs) + ". The change has to be checked and approved again.")


def _owner(run: _Run) -> None:
    rule = owner_rule(run.books)
    if not rule or run.change.new_cents <= run.change.current_cents:
        return
    t, sentence, c = rule["typed"], rule["sentence"], run.change
    run.records.append(f"owner rule v{rule['version']}")
    if (p := t.get("max_increase_percent")) and c.new_cents * 100 > c.current_cents * (100 + p):
        increase = (f"{(c.new_cents - c.current_cents) * 100 / c.current_cents:.1f} percent"
                    if c.current_cents else "a new fee from zero")
        run.add("owner", "OWNER_RULE", f"The owner's rule (“{sentence}”) allows at most {p} percent in one step; this change is {increase}.")
    if (m := t.get("max_increase_cents")) and c.new_cents - c.current_cents > m:
        run.add("owner", "OWNER_RULE", f"The owner's rule (“{sentence}”) allows at most {money(m)} in one step; this change is {money(c.new_cents - c.current_cents)}.")
    if (n := t.get("months_between")) and c.last_increase_on and c.effective_on < _add_months(c.last_increase_on, n):
        run.add("owner", "OWNER_RULE", f"The owner's rule (“{sentence}”) asks for {n} months between increases; the last one took effect {day_words(c.last_increase_on)}.")
    law = [rs.notice_days for rs in run.sets if rs.topic == "notice" and rs.notice_days is not None]
    if (d := t.get("notice_days")) is not None and law and d < max(law):
        run.add("note", "OWNER_RULE_LOOSER", f"The owner's rule asks for {d} days' notice; the law's {max(law)} days applies instead, because an owner rule can only add to the law.")


def _valid_invoice(invoice: str | None, lease: str | None) -> bool:
    if not lease or not isinstance(invoice, str):
        return False
    match = re.fullmatch(re.escape(lease) + r":([0-9]{4})-([0-9]{2})", invoice)
    return bool(match and int(match[1]) >= 1 and 1 <= int(match[2]) <= 12)


def posted_basis(books: Books, invoice: str) -> Recorded | None:
    """Return the decision bound to an actual posted charge, if any."""
    posting = state(books).postings.get(invoice)
    if not posting:
        return None
    decision_id, charge_id = posting
    charge = books.charges.get(charge_id)
    basis = next((r for r in state(books).decisions if r.id == decision_id), None)
    if not charge or not basis or basis.point != "invoice" or basis.outcome != "allow" or basis.invoice != invoice:
        return None
    if not _valid_invoice(invoice, basis.lease) or charge.lease != basis.lease or charge.cents != basis.snapshot.get("new_cents"):
        return None
    return deepcopy(basis)


def post_invoice(led: Ledger, at: datetime, *, decision: str, profile: FacilityProfile | None = None) -> str:
    """Post the amount of an allowed invoice decision and bind the charge to it atomically."""
    basis = next((r for r in state(led.books).decisions if r.id == decision), None)
    if not basis or basis.point != "invoice" or basis.outcome != "allow" or not _valid_invoice(basis.invoice, basis.lease):
        raise Refused("an allowed invoice decision with a valid lease and month is required")
    snap = basis.snapshot
    if (snap.get("id") != basis.change or snap.get("lease") != basis.lease or snap.get("invoice") != basis.invoice
            or snap.get("kind") != "lease_increase" or not isinstance(snap.get("new_cents"), int)
            or isinstance(snap.get("new_cents"), bool) or snap["new_cents"] <= 0 or basis.lease not in led.books.leases
            or basis.invoice in state(led.books).postings):
        raise Refused("the invoice decision cannot be posted")
    due = _when(snap.get("effective_on"), "effective date")
    if local(at).date() < due:
        raise Refused("the change is not effective yet")
    change = _change_from(basis.change, snap)
    current = evaluate(led.books, change, at, point="invoice", profile=profile)
    if not current.allowed:
        raise Refused("the invoice no longer passes the price-change check: " + current.words)
    decision = record_decision(led, at, change=change.id, point="invoice", outcome=current.outcome, reasons=current.reasons,
                               records=current.records, versions=current.versions, lease=change.lease, invoice=change.invoice, snapshot=_snapshot(change))
    charge = led._charge(at, basis.lease, "rent", snap["new_cents"], "rent:" + basis.invoice, due)
    led.emit("price_law.invoice_posted", at, {"invoice": basis.invoice, "decision": decision, "charge": charge},
             f"price_law:posting:{basis.invoice}")
    return charge


def _revision(change: PriceChange) -> dict:
    snapshot = _snapshot(change)
    snapshot.pop("invoice")
    return snapshot


def _snapshot(change: PriceChange) -> dict:
    return json.loads(json.dumps(asdict(change), default=str))


def decide(led: Ledger, at: datetime, change: PriceChange, *, point: str, profile: FacilityProfile | None = None,
           rules: tuple[RuleSet, ...] | None = None) -> Result:
    """Evaluate, then record the decision with its reasons, the records it read and the rule versions: one write, after the answer."""
    if (point == "invoice" and _valid_invoice(change.invoice, change.lease)
            and (basis := posted_basis(led.books, change.invoice)) and basis.change == change.id
            and basis.lease == change.lease and basis.invoice == change.invoice and basis.snapshot == _snapshot(change)):
        return Result(basis.outcome, basis.reasons, basis.records, basis.versions, basis.id)       # already posted: its decision stands
    res = evaluate(led.books, change, at, point=point, profile=profile, rules=rules)
    ident = record_decision(led, at, change=change.id, point=point, outcome=res.outcome, reasons=res.reasons, records=res.records, versions=res.versions,
                            lease=change.lease, invoice=change.invoice, snapshot=_snapshot(change))
    return replace(res, id=ident)


# staff commands ---------------------------------------------------------------------------------------------------------------
ACTIONS = ("record_terms", "record_commitment", "record_notice", "update_notice", "record_emergency", "extend_emergency", "record_emergency_check",
           "record_baseline", "record_exception", "check_price_change", "approve_price_change")
OWNER_ONLY = ("record_exception",)


@dataclass
class Act:
    id: str
    action: str
    source: str
    about: str | None = None            # the price change a check or approval is about
    form: dict | None = None


def _when(value, label: str) -> date:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else _date(value, label)
    except ValueError:
        raise Refused(f"enter a valid {label}") from None


def _maybe(value, label: str) -> date | None:
    return _when(value, label) if value else None


def _change_from(about: str | None, f: dict) -> PriceChange:
    """A price change from a command's form. Every field is checked here, before anything is evaluated or written."""
    cents = [f.get("current_cents"), f.get("new_cents")]
    if (not about or f.get("kind") not in KINDS
            or any(not isinstance(v, int) or isinstance(v, bool) for v in cents)
            or cents[1] <= 0 or cents[0] < 0 or (cents[0] == 0 and f.get("kind") != "fee_change")):
        raise Refused("name the change, its kind and its current and new amounts")
    return PriceChange(about, f["kind"], cents[0], cents[1], _when(f.get("effective_on"), "effective date"), f.get("lease") or None,
                       str(f.get("unit_class", "")), f.get("invoice") or None, f.get("disclosure"), str(f.get("claimed_exception", "")),
                       _maybe(f.get("last_increase_on"), "date of the last increase"), f.get("other_recurring_cents"))


def _stated(res: Result) -> tuple[str, str | None]:
    """The words and named reason a decision gives: a hold says it is held for review, and no approval clears it."""
    if res.allowed:
        notes = " ".join(r.words for r in res.reasons if r.kind == "note")
        return ("The price-change rules allow it. " + notes).strip(), None
    first = next(r for r in res.reasons if r.kind != "note")
    return ("Held for review: " if res.outcome == "hold" else "") + res.words, first.code


def command(led: Ledger, act: Act, now: datetime, profile: FacilityProfile | None = None) -> Decision:
    """One staff command, through the one gate: the person, the action and every field are checked, the law is evaluated, and only then
    is anything written, so a refusal never follows a write. A command id repeated returns its first decision."""
    books = led.books
    if act.id in books.decisions:
        p = books.decisions[act.id]
        return Decision(p["allowed"], p["action"], p["check"], command=act.id, repeated=True, made=p.get("made"), why=p.get("why"))
    f = act.form or {}
    ok, why, check, made = False, "refused", "That action is not available.", None
    try:
        if not (act.source == "owner" or (act.source.startswith("staff:") and act.source[6:] in STAFF_ROLES)):
            raise Refused("Only staff or the owner may do that.")
        if act.action not in ACTIONS:
            raise Refused("That action is not available.")
        if act.action in OWNER_ONLY and act.source != "owner":
            raise Refused("Only the owner records a legal exception.")
        who = act.source
        if act.action == "record_terms":
            made = record_terms(led, now, lease=f.get("lease", ""), signed_on=_when(f.get("signed_on"), "signing date"), agreement_sha256=f.get("agreement_sha256"),
                                notice_methods=f.get("notice_methods", []), notice_days=f.get("notice_days"),
                                email_notice_authorized=f.get("email_notice_authorized", False), last_known_address=f.get("last_known_address", ""),
                                last_known_email=f.get("last_known_email", ""), designated_method=f.get("designated_method"),
                                price_promise_until=_maybe(f.get("price_promise_until"), "promise date"),
                                price_promise_max_cents=f.get("price_promise_max_cents"), rent_change_allowed=f.get("rent_change_allowed", True))
            check = "Lease terms recorded from the signed agreement."
        elif act.action == "record_commitment":
            made = record_commitment(led, now, lease=f.get("lease", ""), kind=f.get("kind"), max_cents=f.get("max_cents"),
                                     covers_from=_when(f.get("covers_from"), "start date"), covers_to=_when(f.get("covers_to"), "end date"),
                                     disclosed_on=_when(f.get("disclosed_on"), "disclosure date"), evidence_sha256=f.get("evidence_sha256"), first_page=f.get("first_page", False))
            check = "Disclosed maximum recorded."
        elif act.action == "record_notice":
            made = record_notice(led, now, change=f.get("change", ""), lease=f.get("lease", ""), document_sha256=f.get("document_sha256"),
                                 recipient=f.get("recipient", ""), channel=f.get("channel"), sent_to=f.get("sent_to", ""),
                                 attempted_on=_when(f.get("attempted_on"), "date it was sent"), dispatch_accepted=f.get("dispatch_accepted", False), by=who,
                                 certificate_of_mailing=f.get("certificate_of_mailing", False), postage_prepaid=f.get("postage_prepaid", False),
                                 confirmation=f.get("confirmation"), delivered_on=_maybe(f.get("delivered_on"), "delivery date"),
                                 acknowledged_on=_maybe(f.get("acknowledged_on"), "acknowledgment date"), new_cents=f.get("new_cents"),
                                 effective_on=_when(f.get("effective_on"), "effective date stated in the notice"))
            check = "Notice recorded as attempted. Whether it counts as service is decided when the change is checked."
        elif act.action == "update_notice":
            update_notice(led, now, change=f.get("change", ""), notice=f.get("notice", ""), confirmation=f.get("confirmation"),
                          delivered_on=_maybe(f.get("delivered_on"), "delivery date"), acknowledged_on=_maybe(f.get("acknowledged_on"), "acknowledgment date"))
            made, check = f.get("notice"), "Notice updated."
        elif act.action == "record_emergency":
            made = record_emergency(led, now, place=f.get("place", ""), name=f.get("name", ""), source=f.get("source", ""), starts=_when(f.get("starts"), "start date"),
                                    ends=_maybe(f.get("ends"), "end date"), products=f.get("products", ["storage"]), scope=f.get("scope", ""))
            check = "Declaration recorded. It is not applied until staff check the facility's status."
        elif act.action == "extend_emergency":
            record_emergency_extension(led, now, restriction=f.get("restriction", ""), ends=_when(f.get("ends"), "end date"), source=f.get("source", ""))
            made, check = f.get("restriction"), "Extension recorded."
        elif act.action == "record_emergency_check":
            made = record_emergency_check(led, now, place=f.get("place", ""), checked_on=_when(f.get("checked_on"), "check date"),
                                          valid_until=_when(f.get("valid_until"), "expiry date"), by=who, source=f.get("source", ""), active=f.get("active", []))
            check = "Emergency status recorded until its expiry date."
        elif act.action == "record_baseline":
            made = record_baseline(led, now, restriction=f.get("restriction", ""), subject=f.get("subject", ""), price_cents=f.get("price_cents"),
                                   observed_on=_when(f.get("observed_on"), "observation date"), source=f.get("source", ""), review=f.get("review", "pending"), by=who)
            check = "Pre-declaration price recorded."
        elif act.action == "record_exception":
            made = record_exception(led, now, change=f.get("change", ""), basis=f.get("basis", ""), covers_cents=f.get("covers_cents"),
                                    document_sha256=f.get("document_sha256"), by=who)
            check = "Legal exception recorded for the price it covers."
        else:
            point = "approval" if act.action == "approve_price_change" else f.get("point", "proposal")
            if point not in POINTS:
                raise Refused("choose where in the change's life it is being checked")
            change = _change_from(act.about, f)
            if act.action == "check_price_change":
                res = decide(led, now, change, point=point, profile=profile)
                made = res.id
            else:
                res = evaluate(books, change, now, point=point, profile=profile)
                made = record_decision(led, now, change=change.id, point=point, outcome=res.outcome, reasons=res.reasons, records=res.records, versions=res.versions,
                                       lease=change.lease, invoice=change.invoice, snapshot=_snapshot(change))
            if res.allowed and act.action == "approve_price_change":
                record_approval(led, now, change=change.id, versions=res.versions, by=who, snapshot=_revision(change))
            check, code = _stated(res)
            ok, why = res.allowed, code
            if ok:
                why = None
        if act.action not in ("check_price_change", "approve_price_change"):
            ok, why = True, None
    except Refused as exc:
        check, made = str(exc), None
    led.emit("gate.decided", now, {"id": act.id, "action": act.action, "source": act.source, "about": act.about, "command": act.id,
                                 "allowed": ok, "check": check, "why": why, "made": made}, f"gate:{act.id}", source=act.source, command=act.id)
    return Decision(ok, act.action, check, command=act.id, made=made, why=why)


def run(store: Store, stream: str, act: Act, now: datetime, profile: FacilityProfile | None = None) -> Decision:
    with store.write():
        led = Ledger(store.books(stream))
        decision = command(led, act, now, profile)
        if not decision.repeated:
            decision.events = [event.id for event in store.append(stream, led.pending) if event.id is not None]
        return decision
