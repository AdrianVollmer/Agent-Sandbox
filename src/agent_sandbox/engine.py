"""Building images and running the compose project with podman or docker."""

import logging
import os
import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_sandbox.compose import AGENT_SERVICE, PROXY_SERVICE
from agent_sandbox.config import ENGINES, ConfigError
from agent_sandbox.squid import ACCESS_LOG_PATH

log = logging.getLogger(__name__)

Runner = Callable[..., subprocess.CompletedProcess[Any]]

# Conventional exit code for termination by SIGINT
_EXIT_INTERRUPTED = 130


class EngineError(Exception):
    """Raised when a container engine command fails."""


def reload_script(proc: str = "/proc") -> str:
    """Return a POSIX sh script that sends SIGHUP (reconfigure) to every Squid process.

    Scanning /proc instead of `squid -k reconfigure` or signaling PID 1 avoids
    depending on a PID file (we disable it) or on how the image starts Squid.
    Only shell builtins are used, so it works in minimal images.
    """
    return (
        "n=0; "
        f"for d in {proc}/[0-9]*; do "
        'read -r c < "$d/comm" 2>/dev/null || continue; '
        '[ "$c" = squid ] || continue; '
        'kill -HUP "${d##*/}" && n=$((n + 1)); '
        "done; "
        '[ "$n" -gt 0 ] || { echo "no squid process found" >&2; exit 1; }'
    )


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


KVM_DEVICE = Path("/dev/kvm")


def check_krun(engine: str, kvm: Path | None = None, access: Callable[[Path, int], bool] = os.access) -> None:
    """Fail early if the agent can't run in a libkrun microVM on this host.

    libkrun needs KVM. Without it, the runtime fails with a less helpful
    error only after the proxy has been started.
    """
    kvm = kvm or KVM_DEVICE
    if engine != "podman":
        log.warning("The krun runtime is only tested with podman, %s must have it registered", engine)
    if not kvm.exists():
        raise ConfigError(f"agent.runtime = 'krun' needs KVM, but {kvm} does not exist")
    if not access(kvm, os.R_OK | os.W_OK):
        raise ConfigError(f"agent.runtime = 'krun' needs KVM, but {kvm} is not accessible (add yourself to group kvm?)")


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

    def exec(self, service: str, command: Sequence[str], *, runner: Runner = subprocess.run) -> str:
        """Run `command` in the running `service` container and return its stdout."""
        cmd = self._compose("exec", "-T", service, *command)
        log.debug("Running %s", cmd)
        result = runner(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or f"exit code {result.returncode}"
            raise EngineError(detail)
        return result.stdout

    def reload_proxy(self, *, runner: Runner = subprocess.run) -> None:
        """Make Squid reread its configuration, including the dynamic exceptions.

        Open connections, including HTTPS tunnels, survive this.
        """
        try:
            self.exec(PROXY_SERVICE, ["sh", "-c", reload_script()], runner=runner)
        except EngineError as e:
            raise EngineError(f"Reloading the proxy failed: {e}") from e
        log.debug("Reloaded the proxy of %s", self.name)

    def proxy_access_log(self, *, runner: Runner = subprocess.run) -> str:
        """Return the proxy's access log."""
        try:
            return self.exec(PROXY_SERVICE, ["cat", ACCESS_LOG_PATH], runner=runner)
        except EngineError as e:
            raise EngineError(f"Reading the proxy's access log failed: {e}") from e

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
