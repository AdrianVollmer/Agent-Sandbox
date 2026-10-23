"""Entry point for `python -m agent_sandbox` and the console script."""

from agent_sandbox.cli import cli


def main() -> None:
    """Run the CLI."""
    cli()


if __name__ == "__main__":
    main()
