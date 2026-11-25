"""One store is used by the request threads and the shift's thread at the same time; nothing may fail or interleave."""
import threading
from datetime import datetime, timezone

from core.store import Event, Store


def test_concurrent_writes_and_reads_on_one_store(tmp_path):
    store = Store(str(tmp_path / "core.db"))
    at = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
    errors, writers, readers = [], 4, 4

    def write(n):
        try:
            for i in range(60):
                with store.write():
                    store.append(f"visitor:{n}", [Event("note.recorded", at, {"n": n, "i": i}, f"note-{n}-{i}")])
        except Exception as error:          # noqa: BLE001 - any failure is the finding
            errors.append(repr(error))

    def read():
        try:
            for _ in range(150):
                for n in range(writers):
                    events = store.stream_events(f"visitor:{n}")
                    assert all(e.kind == "note.recorded" for e in events)
                store.books("visitor:0")
        except Exception as error:          # noqa: BLE001
            errors.append(repr(error))

    threads = [threading.Thread(target=write, args=(n,)) for n in range(writers)] + [threading.Thread(target=read) for _ in range(readers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert all(len(store.stream_events(f"visitor:{n}")) == 60 for n in range(writers))


def test_a_result_reads_like_a_cursor():
    rows = Store().db.execute("SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3")
    assert rows.fetchone() == (1,) and rows.fetchall() == [(2,), (3,)] and rows.fetchone() is None
