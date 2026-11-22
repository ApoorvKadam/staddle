"""The walk record: what staff saw on the floor, kept apart from what the FMS records.

A walk is a route of units, who walked it, when it started and when it finished. A walk may finish with units not checked; those
stay "not checked". Each unit gets one observation per walk: whether a lock was on the door, whose, whether the overlock tag was
there, the state of the door, a short note, who recorded it and when. Every observation says it was staff attested. A correction is
a new observation that supersedes the earlier one; nothing is edited or deleted.

What staff saw never writes the overlock record, a lease, a unit block or money. The books fold it (books.floor) so two checks can read
it: renting, so a unit is not offered while a finding or a work order stands on it, and collections, which will not record an overlock as
lifted while a lock nobody has identified is still seen on the unit. The comparison of the two lives in core/reconcile.py, which reads this record and never writes it.

A work order carries a repair from reported to closed, each change with who and when.

An observation Tilden read from a staff member's typed note (core/walk_reading.py) carries a `read` mark with the quoted words, flags,
tokens and cost; the note is kept as typed and the staff member is its author. walk.investigated is Tilden's report on a finding, and
only a report: nothing it says is a record of what happened.

Events (in the copy's own stream, over the facility's own): walk.started, walk.observed, walk.finished, walk.investigated, and work_order.reported,
work_order.triaged, work_order.assigned, work_order.recorded, work_order.checked, work_order.closed. A second report joins an open order
(work_order.reported_again) only when staff name that order; the code never decides two notes are the same fault.

Staff write all of it through `run`, as the work queue does (core/work.py): staff roles only, a command id returns its first decision
and a repeat writes nothing, the unit must exist, values come from the lists below, notes are capped. The commands move no money,
lease, unit block or overlock record, so the gate's owner rules and holds have nothing to hold here."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .billing import Ledger
from .gate import STAFF_ROLES, WHO, Decision
from .store import Store
from .work import NOTE_MAX, person

LOCK = TAG = ("present", "absent", "unknown")
WHOSE = ("tenant", "facility overlock", "unknown")
DOOR = ("closed", "open", "damaged", "unknown")
CONTENTS = ("seen", "not seen", "unknown")     # belongings inside the unit; unknown unless said, so an older observation or form still reads

ATTESTED = "staff attested"             # what every observation is: a person saw it and said so, no camera or sensor


@dataclass
class Observation:
    id: str
    walk: str
    unit: str
    lock: str                           # present, absent, unknown
    whose: str                          # tenant, facility overlock, unknown
    tag: str                            # the facility's overlock tag: present, absent, unknown
    door: str                           # closed, open, damaged, unknown
    note: str
    by: str
    at: datetime
    supersedes: str | None = None
    superseded_by: str | None = None
    evidence: str = ATTESTED
    read: dict | None = None                    # set when Tilden read the note into these fields: its quotes, flags, tokens and cost
    contents: str = "unknown"                   # belongings seen inside: seen, not seen, unknown


@dataclass
class Walk:
    id: str
    route: tuple[str, ...]                      # the units, in walk order
    by: str                                     # who walked (the staff source that started it)
    started: datetime
    finished: tuple[str, datetime] | None = None    # (who, when)
    observations: list[Observation] = field(default_factory=list)      # every one, corrections included, in the order written

    def history(self, unit: str) -> list[Observation]:
        return [o for o in self.observations if o.unit == unit]

    def current(self, unit: str) -> Observation | None:
        """What stands for the unit on this walk: the latest observation, which supersedes any before it."""
        h = self.history(unit)
        return h[-1] if h else None

    @property
    def not_checked(self) -> tuple[str, ...]:
        return tuple(u for u in self.route if self.current(u) is None)


# a work order: reported, triaged, assigned, work recorded, staff checked, closed. Only the moves are recorded; when a closed order
# lets a unit be rented again is not decided here.
ORDER_STATES = {"work_order.triaged": "triaged", "work_order.assigned": "assigned", "work_order.recorded": "work recorded",
                "work_order.checked": "staff checked", "work_order.closed": "closed"}


@dataclass
class Change:
    state: str
    by: str
    at: datetime
    text: str = ""                              # what staff wrote with the change
    to: str | None = None                       # who an assignment is to


@dataclass
class Report:
    by: str
    at: datetime
    text: str


@dataclass
class WorkOrder:
    id: str
    unit: str
    problem: str
    reported_by: str
    reported: datetime
    state: str = "reported"
    assigned_to: str | None = None
    changes: list[Change] = field(default_factory=list)     # every move, the report first
    reports: list[Report] = field(default_factory=list)     # the first report, then each one staff said is the same fault


class State:
    def __init__(self):
        self.walks: dict[str, Walk] = {}
        self.observations: dict[str, Observation] = {}
        self.orders: dict[str, WorkOrder] = {}
        self.investigations: dict[str, list[dict]] = {}     # finding key -> Tilden's reports on it, oldest first

    def standing(self, unit: str) -> Observation | None:
        """What staff last saw on the unit: its observation from the newest walk that checked it (a correction already supersedes
        within a walk), so a unit the newest walk skipped keeps its older reading."""
        for w in sorted(self.walks.values(), key=lambda w: (w.started, w.id), reverse=True):
            if (o := w.current(unit)) is not None:
                return o
        return None

    def apply(self, kind: str, at: datetime, p: dict) -> None:
        if kind == "work_order.reported":
            o = self.orders[p["order"]] = WorkOrder(p["order"], p["unit"], p["problem"], p["by"], at)
            o.changes.append(Change("reported", p["by"], at))
            o.reports.append(Report(p["by"], at, p["problem"]))
        elif kind == "work_order.reported_again":       # staff named the open order: the state does not move
            o = self.orders[p["order"]]
            o.reports.append(Report(p["by"], at, p["text"]))
            o.changes.append(Change("reported again", p["by"], at, p["text"]))
        elif kind in ORDER_STATES:
            o = self.orders[p["order"]]
            o.state = ORDER_STATES[kind]
            o.assigned_to = p.get("to") or o.assigned_to
            o.changes.append(Change(o.state, p["by"], at, p.get("text", ""), p.get("to")))
        elif kind == "walk.started":
            self.walks[p["walk"]] = Walk(p["walk"], tuple(p["route"]), p["by"], at)
        elif kind == "walk.observed":
            o = Observation(p["observation"], p["walk"], p["unit"], p["lock"], p["whose"], p["tag"], p["door"], p["note"], p["by"], at, p["supersedes"],
                            read=p.get("read"), contents=p.get("contents", "unknown"))
            if o.supersedes:
                self.observations[o.supersedes].superseded_by = o.id
            self.observations[o.id] = o
            self.walks[o.walk].observations.append(o)
        elif kind == "walk.investigated":               # a report only: nothing it says is a record of what happened
            self.investigations.setdefault(p["key"], []).append({**p, "at": at})
        elif kind == "walk.finished":
            self.walks[p["walk"]].finished = (p["by"], at)


def load(store: Store, stream: str):
    """The stream's books and its walk record. The facility's own events come first, so a copy sees the walk in the seeded year and
    its own, and never another copy's."""
    books = store.books(stream)
    wk = State()
    for e in store.events(stream):
        wk.apply(e.kind, e.at, e.payload)
    return books, wk


# staff commands through the gate -----------------------------------------------------------------------------------------------
ACTIONS = {"start_walk": "start a walk", "observe_unit": "record what was seen on a unit", "correct_observation": "correct an observation",
           "finish_walk": "finish a walk", "report_work_order": "report a work order", "triage_work_order": "triage a work order",
           "assign_work_order": "assign a work order", "record_order_work": "record the work on an order",
           "check_order_work": "check the work on an order", "close_work_order": "close a work order"}
# the move each work order command makes, and the state the order must be in first
MOVES = {"triage_work_order": ("work_order.triaged", "reported"), "assign_work_order": ("work_order.assigned", "triaged"),
         "record_order_work": ("work_order.recorded", "assigned"), "check_order_work": ("work_order.checked", "work recorded"),
         "close_work_order": ("work_order.closed", "staff checked")}
NEXT = {"reported": "triaged", "triaged": "assigned", "assigned": "work recorded", "work recorded": "staff checked", "staff checked": "closed"}
NOT_STAFF = {k: v for k, v in WHO.items() if k != "staff"}


@dataclass
class Act:
    """One staff command. `about` is the walk (to observe or finish it), the observation (to correct it) or the work order the command
    is about; `form` holds an observation's lock, whose, tag and door; `to` is a staff role or `person:<name>`."""
    id: str
    action: str
    source: str
    about: str | None = None
    unit: str | None = None
    route: list[str] | None = None
    form: dict | None = None
    text: str | None = None
    to: str | None = None
    read: dict | None = None        # for an observation Tilden read from the staff member's words: the mark kept with it


def _words(text: str | None) -> str:
    return " ".join((text or "").split())


def _unit(books, unit: str | None) -> str | None:
    return None if unit in books.units else f"there is no unit {unit} at this facility"


def _seen(form: dict | None) -> str | None:
    """What is wrong with an observation's values, in words, or None. `whose` may be left out and then reads unknown."""
    form = form or {}
    for name, allowed in (("lock", LOCK), ("whose", WHOSE), ("tag", TAG), ("door", DOOR), ("contents", CONTENTS)):
        if form.get(name, "unknown" if name in ("whose", "contents") else None) not in allowed:
            return f"{name} must be one of: {', '.join(allowed)}"
    return None


def evaluate(books, wk: State, act: Act, now: datetime) -> tuple[bool, str]:
    """The checks, in the gate's order. 1. the action exists and the source is a staff role. 2. safety, fixed in code: the record it is
    about is there and in the right state, the unit exists, the values are from the lists, the words are short enough. 3 and 4. no owner
    rule or hold applies: nothing here moves money, a unit or the overlock record."""
    a = act.action
    if a not in ACTIONS:
        return False, "not an action the system knows"
    role = act.source.removeprefix("staff:")
    if not act.source.startswith("staff:"):
        return False, f"{NOT_STAFF.get(act.source, 'that source')} may not {ACTIONS[a]}; only staff may"
    if role not in STAFF_ROLES:
        return False, f"{role or 'that'} is not a staff role; only staff may {ACTIONS[a]}"
    text = _words(act.text)
    if len(text) > NOTE_MAX:
        return False, f"keep it under {NOTE_MAX} characters"
    if a == "start_walk":
        route = act.route or []
        if not route:
            return False, "a walk needs at least one unit"
        for i, u in enumerate(route):
            if why := _unit(books, u):
                return False, why
            if u in route[:i]:
                return False, f"unit {u} is listed twice"
    elif a in ("observe_unit", "finish_walk"):
        w = wk.walks.get(act.about or "")
        if w is None:
            return False, "there is no such walk"
        if a == "observe_unit":
            if why := _unit(books, act.unit):
                return False, why
            if act.unit not in w.route:
                return False, f"unit {act.unit} is not on this walk's route"
            if w.finished:
                return False, "that walk is finished"
            if seen := w.current(act.unit):
                return False, f"unit {act.unit} already has an observation on this walk ({seen.id}); record a correction instead"
            if why := _seen(act.form):
                return False, why
            return True, "recorded, staff attested"
        if w.finished:
            return False, "that walk is finished"
    elif a == "correct_observation":
        o = wk.observations.get(act.about or "")
        if o is None:
            return False, "there is no such observation"
        if o.superseded_by:
            return False, f"that observation was already corrected; correct the latest one ({o.superseded_by})"
        if why := _seen(act.form):
            return False, why
        return True, "recorded, staff attested"
    elif a == "report_work_order":
        if act.about:               # staff say this is the same fault as an open order; the code never decides that itself
            o = wk.orders.get(act.about)
            if o is None:
                return False, "there is no such work order"
            if act.unit not in (None, o.unit):
                return False, f"that work order is on unit {o.unit}, not {act.unit}"
            if o.state == "closed":
                return False, "that work order is closed; report the fault as a new order"
        elif why := _unit(books, act.unit):
            return False, why
        if len(text) < 3:
            return False, "say in a few words what is wrong"
    else:
        o = wk.orders.get(act.about or "")
        if o is None:
            return False, "there is no such work order"
        if a == "assign_work_order" and not (act.to in STAFF_ROLES or act.to and person(act.to)):
            return False, "work goes to a staff role or a named person"
        if o.state != MOVES[a][1]:
            return False, ("that work order is already closed" if o.state == "closed" else
                           f"that work order is {o.state}; the next change is {NEXT[o.state]}")
    return True, "recorded"


def record(led: Ledger, wk: State, act: Act, now: datetime) -> str:
    """Write an allowed command and return the id of the walk, observation or work order it made or moved. The seed uses this too,
    so a seeded walk is written the way staff write one."""
    who, text, a = act.source, _words(act.text), act.action

    def put(kind: str, payload: dict, key: str) -> None:
        led.emit(kind, now, payload, f"{key}:{act.id}", source=who, command=act.id)
        wk.apply(kind, now, payload)
    if a == "start_walk":
        wid = f"W{len(wk.walks) + 1:04d}"
        put("walk.started", {"walk": wid, "route": list(act.route), "by": who}, "walk")
        return wid
    if a in ("observe_unit", "correct_observation"):
        old = wk.observations.get(act.about) if a == "correct_observation" else None
        walk_id, unit = (old.walk, old.unit) if old else (act.about, act.unit)
        oid, form = f"O{len(wk.observations) + 1:04d}", act.form
        put("walk.observed", {"walk": walk_id, "observation": oid, "unit": unit, "lock": form["lock"], "whose": form.get("whose", "unknown"),
                              "tag": form["tag"], "door": form["door"], "note": text, "by": who, "supersedes": old.id if old else None,
                              **({"read": act.read} if act.read else {}),
                              **({"contents": form["contents"]} if form.get("contents", "unknown") != "unknown" else {})}, "walk")
        return oid
    if a == "finish_walk":
        put("walk.finished", {"walk": act.about, "by": who}, "walk")
        return act.about
    if a == "report_work_order" and act.about:
        put("work_order.reported_again", {"order": act.about, "by": who, "text": text}, "order")
        return act.about
    if a == "report_work_order":
        oid = f"WO{len(wk.orders) + 1:04d}"
        put("work_order.reported", {"order": oid, "unit": act.unit, "problem": text, "by": who}, "order")
        return oid
    put(MOVES[a][0], {"order": act.about, "by": who, "text": text, **({"to": act.to} if a == "assign_work_order" else {})}, "order")
    return act.about


def command(led: Ledger, wk: State, act: Act, now: datetime) -> Decision:
    """Check and record one command on the books and walk record `led` and `wk` hold. A command id already decided returns that
    decision and writes nothing."""
    books = led.books
    if act.id in books.decisions:
        p = books.decisions[act.id]
        return Decision(p["allowed"], p["action"], p["check"], command=act.id, repeated=True, made=p.get("made"))
    ok, check = evaluate(books, wk, act, now)
    made = record(led, wk, act, now) if ok else None
    led.emit("gate.decided", now, {"id": act.id, "action": act.action, "source": act.source, "about": act.about, "unit": act.unit,
                                   "command": act.id, "allowed": ok, "check": check, "made": made}, f"gate:{act.id}", source=act.source, command=act.id)
    return Decision(ok, act.action, check, command=act.id, made=made)


def run(store: Store, stream: str, act: Act, now: datetime) -> Decision:
    """Check and record one staff command in one write transaction."""
    with store.write():
        books, wk = load(store, stream)
        led = Ledger(books)
        d = command(led, wk, act, now)
        if not d.repeated:
            d.events = [e.id for e in store.append(stream, led.pending) if e.id is not None]
        return d
