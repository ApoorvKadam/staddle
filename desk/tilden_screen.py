"""The Tilden screen: what the morning shift is working on, what waits for staff, what is done, and one case in time order, all read from
the visitor's records. Nothing here is written for the page: a line is a record or a recorded step, so a reload rebuilds the page and shows
the same thing. The four staff forms on a case go through core/work_forms.py, the same moves the Walkthrough page makes one at a time."""
from __future__ import annotations

import re
import secrets
from datetime import datetime

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from core import walk, work_forms
from core.books import local
from core.tilden import cases, gate, shift

from .wording import plain

ROLES = ("front_desk", "office_manager", "manager")
TOKEN = re.compile(r"[0-9a-f]{12}")
ACTIONS = ("record_repair", "check_repair", "inspect", "check_inspection")
# what each recorded second check was asked, in the words a person reads
SECOND = {"same fault": "the two reports describe one fault", "ready": "the latest look says the unit is fine to rent",
          "tenant items": "the work would not remove anything the former tenant left", "no work needed": "the reported problem needs no work"}
ORDER_MOVES = {"work_order.triaged": "Triaged the work", "work_order.assigned": "Assigned the work", "work_order.recorded": "Recorded the work",
               "work_order.checked": "Checked the recorded work", "work_order.closed": "Closed the work order", "work_order.returned": "Sent the work back for more"}
COUNTERS = {"tool_calls", "decisions", "wake_start", "fixed_next", "reader_cents"}


_DISPLAY_IDS = re.compile(r"\b(?:WO|W|O)\d+\b|\b[0-9a-f]{32}\b|\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.I)


def screen_text(value: str | None) -> str:
    """Show record wording without the internal identifiers used to link it."""
    return plain(_DISPLAY_IDS.sub("", plain(value or "")))


def second_words(step: dict) -> str:
    """Describe the recorded second check, without displaying its numerical score."""
    name = step.get("second_opinion")
    label = SECOND.get(name, "the question asked")
    policies = {"ready": (gate.READY_CUTOFF, True), "same fault": (gate.SAME_FAULT_CUTOFF, True),
                "no work needed": (gate.NO_WORK_CUTOFF, True), "tenant items": (gate.BELONGINGS_CUTOFF, False)}
    value = step.get("p")
    if name not in policies or isinstance(value, bool) or not isinstance(value, (int, float)):
        return "Second check recorded: " + label
    cutoff, positive = policies[name]
    # The stored score is rounded; at the boundary it cannot establish which side the original value fell on.
    if abs(value - cutoff) < 0.00051:
        return "Second check recorded: " + label
    agreed = (value > cutoff) if positive else (value < cutoff)
    return ("Agreed: " if agreed else "Did not agree: ") + label


def public_reason(reason: str) -> str:
    """Keep internal accounting and confidence details out of operational activity."""
    if re.search(r"\b(?:budget|allowance|cents|cost|spend|spending|reservation|probability|confidence|tokens?)\b", reason or "", re.I):
        return "Tilden cannot continue this work yet."
    return screen_text(reason)


def day_of(now: datetime) -> str:
    return local(now).date().isoformat()


def job(unit: str) -> str:
    return f"Get unit {unit} ready to rent"


def staff_actions(wk, unit: str) -> list[dict]:
    """The staff steps the unit's open work orders and records ask for now, from the records: each is one of the four forms."""
    out = []
    orders = [o for o in sorted(wk.orders.values(), key=lambda o: (o.reported, o.id)) if o.unit == unit and o.state != "closed" and not o.joined_into]
    for o in orders:
        check = o.kind == "check"
        if o.state in ("reported", "triaged", "assigned"):
            out.append({"kind": "inspect" if check else "record_repair", "order": o})
        elif o.state == "work recorded":
            out.append({"kind": "check_inspection" if check else "check_repair", "order": o})
    return out


def staff_words(action: dict) -> str:
    o = action["order"]
    return {"record_repair": f"Waiting for the repair to be recorded: {o.problem if o else ''}",
            "check_repair": "The recorded repair waits for a staff check.",
            "inspect": "An inspection is needed before readiness can be established." if o else "Waiting for staff to look at the unit and write what they see.",
            "check_inspection": "The recorded inspection waits for a staff check."}[action["kind"]]


def _record_words(store, stream, event_id):
    for e in store.events(stream):
        if e.id == event_id:
            p = e.payload
            return p.get("note") or p.get("problem") or p.get("text") or ""
    return ""


def _record_link(store, stream, event_id, unit):
    event = next((e for e in store.events(stream) if e.id == event_id), None)
    if event and event.kind.startswith("work_order.") and event.payload.get("order"):
        return (f"/walkthrough?at=orders#order-{event.payload['order']}", "Open the work order")
    if event and event.kind == "walk.observed" and event.payload.get("unit") == unit:
        return (f"/tilden/case/{unit}#activity-{event_id}", "Open the observation")
    return (f"/units#u-{unit}", "Open the unit")


def timeline(store, stream, case, wk, offered: bool) -> list[dict]:
    """The case's activity in the order it was written: the recorded steps of the case (a second check, a refusal, a handoff, a reason for taking
    the case up again) and the records the unit's case changed or staff wrote."""
    unit = case.unit
    order_ids = {o.id for o in wk.orders.values() if o.unit == unit}
    problem = {o.id: o.problem for o in wk.orders.values()}
    items: list[tuple[int, dict]] = []

    def add(event_id, actor, title, detail="", link=None, cls=None, anchor=None):
        items.append((event_id, {"actor": actor, "cls": cls or {"Tilden": "tilden", "Staff": "staff", "Second check": "second"}.get(actor, "staddle"),
                                 "title": title, "detail": screen_text(detail) if actor == "Staff" else public_reason(detail),
                                 "link": link, "sequence": event_id, "anchor": anchor}))
    flat = []
    seen: list = []
    for h in case.history:
        steps = h["payload"].get("steps")
        if steps is None:
            continue
        fresh = steps[len(seen):] if steps[:len(seen)] == seen else steps
        flat += [(h["id"], s) for s in fresh if isinstance(s, dict)]
        seen = steps
    for i, (hid, s) in enumerate(flat):
        keys = set(s) - COUNTERS
        if not keys:
            continue
        if "second_opinion" in s:
            add(hid, "Second check", second_words(s))
        elif "judged_event" in s:
            add(hid, "Tilden", "Read a staff record", _record_words(store, stream, s["judged_event"]), _record_link(store, stream, s["judged_event"], unit))
        elif "handoff" in s:
            add(hid, "Tilden", "Handed to staff", s["handoff"].get("question", ""), (f"/tilden/case/{unit}", "Open the case"))
        elif "refused" in s:
            add(hid, "Staddle's checks", "Refused the action", s["refused"])
        elif "reason" in s:
            evidence = s.get("evidence_ids") or []
            if s.get("shift_wait"):
                add(hid, "Tilden", "Waiting", s["reason"])
            elif evidence:
                words = [w for w in (_record_words(store, stream, x) for x in evidence) if w]
                add(hid, "Tilden", "Took the case up again after new staff records", s["reason"] + (" " + "; ".join(words) if words else ""), _record_link(store, stream, evidence[0], unit))
            else:
                add(hid, "Tilden", "Recorded a reason", s["reason"])
        elif "unsettled_question" in s:
            add(hid, "Tilden", "The new records did not settle the question", s["unsettled_question"])
    verified_releases = {s["release_event"] for _, s in flat if s.get("release_verified") is True and s.get("release_event")}
    for e in store.events(stream):
        if e.id < case.source_event:
            continue
        p, by = e.payload, str(e.payload.get("by") or e.source or "")
        mine = p.get("case_id") == case.id
        tilden = by == "agent:tilden"
        about = p.get("unit") == unit or p.get("order") in order_ids
        if e.id == case.source_event and e.kind == "lease.ended":
            add(e.id, "Staddle's checks", "Move-out recorded", f"Unit {unit} was moved out of.", (f"/units#u-{unit}", "Open the unit"))
        elif e.kind == "unit.blocked" and p.get("unit") == unit:
            add(e.id, "Tilden" if mine else "Staff", "Held the unit off online renting" if mine else "Placed a hold on the unit", p.get("reason", ""), (f"/units#u-{unit}", "Open the unit"))
        elif e.kind == "unit.unblocked" and mine:
            add(e.id, "Tilden", "Released the unit", "The hold this case placed was lifted.", (f"/units#u-{unit}", "Open the unit"))
            if e.id in verified_releases:
                add(e.id, "Staddle's checks", "Read back from the online rentals page", f"Unit {unit} was offered there again.", ("/rent", "Open the online rentals page"))
        elif e.kind == "work_order.reported" and about:
            if tilden:
                add(e.id, "Tilden", "Asked for an inspection" if p.get("kind") == "check" else "Opened a work order", p.get("problem", ""), (f"/walkthrough?at=orders#order-{p.get('order', '')}", "Open the work order"))
            else:
                add(e.id, "Staff", "Reported a repair", p.get("problem", ""), (f"/walkthrough?at=orders#order-{p.get('order', '')}", "Open the work order"))
        elif e.kind == "work_order.joined" and about:
            add(e.id, "Tilden" if tilden else "Staff", "Linked two reports of the same fault", f"{problem.get(p['order'], '')} joined into {problem.get(p['into'], '')}")
        elif e.kind == "work_order.closed" and about and p.get("no_work"):
            add(e.id, "Tilden" if tilden else "Staff", "Closed a report that needed no work", p.get("text", ""))
        elif e.kind in ORDER_MOVES and about:
            add(e.id, "Tilden" if tilden else "Staff", ORDER_MOVES[e.kind], ((p.get("text") or "") + (f" To {p['to'].replace('_', ' ')}." if p.get("to") else "")).strip(),
                (f"/walkthrough?at=orders#order-{p.get('order', '')}", "Open the work order"))
        elif e.kind == "walk.observed" and p.get("unit") == unit and not tilden:
            add(e.id, "Staff", "Walk note recorded", p.get("note", ""), ("/walkthrough#latest-walk", "Open the walk"), anchor=f"activity-{e.id}")
        elif e.kind == "gate.decided" and p.get("allowed") is False and mine:
            add(e.id, "Staddle's checks", "Refused the action", p.get("check", ""))
    items.sort(key=lambda x: x[0])      # stable: entries from one record keep the order they were added in
    return [entry for _, entry in items]


def mount(app, fd, *, show, need, same_site, form, budget_left, live, facility, offered, clock, role_name) -> None:
    def pick(f, unit: str):
        found = [c for c in cases.load(fd.ready(), fd.stream(f.ident)).values() if c.unit == unit]
        if not found:
            raise HTTPException(404, "Tilden has no case for that unit.")
        return max(found, key=lambda c: c.source_event)

    def row(f, store, stream, case, wk, offered_ids: set, running: bool, admitted: set) -> dict:
        unit, now = case.unit, f.now
        marks = [s for s in case.steps if isinstance(s, dict)]
        last = marks[-1] if marks else {}
        actions = staff_actions(wk, unit)
        status = f.rent.status(unit, now)
        held = status == "blocked"
        if case.state == "done" and unit in offered_ids:
            kind, words = "done", f"Released. Unit {unit} is offered again on the online rentals page."
        elif case.state == "done":
            kind, words = "waiting", f"Unit {unit} is not offered on the online rentals page."
        elif case.id not in admitted and last.get(shift.PAUSED):
            kind, words = "later", last.get("reason", "Not started.")
        elif case.state == "paused":
            kind, words = "waiting", last.get("reason") or "Paused for now."
        elif case.state == "blocked":
            kind, words = "waiting", "Tilden stopped on this case and staff need to look at it."
        elif case.state == "needs staff":
            handoff = next((s["handoff"] for s in reversed(marks) if "handoff" in s), {})
            kind, words = "waiting", handoff.get("question") or (staff_words(actions[0]) if actions else "Waiting for staff records.")
        elif case.state == "waiting":
            kind, words = "waiting", staff_words(actions[0]) if actions else "Waiting for new records on the unit."
        else:
            entries = timeline(store, stream, case, wk, unit in offered_ids)
            kind, words = ("working" if running else "later"), (entries[-1]["title"] if entries else "Case opened.")
        return {"unit": unit, "job": job(unit), "kind": kind, "words": public_reason(words), "held": held, "status": status,
                "actions": [{"kind": a["kind"], "order": a["order"].id if a["order"] else ""} for a in actions] if kind == "waiting" and case.state != "done" else [],
                "walkthrough": kind == "waiting" and not actions and case.state != "done"}

    @app.get("/tilden", include_in_schema=False)
    def tilden_page(request: Request):
        ident = need(request)
        f = facility(fd, ident)
        store, stream = fd.ready(), fd.stream(ident)
        wk = f.rent.floor
        admitted = {e.payload["case"] for e in store.stream_events(stream) if e.kind == "tilden.shift.admitted" and e.payload.get("day") == day_of(f.now)}
        offered_ids = {u.id for u in offered(f)}
        running = fd.shift_running(ident)
        rows = [row(f, store, stream, c, wk, offered_ids, running, admitted) for c in sorted(cases.load(store, stream).values(), key=lambda c: (c.source_event, c.unit))]
        activity = []
        for c in cases.load(store, stream).values():
            for entry in timeline(store, stream, c, wk, c.unit in offered_ids)[-6:]:
                activity.append({**entry, "unit": c.unit})
        started = bool(admitted) or any(r["kind"] == "later" for r in rows)
        return show(request, "fms_tilden.html", "tilden", f, rows=rows, groups=[(k, label, [r for r in rows if r["kind"] == k]) for k, label in (
            ("working", "Working now"), ("waiting", "Waiting for staff"), ("later", "Waiting to continue"), ("done", "Done"))],
            activity=sorted(activity, key=lambda e: e["sequence"], reverse=True)[:12], started=started, running=running, live_on=live, token=secrets.token_hex(6), job=job)

    @app.post("/tilden/start", include_in_schema=False)
    async def tilden_start(request: Request):
        same_site(request)
        ident = need(request)
        data = await form(request)
        token = data.get("token", "")
        if not TOKEN.fullmatch(token):
            raise HTTPException(422, "Reload the page and try again.")
        await run_in_threadpool(fd.start_shift_async, ident, token, budget_left if live else None, live)
        return RedirectResponse("/tilden", status_code=303)

    @app.get("/tilden/case/{unit}", include_in_schema=False)
    def tilden_case(request: Request, unit: str):
        ident = need(request)
        f = facility(fd, ident)
        store, stream = fd.ready(), fd.stream(ident)
        case = pick(f, unit)
        wk = f.rent.floor
        offered_ids = {u.id for u in offered(f)}
        running = fd.shift_running(ident)
        admitted = {e.payload["case"] for e in store.stream_events(stream) if e.kind == "tilden.shift.admitted" and e.payload.get("day") == day_of(f.now)}
        summary = row(f, store, stream, case, wk, offered_ids, running, admitted)
        entries = timeline(store, stream, case, wk, unit in offered_ids)
        forms = []
        for a in staff_actions(wk, unit) if case.state != "done" else []:
            o = a["order"]
            record = next((c for c in reversed(o.changes) if c.state == "work recorded"), None) if o else None
            scope = next((c.text for c in reversed(o.changes) if c.state == "triaged"), "") if o else ""
            forms.append({"kind": a["kind"], "order": o.id if o else "", "problem": screen_text(o.problem) if o else "",
                          "record": {"text": screen_text(record.text)} if record else None,
                          "scope": scope, "scope_display": screen_text(scope), "assigned": o.assigned_to if o else None})
        return show(request, "fms_tilden_case.html", "tilden", f, case=summary, plan=[public_reason(step) for step in case.plan], entries=entries, forms=forms, running=running,
                    roles=ROLES, role_name=role_name, token=secrets.token_hex(6), step_note=plain(fd.take_step_note(ident)) if not request.query_params.get("activity") else "", job=job(unit), clock=clock)

    @app.post("/tilden/case/{unit}/step", include_in_schema=False)
    async def tilden_step(request: Request, unit: str):
        same_site(request)
        ident = need(request)
        data = await form(request)
        action, token, order = data.get("action", ""), data.get("token", ""), data.get("order", "")
        if action not in ACTIONS:
            raise HTTPException(422, "That is not a step staff can take here.")
        if not TOKEN.fullmatch(token):
            raise HTTPException(422, "Reload the page and try again.")
        decision = data.get("decision", "")
        if action in ("check_repair", "check_inspection") and decision not in ("accept", "return"):
            raise HTTPException(422, "Choose whether to accept or return the record.")

        def take() -> None:
            # Staff submissions wait for an ongoing shift before opening a transaction on its store.
            with shift._lock(fd.ready(), fd.stream(ident)):
                with fd.lock:
                    f = facility(fd, ident)
                    pick(f, unit)
                    cid, source = f"tform:{token}:{action}:{order or unit}", "staff:office_manager"
                    wk = walk.load(fd.ready(), fd.stream(ident))[1]
                    if order and (order not in wk.orders or wk.orders[order].unit != unit):
                        raise HTTPException(404, "That work order is not for this unit.")
                    # A retry must return its original result before testing the now-advanced order state.
                    if any(key.startswith(cid + ":") for key in fd.ready().books(fd.stream(ident)).decisions):
                        return
                    try:
                        current_order = wk.orders.get(order)
                        if action in ("record_repair", "inspect") and current_order:
                            if current_order.state in ("triaged", "assigned") and data.get("scope_confirmed") != "yes":
                                raise work_forms.Refusal("confirm the recorded work scope")
                            if current_order.state == "assigned" and data.get("person_confirmed") != "yes":
                                raise work_forms.Refusal("confirm the person already assigned")
                        if action == "record_repair":
                            steps = work_forms.record_repair(wk, order, work=data.get("work"), scope=data.get("scope"), person=data.get("person"), source=source, cid=cid)
                        elif action == "check_repair":
                            steps = work_forms.check_repair(wk, order, accept=decision == "accept", words=data.get("words"), source=source, cid=cid)
                        elif action == "inspect":
                            if data.get("verdict") not in ("ready", "not_ready"):
                                raise work_forms.Refusal("choose ready to rent or not ready to rent")
                            steps = work_forms.inspect_unit(wk, unit, order=order or None, ready=data["verdict"] == "ready", observations=data.get("observations"),
                                                            scope=data.get("scope"), person=data.get("person"), source=source, cid=cid)
                        else:
                            steps = work_forms.check_inspection(wk, order, accept=decision == "accept", words=data.get("words"), source=source, cid=cid)
                    except work_forms.Refusal as why:
                        fd._keep(fd._step_notes, ident, str(why))
                        return
                    outcome = work_forms.commit(fd.ready(), fd.stream(ident), steps, fd.clock())
                    if outcome.allowed:
                        fd._step_notes.pop(ident, None)
                    else:
                        fd._keep(fd._step_notes, ident, re.sub(r"\s*\((?:W|O|WO)\d+\)", "", outcome.reason or "That was refused."))
            # Tilden wakes once, on what was committed: a closed order or a walk note, never a submission that left an order half finished,
            # and not on a submission that was already taken
            if outcome.allowed and not outcome.repeated and outcome.woke and fd.shift:
                fd.wake_shift_async(ident, budget_left if live else None, live)
        await run_in_threadpool(take)
        return RedirectResponse(f"/tilden/case/{unit}", status_code=303)
