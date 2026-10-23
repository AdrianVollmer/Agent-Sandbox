import subprocess
from pathlib import Path

import pytest

from agent_sandbox.config import ConfigError
from agent_sandbox.engine import ComposeProject, EngineError, detect_engine, ensure_image


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
    def __init__(self, returncodes: list[int]) -> None:
        self.calls: list[list[str]] = []
        self.returncodes = returncodes

    def __call__(self, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.returncodes.pop(0))


def test_run_then_down() -> None:
    project = ComposeProject(engine="podman", file=Path("/tmp/c.yaml"), name="sb")
    recorder = Recorder([7, 0])
    code = project.run(run_args=("--user", "1000"), command=("bash",), runner=recorder)
    assert code == 7
    base = ["podman", "compose", "-f", "/tmp/c.yaml", "-p", "sb"]
    assert recorder.calls == [
        [*base, "run", "--rm", "--user", "1000", "agent", "bash"],
        [*base, "down"],
    ]


def test_down_runs_even_when_interrupted() -> None:
    project = ComposeProject(engine="docker", file=Path("/tmp/c.yaml"), name="sb")
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        if "run" in args:
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(args, 0)

    assert project.run(run_args=(), command=(), runner=runner) == 130
    assert calls[-1][-1] == "down"


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
