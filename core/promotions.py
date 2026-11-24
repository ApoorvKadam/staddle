"""First-month deals: the deal, the contract rent and each concession kept as separate records.

A promotion has versions. Each version is an explicit schedule: who and what is eligible, the deal (a price or a percent), exactly which
period it covers, how that period is prorated, when it expires, what it may stack with, whether it follows a transfer, and how many
redemptions and how many cents of concession it may give. A change is a new version; the old ones stay in the history.

A lease keeps its contract rent. A redemption is its own record, made when the lease is signed on a quote that showed the deal and the
first full-price billing date. The concession is a line of its own against the covered period's rent charge: gross rent, concession, net
due and payment are four facts, so the return to the contract rent afterwards is the disclosed end of the deal and never a rent increase.

Events (folded by the books into `Books.promotions`): promotion.version_published, promotion.redeemed, promotion.concession_posted,
promotion.ended. Only promotion.concession_posted moves money, and only as its own journal lines."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .billing import Ledger, Refused
from .books import Books

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


class PromotionRefused(Refused):
    """A refusal with its machine name (`code`) and plain words for staff. Nothing was written."""

    def __init__(self, code: str, words: str):
        super().__init__(words)
        self.code = code


@dataclass
class Version:
    promotion: str
    version: int
    terms: dict
    effective: date
    by: str
    at: datetime


class State:
    """The promotion records of one stream, folded event by event."""

    def __init__(self):
        self.versions: dict[str, list[Version]] = {}

    def apply(self, kind: str, at: datetime, p: dict) -> None:
        if kind == "promotion.version_published":
            self.versions.setdefault(p["promotion"], []).append(Version(p["promotion"], p["version"], dict(p["terms"]), date.fromisoformat(p["effective"]), p["by"], at))


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
