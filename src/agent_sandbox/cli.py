"""Command line interface."""

# Avoid top level imports in this file for optimal start up times
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from agent_sandbox.config import Config

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


def _write_project(config: "Config", directory: Path) -> Path:
    """Write compose file and squid.conf to `directory`, return the compose file path."""
    import json
    import os

    from agent_sandbox.compose import build_compose
    from agent_sandbox.config import ConfigError
    from agent_sandbox.squid import render_squid_conf

    squid_conf = directory / "squid.conf"
    squid_conf.write_text(render_squid_conf(config.proxy))
    try:
        compose = build_compose(config, squid_conf=squid_conf, environ=os.environ, cwd=Path.cwd(), home=Path.home())
    except ConfigError as e:
        raise click.UsageError(str(e)) from e
    compose_file = directory / "compose.yaml"
    # JSON is valid YAML, which spares us a YAML dependency. The file can hold
    # secrets passed via env, so keep it private.
    compose_file.touch(mode=0o600)
    compose_file.write_text(json.dumps(compose, indent=2) + "\n")
    return compose_file


# Stop parsing our own options once the command starts, so its options
# (e.g. `-c`) are passed on instead of being taken as ours
@cli.command(context_settings={"allow_interspersed_args": False})
@_CONFIG_OPTION
@click.option("--engine", type=click.Choice(["podman", "docker"]), help="Override the configured engine.")
@click.option("--rebuild", is_flag=True, help="Rebuild the agent image from `agent.build` even if it exists.")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def run(config_path: Path | None, engine: str | None, rebuild: bool, command: tuple[str, ...]) -> None:
    """Start the proxy and run COMMAND in the agent container.

    Without COMMAND, the configured command runs, or the image default if none
    is configured. Example: `agent-sandbox run claude --dangerously-skip-permissions`.
    """
    import logging
    import secrets
    import tempfile

    from agent_sandbox import engine as engines
    from agent_sandbox.config import ConfigError

    log = logging.getLogger("agent_sandbox.cli")
    config = _load(config_path)
    command = command or config.agent.command
    try:
        engine = engines.detect_engine(engine or config.engine)
    except ConfigError as e:
        raise click.UsageError(str(e)) from e

    with tempfile.TemporaryDirectory(prefix="agent-sandbox-") as tmp:
        if config.agent.build is not None:
            # Separate empty context, the compose file may hold secrets
            context = Path(tmp) / "build"
            context.mkdir()
            try:
                engines.ensure_image(
                    engine, config.agent.image_ref, config.agent.build, context=context, rebuild=rebuild
                )
            except engines.EngineError as e:
                raise click.ClickException(str(e)) from e
        project = engines.ComposeProject(
            engine=engine,
            file=_write_project(config, Path(tmp)),
            name=f"agent-sandbox-{secrets.token_hex(4)}",
        )
        log.info("Starting %s with %s compose", project.name, engine)
        code = project.run(run_args=config.agent.run_args, command=command)
    raise SystemExit(code)


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
