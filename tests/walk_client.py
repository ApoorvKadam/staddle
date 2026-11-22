"""A stand-in for the model client Tilden calls: it returns the answers it was given, in order, and records what it was asked. No network.
An answer may be a function of the prompt, so a test can cite the ids the investigation made for its own run."""
import json
import re
from types import SimpleNamespace


class StandIn:
    def __init__(self, *answers, tokens=(900, 120), on_call=None):
        self.answers, self.asked, self.tokens, self.on_call = list(answers), [], tokens, on_call
        self.messages = self

    def create(self, **kw):
        self.asked.append(kw)
        if self.on_call:
            self.on_call()
        answer = self.answers.pop(0)
        if callable(answer):
            answer = answer(kw["messages"][0]["content"])
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(answer))],
                               usage=SimpleNamespace(input_tokens=self.tokens[0], output_tokens=self.tokens[1], cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))


def history_ids(content: str) -> list[str]:
    """The record ids in an investigation prompt, in order."""
    return re.findall(r"^(\S+-\d+) \[", content, re.M)


def field(value="unknown", words=None):
    return {"value": value, "words": words}


def note_answer(unit=("C335", "C335"), lock=None, whose=None, tag=None, door=None, contents=None, flags=()):
    """What the reading call returns for a note; each field is (value, words) or left unknown."""
    return {"unit": {"value": unit[0], "words": unit[1]} if unit else {"value": None, "words": None},
            "lock": field(*(lock or ("unknown", None))), "whose": field(*(whose or ("unknown", None))), "tag": field(*(tag or ("unknown", None))),
            "door": field(*(door or ("unknown", None))), "contents": field(*(contents or ("unknown", None))), "flags": list(flags)}


def packet(ids, step="check_payment", reason="ledger_shows_paid", summary="The ledger shows nothing past due for the tenant, yet the walk found "
           "the facility overlock on the door.", contradiction="The walk saw an overlock on a unit whose tenant owes nothing past due."):
    return {"summary": summary, "contradiction": contradiction, "records": list(ids), "step": step, "reason": reason}
