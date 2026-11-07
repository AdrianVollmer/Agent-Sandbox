# Agent image with Claude Code, Codex, Hermes, Pi (pi.dev), OpenCode and Maki,
# plus uv, just, mise and toolchains for JavaScript/TypeScript, Rust, Go and C/C++.
#
# Reference it from the config (relative to the config file's directory):
#
#   [agent]
#   build_file = "Dockerfile"
#   command = ["bash"]
#   workdir = "/workspace"
#   env = ["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"]
#
#   [proxy]
#   presets = ["claude-code", "debian", "npm", "uv", "rust", "go", "mise", "github"]
#   allowed_domains = ["api.openai.com"]  # plus whatever your model providers need
#
# The tool builds the image with an empty build context, which is fine since
# this file does not COPY local files.
#
# Everything runs as root. With rootless podman, container root is your host
# user, so files in the workspace stay yours. With rootful docker they
# end up owned by root on the host.

# Node >= 22.19 is required by Pi, Debian trixie only ships 20
FROM docker.io/library/node:24-trixie-slim AS node

FROM docker.io/library/debian:trixie-slim

# Fail on errors anywhere in a pipe, e.g. a failed download in `curl | sh`
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV LANG=C.UTF-8 \
    DEBIAN_FRONTEND=noninteractive

# Base tools, C/C++ toolchain, Go. Unpinned on purpose: this image should
# track current releases, rebuild to update.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash-completion ca-certificates curl wget git openssh-client gnupg \
        less nano procps file unzip xz-utils zip jq ripgrep fd-find tree just \
        python3 python3-venv python3-pip \
        build-essential pkg-config libssl-dev libatomic1 \
        clang clangd clang-format clang-tidy lldb lld \
        cmake ninja-build meson gdb valgrind ccache \
        golang-go \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

# Node.js and npm from the official image, yarn/pnpm via corepack
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
    && corepack enable \
    && node --version && npm --version

# uv and uvx from the official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Agents distributed via npm. pi.dev recommends --ignore-scripts, Codex works
# without them too.
# hadolint ignore=DL3016
RUN npm install -g --ignore-scripts @openai/codex @earendil-works/pi-coding-agent \
    && npm cache clean --force

# IS_SANDBOX=1 (undocumented) lets Claude Code use --dangerously-skip-permissions
# as root. Updates happen by rebuilding the image, hence DISABLE_AUTOUPDATER.
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/root/.local/share/mise/shims:/root/.local/bin:/root/.opencode/bin:/usr/local/cargo/bin:/root/go/bin:$PATH \
    GOTOOLCHAIN=auto \
    IS_SANDBOX=1 \
    DISABLE_AUTOUPDATER=1

# Rust via rustup (default profile includes clippy and rustfmt)
RUN curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs \
        | sh -s -- -y --no-modify-path --profile default \
    && rustup component add rust-analyzer \
    && cargo --version

# mise for per-project tool versions. Interactive shells get `mise activate`,
# agents running non-interactive commands use the shims on PATH (see above).
# Single quotes are intended, .bashrc must get the unexpanded command.
# hadolint ignore=SC2016
RUN curl -fsSL https://mise.run | MISE_INSTALL_PATH=/usr/local/bin/mise sh \
    && echo 'eval "$(mise activate bash)"' >> /root/.bashrc \
    && mise --version

# Claude Code, native installer (the npm package needs a blocked postinstall)
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && claude --version

# Hermes Agent, installs into ~/.hermes; run `hermes setup` once inside
RUN curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh \
        | bash -s -- --non-interactive \
    && command -v hermes

# OpenCode 2, installs into ~/.opencode/bin. The npm package needs a blocked
# postinstall.
RUN curl -fsSL https://opencode.ai/v2/install | bash -s -- --no-modify-path \
    && opencode --version

# Maki, prebuilt binary from GitHub releases (the installer does not verify a
# checksum)
RUN curl -fsSL https://maki.sh/install.sh | MAKI_INSTALL_DIR=/usr/local/bin sh \
    && command -v maki

WORKDIR /workspace
CMD ["bash"]
