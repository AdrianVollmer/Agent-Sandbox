from collections.abc import Callable
from pathlib import Path

import pytest

MINIMAL_CONFIG = """
[agent]
image = "localhost/agent:latest"

[proxy]
allowed_domains = [".anthropic.com"]
"""


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[str], Path]:
    def _write(content: str = MINIMAL_CONFIG) -> Path:
        path = tmp_path / "config.toml"
        path.write_text(content)
        return path

    return _write
