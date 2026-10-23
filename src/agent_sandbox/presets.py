"""Named domain lists for common package registries.

The go, rust and mise lists were checked against a proxy access log while
installing toolchains and fetching dependencies.
"""

_PYPI = ("pypi.org", "files.pythonhosted.org")
_GITHUB_RELEASES = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")
_CARGO = ("crates.io", "index.crates.io", "static.crates.io")

PRESETS: dict[str, tuple[str, ...]] = {
    "debian": ("deb.debian.org", "security.debian.org"),
    # Leading dot covers country mirrors like de.archive.ubuntu.com
    "ubuntu": (".archive.ubuntu.com", "security.ubuntu.com", "ports.ubuntu.com"),
    "alpine": ("dl-cdn.alpinelinux.org",),
    "pypi": _PYPI,
    # uv fetches Python builds from GitHub releases (python-build-standalone)
    "uv": (*_PYPI, *_GITHUB_RELEASES, "astral.sh"),
    "npm": ("registry.npmjs.org", "registry.yarnpkg.com"),
    "cargo": _CARGO,
    # rustup installer, toolchains, components and targets, plus crates
    "rust": (*_CARGO, "sh.rustup.rs", "static.rust-lang.org"),
    # proxy.golang.org redirects module zips and GOTOOLCHAIN downloads to GCS.
    # Since HTTPS is only filtered by host, this opens all of storage.googleapis.com.
    "go": ("proxy.golang.org", "sum.golang.org", "storage.googleapis.com"),
    # Installer, version index, sigstore attestation checks, and the usual tool
    # sources: GitHub releases (python, aqua tools), nodejs.org, dl.google.com (go)
    "mise": (
        "mise.run",
        "mise.jdx.dev",
        "mise-versions.jdx.dev",
        "tuf-repo-cdn.sigstore.dev",
        *_GITHUB_RELEASES,
        "nodejs.org",
        "dl.google.com",
    ),
    # Required hosts from https://code.claude.com/docs/en/network-config, without
    # optional telemetry and storage.googleapis.com (plugin metadata)
    "claude-code": (
        "api.anthropic.com",
        "mcp-proxy.anthropic.com",
        "claude.ai",
        "downloads.claude.ai",
        "claude.com",
        "platform.claude.com",
        "sso.claude.com",
        "code.claude.com",
    ),
    "github": ("github.com", "api.github.com", "codeload.github.com", ".githubusercontent.com"),
}
