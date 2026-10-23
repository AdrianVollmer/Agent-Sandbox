"""Building the compose project that wires the agent to the proxy."""

import logging
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from agent_sandbox.config import Config, ConfigError

log = logging.getLogger(__name__)

AGENT_SERVICE = "agent"
PROXY_SERVICE = "proxy"
SQUID_CONF_PATH = "/etc/squid/squid.conf"
NO_PROXY = "localhost,127.0.0.1,::1"

# Overriding these would put the agent on a network with a route to the outside
_PROTECTED_SERVICE_KEYS = frozenset({"networks", "network_mode", "depends_on"})


def resolve_volume(spec: str, *, cwd: Path, home: Path) -> str:
    """Make relative and ~ host paths absolute.

    The compose file lives in a temporary directory, so relative paths would
    otherwise resolve against that instead of where the user ran the command.
    """
    source, sep, rest = spec.partition(":")
    if not sep:
        return spec  # anonymous volume
    if source == "~" or source.startswith("~/"):
        source = str(home / source[2:])
    elif source == "." or source.startswith(("./", "../")):
        source = os.path.normpath(cwd / source)
    return f"{source}:{rest}"


def named_volumes(volumes: Iterable[str]) -> list[str]:
    """Return volume names (as opposed to host paths) referenced by volume specs."""
    names = []
    for spec in volumes:
        source, sep, _ = spec.partition(":")
        if sep and not source.startswith(("/", ".", "~")):
            names.append(source)
    return names


def resolve_env(entries: Iterable[str], environ: Mapping[str, str]) -> dict[str, str]:
    """Turn `NAME=value` and bare `NAME` (taken from the host) entries into a dict."""
    env = {}
    for entry in entries:
        name, sep, value = entry.partition("=")
        if sep:
            env[name] = value
        elif name in environ:
            env[name] = environ[name]
        else:
            log.warning("Environment variable %s is not set on the host, not passing it on", name)
    return env


def build_compose(
    config: Config, *, squid_conf: Path, environ: Mapping[str, str], cwd: Path, home: Path
) -> dict[str, Any]:
    """Return the compose document as a dict."""
    agent = config.agent
    bad_keys = _PROTECTED_SERVICE_KEYS & agent.service.keys()
    if bad_keys:
        raise ConfigError(
            f"agent.service: {', '.join(sorted(bad_keys))} cannot be overridden, it would break isolation"
        )

    proxy_url = f"http://{PROXY_SERVICE}:{config.proxy.port}"
    proxy_env = {"HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url, "NO_PROXY": NO_PROXY}
    environment = {
        **proxy_env,
        **{k.lower(): v for k, v in proxy_env.items()},
        **resolve_env(agent.env, environ),
    }
    volumes = [resolve_volume(v, cwd=cwd, home=home) for v in agent.volumes]

    agent_service: dict[str, Any] = {
        "image": agent.image_ref,
        "environment": environment,
        "networks": ["internal"],
        "depends_on": [PROXY_SERVICE],
    }
    if agent.command:
        agent_service["command"] = list(agent.command)
    if agent.workdir:
        agent_service["working_dir"] = agent.workdir
    if volumes:
        agent_service["volumes"] = volumes
    agent_service.update(agent.service)

    compose: dict[str, Any] = {
        "services": {
            PROXY_SERVICE: {
                "image": config.proxy.image,
                "volumes": [f"{squid_conf}:{SQUID_CONF_PATH}:ro,Z"],
                "networks": ["internal", "egress"],
            },
            AGENT_SERVICE: agent_service,
        },
        "networks": {"internal": {"internal": True}, "egress": {}},
    }
    if names := named_volumes(volumes):
        # A fixed name keeps the volume stable across our randomly named projects
        compose["volumes"] = {name: {"name": name} for name in names}
    return compose
