"""A fixed-rule worker for the unit readiness job: no model, only keyword rules over staff words and checks on the records.

arm(store, stream, now) is called after each entry is recorded. It opens one case per move-out (a lease that ended), then reads what staff
wrote about the unit since: walk notes, work order reports, the words that finish a work order and the words that finish an inspection.
It acts only through the shared gate (core/tilden/gate.py), so every action carries the owner's grants and the gate's own checks. When
the gate refuses, the reason is read and the worker carries on: it keeps the unit held and waits for a new staff record before it tries the
same action again, and a handoff the gate calls routine is dropped, not repeated.

What it does, in this order each time something new was recorded:
1. Hands to staff, with the staff member's own words and the record that holds them, what only a person can decide: belongings left
   behind (not trash, not a lock on the door, and not a guess), a dispute or a charge for damage to the former tenant, pests, hazards
   such as mold, fuel or an unknown liquid, anything legal, any words that ask a manager to decide, a repair that said it fixed a part
   an inspection then finds failing, and a bare note too vague to act on. Cosmetic wear and an unpaid balance are not handed over.
2. Opens a work order for a real problem a walk note describes (tied to that note, so the repair record that answers the note closes
   it) and a request to look where a note only doubts. A manager's ruling, who is booked to come and a sticker's scuffs are not faults.
   It opens none for what an inspection finds or a repair says is not finished: the unit stays held on the latest look instead.
3. Links open orders about one fault (the rules in core/walk_rules.py say "same", or the same part with symptoms that can be one
   trouble; different parts stay as two).
   A report nobody worked on, which a later look speaks to and finds fine, is closed as needing no work (the gate checks that too).
4. Asks for an inspection once all work is finished and nobody has looked at the unit since. With a handoff still open, it also asks the
   gate for a release at that point: the gate refuses (a look is still open) but judges whether staff's records settled the question
   while a manager's ruling is still among the newest records it reads.
5. Holds the unit at the move-out, when nothing is known about it, and again whenever anything is open. It releases it only when every
   order is finished, the newest look (a walk note, a staff inspection, or a repair record that goes back over the whole room, written
   after the move-out or the last finished work) says the unit is fine, and nothing handed to staff came after that look. A note that
   defers to a walk-through or only repeats what someone said is not a look. A block staff placed is never lifted.

Everything it needs to remember lives in the case record (its `links` field), so a restart between calls loses nothing: the rest is read
again from the records each time."""
from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace

from core import renting, walk
from core import walk_rules as wr
from core.store import Store
from core.tilden import cases, gate, tools
from core.work import NOTE_MAX

AGENT = "agent:tilden"
# events that can change what the worker should do; its own entries and the gate's decisions never wake it
WATCH = {"walk.observed", "work_order.reported", "work_order.reported_again", "work_order.triaged", "work_order.assigned",
         "work_order.recorded", "work_order.checked", "work_order.closed", "work_order.joined", "work_order.kept_apart",
         "unit.blocked", "unit.unblocked", "lease.signed"}
FAILED_INSPECTIONS = 3          # after this many inspections that found the fault again, a person looks

# ---- words that only a person can decide on ------------------------------------------------------------------------------------
BELONGINGS = "Belongings left behind"
LOCKED = "Lock left on the door"
DISPUTE = "Dispute or charge to the former tenant"
PESTS = "Pests"
SAFETY = "Safety"
LEGAL = "Legal matter"
UNCLEAR = "Words too unclear to act on"
FAILING = "Repair keeps failing"
FINDING = "A walk finding still stands"
ESCALATE = "Needs a manager's decision"

# the nouns of walk_rules.BELONG, without its outer \\b(?: ... )\\b
_NOUNS = wr.BELONG.pattern[5:-3].replace("padlocks?|", "").replace("left behind|", "")
_BELONG_NOUN = re.compile(r"\b(?:" + _NOUNS + r")\b")
_BELONG_SAY = re.compile(
    r"\bbelongings?\b|\bpersonal (?:items?|property|effects)\b|\b(?:his|her|their|tenant'?s|former tenant'?s) (?:stuff|things|items|boxes|furniture|property|junk)\b|"
    r"\b(?:stuff|things|items|boxes|furniture|junk|property) (?:left|still|inside|in (?:the|there|it))\b|"
    r"\bdump(?:ed|ing)\b|"
    r"\babandon\w*|\bpick(?:ing)? up (?:his|her|their|the rest|the remaining|more|everything|the stuff|the boxes|the rest of)\b|"
    r"\bcome(?:s|ing)? (?:back |by |in )?(?:to|and) (?:get|grab|collect|pick|retrieve)\b|\bretriev\w*|\bremaining (?:items?|boxes|stuff|things)\b|"
    r"\bnot (?:been |fully |completely )?(?:cleared|emptied|moved) out\b|\bcontents\b|\bhasn'?t (?:cleared|emptied|moved)\b")
_BELONG_LEFT = re.compile(r"\bleft (?:\w+ ){0,3}(?:" + _NOUNS + r")\b")
_PRESENCE = re.compile(r"\b(?:for|get|grab|collect|pick|send|retrieve|coming|come|want|wants|says (?:he|she|they)|next week|tomorrow|left|leaving|still|inside|in (?:the )?(?:unit|there|corner|back|front|middle)|remain\w*|sitting|full of|stacked|piled|"
                       r"there (?:are|is|were|was)|some|several|a few|lot of|lots of)\b")
_TRASHY = re.compile(r"\b(?:trash|garbage|plastic|bin|empty|flattened|broken down|cardboard) (?:bags?|boxes|box)\b|\btrash\b|\bgarbage\b|\bdebris\b|\bwrappers?\b")
_LOCK_ON = re.compile(r"\b(?:padlock|lock)s?\b[^,]{0,25}\b(?:still|left)\b|\bstill (?:has|have|had|got) (?:a |the |his |her |their )?(?:padlock|lock)\b|"
                      r"\b(?:tenant'?s|former tenant'?s|their|his|her|old|someone'?s) (?:padlock|lock)\b[^,]{0,25}\bon\b|\bleft (?:a |the |his |her |their )?(?:padlock|lock)\b")
_EMPTIED = re.compile(r"\b(?:cleared out|emptied|picked up|took (?:everything|all|the rest|his|her|their|it all)|came (?:and|to) (?:get|got|pick|took|collect)|"
                      r"collected|removed (?:everything|all|the rest|it all)|hauled|all gone|nothing (?:left|remains)|no (?:belongings|stuff|items|boxes|furniture)|"
                      r"(?:unit|it)(?:'s| is| was) (?:now )?empty)\b")
_FUTURE = re.compile(r"\b(?:will|going to|gonna|coming|plans? to|wants? to|'ll|tomorrow|friday|saturday|sunday|monday|tuesday|wednesday|thursday|next|later|soon|not|n't)\b")
_DISPUTE = re.compile(
    r"\bdisput\w*|\bdisagree\w*|\bargu\w*|\bcomplain\w*|\bupset\b|\bangry\b|\bthreat\w*|\bdeposit\b|\brefund\w*|"
    r"\bwho(?:'s| is) (?:responsible|paying)\b|\bresponsible for\b|\bcaused by (?:the |a )?(?:former |previous |last )?tenant|"
    r"\b(?:tenant|he|she|they) (?:broke|damaged|caused|punched|kicked|ripped|tore|wrecked|trashed|destroyed)\b|\bnot (?:his|her|their|my|our) fault\b|"
    r"\b(?:wasn'?t|was not|weren'?t) (?:him|her|them|me|us|my|our|his|their)\b|\balready (?:broken|damaged|cracked|bent|dented|like (?:that|this)|there)\b|"
    r"\bbeyond (?:normal )?(?:wear|tear)|\bwear and tear\b|\bblam\w+|\bdenies\b|\bwants? (?:his|a|the) (?:money|deposit|refund)\b")
# a charge is a matter for staff when it is for damage; a plain unpaid balance on a closed account is not about the unit
_CHARGE = re.compile(r"\bcharg(?:e|ed|es|ing)\b|\bdeduct\w*|\bwrite[- ]?off\b|\bbill(?:ed|ing)? (?:him|her|them|the (?:former )?tenant)\b|\bfees?\b")
_DAMAGEISH = re.compile(r"\bdamag\w*|\bbroke\w*|\bbroken\b|\bdent\w*|\bhole\b|\brepair\w*|\bpanel\b|\bcleaning\b|\bcrack\w*|\bwreck\w*|\btorn\b|\bripped\b")
_PEST = re.compile(r"\b(?:roach\w*|cockroach\w*|mice|mouse|rats?|rodents?|droppings?|gnaw\w*|termites?|ants?|bed ?bugs?|(?<!dead )bugs?|wasps?|hornets?|"
                   r"bees?|fleas?|infest\w*|pests?|nests?|snakes?|scorpions?|maggots?|larvae|vermin|critters?|insects?|raccoons?|squirrels?|pigeons?)\b")
_SAFETY = re.compile(
    r"\bmold\w*|\bmildew\w*|\bgas (?:smell|leak|odor)|\bsmell(?:s|ed)? (?:of |like )?(?:gas|smoke|burn\w*|chemical\w*|sewage|fumes?)|\bsmoke\b|"
    r"\bburn(?:t|ing|ed) smell|\bchemicals?\b|\bhazmat\b|\bhazardous\b|\bbiohazard\b|\bsewage\b|\bfumes?\b|\bpropane\b|\bgasoline\b|"
    r"\bfuel\b|\bammo\b|\bammunition\b|\bfirearms?\b|\bguns?\b|\bweapons?\b|\bneedles?\b|\bsyringes?\b|\bblood\b|\bdrugs?\b|"
    r"\bflammable\b|\bexplosive\w*|\bbattery acid\b|\bbroken glass\b|\bsinkhole\b|\basbestos\b|\bunknown (?:liquid|substance|fluid|powder|residue)\b|"
    r"\boily[- ]looking\b|\bgas\b")
_LEGAL = re.compile(
    r"\blien\w*|\bauction\w*|\blegal\w*|\blaw(?:yer|suit|s)?\b|\battorney\w*|\bcourt\b|\bpolice\b|\bsheriff\b|\bcops?\b|\bevict\w*|\bbankrupt\w*|\bdeceased\b|"
    r"\bdied\b|\bpassed away\b|\bdeath\b|\bestate\b|\bprobate\b|\bsubpoena\w*|\binsurance (?:claim|company|adjuster)\b|\bstolen\b|\bstole\b|\btheft\b|"
    r"\bburglar\w*|\bbreak[- ]?in\b|\bbroke in\b|\bbroken into\b|\btrespass\w*|\bcriminal\b|\bfraud\b|\bsue\b|\bsuing\b|\bsued\b|\bdeployed\b|\bmilitary\b|"
    r"\bscra\b|\bcustody\b|\bseiz\w+|\bevidence\b")
_UNCLEAR = re.compile(
    r"\bsomething(?:'s| is)? (?:off|wrong|not right|up|weird|odd|strange|funny|going on)\b|\b(?:weird|odd|strange)\b|\bnot sure\b|\bunsure\b|\bdon'?t know\b|\bno clue\b|\bno idea\b|"
    r"\bidk\b|\bunclear\b|\bhard to (?:tell|say)\b|\bcan'?t (?:tell|say|figure)\b|\bwho knows\b|\bnot right\b|\bdoesn'?t look right\b|\bwrong with (?:it|the unit|this)\b|"
    r"\bsomething about\b|\bhmm+\b|\bwhatever\b|\bsame as before\b|\bsee (?:above|previous|other|last)\b|\bnote to self\b|\?{2,}|"
    r"\b(?:smells?|odou?r|stinks?|reeks?)\b[^,]*\b(?:funny|weird|off|bad|odd|strange)\b|\b(?:funny|weird|odd|strange|bad) (?:smell|odou?r)\b")
_MONEY = re.compile(r"\$|\bdollars?\b|\bcharg(?:e|ed|es|ing)\b|\bfees?\b|\bbill(?:ed|ing)?\b|\bdeposit\b|\brefund\w*|\bowes?\b|\bowed\b|\binvoice\w*|\bdeduct\w*")
_NEG_BEFORE = re.compile(r"(?:\bno\b|\bnot\b|n't\b|\bnothing\b|\bnone\b|\bnever\b|\bwithout\b|\bfree of\b|\bclear of\b)[^,;.]{0,25}$")
TOPICS = ((SAFETY, _SAFETY), (PESTS, _PEST), (LEGAL, _LEGAL), (DISPUTE, _DISPUTE))
# words that ask a person to decide: only staff with authority can settle these, whatever the topic
_ASKS = re.compile(
    r"\b(?:need|needs|needed|requires?|waiting (?:on|for)|asked? for)\b[^.;]{0,50}\b(?:manager|management|supervisor|owner|director)\b|"
    r"\b(?:manager|management|supervisor|owner|director)\b[^.;]{0,25}\b(?:needs?|must|has to|to) (?:decide|choose|determine|authori[sz]e|approve|sort|review|rule)\b|"
    r"\bneeds? (?:a |an )?(?:decision|approval|authori[sz]ation|ruling)\b|\b(?:don'?t|do not|doesn'?t|not) (?:have|had|got|granted|given) (?:the )?(?:permission|authori[sz]ation|approval)\b")
# wear that only looks bad: a scuffed sticker or a loose tag is neither work nor a reason to hold the unit
_COSMETIC = re.compile(r"\bcosmetic\w*|\bsticker\w*|\bdecals?\b|\b(?:number|name|unit) (?:tag|plate|label)s?\b|\bunsightly\b|\btouch-?ups?\b|\bsignage\b|\bplastic tag\b")
# who is coming and when: a plan for work, not a fault
_PLAN = re.compile(r"\bbooked\b|\bscheduled?\b|\brescheduled\b|\barrang\w+|\bon order\b|\bordered\b|\bappointment\b|"
                   r"\bwill (?:come|be|arrive|attend|move|replace|fix|repair|treat|send)\b|\bcoming (?:out|by|on|tomorrow|friday|saturday|monday|tuesday|wednesday|thursday)\b|"
                   r"\bexpected (?:to|on)\b|\bhas ?n'?t (?:attended|come|arrived|visited|completed)\b|\bnot (?:yet )?(?:attended|arrived)\b")
_STILL_BAD = re.compile(r"\bstill (?:leak|drip|stick|jam|broken|damaged|wet|dirty|not|there|here|open|loose|bent)\w*|\bagain\b|\bnot fixed\b")
_NOT_READY_RX = re.compile(
    r"\b(?:not|isn'?t|aren'?t|can'?t|cannot|couldn'?t|haven'?t|hasn'?t|won'?t|never) (?:\w+ ){0,2}?(?:ready|clear|cleared|rentable|available|released?|offered)\b|"
    r"\bstays? (?:unavailable|blocked|closed|held|off|on hold)\b|"
    r"\b(?:remains?|remaining|still|keep(?:ing)?|leav(?:e|ing)|kept) (?:\w+ ){0,3}(?:unavailable|blocked|closed|held|on hold|out of service|off (?:the )?(?:market|list|available list))\b|"
    r"\bon hold\b|\bhold (?:remains|stays|in effect)\b|\bunavailable\b|\bnot (?:safe|usable|rentable)\b|\bout of service\b|\bno (?:new )?rental\b|"
    r"\bkeep \w+ (?:closed|blocked|held)\b")
_NO_HOLD = re.compile(r"\bno (?:\w+ )?hold\b|\bnot? (?:\w+ )?hold (?:needed|required)\b|\bnothing (?:is )?(?:on )?hold\b")
_NOT_DONE = re.compile(r"\b(?:didn'?t|did not|haven'?t|have not|couldn'?t|could not|unable|not (?:yet |fully |completely )?(?:complete\w*|finished|done|fixed)|"
                       r"isn'?t (?:finished|done|complete\w*)|incomplete|temporar\w+|only (?:partly|a part|partially))\b")
_RECHECK = re.compile(r"\b(?:re-?checked|re-?inspected|re-?walked|walked (?:through )?the (?:whole |entire |full )?(?:room|unit|space)|"
                      r"went (?:through|over) the (?:whole |entire |full )?(?:room|unit|space)|checked the (?:whole |entire |full |rest of the )?(?:room|unit|space))\b")
_EXTRA_PART = re.compile(r"\b(?:roof|ceiling|pipes?|joints?|insulation|slab|concrete|hvac|pumps?|vents?|climate|cooling|window|stairs?|ramp)\b")
_LIST_NEG = re.compile(r"\s*(?:no|without|nothing|none)\b", re.I)
_VERBISH = re.compile(r"\b(?:is|are|was|were|has|have|had|looks?|works?|seems?|needs?|runs?|opens?|closes?|locks?)\b", re.I)
_DEFER = re.compile(r"\b(?:i'?ll|will|going to|gonna|to)\s+(?:flag|check|look|inspect|walk|have (?:\w+ )?(?:check|look))\b|\bwalk-?through\b|"
                    r"\b(?:asked|ask|asking|told|let|tell) \w+ (?:to|know)\b[^.]*\b(?:check|walk|look|inspect|recheck)\w*|"
                    r"\bneeds? (?:a |another |the )?(?:final |proper |full )?(?:check|look|inspection|walk-?through)\b|"
                    r"\b(?:until|before|after) (?:the |a )?(?:inspection|walk-?through|check)\b|\bto be checked\b")
_DECIDED = re.compile(r"\b(?:manager|management|supervisor|owner)\b[^.]{0,40}\b(?:decided|approved|authori[sz]ed|directed|instructed|granted|ruled)\b|"
                      r"\b(?:authori[sz]ed|approved|decided to|directed|instructed)\b")
# what someone else said happened is not a look at the unit
_HEARSAY = re.compile(r"\b(?:says?|said|told (?:me|us)|tells? (?:me|us)|claims?|according to|reports? that|called|phoned|emailed|texted|voicemail|message from)\b")
_SAW = re.compile(r"\b(?:i|we) (?:checked|walked|looked|saw|found|inspected|went|opened|verified|confirmed)\b|\b(?:checked|walked|inspected|verified|confirmed) (?:the|it|that|all)\b")
_FAMILY = {"DAMAGE": "mech", "STUCK": "mech", "FLICKER": "elec", "POWER": "elec", "WATER": "water", "NOISE": "noise"}


def _topics_of(low: str) -> list[str]:
    hit = [label for label, rx in TOPICS if _hit(rx, low)]
    if DISPUTE not in hit and _hit(_CHARGE, low) and _DAMAGEISH.search(low):
        hit.append(DISPUTE)
    return hit

# ---- work words: the strongest rules in the code base, with the gaps found in testing filled ---------------------------------
_REPAIR_EXTRA = (r"\bhanging by\b|\blights? (?:is|are) (?:out|dead)\b|\b(?:isn'?t|aren'?t|stopped|quit|stops?)\s+(?:\w+\s+){0,2}(?:working|closing|opening|latching|locking|sealing|rolling|sliding)\b|"
                 r"\b(?:off|out of) (?:the )?(?:track|tracks|rail|rails|hinges?)\b|\bhangs? (?:off|out)\b|\bholes?\b|\bpeeling\b|\brust(?:ed|y|ing)?\b|\bwarp\w*|\bbuckl\w*|\bbulg\w*|\bchipp\w*|\bgouge\w*|\bneeds? (?:a )?(?:fresh )?(?:paint|patch|drywall|caulk\w*|sealing)\b|"
                 r"\brepaint\w*|\bpatch(?:ing)?\b|\bnot (?:working|closing|opening|latching|locking|sealing|seating|staying|holding)\b|\bmisalign\w*|\bspins?\b|\bspinning\b|"
                 r"\bslips?\b|\bslipping\b|\bfalls? (?:off|out|down)\b|\bfalling off\b|\bpops? (?:out|off)\b|\bwon'?t stay\b|\bkeeps? (?:coming|falling|popping|slipping|jumping|sticking)\b|"
                 r"\bdoesn'?t (?:seat|stay|hold|seal)\b|\bdrags?\b|\bsqueak\w*|\bgap\b|\bsunk\b|\bsag\w*|\bswollen\b|\bwater (?:stain|spot|mark)s?\b|\bstains? on (?:the )?ceiling\b|"
                 r"\bsoft spot\b|\bnot (?:turning|rolling|sliding)\b|\bwon'?t (?:roll|slide|turn|lock|latch|close|open|shut|seat)\b")
REPAIR = re.compile(f"(?:{wr.REPAIR_CUE.pattern})|(?:{_REPAIR_EXTRA})")
CLEAN = wr.CLEAN_CUE
_LOOK = re.compile(r"\b(?:smells?|odou?r|stink\w*|reek\w*|noise|noises|sound)\b")
# a repair or cleaning that is already done, so the cue beside it is not an open fault
_FIXED = re.compile(r"\b(?:replaced|repaired|fixed|adjusted|lubed|lubricated|tightened|re-?hung|reattached|re-?aligned|aligned|patched|welded|cleared|resolved|"
                    r"installed|swapped|rebuilt|sealed|reset|serviced|cleaned|swept|mopped|painted|straightened|rolled|pressure washed|vacuumed|dusted)\b|"
                    r"\bno longer\b|\bnot any ?more\b|\bany ?more\b|\bnow (?:rolls|works|opens|closes|latches|locks|runs|slides|catches|seals|is fine|are fine)\b")
_WAS = re.compile(r"\b(?:was|were|had been|used to|before|previously|earlier|originally)\b")
_STILL = re.compile(
    r"\bstill\b|\bagain\b|\bpersist\w*|\bcame back\b|\bcomes back\b|\bnot (?:fixed|repaired|resolved|working|done|finished|complete\w*|solved|cured)\b|"
    r"\b(?:isn'?t|wasn'?t|aren'?t|weren'?t|hasn'?t|didn'?t|doesn'?t|couldn'?t|can'?t|cannot|won'?t) (?:\w+ ){0,2}(?:fix\w*|repair\w*|resolv\w*|solv\w*|work\w*|finish\w*|"
    r"complet\w*|help\w*|cure\w*)\b|\bunable to\b|\bcould ?n'?t\b|\bfail\w*|\bworse\b|\bincomplete\b|\bpartial\w*|\bhalf[- ]?(?:done|finished|way|complete\w*|fixed)\b|\bpart (?:is )?(?:on order|ordered|back ?ordered|coming|arrives)\b|"
    r"\b(?:fixed|repaired|replaced|patched|adjusted|tightened|cleared|sealed|cleaned|reattached|aligned|serviced|welded|installed) (?:only )?"
    r"(?:half|part|some|one|two|a few|a couple|a third|the first|the top|the bottom) of\b|\bonly (?:one|two|a few|some|half|part|the first) of\b|"
    r"\bpartly\b|\bpartially\b|\bhalf of (?:the|it|that)\b|"
    r"\bwaiting (?:on|for)\b|\bback ?order\w*|\bneeds? (?:another|a second|more|further|a new|new|replac\w*|a part|parts|to come back)\b|\bcome back\b|"
    r"\breturn(?:ing)? (?:tomorrow|next|monday|tuesday|wednesday|thursday|friday|saturday|later)\b|\bwill (?:finish|return|come back)\b|\bto be (?:finished|completed|continued)\b|"
    r"\bnot (?:safe|right)\b|\bleak(?:s|ing)\b(?! (?:fixed|stopped|gone))")
_STILL_FINE = re.compile(r"\bstill (?:looks?|is|are|works?|runs?|rolls?|closes|opens|locks?|catches|latches|seals|slides)? ?(?:fine|good|great|smooth\w*|solid|sound|intact|ok|okay|working|"
                         r"functional|clean|tight)\b")
_GOOD = re.compile(
    r"\b(?:fine|good|great|okay|ok|perfect|excellent|solid|sound|smooth\w*|clean|spotless|passes|passed|pass|works|working|ready|rentable|all clear|checked out|dry|secure|tidy|"
    r"clear(?:ed)? (?:to|for) rent\w*|(?:good|safe|fit) (?:to|for) rent\w*|no other (?:issues?|problems?)|"
    r"no (?:issues?|problems?|damage|leaks?|concerns?|defects?|faults?|repairs?)|nothing (?:wrong|else|to fix|to report|further|visible|remaining)|"
    r"(?:rolls?|closes?|opens?|locks?|latches|catches|slides|seals?) (?:and \w+ )?(?:fine|well|smooth\w*|freely|tight|properly|correctly|normally)|looks? (?:good|fine|great|normal|right|ok|okay))\b")
_CANT_SEE = re.compile(r"\b(?:can'?t|couldn'?t|cannot|unable to) (?:see|tell|find|hear|reach|get to|check|get in(?:side)?|enter|access)\b")
_SEES_NOTHING = re.compile(r"\b(?:can'?t|couldn'?t|cannot|didn'?t|did not|don'?t|do not) (?:find|see|notice|spot|detect|hear|smell) (?:any|a|an)\b")
DOOR = re.compile(r"\b(?:door|latch|hinge|track|spring|roll-?up)\b", re.I)     # the words the walk finding looks for in an order
_UNIT = wr._UNIT
_SENT = re.compile(wr._SENTENCE.pattern)
_CLAUSE = re.compile(wr._CLAUSE.pattern, re.I)


def _low(text: str) -> str:
    return wr._clean(text)


def _units(text: str) -> set[str]:
    return {f"{letter.upper()}{int(number)}" for letter, number in _UNIT.findall(text)}


def _negated(low: str, start: int) -> bool:
    return bool(_NEG_BEFORE.search(low[max(0, start - 30):start]))


def _hit(rx: re.Pattern, low: str) -> re.Match | None:
    """A match of rx that no negation ("no mice", "nothing left behind") sits in front of."""
    for m in rx.finditer(low):
        if not _negated(low, m.start()):
            return m
    return None


def _pieces(text: str, unit: str, numbered: bool = False) -> list:
    """(original clause, cleaned clause) pairs of the text, leaving out a sentence that is about another unit; with `numbered`, each
    pair carries its sentence number first."""
    mine, out = wr._unit_key(unit), []
    for number, sentence in enumerate(_SENT.split(str(text or ""))):
        if not sentence.strip():
            continue
        named = _units(sentence)
        if named and mine not in named:
            continue
        clauses: list[str] = []
        for clause in _CLAUSE.split(sentence):
            if not clause.strip():
                continue
            short = len(clause.split()) <= 5 and not _VERBISH.search(clause)
            if clauses and short and _LIST_NEG.match(clauses[-1]):
                clauses[-1] += ", " + clause.strip()          # the "no" in front covers the whole list
            else:
                clauses.append(clause)
        for clause in clauses:
            pair = (" ".join(clause.split()), _low(clause))
            out.append((number, *pair) if numbered else pair)
    return out


def fixed_reader(note: str, route, unit: str):
    """The gate asks a reader whether a note says belongings are inside; this one answers from the word rules, with no model call."""
    contents = "seen" if BELONGINGS in read_words(note, unit).topics else "unknown"
    return SimpleNamespace(refusal=None, flags=None, call=None, fields={"contents": contents})


READERS = {"read_note": fixed_reader}


class Reading:
    def __init__(self):
        self.topics: dict[str, str] = {}        # label -> the staff member's words that raised it
        self.work: list[tuple[str, str]] = []   # (repair | cleaning | look, words), grouped by part
        self.cleared: bool = False              # words saying what was left behind is gone


def _group(items: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """Clauses about one part are one fault: ("repair", clause original, clause cleaned) become one order each. A clause with no part named
    carries on the one before it."""
    groups: list[dict] = []
    for kind, orig, low in items:
        leaves, generic = wr._parts(low)
        names = leaves | generic
        home = next((g for g in groups if g["kind"] == kind and (g["names"] & names)), None)
        if home is None and not names:
            home = next((g for g in reversed(groups) if g["kind"] == kind), None)
        if home is None:
            groups.append({"kind": kind, "names": set(names), "text": [orig]})
        else:
            home["names"] |= names
            home["text"].append(orig)
    return [(g["kind"], "; ".join(g["text"])) for g in groups]


def read_words(text: str, unit: str) -> Reading:
    """What a staff member's words about one unit say: what only a person can decide, and the work a clause confirms or hedges."""
    r = Reading()
    found: list[tuple[int, str, str, str]] = []
    vague: dict[int, str] = {}                  # sentence number -> its unclear words
    decided = {n for n, sentence in enumerate(_SENT.split(str(text or ""))) if _DECIDED.search(_low(sentence))}
    for number, orig, low in _pieces(text, unit, numbered=True):
        cleared = bool(_EMPTIED.search(low)) and not _FUTURE.search(low)
        if cleared:
            r.cleared = True
        hit = _topics_of(low)
        for label in hit:
            r.topics.setdefault(label, orig)
        if _hit(_ASKS, low):
            r.topics.setdefault(ESCALATE, orig)
        if _COSMETIC.search(low):
            continue                              # a scuffed sticker or a loose tag is not work and not a hold
        noun = _BELONG_NOUN.search(low)
        hedged = bool(wr.HEDGE.search(low))
        if not cleared and not hedged and (_hit(_BELONG_SAY, low) or (_hit(_BELONG_LEFT, low) and not _TRASHY.search(low)) or (noun and _PRESENCE.search(low) and not _TRASHY.search(low) and not _negated(low, noun.start()))):
            r.topics.setdefault(BELONGINGS, orig)
            continue
        if PESTS in hit or (DISPUTE in hit and _MONEY.search(low)):
            continue                              # pest control and money are for a person; neither is a work order
        if (_PLAN.search(low) and not _STILL_BAD.search(low)) or number in decided:
            continue                              # who is coming and when, or what a manager ruled, is not a new fault
        confirmed_repair = wr._confirmed(low, REPAIR, (wr.NEG, wr.DONE_REPAIR, wr.NOT_FAULT))
        confirmed_clean = wr._confirmed(low, CLEAN, (wr.NEG, wr.DONE_CLEAN))
        if (wr.PRIOR.search(low) and not _STILL.search(low)) or _CANT_SEE.search(low):
            confirmed_repair = False
        if wr.DIRECTIVE.search(low) and not confirmed_repair:
            continue
        if _NOT_READY_RX.search(low) and not _NO_HOLD.search(low):
            continue                              # a hold statement: the unit waits, but it is not a fault to order
        # a clause with a fault word in it is work (or a look, when hedged); safety and legal words keep only a confirmed repair
        careful = SAFETY in hit or LEGAL in hit
        if confirmed_repair or (confirmed_clean and not careful):
            if wr.HEDGE.search(low):
                if not careful:
                    found.append((number, "look", orig, low))
            else:
                found.append((number, "repair" if confirmed_repair else "cleaning", orig, low))
            continue
        if _hit(_LOOK, low) and not wr.NEG.search(low) and not careful:
            found.append((number, "look", orig, low))
            continue
        if _hit(_UNCLEAR, low) or (wr.HEDGE.search(low) and re.search(r"\b(?:problem|issue|trouble|wrong|bad|concern|off)\b", low) and not wr.NEG.search(low)):
            vague.setdefault(number, orig)
    # words that name no fault and no decision anywhere in the entry are too unclear to act on; a clear fault beside a doubt is not
    if vague and not found and not r.topics:
        r.topics[UNCLEAR] = next(iter(vague.values()))
    # a hedged clause beside a confirmed one in the same sentence is the same worry: one order, not two
    firm = {n for n, kind, _, _ in found if kind != "look"}
    r.work = _group([(kind, orig, low) for n, kind, orig, low in found if kind != "look" or n not in firm])
    return r


def read_finish(texts: list[str], unit: str) -> tuple[str, list[tuple[str, str]], Reading]:
    """The words that finish a work order or an inspection (or a note), oldest first. Returns ("problem" | "fine" | "unclear", what is
    still wrong, the words read for belongings and the like). A fault stands unless a later clause says that part is fine; a statement
    that the unit stays unavailable makes it a problem with nothing new to order."""
    faults: list[tuple[int, str, set, bool]] = []
    goods: list[tuple[int, str, set]] = []
    sensitive = Reading()
    hedged_good = False
    for i, text in enumerate(texts):
        part = read_words(text, unit)
        for label, words in part.topics.items():
            sensitive.topics.setdefault(label, words)
        sensitive.cleared = sensitive.cleared or part.cleared
        for orig, low in _pieces(text, unit):
            leaves, _ = wr._parts(low)
            if _COSMETIC.search(low):
                goods.append((i, orig, leaves))      # cosmetic wear: the unit is usable as it is
                continue
            if _NOT_READY_RX.search(low) and not _NO_HOLD.search(low):
                faults.append((i, orig, leaves, True))
                continue
            if _PLAN.search(low) and not _STILL_BAD.search(low):
                continue
            if read_words(orig, unit).topics:
                continue                       # a topic for staff, not a repair
            hedge = bool(wr.HEDGE.search(low))
            if _SEES_NOTHING.search(low) or wr.NOT_FAULT.search(low):
                if hedge:
                    hedged_good = True
                else:
                    goods.append((i, orig, leaves))
                continue
            still = _STILL.search(low) and not _STILL_FINE.search(low) and not re.search(r"\bany ?more\b|\bno longer\b", low)
            fixed = bool(_FIXED.search(low))
            cue_repair = wr._confirmed(low, REPAIR, (wr.NEG, wr.DONE_REPAIR, wr.NOT_FAULT))
            cue_clean = wr._confirmed(low, CLEAN, (wr.NEG, wr.DONE_CLEAN))
            is_fault = bool(still) or ((cue_repair or cue_clean) and not fixed and not _WAS.search(low))
            if is_fault and not (hedge and not still):
                faults.append((i, orig, leaves, False))
            elif _GOOD.search(low) and not _negated(low, _GOOD.search(low).start()):
                if hedge:
                    hedged_good = True
                else:
                    goods.append((i, orig, leaves))
            elif fixed and not hedge:
                goods.append((i, orig, leaves))      # that part was repaired: it cancels an earlier fault on it
            elif is_fault or (fixed and hedge):
                hedged_good = True
    standing = [f for f in faults if not any(g[0] > f[0] and (not f[2] or (g[2] & f[2]) or not g[2] and not f[2]) for g in goods)]
    if standing:
        grouped = _group([("repair", orig, _low(orig)) for _, orig, _, hold in standing if not hold])
        return "problem", grouped, sensitive
    if goods:
        return "fine", [], sensitive
    return "unclear", [], sensitive


# ---- reading the records -------------------------------------------------------------------------------------------------------
class Snap:
    """The records about one case, read once."""

    def __init__(self, store: Store, stream: str, case: cases.Case, now: datetime):
        self.store, self.stream, self.case, self.now = store, stream, case, now
        self.events = store.stream_events(stream)
        self.at = {e.id: e.at for e in self.events}
        self.books, self.rent = renting.load(store, stream)
        _, self.wk = walk.load(store, stream)
        self.unit = case.unit
        self.reported: dict[str, int] = {}
        self.closed: dict[str, int] = {}
        self.no_work: set[str] = set()          # orders staff's later record showed needed no work: closed, but nothing was repaired
        for e in self.events:
            if e.kind == "work_order.reported":
                self.reported[e.payload["order"]] = e.id
            elif e.kind == "work_order.closed":
                self.closed[e.payload["order"]] = e.id
                if e.payload.get("no_work"):
                    self.no_work.add(e.payload["order"])
        self.latest = max((e.id for e in self.events if e.source != AGENT and e.kind in WATCH), default=0)
        self.blocks: dict[str, str] = {}        # block id -> who placed it
        self.lifted_by_staff = False
        for e in self.events:
            if e.kind == "unit.blocked" and e.payload["unit"] == self.unit and e.id > case.source_event:
                self.blocks[e.payload["block"]] = e.source
            if e.kind == "unit.blocked" and e.payload["unit"] == self.unit:
                self.blocks.setdefault(e.payload["block"], e.source)
        for e in self.events:
            if (e.kind == "unit.unblocked" and e.id > case.source_event and e.source != AGENT
                    and self.blocks.get(e.payload["block"]) == AGENT):
                self.lifted_by_staff = True

    def orders(self) -> list[walk.WorkOrder]:
        """The orders on the unit that belong to this case: reported since the move-out, or still open then."""
        src = self.case.source_event
        out = []
        for o in self.wk.orders.values():
            if o.unit != self.unit or o.joined_into:
                continue
            if self.reported.get(o.id, 0) > src or o.state != "closed" or self.closed.get(o.id, 0) > src:
                out.append(o)
        return sorted(out, key=lambda o: (o.reported, o.id))

    def open_orders(self) -> list[walk.WorkOrder]:
        return [o for o in self.wk.orders.values() if o.unit == self.unit and o.state != "closed"]

    def holder(self):
        return self.rent.holder(self.unit, self.now)

    def our_block(self) -> bool:
        h = self.holder()
        return bool(h and h[0] == "block" and self.blocks.get(h[1]) == AGENT)

    def staff_block(self) -> bool:
        h = self.holder()
        return bool(h and h[0] == "block" and self.blocks.get(h[1]) != AGENT)

    def texts(self, order: walk.WorkOrder) -> list[str]:
        return [c.text for c in order.changes[1:] if c.text and c.by != AGENT]

    def vacant(self) -> bool:
        return self.rent.status(self.unit, self.now) == "vacant"


def klass(order: walk.WorkOrder) -> str:
    """inspection | cleaning | look | repair, from the order's own words. An order whose words name no fault counts as repair: it needs a look
    after the work."""
    if order.kind == "check":
        return "inspection"
    words = " ".join([order.problem] + [r.text for r in order.reports])
    if order.problem.startswith("Cleaning needed:"):
        return "cleaning"
    if order.problem.startswith("Check:"):
        return "look"
    for _, low in _pieces(words, order.unit):
        if wr._confirmed(low, REPAIR, (wr.NEG, wr.DONE_REPAIR)):
            return "repair"
    for _, low in _pieces(words, order.unit):
        if wr._confirmed(low, CLEAN, (wr.NEG, wr.DONE_CLEAN)):
            return "cleaning"
    return "repair"


# the place a cleaning job is about; two cleaning orders are one job only when they name the same places (none named on both: the whole unit)
_AREAS = (("FLOOR", r"floors?|flooring|concrete|carpet|sweep\w*|swept|mop\w*"), ("WALL", r"walls?|panels?|baseboards?"),
          ("CEILING", r"ceilings?"), ("COBWEB", r"cobwebs?|webs?"), ("CORNER", r"corners?"), ("DOOR", r"doors?|tracks?|roll-?up|threshold"),
          ("SHELF", r"shel(?:f|ves)|racks?"), ("LIGHT", r"lights?|fixtures?|lens"), ("DRAIN", r"drains?"), ("WINDOW", r"windows?|vents?"))


def clean_scope(text: str) -> frozenset:
    low = _low(text)
    return frozenset(name for name, pat in _AREAS if re.search(rf"\b(?:{pat})\b", low))


def order_scope(order: walk.WorkOrder) -> frozenset:
    return clean_scope(" ".join([order.problem] + [r.text for r in order.reports]))


def _fits(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= NOTE_MAX else text[:NOTE_MAX - 1].rstrip() + "…"


def _reports(order: walk.WorkOrder, unit: str) -> list[dict]:
    return [{"text": r.text, "by": r.by, "at": r.at.isoformat(), "unit": unit} for r in order.reports]


def verdict(a: walk.WorkOrder, b: walk.WorkOrder) -> str:
    """Whether two orders' reports are one fault: "same" only when some pair says so and none says "different"."""
    seen = {wr.same_fault(x, y) for x in _reports(a, a.unit) for y in _reports(b, b.unit)}
    if wr.DIFFERENT in seen:
        return wr.DIFFERENT
    return wr.SAME if wr.SAME in seen else wr.UNSURE


def covered(snap: Snap, kind: str, text: str) -> bool:
    """Whether an open order on the unit already says this."""
    probe = walk.WorkOrder("probe", snap.unit, text, "staff:front_desk", snap.now, reports=[walk.Report("staff:front_desk", snap.now, text)])
    for o in snap.open_orders():
        k = klass(o)
        if k == "inspection":
            continue
        if kind == "cleaning" and k == "cleaning":
            mine, theirs = clean_scope(text), order_scope(o)
            if mine == theirs or (mine and mine <= theirs):
                return True
        if kind == "repair" and k in ("repair", "look") and verdict(probe, o) == wr.SAME:
            return True
        if kind == "look" and k in ("repair", "look") and verdict(probe, o) == wr.SAME:
            return True
    # the walk finding for a damaged door is answered by any open order that names the door, whatever its wording
    seen = snap.rent.floor.standing(snap.unit)
    if kind == "repair" and DOOR.search(text) and seen and seen.door == "damaged":
        if any(DOOR.search(x.problem + " " + " ".join(r.text for r in x.reports)) for x in snap.open_orders() if klass(x) != "inspection"):
            return True
    return False


def latest_look(snap: Snap, floor: int):
    """The newest look at the unit written after `floor` (the move-out, or the last finished work): (record id, "fine" | "problem", when it was written, its words) or
    None when nobody has looked since. A look is a walk note or a staff inspection that says how the unit is, or a finished work order
    whose own words say the worker went back over the whole room and found it fine. Words that say nothing about the unit's
    condition ("a caller asked about rates") are not a look."""
    looks: list[tuple[int, str, datetime, str]] = []
    for e in snap.events:
        if e.kind == "walk.observed" and e.payload["unit"] == snap.unit and e.id > floor and e.source != AGENT:
            note = e.payload.get("note", "")
            state = read_finish([note], snap.unit)[0]
            low_note = _low(note)
            if state == "fine" and (_DEFER.search(low_note) or (_HEARSAY.search(low_note) and not _SAW.search(low_note))):
                state = "unclear"                    # "I'll flag it for the walk-through": the walk-through is the look; hearsay is not a look
            if state != "unclear":
                looks.append((e.id, state, snap.at[e.id], note))
    for o in snap.orders():
        cid = snap.closed.get(o.id, 0)
        if o.state != "closed" or o.joined_into or o.id in snap.no_work or not o.changes[-1].by.startswith("staff:"):
            continue
        if klass(o) == "inspection":
            if cid > floor:
                state = read_finish(snap.texts(o), snap.unit)[0]
                if state != "unclear":
                    looks.append((cid, state, snap.at[cid], " ".join(snap.texts(o))))
        elif cid >= floor and cid > snap.case.source_event:
            words = " ".join(snap.texts(o))
            if _RECHECK.search(_low(words)) and read_finish(snap.texts(o), snap.unit)[0] == "fine":
                looks.append((cid, "fine", snap.at[cid], words))
    return max(looks) if looks else None


def _part_set(text: str) -> set[str]:
    low = _low(text)
    leaves, generic = wr._parts(low)
    return leaves | generic | {m.lower() for m in _EXTRA_PART.findall(low)}


def _families(text: str) -> set[str]:
    return {_FAMILY[s] for s in wr._symptoms(_low(text)) if s in _FAMILY}


def _text_of(order: walk.WorkOrder) -> str:
    return " ".join([order.problem] + [r.text for r in order.reports])


_COMMON = frozenset("""this that with from have been were does some there their about after before unit room space floor walls wall looks look
looked needs need back near left right door doors like might still clean empty check checked fine good work works working ready rent
inside outside front rear side bottom other than just only what when where which while into over under also even more most very much many
them they then will would could should""".split())


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z]+", _low(text)) if len(w) > 3 and w not in _COMMON}


def _speaks_to(text: str, look_words: str) -> bool:
    """Whether a look's words speak to some words about a fault: they name a part in common, or a content word in common."""
    return bool(_part_set(text) & _part_set(look_words)) or bool(_stems(text) & _stems(look_words))


def addresses(order: walk.WorkOrder, look_words: str) -> bool:
    """Whether a look's words speak to what an order reports."""
    return _speaks_to(_text_of(order), look_words)


def relaxed_same(a: walk.WorkOrder, b: walk.WorkOrder) -> bool:
    """Two open orders on one unit about one part, with symptoms that can be the same trouble (broken and will not close): one fault."""
    ta, tb = _text_of(a), _text_of(b)
    pa, pb = _part_set(ta), _part_set(tb)
    if not pa or not pb or not (pa & pb):
        return False
    fa, fb = _families(ta), _families(tb)
    if fa and fb and not (fa & fb):
        return False
    la, lb = wr._locations(_low(ta)), wr._locations(_low(tb))
    if wr._locations_conflict(la, lb):
        return False
    return abs((a.reported - b.reported).total_seconds()) <= 3 * 86400


# ---- the worker ----------------------------------------------------------------------------------------------------------------
class Worker:
    def __init__(self, store: Store, stream: str, case: cases.Case, now: datetime, check=None):
        self.store, self.stream, self.now, self.check = store, stream, now, check
        self.case = case
        state = (case.links or [{}])[0] if case.links and isinstance(case.links[0], dict) else {}
        self.cursor = state.get("cursor", case.source_event)
        self.open_topics: dict[str, str] = dict(state.get("topics", {}))
        self.topic_at: dict[str, str] = dict(state.get("topic_at", {}))    # when each topic was raised: a look written later answers it, the same entry's words do not
        self.done_orders: list[str] = list(state.get("followed", []))
        self.finished: list[str] = []               # orders read this pass; marked followed only once their follow-up work is recorded
        self.started = bool(state.get("started"))
        self.refused_at: dict[str, int] = dict(state.get("refused_at", {}))   # action -> newest staff record when the gate refused it
        self.declined: list[str] = list(state.get("declined", []))           # handoffs the gate called routine
        # work orders the gate refused: kept with the watermark in refused_at, so a later staff record that makes the order possible
        # (or an end to its reason) finds the intent again. key -> {"kind", "text", "record"}
        self.intents: dict[str, dict] = {k: dict(v) for k, v in dict(state.get("intents", {})).items()}
        self.handed_now = False                                              # a handoff was made in this pass
        self.cur = case.source_event
        self.cur_at = now
        self.evidence: list[str] = []

    # ---- actions
    def act(self, name: str, **args):
        """One action through the shared gate: (its words, whether it was refused). The case it returns replaces the one held here."""
        content, case, error = gate.act(self.store, self.stream, self.case.id, name, args, self.now, check=self.check, readers=READERS)
        self.case = case
        return content, error

    def attempt(self, key: str, latest: int, name: str, **args):
        """Act, but a refused action waits for a newer staff record before it is tried again, so one refusal is not met on every call.
        Returns None when it waited, else the gate's (words, refused)."""
        if self.refused_at.get(key, -1) >= latest:
            return None
        out = self.act(name, **args)
        if out[1]:
            self.refused_at[key] = latest
        else:
            self.refused_at.pop(key, None)
        return out

    def hand(self, label: str, words: str, key: str | None = None) -> None:
        key = key or label
        if key in self.open_topics or key in self.declined:
            return
        reason = _fits(f"{label}: {words}") if words else label
        _, error = self.act("hand_to_staff", question=reason, record_ids=[str(i) for i in self.evidence], role="front_desk",
                            wake_evidence="staff note or work order change")
        if error:
            self.declined.append(key)         # the gate's second check found it routine work: carry on, and do not hand it again
            self.save()
            return
        self.open_topics[key] = words
        self.topic_at[key] = self.cur_at.isoformat()
        self.handed_now = True
        self.save()

    def save(self, state: str | None = None, cursor: int | None = None) -> None:
        if cursor is not None:
            self.cursor = cursor
        links = [{"cursor": self.cursor, "topics": self.open_topics, "topic_at": self.topic_at, "followed": self.done_orders,
                  "started": self.started, "refused_at": self.refused_at, "declined": self.declined, "intents": self.intents}]
        changes = {"links": links}
        if state:
            changes["state"] = state
        self.case = cases.record(self.store, self.stream, self.case.id, "updated", changes, self.now, source=AGENT)

    # ---- one pass
    def run(self) -> None:
        snap = Snap(self.store, self.stream, self.case, self.now)
        new = [e for e in snap.events if e.id > self.cursor and e.source != AGENT and e.kind in WATCH]
        if not new and self.started:
            return
        # the move-out itself is the first turn: nothing is known about the unit yet, so it is held before anything else is read
        newest = max((e.id for e in new), default=self.cursor)
        wants: list[tuple[str, str, str | None]] = []
        for e in new:
            self.cur, self.cur_at = e.id, snap.at[e.id]
            wants += self.read_event(snap, e)
        self.cur, self.cur_at = newest, self.now
        seen: list[tuple[str, str, str | None]] = []
        for kind, text, record in wants:
            if not any(k == kind and (clean_scope(text) == clean_scope(t) if kind == "cleaning" else _same_words(text, t)) for k, t, _ in seen):
                seen.append((kind, text, record))
        tried: set[str] = set()
        for kind, text, record in seen:
            snap = Snap(self.store, self.stream, self.case, self.now)
            if kind == "look":
                if not any(klass(o) == "inspection" for o in snap.open_orders()):
                    self.attempt("inspect", snap.latest, "request_inspection")
            elif record and not covered(snap, kind, text):
                tried.add(f"open:{record}:{kind}")
                self.try_open(snap, kind, text, record)
            elif record:
                self.intents.pop(f"open:{record}:{kind}", None)       # an open order already says it
        self.retry_intents(tried)
        self.done_orders += [i for i in self.finished if i not in self.done_orders]
        self.link(Snap(self.store, self.stream, self.case, self.now))
        self.inspect_and_hold()
        self.started = True
        self.save(cursor=newest)
        self.finish_state()

    def try_open(self, snap: Snap, kind: str, text: str, record: str) -> None:
        """Ask the gate to open the work order. A refusal keeps the intent (with the watermark, so it waits for a newer staff record);
        a success ends it for good."""
        key = f"open:{record}:{kind}"
        out = self.attempt(key, snap.latest, "open_work_order", text=_fits(text), answers_record=record)
        if out is None:
            return
        if not out[1]:
            self.intents.pop(key, None)
            return
        self.intents[key] = {"kind": kind, "text": text, "record": record}
        if "former tenant" in out[0]:
            self.evidence = [record]               # the gate says removing these is a staff decision: it goes to staff
            self.hand(BELONGINGS, text)

    def retry_intents(self, tried: set[str]) -> None:
        """Work orders the gate refused earlier come back once a newer staff record is written, if the work is still needed."""
        for key, intent in list(self.intents.items()):
            if key in tried:
                continue
            snap = Snap(self.store, self.stream, self.case, self.now)
            if not self.still_wanted(snap, intent):
                self.intents.pop(key, None)
                self.refused_at.pop(key, None)
                continue
            self.try_open(snap, intent["kind"], intent["text"], intent["record"])

    @staticmethod
    def still_wanted(snap: Snap, intent: dict) -> bool:
        """Whether the work a refused intent asked for still needs an order: not when staff corrected the observation it came from, the
        door was seen undamaged since, its report was closed, an open order already says it, or a later look speaks to it and finds it fine."""
        record, text = intent["record"], intent["text"]
        seen = snap.wk.observations.get(record)
        if seen is not None:
            if seen.superseded_by:
                return False
            standing = snap.rent.floor.standing(snap.unit)
            if seen.door == "damaged" and standing is not None and standing.door in ("closed", "open"):
                return False
        order = snap.wk.orders.get(record)
        if order is not None and (order.state == "closed" or order.joined_into):
            return False
        if covered(snap, intent["kind"], text):
            return False
        origin = min((e.id for e in snap.events if e.kind in ("walk.observed", "work_order.reported")
                      and record in (e.payload.get("observation"), e.payload.get("order"))), default=snap.case.source_event)
        look = latest_look(snap, origin)
        return not (look and look[1] == "fine" and _speaks_to(text, look[3]))

    def read_event(self, snap: Snap, e) -> list[tuple[str, str, str | None]]:
        p, work = e.payload, []
        text, source, record = None, "", None
        self.evidence = []
        if e.kind == "walk.observed" and p["unit"] == snap.unit:
            text, source, record = p.get("note", ""), "note", p.get("observation")
            self.evidence = [record] if record else []
            if p.get("contents") == "seen":
                self.hand(BELONGINGS, text or "Belongings were seen inside the unit.")
            elif p.get("contents") == "not seen":
                self.open_topics.pop(BELONGINGS, None)
            if p.get("lock") == "present" and p.get("whose") in ("tenant", "unknown"):
                self.hand(LOCKED, text or "A lock is still on the door of the vacated unit.")
            elif p.get("lock") == "absent":
                self.open_topics.pop(LOCKED, None)         # staff recorded the lock gone; "unknown" keeps the handoff
            if p.get("door") == "damaged":
                door = text if DOOR.search(text or "") else f"Door damaged: {text}".rstrip(": ")
                work.append(("repair", door))
        elif e.kind in ("work_order.reported", "work_order.reported_again"):
            o = snap.wk.orders.get(p["order"])
            if o is None or o.unit != snap.unit:
                return []
            text, source = p.get("problem") or p.get("text"), "report"
            self.evidence = [o.id]
        elif e.kind in ("work_order.recorded", "work_order.checked", "work_order.closed"):
            o = snap.wk.orders.get(p["order"])
            if o is None or o.unit != snap.unit:
                return []
            text, source = p.get("text"), "inspection" if klass(o) == "inspection" else "work"
            self.evidence = [o.id]
            if e.kind == "work_order.closed":
                self.after_finish(snap, o)
        if text:
            r = read_words(text, snap.unit)
            for label, words in r.topics.items():
                if label == UNCLEAR and source != "note":
                    continue                 # a report, a repair record or an inspection is already an action: only a bare note can be too vague
                self.hand(label, words)
            if e.kind == "walk.observed":
                work += [w for w in r.work if not (p.get("door") == "damaged" and w[0] == "repair" and w[1] == text)]
        return self.merge(work, record)

    @staticmethod
    def merge(work: list[tuple[str, str]], record: str | None) -> list[tuple[str, str, str | None]]:
        """One entry's findings become one order (its words side by side) and, when the entry only doubts, one request to look."""
        firm = [(k, t) for k, t in work if k != "look"]
        out: list[tuple[str, str, str | None]] = []
        if firm:
            kind = "repair" if any(k == "repair" for k, _ in firm) else "cleaning"
            body = "; ".join(t for _, t in firm)
            out.append((kind, f"Cleaning needed: {body}" if kind == "cleaning" else body, record))
        elif work:
            out.append(("look", work[0][1], record))
        return out

    def after_finish(self, snap: Snap, order: walk.WorkOrder) -> None:
        """What a finished order or inspection says that only a person can settle goes to staff. The unit stays held on what it says is
        still wrong (the latest look decides), so no order is opened for it. Orders opened from an inspection's findings or from a repair's
        "not finished" words were left open when the repair was recorded under another entry, and an open order stops the offer for good;
        in the practice cases that cost the release in 2 of 29 records, so staff's report or the repair record carries the work."""
        if order.id in self.done_orders:
            return
        self.finished.append(order.id)
        state, still, found = read_finish(snap.texts(order), snap.unit)
        for label, words in found.topics.items():
            if label != UNCLEAR:
                self.hand(label, words)
        if klass(order) == "inspection" and state == "problem" and still:
            text = "; ".join(t for _, t in still)
            prev = self.last_repair(snap, snap.closed.get(order.id, 0))
            if prev is not None and self.repair_failed(prev, snap.texts(prev), text):
                self.hand(FAILING, f"{text} (found again after: {' '.join(snap.texts(prev))[:120]})")
            failed = self.times_found(snap, order, still)
            if failed >= FAILED_INSPECTIONS:
                self.hand(FAILING, f"{failed} inspections found the fault again.")

    @staticmethod
    def last_repair(snap: Snap, before: int):
        done = [o for o in snap.wk.orders.values() if o.unit == snap.unit and o.state == "closed" and not o.joined_into
                and klass(o) != "inspection" and o.id not in snap.no_work and 0 < snap.closed.get(o.id, 0) < before]
        return max(done, key=lambda o: snap.closed[o.id]) if done else None

    @staticmethod
    def repair_failed(prev: walk.WorkOrder, texts: list[str], finding: str) -> bool:
        """The last repair said it fixed the part, and the inspection that followed finds that same part still failing."""
        if klass(prev) != "repair":
            return False
        work = " ".join(texts)
        low = _low(work)
        if _STILL.search(low) or _NOT_DONE.search(low):
            return False
        return bool(_part_set(finding) & _part_set(_text_of(prev) + " " + work))

    def times_found(self, snap: Snap, order: walk.WorkOrder, still: list[tuple[str, str]]) -> int:
        """How many inspections up to this one found the same fault as this one (three different faults are not one that keeps failing)."""
        count = 0
        for o in snap.orders():
            if klass(o) != "inspection" or o.state != "closed" or snap.closed.get(o.id, 0) > snap.closed.get(order.id, 0):
                continue
            state, earlier, _ = read_finish(snap.texts(o), snap.unit)
            if state == "problem" and any(_same_fault_words(a, b) for _, a in still for _, b in earlier):
                count += 1
        return count

    # ---- duplicates
    def link(self, snap: Snap) -> None:
        orders = [o for o in snap.open_orders() if klass(o) != "inspection"]
        orders.sort(key=lambda o: (o.reported, o.id))
        gone: set[str] = set()
        for i, later in enumerate(orders):
            for earlier in orders[:i]:
                if earlier.id in gone or later.id in gone or frozenset((earlier.id, later.id)) in snap.wk.apart:
                    continue
                ka, kb = klass(earlier), klass(later)
                if ka == kb == "cleaning":
                    same = order_scope(earlier) == order_scope(later)
                elif "cleaning" in (ka, kb):
                    same = False
                else:
                    same = verdict(earlier, later) == wr.SAME or (verdict(earlier, later) == wr.UNSURE and relaxed_same(earlier, later))
                if not same:
                    continue
                busy = ("work recorded", "staff checked")
                about, into = (later, earlier) if later.state not in busy else (earlier, later)
                if about.state in busy:
                    continue
                out = self.attempt(f"link:{about.id}:{into.id}", snap.latest, "link_reports", order=about.id, into=into.id)
                if out and not out[1]:
                    gone.add(about.id)
                    break

    # ---- inspection, hold, release
    def inspect_and_hold(self) -> None:
        snap = Snap(self.store, self.stream, self.case, self.now)
        orders = snap.orders()
        open_now = [o for o in orders if o.state != "closed"]
        done = [snap.closed.get(o.id, 0) for o in orders if klass(o) != "inspection" and o.state == "closed" and not o.joined_into
                and o.id not in snap.no_work]
        floor = max([self.case.source_event] + done)        # any finished work (repair, cleaning) has to be followed by a look at the unit
        look = latest_look(snap, floor)
        fine = bool(look) and look[1] == "fine"
        if fine:
            # a look after a topic was raised answers it; only a lock stays until staff record it gone
            for key in [k for k in self.open_topics if k != LOCKED and datetime.fromisoformat(self.topic_at.get(k, self.now.isoformat())) < look[2]]:
                self.open_topics.pop(key, None)
                self.topic_at.pop(key, None)
            # a report nobody worked on, which the look speaks to and finds fine, needs no work: the gate decides whether it may be closed
            for o in open_now:
                if (klass(o) != "inspection" and o.state == "reported" and snap.reported.get(o.id, 0) < look[0]
                        and addresses(o, look[3])):
                    self.attempt(f"nowork:{o.id}", snap.latest, "close_report_no_work", order=o.id)
            snap = Snap(self.store, self.stream, self.case, self.now)
            open_now = [o for o in snap.orders() if o.state != "closed"]
        asked = any(klass(o) == "inspection" for o in open_now)
        if floor > self.case.source_event and not open_now and look is None and not asked:
            self.attempt("inspect", snap.latest, "request_inspection")
            snap = Snap(self.store, self.stream, self.case, self.now)
            open_now = [o for o in snap.orders() if o.state != "closed"]
        if (self.open_topics and not self.handed_now and any(klass(o) == "inspection" for o in open_now)
                and not any(klass(o) != "inspection" for o in open_now)):
            # Work is finished and a look is asked for, with a handoff still open. The gate reads whether staff's records settled that
            # question only when a release is asked for, and it reads every staff record since the handoff, so nothing is out of view.
            # Two reasons remain to ask now: once the check passes the gate saves it on the case (settled_question), so the matter is
            # settled for good before the inspection's own words are added to what a later check reads; and a refusal here costs
            # nothing, since the open request for a look makes the gate refuse the release itself.
            if self.case.state == "needs staff":
                self.save(state="working")        # the gate dates a handoff from its newest "needs staff" entry; do not add one now
            self.attempt("settle", snap.latest, "release_unit")
        pending = bool(open_now) or bool(self.open_topics) or not fine
        if pending:
            if snap.vacant() and not snap.lifted_by_staff:
                first = open_now[0].problem if open_now else next(iter(self.open_topics.values()), "no look at the unit yet")
                self.attempt("hold", snap.latest, "hold_unit", reason=_fits(f"Not ready: {first}"))
            return
        if not snap.our_block():
            return                           # nothing of ours to lift; a block staff placed stays
        others = [x for x in tools.read("readiness", self.store, self.stream, self.case, self.now)["reasons"]
                  if x != "the unit is blocked" and not x.startswith("work order ")]
        if others:
            self.hand(FINDING, others[0])
            return
        if self.case.state == "needs staff":
            # the gate judges "staff wrote after the handoff" from the newest case entry that says needs staff, and its own checks write
            # one when the case is in that state: leave the state before asking, or nothing written since can ever count
            self.save(state="waiting")
        self.attempt("release", snap.latest, "release_unit")

    def finish_state(self) -> None:
        snap = Snap(self.store, self.stream, self.case, self.now)
        if self.case.state == "done":
            return                           # the gate closes the case when it releases the unit
        if any(e.kind == "lease.signed" and e.id > self.case.source_event and e.payload.get("unit") == snap.unit for e in snap.events):
            state = "done"
        elif self.handed_now:
            # the gate's steps put the case back to working. A new handoff is put back on top, once: the gate reads "what staff wrote
            # since the handoff" from the newest entry that says needs staff, so putting it back on every pass would drop the earlier
            # staff words (a manager's ruling) from what it judges
            state = "needs staff"
        elif self.case.state == "needs staff" and not self.open_topics:
            state = "working" if snap.open_orders() else "waiting"
        else:
            return
        if state != self.case.state:
            self.save(state=state)


def _label(kind: str, text: str) -> str:
    if kind == "cleaning":
        return f"Cleaning needed: {text}"
    if kind == "look":
        return f"Check: {text}"
    return text


def _same_fault_words(a: str, b: str) -> bool:
    """Whether two finishing clauses are about one fault: the rules say same, or they name a common part (or only the same door)."""
    if _same_words(a, b):
        return True
    (la, ga), (lb, gb) = wr._parts(_low(a)), wr._parts(_low(b))
    return bool(la & lb) or (not la and not lb and bool(ga & gb))


def _same_words(a: str, b: str) -> bool:
    norm = lambda s: " ".join(re.findall(r"[a-z0-9]+", s.lower()))
    return norm(a) == norm(b) or wr.same_fault({"text": a, "unit": "x", "at": None}, {"text": b, "unit": "x", "at": None}) == wr.SAME


def arm(store: Store, stream: str, now: datetime, check=None) -> None:
    """Open a case for each move-out recorded since, then give each open case's new records a turn. `check` is the gate's second-opinion
    function (the gate's own default when none is given)."""
    known = {c.source_event for c in cases.load(store, stream).values()}
    books = None
    for e in store.stream_events(stream):
        if e.kind == "lease.ended" and e.id not in known:
            books = books or store.books(stream)
            lease = books.leases.get(e.payload["lease"])
            if lease is not None:
                cases.open_case(store, stream, e, lease.unit, now)
    newest: dict[str, cases.Case] = {}
    for case in sorted(cases.open_cases(store, stream), key=lambda c: c.source_event):
        newest[case.unit] = case
    for case in cases.open_cases(store, stream):
        if newest[case.unit].id == case.id:
            Worker(store, stream, case, now, check).run()
        else:
            cases.record(store, stream, case.id, "updated", {"state": "done"}, now, source=AGENT)
