"""The agentic FMS: Northstar Storage's own books (core/) behind a sign-in, the work Tilden (the agent inside it) did on them, and
what waits for a person. The Overview at / is the only public page. Every FMS page and every write needs a signed-in session, and each session
works on its own copy of the books (its own stream), so one visitor's changes never reach the next. The demo name and password
are printed on the sign-in page: the sign-in is real (a signed session cookie, refused when missing or forged), the person is not."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace
import json
import re
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import cached_property
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from core import delinquency as col
from core import leads, protection, reconcile, renting, liens as lien_core, auction, shop, work, walk, walk_reading
from core.case import OFF
from core.books import ADMIN_FEE_CENTS, LATE_FEE_CENTS, PROTECTION, RENT, REVIEW_DAYS, at_local, lines, local, month_key
from core.billing import Ledger, Refused
from core.evidence import KIND_WORDS, clock, day, money, month_words
from core.gate import ACTIONS, RENT_SCOPE, STAFF_ACTIONS, STAFF_RULE, WORK_SCOPE, Command, execute
from core.rules import staff_words, waiver_words
from core.seed import story_month

from . import pricing, spend
from .web import AGENT
from .front_desk import ACTION_WORDS, CLAIM_WORDS, SHIFT, CaseIn, FrontDesk, RuleIn, account_words
from .guard import LIFETIME, SLOW_DOWN, UNSET, Unreachable
from .wording import plain

class WalkInvestigationIn(BaseModel):
    key: str


class WalkOrdersIn(BaseModel):
    unit: str
    texts: list[str]
    token: str


class WalkCompareIn(BaseModel):
    first: str
    second: str


class WalkNoteIn(BaseModel):
    walk: str
    unit: str | None = None
    text: str


def walk_report(report: dict) -> dict:
    """Only checked, cited records and staff-facing words leave the desk."""
    history = report.get("history") or []
    def words(value):
        text = str(value or "")
        for record in history:
            text = text.replace(record["id"], "")
        return plain(text)
    packet = report.get("packet")
    if not packet:
        reason = report.get("dropped") or (report.get("checks") or {}).get("dropped") or "the report could not be read"
        reason = re.sub(r" \([^)]*\)", "", reason).replace("the packet", "the report")      # staff words: no internal names or codes
        return {"dropped": words(reason)}
    cited = set(packet.get("records") or [])
    return {"summary": words(packet["summary"]), "contradiction": words(packet["contradiction"]),
            "step": words(walk_reading.STEPS.get(packet["step"], "Staff decide what to do")),
            "reason": words(walk_reading.REASONS.get(packet["reason"], "A person has to decide")),
            "records": [words(f"{h['kind'].capitalize()}: {h['text']}") for h in history if h["id"] in cited]}


def order_reading(report: dict) -> dict:
    """Tilden's reading of two work orders: only a checked reading and staff-facing words leave the desk."""
    records = report.get("records") or []
    def words(value):
        text = str(value or "")
        for record in records:
            text = text.replace(record["id"], "")
        return plain(text)
    reading = report.get("reading")
    if not reading:
        reason = report.get("dropped") or (report.get("checks") or {}).get("dropped") or "the reading could not be read"
        return {"dropped": words(re.sub(r" \([^)]*\)", "", reason).replace("the answer", "the reading"))}
    return {"verdict": words(walk_reading.VERDICTS.get(reading["verdict"], "The words do not settle it")), "summary": words(reading["summary"])}


DEMO_NAME, DEMO_PASSWORD = "dana.reyes", "northstar"
SESSION = "staddle_session"
STAFF = "staff:office_manager"
# the menu in the groups an FMS is organised by; a module joins its group when it is built, never as a greyed-out item
# the facility menu down the left inside Facilities: the day-to-day facility pages, then money and legal work. Today, Messages and
# Settings (Rules) sit in the bar across the top.
SIDE = ((("home", "Home", "house"), ("sitemap", "Site map", "rows-3"), ("units", "Units", "warehouse"), ("walkthrough", "Walkthrough", "list-checks"), ("shop", "Shop", "shopping-bag"), ("pricing", "Pricing", "tag"), ("rentals", "Rentals", "door-open"), ("leads", "Leads", "users"),
         ("tenants", "Tenants", "users")),
        (("collections", "Collections", "hand"), ("liens", "Lien cases", "gavel"), ("gate", "Gate access", "shield-check"),
         ("billing", "Billing and payments", "credit-card")))
# the only addresses that answer without a session; tests/test_fms.py walks every route against this list. The card processor's
# webhook is the one write among them: the processor calls it, no session exists, its signature is the check, and without a signing
# secret it answers 503 (desk/stripe_hook.py)
PUBLIC = {"/", "/film", "/sign-in", "/sign-out", "/static", "/health/live", "/health/ready", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
          "/webhooks/stripe"}
SCORE = Path(__file__).parent / "static" / "first_shift.json"
STAFF_WORDS = {"remove_disputed_fee": "Removed the fee", "keep_under_review": "Kept under review"}
# renting's staff commands as the Today list words them; an attempt the gate refused reads "Asked to ..."
RENT_WORDS = {"price_unit": ("Priced a unit", "Asked to price a unit"), "block_unit": ("Blocked a unit", "Asked to block a unit"),
              "unblock_unit": ("Unblocked a unit", "Asked to unblock a unit"), "reserve": ("Held a unit", "Asked to hold a unit"),
              "cancel_reservation": ("Cancelled a reservation", "Asked to cancel a reservation"),
              "start_checkout": ("Started a checkout", "Asked to start a checkout"),
              "request_transfer": ("Asked for a transfer", "Asked for a transfer"), "approve_transfer": ("Approved a transfer", "Asked to approve a transfer"),
              "complete_transfer": ("Completed a transfer", "Asked to complete a transfer"),
              "request_move_out": ("Recorded a move-out", "Asked to record a move-out"), "review_move_out": ("Reviewed a move-out", "Asked to review a move-out"),
              "final_bill": ("Made the final bill", "Asked to make the final bill"),
              "confirm_access_cleared": ("Confirmed the old gate code is gone", "Asked to confirm the old gate code is gone"), "offer_unit": ("Offered a unit", "Asked to offer a unit"),
              "decline_offer": ("Declined an offer", "Asked to decline an offer"), "retry_access": ("Retried a gate code", "Asked to retry a gate code"),
              # commands that are not staff's to give still leave a refused record when staff try them
              "join_waitlist": ("Joined a waitlist", "Asked to join a waitlist"), "accept_offer": ("Accepted an offer", "Asked to accept an offer"),
              "accept_agreement": ("Accepted an agreement", "Asked to accept an agreement"),
              "confirm_payment": ("Recorded a payment", "Asked to record a payment"),
              "payment_failed": ("Recorded a failed payment", "Asked to record a failed payment"),
              "activate": ("Started a rental", "Asked to start a rental"),
              "confirm_refund": ("Confirmed a refund", "Asked to confirm a refund")}
MAY_DO = {"explain_charge": "Explain a charge from the records", "reverse_duplicate": "Reverse the second copy of a charge posted twice",
          "correct_fee_posted_in_error": "Remove a late fee posted in error", "open_dispute": "Record a dispute for the office manager",
          "waive_fee": "Waive a late fee under the owner's rule", "hand_to_staff": "Hand a message to staff, with the reason"}
STAFF_DO = {"remove_disputed_fee": "Remove a fee the tenant never signed for", "keep_under_review": "Keep a dispute under review until its date"}
BY = {"billing": "Billing", "operator": AGENT, "fixed_workflow": "Fixed workflow"}
JOB_WORDS = {"understand": "Reading", "operate": "Choosing", "plan reply": "Reading the reply about paying in parts"}
WRONG = "That name and password do not match the demo login shown on this page."
# collections, as an FMS shows it: each state in words with its chip colour
PLAN_WORDS = {"proposed": ("Terms worked out, not yet offered", "plain"), "offered": ("Offered, waiting for the tenant", "plain"),
              "accepted": ("Accepted, waiting for the first payment", "wait"), "active": ("In good standing", "done"),
              "breached": ("Breached, waiting for staff", "act"), "review": ("With staff for a decision", "wait"),
              "completed": ("Completed", "plain"), "cancelled": ("Ended", "plain")}
PART_WORDS = {"scheduled": ("Scheduled", "plain"), "due": ("Due", "wait"), "paid": ("Paid", "done"), "partly paid": ("Partly paid", "wait"),
              "missed": ("Missed", "act"), "uncertain": ("Waiting for the card processor", "wait")}
STEP_WORDS = {"clear": ("Clear", "plain"), "eligible": ("Due", "act"), "blocked": ("Held", "wait"), "waiting for staff": ("Waiting for staff", "act"),
              "recorded": ("Recorded", "plain")}
# the work queue on Today: the signed-in role and person, the four views, and each state in words with its chip colour
ROLE, PERSON = STAFF.split(":", 1)[1], f"person:{DEMO_NAME}"
WORK_VIEWS = (("all", "Facility-wide"), ("mine", "My work"), ("waiting", "Waiting"), ("done", "Done today"))
WORK_STATES = {"open": ("Open", "plain"), "assigned": ("Assigned", "plain"), "in progress": ("In progress", "wait"),
               "waiting for evidence": ("Waiting for evidence", "wait"), "blocked": ("Blocked by a hold", "wait"),
               "reopened": ("Reopened: the problem came back", "act"), "done": ("Done", "done"), "cancelled": ("Cancelled", "plain")}
WORK_ANCHORS = {"collections": "collections", "overlock": "overlocks", "reservation": "rentals", "rental": "rentals", "refund": "rentals",
                "access": "codes", "turnover": "turnover", "walk": "walk", "maintenance": "maintenance",
                "protection": "protection", "lien": "lien", "lien stop": "lien", "lead": "leads", "shop": "shop",
                "street_rate": "pricing"}       # where Home's links land: the first item of each kind
WORK_FORMS = {"acknowledge": "acknowledge_work", "start": "start_work", "assign": "assign_work", "note": "note_work", "finish": "finish_work"}
SCOPE_WORDS = {"overlock": "overlock", "lien": "lien eligibility"}
# the tenant's own policy and the facility's plan on the tenant's page: each state in words with its chip colour
POLICY_WORDS = {"reported": ("The tenant's words only, no document", "wait"), "received": ("Document received, not reviewed", "wait"),
                "needs review": ("Needs review", "act"), "reviewed": ("Reviewed by staff", "done"), "rejected": ("Rejected", "plain"),
                "expired": ("Expired", "act")}
PLAN_STATE_WORDS = {"offered": ("Offered, waiting for the tenant's yes", "wait"), "accepted": ("Accepted, not active yet", "wait"),
                    "active": ("Active", "done"), "ended": ("Ended", "plain")}
PROTECTION_FORMS = ("record_policy", "review_policy", "offer_plan", "record_acceptance", "enroll_plan", "end_plan")
# protection's staff commands as the Today list words them; an attempt the gate refused reads "Asked to ..."
PROTECTION_WORDS = {"record_policy_evidence": ("Recorded a policy", "Asked to record a policy"),
                    "verify_policy": ("Reviewed a policy document", "Asked to review a policy document"),
                    "offer_plan": ("Offered the protection plan", "Asked to offer the protection plan"),
                    "record_acceptance": ("Recorded the tenant's acceptance of the plan", "Asked to record the tenant's acceptance of the plan"),
                    "enroll_plan": ("Enrolled in the protection plan", "Asked to enrol in the protection plan"),
                    "end_plan": ("Ended a protection plan", "Asked to end a protection plan"),
                    "publish_plan": ("Published a protection plan version", "Asked to publish a protection plan version"),
                    "post_plan_charge": ("Posted a protection plan charge", "Asked to post a protection plan charge"),
                    "collect_charge": ("Approved collecting a protection plan charge", "Asked to collect a protection plan charge"),
                    "approve_statement": ("Approved a statement", "Asked to approve a statement")}
STEP_FORMS = ("overlock", "lift_overlock", "review_plan", "grant_exemption", "revoke_exemption", "take_payment")
UNIT_FORMS = ("price_unit", "reserve", "block_unit", "unblock_unit")       # what staff do to a unit on the Units page
PAYMENT_WORDS = {"open": ("Waiting for the card processor", "wait"), "pending": ("Waiting for the card processor", "wait"), "succeeded": ("Paid", "done"),
                 "failed": ("Declined", "act"), "returned": ("Returned by the bank", "act"), "closed": ("Not taken", "plain")}
# a rental's payment and its gate code on the tenant's page: (words, chip tone)
RENTAL_PAY = {"paid": ("Paid", "done"), "waiting": ("Waiting for the card processor", "wait"), "returned": ("Returned by the bank", "act"),
              "declined": ("Card declined", "act"), "unpaid": ("Not paid yet", "wait")}
GATE_WORDS = {"verified": ("Code set and read back", "done"), "failed": ("Access pending: the code could not be read back", "act"),
              "requested": ("Access pending", "wait"), "issuing": ("Access pending", "wait"), "issued_unverified": ("Access pending", "wait"),
              "revocation_pending": ("Old code being revoked", "wait"), "revocation_failed": ("Old code still standing after the revoke", "act"),
              "revoked": ("Code revoked", "plain")}
# the owner's settings as a form: field, label, unit; dollars are typed as whole dollars and kept in cents
POLICY_FORM = (("min_tenure_days", "Rented at least", "days"), ("max_debt_cents", "Balance at most", "dollars"),
               ("longest_days", "Longest plan", "days"), ("most_instalments", "Payments after the first", "payments"),
               ("min_upfront_cents", "Smallest first payment", "dollars"), ("min_instalment_cents", "Smallest later payment", "dollars"),
               ("grace_days", "Grace after each due date", "days"))
SCHEDULE_FORM = (("overlock_days", "Overlock", "days"), ("lien_days", "Lien eligibility", "days"))


# the shop: each stock move kind in words, and each payment state; a Today item of the shop lands on the part of the Shop page that closes it
SHOP_MOVES = {"received": "Received", "sold": "Sold at the counter", "return": "Put back after a return", "damage": "Damaged",
              "give-away": "Given away", "count variance": "Count difference accepted"}
SHOP_PAYMENTS = {"open": ("Not paid yet", "wait"), "processing": ("Waiting for the card processor", "wait"), "succeeded": ("Paid", "done"),
                 "failed": ("Card declined", "act"), "returned": ("Returned by the bank", "act")}
SHOP_LINKS = {"stock count variance": "/shop#counts", "shop payment needs review": "/shop#sales", "shop reconciliation": "/shop#records",
              "shop refund owed": "/shop#sales"}
SHOP_CARDS = (("card_ok", "Test card that is accepted"), ("card_declined", "Test card that declines"))
SHOP_FORMS = {"change_price": "products", "add_product": "products", "expect_delivery": "deliveries", "receive_delivery": "deliveries",
              "post_receipt": "deliveries", "give_away": "giveaways", "post_give_away": "giveaways", "record_count": "counts",
              "review_count": "counts", "ask_recount": "counts", "post_difference": "counts", "sell": "counter", "post_sale": "sales",
              "record_return": "returns", "post_return": "returns", "retry_payment": "sales", "cancel_sale": "sales",
              "write_off_payment": "sales", "resume_payment": "sales", "refund_payment": "sales"}
# a reconciliation sentence names records by their id; a person reads the kind of record only
_SHOP_ID = re.compile(r"^(Receipt|Handover|Return|Sale|Count|Stock movement) \S+|^Payment for \S+")


def shop_words(text: str) -> str:
    return "; ".join(_SHOP_ID.sub(lambda m: m.group(1) or "A payment", part) for part in text.split("; "))


def upfirst(text: str) -> str:
    return text[:1].upper() + text[1:]


def staff_name(source: str) -> str:
    """A recorded source as a person reads it: staff:office_manager is the Office manager."""
    return upfirst(source.removeprefix("staff:").replace("_", " "))


def bounded(data: dict, fields, bounds: dict) -> tuple[dict | None, str]:
    """A settings form read field by field into whole numbers inside the fixed limits; dollars become cents. Returns (values, "")
    or (None, why not, in the form's own words)."""
    out = {}
    for key, label, unit in fields:
        lo, hi = bounds[key]
        if unit == "dollars":
            lo, hi = -(-lo // 100), hi // 100
        raw = (data.get(key) or "").strip()
        if not raw.isdecimal() or not lo <= int(raw) <= hi:
            return None, f"{label} must be a whole number from {lo:,} to {hi:,} {unit}"
        out[key] = int(raw) * (100 if unit == "dollars" else 1)
    return out, ""


def dollars(text: str) -> int | None:
    """An amount typed in dollars and cents, to whole cents without floats; None when it is not one."""
    m = re.fullmatch(r"\$?(\d{1,7})(?:\.(\d{1,2}))?", text.strip().replace(",", ""))
    return int(m[1]) * 100 + int((m[2] or "0").ljust(2, "0")) if m else None


def slug(name: str, unit: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", f"{name} {unit}".lower()).strip("-")


def phone(p: str) -> str:
    d = re.sub(r"\D", "", p)[-10:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else p


def date_words(d: date) -> str:
    return f"{d:%a}, {d:%b} {d.day}, {d.year}"


def dated(d: date, year: int) -> str:
    """A day, with its year when it is not this year: a ledger runs back past January."""
    return day(d) if d.year == year else f"{day(d)}, {d.year}"


def words(key: str, value) -> tuple[str, str]:
    """One field of a typed rule as words: cents as dollars, flags as yes or no, codes without underscores."""
    if key.endswith("_cents"):
        return key.removesuffix("_cents").replace("_", " "), money(value)
    if isinstance(value, bool):
        return key.replace("_", " "), "yes" if value else "no"
    return key.replace("_", " "), str(value).replace("_", " ")


class Facility:
    """One session's view of the books: the folded state, the session's own events, and every tenant with their units and money."""

    def __init__(self, fd: FrontDesk, ident: str, everything: bool = False):
        self.b, self.own, self.balanced, self.debits, self.events, self.merchandise = fd.view(ident, everything)
        self.fd, self.today, self.now, self.ident = fd, fd.day, fd.clock(), ident
        b = self.b
        self.leases_of: dict[str, list] = {}
        for lease in sorted(b.leases.values(), key=lambda x: (x.ended is not None, x.unit)):
            self.leases_of.setdefault(lease.tenant, []).append(lease)
        last_paid: dict[str, object] = {}
        for p in b.payments.values():
            if not p.returned and (p.tenant not in last_paid or p.effective_at > last_paid[p.tenant].effective_at):
                last_paid[p.tenant] = p
        self.people: dict[str, dict] = {}
        for t in b.tenants.values():
            ls = self.leases_of.get(t.id, [])
            active = [x for x in ls if x.ended is None]
            self.people[t.id] = {"id": t.id, "name": t.name, "phone": phone(t.phone), "slug": slug(t.name, min(ls, key=lambda x: (x.start, x.id)).unit if ls else "none"),     # the first lease's unit: a transfer never moves the address
                                 "units": [x.unit for x in (active or ls)], "active": bool(active), "past": b.past_due(t.id, self.now),
                                 "owed": sum(b.receivable(x.id) for x in ls) - b.credit.get(t.id, 0), "credit": b.credit.get(t.id, 0),
                                 "dispute": any(d.tenant == t.id and d.outcome is None for d in b.disputes.values()),
                                 "since": f"{day(min(x.start for x in (active or ls)))}, {min(x.start for x in (active or ls)).year}" if ls else "",
                                 "rent": sum(x.rent_cents for x in active),
                                 "paid": (f"{money(last_paid[t.id].cents)} on {day(last_paid[t.id].effective_at)}" if t.id in last_paid else "")}
        self.by_slug = {p["slug"]: p for p in self.people.values()}
        self.role_of = {c["tenant"]: role for role, c in fd.cast.items()}

    @property
    def name(self) -> str:
        return self.b.facility.get("name", "")

    @staticmethod
    def chips(p: dict) -> list[tuple[str, str]]:
        out = []
        if not p["active"]:
            out.append(("Moved out", "plain"))
        if p["past"]:
            out.append((f"Past due {money(p['past'])}", "act"))
        if p["dispute"]:
            out.append(("Fee in dispute", "wait"))
        if p["credit"]:
            out.append((f"Credit {money(p['credit'])}", "plain"))
        return out

    # the work ---------------------------------------------------------------------------------------------------------
    def cases(self) -> list[dict]:
        """Every message worked on this copy, newest first, redrawn from what was recorded: the words, what was read, what the
        records showed, what was done, the books entries and the reply."""
        b, out = self.b, []
        for e in self.own:
            if e.kind not in ("case.closed", "case.handed_over"):
                continue
            p, case = e.payload, e.payload["case"]
            who = self.people.get(p["tenant"] or "")
            claims = []
            for c in p["claims"]:
                if p.get("plan_read") and c["kind"] == "other":       # the plan reading says it better
                    continue
                ch = b.charges.get(c.get("charge") or "")
                about = None
                if ch is not None:
                    about = f"{KIND_WORDS.get(ch.kind, ch.kind)} due {day(ch.due)}" + (f", unit {b.leases[ch.lease].unit}" if who and len(who["units"]) > 1 else "")
                claims.append({"what": CLAIM_WORDS.get(c["kind"], c["kind"]), "quote": c["quote"], "about": about})
            outcomes = [{**o, "words": ACTION_WORDS.get(o["action"], o["action"])} for o in p.get("outcomes", [])]
            entries = [{"account": account_words(a, b), "debit": money(c) if c > 0 else "", "credit": money(-c) if c < 0 else ""}
                       for x in self.own if x.command and x.command.startswith(case + ":") for a, c, _ in lines(x.kind, x.payload)]
            shift = case.rsplit(":", 1)[-1]
            def named(o: dict) -> str:
                ch = b.charges.get(o.get("charge") or "")
                return f"{KIND_WORDS.get(ch.kind, ch.kind).capitalize()} due {day(ch.due)}: {o['words'][:1].lower()}{o['words'][1:]}" if ch else o["words"]
            done = list(dict.fromkeys(named(o) for o in outcomes if o["allowed"] and o["action"] != "hand_to_staff"))
            # why it went to staff in the records' own words when a claim says so, else the case's reason
            why = next((o["says"] for o in outcomes if o["action"] == "hand_to_staff" and o["says"]), None)
            if any(o.get("by") in ("owner rule", "staff rule") for o in outcomes):      # a rule sent it: say which, not the records
                why = p["handed"]
            out.append({"n": e.id, "who": who, "text": p["text"], "handed": p["handed"], "why": why or p["handed"], "reply": p["reply"], "claims": claims, "plan_read": p.get("plan_read"),
                        "outcomes": outcomes, "asked": [a for a in p.get("asked", []) if not a["allowed"]], "entries": entries,
                        "summary": ". ".join(done) + "." if done else "Answered from the records.",
                        # a message can be answered while the work it started still waits: a dispute stays open for staff
                        "waits": bool(who and who["dispute"] and any(o["action"] == "open_dispute" and o["allowed"] for o in outcomes)),
                        "cents": round(sum(c.get("cents", 0) for c in p.get("calls", [])), 4),
                        # what each model call cost, as the Overview shows it
                        "calls": [{"job": JOB_WORDS.get(c["job"], "Model call"), "tokens_in": c["tokens_in"], "tokens_out": c["tokens_out"],
                                   "cents": c["cents"], "seconds": c["ms"] / 1000, "ok": c["outcome"] == "ok"} for c in p.get("calls", [])],
                        "off": p["handed"] == OFF,
                        "shift": shift if any(i["id"] == shift for i in SHIFT) else None})
        return out[::-1]

    def waiting(self, cases: list[dict]) -> list[dict]:
        """This morning's messages that this copy has not worked yet."""
        done = {c["shift"] for c in cases if c["shift"]}
        out = []
        for t in self.fd.tenants():
            if t["shift"] not in done:
                out.append({**t, "who": self.people[self.fd.cast[t["role"]]["tenant"]]})
        return out

    def disputes(self) -> list[dict]:
        """Open disputes, with what staff need to decide: the fee, who posted it, the tenant's words, the schedule they signed,
        whether collection is paused, and the account before and after removing it."""
        b, out = self.b, []
        mine = {e.payload["dispute"] for e in self.own if e.kind == "dispute.opened"}      # recorded on this copy, by the agent
        for d in sorted(b.disputes.values(), key=lambda x: (x.review_by, x.id)):
            if d.outcome is not None:
                continue
            ch = b.charges[d.charge]
            lease = b.leases[ch.lease]
            who = self.people[d.tenant]
            out.append({"id": d.id, "who": who, "fee": KIND_WORDS.get(ch.kind, ch.kind), "amount": money(ch.cents), "unit": lease.unit,
                        "posted": day(ch.posted_at), "by_hand": ch.source.startswith("staff"), "quote": d.quote, "review_by": day(d.review_by),
                        "recorded": f"{'today' if local(d.opened_at).date() == self.today else day(d.opened_at)} by {AGENT if d.id in mine else 'staff at the desk'}",
                        "held": b.held(ch.id), "in_schedule": ch.kind in lease.schedule, "signed": f"{day(lease.start)}, {lease.start.year}",
                        "schedule": ", ".join(KIND_WORDS.get(k, k) for k in lease.schedule) or "no fees",
                        "owed_now": money(who["owed"]), "owed_after": money(who["owed"] - ch.open_cents)})
        return out

    def rent_sentence(self, p: dict) -> str:
        """What a staff renting step did, or was refused, from what the gate recorded: a unit is named only when the step is about one."""
        action, args, who = p["action"], p.get("args") or {}, self.people.get(p.get("tenant") or "")
        unit, asked = (p["about"] if p["about"] in self.b.units else None), not p["allowed"]
        if action == "price_unit" and unit and isinstance(args.get("rent_cents"), int):
            return f"{'Asked to set' if asked else 'Set'} the street rate of unit {unit} to {money(args['rent_cents'])}"
        text = RENT_WORDS[action][1 if asked else 0]
        if unit:
            text = text.replace("a unit", f"unit {unit}")
        if action == "block_unit" and args.get("reason"):
            text += f": {args['reason']}"
        if who:
            text += f" for {who['name']}"
        if action == "reserve" and args.get("until"):
            try:
                text += f" until {day(datetime.fromisoformat(args['until']))}"
            except ValueError:
                pass
        return plain(text)

    def protection_sentence(self, p: dict) -> str:
        """What a staff protection step did, or was refused, as one sentence naming the tenant when the lease, plan, policy or charge it was about
        belongs to one."""
        st, b = protection.state(self.b), self.b
        about = p.get("about") or ""
        record = st.enrolments.get(about) or st.evidence.get(about) or b.charges.get(about)
        lease = b.leases.get(p.get("lease") or "") or b.leases.get(getattr(record, "lease", None) or "")
        who = self.people.get(lease.tenant) if lease else None
        text = PROTECTION_WORDS[p["action"]][0 if p["allowed"] else 1]
        return plain(text + (f" for {who['name']}" if who else ""))

    def decisions(self) -> list[dict]:
        out = []
        for e in self.own:
            if e.kind == "gate.decided" and e.source.startswith("staff:") and e.payload["action"] not in WORK_SCOPE:    # work steps live in the queue
                p = e.payload
                if p["action"] in shop.ACTIONS:       # shop steps are listed on the Shop page with their records
                    continue
                if p["action"] in RENT_SCOPE:       # a renting step: no fee, no note, and often no tenant (pricing a unit); what was done is said from what was recorded
                    out.append({"who": self.people.get(p.get("tenant") or ""), "words": RENT_WORDS[p["action"]][0 if p["allowed"] else 1], "allowed": p["allowed"],
                                "removed": False, "check": p["check"], "reason": None, "fee": "", "renting": True, "text": self.rent_sentence(p)})
                    continue
                if p["action"] in PROTECTION_WORDS:     # a protection step: said as one sentence, with the tenant it is about when the records name one
                    out.append({"who": None, "words": "", "allowed": p["allowed"], "removed": False, "check": plain(p["check"]), "reason": None, "fee": "", "renting": True,
                                "text": self.protection_sentence(p)})
                    continue
                if (p.get("command") or "").startswith("lead-form:"):
                    lead = leads.state(self.b).records.get(p.get("about") or "")
                    name = lead.name if lead else "an inquiry"
                    out.append({"who": None, "words": "", "allowed": p["allowed"], "removed": False, "check": plain(p["check"]), "reason": None, "fee": "", "renting": True,
                                "text": plain(("Updated " if p["allowed"] else "Asked to update ") + name)})
                    continue
                ch = self.b.charges.get(p.get("charge") or "")
                out.append({"who": self.people.get(p.get("tenant") or ""), "words": STAFF_WORDS.get(p["action"], p["action"]), "allowed": p["allowed"],
                            "removed": p["allowed"] and p["action"] == "remove_disputed_fee",
                            "check": p["check"], "reason": p.get("reason"), "fee": f"{money(ch.cents)} {KIND_WORDS.get(ch.kind, ch.kind)}" if ch else ""})
        return out[::-1]

    # the facility -----------------------------------------------------------------------------------------------------
    def occupancy(self) -> list[dict]:
        rented = {x.unit for x in self.b.leases.values() if x.ended is None}
        rows = sorted({u[0] for u in self.b.units})
        return [{"row": r, "units": sum(1 for u in self.b.units if u[0] == r), "rented": sum(1 for u in self.b.units if u[0] == r and u in rented)}
                for r in rows]

    def month(self, key: str) -> dict:
        b = self.b
        charges = [c for c in b.charges.values() if month_key(c.due) == key]
        pays = sorted((p for p in b.payments.values() if month_key(local(p.effective_at).date()) == key), key=lambda p: p.effective_at, reverse=True)
        declined = [x for x in b.declined if month_key(local(x["at"]).date()) == key]
        rows = []
        for p in pays:
            kinds = sorted({KIND_WORDS.get(b.charges[a["charge"]].kind, "") for a in p.applications})
            rows.append({"who": self.people[p.tenant], "amount": money(p.cents), "on": day(p.effective_at), "returned": p.returned,
                         "learned": day(p.known_at) if local(p.known_at).date() != local(p.effective_at).date() else None,
                         "to": " and ".join(kinds) + (" and credit" if p.credit_cents else "") if kinds else ("credit" if p.credit_cents else "")})
        # the month's charges add up: every cent billed is paid, removed (reversed, waived or conceded by a deal) or still open
        return {"key": key, "name": f"{month_words(key)} {key[:4]}",
                "rent": sum(c.cents for c in charges if c.kind == RENT),
                "fees": sum(c.cents for c in charges if c.kind != RENT),
                "paid": sum(c.paid for c in charges),
                "removed": sum(c.cents - c.paid if c.closed else c.conceded for c in charges),
                "collected": sum(p.cents for p in pays if not p.returned),
                "returned": sum(1 for p in pays if p.returned),
                "declined": [{"who": self.people[x["tenant"]], "on": day(x["at"]), "amount": money(x["cents"])} for x in declined],
                "open": sum(c.open_cents for c in charges), "payments": rows}

    def months(self) -> list[tuple[str, str]]:
        keys = sorted({month_key(c.due) for c in self.b.charges.values() if c.due <= self.today}, reverse=True)
        return [(k, f"{month_words(k)} {k[:4]}") for k in keys]

    def past_due(self) -> list[dict]:
        rows = []
        for p in self.people.values():
            if p["past"]:
                oldest = min((c.due for c in self.b.open_charges(p["id"]) if c.due < self.today), default=self.today)
                rows.append({**p, "oldest": day(oldest), "days": (self.today - oldest).days, **self.standing(p["id"])})
        return sorted(rows, key=lambda r: (-r["days"], -r["past"]))

    # renting ----------------------------------------------------------------------------------------------------------------
    @cached_property
    def rent(self) -> renting.Rent:
        """This copy's renting state as of this page load; read only (rent.units.get, never rent.unit, which would add a record)."""
        return self.fd.rent_view(self.ident)

    def pay_state(self, r) -> str:
        return work.pay_state(self.rent, r)

    def move_in(self, r) -> date | None:
        return work.move_in(self.rent, r, self.now)

    def stage(self, r) -> str:
        return work.stage(self.rent, r, self.now)

    def held(self, unit: str) -> tuple[str, dict | None]:
        """What holds a unit now, in a sentence, and the person it is held for (None for a block or when nothing holds it)."""
        rent = self.rent
        h = rent.holder(unit, self.now)
        if h is None:
            return "", None
        kind, rid = h
        if kind == "block":
            return f"Blocked: {rent.blocks[rid].reason}", None
        if kind == "reservation":
            r = rent.reservations[rid]
            return f"Held for {self.people[r.tenant]['name']} until {clock(r.expires_at)}", self.people[r.tenant]
        if kind == "offer":
            e = rent.entries[rid]
            return f"Offered to {self.people[e.tenant]['name']} from the waitlist until {clock(e.expires_at)}", self.people[e.tenant]
        r = rent.rentals[rid]
        who = self.people[r.tenant]
        if r.state == "ended":
            return "Moved out; the old gate code is not revoked yet", who
        if r.state in renting.WAITING:
            stage = self.stage(r)
            return f"{who['name']} is renting it: {stage[:1].lower()}{stage[1:]}", who
        return "", who

    def renting_of(self, tenant: str) -> list[dict]:
        """A tenant's reservations and rentals, newest first, each step in words: where it stands, the payment, the agreement with its
        template version, the lease and the gate code, which counts as working only once it was read back. A rental that is over or whose
        hold ran out says what happened, not what is still to do."""
        rent, b, now, year, rows = self.rent, self.b, self.now, self.today.year, []
        rented = {r.reservation for r in rent.rentals.values() if r.reservation}
        for r in rent.reservations.values():
            if r.tenant == tenant and r.id not in rented:
                live = rent.holder(r.unit, now) == ("reservation", r.id)
                where = ((f"Reserved until {clock(r.expires_at)}", "wait") if live else ("Hold ended", "plain") if r.state in ("held", "expired") else ("Cancelled", "plain"))
                rows.append({"unit": r.unit, "where": (*where, ""), "pay": None, "agreement": None, "lease": None, "gate": None})
        for r in rent.rentals.values():
            if r.tenant != tenant:
                continue
            holds = r.state in renting.WAITING and rent.holder(r.unit, now) == ("rental", r.id)
            live = holds or r.state in ("active", "ending")
            m = rent.moveouts.get(r.moveout or "")
            if holds and self.move_in(r):
                where = ("Waiting for the move-in day", "wait", f"Starts {dated(self.move_in(r), year)}.")
            elif holds:
                where = ("Waiting to start", "wait", f"Hold ends {clock(r.expires_at)}." if now < r.expires_at else
                         f"The hold time ended {clock(r.expires_at)}; the unit stays held while a payment is waiting or paid for.")
            else:
                where = {"active": ("Active", "done", ""), "ended": ("Ended", "plain", ""), "cancelled": ("Cancelled", "plain", f"Cancelled because {r.cancelled_why}." if r.cancelled_why else ""),
                         "ending": ("Moving out" if m else "Moving to another unit", "wait", f"Last day {dated(m.on, year)}." if m else "")
                         }.get(r.state) or ("Hold ended", "plain", f"The hold ended {clock(r.expires_at)}.")
            if r.state == "ended" and renting.needs_clearance(r):
                where = ("Left, gate code check waiting", "wait", "Staff check the gate system before the unit is rented again.")
            note = [where[2]] if where[2] else []
            if r.kind == "transfer" and r.state in renting.WAITING:
                note.append("This is the new unit of a transfer.")
            # a refund keeps its own task (keyed), so one rental can owe several: the one set aside when it was cancelled, the credit left
            # by its final bill, and a payment that came late. Each reads waiting until the processor confirms it, then made.
            asked = [t for t in rent.tasks.values() if t.open and t.kind == "refund" and t.about == r.id]
            sums = [(r.refund["cents"], r.refund["state"], f"refund:{r.id}")] if r.refund else []
            if m and m.bill and m.bill.get("refund_cents"):
                sums.append((m.bill["refund_cents"], m.bill["refund"], f"refund:{m.id}"))
            for cents, state_of, key in sums:
                note.append(f"Refund of {money(cents)} {'was made' if state_of == 'paid' else 'waiting for staff'}.")
            if any(t.key not in {k for *_, k in sums} for t in asked):
                note.append("A refund is waiting for staff.")
            lease = b.leases.get(r.lease or "")
            if r.provenance and lease and r.state == "active":
                where = (f"Active since {dated(lease.start, year)}", "done", "")
            state = self.pay_state(r)
            refund = r.state == "cancelled" and ((r.refund or {}).get("state") == "pending" or asked)
            made = r.state == "cancelled" and (r.refund or {}).get("state") == "paid" and not asked
            pay = (("Refund waiting", "act") if refund else ("Refunded", "plain") if made else RENTAL_PAY[state] if live or state not in ("unpaid", "declined") else
                   ("Not paid" if state == "unpaid" else "Card declined", "plain"))
            ag = rent.agreements.get(r.agreement or "")
            version = f"Agreement version {ag.version.rsplit('-', 1)[-1]}" if ag else ""
            agreement = ((f"Accepted by {self.people[r.tenant]['name']}, {clock(ag.at)}", "done", version) if r.accepted and ag and ag.at else
                         ("Not accepted yet", "wait", version) if live else ("Not accepted", "plain", version))
            if r.provenance:
                pay = ("See ledger", "plain")
                agreement = ("From before Staddle: terms on file, no signed copy", "plain", "")
            a = rent.access.get(r.access or "")
            gate = GATE_WORDS[a.state] if a else ("No gate code on record yet", "plain") if r.state in ("active", "ending") else None
            rows.append({"id": r.id, "unit": r.unit, "where": (where[0], where[1], " ".join(note)), "pay": (*pay, f"Move-in total {money(r.quote['total_cents'])}" if r.quote else f"Monthly rent {money(lease.rent_cents)}"),
                         "agreement": agreement, "gate": (*gate, "") if gate else None,
                         "rental": r, "moveout": m, "transfer": next((t for t in rent.transfers.values() if t.rental == r.id and t.state not in ("completed", "cancelled")), None),
                         "lease": (f"Ended {dated(lease.ended, year)}" if lease.ended else f"Started {dated(lease.start, year)}") if lease else None})
        return rows[::-1]

    @cached_property
    def queue(self) -> list[work.Item]:
        """Every item of staff work on this copy as the records stand now: what the work record kept of it, and what the sources say."""
        wk = work.State()
        for e in self.own:
            wk.apply(e.kind, e.at, e.payload)
        return work.queue(self.b, self.rent, wk, self.now)

    def worked(self, view: str) -> tuple[list[dict], dict[str, int]]:
        """The queue's items for one view, each in words with its links, and how many items each view holds."""
        items = work.open_items(self.queue)
        views = {"all": items, "mine": work.mine(items, ROLE, PERSON), "waiting": [i for i in items if i.state in ("waiting for evidence", "blocked")],
                 "done": [i for i in self.queue if i.state in work.CLOSED and local(i.closed_at).date() == self.today]}

        def who(s: str | None) -> str | None:
            if not s:
                return None
            s = s.removeprefix("staff:")
            return s.removeprefix("person:").replace(".", " ").title() if s.startswith("person:") else upfirst(s.replace("_", " "))
        seen, rows = set(), []
        for i in views[view]:
            text, tone = WORK_STATES[i.state]
            anchor = WORK_ANCHORS[i.source]
            rows.append({"i": i, "anchor": anchor if anchor not in seen else None, "who": self.people.get(i.tenant or ""), "since": dated(i.due, self.today.year), "upcoming": bool(i.due and i.due > self.today),
                         "unit": i.unit if i.source != "collections" else None, "state": f"{text} to {who(i.assignee)}" if i.state == "assigned" else text,
                         "tone": tone, "why": shop_words(plain(i.why)) if i.source == "shop" else plain(i.why), "link": SHOP_LINKS.get(i.kind) if i.source == "shop" else "/pricing" if i.source == "street_rate" else None,
                         "link_label": "Open it in Pricing" if i.source == "street_rate" else "Open it in Shop", "owner": who(i.assignee or i.role), "closes": plain(i.closes), "evidence": plain(i.evidence),
                         "seen": who(i.acknowledged[0]) if i.acknowledged else None,
                         "notes": [(clock(at), who(by), t) for at, by, t in i.notes],
                         "lead": leads.state(self.b).records.get(i.ref) if i.source == "lead" else None,
                         "section": "lien" if i.source in ("lien", "lien stop") else {"lead": "leads", "collections": "collections", "overlock": "collections", "protection": "protection", "shop": "shop"}.get(i.source, "renting"),
                         "lift": i.kind == "lift overlock"})
            seen.add(anchor)
        return rows, {k: len(v) for k, v in views.items()}

    def shop_page(self) -> dict:
        """What the Shop page reads: each product with its stock and moves, deliveries, what waits to be posted, counts, sales and the
        merchandise books, all from this copy's shop records."""
        st = shop.state(self.b)
        moves_of: dict[str, list] = {}
        for m in sorted(st.moves, key=lambda m: m["at"], reverse=True):
            moves_of.setdefault(m["product"], []).append({"at": clock(m["at"]), "kind": SHOP_MOVES.get(m["kind"], m["kind"]), "quantity": m["quantity"], "reason": plain(m["reason"])})
        products = []
        for name, versions in st.versions.items():
            v = versions[-1]
            products.append({"name": name, "price": v["price"], "cost": v["cost"], "tax_class": v["tax_class"], "active": v["active"], "on_hand": st.quantity(name),
                             "held": st.quantity(name) - st.available(name), "moves": moves_of.get(name, []),
                             "versions": [{"at": clock(x["at"]), "price": x["price"], "cost": x["cost"], "active": x["active"]} for x in reversed(versions)]})
        received = {}
        for r in st.receipts.values():
            received[r["delivery"]] = received.get(r["delivery"], 0) + r["quantity"]
        deliveries = [{"id": d["id"], "product": d["product"], "quantity": d["quantity"], "got": received.get(d["id"], 0), "left": d["quantity"] - received.get(d["id"], 0),
                       "reason": plain(d["reason"]), "at": clock(d["at"])} for d in st.deliveries.values()]
        posted = lambda kind: {m.get("reference") for m in st.moves if m["kind"] == kind}
        unposted_receipts = [{"id": r["id"], "product": r["product"], "quantity": r["quantity"], "reason": plain(r["reason"])}
                             for r in st.receipts.values() if r["id"] not in posted("received")]
        handovers = [{"id": h["id"], "product": h["product"], "quantity": h["quantity"], "reason": plain(h["reason"]), "approval": plain(h["approval"]), "what": plain(h["handover"]),
                      "posted": h["id"] in posted("give-away")} for h in st.handovers.values()]
        counts = []
        for c in sorted(st.counts.values(), key=lambda c: c["at"], reverse=True):
            moved = any(m["kind"] == "count variance" and m.get("reference") == c["id"] for m in st.moves)
            review = c.get("review")
            recount = bool(review and review.get("recount"))
            if moved:
                state = ("Reviewed, difference posted to stock", "done")
            elif recount and st.superseded(c["id"]):
                state = ("Recount asked for; a newer count stands in for this one", "plain")
            elif recount:
                state = ("Recount asked for. Waiting for the new count", "wait")
            elif not c["variance"]:
                state = ("The count matches the books", "done")
            elif review:
                state = ("Reviewed, difference not posted yet", "act")
            else:
                state = ("Waiting for review", "act")
            # a button is offered only when the core would take the step: the same check the gate runs
            refusal = st.count_variance_refusal(c["product"], c["variance"], c, reviewed=True) if c["variance"] else None
            clash = st.in_conflict(c)           # another live count of the product disagrees, whether or not this one matches the books
            if not c["variance"] and clash and not review:
                refusal = ("counts_conflict", "Another count of this product, with no stock movement between them, disagrees with this one; a recount is needed.")
            # a recount is offered for a count that cannot stand: it disagrees with another live count, or it was reviewed and the shop would no longer post it
            stuck = bool(refusal) and not moved and not recount and bool((not c["variance"] and not review) or (c["variance"] and review))
            counts.append({"id": c["id"], "product": c["product"], "book": c["book"], "counted": c["counted"], "variance": c["variance"], "at": clock(c["at"]),
                           "state": state, "open": bool(c["variance"] and not review), "accept": refusal is None,
                           "post": bool(c["variance"] and review and not moved and not recount and refusal is None),
                           "recount": stuck,
                           "why_not": plain(refusal[1]) if refusal and not moved and not recount else "",
                           "reason": plain(review["reason"]) if review else ""})
        returned = {}
        for r in st.returns.values():
            returned[r["quote"]] = returned.get(r["quote"], 0) + r["quantity"]
        live = {i.key for i in work.open_items(self.queue)}
        sales, owed_all = [], st.refunds_owed()
        for quote, q in sorted(st.quotes.items(), key=lambda kv: kv[1]["at"], reverse=True):
            problem = f"shop:payment:{quote}" in live
            owed = [a for q2, a in owed_all if q2 == quote]
            if quote in st.attachments and not problem and not owed:
                continue                    # a move-in add-on belongs to the rental's checkout; only a problem with its payment is settled here
            pay = st.payments.get(quote)
            sale, status = st.sales.get(quote), (pay or {}).get("status", "open")
            gone, written = quote in st.cancelled, quote in st.write_offs
            why = st.cancelled.get(quote) or st.write_offs.get(quote) or {}
            sales.append({"quote": quote, "product": q["product"], "quantity": q["quantity"], "total": q["price"] * q["quantity"], "at": clock(q["at"]),
                          "pay": SHOP_PAYMENTS.get(status, (status.replace("_", " "), "plain")), "posted": bool(sale), "reversed": quote in st.reversed_sales,
                          "paid_unposted": status == "succeeded" and not sale and not gone, "returned": returned.get(quote, 0), "cancelled": gone,
                          "written_off": written, "problem": problem, "addon": quote in st.attachments, "reason": plain(why.get("reason", "")),
                          "can_return": bool(sale) and quote not in st.reversed_sales and returned.get(quote, 0) < q["quantity"],
                          "can_retry": bool(pay) and not gone and not written and quote not in st.attachments and status != "succeeded" and st.can_close(quote),
                          "can_cancel": not sale and not gone and status != "succeeded" and (pay is None or st.can_close(quote)),
                          "can_resume": bool(pay) and not gone and quote not in st.attachments and status in ("open", "processing") and not pay.get("problem"),
                          "owed": [{"key": a["key"], "total": money(a["cents"]), "replaced": not (gone or written)} for a in owed],
                          "attempt": (pay or {}).get("key", ""),
                          "can_write_off": bool(sale) and not written and quote not in st.reversed_sales and status in ("returned", "failed"),
                          "left": q["quantity"] - returned.get(quote, 0)})
        sales.sort(key=lambda x: not (x["problem"] or x["owed"]))          # what needs a look comes first
        returns = [{"id": r["id"], "product": r["product"], "quantity": r["quantity"], "reason": plain(r["reason"]), "at": clock(r["at"]),
                    "posted": r["id"] in posted("return")} for r in sorted(st.returns.values(), key=lambda r: r["at"], reverse=True)]
        totals = self.merchandise           # read in the same step as the folded books above, so revenue, sales and stock describe the same records
        books = {"revenue": -totals.get("income:merchandise", 0), "cost": totals.get("cost:merchandise", 0)}
        differences = [shop_words(d) for d in (st.checks[-1]["differences"] if st.checks else [])]
        return {"products": products, "deliveries": deliveries, "unposted_receipts": unposted_receipts, "handovers": handovers, "counts": counts,
                "sales": sales, "returns": returns, "books": books, "differences": differences,
                "sellable": [p for p in products if p["active"]],
                "shown": json.dumps({p["name"]: p["price"] for p in products if p["active"]})}      # the prices the counter form shows, sent back with the sale

    def units(self) -> list[tuple[str, list[dict]]]:
        b, rent = self.b, self.rent
        active = {x.unit: x for x in b.leases.values() if x.ended is None}
        disputed = {b.charges[d.charge].lease for d in b.disputes.values() if d.outcome is None}
        rows: dict[str, list[dict]] = {}
        for u in b.units:
            lease, state = active.get(u), rent.status(u, self.now)
            note, held_for = self.held(u)
            priced = rent.units.get(u)
            cents = priced.rent_cents if priced else None
            item = {"unit": u, "state": state, "chip": None, "who": held_for, "rent": "", "since": "", "note": plain(note),
                    "size": (priced.size if priced else None) or "", "price": money(cents) if cents else "Not priced",
                    "price_value": f"{cents / 100:.2f}" if cents else "",
                    "block": (rent.holder(u, self.now) or ("", ""))[0] == "block"}
            if lease is None:
                last = max((x.ended for x in b.leases.values() if x.unit == u and x.ended), default=None)
                item["since"] = f"Empty since {day(last)}" if last else "Not rented this year"
                why = reconcile.readiness(b, rent, u, self.now) if state == "vacant" else []
                if why and not item["note"]:         # vacant on the books, but something on the floor or a repair keeps it off the market
                    first = re.sub(r"^work order \S+ is", "a work order is", re.sub(r"\s*\(.*\)$", "", why[0].split(":")[0]))
                    item["note"] = plain(f"Not ready to rent: {first}.")
            else:
                p = self.people[lease.tenant]
                chip = ("Fee in dispute", "wait") if lease.id in disputed else (("Past due", "act") if p["past"] else None)
                item.update(chip=chip, who=p, rent=money(lease.rent_cents), since=f"{day(lease.start)}, {lease.start.year}")
            rows.setdefault(u[0], []).append(item)
        return sorted(rows.items())

    @cached_property
    def tenant_labels(self) -> dict[str, str]:
        """Every tenant as staff type the name, mapped to their record: the name, with the phone added where two tenants share one."""
        names = [p["name"] for p in self.people.values()]
        return {(p["name"] if names.count(p["name"]) == 1 else f"{p['name']}, {p['phone']}"): p["slug"]
                for p in sorted(self.people.values(), key=lambda p: (p["name"], p["phone"]))}

    def tenant_typed(self, text: str) -> dict | None:
        """The tenant staff typed, by the name the list shows (case and spacing do not matter); None when it names no one or two."""
        want = " ".join(text.split()).casefold()
        return next((self.by_slug[slug] for label, slug in self.tenant_labels.items() if label.casefold() == want), None)

    @staticmethod
    def unit_counts(rows) -> dict:
        """The legend's figures: every unit counted once, an occupied one by its worst chip."""
        counts = dict.fromkeys(("occupied", "past", "dispute", "reserved", "blocked", "vacant"), 0)
        for _, items in rows:
            for u in items:
                tone = u["chip"][1] if u["chip"] else None
                counts[{"act": "past", "wait": "dispute"}.get(tone) if u["state"] == "occupied" and tone else u["state"]] += 1
        return counts

    # collections ----------------------------------------------------------------------------------------------------------
    def steps(self, lease) -> dict:
        """The overlock and lien-eligibility steps on one unit: the state in words and why, from the same check the gate makes."""
        st, out = col.state(self.b), {}
        for step in col.STEPS:
            state, why = col.step_state(self.b, lease.id, step, self.now)
            if state == "recorded":
                why = f"Since {dated(local((st.overlocks if step == 'overlock' else st.liens)[lease.id]['at']).date(), self.today.year)}, by staff"
            elif state == "clear" and col.days_past_due(self.b, lease.id, self.now) is None:
                why = ""                    # nothing past due: nothing to explain
            text, tone = ("Moved out", "plain") if lease.ended else STEP_WORDS[state]
            out[step] = {"state": state, "words": text, "tone": tone, "why": plain(upfirst(why))}
        return out

    def plan_of(self, tenant: str) -> dict | None:
        """The tenant's latest accepted payment plan as an FMS shows it, or None."""
        plans = [p for p in col.state(self.b).plans.values() if p.tenant == tenant and p.terms]
        if not plans:
            return None
        p = max(plans, key=lambda x: x.history[0][1])
        agreed = next(at for s, at in p.history if s == "accepted")
        parts = []
        for x in col.instalments(self.b, p, self.now):
            text, tone = PART_WORDS[x["state"]]
            if p.state == "cancelled" and x["state"] in ("scheduled", "due", "partly paid"):
                text, tone = "", ""             # the plan ended before this part came due: it has no state
            parts.append({"label": "First payment" if x["part"] == 0 else f"Payment {x['part']}", "due": dated(date.fromisoformat(x["due"]), self.today.year),
                          "amount": money(x["cents"]), "words": text + (f", {money(x['covered'])} so far" if x["state"] == "partly paid" else ""), "tone": tone})
        text, tone = PLAN_WORDS[p.state]
        return {"id": p.id, "state": p.state, "words": text, "tone": tone, "total": money(p.terms["total"]), "consent": p.consent["quote"],
                "agreed": clock(agreed), "policy": p.policy_version, "grace": p.terms["grace_days"], "parts": parts,
                "review": p.state in col.WITH_STAFF}

    def collections(self, tenant: str) -> dict:
        """Everything collections holds for one tenant: the plan, each unit's steps, active exemptions, open promises."""
        st = col.state(self.b)
        units = [{"lease": x.id, "unit": x.unit, **self.steps(x)} for x in self.leases_of.get(tenant, []) if x.ended is None]
        exemptions = [{"id": e.id, "lease": e.lease, "unit": self.b.leases[e.lease].unit, "scope": upfirst(" and ".join(SCOPE_WORDS[s] for s in e.scope)),
                       "reason": e.reason, "until": dated(e.until, self.today.year), "owner": e.owner.replace("_", " ")}
                      for e in st.exemptions.values() if e.tenant == tenant and col.exemption_state(e, self.today) == "active"]
        promises = [{"amount": money(p.cents), "by": dated(p.by, self.today.year), "callback": dated(p.callback, self.today.year), "quote": p.quote}
                    for p in st.promises.values() if p.tenant == tenant and p.state == "open"]
        plan = self.plan_of(tenant)
        cards = [{"id": c["card"], "label": f"{c['brand']} ending {c['last4']}" + (", a test card that declines" if c["token"] == "card_declined" else "")}
                 for c in col.cards_of(self.b, tenant)]
        payments = [{"when": clock(i.created), "amount": money(i.cents), "words": PAYMENT_WORDS[i.state][0], "tone": PAYMENT_WORDS[i.state][1]}
                    for i in sorted((i for i in st.intents.values() if i.tenant == tenant), key=lambda i: i.created, reverse=True)[:6]]
        show = bool(plan or exemptions or promises or self.people[tenant]["past"] or any(u["overlock"]["state"] != "clear" for u in units))
        return {"plan": plan, "units": units, "exemptions": exemptions, "promises": promises, "show": show and bool(units),
                "cards": cards, "card_payments": payments, "owing": col.owing(self.b, tenant),
                "until_max": (self.today + timedelta(days=col.EXEMPTION_DAYS)).isoformat(), "until_min": self.today.isoformat()}

    def lien_files(self, tenant: str) -> list[dict]:
        """Show each file from the facts on this visitor's copy."""
        files = []
        state = lien_core.state(self.b)
        for case in state.cases.values():
            if case.tenant != tenant:
                continue
            where, why = lien_core.case_state(self.b, case, self.now)
            findings = lien_core.findings(self.b, case)
            missing = [x.requirement for x in findings if x.status != "met"]
            next_step = ("Record the written address and prepare the notice copy" if not case.copies and not case.addresses else
                         "Prepare the notice copy" if not case.copies else
                         "Review the changed file" if where == "review required" else
                         "Record the missing evidence" if missing else
                         "A manager approves" if not lien_core.approval(self.b, case) else "Review the auction")
            a = state.auctions.get(case.id)
            sale = state.sales.get(case.id)
            surplus = state.surpluses.get(sale.surplus) if sale and sale.surplus else None
            files.append({"case": case, "where": where.capitalize(), "why": plain(why), "next": next_step,
                          "findings": [{"requirement": plain(x.requirement), "status": x.status,
                                        "detail": plain(re.sub(r"\brecords? \d+(?: and \d+)?(?:, made from copy \d+)?", "the evidence", x.detail)),
                                        "action": plain(x.action)} for x in findings], "missing": missing, "frozen": lien_core.frozen(case),
                          "approval": lien_core.approval(self.b, case), "fingerprint": lien_core.fingerprint(self.b, case),
                          "stops": [{**stop, "detail": plain(stop["detail"]), "resolved": ({**stop["resolved"], "reason": plain(stop["resolved"]["reason"])} if stop["resolved"] else None)} for stop in lien_core.unit_stops(self.b, case.lease)], "auction": a,
                          "auction_words": auction.words(a) if a else None, "sale": sale, "surplus": surplus})
        return files

    # protection -------------------------------------------------------------------------------------------------------------
    def protection_of(self, tenant: str) -> dict:
        """The tenant's own policies and the facility's plan as the records stand, each state in words: what was recorded, who reviewed it and
        how, each enrolment with its acceptance and covered months, the plan's charges with any hold on them, and what staff may do next."""
        b, st, year = self.b, protection.state(self.b), self.today.year
        mine = {x.id: x for x in self.leases_of.get(tenant, [])}
        full = lambda d: f"{day(d)}, {d.year}"
        policies = []
        for e in reversed([e for e in st.evidence.values() if e.lease in mine]):
            words, tone = POLICY_WORDS[e.status]
            review = e.review and {"outcome": "Reviewed" if e.review["outcome"] == "reviewed" else "Rejected", "by": staff_name(e.review["by"]), "when": clock(e.review["at"]),
                                   "how": plain(e.review["how"]), "checked": ", ".join(e.review["checked"]), "note": plain(e.review["note"]),
                                   "confirmation": e.review["confirmation"]}
            policies.append({"id": e.id, "unit": e.unit, "carrier": e.carrier or "Not given", "number": e.number or "Not given",
                             "dates": ("From " + full(e.effective) + (" to " + full(e.expires) if e.expires else ", no end date on it")) if e.effective else
                                      ("Ends " + full(e.expires)) if e.expires else "No dates given",
                             "document": plain(e.document) if e.document else "None", "statement": plain(e.statement), "words": words, "tone": tone,
                             "flags": [protection.FLAG_WORDS[f] for f in e.flags], "review": review, "reviewable": e.status in protection.OPEN_STATES})
        plans = []
        for e in reversed([e for e in st.enrolments.values() if e.lease in mine]):
            words, tone = PLAN_STATE_WORDS[e.state]
            a = st.acceptances.get(e.acceptance or "")
            ended = e.ended and {"on": full(e.ended["on"]), "reason": upfirst(e.ended["reason"]), "by": staff_name(e.ended["by"]),
                                 "unused": f"{e.ended['unused_days']} unused days ({money(e.ended['unused_cents'])}) are on record" if e.ended["unused_days"] else ""}
            plans.append({"id": e.id, "unit": e.unit, "version": e.version, "cents": e.cents, "price": money(e.cents), "words": words, "tone": tone, "state": e.state,
                          "offered": f"Offered {dated(local(e.offered_at).date(), year)} by {staff_name(e.offered_by)}",
                          "acceptance": a and {"who": self.people[a.tenant]["name"], "how": a.how, "words": plain(a.words), "starts": full(a.starts),
                                               "at": clock(a.at)},
                          "periods": [f"{month_words(m)} {m[:4]}" for m in e.periods(self.today)], "ended": ended,
                          "basis": "Moved onto the plan under the owner's rule" if e.basis == "owner rule" else "", "simulated": protection.SIMULATED,
                          "left": mine[e.lease].ended and full(mine[e.lease].ended),
                          "stop_on": (mine[e.lease].ended + timedelta(days=1) if mine[e.lease].ended else self.today).isoformat()})
        charges = []
        for c in sorted((c for c in b.tenant_charges(tenant) if c.kind == PROTECTION), key=lambda c: c.due, reverse=True):
            period = (c.obligation or "").rsplit(":", 1)[-1]
            held = b.held(c.id)
            why = "A dispute holds it until staff decide" if any(h.active and h.charge == c.id for h in b.holds.values()) else upfirst(protection.unsupported(b, c.id) or "")
            charges.append({"month": f"{month_words(period)} {period[:4]}" if re.fullmatch(r"\d{4}-\d{2}", period) else day(c.due), "unit": b.leases[c.lease].unit,
                            "amount": money(c.cents), "state": ("Removed" if c.closed == "reversed" else "Waived") if c.closed else "Paid" if not c.open_cents else "Owed",
                            "held": held, "why": plain(why)})
        try:
            current = protection.version_in_force(b, protection.PRODUCT, self.today)
        except Refused:
            current = None
        active = [x for x in mine.values() if x.ended is None]
        offers = []
        for x in active:
            mine_plans = [e for e in st.enrolments.values() if e.lease == x.id]
            running = next((e for e in mine_plans if e.state == "active"), None)
            if current and not any(e.state in ("offered", "accepted") for e in mine_plans) and (running is None or running.version < current.version):
                offers.append({"lease": x.id, "unit": x.unit, "version": current.version, "price": money(current.cents),
                               "replaces": running.id if running else "", "old": running.version if running else None})
        return {"show": bool(active or policies or plans or charges), "policies": policies, "plans": plans, "charges": charges, "leases": [{"id": x.id, "unit": x.unit} for x in active],
                "offers": offers, "checks": [(f"check_{c.replace(' ', '_')}", c) for c in protection.REVIEW_CHECKS], "accept_hows": protection.ACCEPT_HOWS,
                "end_reasons": protection.END_REASONS, "today": self.today.isoformat()}

    def protection_word(self, lease: str) -> str:
        """What protection a lease has, read from the records and never guessed: the plan if one is active, else where the tenant's own
        policy stands (the newest record that was not rejected; a reviewed one past its end date reads expired before the daily check marks it)."""
        st = protection.state(self.b)
        plans = [e for e in st.enrolments.values() if e.lease == lease and e.state in ("active", "ended") and e.starts is not None]
        if any(e.starts <= self.today and (e.ended is None or e.ended["on"] > self.today) for e in plans):
            return "Plan active"
        if coming := min((e.starts for e in plans if e.ended is None and e.starts > self.today), default=None):
            return f"Plan starts {day(coming)}"          # active in the records, but coverage has not begun: this column says what the lease has now
        e = protection.standing(st, lease)
        if e is None:
            return "None"
        if e.status == "expired" or (e.status == "reviewed" and e.expires is not None and e.expires < self.today):
            return "Own policy expired"
        return "Own policy reviewed" if e.status == "reviewed" else "Own policy needs review"

    def standing(self, tenant: str) -> dict:
        """For the past-due list: the plan's state and the first unit whose overlock is not clear, with why."""
        plans = [p for p in col.state(self.b).plans.values() if p.tenant == tenant and p.terms]
        plan = PLAN_WORDS[max(plans, key=lambda x: x.history[0][1]).state] if plans else None
        leases = self.leases_of.get(tenant, [])
        # the units still rented; a tenant who moved out with money owed is shown on the unit they left, with why no step applies
        units = [(x.unit, self.steps(x)["overlock"]) for x in ([x for x in leases if x.ended is None] or leases[-1:])]
        unit, step = next(((u, s) for u, s in units if s["state"] != "clear"), units[0] if units else (None, None))
        return {"plan": plan, "overlock": step, "overlock_unit": unit if len(units) > 1 else None}

    def ledger(self, tenant: str) -> list[dict]:
        """The tenant's account line by line, newest first, with the balance after each line. Needs everything=True."""
        b = self.b
        mine = self.leases_of.get(tenant, [])
        accounts = {f"receivable:{x.id}" for x in mine} | {f"credit:{tenant}"}
        several = len(mine) > 1
        rows, balance = [], 0
        for e in self.events:
            p = e.payload
            if e.kind == "payment.declined" and p.get("tenant") == tenant:
                rows.append({"on": dated(local(e.at).date(), self.today.year), "what": f"Card declined, {money(p['cents'])}", "by": "Card", "amount": "", "balance": money(balance)})
                continue
            delta = sum(c for a, c, _ in lines(e.kind, p) if a in accounts)
            if delta == 0:
                continue
            balance += delta
            unit = f", unit {b.leases[p['lease']].unit}" if several and p.get("lease") in b.leases else ""
            when = dated(local(e.at).date(), self.today.year)
            if e.kind == "charge.posted":
                obligation = p.get("obligation") or ""
                what = (f"{month_words(obligation.split(':')[2])} rent" if p["kind"] == RENT and obligation.count(":") == 2
                        else KIND_WORDS.get(p["kind"], p["kind"]).capitalize()) + unit + (", posted by hand" if (p.get("source") or "").startswith("staff") else "")
                by = "Staff" if (p.get("source") or "").startswith("staff") else "Billing"
            elif e.kind == "payment.received":
                eff = p["effective_at"]
                when = dated(date.fromisoformat(eff[:10]), self.today.year)
                what = "Card payment" + (f", learned {day(e.at)}" if eff[:10] != local(e.at).date().isoformat() else "")
                by = "Card"
            elif e.kind == "payment.returned":
                what, by = "Payment returned by the bank", "Card"
            elif e.kind == "charge.reversed":
                what = f"{KIND_WORDS.get(p['kind'], p['kind']).capitalize()} removed: {p['reason']}"
                by = "Staff" if e.source.startswith("staff") else BY.get(e.source, "Billing")
            elif e.kind == "charge.waived":
                what = f"{KIND_WORDS.get(p['kind'], p['kind']).capitalize()} waived under the owner's rule"
                by = "Staff" if e.source.startswith("staff") else BY.get(e.source, "Billing")
            else:
                what, by = e.kind.replace(".", " ").replace("_", " ").capitalize(), BY.get(e.source, "Billing")
            rows.append({"on": when, "what": what, "by": by, "amount": money(delta) if delta > 0 else f"-{money(-delta)}", "balance": money(balance)})
        return rows[::-1]

    def rentals(self, tenant: str) -> list[dict]:
        return [{"unit": x.unit, "rent": money(x.rent_cents), "start": f"{day(x.start)}, {x.start.year}",
                 "ended": f"{day(x.ended)}, {x.ended.year}" if x.ended else None,
                 "schedule": ", ".join(KIND_WORDS.get(k, k) for k in x.schedule) or "no fees", "owed": money(self.b.receivable(x.id))}
                for x in self.leases_of.get(tenant, [])]

    def versions(self, name: str, say) -> list[dict]:
        """Every version of a rule on this copy, newest first, in the words code writes from each typed form. Nothing to list
        until a second version exists: the rule in force is already shown by its own panel."""
        held = self.b.rules[name]
        rows = [{"rule": name, "version": h["version"], "words": say(h["typed"]), "when": dated(local(h["at"]).date(), self.today.year),
                 "by": h["by"], "restores": h.get("restores"), "now": h["version"] == held["version"], "same": h["typed"] == held["typed"]}
                for h in reversed(self.b.rule_history[name])]
        return rows if len(rows) > 1 else []

    def rules(self) -> dict:
        rule = self.b.rules.get("late_fee_waiver")
        staff_rule = self.b.rules.get(STAFF_RULE)
        return {"waiver": rule, "waiver_versions": self.versions("late_fee_waiver", waiver_words) if rule else [],
                "staff_versions": self.versions(STAFF_RULE, staff_words) if staff_rule else [], "typed": [words(k, v) for k, v in (rule or {}).get("typed", {}).items() if not isinstance(v, dict)],
                "waiver_words": waiver_words(rule["typed"]) if rule else [], "staff_rule": staff_rule,
                "staff_rule_words": staff_words(staff_rule["typed"]) if staff_rule else [],
                "late_fee": money(LATE_FEE_CENTS), "admin_fee": money(ADMIN_FEE_CENTS), "review_days": REVIEW_DAYS,
                "actions": [(MAY_DO[a], a in ("reverse_duplicate", "correct_fee_posted_in_error", "waive_fee")) for a in ACTIONS],
                "staff": [STAFF_DO[a] for a in STAFF_ACTIONS], "plan_versions": self.plan_versions(), **self.settings()}

    def plan_versions(self) -> list[dict]:
        """Every version of the protection plan, newest first: the price, the terms and the day it took or takes effect. The one in force is the
        newest whose day has come; an older one is replaced, and a later one waits for its day."""
        versions = protection.state(self.b).versions.get(protection.PRODUCT, [])
        try:
            current = protection.version_in_force(self.b, protection.PRODUCT, self.today).version
        except Refused:
            current = None
        return [{"version": v.version, "price": money(v.cents), "terms": plain(v.terms), "effective": f"{day(v.effective)}, {v.effective.year}",
                 "by": staff_name(v.by), "now": v.version == current, "waiting": v.effective > self.today} for v in reversed(versions)]

    def settings(self) -> dict:
        """The owner's plan policy and schedule, each as its form fields with the value in force and the fixed limits as bounds."""
        st = col.state(self.b)

        def fields(spec, held, bounds):
            out = []
            for key, label, unit in spec:
                lo, hi = bounds[key]
                value = (held or {}).get(key)
                if unit == "dollars":
                    lo, hi, value = -(-lo // 100), hi // 100, None if value is None else value // 100
                out.append({"key": key, "label": label, "unit": unit, "min": lo, "max": hi, "value": value})
            return out
        return {"policy": st.policy, "policy_fields": fields(POLICY_FORM, st.policy, col.POLICY_BOUNDS),
                "schedule": st.schedule, "schedule_fields": fields(SCHEDULE_FORM, st.schedule, col.SCHEDULE_BOUNDS)}


async def form(request: Request, limit: int = 8192) -> dict[str, str]:
    """A small form post, read up to its limit: a bigger body is refused before it is held in memory."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(413, "That form is too large.")
    return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}


def mount(app, fd: FrontDesk, settings, guard, from_elsewhere, session_factory, live: bool) -> None:
    spend.SOURCES["front_desk"] = fd.spent_dollars
    from .web import templates

    def session(request: Request) -> str | None:
        return guard.valid(request.cookies.get(SESSION), "session")

    def need(request: Request) -> str:
        ident = session(request)
        if ident is None:
            if request.url.path.startswith("/api/"):
                raise HTTPException(401, "Sign in first.")
            raise HTTPException(303, "Sign in first.", headers={"Location": "/sign-in"})
        return ident

    def same_site(request: Request) -> None:
        if from_elsewhere(request):
            raise HTTPException(403, "this request came from another site")

    def show(request: Request, name: str, nav: str, f: Facility, **ctx):
        return templates.TemplateResponse(request, name, {"nav": nav, "side": SIDE, "agent": AGENT, "f": f, "today": date_words(f.today), "live": live,
                                                          "money": money, "day": day, "clock": clock, "local": local, "phone": phone, **ctx})

    templates.env.filters["plain"] = plain

    def check() -> dict | None:
        return {"sitekey": guard.sitekey} if guard.on else None

    # public ------------------------------------------------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def overview(request: Request):
        # the worked example names the month the FMS's own story sits in, so the two never disagree
        return templates.TemplateResponse(request, "overview.html", {"nav": "front", "facts": fd.facts(), "score": json.loads(SCORE.read_text()),
                                                                     "month": f"{story_month(fd.today()):%B}",
                                                                     "money": money, "agent": AGENT, "signed_in": session(request) is not None})

    @app.get("/film", include_in_schema=False)
    def film_moved():
        return RedirectResponse("/", status_code=301)

    @app.get("/sign-in", include_in_schema=False)
    def sign_in_page(request: Request):
        if session(request):
            return RedirectResponse("/home", status_code=303)
        return templates.TemplateResponse(request, "sign_in.html", {"nav": "sign-in", "check": check(), "demo": (DEMO_NAME, DEMO_PASSWORD)})

    @app.post("/sign-in", include_in_schema=False)
    async def sign_in(request: Request):
        same_site(request)

        def refuse(text: str, status: int):
            return templates.TemplateResponse(request, "sign_in.html", {"nav": "sign-in", "check": check(), "demo": (DEMO_NAME, DEMO_PASSWORD),
                                                                       "error": text, "name": name[:60]}, status_code=status)
        data = await form(request)
        name, password = data.get("name", "").strip().lower(), data.get("password", "")
        if guard.broken:
            return refuse(UNSET.replace("texts and runs are closed", "signing in is closed"), 503)
        # both compared in full whatever the first says, so the time taken does not tell which one was wrong
        right = hmac.compare_digest(name.encode(), DEMO_NAME.encode()) & hmac.compare_digest(password.encode(), DEMO_PASSWORD.encode())
        if not right:
            return refuse(WRONG, 401)
        if guard.on:
            if not guard.allow(spend.address_of(request)):
                return refuse(SLOW_DOWN, 429)
            try:
                passed = await run_in_threadpool(guard.passed, data.get("cf-turnstile-response", ""), spend.address_of(request))
            except Unreachable:
                return refuse("The quick check is not answering. Wait a minute, then reload the page.", 503)
            if not passed:
                return refuse("The quick check did not pass. Reload the page and try again.", 403)
        response = RedirectResponse("/home", status_code=303)
        response.set_cookie(SESSION, guard.cookie("session"), max_age=LIFETIME, httponly=True, samesite="lax", secure=spend.is_https(request))
        return response

    # signed in ---------------------------------------------------------------------------------------------------------
    def site_show(request, template, f, **ctx):
        def site_money(cents):
            return f"${cents // 100:,}" + (f".{cents % 100:02d}" if cents % 100 else "")

        return templates.TemplateResponse(request, template, {"f": f, "money": site_money, "plain": plain, **ctx})

    @app.get("/rent", include_in_schema=False)
    def website(request: Request):
        f = Facility(fd, need(request))

        def area(size):
            parts = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[xX]\s*(\d+(?:\.\d+)?)\s*", size)
            return Decimal(parts[1]) * Decimal(parts[2]) if parts else Decimal("Infinity")

        units = []
        for unit, record in f.rent.units.items():
            if record.rent_cents and record.size and not reconcile.readiness(f.b, f.rent, unit, f.now):      # ready, not only vacant
                units.append(record)
        units.sort(key=lambda u: (area(u.size), u.rent_cents, u.id))
        sizes = sorted({u.size for u in units}, key=lambda size: (area(size), size))
        size = request.query_params.get("size")
        size = size if size in sizes else None        # an unknown size shows every unit
        shown = [u for u in units if u.size == size] if size else units
        return site_show(request, "site_store.html", f, units=shown, sizes=sizes, size=size, token=secrets.token_hex(6),
                         inquiry_note=fd.take_site_note(f.ident))

    @app.post("/rent/inquiry", include_in_schema=False)
    async def website_inquiry(request: Request):
        same_site(request)
        ident = need(request)
        data = await form(request)
        limits = {"name": 80, "phone": 30, "email": 120, "size": 20, "start": 10, "message": 1000}       # a public form: keep each field to a sane size
        if any(len(data.get(k, "")) > n for k, n in limits.items()):
            raise HTTPException(422, "One of the answers is too long. Shorten it and try again.")
        channels = (["phone", "text"] if data.get("phone") else []) + (["email"] if data.get("email") else []) if data.get("consent") == "yes" else []
        token = data.get("token", "")
        if token and not re.fullmatch(r"[0-9a-f]{12}", token):
            raise HTTPException(422, "Reload the page and try again.")
        def save_inquiry():
            with fd.lock:
                if token and (ident, "inquiry:" + token) in fd.site_forms:     # the same page sent again (back and resend): the first press stands, no second inquiry
                    return
                store, stream = fd.ready(), fd.stream(ident)
                with store.write():
                    led = Ledger(store.books(stream))
                    try:
                        leads.capture(led, fd.clock(), source="website inquiry", name=data.get("name", ""), phone=data.get("phone", ""), email=data.get("email", ""), size=data.get("size", ""), start=data.get("start", ""), words=data.get("message", ""), permissions=channels)
                    except Refused as exc:
                        return str(exc)
                    store.append(stream, led.pending)
                if token:
                    fd.site_forms.add((ident, "inquiry:" + token))              # only after it was saved: a refused form can be corrected and sent again
                return None
        why = await run_in_threadpool(save_inquiry)
        if why:                         # the visitor sees why beside the form, not an error page
            fd.put_site_note(ident, why)
            return RedirectResponse("/rent#ask", status_code=303)
        return RedirectResponse("/rent?inquiry=received", status_code=303)

    @app.get("/leads", include_in_schema=False)
    def lead_list(request: Request):
        f = Facility(fd, need(request))
        records = sorted(leads.state(f.b).records.values(), key=lambda lead: lead.arrived, reverse=True)
        next_steps = {}
        for lead in records:
            if lead.status in ("converted", "unqualified", "duplicate"):
                continue
            overdue = leads.due(lead, f.now)
            steps = overdue or leads.due(lead, datetime.max.replace(tzinfo=f.now.tzinfo))
            if steps:
                step, when = (max if overdue else min)(steps, key=lambda item: item[1])
                next_steps[lead.id] = f"{leads.label(step)}, {'overdue since' if overdue else 'due'} {clock(when)}"
            else:
                next_steps[lead.id] = "Follow-up planned" if lead.status in ("new", "contacted") else "No follow-up scheduled"
        return show(request, "fms_leads.html", "leads", f, records=records, next_steps=next_steps, token=secrets.token_hex(6), note=plain(fd.take_step_note(f.ident)))

    @app.post("/leads/new", include_in_schema=False)
    async def lead_new(request: Request):
        same_site(request)
        ident = need(request)
        data = await form(request)
        token, action = data.get("token", ""), data.get("action", "")
        if not re.fullmatch(r"[0-9a-f]{12}", token) or action not in ("record_call", "record_walk_in"):
            raise HTTPException(422, "Reload the page and try again.")
        data["permissions"] = (["phone", "text"] if data.get("phone") else []) + (["email"] if data.get("email") else []) if data.get("consent") == "yes" else []
        def save_lead():
            with fd.lock:
                decision = leads.run(fd.ready(), fd.stream(ident), leads.Act(f"lead-form:{token}:{action}", action, STAFF, form=data), fd.clock())
                if not decision.allowed:
                    fd.keep_step_note(ident, decision.check)
        await run_in_threadpool(save_lead)
        return RedirectResponse("/leads", status_code=303)

    @app.get("/leads/{key}", include_in_schema=False)
    def lead_page(request: Request, key: str):
        f = Facility(fd, need(request))
        lead = leads.state(f.b).records.get(key)
        if lead is None:
            raise HTTPException(404, "No such inquiry.")
        matches = []
        renters = leads.matching_renters(f.b, lead)       # live: a renter created after the inquiry counts too
        recorded = [tuple(m) for m in lead.matches]
        for kind, ident in recorded + [("tenant", ident) for ident in renters if ("tenant", ident) not in recorded]:
            match = leads.state(f.b).records.get(ident) if kind == "lead" else f.b.tenants.get(ident)
            if not match:
                continue
            reasons = []
            if lead.phone and lead.phone == leads.normal_phone(match.phone):
                reasons.append("phone")
            if lead.email and lead.email == (getattr(match, "email", "") or "").strip().lower():
                reasons.append("email")
            url = f"/leads/{ident}" if kind == "lead" else f"/tenants/{f.people[ident]['slug']}"
            matches.append((kind, match, url, " and ".join(reasons) or "contact details"))
        steps = [(c.key.removeprefix(f"lead:{lead.id}:"), leads.label(c.kind)) for c in work.conditions(f.b, f.rent, f.now).values() if c.source == "lead" and c.ref == lead.id and c.kind not in ("conversion needs review", "rental needs review")]
        choices = [(ident, f.b.tenants[ident].name, ", ".join(sorted({r.unit for r in f.rent.rentals.values() if r.tenant == ident}))) for ident in renters]
        settled = ("Converted. Follow-ups have stopped." if lead.status == "converted" else
                   "This person has a signed rental and a verified payment, so follow-ups have stopped. Record the conversion above."
                   if leads.conversion_evidence(f.b, f.rent, lead) else "")      # the core refuses follow-ups then, so the page does not offer them
        stopped = any(other.stopped and (other.id == lead.id or (lead.phone and lead.phone == other.phone) or (lead.email and lead.email == other.email))
                      for other in leads.state(f.b).records.values())      # the core's own test: a stop on any inquiry from this person
        return show(request, "fms_lead.html", "leads", f, lead=lead, matches=matches, choices=choices, steps=steps, settled=settled, closed=leads.closed_reason(lead), stopped=stopped, said=leads.said, token=secrets.token_hex(6), note=plain(fd.take_step_note(f.ident)))

    @app.post("/leads/{key}", include_in_schema=False)
    async def lead_step(request: Request, key: str):
        same_site(request)
        ident = need(request)
        data = await form(request)
        token = data.get("token", "")
        if not re.fullmatch(r"[0-9a-f]{12}", token):
            raise HTTPException(422, "Reload the page and try again.")
        def save_step():
            with fd.lock:
                decision = leads.run(fd.ready(), fd.stream(ident), leads.Act(f"lead-form:{token}:{data.get('action', '')}:{key}:{data.get('other', '')}", data.get("action", ""), STAFF, key, data), fd.clock())
                if not decision.allowed:
                    fd.keep_step_note(ident, decision.check)
        await run_in_threadpool(save_step)
        return RedirectResponse(f"/leads/{key}", status_code=303)

    @app.get("/rent/{unit}", include_in_schema=False)
    def website_rent(request: Request, unit: str):
        f = Facility(fd, need(request))
        record = f.rent.units.get(unit)
        if not record or not record.rent_cents or not record.size or reconcile.readiness(f.b, f.rent, unit, f.now):
            return RedirectResponse("/rent?unavailable=1", status_code=303)
        return site_show(request, "site_renter.html", f, unit=record, token=secrets.token_hex(6))

    @app.post("/rent/{unit}", include_in_schema=False)
    async def website_begin(request: Request, unit: str):
        same_site(request)
        ident = need(request)
        data = await form(request)
        token, name, phone = data.get("token", ""), data.get("name", "").strip(), data.get("phone", "").strip()
        if not re.fullmatch(r"[0-9a-f]{12}", token) or not name or not phone or len(name) > 80 or len(phone) > 30:
            raise HTTPException(422, "Enter your name and phone, then try again.")

        def begin():
            with fd.lock:
                rent = fd.rent_view(ident)
                record = rent.units.get(unit)
                if not record or not record.rent_cents or not record.size:
                    return None
                tenant = fd.add_renter(ident, name, phone)
                held = fd.rent_step(ident, renting.Act(f"site:{token}:reserve", "reserve", "renter", unit, tenant))
                if not held.allowed:
                    return None
                checkout = fd.rent_step(ident, renting.Act(f"site:{token}:checkout", "start_checkout", "renter", held.made, tenant))
                if checkout.allowed:
                    store, stream = fd.ready(), fd.stream(ident)
                    with store.write():
                        led = Ledger(store.books(stream))
                        led.emit("lead.checkout_started", fd.clock(), {"rental": checkout.made, "tenant": tenant, "size": record.size}, f"lead:checkout:{checkout.made}")
                        store.append(stream, led.pending)
                return checkout.made if checkout.allowed else None

        rental = await run_in_threadpool(begin)
        return RedirectResponse(f"/rent/checkout/{rental}" if rental else "/rent?unavailable=1", status_code=303)

    checkout_amounts = {}

    @app.get("/rent/checkout/{rental}", include_in_schema=False)
    def website_checkout(request: Request, rental: str):
        f = Facility(fd, need(request))
        r = f.rent.rentals.get(rental)
        if not r:
            raise HTTPException(404, "No such rental.")
        agreement = f.rent.agreements.get(r.agreement or "")
        if not agreement:
            raise HTTPException(404, "No checkout for this rental.")
        token = secrets.token_hex(6)
        account = 0
        cents, quotes, due = renting.checkout_due(f.b, f.rent, r, f.now)       # the same figures the payment checks, so the page and the charge agree
        if r.transfer:
            checkout_amounts[(f.ident, token, r.id)] = cents
            account = r.quote["total_cents"] - cents         # the account's credit (unused rent of the old unit among it) less older open charges
            r = replace(r, quote={**r.quote, "total_cents": cents})
        shop_state = shop.state(f.b)
        addons = [{"product": shop_state.quotes[q]["product"], "quantity": shop_state.quotes[q]["quantity"],
                   "cents": shop_state.quotes[q]["price"] * shop_state.quotes[q]["quantity"]} for q in quotes]
        return site_show(request, "site_checkout.html", f, rental=r, agreement=agreement, token=token, account=account, addons=addons, due=due,
                         changed=request.query_params.get("changed") == "1")

    @app.post("/rent/checkout/{rental}", include_in_schema=False)
    async def website_pay(request: Request, rental: str):
        same_site(request)
        ident = need(request)
        data = await form(request)
        token, card = data.get("token", ""), data.get("card", "")
        if not re.fullmatch(r"[0-9a-f]{12}", token) or card not in ("card_ok", "card_declined"):
            raise HTTPException(422, "Choose a card.")

        def pay():
            with fd.lock:
                r = fd.rent_view(ident).rentals.get(rental)
                if not r:
                    raise HTTPException(404, "No such rental.")
                if not r.accepted and data.get("accept") != "yes":
                    raise HTTPException(422, "Accept the agreement and choose a card.")
                if (ident, token) in fd.site_forms:     # a second press of the same page returns the first answer
                    return
                shown = int(data["total"]) if data.get("total", "").isdecimal() else None
                if shown is None or fd.checkout_total(ident, rental) != shown:
                    return "changed"        # a total that is missing, not a number or moved since the page was shown: nothing is accepted or charged until the renter sees the current one
                fd.site_forms.add((ident, token))
                if not r.accepted:      # once accepted, a declined card can be tried again without accepting twice
                    args = {"sha256": data.get("sha256", "")}
                    if r.transfer:
                        args["amount_cents"] = checkout_amounts.get((ident, token, r.id))
                    decision = fd.rent_step(ident, renting.Act(f"site:{token}:accept", "accept_agreement", "renter", rental, r.tenant, args))
                    if not decision.allowed or decision.repeated:
                        return
                try:
                    fd.pay_rental(ident, rental, card, shown)      # the core refuses a payment that is not due, so a second press pays nothing
                except renting.TotalChanged:
                    return "changed"
                except Refused:
                    pass

        if await run_in_threadpool(pay) == "changed":
            return RedirectResponse(f"/rent/checkout/{rental}?changed=1", status_code=303)
        return RedirectResponse(f"/rent/result/{rental}", status_code=303)

    @app.get("/rent/result/{rental}", include_in_schema=False)
    def website_result(request: Request, rental: str):
        f = Facility(fd, need(request))
        r = f.rent.rentals.get(rental)
        if not r:
            raise HTTPException(404, "No such rental.")
        row = next(row for row in f.renting_of(r.tenant) if row.get("id") == r.id)
        retry = r.state in renting.WAITING and f.rent.holder(r.unit, f.now) == ("rental", r.id) and f.pay_state(r) != "paid"   # a declined card, hold still on
        return site_show(request, "site_result.html", f, rental=r, row=row, person=f.people[r.tenant], retry=retry)

    @app.post("/sign-out", include_in_schema=False)
    def sign_out(request: Request):
        same_site(request)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(SESSION)
        return response

    @app.post("/start-over", include_in_schema=False)
    async def start_over(request: Request):
        same_site(request)
        ident = need(request)
        await run_in_threadpool(fd.reset, ident)
        return RedirectResponse("/home", status_code=303)

    @app.get("/home", include_in_schema=False)
    def home(request: Request):
        ident = need(request)
        f = Facility(fd, ident)
        cases = f.cases()
        morning = [c for c in cases if c["shift"]]
        live = work.open_items(f.queue)
        count = lambda test: sum(1 for i in live if test(i))
        return show(request, "fms_home.html", "home", f, rows=f.occupancy(), month=f.month(month_key(f.today)), past=f.past_due(),
                    waiting=len(f.waiting(cases)), handed=sum(1 for c in cases if c["handed"]), disputes=len(f.disputes()),
                    tasks=count(lambda i: i.source == "collections"), overlocks=count(lambda i: i.kind == "overlock"),
                    unfinished=count(lambda i: i.source in ("reservation", "rental", "refund") and not i.waits),     # a person is needed, not a checkout under way
                    codes=count(lambda i: i.source == "access"), turnover=count(lambda i: i.source == "turnover"),
                    morning=len(SHIFT), closed=sum(1 for c in morning if not c["handed"]),
                    note=plain(fd.take_reset_note(ident)))

    @app.get("/today", include_in_schema=False)
    def today(request: Request, view: str = "all"):
        ident = need(request)
        fd.sync_work(ident)         # what the records say is staff's to do is kept as items before the page reads them
        f = Facility(fd, ident)
        cases = f.cases()
        view = view if view in dict(WORK_VIEWS) else "all"
        items, counts = f.worked(view)
        return show(request, "fms_today.html", "today", f, cases=cases, waiting=f.waiting(cases), disputes=f.disputes(), decisions=f.decisions(),
                    items=items, counts=counts, view=view, views=WORK_VIEWS, roles=("front_desk", "office_manager", "manager"),
                    me=PERSON, token=secrets.token_hex(6), step_note=plain(fd.take_step_note(ident)))

    @app.post("/work/step", include_in_schema=False)
    async def work_step(request: Request):
        """Staff take one step on an item of work, through the gate on this session's copy: acknowledge it, start it, hand it to a role or
        a person, write a note, or say they checked it. The page's token makes a second press return the first decision, and a refusal
        is said once on Today. Acknowledging never closes an item."""
        same_site(request)
        ident = need(request)
        data = await form(request)
        action, token = data.get("action", ""), data.get("token", "")
        if action not in WORK_FORMS:
            raise HTTPException(422, "That is not a step staff can take here.")
        if not re.fullmatch(r"[0-9a-f]{12}", token):
            raise HTTPException(422, "Reload the page and try again.")
        key, to, text = data.get("key", "")[:80], data.get("to", "")[:60], " ".join(data.get("text", "").split())[:400]
        once = hashlib.sha1(f"{to}|{text}".encode()).hexdigest()[:8]
        await run_in_threadpool(fd.staff_work, ident, work.Act(f"form:{token}:{action}:{key}:{once}", WORK_FORMS[action], STAFF, key, to or None, text or None))
        view = data.get("view", "all")
        return RedirectResponse(f"/today?view={view if view in dict(WORK_VIEWS) else 'all'}#work", status_code=303)

    @app.get("/messages", include_in_schema=False)
    def messages(request: Request, case: int | None = None, unread: str | None = None):
        f = Facility(fd, need(request))
        cases = f.cases()
        waiting = f.waiting(cases)
        opened = next((t for t in waiting if t["shift"] == unread), None)
        picked = None if opened else next((c for c in cases if c["n"] == case), cases[0] if cases else None)
        if picked is None and opened is None and waiting:
            opened = waiting[0]          # nothing read yet: open the first message, not an empty pane
        return show(request, "fms_messages.html", "messages", f, cases=cases, picked=picked, opened=opened, waiting=waiting, part="list")

    @app.get("/messages/try", include_in_schema=False)
    def try_message(request: Request):
        f = Facility(fd, need(request))
        return show(request, "fms_messages.html", "messages", f, tenants=fd.tenants(), part="try")

    @app.get("/units", include_in_schema=False)
    def units(request: Request):
        ident = need(request)
        f = Facility(fd, ident)
        return show(request, "fms_units.html", "units", f, rows=f.units(), occupancy=f.occupancy(), token=secrets.token_hex(6), table_only=True,
                    until_min=f.today.isoformat(), until_max=(f.today + timedelta(days=366)).isoformat(), step_note=plain(fd.take_step_note(ident)))

    @app.get("/tenants", include_in_schema=False)
    def tenants(request: Request, q: str = "", status: str = ""):
        f = Facility(fd, need(request))
        q = q.strip()[:60]
        digits = re.sub(r"\D", "", q)
        everyone = sorted(f.people.values(), key=lambda p: p["name"])
        kinds = {"renting": lambda p: p["active"], "past": lambda p: p["past"] > 0, "dispute": lambda p: p["dispute"],
                 "credit": lambda p: p["credit"] > 0, "out": lambda p: not p["active"]}
        counts = {k: sum(1 for p in everyone if test(p)) for k, test in kinds.items()}
        people = [p for p in everyone if kinds[status](p)] if status in kinds else everyone
        if q:
            people = [p for p in people if q.lower() in p["name"].lower() or q.upper() in p["units"] or (len(digits) >= 3 and digits in re.sub(r"\D", "", p["phone"]))]
        return show(request, "fms_tenants.html", "tenants", f, people=people, q=q, status=status if status in kinds else "", counts=counts,
                    total=len(everyone))

    @app.get("/tenants/{key}", include_in_schema=False)
    def tenant(request: Request, key: str):
        ident = need(request)
        f = Facility(fd, ident, everything=True)
        p = f.by_slug.get(key)
        if p is None:
            raise HTTPException(404, "no such tenant")
        vacant = [u for u in sorted(f.b.units) if not reconcile.readiness(f.b, f.rent, u, f.now) and f.rent.units.get(u) and f.rent.units[u].rent_cents]
        return show(request, "fms_tenant.html", "tenants", f, p=p, rentals=f.rentals(p["id"]), ledger=f.ledger(p["id"]), vacant=vacant,
                    cases=[c for c in f.cases() if c["who"] and c["who"]["id"] == p["id"]],
                    disputes=[d for d in f.disputes() if d["who"]["id"] == p["id"]], col=f.collections(p["id"]),
                    renting=f.renting_of(p["id"]), prot=f.protection_of(p["id"]), lien_files=f.lien_files(p["id"]), manager=STAFF == lien_core.APPROVER, lien_kind_words=lien_core.KIND_WORDS, stop_words=lien_core.STOP_WORDS, surplus_words={"created": "The tenant has not yet been notified.", "held": "The tenant has been notified; the surplus is held.", "payout_requested": "Simulated payout requested.", "paid": "Simulated payout completed."}, token=secrets.token_hex(6), step_note=plain(fd.take_step_note(ident)),
                    note_at=request.query_params.get("at", ""))

    @app.post("/tenants/{key}/renting", include_in_schema=False)
    async def tenant_renting(request: Request, key: str):
        same_site(request)
        ident = need(request)
        data = await form(request)
        action, token = data.get("action", ""), data.get("token", "")
        actions = ("request_move_out", "review_move_out", "final_bill", "request_transfer", "approve_transfer", "complete_transfer", "confirm_access_cleared")
        if action not in actions or not re.fullmatch(r"[0-9a-f]{12}", token):
            raise HTTPException(422, "Reload the page and try again.")
        f = Facility(fd, ident)
        person = f.by_slug.get(key)
        rental = f.rent.rentals.get(data.get("rental", ""))
        if not person or not rental or rental.tenant != person["id"]:
            raise HTTPException(404, "No such rental for this tenant.")
        about, args = rental.id, {}
        if action in ("request_move_out", "request_transfer"):
            field = "on" if action == "request_move_out" else "start"
            try:
                args[field] = date.fromisoformat(data.get(field, "")).isoformat()
            except ValueError:
                raise HTTPException(422, "Choose a date.") from None
            if action == "request_transfer":
                args["unit"] = data.get("target", "")
        elif action in ("review_move_out", "final_bill"):
            about = rental.moveout or ""
        elif action in ("approve_transfer", "complete_transfer"):
            transfer = next((t for t in f.rent.transfers.values() if t.rental == rental.id and t.state != "cancelled"), None)
            about = transfer.id if transfer else ""
        await run_in_threadpool(fd.staff_rent, ident, renting.Act(f"form:{token}:{action}:{rental.id}", action, STAFF, about, args=args))
        return RedirectResponse(f"/tenants/{key}?at=renting#renting", status_code=303)

    @app.post("/tenants/{key}/protection", include_in_schema=False)
    async def tenant_protection(request: Request, key: str):
        """A staff protection step on one tenant's page, through the one protection gate on this session's copy. The tenant comes from the
        address and every lease, policy or plan named must be theirs. The page's token makes a second press return the first decision, and a
        refusal is said once at the section."""
        same_site(request)
        ident = need(request)
        data = await form(request)
        action, token = data.get("action", ""), data.get("token", "")
        if action not in PROTECTION_FORMS or not re.fullmatch(r"[0-9a-f]{12}", token):
            raise HTTPException(422, "Reload the page and try again.")
        f = Facility(fd, ident)
        person = f.by_slug.get(key)
        if person is None:
            raise HTTPException(404, "no such tenant")
        st = protection.state(f.b)
        lease, plan, policy = (data.get(k, "")[:20] for k in ("lease", "plan", "policy"))
        owned = {x.id for x in f.leases_of.get(person["id"], [])}
        if (lease and lease not in owned) or (plan and getattr(st.enrolments.get(plan), "lease", None) not in owned) or \
                (policy and getattr(st.evidence.get(policy), "lease", None) not in owned):
            raise HTTPException(404, "No such record for this tenant.")
        text = lambda k, n=300: " ".join(data.get(k, "").split())[:n]
        number = lambda k: int(data[k]) if data.get(k, "").isdecimal() else None
        if action == "record_policy":
            act = protection.Act(f"form:{token}:{action}:{lease}", "record_policy_evidence", STAFF, lease=lease, text=text("statement"),
                                 form={k: text(k, 120) for k in ("carrier", "number", "effective", "expires", "document")})
        elif action == "review_policy":
            checked = [c for c in protection.REVIEW_CHECKS if data.get("check_" + c.replace(" ", "_"))]
            act = protection.Act(f"form:{token}:{action}:{policy}", "verify_policy", STAFF, about=policy, text=text("note"),
                                 form={"outcome": text("outcome", 12), "how": text("how"), "checked": checked})
        elif action == "offer_plan":
            act = protection.Act(f"form:{token}:{action}:{lease}", "offer_plan", STAFF, lease=lease, form={"replaces": plan or None})
        elif action == "record_acceptance":
            act = protection.Act(f"form:{token}:{action}:{plan}", "record_acceptance", STAFF, about=plan, version=number("version"), cents=number("cents"),
                                 form={"how": text("how", 40), "words": text("words"), "starts": text("starts", 10)})
        elif action == "enroll_plan":
            act = protection.Act(f"form:{token}:{action}:{plan}", "enroll_plan", STAFF, about=plan)
        else:
            act = protection.Act(f"form:{token}:{action}:{plan}", "end_plan", STAFF, about=plan, form={"on": text("on", 10), "reason": text("reason", 40)})
        await run_in_threadpool(fd.staff_protection, ident, act)
        return RedirectResponse(f"/tenants/{key}?at=protection#protection", status_code=303)

    @app.get("/billing", include_in_schema=False)
    def billing(request: Request, month: str = ""):
        f = Facility(fd, need(request))
        months = f.months()
        key = month if month in dict(months) else month_key(f.today)
        return show(request, "fms_billing.html", "billing", f, months=months, month=f.month(key), past=f.past_due(), only_month=True)

    @app.get("/collections", include_in_schema=False)
    def collections(request: Request):
        f = Facility(fd, need(request))
        return show(request, "fms_billing.html", "collections", f, months=f.months(), month=f.month(month_key(f.today)), past=f.past_due(), only_past=True, title="Collections")

    @app.get("/sitemap", include_in_schema=False)
    def sitemap(request: Request):
        ident = need(request)
        f = Facility(fd, ident)
        return show(request, "fms_units.html", "sitemap", f, rows=f.units(), occupancy=f.occupancy(), token=secrets.token_hex(6), map_only=True,
                    title="Site map", until_min=f.today.isoformat(), until_max=(f.today + timedelta(days=366)).isoformat(), step_note="")

    @app.get("/rentals", include_in_schema=False)
    def rentals(request: Request, status: str = ""):
        f = Facility(fd, need(request))
        rent = f.rent
        filters = (("", "All rentals"), ("active", "Active"), ("reservations", "Reservations"),
                   ("transfers", "Transfers"), ("moveouts", "Move-outs"), ("ended", "Ended"), ("attention", "Needs attention"))
        status = status if status in dict(filters) else ""
        tenants = list(dict.fromkeys([r.tenant for r in rent.reservations.values()] + [r.tenant for r in rent.rentals.values()]))
        rows = []
        for tenant in tenants:
            for x in f.renting_of(tenant):
                r = x.get("rental")
                transfer = x.get("transfer") or (rent.transfers.get(r.transfer) if r and r.transfer else None)
                needs = bool(r and any(t.open and t.about in (r.id, r.access) for t in rent.tasks.values()))
                matches = {"active": bool(r and r.state == "active"), "reservations": not r or bool(r and r.state in renting.WAITING and not r.transfer),
                           "transfers": bool(transfer and transfer.state not in ("completed", "cancelled")),
                           "moveouts": bool(x.get("moveout") and x["moveout"].state != "completed"),
                           "ended": bool(r and r.state in ("ended", "cancelled")), "attention": needs}
                if status and not matches[status]:
                    continue
                lease = f.b.leases.get(r.lease or "") if r else None
                cells = (f.people[tenant]["name"], x["unit"], x["where"][0], money(lease.rent_cents) if lease else "-",
                         money(f.b.receivable(lease.id)) if lease else "-", x["agreement"][0] if x["agreement"] else "-",
                         x["gate"][0] if x["gate"] else "-", f.protection_word(lease.id) if lease else "-")
                rows.append({"cells": cells, "slug": f.people[tenant]["slug"]})
        return show(request, "fms_list.html", "rentals", f, title="Rentals", heads=("Tenant", "Unit", "Where it stands", "Monthly rent", "Balance", "Agreement", "Gate code", "Protection"), rows=rows,
                    filters=filters, selected=status, lede="Rentals and reservations, with their agreement and gate code records.",
                    empty="No rentals match this view.")

    @app.get("/walkthrough", include_in_schema=False)
    def walkthrough(request: Request):
        ident = need(request)
        f = Facility(fd, ident)
        floor = f.rent.floor
        walks = sorted(floor.walks.values(), key=lambda w: (w.started, w.id), reverse=True)
        orders = sorted(floor.orders.values(), key=lambda o: (o.state == "closed", -o.reported.timestamp(), o.id))
        groups = {}
        for unit in sorted(f.b.units):
            groups.setdefault(unit[0], []).append(unit)
        def staff(source):
            if not source:
                return "Not assigned"
            return source.removeprefix("staff:").removeprefix("person:").replace("_", " ").replace(".", " ").capitalize()
        moves = {before: (action, after) for action, (_, before) in walk.MOVES.items() for after in (walk.NEXT[before],)}
        found = reconcile.compare(f.b, f.rent, f.now)
        reports = {}
        for x in found:                 # a report read before the unit's records changed is marked so, never shown as current
            rows = floor.investigations.get(x.key)
            if rows:
                reports[x.key] = walk_report(rows[-1])
                now_texts = [h["text"] for h in walk_reading.build_history(f.b, f.rent, x, f.now, "page")]
                reports[x.key]["stale"] = [h["text"] for h in rows[-1].get("history", [])] != now_texts
        same = []                       # two open orders on one unit: code finds the pair, staff decide, Tilden may read them
        for first, second in walk.pairs(floor):
            rows = floor.comparisons.get(f"{first.id}:{second.id}")
            reading = order_reading(rows[-1]) if rows else None
            if reading is not None:
                now_texts = [r["text"] for r in walk_reading.order_records(first, second, "page")]
                reading["stale"] = [r["text"] for r in rows[-1].get("records", [])] != now_texts
            same.append({"first": first, "second": second, "reading": reading})
        return show(request, "fms_walk.html", "walkthrough", f, walks=walks, orders=orders, same=same, order_of=floor.orders,
                    findings=found, reports=reports, groups=groups, moves=moves,
                    roles=("front_desk", "office_manager", "manager"), staff=staff, clock=clock,
                    lock=walk.LOCK, whose=walk.WHOSE, tag=walk.TAG, door=walk.DOOR, contents=walk.CONTENTS, note_max=work.NOTE_MAX,
                    token=secrets.token_hex(6), step_note=plain(fd.take_step_note(ident)), note_at=request.query_params.get("at", ""))

    @app.post("/walkthrough", include_in_schema=False)
    async def walkthrough_step(request: Request):
        same_site(request)
        ident = need(request)
        data = await form(request)
        action, token = data.get("action", ""), data.get("token", "")
        if action not in walk.ACTIONS:
            raise HTTPException(422, "That is not a step staff can take here.")
        if not re.fullmatch(r"[0-9a-f]{12}", token):
            raise HTTPException(422, "Reload the page and try again.")
        section = "orders" if action in walk.MOVES or action in ("join_work_order", "keep_work_orders_apart") or (action == "report_work_order" and data.get("about")) else "staff-forms"
        # the same form sent twice (a double click, a resend) gets its first decision back; another form or other words is another command
        route = sorted(k[5:] for k, v in data.items() if k.startswith("unit_") and v)
        what = hashlib.sha256("|".join([data.get("text") or "", ",".join(route), *(data.get(k, "") for k in ("lock", "whose", "tag", "door", "contents", "to", "into"))]).encode()).hexdigest()[:10]
        act = walk.Act(f"form:{token}:{action}:{data.get('about') or '-'}:{data.get('unit') or '-'}:{what}", action, STAFF, about=data.get("about") or None,
                       unit=data.get("unit") or None, route=[k[5:] for k, v in data.items() if k.startswith("unit_") and v],
                       form={k: data.get(k, "") for k in ("lock", "whose", "tag", "door", "contents") if k != "contents" or data.get(k)},
                       text=data.get("text"), to=data.get("to") or None, into=data.get("into") or None)
        def take_step():
            with fd.lock:
                decision = walk.run(fd.ready(), fd.stream(ident), act, fd.clock())
                if decision.allowed:
                    fd._step_notes.pop(ident, None)
                else:
                    # The gate can name a record in a refusal; the page identifies it by its unit instead.
                    reason = re.sub(r"\s*\((?:W|O|WO)\d+\)", "", decision.check)
                    fd._keep(fd._step_notes, ident, reason)
        await run_in_threadpool(take_step)
        return RedirectResponse(f"/walkthrough?at={section}#{section}", status_code=303)

    pricing.mount(app, fd, need, same_site, form, show, Facility, STAFF)

    @app.get("/shop", include_in_schema=False)
    def shop_page(request: Request):
        ident = need(request)
        fd.sync_work(ident)         # the items the shop records open are kept before the page reads them
        f = Facility(fd, ident)
        note, partial = fd.take_shop_note(ident)
        return show(request, "fms_shop.html", "shop", f, shop=f.shop_page(), cards=SHOP_CARDS, token=secrets.token_hex(6),
                    step_note=plain(note), step_partial=partial, note_at=request.query_params.get("at", ""), note_max=work.NOTE_MAX)

    @app.post("/shop", include_in_schema=False)
    async def shop_step(request: Request):
        """A staff shop step, through the shop's one staff command gate on this session's copy. The page's token makes a second press
        return the first decisions, and a refusal is said once at the part of the page that asked."""
        same_site(request)
        ident = need(request)
        data = await form(request)
        action, token = data.get("action", ""), data.get("token", "")
        if action not in SHOP_FORMS:
            raise HTTPException(422, "That is not a step staff can take here.")
        if not re.fullmatch(r"[0-9a-f]{12}", token):
            raise HTTPException(422, "Reload the page and try again.")
        text = lambda k, n=200: " ".join(data.get(k, "").split())[:n]
        whole = lambda k: int(data[k]) if data.get(k, "").strip().isdecimal() else None
        def cents(k):               # dollars typed as 12 or 12.50, kept in cents; anything else is left for the gate to refuse
            try:
                value = Decimal(data.get(k, "").strip().lstrip("$")) * 100
                return int(value) if value >= 0 and value == int(value) else None
            except ArithmeticError:
                return None
        about = text("about", 120)
        # a product is named exactly as it is stored when it is chosen; only a name being created is tidied (spaces collapsed)
        product = text("product", 80) if action == "add_product" else data.get("product", "")
        # the same form sent twice gets its first decisions back; other words or numbers are another command
        what = hashlib.sha256(json.dumps([[k, data.get(k, "")] for k in sorted(data) if k not in ("token", "action")]).encode()).hexdigest()[:10]
        base = f"form:{token}:{action}:{what}"
        held = lambda: shop.state(fd.view(ident)[0])

        def go() -> str:
            if action in ("change_price", "add_product"):
                latest = held().versions.get(product, [{}])[-1]
                fd.staff_shop(ident, shop.Act(base, "publish_product", STAFF, product, {
                    "price": cents("price"), "cost": cents("cost"), "tax_class": latest.get("tax_class", "standard") if action == "change_price" else text("tax_class", 40),
                    "active": data.get("active") == "yes" if action == "change_price" else True}))
            elif action == "expect_delivery":
                fd.staff_shop(ident, shop.Act(base, "record_delivery", STAFF, None, {"product": product, "quantity": whole("quantity"), "reason": text("reason")}))
            elif action == "receive_delivery":
                delivery = held().deliveries.get(about)
                fd.staff_shop(ident, shop.Act(base, "record_receipt", STAFF, None, {"product": delivery["product"] if delivery else "", "quantity": whole("quantity"),
                                                                                           "reason": text("reason"), "delivery": about}),
                              lambda d: shop.Act(base + ":post", "receive_stock", STAFF, d.made),
                              "The delivery receipt was saved, but it is not added to stock yet:")
            elif action == "post_receipt":
                fd.staff_shop(ident, shop.Act(base, "receive_stock", STAFF, about))
            elif action == "give_away":
                quantity = whole("quantity")
                fd.staff_shop(ident, shop.Act(base, "record_handover", STAFF, None, {"product": product, "quantity": quantity, "reason": text("reason"),
                                                                                            "approval": text("approval"), "handover": text("handover")}),
                                     lambda d: shop.Act(base + ":post", "adjust_stock", STAFF, product, {"kind": "give-away", "quantity": -quantity, "reason": text("reason"), "handover": d.made}),
                                     "The give-away was saved, but it is not taken out of stock yet (use the button on its line once stock allows it):")
            elif action == "post_give_away":
                handover = held().handovers.get(about)
                fd.staff_shop(ident, shop.Act(base, "adjust_stock", STAFF, handover["product"] if handover else "", {
                    "kind": "give-away", "quantity": -handover["quantity"] if handover else 0, "reason": handover["reason"] if handover else "", "handover": about}))
            elif action == "record_count":
                fd.staff_shop(ident, shop.Act(base, "record_count", STAFF, product, {"quantity": whole("quantity")}))
            elif action in ("review_count", "ask_recount", "post_difference"):
                count = held().counts.get(about)
                reason = text("reason")
                if count is None:
                    fd.staff_shop(ident, shop.Act(base, "review_count", STAFF, about, {"reason": reason}))
                elif action == "post_difference":
                    fd.staff_shop(ident, shop.Act(base, "adjust_stock", STAFF, count["product"], {"kind": "count variance", "quantity": count["variance"],
                                                                                                        "reason": (count["review"] or {}).get("reason", reason), "count": about}))
                elif action == "ask_recount":
                    # asking for a recount needs no findings (the button skips the box); the record still says what was asked
                    fd.staff_shop(ident, shop.Act(base, "review_count", STAFF, about, {"reason": reason or "Recount asked for", "recount": True}))
                else:
                    fd.staff_shop(ident, shop.Act(base, "review_count", STAFF, about, {"reason": reason}),
                                         lambda d: shop.Act(base + ":post", "adjust_stock", STAFF, count["product"], {"kind": "count variance", "quantity": count["variance"],
                                                                                                                       "reason": reason, "count": about}),
                                         "Your review of the count was saved, but its difference is not posted to stock:")
            elif action == "sell":
                card = data.get("card", "")
                if card not in dict(SHOP_CARDS):
                    raise HTTPException(422, "Choose a card.")
                try:
                    shown = json.loads(data.get("shown", "{}"))
                except ValueError:
                    shown = {}
                price = shown.get(product) if isinstance(shown, dict) else None
                fd.shop_counter_sale(ident, shop.Act(base, "quote_sale", STAFF, product, {"quantity": whole("quantity"), "price": price}), card)
            elif action == "post_sale":
                fd.staff_shop(ident, shop.Act(base, "complete_sale", STAFF, about))
            elif action == "record_return":
                fd.staff_shop(ident, shop.Act(base, "record_return", STAFF, about, {"quantity": whole("quantity"), "condition": text("condition", 20), "reason": text("reason")}),
                                     lambda d: shop.Act(base + ":post", "return_stock", STAFF, d.made),
                                     "The return was saved, but it is not back in stock yet:")
            elif action == "post_return":
                fd.staff_shop(ident, shop.Act(base, "return_stock", STAFF, about))
            elif action == "retry_payment":
                card = data.get("card", "")
                if card not in dict(SHOP_CARDS):
                    raise HTTPException(422, "Choose a card.")
                fd.shop_retry_payment(ident, shop.Act(base, "retry_payment", STAFF, about), card)
            elif action == "resume_payment":
                fd.shop_resume_payment(ident, shop.Act(base, "resume_payment", STAFF, about, {"key": text("key", 200)}))
            elif action == "refund_payment":
                fd.staff_shop(ident, shop.Act(base, "refund_payment", STAFF, about, {"key": text("key", 200), "reason": text("reason")}))
            elif action == "cancel_sale":
                fd.staff_shop(ident, shop.Act(base, "cancel_sale", STAFF, about, {"reason": text("reason")}))
            else:                       # write_off_payment
                fd.staff_shop(ident, shop.Act(base, "write_off_payment", STAFF, about, {"reason": text("reason")}))
            return SHOP_FORMS[action]

        section = await run_in_threadpool(go)
        return RedirectResponse(f"/shop?at={section}#{section}", status_code=303)

    @app.get("/liens", include_in_schema=False)
    def lien_cases(request: Request):
        from core import liens as lien_core
        f = Facility(fd, need(request))
        cases = lien_core.state(f.b).cases.values()
        rows = [{"slug": f.people[c.tenant]["slug"], "cells": (f.people[c.tenant]["name"], c.unit, item["where"], (f"{len(item['missing'])} records missing" if len(item["missing"]) > 1 else upfirst(item["missing"][0]) if item["missing"] else "Nothing missing"), item["next"])} for c in cases if not c.closed for item in f.lien_files(c.tenant) if item["case"].id == c.id]
        return show(request, "fms_list.html", "liens", f, title="Lien cases", heads=("Tenant", "Unit", "Where the file stands", "What it still needs", "Next step"), rows=rows, lien_list=True,
                    lede="Open lien files: evidence, the frozen ledger copy, approvals and legal stops.", empty="No lien case is open.")

    @app.get("/gate", include_in_schema=False)
    def gate_access(request: Request):
        f = Facility(fd, need(request))
        rent = f.rent
        tenants = list(dict.fromkeys(r.tenant for r in rent.rentals.values()))
        rows = [(f.people[t]["name"], x["unit"], x["gate"][0]) for t in tenants for x in f.renting_of(t) if x["gate"]]
        return show(request, "fms_list.html", "gate", f, title="Gate access", heads=("Tenant", "Unit", "Gate code"), rows=rows,
                    lede="Gate codes set for rentals, each counted only once it was read back from the gate system.",
                    empty="No gate code has been set on this copy yet.")

    @app.get("/rules", include_in_schema=False)
    def rules(request: Request, stale: int = 0):
        ident = need(request)
        f = Facility(fd, ident)
        return show(request, "fms_rules.html", "rules", f, stale=bool(stale), note=plain(fd.take_note(ident)), token=secrets.token_hex(6), today_iso=f.today.isoformat(),
                    **f.rules())

    # writes ------------------------------------------------------------------------------------------------------------
    @app.post("/api/fms/messages")
    def run_message(body: CaseIn, request: Request, ident: str = Depends(need)):
        """A tenant's message through the agent on this session's copy: this morning's messages, or one typed in Try a tenant message."""
        same_site(request)
        if not live:
            return fd.run(ident, body, live)

        def budget_left() -> float:
            with session_factory() as s:
                return settings.daily_budget_usd - spend.spent_today(s)
        return fd.run(ident, body, live, settings.visitor_cases_per_day, budget_left)

    def walk_budget_left() -> float:
        with session_factory() as s:
            return settings.daily_budget_usd - spend.spent_today(s)

    def walk_refusal(exc: HTTPException):
        # The desk explains the refusal; the page does not show daily counts.
        detail = re.sub(r"today's \d+ ", "today's ", str(exc.detail))
        raise HTTPException(exc.status_code, plain(detail)) from None

    @app.post("/api/fms/walk/investigate")
    def investigate_walk(body: WalkInvestigationIn, request: Request, ident: str = Depends(need)):
        same_site(request)
        try:
            result = fd.investigate_finding(ident, body.key, live, walk_budget_left if live else None)
        except HTTPException as exc:
            walk_refusal(exc)
        if result.get("off"):
            return {"reason": plain(result["reason"])}
        return walk_report(result)

    @app.post("/api/fms/walk/orders")
    def open_walk_orders(body: WalkOrdersIn, request: Request, ident: str = Depends(need)):
        """The work a typed note named, opened as work orders after staff ticked them: each through the walk gate, the first refusal
        stops the rest and comes back in plain words. The same text sent again with the same page token returns its first decision."""
        same_site(request)
        if not re.fullmatch(r"[0-9a-f]{12}", body.token):
            raise HTTPException(422, "Reload the page and try again.")
        results, made = [], []
        with fd.lock:
            for text in body.texts[:walk_reading.WORK_MAX]:
                what = hashlib.sha256(text.encode()).hexdigest()[:10]
                d = walk.run(fd.ready(), fd.stream(ident), walk.Act(f"orders:{body.token}:{body.unit}:{what}", "report_work_order", STAFF,
                                                                     unit=body.unit, text=text), fd.clock())
                results.append({"text": plain(text), "allowed": d.allowed, "check": plain(re.sub(r"\s*\((?:W|O|WO)\d+\)", "", d.check))})
                if not d.allowed:
                    break
                made.append(d.made)
            # staff ticked these as separate pieces of work, so they are not offered back as a possible duplicate of each other
            for i, later in enumerate(made):
                for earlier in made[:i]:
                    walk.run(fd.ready(), fd.stream(ident), walk.Act(f"orders-apart:{body.token}:{earlier}:{later}", "keep_work_orders_apart", STAFF,
                                                                    about=later, into=earlier, text="Opened as separate work from one walk note"), fd.clock())
        return {"results": results}

    @app.post("/api/fms/walk/compare")
    def compare_walk_orders(body: WalkCompareIn, request: Request, ident: str = Depends(need)):
        same_site(request)
        try:
            result = fd.compare_orders(ident, body.first, body.second, live, walk_budget_left if live else None)
        except HTTPException as exc:
            walk_refusal(exc)
        if result.get("off"):
            return {"reason": plain(result["reason"])}
        return order_reading(result)

    @app.post("/api/fms/walk/note")
    def read_walk(body: WalkNoteIn, request: Request, ident: str = Depends(need)):
        same_site(request)
        try:
            # Tilden drafts and staff record: a quoted word can exist and still not mean the value read from it, so the page fills the
            # form and staff press Record
            result = fd.read_walk_note(ident, body.walk, body.unit, body.text, live, walk_budget_left if live else None, source=STAFF, record=False)
        except HTTPException as exc:
            walk_refusal(exc)
        reading = result.get("reading")
        fields = {name: {"value": plain(value if value != "unknown" else "not said"), "raw": value,
                         "words": plain(reading.quotes.get(name))} for name, value in reading.fields.items()} if reading else {}
        work = [{"kind": walk_reading.WORK[w["kind"]], "words": plain(w["words"])} for w in reading.work] if reading else []
        return {"recorded": result["recorded"], "reason": plain(result.get("reason")), "fields": fields, "unit": reading.unit if reading else None,
                "work": work, "request": bool(reading and "staff_request" in reading.flags)}

    @app.post("/api/fms/rules")
    def propose_rule(body: RuleIn, request: Request, ident: str = Depends(need)):
        """An owner sentence read into a proposed rule on this session's copy: what it says, what changes, last year replayed."""
        same_site(request)

        def budget_left() -> float:
            with session_factory() as s:
                return settings.daily_budget_usd - spend.spent_today(s)
        pid, p, effect = fd.rule_proposal(ident, body.sentence, live, budget_left if live else None)
        f = Facility(fd, ident)
        names = {t: f.people[t]["name"] for t in f.people}
        changed = lambda rows: [{"who": names.get(r["tenant"], ""), "due": day(date.fromisoformat(r["due"])), "asked": day(date.fromisoformat(r["asked"])),
                                 "before": r["old"], "after": r["new"]} for r in rows[:8]]
        return {"id": pid, "outcome": p.outcome, "kind": p.kind, "words": p.words, "reason": p.reason, "not_expressed": p.not_expressed,
                "quotes": [q["words"] for q in p.quotes], "cents": round(p.call.cents, 4) if p.call else 0,
                "replay": {"fees": effect["fees"], "before": effect["old"], "after": effect["new"], "now_allowed": len(effect["to_allowed"]),
                           "now_refused": len(effect["to_refused"]), "allowed": changed(effect["to_allowed"]), "refused": changed(effect["to_refused"])}
                if effect else None}

    @app.post("/rules/approve", include_in_schema=False)
    async def approve_rule(request: Request):
        """The visitor approves the proposal they were shown, as the owner, on this session's copy only."""
        same_site(request)
        ident = need(request)
        data = await form(request)
        try:
            await run_in_threadpool(fd.approve_rule, ident, data.get("proposal", "")[:20])
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            return RedirectResponse("/rules?stale=1#h-write", status_code=303)
        return RedirectResponse("/rules#in-force", status_code=303)

    @app.post("/rules/restore", include_in_schema=False)
    async def restore_rule(request: Request):
        """The visitor, as the owner, goes back to an earlier version of a rule on this session's copy only."""
        same_site(request)
        ident = need(request)
        data = await form(request)
        version = data.get("version", "")[:4]
        try:
            await run_in_threadpool(fd.restore_rule, ident, data.get("rule", "")[:40], int(version) if version.isdecimal() else 0)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            return RedirectResponse("/rules#in-force", status_code=303)     # the page says why nothing changed
        return RedirectResponse("/rules#in-force", status_code=303)

    @app.post("/collections/acknowledge", include_in_schema=False)
    async def acknowledge(request: Request):
        """Staff saw a collections task; it leaves Today. A second press changes nothing."""
        same_site(request)
        ident = need(request)
        data = await form(request)
        await run_in_threadpool(fd.acknowledge, ident, data.get("task", "")[:12], STAFF)
        return RedirectResponse("/today#collections", status_code=303)

    @app.post("/tenants/{key}/collections", include_in_schema=False)
    async def collections_step(request: Request, key: str):
        """A staff step on one tenant's collections, through the gate on this session's copy: record or lift an overlock, reinstate
        or end a breached plan, grant or revoke an exemption. The tenant comes from the address, never the form; the gate checks the
        unit, plan or exemption belongs to them. The page's token makes a second press return the first decision."""
        same_site(request)
        ident = need(request)
        data = await form(request)
        action, token = data.get("action", ""), data.get("token", "")
        if action not in STEP_FORMS:
            raise HTTPException(422, "That is not a step staff can take here.")
        if not re.fullmatch(r"[0-9a-f]{12}", token):
            raise HTTPException(422, "Reload the page and try again.")
        p = Facility(fd, ident).by_slug.get(key)
        if p is None:
            raise HTTPException(404, "no such tenant")
        reason = " ".join(data.get("reason", "").split())[:300]
        cents = dollars(data.get("amount", "")) if action == "take_payment" else None
        if action == "take_payment" and cents is None:
            raise HTTPException(422, "Type the amount in dollars and cents, for example 25.00.")
        typed = {"take_payment": {"cents": cents, "card": data.get("card", "")[:12]},
                 "review_plan": {"outcome": data.get("outcome", "")},
                 "grant_exemption": {"scope": [s for s in col.STEPS if data.get(f"scope_{s}")], "reason": reason, "until": data.get("until", "")[:10]},
                 "revoke_exemption": {"exemption": data.get("exemption", "")[:12]}}.get(action)
        if action == "review_plan" and len(reason) < 3:
            raise HTTPException(422, "Write what you agreed with the tenant, in a few words.")
        target = (data.get("lease") or data.get("plan") or data.get("exemption") or "")[:12]
        if action == "review_plan":
            target += ":" + typed["outcome"][:10]           # Reinstate and End on one page are two different decisions
        cmd = Command(f"form:{token}:{action}:{target}", action, p["id"], STAFF, lease=data.get("lease", "")[:12] or None,
                      plan=data.get("plan", "")[:12] or None, form=typed, reason=reason or None)
        await run_in_threadpool(fd.take_card_payment if action == "take_payment" else fd.staff_step, ident, cmd)
        if data.get("next") == "today" and action in ("overlock", "lift_overlock"):
            return RedirectResponse("/today#overlocks", status_code=303)       # pressed on Today's list: back to the list
        return RedirectResponse(f"/tenants/{key}#collections", status_code=303)

    @app.post("/tenants/{key}/lien", include_in_schema=False)
    async def lien_step(request: Request, key: str):
        same_site(request)
        ident = need(request)
        data = await form(request)
        action, token = data.get("action", ""), data.get("token", "")
        allowed = ("flag_lien_eligible", "open_lien_case", "record_lien_address", "prepare_lien_notice",
                   "approve_lien_case", "record_lien_stop", "resolve_lien_stop", "list_lien_auction", "cancel_lien_auction")
        if action not in allowed:
            raise HTTPException(422, "That is not a step staff can take here.")
        if not re.fullmatch(r"[0-9a-f]{12}", token):
            raise HTTPException(422, "Reload the page and try again.")
        p = Facility(fd, ident).by_slug.get(key)
        if p is None:
            raise HTTPException(404, "no such tenant")
        lease = data.get("lease", "")[:20]
        typed = {k: data.get(k, "")[:300].strip() for k in ("case", "address", "basis", "kind", "detail", "source", "stop", "fingerprint")}
        reason = " ".join(data.get("reason", "").split())[:300]
        cmd = Command(f"form:{token}:{action}:{lease}", action, p["id"], STAFF, lease=lease, form=typed, reason=reason or None)
        await run_in_threadpool(fd.staff_step, ident, cmd)
        return RedirectResponse(f"/tenants/{key}?at=lien#lien", status_code=303)

    @app.post("/units/{unit}/renting", include_in_schema=False)
    async def unit_step(request: Request, unit: str):
        """A staff renting step on one unit, through the gate on this session's copy: price it, hold it for a tenant, block it or unblock
        it. The unit comes from the address, never the form. The page's token makes a second press return the first decision, and a
        refusal is said once on Units."""
        same_site(request)
        ident = need(request)
        data = await form(request)
        action, token = data.get("action", ""), data.get("token", "")
        if action not in UNIT_FORMS:
            raise HTTPException(422, "That is not a step staff can take here.")
        if not re.fullmatch(r"[0-9a-f]{12}", token):
            raise HTTPException(422, "Reload the page and try again.")
        f = Facility(fd, ident)
        if unit not in f.b.units:
            raise HTTPException(404, "no such unit")
        tenant, args = None, {}
        if action == "price_unit":
            cents = dollars(data.get("amount", ""))
            if cents is None:
                raise HTTPException(422, "Type the price in dollars and cents, for example 125.00.")
            priced = f.rent.units.get(unit)
            args = {"rent_cents": cents, "size": priced.size if priced else None}
        elif action == "reserve":
            person = f.tenant_typed(data.get("tenant", "")[:80])
            if person is None:      # a typed name can miss, so it is said on the page like a refusal, not answered with an error
                fd.keep_step_note(ident, "no tenant has that name, or two do; pick the name from the list as it shows, with the phone where two share a name")
                return RedirectResponse("/units#note", status_code=303)
            try:
                last = date.fromisoformat(data.get("until", "")[:10])
            except ValueError:
                raise HTTPException(422, "Pick the last day of the hold.") from None
            if not f.today <= last <= f.today + timedelta(days=366):
                raise HTTPException(422, "The hold can end from today to a year ahead.")
            tenant, args = person["id"], {"until": at_local(last, 23, 59).isoformat()}
        elif action == "block_unit":
            reason = " ".join(data.get("reason", "").split())[:300]
            if len(reason) < 3:
                raise HTTPException(422, "Write why the unit is blocked, in a few words.")
            args = {"reason": reason}
        done = await run_in_threadpool(fd.staff_rent, ident, renting.Act(f"form:{token}:{action}:{unit}", action, STAFF, unit, tenant, args))
        return RedirectResponse(f"/units#u-{unit}" if done else "/units#note", status_code=303)     # a refusal lands where the note is

    @app.post("/rules/plan-policy", include_in_schema=False)
    async def plan_policy(request: Request):
        """The owner adopts a new version of the plan policy on this session's copy; outside the fixed limits nothing changes."""
        same_site(request)
        ident = need(request)
        values, why = bounded(await form(request), POLICY_FORM, col.POLICY_BOUNDS)
        done = await run_in_threadpool(fd.owner_setting, ident, "policy", values, why)
        return RedirectResponse("/rules#plan-policy" if done else "/rules#in-force", status_code=303)     # a refusal is said above the panels

    @app.post("/rules/protection-plan", include_in_schema=False)
    async def protection_plan(request: Request):
        """Staff publish the next version of the protection plan on this session's copy, through the protection gate. The old versions stay as
        they were; a price or terms the gate refuses change nothing, and Settings says why once above the panels."""
        same_site(request)
        ident = need(request)
        data = await form(request)
        token = data.get("token", "")
        if not re.fullmatch(r"[0-9a-f]{12}", token):
            raise HTTPException(422, "Reload the page and try again.")
        act = protection.Act(f"form:{token}:publish_plan", "publish_plan", STAFF,
                             form={"cents": dollars(data.get("price", "")), "terms": " ".join(data.get("terms", "").split())[:600], "effective": data.get("effective", "")[:10]})
        done = await run_in_threadpool(fd.staff_protection, ident, act, True)
        return RedirectResponse("/rules#protection-plan" if done else "/rules#in-force", status_code=303)

    @app.post("/rules/schedule", include_in_schema=False)
    async def schedule(request: Request):
        """The owner adopts a new version of the schedule on this session's copy; outside the fixed limits nothing changes."""
        same_site(request)
        ident = need(request)
        values, why = bounded(await form(request), SCHEDULE_FORM, col.SCHEDULE_BOUNDS)
        done = await run_in_threadpool(fd.owner_setting, ident, "schedule", values, why)
        return RedirectResponse("/rules#schedule" if done else "/rules#in-force", status_code=303)

    @app.post("/disputes/decide", include_in_schema=False)
    async def decide(request: Request):
        """A staff decision on an open dispute, through the gate, on this session's copy. One decision per dispute and kind: a
        second press gets the first decision back. The facts are checked again inside the write."""
        same_site(request)
        ident = need(request)
        data = await form(request)
        action, dispute, reason = data.get("action", ""), data.get("dispute", ""), " ".join(data.get("reason", "").split())
        if action not in STAFF_ACTIONS:
            raise HTTPException(422, "That is not a decision staff can make here.")
        if not 3 <= len(reason) <= 300:
            raise HTTPException(422, "Write what you checked, in a few words.")

        def act() -> None:
            with fd.lock:
                store = fd.ready()
                stream = fd.stream(ident)
                d = store.books(stream).disputes.get(dispute)
                if d is None:
                    raise HTTPException(404, "There is no such dispute on this copy.")
                execute(store, stream, Command(f"staff:{dispute}:{action}", action, d.tenant, STAFF, d.charge, reason=reason), fd.clock())
        await run_in_threadpool(act)
        return RedirectResponse("/today#decided", status_code=303)
