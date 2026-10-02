from __future__ import annotations

from pathlib import Path

import pytest

from scripts import demo_v09_api as demo


def _allow_unfrozen_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    # The test is deliberately run before RC8's identity freeze; app creation
    # normally rejects a stale frozen runtime hash.
    monkeypatch.setattr("app.api.app.release_identity", lambda: {
        "release_source_fingerprint": "test-source",
        "runtime_payload_fingerprint": "test-runtime",
    })


def test_rc8_demo_releases_sqlite_for_immediate_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_unfrozen_construction(monkeypatch)
    database = tmp_path / "demo.sqlite"
    result = demo.run_demo(database)
    assert result == {"state": "COMPLETED_VERIFIED", "messages": 1, "evidence": 3}
    database.unlink()  # Windows-safe only after TestClient and engine teardown.
    assert not database.exists()


def test_rc8_demo_repeated_lifecycles_release_each_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_unfrozen_construction(monkeypatch)
    for number in range(3):
        database = tmp_path / f"demo-{number}.sqlite"
        assert demo.run_demo(database)["messages"] == 1
        database.unlink()


def test_rc8_demo_disposes_engine_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "failure.sqlite"
    engine = demo.make_engine(f"sqlite:///{database}")
    disposed = False
    original_dispose = engine.dispose

    def dispose() -> None:
        nonlocal disposed
        disposed = True
        original_dispose()

    monkeypatch.setattr(demo, "make_engine", lambda _url: engine)
    monkeypatch.setattr(demo, "create_schema", lambda _engine: (_ for _ in ()).throw(RuntimeError("injected")))
    monkeypatch.setattr(engine, "dispose", dispose)
    with pytest.raises(RuntimeError, match="injected"):
        demo.run_demo(database)
    assert disposed


def test_rc8_demo_cleanup_is_not_suppressed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "cleanup.sqlite"
    engine = demo.make_engine(f"sqlite:///{database}")
    monkeypatch.setattr(demo, "make_engine", lambda _url: engine)
    monkeypatch.setattr(engine, "dispose", lambda: (_ for _ in ()).throw(RuntimeError("dispose failed")))
    with pytest.raises(RuntimeError, match="dispose failed"):
        demo.run_demo(database)
