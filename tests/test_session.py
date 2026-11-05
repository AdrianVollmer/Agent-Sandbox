from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_sandbox.session import Session, SessionError, SessionRegistry, select_session

STARTED = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)


def _session(name: str = "agent-sandbox-aaaa1111", cwd: str = "/work/a", pid: int = 100) -> Session:
    return Session(name=name, engine="podman", directory=Path(f"/tmp/{name}"), cwd=Path(cwd), pid=pid, started=STARTED)


def test_session_paths() -> None:
    session = _session()
    assert session.short_id == "aaaa1111"
    assert session.project.file == Path("/tmp/agent-sandbox-aaaa1111/compose.yaml")
    assert session.project.name == "agent-sandbox-aaaa1111"
    assert session.allowlist.directory == Path("/tmp/agent-sandbox-aaaa1111/dynamic")


def test_registry_roundtrip_and_unregister(tmp_path: Path) -> None:
    registry = SessionRegistry(tmp_path / "sessions", is_alive=lambda _pid: True)
    session = _session()
    registry.register(session)
    assert registry.sessions() == [session]
    registry.unregister(session.name)
    registry.unregister(session.name)  # idempotent
    assert registry.sessions() == []


def test_registry_prunes_dead_sessions(tmp_path: Path) -> None:
    # A `run` killed with SIGKILL never unregisters
    registry = SessionRegistry(tmp_path / "sessions", is_alive=lambda pid: pid == 1)
    registry.register(_session("agent-sandbox-alive", pid=1))
    registry.register(_session("agent-sandbox-dead", pid=2))
    assert [s.name for s in registry.sessions()] == ["agent-sandbox-alive"]
    assert sorted(p.name for p in (tmp_path / "sessions").iterdir()) == ["agent-sandbox-alive.json"]


def test_registry_without_directory(tmp_path: Path) -> None:
    assert SessionRegistry(tmp_path / "missing").sessions() == []


def test_registry_skips_corrupt_files(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    registry = SessionRegistry(tmp_path, is_alive=lambda _pid: True)
    (tmp_path / "agent-sandbox-x.json").write_text("{not json")
    assert registry.sessions() == []
    assert "agent-sandbox-x.json" in caplog.text


A = _session("agent-sandbox-aaaa1111", cwd="/work/a")
B = _session("agent-sandbox-bbbb2222", cwd="/work/b")
B2 = _session("agent-sandbox-cccc3333", cwd="/work/b")


@pytest.mark.parametrize(
    ("sessions", "name", "cwd", "expected"),
    [
        ([A], None, "/elsewhere", A),
        ([A, B], None, "/work/b", B),
        ([A, B], "aaaa1111", "/work/b", A),
        ([A, B], "agent-sandbox-bbbb2222", "/work/a", B),
    ],
)
def test_select_session(sessions: list[Session], name: str | None, cwd: str, expected: Session) -> None:
    assert select_session(sessions, name, Path(cwd)) == expected


@pytest.mark.parametrize(
    ("sessions", "name", "cwd", "message"),
    [
        ([], None, "/work/a", "No running sessions"),
        ([A], "nope", "/work/a", "No running session 'nope'"),
        ([A, B, B2], None, "/work/b", "Several sessions"),
        ([A, B], None, "/elsewhere", "Several sessions"),
    ],
)
def test_select_session_errors(sessions: list[Session], name: str | None, cwd: str, message: str) -> None:
    with pytest.raises(SessionError, match=message):
        select_session(sessions, name, Path(cwd))
