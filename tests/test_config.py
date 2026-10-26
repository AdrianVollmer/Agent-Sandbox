from collections.abc import Callable
from pathlib import Path

import pytest

from agent_sandbox.config import (
    DEFAULT_PROXY_IMAGE,
    ConfigError,
    config_search_paths,
    find_config,
    load_config,
    state_dir,
)


def test_search_paths_use_xdg_config_home() -> None:
    paths = config_search_paths({"XDG_CONFIG_HOME": "/xdg", "HOME": "/home/u"})
    assert paths == [Path("/xdg/agent-sandbox/config.toml"), Path("/etc/agent-sandbox/config.toml")]


def test_search_paths_fall_back_to_home_config() -> None:
    paths = config_search_paths({"HOME": "/home/u"})
    assert paths[0] == Path("/home/u/.config/agent-sandbox/config.toml")


def test_search_paths_ignore_relative_xdg_config_home() -> None:
    # The XDG spec says relative paths are invalid and must be ignored
    paths = config_search_paths({"XDG_CONFIG_HOME": "relative", "HOME": "/home/u"})
    assert paths[0] == Path("/home/u/.config/agent-sandbox/config.toml")


def test_find_config_prefers_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user = tmp_path / "user.toml"
    system = tmp_path / "system.toml"
    user.touch()
    system.touch()
    monkeypatch.setattr("agent_sandbox.config.config_search_paths", lambda _env: [user, system])
    assert find_config({}) == user


def test_find_config_falls_back_to_system(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    system = tmp_path / "system.toml"
    system.touch()
    monkeypatch.setattr("agent_sandbox.config.config_search_paths", lambda _env: [tmp_path / "missing", system])
    assert find_config({}) == system


def test_find_config_raises_when_nothing_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_sandbox.config.config_search_paths", lambda _env: [tmp_path / "missing"])
    with pytest.raises(ConfigError, match="No config file found"):
        find_config({})


def test_load_minimal_config(write_config: Callable[..., Path]) -> None:
    config = load_config(write_config())
    assert config.engine is None
    assert config.agent.image == "localhost/agent:latest"
    assert config.agent.command == ()
    assert config.proxy.image == DEFAULT_PROXY_IMAGE
    assert config.proxy.port == 3128
    assert config.proxy.allowed_domains == (".anthropic.com",)


def test_load_full_config(write_config: Callable[..., Path]) -> None:
    config = load_config(
        write_config(
            """
            engine = "docker"

            [agent]
            image = "img"
            command = ["claude", "--help"]
            workdir = "/workspace"
            volumes = [".:/workspace:Z"]
            env = ["FOO=bar", "ANTHROPIC_API_KEY"]
            run_args = ["--user", "1000"]

            [agent.service]
            cap_drop = ["ALL"]

            [proxy]
            image = "squid"
            port = 8080
            allowed_domains = ["github.com"]
            """
        )
    )
    assert config.engine == "docker"
    assert config.agent.command == ("claude", "--help")
    assert config.agent.workdir == "/workspace"
    assert config.agent.volumes == (".:/workspace:Z",)
    assert config.agent.env == ("FOO=bar", "ANTHROPIC_API_KEY")
    assert config.agent.run_args == ("--user", "1000")
    assert config.agent.service == {"cap_drop": ["ALL"]}
    assert config.proxy.image == "squid"
    assert config.proxy.port == 8080


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[proxy]\nallowed_domains = []", r"agent: missing section"),
        ("[agent]\n[proxy]\nallowed_domains = []", r"agent: set exactly one of"),
        ("[agent]\nimage = 1\n[proxy]\nallowed_domains = []", r"agent\.image: expected a string"),
        ('[agent]\nimage = "i"\nvolumes = "x"\n[proxy]\nallowed_domains = []', r"agent\.volumes: expected a list"),
        ('[agent]\nimage = "i"\nimgae = "typo"\n[proxy]\nallowed_domains = []', r"agent: unknown key 'imgae'"),
        ('engine = "lxc"\n[agent]\nimage = "i"\n[proxy]\nallowed_domains = []', r"engine: must be one of"),
        ('[agent]\nimage = "i"\n[proxy]\nallowed_domains = ["a.b"]\nport = 0', r"proxy\.port: must be between"),
        ('[agent]\nimage = "i"\n[proxy]\nallowed_domains = ["a.b"]\nport = true', r"proxy\.port: expected an integer"),
        ('[agent]\nimage = "i"\n[proxy]\nallowed_domains = ["bad domain"]', r"invalid domain 'bad domain'"),
        ('[agent]\nimage = "i"\n[proxy]\nallowed_domains = []', r"proxy\.allowed_domains: must not be empty"),
        ('[agent]\nimage = "i"\nenv = ["=x"]\n[proxy]\nallowed_domains = ["a.b"]', r"agent\.env: invalid entry"),
        ('[agent]\nimage = "i"', r"proxy: missing section"),
        ("not toml [", r"invalid TOML"),
    ],
)
def test_invalid_configs(write_config: Callable[..., Path], content: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(content))


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Cannot read"):
        load_config(tmp_path / "nope.toml")


def test_presets_expand_to_domains(write_config: Callable[..., Path]) -> None:
    config = load_config(
        write_config('[agent]\nimage = "i"\n[proxy]\npresets = ["pypi"]\nallowed_domains = ["example.com"]')
    )
    assert config.proxy.presets == ("pypi",)
    assert config.proxy.domains() == ("pypi.org", "files.pythonhosted.org", "example.com")


def test_presets_alone_are_enough(write_config: Callable[..., Path]) -> None:
    config = load_config(write_config('[agent]\nimage = "i"\n[proxy]\npresets = ["debian"]'))
    assert "deb.debian.org" in config.proxy.domains()


def test_unknown_preset(write_config: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError, match=r"proxy\.presets: unknown preset 'debain' \(available: .*debian"):
        load_config(write_config('[agent]\nimage = "i"\n[proxy]\npresets = ["debain"]'))


BUILD_CONFIG = '[agent]\nbuild = """\nFROM debian\nRUN true\n"""\n[proxy]\npresets = ["debian"]'


def test_build_instead_of_image(write_config: Callable[..., Path]) -> None:
    agent = load_config(write_config(BUILD_CONFIG)).agent
    assert agent.image is None
    assert agent.build == "FROM debian\nRUN true\n"
    # Tag is derived from the Dockerfile, so a changed Dockerfile gets a fresh build
    assert agent.image_ref.startswith("localhost/agent-sandbox:")
    other = load_config(write_config(BUILD_CONFIG.replace("RUN true", "RUN false"))).agent
    assert agent.image_ref != other.image_ref
    assert agent.image_ref == load_config(write_config(BUILD_CONFIG)).agent.image_ref


def test_image_ref_is_image_when_set(write_config: Callable[..., Path]) -> None:
    assert load_config(write_config()).agent.image_ref == "localhost/agent:latest"


def test_build_file_relative_to_config_dir(tmp_path: Path, write_config: Callable[..., Path]) -> None:
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "Dockerfile").write_text("FROM debian\n")
    inline = load_config(write_config('[agent]\nbuild = "FROM debian\\n"\n[proxy]\npresets = ["debian"]')).agent
    agent = load_config(write_config('[agent]\nbuild_file = "images/Dockerfile"\n[proxy]\npresets = ["debian"]')).agent
    assert agent.build == "FROM debian\n"
    assert agent.build_file == tmp_path / "images" / "Dockerfile"
    # Same content as inline, same image
    assert agent.image_ref == inline.image_ref


def test_build_file_expands_home(
    tmp_path: Path, write_config: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Dockerfile").write_text("FROM x\n")
    agent = load_config(write_config('[agent]\nbuild_file = "~/Dockerfile"\n[proxy]\npresets = ["debian"]')).agent
    assert agent.build == "FROM x\n"


def test_build_file_missing(write_config: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError, match=r"agent\.build_file: cannot read .*nope"):
        load_config(write_config('[agent]\nbuild_file = "nope"\n[proxy]\npresets = ["debian"]'))


@pytest.mark.parametrize(
    "agent",
    ['image = "i"\nbuild = "FROM x"', "", 'build = "FROM x"\nbuild_file = "Dockerfile"'],
)
def test_exactly_one_image_source(write_config: Callable[..., Path], agent: str) -> None:
    with pytest.raises(ConfigError, match=r"agent: set exactly one of 'image', 'build' and 'build_file'"):
        load_config(write_config(f'[agent]\n{agent}\n[proxy]\npresets = ["debian"]'))


def test_state_dir_uses_xdg_state_home() -> None:
    assert state_dir({"XDG_STATE_HOME": "/xdg", "HOME": "/home/u"}) == Path("/xdg/agent-sandbox")


def test_state_dir_falls_back_to_home() -> None:
    assert state_dir({"XDG_STATE_HOME": "relative", "HOME": "/home/u"}) == Path("/home/u/.local/state/agent-sandbox")


def test_allowed_ips_and_ports(write_config: Callable[..., Path]) -> None:
    proxy = load_config(
        write_config(
            '[agent]\nimage = "i"\n[proxy]\n'
            'allowed_ips = ["192.168.178.10", "10.0.0.0/8", "fd00::/8"]\nallowed_ports = [11434, 8080]'
        )
    ).proxy
    # IPs alone are enough, no domains needed
    assert proxy.allowed_ips == ("192.168.178.10", "10.0.0.0/8", "fd00::/8")
    assert proxy.allowed_ports == (11434, 8080)


@pytest.mark.parametrize(
    ("proxy", "message"),
    [
        ('allowed_ips = ["192.168.1.300"]', r"proxy\.allowed_ips: invalid IP address or network '192\.168\.1\.300'"),
        # Host bits set, almost certainly a typo for 10.0.0.0/8 or 10.1.2.3
        ('allowed_ips = ["10.1.2.3/8"]', r"proxy\.allowed_ips: invalid IP address or network '10\.1\.2\.3/8'"),
        ('allowed_ips = ["example.com"]', r"proxy\.allowed_ips: invalid IP address"),
        ('presets = ["debian"]\nallowed_ports = [0]', r"proxy\.allowed_ports: must be between 1 and 65535, got 0"),
        ('presets = ["debian"]\nallowed_ports = ["80"]', r"proxy\.allowed_ports: expected a list of integers"),
        ('presets = ["debian"]\nallowed_ports = [true]', r"proxy\.allowed_ports: expected a list of integers"),
        ("allowed_ports = [8080]", r"must not be empty without presets or allowed_ips"),
    ],
)
def test_invalid_ips_and_ports(write_config: Callable[..., Path], proxy: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(f'[agent]\nimage = "i"\n[proxy]\n{proxy}'))


def test_host_dns(write_config: Callable[..., Path]) -> None:
    assert load_config(write_config()).proxy.host_dns is False
    config = write_config('[agent]\nimage = "i"\n[proxy]\npresets = ["debian"]\nhost_dns = true')
    assert load_config(config).proxy.host_dns is True


def test_host_dns_must_be_bool(write_config: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError, match=r"proxy\.host_dns: expected a boolean"):
        load_config(write_config('[agent]\nimage = "i"\n[proxy]\npresets = ["debian"]\nhost_dns = "yes"'))
