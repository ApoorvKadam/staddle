import json
from datetime import datetime, timezone
from urllib.error import URLError

from core.store import Event, Store
from core.tilden import cases, decide

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


def setup_case():
    store = Store()
    with store.write():
        source = store.append('copy', [Event('lease.ended', NOW, {'unit': 'A1'}, 'source')])[0]
    return store, cases.open_case(store, 'copy', source, 'A1', NOW)


def test_predicate_check_records_probability_and_failure(monkeypatch):
    store, case = setup_case()
    monkeypatch.setenv('TILDEN_DECIDE_BASE_URL', 'https://example.invalid')
    monkeypatch.setenv('TILDEN_DECIDE_API_KEY', 'secret')
    monkeypatch.setenv('TILDEN_DECIDE_MODEL', 'configured')
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps({'answers': [{'type': 'predicate', 'name': 'check', 'probability': 0.91}], 'usage': {'input_tokens': 10}}).encode()
    def send(request, timeout):
        assert json.loads(request.data)['questions'] == [{'type': 'predicate', 'name': 'check', 'instructions': 'Ready?'}]
        return Response()
    monkeypatch.setattr(decide, 'urlopen', send)
    result = decide.check(store, 'copy', case.id, 'predicate', 'Ready?', 'record', NOW)
    assert result.probabilities == {'yes': 0.91}
    assert store.stream_events('copy')[-1].payload['cost_cents'] == 10 * decide.INPUT_CENTS_PER_TOKEN
    monkeypatch.setattr(decide, 'urlopen', lambda *args, **kwargs: (_ for _ in ()).throw(URLError('offline')))
    assert decide.check(store, 'copy', case.id, 'predicate', 'Ready?', 'record', NOW).choice is None


def test_choice_check_validates_and_records(monkeypatch):
    store, case = setup_case()
    monkeypatch.setenv('TILDEN_DECIDE_BASE_URL', 'https://example.invalid')
    monkeypatch.setenv('TILDEN_DECIDE_API_KEY', 'secret')
    monkeypatch.setenv('TILDEN_DECIDE_MODEL', 'configured')
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            return json.dumps({'answers': [{'type': 'choice', 'name': 'check', 'choice': 'same', 'probabilities': [{'value': 'same', 'probability': 0.8}, {'value': 'different', 'probability': 0.2}]}]}).encode()
    monkeypatch.setattr(decide, 'urlopen', lambda *args, **kwargs: Response())
    result = decide.check(store, 'copy', case.id, 'choice', 'One fault?', 'records', NOW, ['same', 'different'])
    assert result.choice == 'same' and result.probabilities['same'] == 0.8
