"""Ask for one bounded choice and keep its outcome with the case."""
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from urllib.request import Request, urlopen
from uuid import uuid4

from core.store import Event, Store
from core.tilden import cases


# Input costs ten cents per million tokens; output tokens are free.
INPUT_CENTS_PER_TOKEN = 10 / 1_000_000


@dataclass
class Result:
    choice: str | None
    probabilities: dict
    reason: str | None = None
    usage: dict | None = None
    confidence: float | None = None


def check(store: Store, stream: str, case_id: str, kind: str, question: str, text: str, now: datetime, choices=None) -> Result:
    """Ask one bounded question. Failure is recorded and never becomes an answer."""
    if case_id not in cases.load(store, stream):
        raise KeyError(case_id)
    if kind not in ('predicate', 'choice') or (kind == 'choice' and (not choices or len(set(choices)) != len(choices))):
        raise ValueError('invalid question')
    usage, outcome, reason, selected, probabilities = {}, 'failed', None, None, {}
    try:
        base = os.environ['TILDEN_DECIDE_BASE_URL'].rstrip('/')
        key, model = os.environ['TILDEN_DECIDE_API_KEY'], os.environ['TILDEN_DECIDE_MODEL']
        if not base.startswith(('https://', 'http://')) or not key or not model:
            raise ValueError('not configured')
        query = {'type': kind, 'name': 'check', 'instructions': question}
        if kind == 'choice':
            query['choices'] = [{'value': value} for value in choices]
        body = {'model': model, 'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}], 'questions': [query]}
        request = Request(base + '/decisions', data=json.dumps(body).encode(), method='POST',
                          headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
        usage = data.get('usage', {})
        if not isinstance(usage, dict):
            usage = {}
        answer = next(a for a in data['answers'] if a.get('name') == 'check')
        if answer.get('type') == 'refusal':
            outcome, reason = 'refused', 'The decision service refused the request.'
        elif kind == 'predicate' and answer.get('type') == 'predicate':
            probability = answer['probability']
            if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
                raise ValueError('invalid probability')
            probabilities, selected, outcome = {'yes': probability}, 'yes', 'chosen'
        elif kind == 'choice' and answer.get('type') == 'choice':
            selected = answer['choice']
            entries = answer['probabilities']
            if selected not in choices or not isinstance(entries, list):
                raise ValueError('invalid choice')
            probabilities = {value: 0 for value in choices}
            seen = set()
            for entry in entries:
                value, probability = entry['value'], entry['probability']
                if value not in choices or value in seen or isinstance(probability, bool) or not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
                    raise ValueError('invalid probability')
                seen.add(value)
                probabilities[value] = probability
            if abs(sum(probabilities.values()) - 1) > 0.01:
                raise ValueError('invalid probabilities')
            outcome = 'chosen'
        else:
            raise ValueError('invalid answer')
    except Exception:
        selected, probabilities, reason = None, {}, 'The decision service failed or returned an invalid answer.'
    tokens = usage.get('input_tokens', 0)
    cost = max(0, tokens) * INPUT_CENTS_PER_TOKEN if isinstance(tokens, int) and not isinstance(tokens, bool) else 0
    with store.write():
        store.append(stream, [Event('tilden.decision', now, {'case': case_id, 'outcome': outcome, 'choice': selected,
            'probabilities': probabilities, 'reason': reason, 'usage': usage, 'confidence': None, 'cost_cents': cost},
            f'tilden:decision:{uuid4().hex}', source='code')])
    return Result(selected, probabilities, reason, usage)
