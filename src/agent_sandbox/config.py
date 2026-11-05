"""Loading and validating the TOML configuration."""

import hashlib
import ipaddress
import logging
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_sandbox.presets import PRESETS

log = logging.getLogger(__name__)

APP_NAME = "agent-sandbox"
CONFIG_FILENAME = "config.toml"
SYSTEM_CONFIG_DIR = Path("/etc")
DEFAULT_PROXY_IMAGE = "docker.io/ubuntu/squid:latest"
BUILD_IMAGE_REPO = "localhost/agent-sandbox"
ENGINES = ("podman", "docker")

_DOMAIN_RE = re.compile(r"^\.?([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]*[a-z0-9])?$", re.IGNORECASE)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigError(Exception):
    """Raised for missing or invalid configuration."""


def is_valid_domain(domain: str) -> bool:
    """Return whether `domain` is a host name, optionally with a leading dot for subdomains."""
    return bool(_DOMAIN_RE.match(domain))


def parse_network(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """Parse an IP address or network, raising ValueError for invalid ones.

    Strict, since host bits set in a network (10.1.2.3/8) are most likely a typo.
    """
    return ipaddress.ip_network(value, strict=True)


@dataclass(frozen=True)
class ProxyConfig:
    """Settings for the Squid sidecar that is the agent's only way out."""

    allowed_domains: tuple[str, ...] = ()
    presets: tuple[str, ...] = ()
    allowed_ips: tuple[str, ...] = ()
    # Destination ports allowed in addition to 80 and 443
    allowed_ports: tuple[int, ...] = ()
    # Resolve allowed names on the host and pin them in the proxy's /etc/hosts
    host_dns: bool = False
    image: str = DEFAULT_PROXY_IMAGE
    port: int = 3128

    def domains(self) -> tuple[str, ...]:
        """Return the domains from all presets followed by the explicit ones."""
        return (*(d for name in self.presets for d in PRESETS[name]), *self.allowed_domains)


@dataclass(frozen=True)
class AgentConfig:
    """Settings for the container the agent runs in."""

    image: str | None = None
    build: str | None = None
    build_file: Path | None = None
    command: tuple[str, ...] = ()
    workdir: str | None = None
    volumes: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    run_args: tuple[str, ...] = ()
    service: Mapping[str, Any] = field(default_factory=dict)

    @property
    def image_ref(self) -> str:
        """Return the image to run, a content-addressed tag when built from `build`."""
        if self.image:
            return self.image
        assert self.build is not None  # noqa: S101 - guaranteed by _parse_agent
        digest = hashlib.sha256(self.build.encode()).hexdigest()[:12]
        return f"{BUILD_IMAGE_REPO}:{digest}"


@dataclass(frozen=True)
class Config:
    """Top-level configuration."""

    agent: AgentConfig
    proxy: ProxyConfig
    engine: str | None = None


def _xdg_dir(environ: Mapping[str, str], variable: str, default: str) -> Path:
    """Resolve an XDG base directory, falling back to `default` below $HOME."""
    value = environ.get(variable, "")
    # Per the XDG spec, relative paths are invalid and must be ignored
    if value and Path(value).is_absolute():
        return Path(value)
    return Path(environ.get("HOME", "~")) / default


def config_search_paths(environ: Mapping[str, str]) -> list[Path]:
    """Return candidate config files, highest priority first."""
    user_dir = _xdg_dir(environ, "XDG_CONFIG_HOME", ".config")
    return [user_dir / APP_NAME / CONFIG_FILENAME, SYSTEM_CONFIG_DIR / APP_NAME / CONFIG_FILENAME]


def state_dir(environ: Mapping[str, str]) -> Path:
    """Return the directory for logs and other state."""
    return _xdg_dir(environ, "XDG_STATE_HOME", ".local/state") / APP_NAME


def find_config(environ: Mapping[str, str]) -> Path:
    """Return the first existing config file."""
    candidates = config_search_paths(environ)
    for path in candidates:
        if path.is_file():
            log.debug("Using config file %s", path)
            return path
        log.debug("No config file at %s", path)
    searched = ", ".join(str(p) for p in candidates)
    raise ConfigError(f"No config file found (searched: {searched})")


def load_config(path: Path) -> Config:
    """Read and validate a config file."""
    try:
        text = path.read_text()
    except OSError as e:
        raise ConfigError(f"Cannot read {path}: {e}") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e
    return parse_config(data, base_dir=path.parent)


def parse_config(data: Mapping[str, Any], base_dir: Path | None = None) -> Config:
    """Build a Config from parsed TOML data.

    Relative paths in the config, such as `agent.build_file`, are resolved
    against `base_dir`, normally the directory of the config file.
    """
    _reject_unknown_keys(data, "", {"engine", "agent", "proxy"})
    engine = _optional(data, "engine", str, "")
    if engine is not None and engine not in ENGINES:
        raise ConfigError(f"engine: must be one of {', '.join(ENGINES)}, got {engine!r}")
    return Config(
        agent=_parse_agent(_section(data, "agent"), base_dir or Path.cwd()),
        proxy=_parse_proxy(_section(data, "proxy")),
        engine=engine,
    )


def _parse_agent(data: Mapping[str, Any], base_dir: Path) -> AgentConfig:
    prefix = "agent."
    _reject_unknown_keys(
        data, "agent", {"image", "build", "build_file", "command", "workdir", "volumes", "env", "run_args", "service"}
    )
    env = _str_list(data, "env", prefix)
    for entry in env:
        if not _ENV_NAME_RE.match(entry.partition("=")[0]):
            raise ConfigError(f"agent.env: invalid entry {entry!r}, expected NAME or NAME=value")
    image = _optional(data, "image", str, prefix)
    build = _optional(data, "build", str, prefix)
    build_file_value = _optional(data, "build_file", str, prefix)
    if sum(bool(v) for v in (image, build, build_file_value)) != 1:
        raise ConfigError(
            "agent: set exactly one of 'image', 'build' and 'build_file' (inline Dockerfile or path to one)"
        )
    build_file = None
    if build_file_value:
        build_file = base_dir / Path(build_file_value).expanduser()
        try:
            build = build_file.read_text()
        except OSError as e:
            raise ConfigError(f"agent.build_file: cannot read {build_file}: {e}") from e
        log.debug("Read Dockerfile from %s", build_file)
    return AgentConfig(
        image=image,
        build=build,
        build_file=build_file,
        command=_str_list(data, "command", prefix),
        workdir=_optional(data, "workdir", str, prefix),
        volumes=_str_list(data, "volumes", prefix),
        env=env,
        run_args=_str_list(data, "run_args", prefix),
        service=_optional(data, "service", dict, prefix) or {},
    )


def _parse_proxy(data: Mapping[str, Any]) -> ProxyConfig:
    prefix = "proxy."
    _reject_unknown_keys(
        data, "proxy", {"image", "port", "allowed_domains", "presets", "allowed_ips", "allowed_ports", "host_dns"}
    )
    domains = _str_list(data, "allowed_domains", prefix)
    presets = _str_list(data, "presets", prefix)
    ips = _str_list(data, "allowed_ips", prefix)
    for preset in presets:
        if preset not in PRESETS:
            raise ConfigError(f"proxy.presets: unknown preset {preset!r} (available: {', '.join(sorted(PRESETS))})")
    if not domains and not presets and not ips:
        raise ConfigError(
            "proxy.allowed_domains: must not be empty without presets or allowed_ips, "
            "the agent would have no network access"
        )
    for domain in domains:
        if not is_valid_domain(domain):
            raise ConfigError(f"proxy.allowed_domains: invalid domain {domain!r}")
    for ip in ips:
        try:
            parse_network(ip)
        except ValueError as e:
            raise ConfigError(f"proxy.allowed_ips: invalid IP address or network {ip!r}") from e
    allowed_ports = _int_list(data, "allowed_ports", prefix)
    for allowed_port in allowed_ports:
        _check_port(allowed_port, f"{prefix}allowed_ports")
    port = _optional(data, "port", int, prefix)
    if port is not None:
        _check_port(port, f"{prefix}port")
    return ProxyConfig(
        allowed_domains=domains,
        presets=presets,
        allowed_ips=ips,
        allowed_ports=allowed_ports,
        host_dns=bool(_optional(data, "host_dns", bool, prefix)),
        image=_optional(data, "image", str, prefix) or DEFAULT_PROXY_IMAGE,
        port=port or ProxyConfig.port,
    )


_TYPE_NAMES: dict[type, str] = {bool: "a boolean", str: "a string", int: "an integer", dict: "a table", list: "a list"}


def _optional[T](data: Mapping[str, Any], key: str, kind: type[T], prefix: str) -> T | None:
    value = data.get(key)
    if value is None:
        return None
    # bool is a subclass of int, but `port = true` is a mistake
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ConfigError(f"{prefix}{key}: expected {_TYPE_NAMES[kind]}, got {type(value).__name__}")
    return value


def _str_list(data: Mapping[str, Any], key: str, prefix: str) -> tuple[str, ...]:
    value = _optional(data, key, list, prefix) or []
    if not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{prefix}{key}: expected a list of strings")
    return tuple(value)


def _int_list(data: Mapping[str, Any], key: str, prefix: str) -> tuple[int, ...]:
    value = _optional(data, key, list, prefix) or []
    # bool is a subclass of int, but `true` is not a port
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in value):
        raise ConfigError(f"{prefix}{key}: expected a list of integers")
    return tuple(value)


def _check_port(port: int, label: str) -> None:
    if not 1 <= port <= 65535:
        raise ConfigError(f"{label}: must be between 1 and 65535, got {port}")


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    if name not in data:
        raise ConfigError(f"{name}: missing section [{name}]")
    section = _optional(data, name, dict, "")
    assert section is not None  # noqa: S101 - presence checked above
    return section


def _reject_unknown_keys(data: Mapping[str, Any], where: str, allowed: set[str]) -> None:
    for key in data:
        if key not in allowed:
            label = where or "config"
            raise ConfigError(f"{label}: unknown key {key!r} (allowed: {', '.join(sorted(allowed))})")
