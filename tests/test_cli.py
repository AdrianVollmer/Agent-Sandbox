import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from agent_sandbox.cli import cli
from agent_sandbox.engine import ComposeProject, EngineError
from tests.conftest import MINIMAL_CONFIG


def test_render_prints_compose_and_squid_conf(write_config: Callable[..., Path]) -> None:
    result = CliRunner().invoke(cli, ["render", "--config", str(write_config())])
    assert result.exit_code == 0, result.output
    compose_text, squid_text = result.output.split("# squid.conf\n")
    compose = json.loads(compose_text.removeprefix("# compose.yaml\n"))
    assert compose["services"]["agent"]["image"] == "localhost/agent:latest"
    assert "acl allowed_domains dstdomain .anthropic.com" in squid_text


def test_invalid_config_reports_error(write_config: Callable[..., Path]) -> None:
    result = CliRunner().invoke(cli, ["render", "--config", str(write_config("[agent]"))])
    assert result.exit_code == 2
    assert "agent: set exactly one of" in result.output


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def _run(self: ComposeProject, *, run_args: tuple[str, ...], command: tuple[str, ...]) -> int:
        seen["command"] = command
        seen["compose"] = json.loads(self.file.read_text())
        return 3

    def _teardown(self: ComposeProject, *, log_file: Path, cleanup: Path) -> None:
        seen["teardown"] = {
            "log_file": log_file,
            "cleanup": cleanup,
            "files": sorted(p.name for p in cleanup.iterdir()),
        }

    monkeypatch.setattr("agent_sandbox.engine.ComposeProject.run", _run)
    monkeypatch.setattr("agent_sandbox.engine.ComposeProject.teardown_detached", _teardown)
    monkeypatch.setattr("agent_sandbox.engine.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return seen


SHELL_CONFIG = MINIMAL_CONFIG.replace('image = "localhost/agent:latest"', 'image = "i"\ncommand = ["bash"]')


def test_run_passes_exit_code_and_compose(write_config: Callable[..., Path], fake_run: dict[str, Any]) -> None:
    result = CliRunner().invoke(cli, ["run", "--config", str(write_config())])
    assert result.exit_code == 3, result.output
    assert "agent" in fake_run["compose"]["services"]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], ("bash",)),
        (["claude", "--dangerously-skip-permissions"], ("claude", "--dangerously-skip-permissions")),
        # Once the command starts, our own options are not parsed any more
        (["claude", "-c", "--engine", "x"], ("claude", "-c", "--engine", "x")),
        (["--", "claude"], ("claude",)),
    ],
)
def test_run_command_line(
    write_config: Callable[..., Path], fake_run: dict[str, Any], args: list[str], expected: tuple[str, ...]
) -> None:
    result = CliRunner().invoke(cli, ["run", "--config", str(write_config(SHELL_CONFIG)), *args])
    assert result.exit_code == 3, result.output
    assert fake_run["command"] == expected


def test_run_without_command_uses_image_default(write_config: Callable[..., Path], fake_run: dict[str, Any]) -> None:
    result = CliRunner().invoke(cli, ["run", "--config", str(write_config())])
    assert result.exit_code == 3, result.output
    assert fake_run["command"] == ()


def test_presets_command_lists_presets() -> None:
    result = CliRunner().invoke(cli, ["presets"])
    assert result.exit_code == 0
    assert "debian" in result.output
    assert "deb.debian.org" in result.output


@pytest.mark.parametrize("rebuild", [False, True])
def test_run_builds_inline_dockerfile(
    write_config: Callable[..., Path], fake_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch, rebuild: bool
) -> None:
    builds: list[tuple[str, str, bool]] = []

    def fake_ensure(engine: str, tag: str, dockerfile: str, *, context: Path, rebuild: bool = False) -> None:
        builds.append((tag, dockerfile, rebuild))
        assert context.is_dir()
        # The build context must not contain the compose file, which may hold secrets
        assert list(context.iterdir()) == []

    monkeypatch.setattr("agent_sandbox.engine.ensure_image", fake_ensure)
    config = write_config('[agent]\nbuild = "FROM debian"\n[proxy]\npresets = ["debian"]')
    result = CliRunner().invoke(cli, ["run", "--config", str(config), *(["--rebuild"] if rebuild else [])])
    assert result.exit_code == 3, result.output
    [(tag, dockerfile, seen_rebuild)] = builds
    assert dockerfile == "FROM debian"
    assert seen_rebuild is rebuild
    assert fake_run["compose"]["services"]["agent"]["image"] == tag


def test_run_with_image_does_not_build(
    write_config: Callable[..., Path], fake_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agent_sandbox.engine.ensure_image", lambda *_a, **_k: pytest.fail("should not build"))
    result = CliRunner().invoke(cli, ["run", "--config", str(write_config())])
    assert result.exit_code == 3, result.output


def test_run_hands_temp_dir_to_teardown(
    write_config: Callable[..., Path], fake_run: dict[str, Any], tmp_path: Path
) -> None:
    result = CliRunner().invoke(cli, ["run", "--config", str(write_config())])
    assert result.exit_code == 3, result.output
    teardown = fake_run["teardown"]
    # The detached teardown needs the compose file and deletes the directory itself
    assert "compose.yaml" in teardown["files"]
    assert teardown["log_file"] == tmp_path / "state" / "agent-sandbox" / "teardown.log"


def test_run_cleans_up_when_build_fails(
    write_config: Callable[..., Path], fake_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp(**kwargs: Any) -> str:
        path = real_mkdtemp(**kwargs)
        created.append(Path(path))
        return path

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise EngineError("build broke")

    monkeypatch.setattr("tempfile.mkdtemp", mkdtemp)
    monkeypatch.setattr("agent_sandbox.engine.ensure_image", fail)
    config = write_config('[agent]\nbuild = "FROM x"\n[proxy]\npresets = ["debian"]')
    result = CliRunner().invoke(cli, ["run", "--config", str(config)])
    assert result.exit_code == 1
    assert "build broke" in result.output
    assert created
    assert not any(p.exists() for p in created)
