"""Compact work-order forms: one staff submission that commits the several ordinary work-order moves it stands for.

A work order keeps all its states (reported, triaged, assigned, work recorded, staff checked, closed). A form does not skip one: each move
is its own command through the walk gate, so each is checked, authorized and recorded as its own event, in the order the states run. What the
form removes is the need for a separate screen for each move. Everything a move records comes from what the staff member typed or chose in the
form; nothing is made up to carry an order along: no triage words, assignee, time or staff check that the submission did not give.

A submission is all or nothing. Every move is decided on the folded records first and the events are written in one transaction, so an
interruption leaves either every event or none, and a refusal at any move writes nothing. A submission sent twice (the same command id) decides
nothing new and writes nothing."""
from dataclasses import dataclass, field

from core import walk
from core.billing import Ledger
from core.gate import STAFF_ROLES

UNKNOWN = {"lock": "unknown", "whose": "unknown", "tag": "unknown", "door": "unknown"}
READY, NOT_READY = "Ready to rent", "Not ready to rent"


@dataclass
class Outcome:
    allowed: bool
    reason: str | None = None
    events: list = field(default_factory=list)       # the stored events this submission wrote
    repeated: bool = False                          # the same submission was already committed: nothing was written

    @property
    def woke(self) -> bool:
        """Whether the committed records are ones Tilden wakes on: an order closed, or a walk note written. A submission that leaves an order
        half finished wakes nothing, and one that writes several of these is still one wake."""
        return any(e.kind == "work_order.closed" or (e.kind == "walk.observed" and e.payload.get("note")) for e in self.events)


class Refusal(Exception):
    pass


def commit(store, stream, steps, now) -> Outcome:
    """Decide each step on the folded records, one after the other, and write every event in one transaction. A step is (name, make): make
    receives what the earlier steps made (a walk's id) and returns the walk command."""
    with store.write():
        books, wk = walk.load(store, stream)
        led = Ledger(books)
        made: dict = {}
        for name, make in steps:
            decision = walk.command(led, wk, make(made), now)
            if not decision.repeated and not decision.allowed:
                return Outcome(False, decision.check)      # nothing is appended: the earlier moves of this submission are not kept
            made[name] = decision.made
        written = store.append(stream, led.pending)
    return Outcome(True, events=[e for e in written if e.id is not None], repeated=not led.pending)


def _words(text) -> str:
    return " ".join((text or "").split())


def _need(text, what: str) -> str:
    text = _words(text)
    if len(text) < 3:
        raise Refusal(what)
    return text


def _order(wk, order_id, kind):
    o = wk.orders.get(order_id or "")
    if o is None:
        raise Refusal("there is no such work order")
    if (o.kind == "check") != (kind == "check"):
        raise Refusal("that is not " + ("an inspection" if kind == "check" else "a repair") + " order")
    return o


def _move(cid, source, action, order, **kw):
    return (action, lambda made: walk.Act(f"{cid}:{action}:{order}", action, source, about=order, **kw))


def _ordinary_steps(o, cid, source, scope, person, work):
    """The moves from where the order is now to its work being recorded: triage and assignment from what the form confirmed, then the work."""
    steps = []
    scope = _need(scope, "say what the work is")
    person = _need(person, "say who does the work")
    if person not in STAFF_ROLES and not person.startswith("person:"):
        person = "person:" + person
    if o.state == "reported":
        steps.append(_move(cid, source, "triage_work_order", o.id, text=scope))
    else:
        recorded_scope = next((c.text for c in reversed(o.changes) if c.state == "triaged"), "")
        if scope != recorded_scope:
            raise Refusal("confirm the recorded work scope")
    if o.state in ("reported", "triaged"):
        steps.append(_move(cid, source, "assign_work_order", o.id, to=person))
    elif o.state == "assigned" and person != o.assigned_to:
        raise Refusal("confirm the person already assigned")
    if o.state not in ("reported", "triaged", "assigned"):
        raise Refusal("work is already recorded on that order")
    steps.append(_move(cid, source, "record_order_work", o.id, text=work))
    return steps


def record_repair(wk, order, *, work, scope, person, source, cid) -> list:
    """1. Record the repair: the work performed, with the scope and the person confirmed in the same form."""
    o = _order(wk, order, "repair")
    return _ordinary_steps(o, cid, source, scope, person, _need(work, "say what work was done"))


def check_repair(wk, order, *, accept, words, source, cid) -> list:
    """2. Check the recorded repair: Accept is the staff check, and the order closes once it is checked; Needs more work returns it."""
    o = _order(wk, order, "repair")
    if o.state != "work recorded":
        raise Refusal("only recorded work that has not been checked can be reviewed")
    if accept:
        return [_move(cid, source, "check_order_work", o.id, text=_need(words, "say what you checked")), _move(cid, source, "close_work_order", o.id)]
    return [_move(cid, source, "return_order_work", o.id, text=_need(words, "say what is still to do"))]


def inspect_unit(wk, unit, *, order, ready, observations, scope, person, source, cid) -> list:
    """3. Inspect the unit: one walk of this unit with its observation, written the way the walk page writes one, and, when an inspection order
    is open, that order's moves up to its work being recorded. The verdict is recorded on the observation; the form fields staff did not give stay
    unknown."""
    text = f"{READY if ready else NOT_READY}. {_need(observations, 'say what you saw')}"
    steps = []
    o = _order(wk, order, "check") if order else None
    if o and o.state not in ("reported", "triaged", "assigned"):
        raise Refusal("that inspection has already been recorded")
    if o and o.unit != unit:
        raise Refusal("that inspection is for another unit")
    if o:
        steps += _ordinary_steps(o, cid, source, scope, person, text)[:-1]
    steps += [("walk", lambda made: walk.Act(f"{cid}:start_walk:{unit}", "start_walk", source, route=[unit])),
              ("seen", lambda made: walk.Act(f"{cid}:observe_unit:{unit}", "observe_unit", source, about=made["walk"], unit=unit, form={**UNKNOWN, "verdict": "ready" if ready else "not ready"}, text=text)),
              ("done", lambda made: walk.Act(f"{cid}:finish_walk:{unit}", "finish_walk", source, about=made["walk"]))]
    if o:
        steps.append(_move(cid, source, "record_order_work", o.id, text=text))
    return steps


def check_inspection(wk, order, *, accept, words, source, cid) -> list:
    """4. Check the inspection record: Accept is the staff check and the order closes; Return for correction sends it back. Accepting the record
    says the inspection was done properly, never that the unit is ready: a negative observation stays on the records and still blocks release."""
    o = _order(wk, order, "check")
    if o.state != "work recorded":
        raise Refusal("only a recorded inspection that has not been checked can be reviewed")
    if accept:
        return [_move(cid, source, "check_order_work", o.id, text=_need(words, "say what you checked")), _move(cid, source, "close_work_order", o.id)]
    return [_move(cid, source, "return_order_work", o.id, text=_need(words, "say what to correct"))]
