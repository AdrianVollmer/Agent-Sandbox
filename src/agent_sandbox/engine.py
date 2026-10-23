"""Building images and running the compose project with podman or docker."""

import logging
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
        """Run the agent interactively, then tear down the proxy and networks."""
        cmd = self._compose("run", "--rm", *run_args, AGENT_SERVICE, *command)
        log.debug("Running %s", cmd)
        try:
            code = runner(cmd, check=False).returncode
        except KeyboardInterrupt:
            log.warning("Interrupted")
            code = _EXIT_INTERRUPTED
        finally:
            self._down(runner)
        return code

    def _down(self, runner: Runner) -> None:
        log.info("Tearing down project %s", self.name)
        # podman-compose down reports the `run --rm` container as missing since
        # it is already gone; keep that noise out unless something failed
        down = runner(self._compose("down"), check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        output = (down.stdout or "").strip()
        if down.returncode != 0:
            log.warning("Teardown of %s failed (exit code %d):\n%s", self.name, down.returncode, output)
        elif output:
            log.debug("compose down output:\n%s", output)
