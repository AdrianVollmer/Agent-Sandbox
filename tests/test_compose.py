from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_sandbox.compose import (
    AGENT_SERVICE,
    PROXY_SERVICE,
    SQUID_CONF_PATH,
    build_compose,
    resolve_env,
    resolve_volume,
)
from agent_sandbox.config import AgentConfig, Config, ConfigError, ProxyConfig

CWD = Path("/work/project")
HOME = Path("/home/u")


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (".:/workspace:Z", "/work/project:/workspace:Z"),
        ("./sub:/data", "/work/project/sub:/data"),
        ("~/.claude:/root/.claude", "/home/u/.claude:/root/.claude"),
        ("/abs:/abs:ro", "/abs:/abs:ro"),
        ("named:/data", "named:/data"),
        ("/anonymous", "/anonymous"),
    ],
)
def test_resolve_volume(spec: str, expected: str) -> None:
    assert resolve_volume(spec, cwd=CWD, home=HOME) == expected


def test_resolve_env_passes_literals_and_host_values() -> None:
    env = resolve_env(("FOO=bar", "EMPTY=", "TOKEN"), {"TOKEN": "secret"})
    assert env == {"FOO": "bar", "EMPTY": "", "TOKEN": "secret"}


def test_resolve_env_skips_unset_host_variables(caplog: pytest.LogCaptureFixture) -> None:
    assert resolve_env(("MISSING",), {}) == {}
    assert "MISSING" in caplog.text


BASE = Config(
    agent=AgentConfig(image="agent-img"),
    proxy=ProxyConfig(allowed_domains=(".example.com",), image="squid-img", port=3128),
)


def _config(**agent: Any) -> Config:
    return replace(BASE, agent=replace(BASE.agent, **agent))


def _build(config: Config) -> dict[str, Any]:
    return build_compose(
        config, squid_conf=Path("/tmp/x/squid.conf"), dynamic_dir=Path("/tmp/x/dynamic"), environ={}, cwd=CWD, home=HOME
    )


def test_compose_isolates_agent_on_internal_network() -> None:
    compose = _build(_config())
    agent = compose["services"][AGENT_SERVICE]
    proxy = compose["services"][PROXY_SERVICE]
    assert agent["networks"] == ["internal"]
    assert sorted(proxy["networks"]) == ["egress", "internal"]
    assert compose["networks"]["internal"]["internal"] is True
    assert agent["depends_on"] == [PROXY_SERVICE]
    assert proxy["image"] == "squid-img"
    assert proxy["volumes"][0] == f"/tmp/x/squid.conf:{SQUID_CONF_PATH}:ro,Z"


def test_compose_points_agent_at_proxy() -> None:
    env = _build(_config())["services"][AGENT_SERVICE]["environment"]
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert env[key] == "http://proxy:3128"
    assert "localhost" in env["NO_PROXY"]


def test_compose_user_env_overrides_proxy_env() -> None:
    env = _build(_config(env=("NO_PROXY=custom",)))["services"][AGENT_SERVICE]["environment"]
    assert env["NO_PROXY"] == "custom"


def test_compose_agent_options() -> None:
    config = _config(command=("claude",), workdir="/workspace", volumes=(".:/workspace", "cache:/cache"))
    compose = _build(config)
    agent = compose["services"][AGENT_SERVICE]
    assert agent["image"] == "agent-img"
    assert agent["command"] == ["claude"]
    assert agent["working_dir"] == "/workspace"
    assert agent["volumes"] == ["/work/project:/workspace", "cache:/cache"]
    # Named volumes get a fixed name so they persist across randomly named projects
    assert compose["volumes"] == {"cache": {"name": "cache"}}


def test_compose_merges_raw_service_keys() -> None:
    agent = _build(_config(service={"cap_drop": ["ALL"], "image": "override"}))["services"][AGENT_SERVICE]
    assert agent["cap_drop"] == ["ALL"]
    assert agent["image"] == "override"


@pytest.mark.parametrize("key", ["networks", "depends_on"])
def test_compose_rejects_service_keys_that_break_isolation(key: str) -> None:
    with pytest.raises(ConfigError, match=key):
        _build(_config(service={key: []}))


def test_compose_pins_proxy_hosts() -> None:
    compose = build_compose(
        BASE,
        squid_conf=Path("/tmp/x/squid.conf"),
        dynamic_dir=Path("/tmp/x/dynamic"),
        environ={},
        cwd=CWD,
        home=HOME,
        proxy_hosts={"ai.syss.intern": ["10.1.2.3", "10.1.2.4"]},
    )
    assert compose["services"][PROXY_SERVICE]["extra_hosts"] == ["ai.syss.intern:10.1.2.3", "ai.syss.intern:10.1.2.4"]
    # The agent never resolves anything itself, everything goes through the proxy
    assert "extra_hosts" not in compose["services"][AGENT_SERVICE]


def test_compose_without_proxy_hosts_has_no_extra_hosts() -> None:
    assert "extra_hosts" not in _build(BASE)["services"][PROXY_SERVICE]


def test_compose_mounts_dynamic_exceptions_read_only() -> None:
    # A directory, not single files: those are replaced atomically, which a file bind mount would not follow
    volumes = _build(_config())["services"][PROXY_SERVICE]["volumes"]
    assert "/tmp/x/dynamic:/etc/squid/agent-sandbox:ro,Z" in volumes
