import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_sandbox.config import ConfigError
from agent_sandbox.engine import ComposeProject, EngineError, check_krun, detect_engine, ensure_image, reload_script


def test_detect_engine_prefers_podman() -> None:
    assert detect_engine(None, which=lambda name: f"/usr/bin/{name}") == "podman"


def test_detect_engine_falls_back_to_docker() -> None:
    assert detect_engine(None, which=lambda name: "/usr/bin/docker" if name == "docker" else None) == "docker"


def test_detect_engine_honors_preference() -> None:
    assert detect_engine("docker", which=lambda name: f"/usr/bin/{name}") == "docker"


def test_detect_engine_preferred_missing() -> None:
    with pytest.raises(ConfigError, match="docker"):
        detect_engine("docker", which=lambda _name: None)


def test_detect_engine_none_available() -> None:
    with pytest.raises(ConfigError, match="Neither podman nor docker"):
        detect_engine(None, which=lambda _name: None)


class Recorder:
    def __init__(self, returncodes: list[int], stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, object]] = []
        self.returncodes = returncodes
        self.stdout = stdout

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        self.kwargs.append(kwargs)
        return subprocess.CompletedProcess(args, self.returncodes.pop(0), stdout=self.stdout)


def test_run_returns_exit_code() -> None:
    project = ComposeProject(engine="podman", file=Path("/tmp/c.yaml"), name="sb")
    recorder = Recorder([0, 7])
    code = project.run(run_args=("--user", "1000"), command=("bash",), runner=recorder)
    assert code == 7
    base = ["podman", "compose", "-f", "/tmp/c.yaml", "-p", "sb"]
    assert recorder.calls == [
        [*base, "up", "-d", "proxy"],
        [*base, "run", "--rm", "--no-deps", "--user", "1000", "agent", "bash"],
    ]
    assert recorder.kwargs[0]["stdout"] == subprocess.DEVNULL


def test_run_proxy_start_fails() -> None:
    project = ComposeProject(engine="podman", file=Path("/tmp/c.yaml"), name="sb")
    recorder = Recorder([1])
    with pytest.raises(EngineError, match="proxy"):
        project.run(run_args=(), command=(), runner=recorder)


def test_run_interrupted() -> None:
    project = ComposeProject(engine="docker", file=Path("/tmp/c.yaml"), name="sb")

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise KeyboardInterrupt

    assert project.run(run_args=(), command=(), runner=runner) == 130


def test_teardown_is_detached(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def spawn(args: list[str], **kwargs: Any) -> None:
        calls.append((args, kwargs))

    project = ComposeProject(engine="podman", file=tmp_path / "c.yaml", name="sb")
    log_file = tmp_path / "state" / "teardown.log"
    project.teardown_detached(log_file=log_file, cleanup=tmp_path / "work", spawn=spawn)
    [(args, kwargs)] = calls
    assert args[:2] == ["sh", "-c"]
    assert "podman compose -f" in args[2]
    assert " down -t 0" in args[2]
    # Survives the terminal going away, e.g. Ctrl-C after we exit
    assert kwargs["start_new_session"] is True
    assert log_file.parent.is_dir()


def test_teardown_script_logs_and_cleans_up(tmp_path: Path) -> None:
    # Fake engine that prints its arguments and fails, like a broken `down`
    engine = tmp_path / "fake-engine"
    engine.write_text('#!/bin/sh\necho "called with: $*"\nexit 3\n')
    engine.chmod(0o755)
    work = tmp_path / "work dir"
    work.mkdir()
    (work / "compose.yaml").write_text("{}")
    log_file = tmp_path / "state" / "teardown.log"
    project = ComposeProject(engine=str(engine), file=work / "compose.yaml", name="sb")

    process = project.teardown_detached(log_file=log_file, cleanup=work)
    assert process is not None
    process.wait(timeout=10)

    log = log_file.read_text()
    assert "sb" in log
    assert f"called with: compose -f {work / 'compose.yaml'} -p sb down -t 0" in log
    assert "exit code 3" in log
    assert not work.exists()


def test_ensure_image_skips_existing_image(tmp_path: Path) -> None:
    recorder = Recorder([0])
    ensure_image("podman", "t:1", "FROM x\n", context=tmp_path, runner=recorder)
    assert recorder.calls == [["podman", "image", "inspect", "t:1"]]


def test_ensure_image_builds_missing_image(tmp_path: Path) -> None:
    recorder = Recorder([1, 0])
    ensure_image("docker", "t:1", "FROM x\n", context=tmp_path, runner=recorder)
    assert recorder.calls[1] == ["docker", "build", "-t", "t:1", str(tmp_path)]
    assert (tmp_path / "Dockerfile").read_text() == "FROM x\n"


def test_ensure_image_rebuild_skips_check(tmp_path: Path) -> None:
    recorder = Recorder([0])
    ensure_image("podman", "t:1", "FROM x\n", context=tmp_path, rebuild=True, runner=recorder)
    assert recorder.calls == [["podman", "build", "-t", "t:1", str(tmp_path)]]


def test_ensure_image_build_failure(tmp_path: Path) -> None:
    with pytest.raises(EngineError, match="exit code 2"):
        ensure_image("podman", "t:1", "FROM x\n", context=tmp_path, runner=Recorder([1, 2]))


BASE_CMD = ["podman", "compose", "-f", "/tmp/c.yaml", "-p", "sb"]


def test_reload_proxy_signals_squid_via_exec() -> None:
    project = ComposeProject(engine="podman", file=Path("/tmp/c.yaml"), name="sb")
    recorder = Recorder([0])
    project.reload_proxy(runner=recorder)
    [call] = recorder.calls
    assert call[:9] == [*BASE_CMD, "exec", "-T", "proxy"]
    assert call[9:11] == ["sh", "-c"]
    assert recorder.kwargs[0]["capture_output"] is True


def test_reload_proxy_failure_raises() -> None:
    project = ComposeProject(engine="podman", file=Path("/tmp/c.yaml"), name="sb")
    with pytest.raises(EngineError, match="Reloading the proxy failed"):
        project.reload_proxy(runner=Recorder([1]))


def test_proxy_access_log() -> None:
    project = ComposeProject(engine="podman", file=Path("/tmp/c.yaml"), name="sb")
    recorder = Recorder([0], stdout="line\n")
    assert project.proxy_access_log(runner=recorder) == "line\n"
    assert recorder.calls == [[*BASE_CMD, "exec", "-T", "proxy", "cat", "/var/log/squid/access.log"]]


def test_reload_script_signals_every_squid_process(tmp_path: Path) -> None:
    # Run the real script against a fake /proc with dash-compatible sh
    proc = tmp_path / "proc"
    for pid, comm in [(1, "squid"), (7, "log_file_daemon"), (9, "squid"), (12, "sh")]:
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "comm").write_text(f"{comm}\n")
    script = reload_script(proc=str(proc)).replace("kill -HUP", "echo")
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert sorted(result.stdout.split()) == ["1", "9"]


def test_reload_script_fails_without_squid(tmp_path: Path) -> None:
    (tmp_path / "proc" / "1").mkdir(parents=True)
    result = subprocess.run(["sh", "-c", reload_script(proc=str(tmp_path / "proc"))], capture_output=True, check=False)
    assert result.returncode == 1
    assert b"no squid process" in result.stderr


def test_check_krun_needs_kvm(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        check_krun("podman", kvm=tmp_path / "kvm")


def test_check_krun_needs_kvm_access(tmp_path: Path) -> None:
    kvm = tmp_path / "kvm"
    kvm.touch()
    with pytest.raises(ConfigError, match="not accessible"):
        check_krun("podman", kvm=kvm, access=lambda _path, _mode: False)


def test_check_krun_passes_with_kvm(tmp_path: Path) -> None:
    kvm = tmp_path / "kvm"
    kvm.touch()
    check_krun("podman", kvm=kvm, access=lambda _path, _mode: True)
