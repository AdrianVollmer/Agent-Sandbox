"""Command line interface."""

# Avoid top level imports in this file for optimal start up times
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from click.shell_completion import CompletionItem

    from agent_sandbox.config import Config
    from agent_sandbox.engine import ComposeProject
    from agent_sandbox.session import Session, SessionRegistry
    from agent_sandbox.workspace import Mount, Workspace, WorkspaceStore

_CONFIG_OPTION = click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Config file. Default: $XDG_CONFIG_HOME/agent-sandbox/config.toml, then /etc/agent-sandbox/config.toml.",
)


@click.group()
@click.option("-v", "--verbose", count=True, help="Enable debug output.")
def cli(verbose: int) -> None:
    """Run an agent in a container whose only network access is an allowlisting proxy."""
    from agent_sandbox.log import setup_logging

    setup_logging(verbose)


def _load(config_path: Path | None) -> "Config":
    import os

    from agent_sandbox.config import ConfigError, find_config, load_config

    try:
        return load_config(config_path or find_config(os.environ))
    except ConfigError as e:
        raise click.UsageError(str(e)) from e


def _write_project(config: "Config", directory: Path, workspace: "Mount | None" = None) -> Path:
    """Write compose file and squid.conf to `directory`, return the compose file path."""
    import json
    import os

    from agent_sandbox.allowlist import DynamicAllowlist
    from agent_sandbox.compose import build_compose
    from agent_sandbox.config import ConfigError
    from agent_sandbox.session import COMPOSE_FILE, DYNAMIC_DIR
    from agent_sandbox.squid import render_squid_conf

    squid_conf = directory / "squid.conf"
    squid_conf.write_text(render_squid_conf(config.proxy))
    dynamic_dir = directory / DYNAMIC_DIR
    DynamicAllowlist(dynamic_dir).create()
    proxy_hosts = None
    if config.proxy.host_dns:
        from agent_sandbox.hostdns import pinnable_names, resolve_on_host

        proxy_hosts = resolve_on_host(pinnable_names(config.proxy))
    try:
        compose = build_compose(
            config,
            squid_conf=squid_conf,
            dynamic_dir=dynamic_dir,
            environ=os.environ,
            cwd=Path.cwd(),
            home=Path.home(),
            proxy_hosts=proxy_hosts,
            workspace=workspace,
        )
    except ConfigError as e:
        raise click.UsageError(str(e)) from e
    compose_file = directory / COMPOSE_FILE
    # JSON is valid YAML, which spares us a YAML dependency. The file can hold
    # secrets passed via env, so keep it private.
    compose_file.touch(mode=0o600)
    compose_file.write_text(json.dumps(compose, indent=2) + "\n")
    return compose_file


def _prepare_project(
    config: "Config", engine: str, *, rebuild: bool, name: str, workspace: "Mount"
) -> "tuple[ComposeProject, Path]":
    """Build the image if needed and write the project to a new temporary directory."""
    import shutil
    import tempfile

    from agent_sandbox import engine as engines
    from agent_sandbox.session import PROJECT_PREFIX

    tmp = Path(tempfile.mkdtemp(prefix=PROJECT_PREFIX))
    try:
        if config.agent.build is not None:
            # Separate empty context, the compose file may hold secrets
            context = tmp / "build"
            context.mkdir()
            engines.ensure_image(engine, config.agent.image_ref, config.agent.build, context=context, rebuild=rebuild)
        project = engines.ComposeProject(engine=engine, file=_write_project(config, tmp, workspace), name=name)
    except BaseException as e:
        shutil.rmtree(tmp, ignore_errors=True)
        if isinstance(e, engines.EngineError):
            raise click.ClickException(str(e)) from e
        raise
    return project, tmp


# Stop parsing our own options once the command starts, so its options
# (e.g. `-c`) are passed on instead of being taken as ours
@cli.command(context_settings={"allow_interspersed_args": False})
@_CONFIG_OPTION
@click.option("--engine", type=click.Choice(["podman", "docker"]), help="Override the configured engine.")
@click.option("--rebuild", is_flag=True, help="Rebuild the agent image from `agent.build` even if it exists.")
@click.option("--rw", is_flag=True, help="Mount the current directory read-write instead of working in a clone.")
@click.option(
    "--resume",
    "resume_id",
    metavar="ID",
    shell_complete=lambda *a: _complete_workspace(*a),
    help="Continue in an existing workspace (see `workspaces`).",
)
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def run(
    config_path: Path | None,
    engine: str | None,
    rebuild: bool,
    rw: bool,
    resume_id: str | None,
    command: tuple[str, ...],
) -> None:
    """Start the proxy and run COMMAND in the agent container.

    Inside a git repository, the agent works in a clone of it, see `fetch` to
    get its commits. Elsewhere, the current directory is mounted read-only
    unless --rw is given. Without COMMAND, the configured command runs, or
    the image default if none is configured. Example: `agent-sandbox run
    claude --dangerously-skip-permissions`.
    """
    import logging
    import os
    import secrets
    from datetime import datetime

    from agent_sandbox import engine as engines
    from agent_sandbox.config import ConfigError, state_dir
    from agent_sandbox.session import PROJECT_PREFIX, Session
    from agent_sandbox.workspace import DEFAULT_WORKDIR, WorkspaceError

    log = logging.getLogger("agent_sandbox.cli")
    if rw and resume_id:
        raise click.UsageError("--rw and --resume cannot be combined, the workspace keeps its mode")
    config = _load(config_path)
    command = command or config.agent.command
    try:
        engine = engines.detect_engine(engine or config.engine)
    except ConfigError as e:
        raise click.UsageError(str(e)) from e

    store = _workspaces()
    short_id = secrets.token_hex(4)
    try:
        if resume_id:
            workspace = store.get(resume_id)
            _check_not_in_use(workspace)
        else:
            workspace = store.create(short_id, Path.cwd(), read_write=rw)
    except WorkspaceError as e:
        raise click.ClickException(str(e)) from e
    try:
        mount = workspace.mount(config.agent.workdir or DEFAULT_WORKDIR)
        project, tmp = _prepare_project(
            config, engine, rebuild=rebuild, name=f"{PROJECT_PREFIX}{short_id}", workspace=mount
        )
    except BaseException:
        if not resume_id:
            store.discard(workspace)
        raise
    session = Session(
        name=project.name,
        engine=engine,
        directory=tmp,
        cwd=Path.cwd(),
        pid=os.getpid(),
        started=datetime.now().astimezone(),
        workspace=workspace.id,
    )
    registry = _registry()
    log.info("Starting %s with %s compose", project.name, engine)
    try:
        registry.register(session)
        code = project.run(run_args=config.agent.run_args, command=command)
    finally:
        registry.unregister(session.name)
        # Deletes tmp once the project is down, so no context manager here
        project.teardown_detached(log_file=state_dir(os.environ) / "teardown.log", cleanup=tmp)
        _report_workspace(store, workspace, keep=bool(resume_id))
    raise SystemExit(code)


def _report_workspace(store: "WorkspaceStore", workspace: "Workspace", *, keep: bool) -> None:
    """Tell the user how to get at the agent's work, or drop a workspace with nothing in it."""
    import logging

    from agent_sandbox.workspace import CLONE

    log = logging.getLogger("agent_sandbox.cli")
    outbox = workspace.outbox_entries()
    if workspace.mode != CLONE and not outbox and not keep:
        store.discard(workspace)
        return
    if workspace.mode == CLONE:
        log.info(
            "The agent's work is in workspace %s. Fetch it with `agent-sandbox fetch %s`, then review "
            "`git log -p HEAD..%s`",
            workspace.id,
            workspace.id,
            workspace.agent_ref,
        )
    if outbox:
        log.info("Outbox: %s. Copy it out with `agent-sandbox get -w %s`", " ".join(outbox), workspace.id)
    log.info("Remove the workspace with `agent-sandbox discard %s` when done", workspace.id)


@cli.command()
@_CONFIG_OPTION
def render(config_path: Path | None) -> None:
    """Print the generated compose file and squid.conf without running anything."""
    import tempfile

    config = _load(config_path)
    with tempfile.TemporaryDirectory(prefix="agent-sandbox-") as tmp:
        compose_file = _write_project(config, Path(tmp))
        click.echo("# compose.yaml")
        click.echo(compose_file.read_text(), nl=False)
        click.echo("# squid.conf")
        click.echo((Path(tmp) / "squid.conf").read_text(), nl=False)


@cli.command()
def presets() -> None:
    """List the domain presets usable in `proxy.presets`."""
    from agent_sandbox.presets import PRESETS

    for name, domains in sorted(PRESETS.items()):
        click.echo(f"{click.style(name, bold=True)}: {' '.join(domains)}")


def _workspaces() -> "WorkspaceStore":
    import os

    from agent_sandbox.config import state_dir
    from agent_sandbox.workspace import WorkspaceStore

    return WorkspaceStore(state_dir(os.environ) / "workspaces")


def _select_workspace(workspace_id: str | None) -> "Workspace":
    from agent_sandbox.workspace import WorkspaceError

    try:
        return _workspaces().select(workspace_id, Path.cwd())
    except WorkspaceError as e:
        raise click.ClickException(str(e)) from e


def _check_not_in_use(workspace: "Workspace") -> None:
    from agent_sandbox.workspace import WorkspaceError

    for session in _registry().sessions():
        if session.workspace == workspace.id:
            raise WorkspaceError(f"Workspace {workspace.id} is in use by session {session.short_id}")


def _complete_workspace(_ctx: click.Context, _param: click.Parameter, incomplete: str) -> "list[CompletionItem]":
    from click.shell_completion import CompletionItem

    return [
        CompletionItem(w.id, help=f"{w.mode} {w.source}")
        for w in _workspaces().workspaces()
        if w.id.startswith(incomplete)
    ]


def _registry() -> "SessionRegistry":
    import os

    from agent_sandbox.config import state_dir
    from agent_sandbox.session import SessionRegistry

    return SessionRegistry(state_dir(os.environ) / "sessions")


def _select(name: str | None) -> "Session":
    from agent_sandbox.session import SessionError, select_session

    try:
        return select_session(_registry().sessions(), name, Path.cwd())
    except SessionError as e:
        raise click.ClickException(str(e)) from e


def _complete_session(_ctx: click.Context, _param: click.Parameter, incomplete: str) -> "list[CompletionItem]":
    from click.shell_completion import CompletionItem

    return [
        CompletionItem(s.short_id, help=str(s.cwd)) for s in _registry().sessions() if s.short_id.startswith(incomplete)
    ]


def _complete_exception(ctx: click.Context, _param: click.Parameter, incomplete: str) -> "list[CompletionItem]":
    """Offer the exceptions of the selected session that are not on the command line yet."""
    from click.shell_completion import CompletionItem

    from agent_sandbox.session import SessionError, select_session

    try:
        session = select_session(_registry().sessions(), ctx.params.get("session_name"), Path.cwd())
    except SessionError:
        return []
    given = set(ctx.params.get("destinations") or ())
    return [CompletionItem(e) for e in session.allowlist.entries() if e.startswith(incomplete) and e not in given]


_SESSION_OPTION = click.option(
    "-s",
    "--session",
    "session_name",
    metavar="ID",
    shell_complete=_complete_session,
    help="Session to act on (see `sessions`). Default: the only one, else the only one started in this directory.",
)


@cli.command()
def sessions() -> None:
    """List running sessions and the exceptions added to them."""
    running = _registry().sessions()
    if not running:
        click.echo("No running sessions")
    for session in running:
        started = session.started.astimezone().strftime("%Y-%m-%d %H:%M")
        click.echo(f"{click.style(session.short_id, bold=True)}  {started}  {session.cwd}")
        if session.workspace:
            click.echo(f"  workspace: {session.workspace}")
        if entries := session.allowlist.entries():
            click.echo(f"  allowed: {' '.join(entries)}")


@cli.command()
@_SESSION_OPTION
def blocked(session_name: str | None) -> None:
    """List destinations the proxy denied, newest first.

    Destinations allowed since are left out.
    """
    from agent_sandbox.accesslog import denied_destinations
    from agent_sandbox.engine import EngineError

    session = _select(session_name)
    try:
        log_text = session.project.proxy_access_log()
    except EngineError as e:
        raise click.ClickException(str(e)) from e
    allowlist = session.allowlist
    denials = [d for d in denied_destinations(log_text.splitlines()) if not allowlist.allows(d.host)]
    if not denials:
        click.echo("Nothing blocked")
    for denial in denials:
        port = "" if denial.port is None else str(denial.port)
        click.echo(f"{denial.count:>5}  {denial.last_seen.strftime('%H:%M:%S')}  {port:>5}  {denial.host}")


def _change_allowlist(session_name: str | None, destinations: tuple[str, ...], *, add: bool) -> None:
    from agent_sandbox.allowlist import parse_destination
    from agent_sandbox.config import ConfigError
    from agent_sandbox.engine import EngineError

    session = _select(session_name)
    allowlist = session.allowlist
    try:
        requested = list(dict.fromkeys(map(parse_destination, destinations)))
        changed = allowlist.add(requested) if add else allowlist.remove(requested)
    except ConfigError as e:
        raise click.UsageError(str(e)) from e
    verb, unchanged = ("allowed", "already allowed") if add else ("revoked", "not a dynamic exception")
    for entry in changed:
        click.echo(f"{verb}: {entry}")
    for entry in requested:
        if entry not in changed:
            click.echo(f"{unchanged}: {entry}")
    if not changed:
        return
    try:
        session.project.reload_proxy()
    except EngineError as e:
        raise click.ClickException(f"{e}. The change is saved and applies on the next successful reload.") from e
    click.echo(f"Reloaded the proxy of session {session.short_id}")


@cli.command()
@_SESSION_OPTION
@click.argument("destinations", nargs=-1, required=True)
def allow(session_name: str | None, destinations: tuple[str, ...]) -> None:
    """Let a running session reach DESTINATIONS until it ends.

    A destination is a domain (.example.com includes subdomains), an IP
    address or a network. Ports stay limited to those in the config.
    """
    _change_allowlist(session_name, destinations, add=True)


@cli.command()
@_SESSION_OPTION
@click.argument("destinations", nargs=-1, required=True, shell_complete=_complete_exception)
def revoke(session_name: str | None, destinations: tuple[str, ...]) -> None:
    """Remove exceptions added with `allow`.

    New connections are refused right away, open ones stay up.
    """
    _change_allowlist(session_name, destinations, add=False)


_WORKSPACE_OPTION = click.option(
    "-w",
    "--workspace",
    "workspace_id",
    metavar="ID",
    shell_complete=_complete_workspace,
    help="Workspace to act on (see `workspaces`). Default: the only one, else the only one for this directory.",
)


@cli.command()
def workspaces() -> None:
    """List workspaces: clones the agent worked in and the exchange folders."""
    from agent_sandbox.workspace import CLONE

    found = _workspaces().workspaces()
    if not found:
        click.echo("No workspaces")
    in_use = {s.workspace: s.short_id for s in _registry().sessions() if s.workspace}
    for workspace in found:
        created = workspace.created.astimezone().strftime("%Y-%m-%d %H:%M")
        running = f"  (session {in_use[workspace.id]})" if workspace.id in in_use else ""
        click.echo(
            f"{click.style(workspace.id, bold=True)}  {created}  {workspace.mode:5}  {workspace.source}{running}"
        )
        if workspace.mode == CLONE:
            click.echo(f"  clone:  {workspace.repo}")
        click.echo(f"  inbox:  {workspace.inbox}")
        outbox = workspace.outbox_entries()
        click.echo(f"  outbox: {workspace.outbox} ({len(outbox)} entries)")


@cli.command()
@click.argument("workspace_id", metavar="[ID]", required=False, shell_complete=_complete_workspace)
def fetch(workspace_id: str | None) -> None:
    """Fetch the branches of a clone into the repository it was made from.

    They end up as remote-tracking branches under sandbox/ID/, the agent's
    own branch as sandbox/ID/agent. Nothing is checked out or merged.
    """
    from agent_sandbox import workspace as ws

    workspace = _select_workspace(workspace_id)
    try:
        refs = ws.fetch(workspace)
    except ws.WorkspaceError as e:
        raise click.ClickException(str(e)) from e
    for ref in refs:
        click.echo(ref.removeprefix("refs/remotes/"))
    if workspace.base:
        click.echo(
            f"Review with `git log -p {workspace.base[:12]}..{workspace.agent_ref}`, "
            f"merge with `git merge {workspace.agent_ref}`",
            err=True,
        )


@cli.command()
@click.argument("workspace_ids", metavar="ID...", nargs=-1, required=True, shell_complete=_complete_workspace)
def discard(workspace_ids: tuple[str, ...]) -> None:
    """Delete workspaces with their clone and exchange folders.

    Branches fetched from them are deleted too, merged commits stay.
    """
    from agent_sandbox.workspace import WorkspaceError

    store = _workspaces()
    try:
        selected = [store.get(i) for i in workspace_ids]
        for workspace in selected:
            _check_not_in_use(workspace)
    except WorkspaceError as e:
        raise click.ClickException(str(e)) from e
    for workspace in selected:
        refs = store.discard(workspace)
        click.echo(f"discarded: {workspace.id}" + (f" ({len(refs)} fetched branches deleted)" if refs else ""))


@cli.command()
@_WORKSPACE_OPTION
@click.argument("paths", nargs=-1, required=True, type=click.Path(path_type=Path, exists=True))
def put(workspace_id: str | None, paths: tuple[Path, ...]) -> None:
    """Copy files or directories into a workspace's inbox.

    The agent sees them read-only under $SANDBOX_INBOX (/exchange/in), right
    away if it is running.
    """
    from agent_sandbox import workspace as ws

    workspace = _select_workspace(workspace_id)
    try:
        names = ws.put(workspace, paths)
    except (ws.WorkspaceError, OSError) as e:
        raise click.ClickException(str(e)) from e
    for name in names:
        click.echo(f"{ws.INBOX_PATH}/{name}")


@cli.command()
@_WORKSPACE_OPTION
@click.option(
    "-o",
    "--output",
    "dest",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
    help="Directory to copy to.",
)
@click.option("--keep-mode", is_flag=True, help="Keep executable bits, which are dropped by default.")
@click.argument("names", nargs=-1)
def get(workspace_id: str | None, dest: Path, keep_mode: bool, names: tuple[str, ...]) -> None:
    """Copy NAMES (default: everything) from a workspace's outbox.

    The agent writes them to $SANDBOX_OUTBOX (/exchange/out). Treat them as
    untrusted: symlinks are skipped and executable bits are dropped unless
    --keep-mode is given.
    """
    from agent_sandbox import workspace as ws

    workspace = _select_workspace(workspace_id)
    try:
        copied = ws.get(workspace, names, dest, keep_mode=keep_mode)
    except (ws.WorkspaceError, OSError) as e:
        raise click.ClickException(str(e)) from e
    if not copied:
        click.echo("Nothing copied")
    for path in copied:
        click.echo(path)
