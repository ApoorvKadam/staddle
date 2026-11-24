"""Our agent working tenant messages on the facility's own books (the core in core/), for the FMS pages in desk/fms.py.

Each signed-in visitor works on a scratch copy of the generated year (their own stream), so one visitor's reversal never changes
what the next one sees. A message is read by our agent; code checks every claim against the records and decides; the gate
records it. Runs count against the visitor's tries and the day's budget."""
from __future__ import annotations

import glob
import logging
import os
import secrets
import threading
from collections import OrderedDict
from dataclasses import asdict
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException
from pydantic import BaseModel, Field

from core import auction, delinquency, protection, reconcile, renting, shop, walk_reading, work
from core import walk as walks          # not `walk`: read_walk_note takes a walk by that name
from core.books import ZONE, at_local, local
from core.billing import Ledger, Refused
from core.case import READ_ONLY, handle
from core.gate import STAFF_RULE, Command, execute
from core.rules import Proposal, compile_rule, replay
from core.cases import load
from core.seed import build
from core.store import BASE, Store

from . import spend

SHIFT = load("shift")["items"]
SWEEP_EVERY = 3600              # seconds between collections passes; each writes only what is new, so a copy started today is swept today
log = logging.getLogger("desk.front_desk")
WORST_CASE_CENTS = 0.1          # the most one message can cost on the live link (one reading call on Haiku, capped)
RULE_WORST_CENTS = 0.2          # the most reading one owner sentence can cost (one call on Haiku, output capped)
RULES_PER_DAY = 3               # owner sentences a visitor may have read in a day, apart from the message tries
RULE_MODEL = ("claude-haiku-5-5", "low")
WALK_MODEL = ("claude-haiku-5-5", "low")      # Tilden on the walk: reading a typed note, and investigating a finding; the graded set decides later
WALK_NOTE_WORST_CENTS = 0.1     # the most reading one typed note can cost (one call on Haiku, output capped at 600 tokens)
INVESTIGATE_WORST_CENTS = 0.3   # the most one investigation can cost (one call on Haiku with the unit's history, output capped at 900 tokens)
WALK_NOTES_PER_DAY = 10         # typed notes a visitor may have read in a day, apart from the message and rule tries
INVESTIGATIONS_PER_DAY = 5      # findings a visitor may have investigated in a day (readings of two work orders count here too)
SAME_WORST_CENTS = 0.2          # the most reading two work orders can cost (one call on Haiku, output capped at 500 tokens)
KEEP_FOR = timedelta(hours=24)
HOLD_RETRY = 60                 # seconds before a day that cannot leave its waiting payments asks the processor again
ACTION_WORDS = {"explain_charge": "Explained from the records", "reverse_duplicate": "Reversed the second copy",
                "correct_fee_posted_in_error": "Removed as posted in error", "open_dispute": "Dispute recorded for the office manager",
                "waive_fee": "Waived under the owner's rule", "hand_to_staff": "Handed to staff",
                "propose_plan": "Plans worked out under the owner's policy", "offer_plan": "Plans offered to the tenant",
                "accept_plan": "Plan agreed in the tenant's words", "record_promise": "Promise to pay noted"}
CLAIM_WORDS = {"paid_on_time": "says they paid in time", "charged_twice": "says a charge appears twice", "never_agreed": "says they never agreed to it",
               "challenges_record": "challenges a record", "wants_waiver": "asks for it to be waived", "wrong_amount": "says the amount is wrong",
               "what_is_this": "asks what it is", "payment_issue": "asks about their card payments", "other": "says something else"}


def account_words(account: str, books) -> str:
    kind, _, ident = account.partition(":")
    if kind == "receivable":
        return f"Owed by the tenant, unit {books.leases[ident].unit}"
    if kind == "credit":
        return "Held for the tenant as credit"
    if kind == "surplus":
        return "Owed to the tenant from a sale, not credit"
    return {"income:rent": "Rent income", "income:fees": "Fee income", "income:protection": "Protection plan income", "contra:waivers": "Waivers given",
            "clearing:card": "Card payments on the way", "clearing:auction": "Auction money on the way"}.get(account, account)


class RuleIn(BaseModel):
    sentence: str = Field(min_length=3, max_length=400)


class CaseIn(BaseModel):
    tenant: str = Field(max_length=20)
    text: str = Field(min_length=1, max_length=600)
    shift: str | None = Field(default=None, max_length=8)      # a shift message's id, when the page runs the shift
    run: str | None = Field(default=None, max_length=40)       # one shift run is one try


def as_utc(at: datetime) -> datetime:
    """A naive time here is UTC, as spend.day_start() gives it; astimezone() would read it as the server's local time,
    which on New York time put the day's start at 5 AM and let a repeated press through between midnight and then."""
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)


class FrontDesk:
    def __init__(self, path: str, client=None, clock=None, processor_path: str | None = None, processor=None):
        self.path, self.client = path, client
        # the simulated payment processor keeps its records beside the core's database: one per running app, never in the repository
        if processor_path is None and path != ":memory:":
            processor_path = os.path.join(os.path.dirname(os.path.abspath(path)), "processor.json")
        self.processor = processor or delinquency.SimulatedProcessor(processor_path)
        # the stand-in gate keeps its records there too: a restart finds the codes it made and issues nothing twice. Until a real
        # access system is connected every gate code in a copy is the stand-in's, and its records say so.
        self.access = renting.StandIn(path=None if path == ":memory:" else os.path.join(os.path.dirname(os.path.abspath(path)), "access.json"))
        # the simulated auction platform keeps its listings in a file of its own, cleaned up per copy like the gate's codes
        self.auction = auction.AuctionSite(path=None if path == ":memory:" else os.path.join(os.path.dirname(os.path.abspath(path)), "auction.json"))
        self.clock = clock or (lambda: datetime.now(ZONE))      # the real time in Orlando; tests pass a fixed one
        self.day: date | None = None                            # the day the books were made for
        self.lock = threading.RLock()
        # website checkout forms already answered, by copy and page token, so a second press of one page never pays twice.
        # ponytail: kept in memory; a restart forgets them, and a restart starts every copy afresh anyway
        self.site_forms: set[tuple[str, str]] = set()
        self.store: Store | None = None
        self.cast: dict[str, dict] = {}
        self.reserved = 0.0
        self._year = None
        self._views: OrderedDict[str, tuple] = OrderedDict()
        self._rents: OrderedDict[str, renting.Rent] = OrderedDict()      # stream -> its renting state, folded on the books view() holds
        # ponytail: the last proposal per visitor, in memory; a restart drops it and the owner asks again
        self._proposals: OrderedDict[str, tuple] = OrderedDict()     # visitor -> (id, proposal, sentence, rule versions, day)
        self._resets: dict[str, int] = {}                            # visitor -> how many times their copy was reset
        self._notes: dict[str, str] = {}                             # visitor -> why a going back was refused, shown once on the Rules page
        self._step_saved: set[str] = set()                          # visitors whose kept note follows a record that was saved
        self._step_notes: dict[str, str] = {}                        # visitor -> why a staff step on a tenant was refused, shown once there
        self._reset_notes: dict[str, str] = {}                       # visitor -> why Start over was refused, shown once on Home
        self._hold_until = 0.0                                       # monotonic time before which a held day does not ask the processor again
        self._facts: dict | None = None

    def today(self) -> date:
        return local(self.clock()).date()

    def ready(self) -> Store:
        """The core's database, with the generated year up to this morning in it; a new day makes new books."""
        with self.lock:
            today = self.today()
            if self.store is None or self.day != today:
                if self.store is not None and not self._may_leave_the_day():
                    return self.store       # a payment of the old day is still waiting at the processor: that day stays until it is settled
                self._seed(today)
        return self.store

    # a payment the processor has not settled is never dropped with its copy: Start over, the new day and the cleaning of idle copies each
    # ask the processor first and go ahead only when none is left waiting
    @staticmethod
    def _unresolved(store: Store, stream: str) -> list:
        return [i for i in delinquency.state(store.books(stream)).intents.values() if i.state in delinquency.UNSETTLED]

    def _resolve(self, store: Store, streams: list[str]) -> bool:
        """Ask the processor about each copy's waiting payments: settle what it settled, cancel what it still processes, read again so the
        cancellation is recorded. Stops at the first answer that does not come, since every further copy would wait out its own timeout
        with the lock held. True only when no copy has a waiting payment left, judged by the books and not by what the calls returned.
        ponytail: a processor that never answers again (a revoked key, a payment record deleted there) keeps the copy and the day;
        the error is logged on every attempt, and an operator settles it by hand."""
        now = self.clock()
        for s in streams:
            try:
                delinquency.reconcile(store, s, self.processor, now)
                self._settle_rentals(store, s, now)
                waiting = self._unresolved(store, s)
                if any(i.state == "pending" for i in waiting):
                    delinquency.close_copy(self.processor, s, min(i.created for i in waiting))
                    delinquency.reconcile(store, s, self.processor, now)
                    self._settle_rentals(store, s, now)
            except (delinquency.ProcessorTimeout, delinquency.ProcessorRefused) as exc:
                # the first answer that does not come ends the pass: one wait under the lock per attempt, not one per copy
                log.error("the processor could not settle the payments of %s: %s", s, type(exc).__name__)
                break
            except Exception:           # as sweep() does: a fault in one copy is logged, the copy stays as it is, and no page is taken down
                log.exception("settling the payments of %s failed", s)
        return not any(self._unresolved(store, s) for s in streams)

    def _may_leave_the_day(self) -> bool:
        if time.monotonic() < self._hold_until:         # asked a moment ago and got no answer: pages do not each wait on the processor
            return False
        streams = [s for (s,) in self.store.db.execute("SELECT DISTINCT stream FROM events WHERE stream LIKE 'visitor:%'")]
        waiting = [s for s in streams if self._unresolved(self.store, s)]
        if not waiting or self._resolve(self.store, waiting):
            return True
        self._hold_until = time.monotonic() + HOLD_RETRY
        log.error("the day of %s stays: %d copies still have a payment the processor has not settled", self.day, len(waiting))
        return False

    def _seed(self, today: date) -> None:
        """The year up to midnight today, written into a database of the day's own (seconds on a small instance; desk/api.py
        starts it at boot). Yesterday's file keeps yesterday's visitor copies: the footer says a copy lasts a day."""
        root, ext = os.path.splitext(self.path)
        store = Store(self.path if self.path == ":memory:" else f"{root}-{today.isoformat()}{ext}")
        year = build(today=today, cutoff=at_local(today), standing=True)
        if not store.events(BASE):
            with store.write():
                store.append(BASE, year.led.pending)
        # what each visitor used, kept apart from their copy of the books: Start over removes the copy, never this
        store.db.execute("CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY, at TEXT NOT NULL, visitor TEXT NOT NULL, "
                         "run TEXT NOT NULL, shift TEXT, cents REAL NOT NULL)")
        store.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS usage_once ON usage(visitor, run, shift)")
        self._views.clear()             # event ids start again in the new file, so no cached view may match
        self._rents.clear()
        self._facts = None
        self.cast, self.store, self._year, self.day = year.cast, store, year, today
        # the gate codes the seeded tenants already hold: worked out once from the day's own base records, never saved per code
        self.access.know(store.events(BASE))
        if self.path != ":memory:":     # visitor copies last a day: files older than yesterday's go (a run begun before midnight keeps yesterday's)
            keep = {f"{root}-{d.isoformat()}{ext}" for d in (today, today - timedelta(days=1))}
            def gone() -> list[str]:
                return [f for f in glob.glob(f"{glob.escape(root)}-????-??-??{ext}*") if f.removesuffix("-wal").removesuffix("-shm") not in keep]
            # the gate codes of the copies in a file that goes are noted first, and forgotten once it is gone; the files are listed
            # again after reading them, since opening a database leaves its -wal and -shm files beside it
            prefixes = self._note_copies_of([f for f in gone() if not f.endswith(("-wal", "-shm"))])
            if prefixes is not None:
                for f in gone():
                    os.remove(f)
                for prefix in prefixes:
                    self._forget(prefix)
        self._finish_cleanups()

    def _outside(self) -> tuple:
        """The simulated outside systems whose records are kept per copy: each notes, forgets and releases a copy the same way."""
        return self.access, self.auction

    def _note_copies_of(self, files: list[str]) -> list[str] | None:
        """The gate-code prefix of every visitor copy in these database files, each written to the stand-in's file before the files are
        deleted. None when a file cannot be read or a note cannot be written: nothing is deleted then and the next seed tries again."""
        prefixes = []
        for f in files:
            try:
                prefixes += [m + ":" for m in renting.copy_marks_in(f)]
            except Exception:
                log.exception("the copies in %s could not be read, so their gate codes cannot be named and the old day files stay for now", f)
                return None
        try:
            for system in self._outside():
                for prefix in prefixes:
                    system.note(prefix)
        except Exception:
            log.exception("the gate cleanup could not be noted, so old day files stay for now")
            return None
        return prefixes

    def _forget(self, prefix: str) -> None:
        """Forget a gone copy's codes on the stand-in gate. A failure is logged and the note on file lets a later pass finish it."""
        for system in self._outside():
            try:
                system.forget(prefix)
            except Exception:
                log.exception("forgetting the records of %s failed; a later pass finishes it", prefix)

    def _finish_cleanups(self) -> None:
        """Finish what a stop or a fault left noted: the codes of a copy that is gone are forgotten, and a note for a copy that is still
        here (its drop failed) is cleared with its codes kept."""
        for prefix in sorted({x for system in self._outside() for x in system.pending}):
            alive = self._copy_is_alive(prefix.rstrip(":"))
            if alive is None:
                continue                    # a day file could not be read: the note stays until every file can be checked
            if alive:
                for system in self._outside():
                    try:
                        system.release(prefix)
                    except Exception:
                        log.exception("clearing the gate cleanup note of %s failed", prefix)
            else:
                self._forget(prefix)

    def _copy_is_alive(self, copy: str) -> bool | None:
        """Whether a copy (its name and mark) is still in the current day's database or in any day file that is kept. None when it is in
        none that could be read but a kept file could not be read: it may be there, so nothing is decided."""
        stream = copy.split("@")[0]
        if self.store.db.execute("SELECT 1 FROM events WHERE stream = ? LIMIT 1", (stream,)).fetchone() and renting.copy_mark(self.store, stream) == copy:
            return True
        if self.path == ":memory:":
            return False
        root, ext = os.path.splitext(self.path)
        unread = False
        for f in glob.glob(f"{glob.escape(root)}-????-??-??{ext}"):
            if f.endswith(f"-{self.day.isoformat()}{ext}"):
                continue                    # the current day's file is the store checked above
            try:
                if copy in renting.copy_marks_in(f):
                    return True
            except Exception:
                log.exception("the copies in %s could not be read, so a gate cleanup note stays", f)
                unread = True
        return None if unread else False

    def view(self, visitor: str, everything: bool = False) -> tuple:
        """What the pages read for one copy: its books, its own events, whether every entry balances and the debits, and with
        everything=True all its events, and the copy's merchandise totals up to the same last event. Folded once per change and kept for the last few copies; a page never changes them."""
        with self.lock:
            store = self.ready()
            stream = self.stream(visitor)
            last = store.db.execute("SELECT MAX(id) FROM events WHERE stream IN (?, ?)", (BASE, stream)).fetchone()[0]
            hit = self._views.get(stream)
            if hit is None or hit[0] != last:
                total, debits = store.db.execute("SELECT COALESCE(SUM(cents), 0), COALESCE(SUM(CASE WHEN cents > 0 THEN cents END), 0) "
                                                 "FROM journal_lines WHERE stream IN (?, ?)", (BASE, stream)).fetchone()
                merchandise = dict(store.db.execute("SELECT account, SUM(cents) FROM journal_lines WHERE stream = ? AND event_id <= ? "
                                                    "AND account IN ('income:merchandise', 'cost:merchandise') GROUP BY account", (stream, last)).fetchall())
                hit = (last, store.books(stream), store.stream_events(stream), total == 0, debits, merchandise)
                self._views[stream] = hit
                # ponytail: eight copies held in memory; a shared cache keyed by event id if many visitors read at once
                while len(self._views) > 8:
                    self._views.popitem(last=False)
            self._views.move_to_end(stream)
            return hit[1], hit[2], hit[3], hit[4], (store.events(stream) if everything else []), hit[5]

    def rent_view(self, visitor: str) -> renting.Rent:
        """One copy's renting state, folded once per change and kept for the last few copies like view(). It reads the same folded books
        as view(), so a copy holds one set of books, not two; a page only reads either."""
        with self.lock:
            books = self.view(visitor)[0]
            store, stream = self.ready(), self.stream(visitor)
            hit = self._rents.get(stream)
            if hit is None or hit.books is not books:
                hit = renting.Rent(books)
                for e in store.events(stream):
                    hit.apply(e.kind, e.at, e.payload)
                self._rents[stream] = hit
                while len(self._rents) > 8:
                    self._rents.popitem(last=False)
            self._rents.move_to_end(stream)
            return hit

    def facts(self) -> dict:
        """The facility's own numbers for the Overview, from the base year (which never changes after it is built)."""
        if self._facts is not None and self.day == self.today():     # read without the lock, so the Overview never waits behind a running case
            return self._facts
        with self.lock:
            store = self.ready()
            if self._facts is None:
                b, now = store.books(BASE), self.clock()
                self._facts = {"units": len(b.units), "rented": sum(1 for x in b.leases.values() if x.ended is None), "tenants": len(b.tenants),
                               "leases": len(b.leases), "charges": len(b.charges), "payments": len(b.payments), "declined": len(b.declined),
                               "past_due": sum(1 for t in b.tenants if b.past_due(t, now)), "past_due_cents": sum(b.past_due(t, now) for t in b.tenants)}
            return self._facts

    def sweep(self) -> list[str]:
        """The collections pass over every visitor copy. The processor's events that no webhook brought are delivered first; then
        each copy that has payments is reconciled with the processor (a payment whose answer was lost is settled from its record),
        and its due parts are noticed, missed ones breached with a staff task, promises and exemptions settled. At boot it catches
        up on whatever fell due while nothing ran. Every record is keyed, so a pass with nothing new writes nothing. Returns the
        copies it swept. Each copy then gets the renting pass: a rental waiting on its payment moves with the answer, holds that ran
        out are written down, each gate code is carried forward on the stand-in gate, and each code is checked against its rental (a
        task for one that never read back or that an ended rental still has). Then a policy past its expiry date is marked expired and the
        work queue is checked against all of it."""
        with self.lock:
            store = self.ready()
            self._finish_cleanups()
            for event in self.processor.take_events():
                try:
                    handler = shop.webhook if (event.get('metadata') or {}).get('shop_quote') else delinquency.webhook
                    handler(store, self.processor, event, self.clock())
                except Exception:       # one bad event never stops the others; reconciliation settles it later
                    log.exception("processor event %s failed", event.get("id"))
            streams = [s for (s,) in store.db.execute("SELECT DISTINCT stream FROM events WHERE stream LIKE 'visitor:%' ORDER BY stream")]
        swept = []
        for s in streams:
            with self.lock:         # one copy at a time, so pages wait for one copy's pass at most
                now = self.clock()
                try:                # a fault in one copy never stops the copies after it
                    intents = delinquency.state(store.books(s)).intents
                    if intents:
                        delinquency.reconcile(store, s, self.processor, now)
                    delinquency.sweep(store, s, now)
                    swept.append(s)
                except Exception:
                    log.exception("collections sweep failed for %s", s)
                # renting reads what the pass above settled: a rental waiting on its payment moves with the answer, holds that ran out
                # are written down and each gate code is carried forward, then each code is checked against its rental. Each step is
                # tried alone, so a fault stops neither the steps after it, the collections pass, nor another copy.
                for step in (renting.settle_payments, lambda st, c, n: renting.advance(st, c, self.access, n), renting.check_codes, protection.sweep,
                             lambda st, c, n: shop.reconcile(st, c, self.processor, n), shop.complete_confirmed, shop.sweep, work.sweep):
                    try:
                        step(store, s, now)
                    except Exception:
                        log.exception("renting pass failed for %s", s)
        return swept

    def processor_event(self, event: dict) -> str:
        """A signed processor event on the webhook, applied the way the hourly pass applies one. A payment the processor no longer
        knows is answered as unknown, so it is not sent again."""
        with self.lock:
            store, now = self.ready(), self.clock()
            try:
                handler = shop.webhook if (event.get('metadata') or {}).get('shop_quote') else delinquency.webhook
                result = handler(store, self.processor, event, now)
            except delinquency.ProcessorRefused:
                return "unknown"
            if result == "applied" and handler is delinquency.webhook:  # a rental waiting on its payment moves at once
                stream = event["metadata"]["copy"]
                self._settle_rentals(store, stream, now)
                self._advance(store, stream, now)
            elif result == "applied":                                   # a counter payment that was confirmed late: its sale posts through the same step
                shop.complete_confirmed(store, event["metadata"]["copy"], now)
            return result

    def serve(self) -> None:
        """Run at boot in a thread of its own: build the day's year, catch up, then sweep every SWEEP_EVERY seconds."""
        while True:
            try:
                self.sweep()
            except Exception:       # a failed pass is logged and the next one still runs
                log.exception("collections sweep failed")
            time.sleep(SWEEP_EVERY)

    @staticmethod
    def stream(visitor: str) -> str:
        return f"visitor:{visitor}"

    def spent_dollars(self, since: datetime) -> float:
        """What our agent's front desk calls cost since `since`, plus what a running case has reserved."""
        with self.lock:
            cents = self.ready().db.execute("SELECT COALESCE(SUM(cents), 0) FROM usage WHERE at >= ?",
                                            (as_utc(since).isoformat(),)).fetchone()[0]
        return (cents + self.reserved) / 100

    def runs_today(self, visitor: str) -> dict[str, set]:
        """Today's tries by this visitor, each with the shift messages it ran: a shift is one try, a typed message is one try."""
        since = as_utc(spend.day_start()).isoformat()
        out: dict[str, set] = {}
        rows = self.ready().db.execute("SELECT run, shift FROM usage WHERE visitor = ? AND at >= ? AND (shift IS NULL OR shift NOT IN ('rule', 'walk', 'investigate'))", (visitor, since))
        for run, shift in rows:
            out.setdefault(run, set()).add(shift)
        return out

    def clean(self) -> None:
        """Visitor copies older than a day are removed; the facility's own stream never is."""
        cutoff = (datetime.now(timezone.utc) - KEEP_FOR).isoformat()
        old = [s for (s,) in self.store.db.execute("SELECT stream FROM events WHERE stream != 'base' GROUP BY stream HAVING MAX(recorded_at) < ?", (cutoff,))]
        for s in old:
            if self._unresolved(self.store, s):
                continue                    # a payment the processor has not settled keeps its copy
            mark = renting.copy_mark(self.store, s) + ":"      # named before the copy goes: it is read from the copy's first event
            try:
                for system in self._outside():
                    system.note(mark)           # on file before the drop, so a fault in forgetting is finished later
            except Exception:
                log.exception("the gate cleanup of %s could not be noted, so the copy stays for now", s)
                continue
            self.store.drop_stream(s)
            self._views.pop(s, None)        # event ids can be used again once a copy is gone, so its cached view goes with it
            self._rents.pop(s, None)
            self._forget(mark)              # its codes on the stand-in gate go with it

    def tenants(self) -> list[dict]:
        self.ready()
        b = self._year.led.books
        return [{"role": i["role"], "name": b.tenants[self.cast[i["role"]]["tenant"]].name, "shift": i["id"], "text": self._year.words(i["text"]),
                 "units": [b.leases[l].unit for l in self.cast[i["role"]]["leases"]]} for i in SHIFT]

    def run(self, visitor: str, body: CaseIn, live: bool, tries_per_day: int | None = None, budget_left=None) -> dict:
        with self.lock:
            store = self.ready()
            self.clean()
            if body.tenant not in self.cast:
                raise HTTPException(404, "no such tenant on the page")
            item = next((i for i in SHIFT if i["id"] == body.shift), None)
            if body.shift is not None and (item is None or self._year.words(item["text"]) != body.text or item["role"] != body.tenant):
                raise HTTPException(422, "that is not one of this morning's messages")
            # a run id is honoured only for this morning's messages, each once; a typed message is always a try of its own
            # the New York day is part of a shift's run, so the same page can run the shift again tomorrow
            run = f"{spend.day_start().date()}:{body.run}" if (item is not None and body.run) else secrets.token_hex(6)
            runs = self.runs_today(visitor)
            # a double press or a retry of the same morning message is refused whether or not tries are counted
            if run in runs and body.shift in runs[run]:
                raise HTTPException(409, "That message already ran in this shift. Start a new shift to run it again.")
            if tries_per_day is not None:
                if run not in runs and len(runs) >= tries_per_day:
                    raise HTTPException(429, f"You have used today's {tries_per_day} tries. They come back at midnight, New York time.")
            if budget_left is not None and budget_left() < WORST_CASE_CENTS / 100:
                raise HTTPException(429, "Today's budget for live runs is used up. It comes back at midnight, New York time.")
            # the try is written in the same locked step as the check, so two requests at once cannot both pass it
            usage = store.db.execute("INSERT INTO usage(at, visitor, run, shift, cents) VALUES (?,?,?,?,0)",
                                     (datetime.now(timezone.utc).isoformat(), visitor, run, body.shift)).lastrowid
            stream = self.stream(visitor)
            phone = self.cast[body.tenant]["phone"]
            case = f"{run}:{body.shift or secrets.token_hex(4)}"
            started = time.monotonic()
            self.reserved += WORST_CASE_CENTS
        # ponytail: one lock serializes the core's database, model call included (a case takes a few seconds);
        # per-visitor locks when visitors start to wait on each other
        with self.lock:
            cost = WORST_CASE_CENTS
            try:
                res = handle(store, stream, case, phone, body.text, READ_ONLY, self.clock(), client=self.client, model_off=not live)
                cost = res.cents                 # a failed case keeps its worst cost: it may still have been billed
            finally:
                # the cost is written and the reservation released in one step, so no request sees neither
                store.db.execute("UPDATE usage SET cents = ? WHERE id = ?", (cost, usage))
                self.reserved -= WORST_CASE_CENTS
        return {"case": case, "handed": res.handed, "cents": round(res.cents, 4), "seconds": round(time.monotonic() - started, 1), "off": not live}

    def reset(self, visitor: str) -> bool:
        """Throw away this visitor's copy. A copy with a card payment the processor has not settled is kept: the processor is asked first
        (settle it, or cancel it if it is still processing), and if it cannot say, nothing is thrown away and Home says so once, so the
        payment is never lost and never taken a second time on a fresh copy. Returns whether the copy was started over."""
        with self.lock:
            store, stream = self.ready(), self.stream(visitor)
            if self._unresolved(store, stream) and not self._resolve(store, [stream]):
                self._keep(self._reset_notes, visitor, "Start over waits: a card payment on this copy has not been settled by the card processor "
                           "yet, so nothing was thrown away. Try again once the card processor has answered.")
                return False
            mark = renting.copy_mark(store, stream) + ":"
            for system in self._outside():
                system.note(mark)               # on file before anything changes: if it cannot be written, nothing is dropped
            self._proposals.pop(visitor, None)
            self._notes.pop(visitor, None)
            self._reset_notes.pop(visitor, None)
            self._resets[visitor] = self._resets.get(visitor, 0) + 1
            store.drop_stream(stream)
            self._views.pop(stream, None)
            self._rents.pop(stream, None)
            self._forget(mark)                  # nor any code of it on the stand-in gate
            return True


    # owner rules ------------------------------------------------------------------------------------------------------
    def rule_proposal(self, visitor: str, sentence: str, live: bool, budget_left=None) -> tuple[str, Proposal, dict]:
        """One owner sentence read into a proposed rule, checked by code, with last year replayed under it. A try of its own:
        three a day per visitor, inside the day's budget."""
        with self.lock:
            store = self.ready()
            books = store.books(self.stream(visitor))
            current = dict(books.rules["late_fee_waiver"]["typed"])
            versions = tuple(books.rules.get(n, {}).get("version", 0) for n in ("late_fee_waiver", STAFF_RULE))
            day = self.day
            copy = self._resets.get(visitor, 0)
            since = as_utc(spend.day_start()).isoformat()
            used = store.db.execute("SELECT COUNT(*) FROM usage WHERE visitor = ? AND shift = 'rule' AND at >= ?", (visitor, since)).fetchone()[0]
            if live and used >= RULES_PER_DAY:
                raise HTTPException(429, f"You have used today's {RULES_PER_DAY} rule readings. They come back at midnight, New York time.")
            if live and budget_left is not None and budget_left() < RULE_WORST_CENTS / 100:
                raise HTTPException(429, "Today's budget for live runs is used up. It comes back at midnight, New York time.")
            usage = None
            if live:
                usage = store.db.execute("INSERT INTO usage(at, visitor, run, shift, cents) VALUES (?,?,?,?,0)",
                                         (datetime.now(timezone.utc).isoformat(), visitor, "rule:" + secrets.token_hex(4), "rule")).lastrowid
                self.reserved += RULE_WORST_CENTS
        p = None
        try:
            if live and self.client is not None:
                p = compile_rule(self.client, RULE_MODEL[0], RULE_MODEL[1], sentence, current)
            else:
                p = Proposal("cannot", reason="Tilden is off on this copy, so no sentence can be read into a rule")
        finally:
            with self.lock:     # a failed call keeps its worst cost: it may still have been billed
                if usage is not None:
                    store.db.execute("UPDATE usage SET cents = ? WHERE id = ?", (p.call.cents if p and p.call else RULE_WORST_CENTS, usage))
                    self.reserved -= RULE_WORST_CENTS
        effect = {}
        if p.outcome == "adopt" and p.kind == "waiver":
            with self.lock:
                events = store.events(self.stream(visitor))
            effect = replay(events, current, p.typed)
        pid = secrets.token_hex(6)
        with self.lock:
            if self._resets.get(visitor, 0) != copy:      # the copy was reset while the sentence was read: this result is for the old one
                raise HTTPException(409, "Your copy was started over while the rule was read. Write the rule again.")
            self._proposals[visitor] = (pid, p, sentence, versions, day)
            while len(self._proposals) > 64:
                self._proposals.popitem(last=False)
        return pid, p, effect

    def approve_rule(self, visitor: str, pid: str) -> int:
        """The visitor, acting as the owner on their own copy, adopts the proposal they were shown. Returns the new version."""
        with self.lock:
            held = self._proposals.get(visitor)
            if held is None or held[0] != pid or held[1].outcome != "adopt":
                raise HTTPException(409, "That proposal is no longer here. Write the rule again.")
            _, p, sentence, versions, day = held
            store = self.ready()
            stream = self.stream(visitor)
            books = store.books(stream)
            now = tuple(books.rules.get(n, {}).get("version", 0) for n in ("late_fee_waiver", STAFF_RULE))
            if now != versions or day != self.day:     # a rule changed, or the books were rebuilt, since it was read
                del self._proposals[visitor]
                raise HTTPException(409, "The rules changed since this proposal was read. Write the rule again.")
            name = "late_fee_waiver" if p.kind == "waiver" else STAFF_RULE
            typed = dict(p.typed)
            before = books.rules.get(name)
            if name == STAFF_RULE and before:          # a staff rule adds to the conditions already in force
                old = before["typed"]
                typed = {"action": "send_to_staff", "several_units": bool(old.get("several_units") or typed["several_units"]),
                         **{k: sorted(set(old.get(k, [])) | set(typed[k])) for k in ("flags", "claim_kinds", "actions")}}
                sentence = before["sentence"] + " " + sentence
            version = (before or {}).get("version", 0) + 1
            led = Ledger(books)
            led.adopt_rule(self.clock(), name, version, sentence, typed, by="owner")
            with store.write():
                store.append(stream, led.pending)
            del self._proposals[visitor]
            return version

    def restore_rule(self, visitor: str, name: str, version: int) -> int:
        """The visitor, acting as the owner on their own copy, goes back to an earlier version of a rule. Returns the new version.
        A proposal read before this is refused at its approval, since the rules changed."""
        with self.lock:
            store = self.ready()
            stream = self.stream(visitor)
            led = Ledger(store.books(stream))
            try:
                led.restore_rule(self.clock(), name, version, by="owner")
            except Refused as exc:
                self._notes[visitor] = f"{str(exc)[:1].upper()}{str(exc)[1:]}."
                while len(self._notes) > 64:
                    self._notes.pop(next(iter(self._notes)))
                raise HTTPException(409, str(exc)) from exc
            with store.write():
                store.append(stream, led.pending)
            self._notes.pop(visitor, None)      # a refusal not yet shown is out of date once a going back succeeds
            return led.books.rules[name]["version"]

    # Tilden on the walk -------------------------------------------------------------------------------------------------------
    def _tilden_try(self, visitor: str, shift: str, per_day: int, worst_cents: float, noun: str, budget_left) -> tuple[Store, int]:
        """What every live Tilden job on a copy passes before any call, as rule_proposal does. Call it inside the lock. The visitor's count
        for the day and the day's budget are checked first, and a refusal costs nothing; then the try is written and its worst cost
        reserved in one locked step, so two requests at once cannot both pass. Returns the store and the usage row to settle."""
        store = self.ready()
        since = as_utc(spend.day_start()).isoformat()
        used = store.db.execute("SELECT COUNT(*) FROM usage WHERE visitor = ? AND shift = ? AND at >= ?", (visitor, shift, since)).fetchone()[0]
        if used >= per_day:
            raise HTTPException(429, f"You have used today's {per_day} {noun}. They come back at midnight, New York time.")
        if budget_left is not None and budget_left() < worst_cents / 100:
            raise HTTPException(429, "Today's budget for live runs is used up. It comes back at midnight, New York time.")
        usage = store.db.execute("INSERT INTO usage(at, visitor, run, shift, cents) VALUES (?,?,?,?,0)",
                                 (datetime.now(timezone.utc).isoformat(), visitor, f"{shift}:{secrets.token_hex(4)}", shift)).lastrowid
        self.reserved += worst_cents
        return store, usage

    def _tilden_settle(self, store: Store, usage: int, call, worst_cents: float) -> None:
        """Write what the call cost and release the reservation in one step. A failed call keeps its worst cost: it may still have been billed."""
        with self.lock:
            store.db.execute("UPDATE usage SET cents = ? WHERE id = ?", (call.cents if call else worst_cents, usage))
            self.reserved -= worst_cents

    def read_walk_note(self, visitor: str, walk: str, unit: str | None, text: str, live: bool = True, budget_left=None,
                       source: str = "staff:office_manager", record: bool = True) -> dict:
        """A note a staff member typed about one unit of a walk, read into an observation. Tilden drafts the fields from their words and
        code keeps only what the note quotes (core/walk_reading.py). The page asks with record=False: a quoted word can exist and still
        not mean the value read from it, so the draft fills the form and staff record it themselves. With record=True a kept draft is
        recorded through walk.run as the typing staff member's own, staff attested, with the note verbatim and a mark that Tilden read
        it; that path stays for a staff tool that shows the draft before sending. One that is not kept says why in plain words and
        records nothing. Ten a day per visitor, inside the day's budget; a refused run costs nothing."""
        text = " ".join((text or "").split())
        with self.lock:
            store, stream = self.ready(), self.stream(visitor)
            w = walks.load(store, stream)[1].walks.get(walk)
            if w is None:
                raise HTTPException(404, "There is no such walk on your copy.")
            if unit and unit not in w.route:
                raise HTTPException(422, f"Unit {unit} is not on this walk's route, so nothing was recorded.")
            if not text:
                raise HTTPException(422, "Type what you saw first.")
            if len(text) > walks.NOTE_MAX:
                raise HTTPException(422, f"Keep the note under {walks.NOTE_MAX} characters.")
            if not live or self.client is None:
                return {"recorded": False, "off": True, "made": None, "reading": None, "cents": 0.0,
                        "reason": "Tilden is off on this copy, so no note can be read into an observation"}
            copy = self._resets.get(visitor, 0)
            store, usage = self._tilden_try(visitor, "walk", WALK_NOTES_PER_DAY, WALK_NOTE_WORST_CENTS, "note readings", budget_left)
        reading = None
        try:
            reading = walk_reading.read_note(self.client, WALK_MODEL[0], WALK_MODEL[1], text, w.route, unit)
        finally:
            self._tilden_settle(store, usage, reading.call if reading else None, WALK_NOTE_WORST_CENTS)
        cents = reading.call.cents if reading.call else 0.0
        with self.lock:
            if self._resets.get(visitor, 0) != copy:        # the copy was reset while the note was read: this result is for the old one
                raise HTTPException(409, "Your copy was started over while the note was read. Type it again.")
            if self.ready() is not store:                    # the day turned over during the call: the copy now lives in a new day's store
                raise HTTPException(409, "The day turned over while the note was read. Type it again.")
            out = {"recorded": False, "off": False, "made": None, "reading": reading, "cents": cents, "reason": reading.refusal,
                   "money": "mentions_money" in reading.flags}
            if reading.refusal or not record:              # a draft for the page: staff check the fields and record it themselves
                return out
            now = self.clock()
            standing = walks.load(store, stream)[1].walks[walk].current(reading.unit)
            mark = {"by": "tilden", "model": WALK_MODEL[0], "effort": WALK_MODEL[1], "quotes": reading.quotes, "flags": reading.flags, "work": reading.work,
                    "tokens_in": reading.call.tokens_in, "tokens_out": reading.call.tokens_out, "cents": round(cents, 6)}
            act = walks.Act(f"note:{secrets.token_hex(6)}", "correct_observation" if standing else "observe_unit", source,
                            about=standing.id if standing else walk, unit=reading.unit, form=reading.fields, text=text, read=mark)
            d = walks.run(store, stream, act, now)       # staff's own observation: the typing staff member is its author
            out.update(recorded=d.allowed, made=d.made, reason=None if d.allowed else d.check, corrected=bool(standing) and d.allowed)
            return out

    def investigate_finding(self, visitor: str, key: str, live: bool = True, budget_left=None) -> dict:
        """Tilden's report on one walk finding. Code builds the unit's history as a fixed list of records with ids made for this run;
        Tilden returns a packet, and code keeps it only when every cited id is in that history, the next step is open for this kind of
        finding and no sentence says an action happened (core/walk_reading.py). Whatever comes back, the only record written is the
        report itself (walk.investigated): no overlock, lease, block or money changes. Five a day per visitor; a refused run costs nothing."""
        with self.lock:
            store, stream = self.ready(), self.stream(visitor)
            books, rent = renting.load(store, stream)
            now = self.clock()
            f = next((x for x in reconcile.compare(books, rent, now) if x.key == key), None)
            if f is None:
                raise HTTPException(404, "That finding is not on the list any more: the records and the walk now agree.")
            if not live or self.client is None:
                return {"off": True, "key": key, "packet": None, "dropped": None, "history": [], "cents": 0.0,
                        "reason": "Tilden is off on this copy, so no finding can be investigated"}
            # a report is a reading of the unit's records: asked again over the same records it can only differ by chance, and staff
            # would keep the answer they liked best, so a kept report stands until something in those records changes
            last = (rent.floor.investigations.get(key) or [None])[-1]
            if last and last.get("packet") and [h["text"] for h in last.get("history", [])] == [h["text"] for h in walk_reading.build_history(books, rent, f, now, "same")]:
                raise HTTPException(409, "Tilden already looked into this finding and nothing in its records has changed since; its report is below.")
            copy = self._resets.get(visitor, 0)
            store, usage = self._tilden_try(visitor, "investigate", INVESTIGATIONS_PER_DAY, INVESTIGATE_WORST_CENTS, "investigations", budget_left)
            run = secrets.token_hex(4)
            history = walk_reading.build_history(books, rent, f, now, run)
        result = None
        try:
            result = walk_reading.investigate(self.client, WALK_MODEL[0], WALK_MODEL[1], f, history, run,
                                              facts=walk_reading.reason_facts(books, rent, f, now))
        finally:
            self._tilden_settle(store, usage, result.call if result else None, INVESTIGATE_WORST_CENTS)
        with self.lock:
            if self._resets.get(visitor, 0) != copy:
                raise HTTPException(409, "Your copy was started over while the finding was investigated. Ask again.")
            if self.ready() is not store:
                raise HTTPException(409, "The day turned over while the finding was investigated. Ask again.")
            books2, rent2 = renting.load(store, stream)      # a report is for the records it read: if staff changed them meanwhile, it is stale
            f2 = next((x for x in reconcile.compare(books2, rent2, self.clock()) if x.key == key), None)
            if f2 is None or [h["text"] for h in walk_reading.build_history(books2, rent2, f2, self.clock(), run)] != [h["text"] for h in history]:
                raise HTTPException(409, "The unit's records changed while Tilden read them. Ask again.")
            payload = {"key": key, "unit": f.unit, "slug": f.slug, "run": run, "finding": {"what": f.what, "why": f.why}, "history": history,
                       "packet": result.packet, "checks": {"kept": result.packet is not None, "dropped": result.dropped},
                       "call": asdict(result.call) if result.call else None}
            with store.write():                      # the only thing written: the report itself
                led = Ledger(store.books(stream))
                led.emit("walk.investigated", now, payload, f"investigate:{run}", source="operator")
                store.append(stream, led.pending)
            return {"off": False, "key": key, "packet": result.packet, "dropped": result.dropped, "history": history,
                    "cents": result.call.cents if result.call else 0.0, "reason": result.dropped}

    def compare_orders(self, visitor: str, first: str, second: str, live: bool = True, budget_left=None) -> dict:
        """Tilden's reading of two open work orders on one unit: one fault reported twice, or two. Code keeps it only when it cites a
        report of each order and says nothing was done (core/walk_reading.py). The only record written is the reading itself
        (work_order.compared); joining the orders is a staff step. It counts against the day's investigations; a refused run costs nothing."""
        key = f"{first}:{second}"

        def pair_of(wk):
            return next(((a, b) for a, b in walks.pairs(wk) if (a.id, b.id) == (first, second)), None)
        with self.lock:
            store, stream = self.ready(), self.stream(visitor)
            wk = walks.load(store, stream)[1]
            pair = pair_of(wk)
            if pair is None:
                raise HTTPException(404, "Those two work orders are not both open on one unit any more.")
            if not live or self.client is None:
                return {"off": True, "pair": key, "reading": None, "dropped": None, "records": [], "cents": 0.0,
                        "reason": "Tilden is off on this copy, so the two orders cannot be read"}
            # asked again over the same reports a reading can only differ by chance, so a kept one stands until a report changes
            last = (wk.comparisons.get(key) or [None])[-1]
            if last and last.get("reading") and [r["text"] for r in last["records"]] == [r["text"] for r in walk_reading.order_records(*pair, "same")]:
                raise HTTPException(409, "Tilden already read these two orders and nothing in their reports has changed since; its reading is below.")
            copy = self._resets.get(visitor, 0)
            store, usage = self._tilden_try(visitor, "investigate", INVESTIGATIONS_PER_DAY, SAME_WORST_CENTS, "investigations", budget_left)
            run, now = secrets.token_hex(4), self.clock()
        result = None
        try:
            result = walk_reading.same_fault(self.client, WALK_MODEL[0], WALK_MODEL[1], *pair, run)
        finally:
            self._tilden_settle(store, usage, result.call if result else None, SAME_WORST_CENTS)
        with self.lock:
            if self._resets.get(visitor, 0) != copy:
                raise HTTPException(409, "Your copy was started over while the orders were read. Ask again.")
            if self.ready() is not store:
                raise HTTPException(409, "The day turned over while the orders were read. Ask again.")
            wk2 = walks.load(store, stream)[1]
            again = pair_of(wk2)
            if again is None or [r["text"] for r in walk_reading.order_records(*again, run)] != [r["text"] for r in result.records]:
                raise HTTPException(409, "The two orders changed while Tilden read them. Ask again.")
            # another request may have kept a reading of the same reports while this one was out: that one stands
            kept = next((x for x in reversed(wk2.comparisons.get(key) or []) if x.get("reading")
                         and [r["text"] for r in x["records"]] == [r["text"] for r in result.records]), None)
            if kept:
                return {"off": False, "pair": key, "reading": kept["reading"], "dropped": None, "records": kept["records"],
                        "cents": result.call.cents if result.call else 0.0, "reason": None}
            payload = {"pair": key, "unit": pair[0].unit, "run": run, "records": result.records, "reading": result.reading,
                       "checks": {"kept": result.reading is not None, "dropped": result.dropped},
                       "call": asdict(result.call) if result.call else None}
            with store.write():                      # the only thing written: the reading itself
                led = Ledger(store.books(stream))
                led.emit("work_order.compared", now, payload, f"compare:{run}", source="operator")
                store.append(stream, led.pending)
            return {"off": False, "pair": key, "reading": result.reading, "dropped": result.dropped, "records": result.records,
                    "cents": result.call.cents if result.call else 0.0, "reason": result.dropped}

    # renting ------------------------------------------------------------------------------------------------------------------
    def add_renter(self, visitor: str, name: str, phone: str) -> str:
        """A new tenant for someone renting on the website. Typed details never pick an existing tenant: nothing on the website proves
        who someone is, and an existing tenant's account credit can start a rental with no card."""
        with self.lock:
            store, stream = self.ready(), self.stream(visitor)
            with store.write():
                led = Ledger(store.books(stream))
                tenant = led.add_tenant(self.clock(), name, phone)
                store.append(stream, led.pending)
            return tenant

    def rent_step(self, visitor: str, act: renting.Act):
        """One renting command on a visitor's copy. A rental it starts gets its gate code at once, from the stand-in gate."""
        with self.lock:
            store, stream, now = self.ready(), self.stream(visitor), self.clock()
            decision = renting.run(store, stream, act, now)
            self._advance(store, stream, now)
            return decision

    def keep_step_note(self, visitor: str, text: str) -> None:
        """Why a staff step was not taken before it reached the gate, kept to be shown once on the page that asked."""
        with self.lock:
            self._keep(self._step_notes, visitor, text)

    def staff_rent(self, visitor: str, act: renting.Act) -> bool:
        """A staff renting step on this copy, through the gate: whether it was done. A refusal is kept to be shown once on the page that asked."""
        d = self.rent_step(visitor, act)
        with self.lock:
            if d.allowed:
                self._step_notes.pop(visitor, None)
            else:
                self._keep(self._step_notes, visitor, d.check)
        return d.allowed

    def staff_protection(self, visitor: str, act: protection.Act, settings: bool = False) -> bool:
        """A staff protection step on this copy, through the gate: whether it was done. A refusal is kept to be shown once, on the tenant's
        page or, for a plan version, on Settings."""
        with self.lock:
            d = protection.run(self.ready(), self.stream(visitor), act, self.clock())
            notes = self._notes if settings else self._step_notes
            if d.allowed:
                notes.pop(visitor, None)
            else:
                self._keep(notes, visitor, d.check)
            return d.allowed

    def staff_shop(self, visitor: str, act: shop.Act, then=None, saved: str = "") -> bool:
        """A staff shop step on this copy, through the shop's one command gate: whether it was done. `then` makes the step that follows
        (a receipt is posted to stock, a return goes back on the shelf) from the first decision; both ids come from the page's token, so a
        second press repeats the first decisions and posts nothing twice. A refusal is kept to be shown once on the Shop page. When the
        follow-up is refused after the first record was saved, `saved` says which record that is, and the note never says nothing changed."""
        with self.lock:
            store, stream, now = self.ready(), self.stream(visitor), self.clock()
            d = shop.run(store, stream, act, now)
            kept = False
            if d.allowed and then is not None:
                d = shop.run(store, stream, then(d), now)
                kept = not d.allowed
            self._shop_done(store, stream, now, d, visitor, saved if kept else "")
            return d.allowed

    def shop_counter_sale(self, visitor: str, act: shop.Act, card: str) -> bool:
        """A sale at the counter: the quote freezes price and cost and holds the stock, the test card is asked once, and the sale posts
        only when the processor confirms the payment. A payment still waiting, or one the processor declined, posts nothing."""
        with self.lock:
            store, stream, now = self.ready(), self.stream(visitor), self.clock()
            d = shop.run(store, stream, act, now)
            kept = d.allowed
            if d.allowed:
                d = self._shop_collect(store, stream, now, act, d.made, card, "first")      # a replayed form resumes the attempt it created
            self._shop_done(store, stream, now, d, visitor, "The sale was started and its stock is held, but it is not posted:" if kept else "")
            return d.allowed

    def shop_retry_payment(self, visitor: str, act: shop.Act, card: str) -> bool:
        """A new card payment on a sale whose payment was declined or returned: the retry is recorded through the gate first, then the
        card is asked once. A sale not yet posted posts when it is confirmed; one already posted only has its money back."""
        with self.lock:
            store, stream, now = self.ready(), self.stream(visitor), self.clock()
            d = shop.run(store, stream, shop.Act(act.id, act.action, act.source, act.about, dict(act.form or {}, card=card)), now)
            kept = d.allowed
            if d.allowed:
                d = self._shop_collect(store, stream, now, act, d.made, card, f"shop:retry:{act.id}")     # only the attempt this form made
            self._shop_done(store, stream, now, d, visitor, "The new payment attempt was recorded, but the payment did not come in:" if kept else "")
            return d.allowed

    def shop_resume_payment(self, visitor: str, act: shop.Act) -> bool:
        """Ask the card processor again about a payment whose answer never came back: the same attempt, with its own key and card, so
        the processor hands back the payment it holds, never a second one. A replayed form asks only the attempt it was made for."""
        with self.lock:
            store, stream, now = self.ready(), self.stream(visitor), self.clock()
            d = shop.run(store, stream, act, now)
            if d.allowed:
                d = self._shop_collect(store, stream, now, act, act.about, "", d.made)
            self._shop_done(store, stream, now, d, visitor)
            return d.allowed

    def _shop_collect(self, store: Store, stream: str, now: datetime, act: shop.Act, quote: str, card: str, which: str | None = None):
        """Ask the processor for the frozen quote's payment and post the sale on a confirmed one. `which` binds the step to one attempt."""
        try:
            payment = shop.pay(store, stream, self.processor, quote, card, now, which)
        except Refused as exc:
            return shop.Decision(False, act.action, str(exc))
        st = shop.state(store.books(stream))
        status = (payment or {}).get("status")
        if payment is not None and quote in st.payments and payment["key"] != st.payments[quote]["key"]:
            return shop.Decision(False, act.action, "a newer payment replaced this one; what the processor said about this one is on record")
        if status == "succeeded":
            if quote in st.sales:
                return shop.Decision(True, act.action, "The payment was taken.")
            if quote in st.cancelled:
                return shop.Decision(False, act.action, "the sale was cancelled before the payment came in; the money is owed back and is on Today")
            return shop.run(store, stream, shop.Act(act.id + ":sale", "complete_sale", act.source, quote), now)
        return shop.Decision(False, act.action, "the card was declined; the payment is on Today for staff to look at" if status == "failed"
                             else "the card payment is not confirmed yet; the sale posts when it is")

    def _shop_done(self, store: Store, stream: str, now: datetime, d, visitor: str, saved: str = "") -> None:
        """After a shop step: keep why it was refused, and run the daily check so Today shows what the step settled. `saved` leads the
        note when an earlier part of the same step was recorded, so the page does not say nothing changed."""
        self._step_saved.discard(visitor)
        if d.allowed:
            self._step_notes.pop(visitor, None)
        elif saved:
            why = d.check.rstrip(". ")
            self._keep(self._step_notes, visitor, f"{saved} {why[:1].lower() + why[1:] if why[1:2].islower() else why}")
            self._step_saved.add(visitor)
        else:
            self._keep(self._step_notes, visitor, d.check)
        try:
            shop.sweep(store, stream, now)
        except Exception:
            log.exception("shop check failed for %s", stream)

    def pay_rental(self, visitor: str, rental: str, card: str, expected_cents: int | None = None):
        """A renter's card payment for a rental in checkout, through the processor; a rental it starts gets its code at once.
        `expected_cents` is the combined total the page showed: the core charges nothing if it is different now."""
        with self.lock:
            store, stream, now = self.ready(), self.stream(visitor), self.clock()
            intent = renting.pay_rental(store, stream, self.processor, rental, card, now, expected_cents)
            from core import leads
            with store.write():
                books, rent = renting.load(store, stream)
                led = Ledger(books)
                leads.convert(led, rent, now)
                store.append(stream, led.pending)
            self._advance(store, stream, now)
            return intent

    def checkout_total(self, visitor: str, rental: str) -> int | None:
        """What a rental in checkout would be charged now, add-ons included: the number the page shows and the payment checks."""
        with self.lock:
            books, rent = renting.load(self.ready(), self.stream(visitor))
            r = rent.rentals.get(rental)
            return renting.checkout_due(books, rent, r, self.clock())[2] if r else None

    def _settle_rentals(self, store: Store, stream: str, now: datetime) -> None:
        """Carry the copy's rentals forward from what its payments say. Run after every way a payment's state can change, so the wait of
        a rental is answered the moment its intent is closed and a later success is never taken for the first answer. A fault is logged
        and the hourly pass finishes it: the payment is already recorded."""
        try:
            renting.settle_payments(store, stream, now)
        except Exception:
            log.exception("renting pass failed for %s", stream)

    def _advance(self, store: Store, stream: str, now: datetime) -> None:
        """Carry the copy's gate codes forward. What was just written (a charge, a started rental) stands whether this works or not:
        a fault is logged and the hourly pass finishes the code."""
        try:
            renting.advance(store, stream, self.access, now)
        except Exception:
            log.exception("gate code step failed for %s", stream)

    # collections ------------------------------------------------------------------------------------------------------------
    def _keep(self, notes: dict[str, str], visitor: str, text: str) -> None:
        if notes is self._step_notes:
            self._step_saved.discard(visitor)           # a new note is a plain refusal unless the caller says part of its step was saved
        notes[visitor] = f"{text[:1].upper()}{text[1:]}" + ("" if text.endswith(".") else ".")
        while len(notes) > 64:
            notes.pop(next(iter(notes)))

    def staff_step(self, visitor: str, cmd: Command) -> None:
        """A staff step on a tenant's collections, through the gate on this copy. A refusal is kept to be shown once on that page."""
        with self.lock:
            d = execute(self.ready(), self.stream(visitor), cmd, self.clock())
            if d.allowed:
                self._step_notes.pop(visitor, None)
            else:
                self._keep(self._step_notes, visitor, d.check)

    def sync_work(self, visitor: str) -> None:
        """Keep this copy's work items as the records say, before a page reads them. Writes only what is new; a fault is logged and the
        page still reads the items from the records."""
        with self.lock:
            try:
                work.sweep(self.ready(), self.stream(visitor), self.clock())
            except Exception:
                log.exception("work check failed for %s", visitor)

    def staff_work(self, visitor: str, act: work.Act) -> bool:
        """A staff step on an item of work, through the gate on this copy: whether it was done. A refusal is kept to be shown once on Today."""
        with self.lock:
            d = work.run(self.ready(), self.stream(visitor), act, self.clock())
            if d.allowed:
                self._step_notes.pop(visitor, None)
            else:
                self._keep(self._step_notes, visitor, d.check)
            return d.allowed

    def take_card_payment(self, visitor: str, cmd: Command) -> None:
        """Staff take a card payment on a tenant's page. The gate decides and records; a refusal is kept to be shown once. When it allows
        a command it has not seen before, the payment goes through the processor on the card the tenant has on file. It counts when the
        processor confirms it; until then (or when the answer is lost) it shows as waiting, and the hourly pass settles it.
        ponytail: the decision and the intent are two writes (pay() opens its own transaction); a crash between them leaves a decision
        with no payment, and staff press the button again on a fresh page."""
        with self.lock:
            store, stream = self.ready(), self.stream(visitor)
            d = execute(store, stream, cmd, self.clock())
            if not d.allowed:
                return self._keep(self._step_notes, visitor, d.check)
            self._step_notes.pop(visitor, None)
            if d.repeated:          # the same page pressed twice: the first decision comes back and nothing is paid again
                return
            token = next(c["token"] for c in delinquency.cards_of(store.books(stream), cmd.tenant) if c["card"] == cmd.form["card"])
            try:
                intent = delinquency.pay(store, stream, self.processor, cmd.tenant, cmd.form["cents"], token, self.clock())
            except Refused as exc:
                return self._keep(self._step_notes, visitor, str(exc))
            if intent.state == "closed":
                self._keep(self._step_notes, visitor, "the card processor did not take the payment")

    def take_reset_note(self, visitor: str) -> str:
        """Why the last Start over was refused, once; empty when nothing was."""
        with self.lock:
            return self._reset_notes.pop(visitor, "")

    def take_step_note(self, visitor: str) -> str:
        """Why the last staff step on a tenant was refused, once; empty when nothing was."""
        with self.lock:
            self._step_saved.discard(visitor)
            return self._step_notes.pop(visitor, "")

    def take_shop_note(self, visitor: str) -> tuple[str, bool]:
        """The last shop step's note, once, and whether part of that step was saved before it was refused."""
        with self.lock:
            partial = visitor in self._step_saved
            self._step_saved.discard(visitor)
            return self._step_notes.pop(visitor, ""), partial

    def acknowledge(self, visitor: str, task: str, who: str) -> None:
        """Staff saw a collections task on this copy; a task already acknowledged, or not here, changes nothing."""
        with self.lock:
            store = self.ready()
            led = Ledger(store.books(self.stream(visitor)))
            try:
                delinquency.acknowledge(led, task, who, self.clock())
            except Refused:
                return
            with store.write():
                store.append(self.stream(visitor), led.pending)

    def owner_setting(self, visitor: str, kind: str, values: dict | None, why: str = "") -> bool:
        """The visitor, as the owner on their own copy, adopts the next version of the plan policy or the schedule. Values outside
        the fixed limits, or the same as in force, change nothing, and the Rules page says why once."""
        with self.lock:
            store = self.ready()
            led = Ledger(store.books(self.stream(visitor)))
            held = getattr(delinquency.state(led.books), "policy" if kind == "policy" else "schedule") or {}
            if values is not None and all(held.get(k) == v for k, v in values.items()):
                why = f"that is already the {'plan policy' if kind == 'policy' else 'schedule'} in force"
            if values is None or why:
                self._keep(self._notes, visitor, why)
                return False
            try:
                if kind == "policy":
                    delinquency.adopt_policy(led, self.clock(), held.get("version", 0) + 1, values)
                else:
                    delinquency.adopt_schedule(led, self.clock(), held.get("version", 0) + 1, values["overlock_days"], values["lien_days"])
            except Refused as exc:
                self._keep(self._notes, visitor, str(exc))
                return False
            with store.write():
                store.append(self.stream(visitor), led.pending)
            self._notes.pop(visitor, None)
            return True

    def take_note(self, visitor: str) -> str:
        """Why the last going back was refused, once; empty when nothing was."""
        with self.lock:
            return self._notes.pop(visitor, "")
