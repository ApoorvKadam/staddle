"""Protection plans and the tenant's own insurance proof: two separate records.

The facility's protection plan is a simulated product. A plan has versions (product, version, monthly price, terms, the day it takes
effect); a new price or new terms is a new version and the old one stays in the history. An offer names a version, a price, a lease and a
unit. Acceptance is its own record and must be explicit and attributable. An offer, a price-list change, a staff tick, a statement line, an
expired private policy or a note is never acceptance. An enrolment is offered, accepted, active (simulated) or ended. No insurance policy is
ever issued, and no text a staff member reads calls the plan insurance.

The tenant's own policy is a different record: carrier, policy number, dates, whether a document came, and what staff did with it. Staff
review is the facility's check of a document and is never called the insurer's confirmation.

Events (in the copy's own stream, folded by the books into `Books.protection`): protection.version_published, protection.offered,
protection.accepted, protection.enrolled, protection.ended, protection.noted, protection.charged, protection.collect_requested,
protection.statement_approved, protection.evidence_recorded, protection.evidence_reviewed, protection.evidence_expired,
protection.notice_sent. Nothing here moves money except a plan charge, which is one ordinary charge posted through the ledger."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .billing import Ledger, Refused
from .books import PROTECTION, Books, local
from .evidence import day as dayname, money
from .gate import STAFF_ROLES, WHO, Decision
from .store import Store

PRODUCT = "protection_plan"
SIMULATED = "Simulated protection-plan enrolment, no insurance policy issued"
NOT_THE_INSURER = "Staff review of the document; not confirmed with the insurer"       # what every review is, in the words kept with it


@dataclass
class Version:
    product: str
    version: int
    cents: int
    terms: str
    effective: date
    by: str
    at: datetime


@dataclass
class Acceptance:
    """The tenant's own yes, as its own record: who, how, in their words, to which version, price, lease and unit, from which day."""
    id: str
    enrolment: str
    tenant: str
    lease: str
    unit: str
    product: str
    version: int
    cents: int
    by: str
    how: str
    words: str
    starts: date
    at: datetime


@dataclass
class Enrolment:
    """An offer and what became of it. Its terms are frozen when it is offered; a new version, price or unit is a new enrolment."""
    id: str
    lease: str
    unit: str
    tenant: str
    product: str
    version: int
    cents: int
    offered_by: str
    offered_at: datetime
    replaces: str | None = None                 # the enrolment a newer version takes over from once it is accepted and active
    state: str = "offered"                      # offered, accepted, active (simulated), ended
    acceptance: str | None = None
    starts: date | None = None                  # the first day it covers, once active
    basis: str | None = None                    # acceptance, or owner rule (moved onto the plan after missing proof, with the notice on record)
    authority: dict = field(default_factory=dict)           # the acceptance, or the owner rule and its notice, that a charge names
    ended: dict | None = None                   # on (the first day not covered), reason, by, at, unused days and cents, the charge they come from
    notes: list[dict] = field(default_factory=list)         # staff ticks and notes: kept, never acceptance
    history: list[dict] = field(default_factory=list)       # every move, with who and when

    def covers(self, period: str) -> bool:
        """Whether the enrolment covers this month ("2026-11"): it covered at least one day, started by the month's last day and has not ended
        before its first. The end date is the first day not covered, so a plan that ends the day it starts covered nothing."""
        first, last = month_span(period)
        return self.starts is not None and not self._empty() and self.starts <= last and (self.ended is None or self.ended["on"] > first)

    def _empty(self) -> bool:
        return self.ended is not None and self.starts is not None and self.ended["on"] <= self.starts

    def periods(self, through: date) -> list[str]:
        """The months it covers, from the start month to the end date or `through`, whichever is first."""
        if self.starts is None or self._empty():
            return []
        stop = min(through, self.ended["on"] - timedelta(days=1)) if self.ended else through
        out, y, m = [], self.starts.year, self.starts.month
        while (y, m) <= (stop.year, stop.month):
            out.append(f"{y:04d}-{m:02d}")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return out


@dataclass
class Evidence:
    """The tenant's own policy, as far as staff know it. It is not an enrolment and enrols nobody."""
    id: str
    lease: str
    unit: str
    tenant: str
    carrier: str | None
    number: str | None
    effective: date | None
    expires: date | None
    document: str | None                        # what was received; None when only the tenant's words are on file
    statement: str
    flags: tuple[str, ...]
    status: str                                 # reported, received, needs review, reviewed, rejected, expired
    recorded_by: str
    recorded_at: datetime
    renews: str | None = None
    review: dict | None = None                  # outcome, by, how, what was checked, a note, when, and what the review is not
    history: list[dict] = field(default_factory=list)


def month_span(period: str) -> tuple[date, date]:
    """The first and last day of a month written "2026-11"."""
    y, m = map(int, period.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


class State:
    """The protection records of one stream, folded event by event."""

    def __init__(self):
        self.versions: dict[str, list[Version]] = {}
        self.enrolments: dict[str, Enrolment] = {}
        self.acceptances: dict[str, Acceptance] = {}
        self.evidence: dict[str, Evidence] = {}
        self.notices: dict[str, dict] = {}                  # the notices sent before an owner rule moves a tenant onto the plan
        self.charges: dict[str, dict] = {}                  # plan charge id -> the enrolment, authority, version, price, lease, unit and period it was posted on

    def apply(self, kind: str, at: datetime, p: dict) -> None:
        if kind == "protection.version_published":
            self.versions.setdefault(p["product"], []).append(
                Version(p["product"], p["version"], p["cents"], p["terms"], date.fromisoformat(p["effective"]), p["by"], at))
        elif kind == "protection.offered":
            e = self.enrolments[p["enrolment"]] = Enrolment(p["enrolment"], p["lease"], p["unit"], p["tenant"], p["product"], p["version"], p["cents"],
                                                            p["by"], at, p.get("replaces"))
            e.history.append({"state": "offered", "at": at, "by": p["by"], "text": ""})
        elif kind == "protection.accepted":
            self.acceptances[p["acceptance"]] = Acceptance(p["acceptance"], p["enrolment"], p["tenant"], p["lease"], p["unit"], p["product"], p["version"],
                                                           p["cents"], p["by"], p["how"], p["words"], date.fromisoformat(p["starts"]), at)
            e = self.enrolments[p["enrolment"]]
            e.state, e.acceptance = "accepted", p["acceptance"]
            e.history.append({"state": "accepted", "at": at, "by": p["by"], "text": p["words"]})
        elif kind == "protection.noted":
            self.enrolments[p["enrolment"]].notes.append({"kind": p["note"], "text": p["text"], "by": p["by"], "at": at})
        elif kind == "protection.evidence_recorded":
            iso = lambda k: date.fromisoformat(p[k]) if p.get(k) else None
            self.evidence[p["evidence"]] = e = Evidence(p["evidence"], p["lease"], p["unit"], p["tenant"], p["carrier"], p["number"], iso("effective"),
                                                        iso("expires"), p["document"], p["statement"], tuple(p["flags"]), p["status"], p["by"], at, p["renews"])
            e.history.append({"status": e.status, "at": at, "by": p["by"], "text": ", ".join(e.flags)})
        elif kind == "protection.evidence_reviewed":
            e = self.evidence[p["evidence"]]
            e.status = p["outcome"]
            e.review = {"outcome": p["outcome"], "by": p["by"], "how": p["how"], "checked": list(p["checked"]), "note": p["note"], "at": at,
                        "confirmation": NOT_THE_INSURER}
            e.history.append({"status": e.status, "at": at, "by": p["by"], "text": p["note"] or p["how"]})
        elif kind == "protection.evidence_expired":
            e = self.evidence[p["evidence"]]
            e.status = "expired"
            e.history.append({"status": "expired", "at": at, "by": "code", "text": f"the policy ended {e.expires.isoformat()}"})
        elif kind == "protection.notice_sent":
            self.notices[p["notice"]] = {**p, "at": at}
        elif kind == "protection.charged":
            self.charges[p["charge"]] = {**p, "at": at}
        elif kind == "protection.enrolled":
            e = self.enrolments[p["enrolment"]]
            e.state, e.starts, e.basis, e.authority = "active", date.fromisoformat(p["starts"]), p["basis"], p["authority"]
            e.history.append({"state": "active", "at": at, "by": p["by"], "text": SIMULATED})
        elif kind == "protection.ended":
            e = self.enrolments[p["enrolment"]]
            e.state = "ended"
            e.ended = {"on": date.fromisoformat(p["on"]), "reason": p["reason"], "by": p["by"], "at": at, "unused_days": p["unused_days"],
                       "unused_cents": p["unused_cents"], "charge": p["charge"]}
            e.history.append({"state": "ended", "at": at, "by": p["by"], "text": p["reason"]})


def state(books: Books) -> State:
    """The books' protection records; empty when nothing was ever written."""
    return books.protection or State()


# plan products ------------------------------------------------------------------------------------------------------------------
def _words(text: str | None) -> str:
    return " ".join((text or "").split())


def publish_version(led: Ledger, product: str, cents: int, terms: str, effective: date, by: str, at: datetime) -> int:
    """Add the next version of a plan. It must change the price or the terms, and may not take effect before the version it follows. The
    versions before it stay as they were; no enrolment moves to it (that needs a new acceptance)."""
    terms, versions = _words(terms), state(led.books).versions.get(product, [])
    if not isinstance(cents, int) or cents <= 0:
        raise Refused("a plan needs a monthly price above zero")
    if not terms:
        raise Refused("a plan needs its terms in words")
    if versions and (cents, terms) == (versions[-1].cents, versions[-1].terms):
        raise Refused("the latest version already has this price and these terms: a new version needs a new price or new terms")
    if versions and effective < versions[-1].effective:
        raise Refused(f"a new version cannot take effect before the one it follows ({versions[-1].effective.isoformat()})")
    n = len(versions) + 1
    led.emit("protection.version_published", at, {"product": product, "version": n, "cents": cents, "terms": terms,
                                                  "effective": effective.isoformat(), "by": by}, f"protection:version:{product}:{n}", source=by)
    return n


# offers and acceptance --------------------------------------------------------------------------------------------------------
ACCEPT_HOWS = ("signed agreement", "tenant message", "online form")     # the tenant's own acts; a staff tick or a statement line is none of them
NOTES = ("staff tick", "note")


def version_in_force(books: Books, product: str, on: date, version: int | None = None) -> Version:
    versions = state(books).versions.get(product, [])
    if version is not None:
        if not 1 <= version <= len(versions):
            raise Refused(f"there is no version {version} of the plan")
        return versions[version - 1]
    live = [v for v in versions if v.effective <= on]
    if not live:
        raise Refused("no version of the plan is in force yet")
    return live[-1]


def offer(led: Ledger, lease: str, by: str, at: datetime, version: int | None = None, replaces: str | None = None, product: str = PRODUCT) -> str:
    """Offer a version of the plan on a lease. The offer freezes the version, its price, the lease and the unit; it is not acceptance and
    enrols nobody. A lease has one open offer at a time, and an active plan is only replaced by a newer version that names it."""
    rec, v = _offer_check(led.books, lease, at, version, replaces, product)
    st = state(led.books)
    eid = f"PE{len(st.enrolments) + 1:04d}"
    payload = {"enrolment": eid, "lease": lease, "unit": rec.unit, "tenant": rec.tenant, "product": product, "version": v.version, "cents": v.cents, "by": by}
    led.emit("protection.offered", at, payload | ({"replaces": replaces} if replaces else {}), f"protection:offer:{eid}", source=by)
    return eid


def _offer_check(books: Books, lease: str, at: datetime, version: int | None, replaces: str | None, product: str, ending: str | None = None):
    """Everything an offer needs to be true, as the lease and the version it would name; raises Refused and writes nothing. `ending` is a plan
    about to end in the same step (a transfer): it does not count as an active plan on the lease."""
    st = state(books)
    rec = books.leases.get(lease)
    if rec is None:
        raise Refused(f"there is no lease {lease}")
    if rec.ended is not None:
        raise Refused(f"the lease on unit {rec.unit} has ended")
    if any(e.lease == lease and e.state in ("offered", "accepted") for e in st.enrolments.values()):
        raise Refused("an offer is already open for this lease")
    if (old := next((e for e in st.enrolments.values() if e.lease == lease and e.state == "active" and e.id != ending), None)) and replaces != old.id:
        raise Refused("the lease is already on the plan; a newer version replaces it only when the offer names it")
    if replaces and (replaces not in st.enrolments or st.enrolments[replaces].lease != lease or st.enrolments[replaces].state != "active"):
        raise Refused("only an active plan on the same lease can be replaced")
    v = version_in_force(books, product, local(at).date(), version)
    if replaces and v.version <= st.enrolments[replaces].version:
        raise Refused(f"a replacement must be a newer version than the plan it replaces (version {st.enrolments[replaces].version})")
    return rec, v


def accept(led: Ledger, enrolment: str, by: str, how: str, words: str, version: int, cents: int, starts: date, at: datetime) -> str:
    """The tenant's explicit acceptance of this offer: their own act (`how`), their own words, the version and price they saw, and the day
    coverage starts. Anything else is refused: it is not acceptance."""
    st = state(led.books)
    e, words = st.enrolments.get(enrolment), _words(words)
    if e is None:
        raise Refused("there is no such offer")
    if e.state != "offered":
        raise Refused("that offer was already accepted" if e.state == "accepted" else f"that offer is already {e.state}")
    if how not in ACCEPT_HOWS:
        raise Refused(f"acceptance must be explicit: {', '.join(ACCEPT_HOWS)}; a staff tick or a note is not acceptance")
    if len(words) < 3:
        raise Refused("acceptance needs the tenant's own words")
    if by != e.tenant:
        raise Refused("acceptance is the tenant's own; nobody else can give it for them")
    if (version, cents) != (e.version, e.cents):
        raise Refused(f"that acceptance is for other terms than this offer (version {e.version} at {money(e.cents)})")
    if starts < led.books.leases[e.lease].start:
        raise Refused("coverage cannot start before the lease does")
    if problem := _effective_check(led.books, e, starts):
        raise Refused(problem[1])
    aid = f"PA{len(st.acceptances) + 1:04d}"
    led.emit("protection.accepted", at, {"acceptance": aid, "enrolment": enrolment, "tenant": by, "lease": e.lease, "unit": e.unit, "product": e.product,
                                         "version": version, "cents": cents, "by": by, "how": how, "words": words, "starts": starts.isoformat()},
             f"protection:accept:{aid}", source=by)
    return aid


def note(led: Ledger, enrolment: str, kind: str, text: str, by: str, at: datetime) -> None:
    """A staff tick or a note on an offer. Kept as written; it changes no state and is never acceptance."""
    e = state(led.books).enrolments.get(enrolment)
    if e is None:
        raise Refused("there is no such offer")
    if kind not in NOTES:
        raise Refused(f"a note is one of: {', '.join(NOTES)}")
    led.emit("protection.noted", at, {"enrolment": enrolment, "note": kind, "text": _words(text), "by": by}, f"protection:note:{enrolment}:{len(e.notes) + 1}",
             source=by)


# enrolments ---------------------------------------------------------------------------------------------------------------------
END_REASONS = ("move-out", "cancellation", "transfer", "newer version")


def enrol_check(books: Books, enrolment: str) -> tuple[str, str] | None:
    """Why an offer cannot be made active, as (the reason's name, words), or None when it can: an accepted offer, active only once."""
    st = state(books)
    e = st.enrolments.get(enrolment)
    if e is None:
        return "no_offer", "there is no such offer"
    if e.state == "active":
        return "already_active", "that plan is already active"
    if e.state == "ended":
        return "plan_ended", "that plan already ended"
    if e.state == "offered":
        if any(a.tenant == e.tenant for a in st.acceptances.values()):      # a yes on record, but to other terms
            return "acceptance_other_terms", "the acceptance on record is for another price, version or unit"
        return "no_acceptance", "no explicit acceptance of this offer is on record"
    starts = st.acceptances[e.acceptance].starts
    return _start_check(books, e, starts) or _replacement_check(st, e, starts)


def _start_check(books: Books, e: Enrolment, starts: date) -> tuple[str, str] | None:
    """Whether coverage can begin on `starts`: the lease is still running and has begun by then. Checked before any activation writes an event."""
    lease = books.leases[e.lease]
    if lease.ended is not None:
        return "lease_ended", f"the lease on unit {e.unit} ended {lease.ended.isoformat()}, so the plan cannot be made active"
    if starts < lease.start:
        return "before_lease", f"coverage cannot start before the lease does ({lease.start.isoformat()})"
    return _effective_check(books, e, starts)


def _effective_check(books: Books, e: Enrolment, starts: date) -> tuple[str, str] | None:
    """Coverage cannot begin before the version it is on takes effect; future terms can be offered, and covered from their first day."""
    versions = state(books).versions.get(e.product, [])
    if 1 <= e.version <= len(versions) and starts < versions[e.version - 1].effective:
        return "before_effective", f"version {e.version} of the plan takes effect {versions[e.version - 1].effective.isoformat()}, so coverage cannot start before then"
    return None


def _replacement_check(st: State, e: Enrolment, starts: date) -> tuple[str, str] | None:
    """A newer version takes over from the plan it replaces on its first day, so it cannot start before that plan did."""
    old = st.enrolments.get(e.replaces) if e.replaces else None
    if old is not None and old.state == "active" and starts < old.starts:
        return "starts_early", f"the new plan cannot start before the one it replaces began ({old.starts.isoformat()})"
    return None


def activate(led: Ledger, enrolment: str, by: str, at: datetime) -> None:
    """Make an accepted offer active (a simulated enrolment, no policy issued). It covers from the day the acceptance says. When it replaces
    an earlier version on the lease, that one ends the same day."""
    if problem := enrol_check(led.books, enrolment):
        raise Refused(problem[1])
    st = state(led.books)
    e = st.enrolments[enrolment]
    acc = st.acceptances[e.acceptance]
    led.emit("protection.enrolled", at, {"enrolment": enrolment, "starts": acc.starts.isoformat(), "basis": "acceptance", "authority": {"acceptance": acc.id},
                                         "by": by}, f"protection:enrol:{enrolment}", source=by)
    if e.replaces and st.enrolments[e.replaces].state == "active":
        end_enrolment(led, e.replaces, acc.starts, "newer version", by, at)


RULE = "protection_after_missing_proof"       # the owner's rule that moves a tenant onto the plan after missing proof (typed: notice_days)
NOTICE_HOWS = ("email", "mail", "text message", "in person")


def send_notice(led: Ledger, enrolment: str, by: str, at: datetime, how: str, words: str) -> str:
    """Record the notice that a tenant with no current proof of their own policy will be moved onto the plan: which version, price and unit,
    how it was sent and what it said. It is the notice the owner's rule asks for; it is not acceptance."""
    e, words = state(led.books).enrolments.get(enrolment), _words(words)
    if e is None or e.state != "offered":
        raise Refused("a notice goes with an offer nobody has answered")
    if how not in NOTICE_HOWS:
        raise Refused(f"a notice is sent by one of: {', '.join(NOTICE_HOWS)}")
    if len(words) < 20:
        raise Refused("write down what the notice said")
    nid = f"PN{len(state(led.books).notices) + 1:04d}"
    led.emit("protection.notice_sent", at, {"notice": nid, "enrolment": enrolment, "tenant": e.tenant, "lease": e.lease, "unit": e.unit, "version": e.version,
                                            "cents": e.cents, "by": by, "how": how, "words": words}, f"protection:notice:{nid}", source=by)
    return nid


def activate_by_rule(led: Ledger, enrolment: str, by: str, at: datetime, notice: str, rule: int, starts: date) -> None:
    """Make an offer nobody accepted active under the owner's rule, naming the rule's version and the notice. The caller has checked both."""
    st = state(led.books)
    e = st.enrolments[enrolment]
    if problem := _start_check(led.books, e, starts):
        raise Refused(problem[1])
    led.emit("protection.enrolled", at, {"enrolment": enrolment, "starts": starts.isoformat(), "basis": "owner rule", "authority": {"rule": rule, "notice": notice},
                                         "by": by}, f"protection:enrol:{enrolment}", source=by)
    if e.replaces and st.enrolments[e.replaces].state == "active":
        end_enrolment(led, e.replaces, starts, "newer version", by, at)


def end_enrolment(led: Ledger, enrolment: str, on: date, reason: str, by: str, at: datetime) -> None:
    """End an active plan. `on` is the first day it no longer covers. The unused days of the month `on` falls in are counted once, by that
    month's actual days, against the plan charge already billed for it; the amount is a record of what the tenant did not use, and no
    money moves here. Refused when the plan is not active, was already ended, or `on` is before it began."""
    st = state(led.books)
    e = st.enrolments.get(enrolment)
    if e is None:
        raise Refused("there is no such plan")
    if e.state == "ended":
        raise Refused("that plan is already ended")
    if e.state != "active":
        raise Refused("only an active plan can end")
    if reason not in END_REASONS:
        raise Refused(f"the reason for ending is one of: {', '.join(END_REASONS)}")
    if on < e.starts:
        raise Refused("a plan cannot end before coverage started")
    left = led.books.leases[e.lease].ended
    if left is not None and on > left + timedelta(days=1):
        raise Refused(f"the lease ended {dayname(left)}, so the plan cannot cover days after that: it stops covering from {dayname(left + timedelta(days=1))}")
    period = f"{on.year:04d}-{on.month:02d}"
    # only the charge recorded against this enrolment: another version's charge for the month is that version's, and was prorated when it ended
    billed = [c for c in led.books.live(f"protection:{e.lease}:{period}") if st.charges.get(c.id, {}).get("enrolment") == e.id]
    days = calendar.monthrange(on.year, on.month)[1]
    unused = days - on.day + 1 if billed else 0
    cents = (2 * billed[0].cents * unused + days) // (2 * days) if billed else 0        # half up to the cent, as the first month's rent is
    led.emit("protection.ended", at, {"enrolment": enrolment, "on": on.isoformat(), "reason": reason, "by": by, "unused_days": unused,
                                      "unused_cents": cents, "charge": billed[0].id if billed else None}, f"protection:end:{enrolment}", source=by)


def end_at_move_out(led: Ledger, enrolment: str, by: str, at: datetime) -> None:
    """End the plan with its lease: the first day not covered is the day after the tenant's last day."""
    e = state(led.books).enrolments.get(enrolment)
    if e is None:
        raise Refused("there is no such plan")
    lease = led.books.leases[e.lease]
    if lease.ended is None:
        raise Refused("the lease has not ended")
    end_enrolment(led, enrolment, lease.ended + timedelta(days=1), "move-out", by, at)


def reoffer(led: Ledger, enrolment: str, lease: str, by: str, at: datetime, version: int | None = None) -> str:
    """A transfer: the plan on the unit the tenant leaves ends, and the tenant is offered the plan on the new lease. Nothing carries over:
    the new offer has its own version and price and needs its own acceptance."""
    st = state(led.books)
    old = st.enrolments.get(enrolment)
    new = led.books.leases.get(lease)
    if old is None or new is None:
        raise Refused("there is no such plan or lease")
    if new.tenant != old.tenant:
        raise Refused("a transfer stays with the same tenant")
    _offer_check(led.books, lease, at, version, None, old.product, ending=old.id)      # the new offer must be possible before the old plan ends, so a refusal leaves nothing written
    end_enrolment(led, enrolment, local(at).date(), "transfer", by, at)
    return offer(led, lease, by, at, version, product=old.product)


# the tenant's own policy ------------------------------------------------------------------------------------------------------
REVIEW_CHECKS = ("carrier", "policy number", "dates")       # what staff read on the document for a review to count
OPEN_STATES = ("reported", "received", "needs review")      # the states a review can still move
STATEMENT_MAX = 300
FLAG_WORDS = {"placeholder number": "The policy number looks like a placeholder.", "no expiry date": "It names no expiry date.",
              "already expired": "Its expiry date has already passed."}
EXPIRY_WINDOW_DAYS = 30             # a reviewed policy this close to its end becomes an item of work before it lapses
REVIEW_DUE_DAYS = 2                 # staff look at a new policy document within two days of its arrival
PLACEHOLDER_STARTS = ("TEMP", "TBD", "TEST", "PENDING", "UNKNOWN", "NONE")


def placeholder_number(number: str | None) -> bool:
    """Whether a policy number is a stand-in (TEMP, TBD, 0000000, 123456789) or too short to be one."""
    s = re.sub(r"[^A-Z0-9]", "", (number or "").upper())
    return len(s) < 5 or len(set(s)) == 1 or s.startswith(PLACEHOLDER_STARTS) or s in "0123456789" or s in "9876543210"


def standing(st: State, lease: str) -> Evidence | None:
    """The tenant's policy as it stands for a lease: the newest record that was not rejected."""
    return next((e for e in reversed(list(st.evidence.values())) if e.lease == lease and e.status != "rejected"), None)


def last_reviewed(st: State, lease: str) -> Evidence | None:
    """The newest policy record on the lease whose review passed (it may have expired since): what the tenant's proof last stood on."""
    return next((e for e in reversed(list(st.evidence.values())) if e.lease == lease and e.review and e.review["outcome"] == "reviewed"), None)


def open_statement(st: State, lease: str) -> str | None:
    """The newest statement on the lease that came with no document and is still waiting for one: a statement whose document was uploaded and
    passed review is answered, though it stays "reported"; one whose upload is still unreviewed is not."""
    return next((e.id for e in reversed(list(st.evidence.values())) if e.lease == lease and e.document is None and e.status == "reported"
                 and not document_arrived(st, e)), None)


def document_arrived(st: State, statement: Evidence) -> bool:
    """Whether the document a statement was waiting for was uploaded against it and passed staff review."""
    return any(x.renews == statement.id and x.review and x.review["outcome"] == "reviewed" for x in st.evidence.values())


def current_proof(st: State, lease: str, on: date) -> Evidence | None:
    """The reviewed policy on the lease that covers `on`: staff passed its review, it has started and has not ended. An unreviewed renewal or a
    policy that starts later is not it, and neither replaces it. A reviewed policy with no start date counts as started: the tenant is not moved
    onto a paid plan on a guess."""
    return next((e for e in reversed(list(st.evidence.values())) if e.lease == lease and e.review and e.review["outcome"] == "reviewed"
                 and e.expires is not None and e.expires >= on and (e.effective is None or e.effective <= on)), None)


def record_evidence(led: Ledger, lease: str, by: str, at: datetime, carrier: str | None = None, number: str | None = None, effective: date | None = None,
                    expires: date | None = None, document: str | None = None, statement: str = "", renews: str | None = None) -> str:
    """Record what a tenant gave about their own policy. With a document the carrier and number are the ones on it; with only words it is
    reported. A placeholder number, no expiry date or a date already past puts it in needs review. Nothing here enrols anyone."""
    books, st = led.books, state(led.books)
    rec = books.leases.get(lease)
    carrier, number, document, statement = _words(carrier) or None, _words(number) or None, _words(document) or None, _words(statement)
    if rec is None:
        raise Refused(f"there is no lease {lease}")
    if rec.ended is not None:
        raise Refused(f"the lease on unit {rec.unit} has ended")
    if not (carrier or number or document or statement):
        raise Refused("there is nothing to record")
    if len(statement) > STATEMENT_MAX:
        raise Refused(f"keep the tenant's words under {STATEMENT_MAX} characters")
    if document and not (carrier and number):
        raise Refused("a document needs the carrier and the policy number as written on it")
    if effective and expires and expires < effective:
        raise Refused("the policy ends before it starts")
    if renews and (renews not in st.evidence or st.evidence[renews].lease != lease):
        raise Refused("a renewal follows a policy record on the same lease")
    if renews is None and document:          # the document staff asked for answers the statement still waiting for one on this lease
        renews = open_statement(st, lease)
    if document and any(e.lease == lease and e.status != "rejected" and e.document and
                        (e.carrier, e.number, e.effective, e.expires, e.document) == (carrier, number, effective, expires, document) for e in st.evidence.values()):
        raise Refused("that document is already on record")
    flags = ()
    if document:
        flags = tuple(f for f, bad in (("placeholder number", placeholder_number(number)), ("no expiry date", expires is None),
                                       ("already expired", expires is not None and expires < local(at).date())) if bad)
    status = "reported" if not document else "needs review" if flags else "received"
    eid = f"PV{len(st.evidence) + 1:04d}"
    iso = lambda d: d.isoformat() if d else None
    led.emit("protection.evidence_recorded", at, {"evidence": eid, "lease": lease, "unit": rec.unit, "tenant": rec.tenant, "carrier": carrier, "number": number,
                                                  "effective": iso(effective), "expires": iso(expires), "document": document, "statement": statement,
                                                  "flags": list(flags), "status": status, "by": by, "renews": renews}, f"protection:evidence:{eid}", source=by)
    return eid


def review_check(books: Books, evidence: str, outcome: str, how: str, checked: list[str] | None, note: str, at: datetime) -> tuple[str, str] | None:
    """Why a review cannot be recorded, as (the reason's name, words), or None. A review that passes needs a document, a real policy number,
    an expiry date still ahead, and a record of what staff read and how; "looks fine" is none of that."""
    e = state(books).evidence.get(evidence)
    if e is None:
        return "no_evidence", "there is no such policy record"
    if e.status not in OPEN_STATES:
        return {"reviewed": ("already_reviewed", "that policy was already reviewed"),
                "rejected": ("rejected", "that policy record was rejected; record a new upload"),
                "expired": ("expired", "that policy expired; record the renewal as new evidence")}[e.status]
    if outcome == "rejected":
        return None if len(_words(note)) >= 3 else ("no_reason", "say why it is rejected")
    if outcome != "accepted":
        return "outcome", "a review ends accepted or rejected"
    if e.document is None:
        return "no_document", "no policy document is on file, only a statement"
    if placeholder_number(e.number):
        return "placeholder_number", "the policy number is a placeholder; a valid policy number is needed first"
    if e.expires is None:
        return "no_expiry", "the document names no expiry date, and a review cannot pass without one"
    if e.expires < local(at).date():
        return "expired", "the policy on the document has already ended"
    if len(_words(how)) < 10 or not set(REVIEW_CHECKS) <= set(checked or ()):
        return "not_reviewed", "no staff review of the document is recorded: say what was checked (carrier, policy number, dates) and how"
    return None


def review_evidence(led: Ledger, evidence: str, by: str, at: datetime, outcome: str, how: str, checked: list[str], note: str = "") -> None:
    """Record staff's review of a policy document: accepted (the status is "reviewed") or rejected. It is the facility's check of the
    document and is kept as that; the insurer is never asked and is never said to have confirmed anything."""
    if problem := review_check(led.books, evidence, outcome, how, checked, note, at):
        raise Refused(problem[1])
    led.emit("protection.evidence_reviewed", at, {"evidence": evidence, "outcome": "reviewed" if outcome == "accepted" else "rejected", "by": by,
                                                  "how": _words(how), "checked": list(checked or ()), "note": _words(note)},
             f"protection:review:{evidence}", source=by)


def expire_evidence(led: Ledger, evidence: str, at: datetime) -> None:
    """The daily check's move: a policy whose expiry date has passed becomes expired. Its earlier states and its review stay in the history."""
    e = state(led.books).evidence.get(evidence)
    if e is None:
        raise Refused("there is no such policy record")
    if e.status == "expired":
        raise Refused("that policy is already expired")
    if e.status == "rejected" or e.expires is None or e.expires >= local(at).date():
        raise Refused("that policy is not yet past its expiry date")
    led.emit("protection.evidence_expired", at, {"evidence": evidence}, f"protection:expired:{evidence}", source="code")


def sweep(store: Store, stream: str, now: datetime) -> int:
    """The daily check of the tenant's own policies: each one past its expiry date becomes expired, its history kept. The plan's charges are
    read against enrolments, covered periods and lease end dates by the work queue (core/work.py), which runs right after this. Returns the
    events written; a second pass writes none."""
    with store.write():
        led = Ledger(store.books(stream))
        today = local(now).date()
        for e in list(state(led.books).evidence.values()):
            if e.status in OPEN_STATES + ("reviewed",) and e.expires is not None and e.expires < today:
                expire_evidence(led, e.id, now)
        store.append(stream, led.pending)
        return len(led.pending)


# the plan's charge ------------------------------------------------------------------------------------------------------------------
_MONTH = r"(?!0000)\d{4}-(?:0[1-9]|1[0-2])"          # a real calendar month: year 0000 and month 13 are not
_OBLIGATION = re.compile(rf"protection:(L\d+):({_MONTH})")
_PERIOD = re.compile(_MONTH)


def covering(st: State, lease: str, period: str) -> Enrolment | None:
    """The enrolment that covers this month on the lease, active or ended since (a charge it covered stays supported)."""
    return next((e for e in st.enrolments.values() if e.lease == lease and e.state in ("active", "ended") and e.covers(period)), None)


def unsupported(books: Books, charge: str) -> str | None:
    """Why a plan charge has nothing behind it, in words, or None. A plan charge is supported by an enrolment on its lease that covers its
    period, at the price that enrolment was accepted at; a charge posted any other way (by hand, say) is not, and is held until staff
    correct it."""
    c = books.charges.get(charge)
    if c is None or c.kind != PROTECTION or c.closed:
        return None
    m = _OBLIGATION.fullmatch(c.obligation or "")
    if m is None or m.group(1) != c.lease:
        return "the charge does not name its lease and its month"
    e = covering(state(books), c.lease, m.group(2))
    if e is None:
        return "no accepted plan covers this period"
    if e.cents != c.cents:
        return f"the plan was accepted at {money(e.cents)}, not {money(c.cents)}"
    if charge not in state(books).charges:
        return "no record shows who authorised this charge"          # right lease, month and price, but posted by hand: nothing it may spend
    return None


def plan_charge_check(books: Books, lease: str, period: str, cents: int | None, version: int | None) -> tuple[Enrolment | None, str, str]:
    """The checks for one plan charge, as (the enrolment that supports it, the reason's name, words); the enrolment is None when it is refused."""
    st, rec = state(books), books.leases.get(lease)
    if rec is None:
        return None, "no_lease", f"there is no lease {lease}"
    if not _PERIOD.fullmatch(period or ""):
        return None, "period", "name the month as year-month, for example 2026-11"
    first, last = month_span(period)
    mine = [e for e in st.enrolments.values() if e.lease == lease]
    if not mine:
        return None, "no_acceptance", "no offer and no explicit acceptance of the protection plan is on record for this lease"
    live = covering(st, lease, period)
    if live is not None:
        if rec.ended is not None and max(first, live.starts) > rec.ended:       # a plan that began after the lease ended covered no day of it
            return None, "plan_ended", f"the lease ended {rec.ended.isoformat()}, so the plan does not run into {period}"
        if (cents is not None and cents != live.cents) or (version is not None and version != live.version):
            return None, "acceptance_other_terms", f"the accepted terms are version {live.version} at {money(live.cents)}; any other price or version needs a new acceptance"
        if twin := books.billed(f"protection:{lease}:{period}"):
            return None, "already_charged", f"this lease already has a plan charge for {period} ({twin[0].id})"
        return live, "", ""
    if any(e.state == "ended" and e.starts is not None and e.starts <= last for e in mine):
        e = next(e for e in mine if e.state == "ended" and e.starts is not None and e.starts <= last)
        return None, "plan_ended", f"the plan ended {e.ended['on'].isoformat()}, before {period}; the lease alone covers nothing"
    if active := next((e for e in mine if e.state == "active"), None):
        return None, "no_acceptance", f"the accepted plan covers from {active.starts.isoformat()}, not {period}"
    if accepted := next((e for e in mine if e.state == "accepted"), None):
        return None, "not_active", f"{accepted.id} is accepted but staff have not made it active yet"
    problem = enrol_check(books, mine[-1].id)
    return None, problem[0], problem[1]


# staff commands through the gate ---------------------------------------------------------------------------------------------------
ACTIONS = {"record_policy_evidence": "record a tenant's own policy", "verify_policy": "review a policy document", "enroll_plan": "enrol a tenant in the protection plan",
           "end_plan": "end a protection plan", "post_plan_charge": "post a protection plan charge", "collect_charge": "collect a protection plan charge",
           "approve_statement": "approve a statement", "offer_plan": "offer the protection plan", "record_acceptance": "record a tenant's acceptance of the plan",
           "publish_plan": "publish a version of the protection plan"}
NOT_STAFF = {k: v for k, v in WHO.items() if k != "staff"}
INSURANCE_WORDS = re.compile(r"\b(?:insur\w*|premium\w*|underwrit\w*|reimburs\w*|indemni\w*|coverage)\b", re.I)
NO_CONSENT = (" A policy that expired or was never reviewed is not consent; moving a tenant onto the plan needs an adopted owner rule with the "
              "notice on record.")


@dataclass
class Act:
    """One staff command. `about` is the enrolment, the policy record or the charge it is about; `form` carries a policy's carrier, number and
    dates, a review's outcome, how and what was checked, an enrolment's basis and notice, or an end's date and reason."""
    id: str
    action: str
    source: str
    about: str | None = None
    lease: str | None = None
    period: str | None = None
    cents: int | None = None
    version: int | None = None
    form: dict | None = None
    text: str | None = None


def _date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise Refused(f"{value!r} is not a date; write it as year-month-day") from None


def _deny(why: str, check: str) -> tuple[bool, str, str, None]:
    return False, why, check, None


def _record_evidence(led: Ledger, act: Act, now: datetime):
    f = act.form or {}
    eid = record_evidence(led, act.lease or "", act.source, now, f.get("carrier"), f.get("number"), _date(f.get("effective")), _date(f.get("expires")),
                          f.get("document"), act.text or "", f.get("renews"))
    e = state(led.books).evidence[eid]
    flags = f" ({', '.join(e.flags)})" if e.flags else ""
    return True, "", f"recorded, not reviewed{flags}; staff review is due {dayname(local(now).date() + timedelta(days=REVIEW_DUE_DAYS))}", eid


def _verify(led: Ledger, act: Act, now: datetime):
    f = act.form or {}
    outcome, note = f.get("outcome", "accepted"), act.text or f.get("note") or ""
    if problem := review_check(led.books, act.about or "", outcome, f.get("how") or "", f.get("checked"), note, now):
        return _deny(*problem)
    review_evidence(led, act.about, act.source, now, outcome, f.get("how") or "", f.get("checked") or [], note)
    return True, "", ("staff review recorded: the facility's check of the document, not the insurer's confirmation" if outcome == "accepted"
                      else "rejected; the reason is on record"), act.about


def _enrolment_for(st: State, act: Act) -> Enrolment | None:
    if act.about:
        return st.enrolments.get(act.about)
    mine = [e for e in st.enrolments.values() if e.lease == act.lease]
    return next((e for e in reversed(mine) if e.state in ("offered", "accepted")), mine[-1] if mine else None)


def _enroll(led: Ledger, act: Act, now: datetime):
    books = led.books
    st = state(books)
    e = _enrolment_for(st, act)
    proof = standing(st, act.lease or (e.lease if e else ""))
    missing = proof is None or proof.status != "reviewed"
    if e is None:
        return _deny("no_acceptance", "no offer and no explicit acceptance of the protection plan is on record for this lease." + (NO_CONSENT if missing else ""))
    basis = (act.form or {}).get("basis")
    if basis == "owner_rule":
        return _enroll_by_rule(led, act, e, now)
    if problem := enrol_check(books, e.id):
        return _deny(problem[0], problem[1] + (NO_CONSENT if problem[0] == "no_acceptance" and missing else ""))
    activate(led, e.id, act.source, now)
    return True, "", f"{SIMULATED}; it covers from {dayname(st.acceptances[e.acceptance].starts)}", e.id


def _enroll_by_rule(led: Ledger, act: Act, e: Enrolment, now: datetime):
    """Moving a tenant onto the plan after their own proof is missing: only by an adopted owner rule, with the notice on record and its days run."""
    books = led.books
    st, today = state(books), local(now).date()
    rule = books.rules.get(RULE)
    typed = (rule or {}).get("typed", {})
    days = typed.get("notice_days")
    if rule is None or typed.get("action") != "enroll_after_missing_proof" or not isinstance(days, int) or isinstance(days, bool) or days < 0:
        return _deny("no_acceptance", "moving a tenant onto the plan after missing proof needs an adopted owner rule with the notice on record, and none is adopted")
    if e.state != "offered":
        problem = enrol_check(books, e.id)
        return _deny(problem[0], problem[1]) if problem else _deny("accepted", "the tenant already accepted this offer; enrol it on that acceptance")
    notice = st.notices.get((act.form or {}).get("notice") or "")
    if notice is None or notice["enrolment"] != e.id:
        return _deny("no_notice", "the notice to the tenant is not on record for this offer")
    sent = local(notice["at"]).date()
    if (today - sent).days < days:
        return _deny("notice_too_recent", f"the notice was sent {dayname(sent)}, and the owner's rule waits {days} days after it")
    if current_proof(st, e.lease, today) is not None:
        return _deny("proof_on_file", "the tenant's own policy is reviewed and current, so proof is not missing")
    starts = max(today, sent + timedelta(days=days))
    if problem := _start_check(books, e, starts) or _replacement_check(st, e, starts):
        return _deny(*problem)
    activate_by_rule(led, e.id, act.source, now, notice["notice"], rule["version"], starts)
    return True, "", f"{SIMULATED}; moved onto the plan under the owner's rule, notice {notice['notice']} on record, covering from {dayname(starts)}", e.id


def _end(led: Ledger, act: Act, now: datetime):
    f = act.form or {}
    on = _date(f.get("on"))
    if on is None:
        raise Refused("say the day the plan stops covering")
    end_enrolment(led, act.about or "", on, f.get("reason") or "", act.source, now)
    ended = state(led.books).enrolments[act.about].ended
    unused = f"; {ended['unused_days']} unused days ({money(ended['unused_cents'])}) are on record" if ended["unused_days"] else ""
    return True, "", f"plan ended; it covers nothing from {dayname(on)}{unused}", act.about


def _post_charge(led: Ledger, act: Act, now: datetime):
    books = led.books
    e, why, words = plan_charge_check(books, act.lease or "", act.period or "", act.cents, act.version)
    if e is None:
        return _deny(why, words)
    first, _ = month_span(act.period)
    cid = led.charge_by_hand(now, act.lease, PROTECTION, e.cents, f"protection:{act.lease}:{act.period}", max(first, local(now).date()),
                             f"Protection plan, {act.period}", act.source.removeprefix("staff:"), credit=False)
    led.emit("protection.charged", now, {"charge": cid, "enrolment": e.id, "authority": e.authority, "lease": e.lease, "unit": e.unit, "period": act.period,
                                         "version": e.version, "cents": e.cents}, f"protection:charged:{e.lease}:{act.period}", source=act.source, command=act.id,
             effects=(f"protection-charge:{e.lease}:{act.period}",))
    led.apply_credit(now, e.tenant)             # only now, with the charge and its authority on record: until then it was held and credit skipped it
    on = (f"on the acceptance of {e.authority['acceptance']}" if "acceptance" in e.authority
          else f"under the owner's rule, after notice {e.authority.get('notice')}")        # a rule's notice is the owner's authority, not a tenant's yes
    return True, "", f"{money(e.cents)} for {act.period}, version {e.version}, {on}", cid


def _collect(led: Ledger, act: Act, now: datetime):
    books = led.books
    ch = books.charges.get(act.about or "")
    if ch is None:
        return _deny("no_charge", "there is no such charge")
    if ch.kind != PROTECTION:
        return _deny("not_plan_charge", "this command collects protection plan charges only")
    if ch.closed:
        return _deny("closed", f"that charge was already {ch.closed}")
    if ch.open_cents == 0:
        return _deny("paid", "nothing is owed on that charge")
    if books.held(ch.id):
        dispute = next((d for d in books.disputes.values() if d.charge == ch.id and d.outcome is None), None)
        return _deny("hold", f"dispute {dispute.id} holds this charge until staff decide it" if dispute
                     else f"{unsupported(books, ch.id)}, so the charge is held until staff correct it")
    led.emit("protection.collect_requested", now, {"charge": ch.id, "by": act.source}, f"protection:collect:{act.id}", source=act.source, command=act.id)
    return True, "", "collection of the plan charge may go ahead; nothing is paid by this", ch.id


def _statement(led: Ledger, act: Act, now: datetime):
    text = _words(act.text)
    if not text:
        raise Refused("a statement needs words")
    if INSURANCE_WORDS.search(re.sub(r"no insurance policy issued", "", text, flags=re.I)):
        return _deny("insurance_label", "the protection plan is not insurance: a statement may not call it insurance, a premium or coverage, so keep the protection-plan label")
    led.emit("protection.statement_approved", now, {"text": text, "by": act.source}, f"protection:statement:{act.id}", source=act.source, command=act.id)
    return True, "", "approved, with the protection-plan label", None


def _offer(led: Ledger, act: Act, now: datetime):
    eid = offer(led, act.lease or "", act.source, now, act.version, (act.form or {}).get("replaces"))
    e = state(led.books).enrolments[eid]
    return True, "", f"offered version {e.version} at {money(e.cents)}; not accepted, and nobody is enrolled", eid


def _accept(led: Ledger, act: Act, now: datetime):
    """Staff write down the tenant's own yes. The page names the version and price the tenant was shown, so a stale page cannot record a yes to
    other terms; the tenant stays the acceptance's author, staff the one who wrote it down."""
    f = act.form or {}
    e = state(led.books).enrolments.get(act.about or "")
    if e is None:
        raise Refused("there is no such offer")
    if act.version is None or act.cents is None:
        raise Refused("name the version and the price the tenant agreed to")
    starts = _date(f.get("starts"))
    if starts is None:
        raise Refused("say the day coverage starts")
    aid = accept(led, e.id, e.tenant, f.get("how") or "", f.get("words") or "", act.version, act.cents, starts, now)
    return True, "", f"the tenant's acceptance is on record; the plan is not active until staff enrol it, and it covers from {dayname(starts)}", aid


def _publish(led: Ledger, act: Act, now: datetime):
    f = act.form or {}
    effective = _date(f.get("effective"))
    if effective is None:
        raise Refused("say the day it takes effect")
    n = publish_version(led, PRODUCT, f.get("cents"), f.get("terms") or "", effective, act.source, now)
    return True, "", f"version {n} of the plan is published and takes effect {dayname(effective)}; nobody moves to it without a new acceptance", str(n)


HANDLERS = {"offer_plan": _offer, "record_acceptance": _accept, "publish_plan": _publish, "record_policy_evidence": _record_evidence, "verify_policy": _verify, "enroll_plan": _enroll, "end_plan": _end, "post_plan_charge": _post_charge,
            "collect_charge": _collect, "approve_statement": _statement}


def decide(led: Ledger, act: Act, now: datetime) -> tuple[bool, str, str, str | None]:
    """The checks, in the gate's order. 1. the action exists and the source is a staff role. 2. safety, fixed in code, each handler's own
    checks, before anything is written. 3. the owner's rule, where enrolling after missing proof asks for it. 4. holds: a held charge is not
    collected. Returns (allowed, the reason's name, words, the record made)."""
    if act.action not in ACTIONS:
        return _deny("", "not an action the system knows")
    role = act.source.removeprefix("staff:")
    if not act.source.startswith("staff:"):
        return _deny("", f"{NOT_STAFF.get(act.source, 'that source')} may not {ACTIONS[act.action]}; only staff may")
    if role not in STAFF_ROLES:
        return _deny("", f"{role or 'that'} is not a staff role; only staff may {ACTIONS[act.action]}")
    try:
        return HANDLERS[act.action](led, act, now)
    except Refused as r:            # every handler validates before it writes, so nothing was emitted
        return _deny("refused", str(r))


def command(led: Ledger, act: Act, now: datetime) -> Decision:
    """Decide and record one command on the books `led` holds. A command id already decided returns that decision and writes nothing."""
    books = led.books
    if act.id in books.decisions:
        p = books.decisions[act.id]
        return Decision(p["allowed"], p["action"], p["check"], command=act.id, repeated=True, made=p.get("made"), why=p.get("why"))
    ok, why, check, made = decide(led, act, now)
    led.emit("gate.decided", now, {"id": act.id, "action": act.action, "source": act.source, "about": act.about, "lease": act.lease, "command": act.id,
                                   "allowed": ok, "check": check, "why": why or None, "made": made}, f"gate:{act.id}", source=act.source, command=act.id)
    return Decision(ok, act.action, check, command=act.id, made=made, why=why or None)


def run(store: Store, stream: str, act: Act, now: datetime) -> Decision:
    """Check and record one staff command in one write transaction."""
    with store.write():
        led = Ledger(store.books(stream))
        d = command(led, act, now)
        if not d.repeated:
            d.events = [e.id for e in store.append(stream, led.pending) if e.id is not None]
        return d
