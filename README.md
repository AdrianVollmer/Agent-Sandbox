# Agent Sandbox

Run an agent in a sandboxed environment.

Run an agent in a container whose only network access is an allowlisting
HTTP proxy.

## How it works

`agent-sandbox run` generates a compose project with two services:

- `proxy`: a Squid container attached to an internal network and an egress
  network. It only lets requests to `allowed_domains` and the domains of the
  configured `presets` through (HTTP on port 80, HTTPS via CONNECT on port
  443).
- `agent`: your image, or one built from a Dockerfile (`agent.build_file`,
  or inline as `agent.build`). It is attached to the internal network only,
  so it has no route to the outside, and `HTTP_PROXY`/`HTTPS_PROXY` (plus
  lowercase variants) point at the proxy.

The tool then runs `podman compose run --rm agent` (or `docker compose`).
When the agent exits, `compose down` removes the project in the background, so
you get your prompt back right away; its output goes to
`$XDG_STATE_HOME/agent-sandbox/teardown.log` (default `~/.local/state/...`).
Every invocation gets a random project name, so several sandboxes can run side
by side.

## Configuration

The config is TOML and is looked up in this order, first match wins:

1. `--config PATH`
2. `$XDG_CONFIG_HOME/agent-sandbox/config.toml` (default `~/.config/...`)
3. `/etc/agent-sandbox/config.toml`

See [`examples/config.toml`](examples/config.toml) for all options. [`examples/Dockerfile`](examples/Dockerfile)
builds an agent image with Claude Code, Codex, Hermes, Pi, OpenCode, Maki, mise and toolchains for
JavaScript, Rust, Go and C/C++; its header shows the matching config. Unknown
keys are rejected to catch typos.

## Usage

```sh
agent-sandbox run                 # configured command (e.g. a shell), else image default
agent-sandbox run claude --dangerously-skip-permissions
agent-sandbox run --rebuild       # rebuild the image from the Dockerfile first
agent-sandbox run --engine docker
agent-sandbox render              # print compose file and squid.conf, run nothing
agent-sandbox presets             # list domain presets (claude-code, debian, pypi, uv, npm, ...)
agent-sandbox -v run              # debug logging
```

Notes:

- Environment values copied from the host end up in the generated compose file.
  It lives in a private temporary directory (mode 0600) and is deleted after
  the run.
- Tools that ignore proxy environment variables will have no network access.
  That is intended.
- Besides domains, `proxy.allowed_ips` takes IP addresses and networks, and
  `proxy.allowed_ports` opens destination ports beyond 80 and 443. To reach a
  service on the host, allow `host.containers.internal` (podman) or the
  host's bridge address, usually `172.17.0.1` (docker), plus its port.
- If the host resolves names the containers can't (internal domains via VPN
  or split DNS), set `proxy.host_dns = true`. The tool then resolves the
  allowed names on the host at startup and pins them in the proxy's
  `/etc/hosts`; wildcard entries can't be pinned.
- Custom CA certificates (e.g. a TLS-inspecting corporate network) must be
  trusted inside the agent container, since the proxy only tunnels HTTPS.
  Mount the host CA bundle and set `NODE_EXTRA_CA_CERTS`/`REQUESTS_CA_BUNDLE`;
  `examples/config.toml` has a commented example.
- Images from `agent.build`/`agent.build_file` are built by the tool itself,
  with normal network access (not through the proxy), and tagged
  `localhost/agent-sandbox:<hash>`. Old tags are not cleaned up;
  `podman image prune` takes care of them.
- The compose file is written as JSON, which compose parses as YAML.

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
