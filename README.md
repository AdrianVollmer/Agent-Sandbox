# Agent Sandbox

Run an agent in a sandboxed environment

## Development

This project uses [uv](https://docs.astral.sh/uv/) for dependency management
and [just](https://github.com/casey/just) as a task runner.

```sh
just sync           # install dependencies
just fmt            # format code
just lint           # lint with ruff
just typecheck      # type-check with ty
just check          # lint + typecheck + format-check (no modifications)
just test           # run tests
just run [args]     # run the app
just build          # build sdist and wheel
just docs           # serve docs locally
just hooks-install  # run checks automatically on commit
just bump [level]   # tag the next version (patch/minor/major), default patch
```

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs `just ci` on every push and
pull request: `check` (lint, type-check, format-check), tests, a docs
build, and a package build.

## License

MIT © Adrian Vollmer
