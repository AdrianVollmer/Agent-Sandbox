"""Building images and running the compose project with podman or docker."""

import logging
import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_sandbox.compose import AGENT_SERVICE
from agent_sandbox.config import ENGINES, ConfigError

log = logging.getLogger(__name__)

Runner = Callable[..., subprocess.CompletedProcess[Any]]

# Conventional exit code for termination by SIGINT
_EXIT_INTERRUPTED = 130


class EngineError(Exception):
    """Raised when a container engine command fails."""


def detect_engine(preferred: str | None, which: Callable[[str], str | None] | None = None) -> str:
    """Return the container engine to use, preferring podman."""
    which = which or shutil.which
    if preferred:
        if not which(preferred):
            raise ConfigError(f"Configured engine {preferred!r} not found in PATH")
        return preferred
    for engine in ENGINES:
        if which(engine):
            log.debug("Detected container engine %s", engine)
            return engine
    raise ConfigError("Neither podman nor docker found in PATH")


def ensure_image(
    engine: str, tag: str, dockerfile: str, *, context: Path, rebuild: bool = False, runner: Runner = subprocess.run
) -> None:
    """Build `dockerfile` as `tag` unless that image already exists.

    `context` should be an empty directory, it becomes the build context.
    """
    if not rebuild:
        inspect = runner([engine, "image", "inspect", tag], check=False, capture_output=True)
        if inspect.returncode == 0:
            log.debug("Image %s exists, not building", tag)
            return
    (context / "Dockerfile").write_text(dockerfile)
    log.info("Building image %s", tag)
    code = runner([engine, "build", "-t", tag, str(context)], check=False).returncode
    if code != 0:
        raise EngineError(f"Building {tag} failed with exit code {code}")


@dataclass(frozen=True)
class ComposeProject:
    """A compose file on disk plus the engine and project name to run it with."""

    engine: str
    file: Path
    name: str

    def _compose(self, *args: str) -> list[str]:
        return [self.engine, "compose", "-f", str(self.file), "-p", self.name, *args]

    def run(self, *, run_args: Sequence[str], command: Sequence[str], runner: Runner = subprocess.run) -> int:
        """Run the agent interactively and return its exit code."""
        cmd = self._compose("run", "--rm", *run_args, AGENT_SERVICE, *command)
        log.debug("Running %s", cmd)
        try:
            return runner(cmd, check=False).returncode
        except KeyboardInterrupt:
            log.warning("Interrupted")
            return _EXIT_INTERRUPTED

    def teardown_detached(self, *, log_file: Path, cleanup: Path, spawn: Callable[..., Any] = subprocess.Popen) -> Any:
        """Remove the project in a background process, then delete `cleanup`.

        The agent is gone at this point, so nobody needs to wait for this.
        `down -t 0` kills the proxy right away, it holds no state worth a
        graceful stop. Output, including podman-compose complaining about the
        already removed `run --rm` container, goes to `log_file`.
        """
        log_file.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        script = (
            f"{{ echo {shlex.quote(f'== {stamp} {self.name}')}; "
            f"{shlex.join(self._compose('down', '-t', '0'))} "
            '|| echo "teardown failed with exit code $?"; '
            f"}} >> {shlex.quote(str(log_file))} 2>&1; "
            f"rm -rf -- {shlex.quote(str(cleanup))}"
        )
        log.info("Tearing down %s in the background, log: %s", self.name, log_file)
        return spawn(
            ["sh", "-c", script],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
