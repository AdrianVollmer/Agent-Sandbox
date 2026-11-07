"""Bookkeeping of running sandboxes, so other commands can find and manage them.

`run` registers its session in the state directory and unregisters it when
the agent exits. Entries of `run` processes that died without cleaning up
are pruned on the next lookup.
"""

import json
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent_sandbox.allowlist import DynamicAllowlist
from agent_sandbox.engine import ComposeProject

log = logging.getLogger(__name__)

PROJECT_PREFIX = "agent-sandbox-"
COMPOSE_FILE = "compose.yaml"
DYNAMIC_DIR = "dynamic"


class SessionError(Exception):
    """Raised when no session or more than one matches."""


@dataclass(frozen=True)
class Session:
    """A running sandbox: its compose project and the directory holding its files."""

    name: str
    engine: str
    directory: Path
    cwd: Path
    pid: int
    started: datetime
    # ID of the workspace the agent works in, see agent_sandbox.workspace
    workspace: str | None = None

    @property
    def short_id(self) -> str:
        """Return the random part of the project name."""
        return self.name.removeprefix(PROJECT_PREFIX)

    @property
    def project(self) -> ComposeProject:
        """Return the compose project to run engine commands against."""
        return ComposeProject(engine=self.engine, file=self.directory / COMPOSE_FILE, name=self.name)

    @property
    def allowlist(self) -> DynamicAllowlist:
        """Return the exceptions added while the session runs."""
        return DynamicAllowlist(self.directory / DYNAMIC_DIR)

    def to_json(self) -> str:
        """Serialize for the registry."""
        data = {
            "name": self.name,
            "engine": self.engine,
            "directory": str(self.directory),
            "cwd": str(self.cwd),
            "pid": self.pid,
            "started": self.started.isoformat(),
            "workspace": self.workspace,
        }
        return json.dumps(data, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "Session":
        """Deserialize a registry entry, raising ValueError or KeyError if it is malformed."""
        data = json.loads(text)
        return cls(
            name=data["name"],
            engine=data["engine"],
            directory=Path(data["directory"]),
            cwd=Path(data["cwd"]),
            pid=int(data["pid"]),
            started=datetime.fromisoformat(data["started"]),
            workspace=data.get("workspace"),
        )


def pid_alive(pid: int) -> bool:
    """Return whether a process with this PID exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, but belongs to someone else
    return True


@dataclass(frozen=True)
class SessionRegistry:
    """One JSON file per running session in `directory`."""

    directory: Path
    is_alive: Callable[[int], bool] = pid_alive

    def _path(self, name: str) -> Path:
        return self.directory / f"{name}.json"

    def register(self, session: Session) -> None:
        """Record a started session."""
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path(session.name).write_text(session.to_json())
        log.debug("Registered session %s", session.name)

    def unregister(self, name: str) -> None:
        """Forget a session; no error if it is unknown."""
        self._path(name).unlink(missing_ok=True)
        log.debug("Unregistered session %s", name)

    def sessions(self) -> list[Session]:
        """Return the running sessions, oldest first, pruning stale entries."""
        if not self.directory.is_dir():
            return []
        sessions = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                session = Session.from_json(path.read_text())
            except (OSError, ValueError, KeyError, TypeError) as e:
                log.warning("Ignoring unreadable session file %s: %s", path, e)
                continue
            if not self.is_alive(session.pid):
                log.debug("Pruning session %s, its process %d is gone", session.name, session.pid)
                path.unlink(missing_ok=True)
                continue
            sessions.append(session)
        return sorted(sessions, key=lambda s: s.started)


def select_session(sessions: Sequence[Session], name: str | None, cwd: Path) -> Session:
    """Pick the session named `name` (full or short id), else the only one, else the only one started in `cwd`."""
    if not sessions:
        raise SessionError("No running sessions")
    if name is not None:
        for session in sessions:
            if name in (session.name, session.short_id):
                return session
        raise SessionError(f"No running session {name!r}")
    if len(sessions) == 1:
        return sessions[0]
    here = [s for s in sessions if s.cwd == cwd]
    if len(here) == 1:
        log.debug("Picked session %s started in %s", here[0].name, cwd)
        return here[0]
    ids = ", ".join(s.short_id for s in sessions)
    raise SessionError(f"Several sessions are running ({ids}), pick one with --session")
