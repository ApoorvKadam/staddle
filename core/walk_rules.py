"""Rule-based readers for the walkthrough's two word jobs: the comparison arm an AI reader is graded against.

No model, no network, standard library only. The rules are general: part vocabularies with synonyms, symptom
families, negation, hedges and "already done" set aside by clause, belongings and access terms never counted as
work, staff directives never read as facts. A verdict the words do not settle is "unsure", which is the safe answer
on both jobs: "same" needs a part both reports name, "different" needs a part on each side and no part in common.

same_fault(first, second) -> "same" | "different" | "unsure"
    Each report is a dict with "text", "by", "at" (ISO date and time) and "unit". "by" is accepted and not used:
    no rule here depends on who wrote a report.

note_work(note, unit) -> set of {"cleaning", "repair"}
    Work counts only in a clause that is not a hedge, not negated, not already done, not about belongings or access,
    and not a directive. A note that names a different unit gives no work.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

SAME, DIFFERENT, UNSURE = "same", "different", "unsure"
CLEANING, REPAIR = "cleaning", "repair"

# two reports further apart than this may be a new failure after a repair, so "same" is not claimed
GAP_LIMIT = timedelta(hours=48)

# A leaf is one part of a unit, with its synonyms. The first pattern to take a phrase keeps it, so "wall panel" is
# not also "panel". A generic word ("door", "light") names a thing without its part: it can agree with another generic
# word, but never settles that two reports are about one part.
LEAVES = [
    ("WALL_PANEL", r"wall (?:panels?|sections?|sheets?|boards?)"),
    ("TRACK", r"tracks?|rails?|channels?"),
    ("ROLLER", r"rollers?|wheels?"),
    ("HINGE", r"hinges?"),
    ("LATCH", r"latch(?:es)?|hasps?|bolts?|metal catch"),
    ("LOCK", r"locks?|cylinders?|barrels?|keyways?|keys?"),
    ("SEAL", r"seals?|weather ?strips?|rubber|flaps?|gaskets?|thresholds?"),
    ("PANEL", r"panels?"),
    ("HANDLE", r"handles?|knobs?"),
    ("SPRING", r"springs?"),
    ("MOTOR", r"motors?|openers?"),
    ("BULB", r"bulbs?|ballasts?"),
    ("COVER", r"covers?|clips?|lens|lenses|diffusers?"),
    ("SWITCH", r"switch(?:es)?"),
    ("OUTLET", r"outlets?|sockets?|receptacles?|plugs?"),
    ("WIRING", r"wires?|wiring|breakers?"),
    ("DRAIN", r"drain(?: lines?| pans?)?|condensate|drip pans?"),
    ("FAN", r"fans?|blowers?"),
    ("PADLOCK", r"padlocks?"),
    ("KEYPAD", r"keypads?"),
    ("CAMERA", r"cameras?"),
    ("GATE", r"gates?"),
]
GENERIC = {
    "DOOR": r"doors?|roll-?up|shutters?",
    "LIGHT": r"lights?|lighting|fixtures?|tubes?",
    "AC": r"a/c|ac|air conditioners?|hvac|mini-?splits?",
}
LOCATION = [
    ("UP", r"top|upper|overhead|ceiling|above"),
    ("DOWN", r"bottom|lower|under|underneath|floor|below"),
    ("LEFT", r"left"),
    ("RIGHT", r"right"),
]
_OPPOSITE = (("UP", "DOWN"), ("LEFT", "RIGHT"))

# what a fault looks like, from either report; two reports may share a part and still differ in kind of symptom
SYMPTOMS = {
    "DAMAGE": re.compile(
        r"\b(?:broke\w*|broken|break\w*|crack\w*|split\w*|rip+ed|rips?|ripping|tor[nm]|tear\w*|bent|bends?|dent\w*|crooked|"
        r"loose\w*|separat\w*|pulling apart|pulled apart|hangs?|hanging|sagg\w*|weld\w*|jump\w*|out of (?:the )?(?:track|rail)|"
        r"off (?:the )?(?:track|rail)|sitting outside|pulling out|pulled out|coming out|come out|came out|came off|dislodg\w*|"
        r"displace\w*|out of place|missing|flush|uneven|funny|wires? showing|exposed|stripped|clogged)\b"),
    "STUCK": re.compile(
        r"\b(?:stuck(?! to)|sticks?|sticking|jam\w*|bind\w*|won'?t(?! turn on| come on| light)|can'?t|cannot|hard to|"
        r"catch(?:es|ing)? (?:on|there|at)|doesn'?t (?:catch|latch|close|lock|roll|open|turn|line up|slide|shut|go|move)|"
        r"not (?:catch|latch|close|lock|roll|open|turn|line up|slide|shut|go|move)\w*|line up|lined up|stops?|halfway|"
        r"hits?|hitting|keeps? (?:stopping|sticking))\b"),
    "FLICKER": re.compile(r"\b(?:flicker\w*|strobe\w*|flash\w*|blink\w*)\b"),
    "WATER": re.compile(r"\b(?:water\w*|rain\w*|leak\w*|drip\w*|puddle\w*|wet|damp|seep\w*|moisture|flood\w*|stain\w*)\b"),
    "NOISE": re.compile(r"\b(?:grind\w*|clunk\w*|squeal\w*|rattl\w*|loud\w*|screech\w*|buzz\w*|noise\w*|sound\w*)\b"),
    "POWER": re.compile(
        r"\b(?:dead|no power|no light|no lights|won'?t turn on|won'?t come on|doesn'?t turn on|does nothing|not turning on|"
        r"burn\w* out|burnt out|blown|nothing happens|no electricity)\b"),
}

# staff text that is not a report of the unit's state: a manager's approval, a decision, a fee, an instruction to others
DIRECTIVE = re.compile(
    r"\b(?:approved?|approval|decisions?|merge|merged|closure|skip|online|ready|charge|charged|fees?|bill|billed|dump|"
    r"throw away|dispose|disposal|write off|waive|waived)\b|\$\d|\bclose (?:it|out|the)\b|\bput (?:it|the unit) online\b")
# uncertain wording: a guess is not a confirmed fault or a confirmed piece of work
HEDGE = re.compile(
    r"\b(?:might|maybe|could|possibly|probably|perhaps|not sure|unsure|hard to tell|i think|i guess|pretty sure|i'm not sure|"
    r"looks? like|seems? like|appears?|one time|once in a while|sometimes|now and then|a while back|guess)\b|\b\w+-looking\b")
# a repair already made, reported second-hand or as done: it is no longer the open fault
PRIOR = re.compile(
    r"\b(?:replaced|repaired|(?<!to be )fixed|serviced|maintenance (?:came|was|has|did|fixed)|somebody (?:already )?(?:fixed|replaced|"
    r"repaired)|already (?:fixed|replaced|repaired|serviced))\b")
# belongings and access items left by a former tenant: never cleaning or repair work
BELONG = re.compile(
    r"\b(?:bikes?|bicycles?|couch(?:es)?|sofas?|suitcases?|bags?|(?<!outlet )(?<!electrical )boxes|(?<!outlet )(?<!electrical )box|"
    r"clothes|clothing|furniture|mattress(?:es)?|dressers?|tables?|chairs?|beds?|cribs?|appliances?|fridge|refrigerators?|tvs?|"
    r"televisions?|junk|belongings?|toys?|tubs?|totes?|bins?|crates?|desks?|rugs?|tires?|mowers?|padlocks?|stuff left|"
    r"things left|items left|left behind|crap)\b")
CLEAN_CUE = re.compile(
    r"\b(?:dust|dusty|dusting|dirt|dirty|filthy|gross|mud|muddy|sticky|grime|grimy|greasy|grease|cobwebs?|bugs|wrappers?|trash|debris|mess|messy|"
    r"litter|scrape|scraped|scraping|sweep|sweeping|mop|mopping|spills?|spilled|soda|crumbs|gum|"
    r"needs? (?:a |some )?(?:good |quick |full |thorough |deep )?(?:cleaning|clean|sweep|mop|scrub)|need (?:to )?(?:clean|sweep|mop|scrub|dust)\w*|"
    r"stuck to (?:the )?(?:floor|carpet)|stains? (?:on|across|all over) (?:the )?floor|wet (?:patch|spot|spots|floor|underneath|area)|"
    r"floor (?:is |was |has )?wet)\b")
REPAIR_CUE = re.compile(
    r"\b(?:broke|broken|break(?:s|ing)?|cracked|cracks?|split|ripped|rips?|torn|tears?|bent|dented|dents?|crooked|loose|"
    r"hangs? (?:funny|crooked|uneven|down|loose|low|off)|hanging (?:down|loose|off|low)|sagging|welds?|"
    r"separat(?:e|es|ing|ed)|pulling (?:apart|out)|pulled (?:apart|out)|flicker(?:s|ing)?|strobe|drips?|dripping|leaks?|leaking|"
    r"clogged|clogs?|water (?:damage|coming|getting|dripping|leaks?)|grind(?:s|ing)?|clunk(?:s|ing)?|squeal\w*|rattl\w*|screech\w*|"
    r"needs? (?:fixing|a fix|repairs?|replac\w*)|need (?:to )?(?:fix|repair|replace)\w*|needs? to be (?:fixed|repaired|replaced)|"
    r"to be replaced|needs? (?:a )?new|needs? replacing|replacement|damaged|busted|snapped|smashed|shattered|(?:is|are) missing|"
    r"burn(?:s|ed|t)? out|blown|wires? showing|exposed wires?|won'?t|can'?t|"
    r"(?:doesn'?t|don'?t|does not|do not) (?:catch|latch|close|lock|roll|open|turn|line up|slide|shut|go|move|work|turn on|come on|seal)|"
    r"stuck(?! to)|sticks?|sticking|hard to (?:push|close|open|slide|shut|lift|pull)|jam(?:s|med)?|does nothing|no power|"
    r"no light|dead(?! bugs?)|lights? (?:dead|out)|not (?:catch|latch|close|lock|roll|open|turn|line up|slide|shut|go|move|work)\w*|"
    r"missing (?:a |the |its )?(?:handle|lock|latch|hinge|screw|bolt|knob|panel|tile|cover|seal|strip|light|bulb))\b")
# a negation: the cue after it in the same clause is not true of the unit
NEG = re.compile(
    r"\b(?:no|none|nothing|never|without|nothin)\b|\bnot (?:needed|necessary|needing|leaking|broken|damaged|bent|loose|cracked|"
    r"dirty|dusty|wet|an issue|a problem|any)\b|\b(?:is|are|was|were)n'?t (?:broken|damaged|bent|loose|cracked|dirty|dusty|wet|"
    r"leaking|split|torn)\b|\b(?:didn'?t|don'?t|did not|do not) (?:see|notice|find|need)\b|\bdoesn'?t need\b|\b(?:needs?|need) no\b|"
    r"\b(?:fine|okay|ok|spotless)\b|"
    r"\b(?:is|are|was|were|looks?|looked|seems?|seemed|runs?|works?|opens?|shuts?|rolls?|look|looking)\s+(?:fine|okay|ok|normal|smooth|good|nice)\b")
# a clause that says the part is fine: an observation of no fault, so it must not read as a fault report. Narrower than NEG:
# "no power" and "won't latch" are faults in the unit's own words
NOT_FAULT = re.compile(
    r"\b(?:not|never|isn'?t|aren'?t|wasn'?t|weren'?t|hasn'?t|haven'?t)\s+(?:\w+\s+)?(?:broken|broke|cracked|damaged|bent|loose|split|torn|ripped|"
    r"dented|leaking|stuck|jammed|sagging|missing|clogged|dead|flickering)\b|"
    r"\bno\s+(?:visible\s+|signs? of\s+)?(?:cracks?|damage|leaks?|dents?|tears?|issues?|problems?|wear|rips?)\b|"
    r"\b(?:is|are|was|were|looks?|looked|seems?|seemed|runs?|works?|opens?|shuts?|rolls?|sits?)\s+(?:fine|okay|ok|normal|smooth|good|nice|intact)\b")
DONE_CLEAN = re.compile(r"\b(?:swept|cleaned|mopped|vacuumed|spotless|already cleaned|already swept)\b")
DONE_REPAIR = re.compile(r"\b(?:already (?:fixed|replaced|repaired)|(?:fixed|replaced|repaired) (?:already|yesterday|earlier))\b")

_UNIT = re.compile(r"\b([A-Za-z])-?(\d{1,3})\b")
_SENTENCE = re.compile(r"[.!?;:\n]+")
_CLAUSE = re.compile(r",|\b(?:and|but|though|however|although|while)\b")


def _clean(text: object) -> str:
    t = " ".join(str(text or "").lower().replace("’", "'").split())
    return re.sub(r"\b(won|can|don|doesn|didn|isn|aren|wasn|weren|couldn|shouldn|wouldn)t\b", r"\1't", t)


def _sentences(text: object) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(_clean(text)) if s.strip()]


def _clauses(sentence: str) -> list[str]:
    return [c.strip() for c in _CLAUSE.split(sentence) if c.strip()]


def _unit_key(value: object) -> str:
    m = re.fullmatch(r"\s*([A-Za-z])-?(\d{1,3})\s*", str(value or ""))
    return f"{m[1].upper()}{int(m[2])}" if m else str(value or "").strip().upper()


def _parts(sentence: str) -> tuple[set[str], set[str]]:
    """The leaf parts and the generic things a sentence names; a leaf's words are used up so no other pattern takes them."""
    leaves: set[str] = set()
    rest = sentence
    for name, pat in LEAVES:
        rx = re.compile(rf"\b(?:{pat})\b")
        if rx.search(rest):
            leaves.add(name)
            rest = rx.sub(" ", rest)
    generic = {name for name, pat in GENERIC.items() if re.search(rf"\b(?:{pat})\b", rest)}
    return leaves, generic


def _locations(sentence: str) -> set[str]:
    return {name for name, pat in LOCATION if re.search(rf"\b(?:{pat})\b", sentence)}


def _symptoms(sentence: str) -> set[str]:
    return {name for name, rx in SYMPTOMS.items() if rx.search(sentence)}


class _Report:
    """One report, read sentence by sentence: current leaves, the leaves of a repair already made, and the symptoms."""

    def __init__(self, text: object) -> None:
        self.leaves: set[str] = set()
        self.prior: set[str] = set()
        self.generic: set[str] = set()
        self.symptoms: set[str] = set()
        self.locations: set[str] = set()
        whole_prior = False
        for s in _sentences(text):
            hedged = bool(HEDGE.search(s))
            # a request or a repair already made rules out its own clause, not the fault reported beside it
            for clause in _clauses(s):
                if DIRECTIVE.search(clause):
                    continue
                leaves, generic = _parts(clause)
                if PRIOR.search(clause):
                    if leaves:
                        self.prior |= leaves
                    else:
                        whole_prior = True  # "it was replaced": the repair may be to any part this report names
                    continue
                if hedged or NOT_FAULT.search(clause):
                    continue
                self.leaves |= leaves
                self.generic |= generic
                self.symptoms |= _symptoms(clause)
                self.locations |= _locations(clause)
        if whole_prior:
            self.prior |= self.leaves


def _when(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _gap(first: dict, second: dict) -> timedelta | None:
    a, b = _when(first.get("at")), _when(second.get("at"))
    if a is None or b is None:
        return None
    try:
        return abs(b - a)
    except TypeError:  # one time has a zone and the other does not
        return None


def _locations_conflict(a: set[str], b: set[str]) -> bool:
    for p, q in _OPPOSITE:
        if (p in a and q in b and p not in b) or (q in a and p in b and q not in a):
            return True
    return False


def same_fault(first: dict, second: dict) -> str:
    """Whether two open repair reports on one unit describe one fault, two faults, or cannot be told apart from the words."""
    if _unit_key(first.get("unit")) != _unit_key(second.get("unit")):
        return UNSURE
    a, b = _Report(first.get("text")), _Report(second.get("text"))
    if a.leaves and b.leaves and not (a.leaves & b.leaves):
        if a.symptoms == {"POWER"} and b.symptoms == {"POWER"}:
            return UNSURE  # two dead circuits on different parts can share one breaker
        return DIFFERENT
    if a.leaves and b.leaves:
        if a.leaves != b.leaves:
            return UNSURE  # one report names parts the other does not
    elif not (a.generic and a.generic == b.generic and not a.leaves and not b.leaves and a.symptoms & b.symptoms):
        # no named part on at least one side: the same generic thing with the same symptom is the only other agreement
        return UNSURE
    if a.symptoms and b.symptoms and not (a.symptoms & b.symptoms):
        return UNSURE
    if _locations_conflict(a.locations, b.locations):
        return UNSURE
    if (a.leaves & b.prior) or (b.leaves & a.prior):
        return UNSURE  # the part was already repaired: this may be a new failure
    gap = _gap(first, second)
    if gap is not None and gap > GAP_LIMIT:
        return UNSURE
    return SAME


def _confirmed(clause: str, cue: re.Pattern, stoppers: tuple[re.Pattern, ...]) -> bool:
    """A cue counts when no negation or "already done" word comes before it in the same clause."""
    starts = [m.start() for m in (s.search(clause) for s in stoppers) if m]
    first_stop = min(starts) if starts else len(clause) + 1
    return any(m.start() <= first_stop for m in cue.finditer(clause))


def note_work(note: str, unit: str) -> set[str]:
    """The cleaning and repair work a move-out note confirms for this unit, read clause by clause."""
    named = {f"{letter.upper()}{int(number)}" for letter, number in _UNIT.findall(str(note or ""))}
    if named and named != {_unit_key(unit)}:
        return set()  # the note is about another unit
    work: set[str] = set()
    for sentence in _sentences(note):
        for clause in _clauses(sentence):
            if DIRECTIVE.search(clause) or BELONG.search(clause) or HEDGE.search(clause):
                continue
            if _confirmed(clause, CLEAN_CUE, (NEG, DONE_CLEAN)):
                work.add(CLEANING)
            if _confirmed(clause, REPAIR_CUE, (NEG, DONE_REPAIR)):
                work.add(REPAIR)
    return work
