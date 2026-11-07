import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.shell_completion import CompletionItem, ShellComplete
from click.testing import CliRunner

from agent_sandbox.cli import cli
from agent_sandbox.engine import ComposeProject, EngineError
from agent_sandbox.session import Session, SessionRegistry
from agent_sandbox.workspace import WorkspaceStore
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
    # Not a git repository, so runs mount it read-only instead of cloning
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
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


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in {"NAME": "Test", "EMAIL": "test@example.com"}.items():
        monkeypatch.setenv(f"GIT_AUTHOR_{key}", value)
        monkeypatch.setenv(f"GIT_COMMITTER_{key}", value)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    (repo / "f").write_text("x")
    _git(repo, "add", "f")
    _git(repo, "commit", "--quiet", "-m", "init")
    monkeypatch.chdir(repo)
    return repo


def _store(tmp_path: Path) -> WorkspaceStore:
    return WorkspaceStore(tmp_path / "state" / "agent-sandbox" / "workspaces")


def test_run_in_git_repo_uses_clone(
    write_config: Callable[..., Path], fake_run: dict[str, Any], git_repo: Path, tmp_path: Path
) -> None:
    result = CliRunner().invoke(cli, ["run", "--config", str(write_config())])
    assert result.exit_code == 3, result.output
    [ws] = _store(tmp_path).workspaces()
    assert ws.mode == "clone"
    agent = fake_run["compose"]["services"]["agent"]
    assert f"{ws.repo}:/workspace:Z" in agent["volumes"]
    assert not any(v.startswith(f"{git_repo}:") for v in agent["volumes"])
    assert agent["working_dir"] == "/workspace"
    assert agent["environment"]["SANDBOX_OUTBOX"] == "/exchange/out"
    # Kept after the run, so the work can be fetched
    assert ws.directory.is_dir()


def test_run_outside_git_mounts_read_only_and_cleans_up(
    write_config: Callable[..., Path], fake_run: dict[str, Any], tmp_path: Path
) -> None:
    result = CliRunner().invoke(cli, ["run", "--config", str(write_config())])
    assert result.exit_code == 3, result.output
    assert f"{Path.cwd()}:/workspace:ro,Z" in fake_run["compose"]["services"]["agent"]["volumes"]
    # Nothing in the outbox, nothing to keep
    assert _store(tmp_path).workspaces() == []


def test_run_rw_mounts_read_write(
    write_config: Callable[..., Path], fake_run: dict[str, Any], git_repo: Path, tmp_path: Path
) -> None:
    result = CliRunner().invoke(cli, ["run", "--rw", "--config", str(write_config())])
    assert result.exit_code == 3, result.output
    assert f"{git_repo}:/workspace:Z" in fake_run["compose"]["services"]["agent"]["volumes"]


def test_run_keeps_read_only_workspace_with_outbox(
    write_config: Callable[..., Path], fake_run: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _run(self: ComposeProject, *, run_args: tuple[str, ...], command: tuple[str, ...]) -> int:
        [ws] = _store(tmp_path).workspaces()
        (ws.outbox / "result").write_text("x")
        return 0

    monkeypatch.setattr("agent_sandbox.engine.ComposeProject.run", _run)
    result = CliRunner().invoke(cli, ["run", "--config", str(write_config())])
    assert result.exit_code == 0, result.output
    [ws] = _store(tmp_path).workspaces()
    assert ws.outbox_entries() == ["result"]


@pytest.mark.parametrize("volume", [".:/workspace:Z", ".:/elsewhere"])
def test_run_rejects_volumes_bypassing_workspace(
    write_config: Callable[..., Path], fake_run: dict[str, Any], tmp_path: Path, volume: str
) -> None:
    config = MINIMAL_CONFIG.replace('image = "localhost/agent:latest"', f'image = "i"\nvolumes = ["{volume}"]')
    result = CliRunner().invoke(cli, ["run", "--config", str(write_config(config))])
    assert result.exit_code == 2, result.output
    assert "compose" not in fake_run
    # The workspace made for the failed run is gone again
    assert _store(tmp_path).workspaces() == []


def test_run_resume_reuses_workspace(
    write_config: Callable[..., Path], fake_run: dict[str, Any], git_repo: Path, tmp_path: Path
) -> None:
    ws = _store(tmp_path).create("feed0001", git_repo)
    result = CliRunner().invoke(cli, ["run", "--resume", "feed0001", "--config", str(write_config())])
    assert result.exit_code == 3, result.output
    assert f"{ws.repo}:/workspace:Z" in fake_run["compose"]["services"]["agent"]["volumes"]
    assert [w.id for w in _store(tmp_path).workspaces()] == ["feed0001"]


def test_run_resume_rejects_rw(write_config: Callable[..., Path], fake_run: dict[str, Any]) -> None:
    result = CliRunner().invoke(cli, ["run", "--rw", "--resume", "x", "--config", str(write_config())])
    assert result.exit_code == 2


def test_workspace_commands(git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    ws = _store(tmp_path).create("feed0001", git_repo)
    (ws.repo / "new").write_text("y")
    _git(ws.repo, "add", "new")
    _git(ws.repo, "commit", "--quiet", "-m", "agent work")
    runner = CliRunner()

    listing = runner.invoke(cli, ["workspaces"])
    assert listing.exit_code == 0, listing.output
    assert "feed0001" in listing.output
    assert str(ws.repo) in listing.output

    fetched = runner.invoke(cli, ["fetch"])
    assert fetched.exit_code == 0, fetched.output
    assert "sandbox/feed0001/agent" in fetched.output
    assert _git(git_repo, "log", "--format=%s", "-1", "sandbox/feed0001/agent").strip() == "agent work"

    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"\0")
    put = runner.invoke(cli, ["put", str(payload)])
    assert put.exit_code == 0, put.output
    assert (ws.inbox / "payload.bin").exists()

    (ws.outbox / "artifact").write_text("z")
    got = runner.invoke(cli, ["get", "-o", str(tmp_path / "out")])
    assert got.exit_code == 0, got.output
    assert (tmp_path / "out" / "artifact").read_text() == "z"

    discarded = runner.invoke(cli, ["discard", "feed0001"])
    assert discarded.exit_code == 0, discarded.output
    assert not ws.directory.exists()
    assert _git(git_repo, "for-each-ref", "refs/remotes/sandbox/").strip() == ""


def test_discard_refuses_workspace_in_use(live_session: Session, tmp_path: Path) -> None:
    ws = _store(tmp_path).create("feed0001", tmp_path, read_write=True)
    SessionRegistry(tmp_path / "state" / "agent-sandbox" / "sessions").register(replace(live_session, workspace=ws.id))
    result = CliRunner().invoke(cli, ["discard", "feed0001"])
    assert result.exit_code == 1
    assert "in use" in result.output
    assert ws.directory.exists()
