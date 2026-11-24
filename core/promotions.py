"""First-month deals: the deal, the contract rent and each concession kept as separate records.

A promotion has versions. Each version is an explicit schedule: who and what is eligible, the deal (a price or a percent), exactly which
period it covers, how that period is prorated, when it expires, what it may stack with, whether it follows a transfer, and how many
redemptions and how many cents of concession it may give. A change is a new version; the old ones stay in the history.

A lease keeps its contract rent. A redemption is its own record, made when the lease is signed on a quote that showed the deal and the
first full-price billing date. The concession is a line of its own against the covered period's rent charge: gross rent, concession, net
due and payment are four facts, so the return to the contract rent afterwards is the disclosed end of the deal and never a rent increase.

Every refusal is raised before anything is written, with a machine name (`code`) and plain words for staff.

Events (folded by the books into `Books.promotions`): promotion.version_published, promotion.redeemed, promotion.concession_posted,
promotion.ended. Only promotion.concession_posted moves money, and only as its own journal lines."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from .billing import Ledger, Refused, first_month
from .books import Books, local, month_key
from .evidence import money

TERMS_INCOMPLETE = "PROMOTION_TERMS_INCOMPLETE"
NOT_ELIGIBLE = "PROMOTION_NOT_ELIGIBLE"
BUDGET_EXCEEDED = "PROMOTION_BUDGET_EXCEEDED"
STACK_CONFLICT = "PROMOTION_STACK_CONFLICT"
ALREADY_REDEEMED = "PROMOTION_ALREADY_REDEEMED"

COVERS = {"rest_of_first_month": "by_days", "first_full_month": "none"}     # what is covered -> the proration that goes with it
CUSTOMERS = ("new", "any")                                                   # new: nobody who held a lease here before
TRANSFERS = ("carry", "end")
REQUIRED = ("name", "unit_types", "customers", "covers", "proration", "expires", "stacks_with", "transfer", "max_redemptions", "max_concession_cents")
PUBLISHERS = ("owner", "staff:front_desk", "staff:office_manager", "staff:manager")
RENT_MONTH = re.compile(r"rent:[^:]+:(\d{4}-\d{2})")


class PromotionRefused(Refused):
    """A refusal with its machine name (`code`) and plain words for staff. Nothing was written."""

    def __init__(self, code: str, words: str):
        super().__init__(words)
        self.code = code


class Words(str):
    """A refusal's words that carry their machine name, for the gate's decision record."""
    code: str | None = None

    def __new__(cls, text: str, code: str | None = None):
        s = super().__new__(cls, text)
        s.code = code
        return s


def words(r: PromotionRefused) -> Words:
    return Words(str(r), r.code)


@dataclass
class Version:
    promotion: str
    version: int
    terms: dict
    effective: date
    by: str
    at: datetime


@dataclass
class Redemption:
    """One promotion used on one lease: the version it was made on, the months it covers and what it was expected to give."""
    id: str
    promotion: str
    version: int
    lease: str
    tenant: str
    unit: str
    start: date
    periods: list[str]
    planned_cents: int
    first_full_price: date
    carried_from: str | None
    at: datetime
    state: str = "active"                       # active, ended
    concessions: list[dict] = field(default_factory=list)       # each concession posted: charge, period, cents
    ended: dict | None = None                   # reason, the lease it moved to (if carried), when


class State:
    """The promotion records of one stream, folded event by event."""

    def __init__(self):
        self.versions: dict[str, list[Version]] = {}
        self.redemptions: dict[str, Redemption] = {}

    def apply(self, kind: str, at: datetime, p: dict) -> None:
        if kind == "promotion.version_published":
            self.versions.setdefault(p["promotion"], []).append(Version(p["promotion"], p["version"], dict(p["terms"]), date.fromisoformat(p["effective"]), p["by"], at))
        elif kind == "promotion.redeemed":
            self.redemptions[p["redemption"]] = Redemption(p["redemption"], p["promotion"], p["version"], p["lease"], p["tenant"], p["unit"], date.fromisoformat(p["start"]),
                                                           list(p["periods"]), p["planned_cents"], date.fromisoformat(p["first_full_price"]), p.get("carried_from"), at)
        elif kind == "promotion.concession_posted":
            if (r := self.redemptions.get(p["redemption"])) is not None:
                r.concessions.append({"charge": p["charge"], "period": p["period"], "cents": p["cents"], "at": at})
        elif kind == "promotion.ended":
            r = self.redemptions[p["redemption"]]
            r.state, r.ended = "ended", {"reason": p["reason"], "to": p.get("to"), "at": at}


def state(books: Books) -> State:
    """The books' promotion records; empty when nothing was ever written."""
    return books.promotions or State()


# the versions ---------------------------------------------------------------------------------------------------------------------
def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def terms_problem(t: dict) -> str | None:
    """Why a schedule is not complete or contradicts itself, in words, or None. The same check publishes a version and prices a quote."""
    if not isinstance(t, dict):
        return "the terms are missing"
    for key in REQUIRED:
        if t.get(key) in (None, "", []) and not (key == "stacks_with" and t.get(key) == []):
            return f"the {key.replace('_', ' ')} is missing"
    if not (isinstance(t["name"], str) and t["name"].strip()):
        return "the name is missing"
    if not (isinstance(t["unit_types"], list) and t["unit_types"] and all(isinstance(x, str) and x for x in t["unit_types"])):
        return "the eligible unit types must be a list of sizes"
    if t["customers"] not in CUSTOMERS:
        return f"the customers must be one of: {', '.join(CUSTOMERS)}"
    price, percent = t.get("price_cents"), t.get("percent")
    if (price is None) == (percent is None):
        return "the deal must be either a price or a percent off, not both and not neither"
    if price is not None and not (_is_int(price) and price >= 0):
        return "the deal price must be whole cents, zero or more"
    if percent is not None and not (_is_int(percent) and 1 <= percent <= 100):
        return "the percent off must be a whole number from 1 to 100"
    if t["covers"] not in COVERS:
        return f"the covered period must be one of: {', '.join(COVERS)}"
    if t["proration"] != COVERS[t["covers"]]:
        return f"{t['covers'].replace('_', ' ')} is prorated '{COVERS[t['covers']]}', not '{t['proration']}'"
    try:
        date.fromisoformat(t["expires"])
    except (TypeError, ValueError):
        return "the expiry must be a real date, year-month-day"
    if not (isinstance(t["stacks_with"], list) and all(isinstance(x, str) for x in t["stacks_with"])):
        return "what it stacks with must be a list of promotions, empty for none"
    if t["transfer"] not in TRANSFERS:
        return f"the transfer behaviour must be one of: {', '.join(TRANSFERS)}"
    if not (_is_int(t["max_redemptions"]) and t["max_redemptions"] >= 1):
        return "the redemption limit must be at least 1"
    if not (_is_int(t["max_concession_cents"]) and t["max_concession_cents"] >= 1):
        return "the budget must be at least one cent"
    return None


def publish_version(led: Ledger, promotion: str, terms: dict, effective: date, by: str, at: datetime) -> int:
    """Add the next version of a promotion. The schedule must be complete, and must differ from the latest version; it cannot take effect
    before the version it follows. Redemptions already made keep the version they were made on."""
    if by not in PUBLISHERS:
        raise Refused("only staff or the owner publish a promotion")
    if problem := terms_problem(terms):
        raise PromotionRefused(TERMS_INCOMPLETE, f"the offer's terms are not complete: {problem}")
    versions = state(led.books).versions.get(promotion, [])
    if versions and terms == versions[-1].terms:
        raise Refused("the latest version already has these terms: a new version needs different terms")
    if versions and effective < versions[-1].effective:
        raise Refused(f"a new version cannot take effect before the version it follows ({versions[-1].effective.isoformat()})")
    n = len(versions) + 1
    led.emit("promotion.version_published", at, {"promotion": promotion, "version": n, "terms": dict(terms), "effective": effective.isoformat(), "by": by},
             f"promotion:version:{promotion}:{n}", source=by)
    return n


def version_in_force(books: Books, promotion: str, on: date, version: int | None = None) -> Version:
    versions = state(books).versions.get(promotion, [])
    if version is not None:
        if not 1 <= version <= len(versions):
            raise PromotionRefused(NOT_ELIGIBLE, f"there is no version {version} of the promotion")
        return versions[version - 1]
    live = [v for v in versions if v.effective <= on]
    if not live:
        raise PromotionRefused(NOT_ELIGIBLE, "the promotion is not in force yet" if versions else "there is no such promotion")
    return live[-1]


# the schedule's arithmetic ----------------------------------------------------------------------------------------------------------
def _day(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def _month_name(period: str) -> str:
    y, m = map(int, period.split("-"))
    return f"{calendar.month_name[m]} {y}"


def _next_first(d: date) -> date:
    return date(d.year + d.month // 12, d.month % 12 + 1, 1)


def covered_periods(terms: dict, start: date) -> list[str]:
    """The months a deal covers for a lease starting `start`: the rest of the first month, or the first full month (the month after the
    start unless the lease starts on the 1st, when the first month is already whole)."""
    if terms["covers"] == "rest_of_first_month" or start.day == 1:
        return [month_key(start)]
    return [month_key(_next_first(start))]


def deal(terms: dict, remaining: int) -> int:
    """The cents a deal takes off a period that still owes `remaining`: down to its price, or its percent, half up, never below nothing."""
    if terms.get("price_cents") is not None:
        return max(0, remaining - terms["price_cents"])
    return min(remaining, (2 * remaining * terms["percent"] + 100) // 200)


def _gross(period: str, rent_cents: int, start: date) -> int:
    return first_month(rent_cents, start) if period == month_key(start) else rent_cents


def _price(versions: list[Version], rent_cents: int, start: date, periods: dict[str, list[str]]) -> dict:
    """The quote's promotion part. `periods` maps each version to the months it covers. Several deals on one month apply in the order named,
    each to what the one before left owing, so the total can never pass the gross rent."""
    every = sorted({p for ps in periods.values() for p in ps})
    items = [{"promotion": v.promotion, "version": v.version, "name": v.terms["name"], "concessions": {}} for v in versions]
    rows = []
    for period in every:
        gross = _gross(period, rent_cents, start)
        left = gross
        for v, item in zip(versions, items):
            if period in periods[v.promotion]:
                item["concessions"][period] = c = deal(v.terms, left)
                left -= c
        label = (f"the rest of {_month_name(period)}, from {_day(start)}" if period == month_key(start) and start.day != 1 else _month_name(period))
        rows.append({"period": period, "label": label, "gross_cents": gross, "concession_cents": gross - left, "net_cents": left})
    last = every[-1]
    y, m = map(int, last.split("-"))
    return {"items": items, "periods": rows, "rent_cents": rent_cents, "concession_cents": sum(r["concession_cents"] for r in rows),
            "first_concession_cents": next((r["concession_cents"] for r in rows if r["period"] == month_key(start)), 0),
            "first_full_price": _next_first(date(y, m, 1)).isoformat(), "carried_from": []}


def deal_text(q: dict) -> str:
    """The agreement's lines about the deal: what it takes off each covered month and the first bill at the contract rent. Empty without a deal."""
    out = ""
    for ended in q.get("promotion_ended", []):
        out += f"The deal {ended['name']} on the old unit ends with that unit and does not carry to this one: {ended['reason']}.\n"
    p = q.get("promotion")
    if not p:
        return out
    out += f"Deal: {' and '.join(i['name'] for i in p['items'])}.\n"
    for r in p["periods"]:
        out += f"  {r['label'][0].upper() + r['label'][1:]}: rent {money(r['gross_cents'])} less the deal {money(r['concession_cents'])} is {money(r['net_cents'])}.\n"
    return out + (f"The deal ends after that. The first bill at the full rent is on {_day(date.fromisoformat(p['first_full_price']))}: from then the rent is "
                  f"{money(p['rent_cents'])} a month, as agreed here. That is not a rent increase.\n")


# eligibility ------------------------------------------------------------------------------------------------------------------------
def _claims(books: Books, rent, now: datetime, promotion: str, own: set[str]) -> list[tuple[str, dict]]:
    """The quotes that hold a use of this promotion right now and are not signed yet: a held reservation, or a rental in checkout."""
    quotes = [(x.tenant, x.quote) for x in rent.reservations.values() if x.id not in own and x.state == "held" and rent.holder(x.unit, now) == ("reservation", x.id)]
    quotes += [(r.tenant, r.quote) for r in rent.rentals.values() if r.id not in own and r.kind == "new" and not r.lease and r.quote
               and rent.holder(r.unit, now) == ("rental", r.id)]
    return [(tenant, item) for tenant, q in quotes for item in (q.get("promotion") or {}).get("items", []) if item["promotion"] == promotion]


def price(books: Books, rent, ids: list[str], *, unit: str, tenant: str, start: date, now: datetime, pinned: dict[str, int] | None = None,
          own: set[str] | None = None) -> dict:
    """Check every promotion named for this unit, tenant and start day, and price them: the quote's promotion part. Raises PromotionRefused
    (code and plain words) and writes nothing. `pinned` holds the versions a quote already froze, which are not re-read for expiry; `own` the
    reservation and rental that already hold a use, so a re-check does not count them twice."""
    own, today = own or set(), local(now).date()
    st = state(books)
    if len(set(ids)) != len(ids):
        raise PromotionRefused(STACK_CONFLICT, "the same promotion cannot be used twice on one rental")
    versions = []
    for pid in ids:
        v = version_in_force(books, pid, today, (pinned or {}).get(pid))
        versions.append(v)
        name = v.terms.get("name", pid)
        if problem := terms_problem(v.terms):
            raise PromotionRefused(TERMS_INCOMPLETE, f"{name}: the offer's terms are not complete: {problem}")
    for i, a in enumerate(versions):                        # two deals stack only when each names the other
        for b in versions[i + 1:]:
            if b.promotion not in a.terms["stacks_with"] or a.promotion not in b.terms["stacks_with"]:
                raise PromotionRefused(STACK_CONFLICT, f"{a.terms['name']} and {b.terms['name']} cannot be combined: stacking needs both offers to name each other")
    size = rent.unit(unit).size
    rent_cents = rent.units[unit].rent_cents
    periods = {}
    for v in versions:
        t, name = v.terms, v.terms["name"]
        mine = [r for r in st.redemptions.values() if r.tenant == tenant and r.promotion == v.promotion]
        if mine:
            raise PromotionRefused(ALREADY_REDEEMED, f"{name} was already used by this customer on unit {mine[0].unit}; it can be used once")
        if any(t2 == tenant for t2, _ in _claims(books, rent, now, v.promotion, own)):
            raise PromotionRefused(ALREADY_REDEEMED, f"{name} is already on a rental this customer has in checkout; it can be used once")
        if pinned is None and today > date.fromisoformat(t["expires"]):
            raise PromotionRefused(NOT_ELIGIBLE, f"{name} ended on {_day(date.fromisoformat(t['expires']))}")
        if size not in t["unit_types"]:
            raise PromotionRefused(NOT_ELIGIBLE, f"{name} is for {' and '.join(t['unit_types'])} units; unit {unit} is {size or 'not sized'}")
        if t["customers"] == "new" and any(x.tenant == tenant for x in books.leases.values()):
            raise PromotionRefused(NOT_ELIGIBLE, f"{name} is for new customers; this customer has rented here before")
        periods[v.promotion] = covered_periods(t, start)
    quote = _price(versions, rent_cents, start, periods)
    for v, item in zip(versions, quote["items"]):
        t, total = v.terms, sum(item["concessions"].values())
        if total <= 0:
            raise PromotionRefused(NOT_ELIGIBLE, f"{t['name']} would take nothing off this unit's rent")
        others = [r for r in st.redemptions.values() if r.promotion == v.promotion and r.carried_from is None]
        claims = _claims(books, rent, now, v.promotion, own)
        if len(others) + len(claims) >= t["max_redemptions"]:
            raise PromotionRefused(BUDGET_EXCEEDED, f"{t['name']} has reached its limit of {t['max_redemptions']} redemptions")
        used = sum(r.planned_cents for r in others) + sum(sum(c["concessions"].values()) for _, c in claims)
        if used + total > t["max_concession_cents"]:
            raise PromotionRefused(BUDGET_EXCEEDED, f"{t['name']} has {money(max(0, t['max_concession_cents'] - used))} of its {money(t['max_concession_cents'])} budget left; "
                                                    f"this rental would take {money(total)}")
    return quote


def recheck(books: Books, rent, rental, now: datetime) -> Words | None:
    """The refusal for a rental whose frozen quote names a deal that can no longer be given, or None: the same checks, on the versions the quote
    froze. Run before the renter accepts and before the move-in, so a refusal comes before any money or lease."""
    p = (rental.quote or {}).get("promotion")
    if not p or rental.kind != "new" or rental.lease:
        return None
    own = {rental.id} | ({rental.reservation} if rental.reservation else set())
    try:
        price(books, rent, [i["promotion"] for i in p["items"]], unit=rental.unit, tenant=rental.tenant, start=date.fromisoformat(rental.quote["start"]),
              now=now, pinned={i["promotion"]: i["version"] for i in p["items"]}, own=own)
    except PromotionRefused as r:
        return words(r)
    return None


# redemption and concession ---------------------------------------------------------------------------------------------------------
def redeem(led: Ledger, lease: str, tenant: str, unit: str, promo: dict, start: date, at: datetime) -> list[str]:
    """Record the redemptions a signed quote names, one per promotion, before the lease's first charge posts. The checks were made when the
    quote was priced and again before acceptance; a carried deal is not a new use and counts nowhere twice."""
    out = []
    for item in promo["items"]:
        rid = f"PR{len(state(led.books).redemptions) + 1:04d}"
        periods = sorted(item["concessions"])
        led.emit("promotion.redeemed", at, {"redemption": rid, "promotion": item["promotion"], "version": item["version"], "lease": lease, "tenant": tenant,
                                           "unit": unit, "start": start.isoformat(), "periods": periods, "planned_cents": sum(item["concessions"].values()),
                                           "first_full_price": promo["first_full_price"], "carried_from": item.get("carried_from")},
                 f"promotion:redeem:{rid}", source="code")
        out.append(rid)
    return out


def consumed(books: Books, r: Redemption) -> set[str]:
    """The covered months that already carry this redemption's concession on a charge still standing."""
    return {c["period"] for c in r.concessions if not books.charges[c["charge"]].closed}


def apply_to_charge(led: Ledger, charge: str, at: datetime) -> None:
    """Called as a rent charge posts: each active redemption on the lease that covers the charge's month and has not given it yet posts its
    concession as its own line, on what the charge still owes. Posted again on a charge that replaces a reversed one."""
    st, books = led.books.promotions, led.books
    ch = books.charges[charge]
    m = RENT_MONTH.fullmatch(ch.obligation or "")
    if st is None or m is None:
        return
    left = ch.cents - ch.conceded
    for r in list(st.redemptions.values()):
        if r.lease != ch.lease or r.state != "active" or m[1] not in r.periods or m[1] in consumed(books, r):
            continue
        cents = deal(st.versions[r.promotion][r.version - 1].terms, left)
        if cents:
            led.emit("promotion.concession_posted", at, {"redemption": r.id, "promotion": r.promotion, "charge": charge, "lease": ch.lease, "tenant": ch.tenant,
                                                        "period": m[1], "cents": cents, "gross_cents": ch.cents}, f"promotion:concession:{charge}:{r.id}", source="code")
            left -= cents


def ending_disclosed(books: Books, lease: str, on: date) -> Redemption | None:
    """The redemption whose accepted agreement named `on` as the first bill at the full rent, if the lease has one. Such a bill is the end of
    the deal, never a rent increase."""
    return next((r for r in state(books).redemptions.values() if r.lease == lease and r.first_full_price == on), None)


# transfers --------------------------------------------------------------------------------------------------------------------------
def carry(books: Books, rent, old_lease: str, unit: str, start: date, now: datetime) -> tuple[dict | None, list[dict]]:
    """What a transfer does to the deals on the lease being left: (the promotion part of the new quote, the deals that end). Only a deal whose
    terms say 'carry', whose months are not all given yet and whose unit sizes include the new unit moves; the rest end with the old unit.
    Nothing is checked against budget again: a carried deal is the same use."""
    st, size = state(books), rent.unit(unit).size
    versions, periods, ended, ids = [], {}, [], {}
    for r in st.redemptions.values():
        left = [p for p in r.periods if p not in consumed(books, r)]
        if r.lease != old_lease or r.state != "active" or not left:
            continue
        v = st.versions[r.promotion][r.version - 1]
        why = ("its terms do not follow a transfer" if v.terms["transfer"] != "carry"
               else f"it is for {' and '.join(v.terms['unit_types'])} units and the new unit is {size or 'not sized'}" if size not in v.terms["unit_types"] else None)
        if why:
            ended.append({"promotion": r.promotion, "name": v.terms["name"], "reason": why})
            continue
        versions.append(v)
        periods[v.promotion] = left
        ids[v.promotion] = r.id
    if not versions:
        return None, ended
    quote = _price(versions, rent.units[unit].rent_cents, start, periods)
    for item in quote["items"]:
        item["carried_from"] = ids[item["promotion"]]
    quote["carried_from"] = sorted(ids.values())
    return quote, ended


def settle_transfer(led: Ledger, old_lease: str, new_lease: str, at: datetime) -> None:
    """End every active redemption on the lease left: those the new lease took over say where they went, the rest say the transfer ended them."""
    st = state(led.books)
    carried = {r.carried_from: r.id for r in st.redemptions.values() if r.lease == new_lease}
    for r in [r for r in st.redemptions.values() if r.lease == old_lease and r.state == "active"]:
        led.emit("promotion.ended", at, {"redemption": r.id, "reason": "transfer", "to": new_lease if r.id in carried else None},
                 f"promotion:ended:{r.id}", source="code")
