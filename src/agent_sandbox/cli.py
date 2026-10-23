"""Command line interface."""

# Avoid top level imports in this file for optimal start up times
import click


@click.group()
def cli() -> None:
    """Run an agent in a sandboxed environment."""


@cli.command()
def hello() -> None:
    """Print a greeting."""
    click.echo("Hello from Agent Sandbox!")
