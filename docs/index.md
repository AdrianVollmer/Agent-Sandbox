# Agent Sandbox

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

See `examples/config.toml` in the repository for all options. `examples/Dockerfile`
builds an agent image with Claude Code, Codex, Hermes, Pi, OpenCode, Maki, mise and toolchains for
JavaScript, Rust, Go and C/C++; its header shows the matching config. Unknown
keys are rejected to catch typos.

## Usage

```sh
agent-sandbox run                 # configured command (e.g. a shell), else image default
agent-sandbox run claude --dangerously-skip-permissions
agent-sandbox run --rebuild       # rebuild the image from the Dockerfile first
agent-sandbox run --engine docker
agent-sandbox run --krun            # run the agent in a libkrun microVM
agent-sandbox render              # print compose file and squid.conf, run nothing
agent-sandbox presets             # list domain presets (claude-code, debian, pypi, uv, npm, jsr, ...)
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

## Running the agent in a microVM (libkrun)

For a stronger boundary than a container, the agent can run in a
[libkrun](https://github.com/containers/libkrun) microVM with its own kernel.
This uses the `krun` OCI runtime, which is crun built with libkrun support
(`crun-krun` on Fedora), and needs access to `/dev/kvm`.

```toml
[agent]
runtime = "krun"
# Optional, crun defaults to one vCPU per host CPU (max 16) and the
# container's memory limit, or 1024 MiB without one
krun = { cpus = 4, ram_mib = 4096 }
```

`agent-sandbox run --krun` does the same for a single run, and
`agent-sandbox render --krun` shows the result. The proxy stays a normal
container.

Network isolation is unchanged: libkrun's default networking (TSI) opens the
guest's sockets in the container's network namespace, which is only attached
to the internal network, so the proxy is still the only way out. Volumes are
shared with the guest via virtio-fs.

Caveats:

- Tested with podman only. With docker, `krun` must be registered as a runtime
  in the daemon config.
- Container hardening like `cap_drop` and `security_opt` constrains the VM
  process on the host. Don't rely on it inside the guest, where the guest
  kernel is the boundary.

## Allowing destinations at runtime

When the agent hits a blocked destination, you can open it for the running
session from another terminal, without restarting:

```sh
agent-sandbox sessions                 # running sessions and their exceptions
agent-sandbox blocked                  # what the proxy denied, newest first
agent-sandbox allow pypi.org .pythonhosted.org
agent-sandbox revoke pypi.org
agent-sandbox allow -s 1a2b3c4d 10.0.0.5   # pick a session by its ID
```

Without `--session`, the commands act on the only running session, or on the
only one started in the current directory.

Shell completion covers session IDs and, for `revoke`, the session's current
exceptions. Enable it with Click's mechanism, e.g. for bash:
`eval "$(_AGENT_SANDBOX_COMPLETE=bash_source agent-sandbox)"` (`zsh_source`
and `fish_source` work the same way).

A destination is a domain (a leading dot includes subdomains), an IP address
or a network. Exceptions last until the session ends; to keep one, add it to
the config. They don't open extra ports, and with `proxy.host_dns` they are
resolved by the proxy, not pinned.

How it works: each session keeps its exceptions in files in its temporary
directory, mounted read-only into the proxy. `allow` and `revoke` rewrite
them and make Squid reload. Connections already open stay up, so `revoke`
only stops new ones. Nothing in the agent container can reach these files or
the commands, so the agent can't grant itself access.
