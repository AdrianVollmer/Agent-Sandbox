import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.shell_completion import CompletionItem, ShellComplete
from click.testing import CliRunner

from agent_sandbox.cli import cli
from agent_sandbox.engine import ComposeProject, EngineError
from agent_sandbox.session import Session, SessionRegistry
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


def test_render_with_host_dns_pins_names(write_config: Callable[..., Path], monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(host: str, *_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        return [(2, 1, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr("agent_sandbox.hostdns.socket.getaddrinfo", resolve)
    config = write_config('[agent]\nimage = "i"\n[proxy]\nallowed_domains = ["ai.syss.intern"]\nhost_dns = true')
    result = CliRunner().invoke(cli, ["render", "--config", str(config)])
    assert result.exit_code == 0, result.output
    compose = json.loads(result.output.split("# squid.conf\n")[0].removeprefix("# compose.yaml\n"))
    assert compose["services"]["proxy"]["extra_hosts"] == ["ai.syss.intern:10.1.2.3"]


def test_run_registers_session_while_agent_runs(
    write_config: Callable[..., Path], fake_run: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = SessionRegistry(tmp_path / "state" / "agent-sandbox" / "sessions")
    during: list[Any] = []

    def _run(self: ComposeProject, *, run_args: tuple[str, ...], command: tuple[str, ...]) -> int:
        during.extend(registry.sessions())
        fake_run["compose"] = json.loads(self.file.read_text())
        return 0

    monkeypatch.setattr("agent_sandbox.engine.ComposeProject.run", _run)
    result = CliRunner().invoke(cli, ["run", "--config", str(write_config())])
    assert result.exit_code == 0, result.output
    [session] = during
    assert session.cwd == Path.cwd()
    assert session.allowlist.entries() == []
    assert (
        f"{session.allowlist.directory}:/etc/squid/agent-sandbox:ro,Z"
        in fake_run["compose"]["services"]["proxy"]["volumes"]
    )
    assert registry.sessions() == []


@pytest.fixture
def live_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Session:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    session = Session(
        name="agent-sandbox-abcd1234",
        engine="podman",
        directory=tmp_path / "work",
        cwd=tmp_path,
        pid=os.getpid(),
        started=datetime(2026, 11, 5, 10, 0, tzinfo=UTC),
    )
    session.directory.mkdir()
    session.allowlist.create()
    SessionRegistry(tmp_path / "state" / "agent-sandbox" / "sessions").register(session)
    return session


@pytest.fixture
def reloads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr("agent_sandbox.engine.ComposeProject.reload_proxy", lambda self: calls.append(self.name))
    return calls


def test_allow_adds_exceptions_and_reloads(live_session: Session, reloads: list[str]) -> None:
    result = CliRunner().invoke(cli, ["allow", "pypi.org", "10.0.0.0/8"])
    assert result.exit_code == 0, result.output
    assert live_session.allowlist.entries() == ["pypi.org", "10.0.0.0/8"]
    assert reloads == ["agent-sandbox-abcd1234"]
    assert "pypi.org" in result.output


def test_allow_nothing_new_skips_reload(live_session: Session, reloads: list[str]) -> None:
    live_session.allowlist.add(["pypi.org"])
    result = CliRunner().invoke(cli, ["allow", "-s", "abcd1234", "pypi.org"])
    assert result.exit_code == 0, result.output
    assert reloads == []


def test_allow_rejects_invalid_destination(live_session: Session, reloads: list[str]) -> None:
    result = CliRunner().invoke(cli, ["allow", "pypi.org:443"])
    assert result.exit_code == 2
    assert "Invalid destination" in result.output
    assert live_session.allowlist.entries() == []
    assert reloads == []


def test_allow_without_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    result = CliRunner().invoke(cli, ["allow", "pypi.org"])
    assert result.exit_code == 1
    assert "No running sessions" in result.output


def test_allow_reports_reload_failure(live_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(self: ComposeProject) -> None:
        raise EngineError("Reloading the proxy failed: boom")

    monkeypatch.setattr("agent_sandbox.engine.ComposeProject.reload_proxy", fail)
    result = CliRunner().invoke(cli, ["allow", "pypi.org"])
    assert result.exit_code == 1
    assert "boom" in result.output


def test_revoke_removes_exceptions_and_reloads(live_session: Session, reloads: list[str]) -> None:
    live_session.allowlist.add(["pypi.org", "github.com"])
    result = CliRunner().invoke(cli, ["revoke", "pypi.org", "nothere.com"])
    assert result.exit_code == 0, result.output
    assert live_session.allowlist.entries() == ["github.com"]
    assert reloads == ["agent-sandbox-abcd1234"]
    assert "nothere.com" in result.output


def test_sessions_lists_running_sessions(live_session: Session) -> None:
    live_session.allowlist.add(["pypi.org"])
    result = CliRunner().invoke(cli, ["sessions"])
    assert result.exit_code == 0, result.output
    assert "abcd1234" in result.output
    assert str(live_session.cwd) in result.output
    assert "pypi.org" in result.output


def test_sessions_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    result = CliRunner().invoke(cli, ["sessions"])
    assert result.exit_code == 0
    assert "No running sessions" in result.output


ACCESS_LOG = """\
1793872474.321 0 10.89.0.3 TCP_DENIED/403 3385 CONNECT pypi.org:443 - HIER_NONE/- text/html
1793872475.321 0 10.89.0.3 TCP_DENIED/403 3385 CONNECT pypi.org:443 - HIER_NONE/- text/html
1793872476.321 0 10.89.0.3 TCP_DENIED/403 3385 CONNECT github.com:443 - HIER_NONE/- text/html
"""


def test_blocked_lists_denials_not_allowed_since(live_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_sandbox.engine.ComposeProject.proxy_access_log", lambda self: ACCESS_LOG)
    live_session.allowlist.add(["github.com"])
    result = CliRunner().invoke(cli, ["blocked"])
    assert result.exit_code == 0, result.output
    [line] = [line for line in result.output.splitlines() if "pypi.org" in line]
    assert line.split()[0] == "2"
    assert "github.com" not in result.output


def test_blocked_nothing(live_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_sandbox.engine.ComposeProject.proxy_access_log", lambda self: "")
    result = CliRunner().invoke(cli, ["blocked"])
    assert result.exit_code == 0
    assert "Nothing blocked" in result.output


def _complete(args: list[str], incomplete: str) -> list[CompletionItem]:
    return ShellComplete(cli, {}, "agent-sandbox", "_AGENT_SANDBOX_COMPLETE").get_completions(args, incomplete)


@pytest.mark.parametrize("command", ["allow", "revoke", "blocked"])
def test_complete_session_ids(live_session: Session, command: str) -> None:
    [item] = _complete([command, "--session"], "ab")
    assert item.value == "abcd1234"
    assert item.help == str(live_session.cwd)
    assert _complete([command, "-s"], "zz") == []


def test_complete_revoke_offers_current_exceptions(live_session: Session) -> None:
    live_session.allowlist.add(["pypi.org", "github.com", "10.0.0.0/8"])
    assert [i.value for i in _complete(["revoke"], "p")] == ["pypi.org"]
    assert [i.value for i in _complete(["revoke", "-s", "abcd1234", "pypi.org"], "")] == ["github.com", "10.0.0.0/8"]


def test_complete_revoke_without_matching_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Completion must stay quiet instead of failing in the user's shell
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert _complete(["revoke"], "") == []


def test_render_krun_flag_sets_runtime(write_config: Callable[..., Path]) -> None:
    result = CliRunner().invoke(cli, ["render", "--krun", "--config", str(write_config())])
    assert result.exit_code == 0, result.output
    compose = json.loads(result.output.split("# squid.conf\n")[0].removeprefix("# compose.yaml\n"))
    assert compose["services"]["agent"]["runtime"] == "krun"


def test_run_krun_checks_kvm(
    write_config: Callable[..., Path], fake_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("agent_sandbox.engine.KVM_DEVICE", tmp_path / "no-kvm")
    result = CliRunner().invoke(cli, ["run", "--krun", "--config", str(write_config())])
    assert result.exit_code == 2
    assert "needs KVM" in result.output
    assert "compose" not in fake_run
