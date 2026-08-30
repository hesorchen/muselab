from __future__ import annotations

import re
from pathlib import Path

from claude_agent_sdk._cli_version import __cli_version__ as bundled_cli_version
from claude_agent_sdk._version import __version__ as sdk_version


ROOT = Path(__file__).resolve().parents[1]


def _required_match(pattern: str, text: str, source: str) -> re.Match[str]:
    match = re.search(pattern, text, flags=re.DOTALL | re.MULTILINE)
    assert match is not None, f"version baseline missing from {source}"
    return match


def test_native_and_docker_cli_pins_match_bundled_cli():
    versions = (ROOT / "scripts" / "versions.env").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    native_pin = _required_match(
        r'^CLAUDE_CLI_VERSION="([^"]+)"$', versions, "scripts/versions.env"
    ).group(1)
    docker_pin = _required_match(
        r"@anthropic-ai/claude-code@([0-9.]+)", dockerfile, "Dockerfile"
    ).group(1)

    assert native_pin == docker_pin == bundled_cli_version


def test_gateway_docs_share_the_installed_sdk_cli_baseline():
    english = (ROOT / "docs" / "codex-gateway.md").read_text(encoding="utf-8")
    chinese = (ROOT / "docs" / "codex-gateway_zh.md").read_text(encoding="utf-8")

    english_baseline = _required_match(
        r"tested on ([0-9-]+) is CLIProxyAPI `v([^`]+)`,\s*"
        r"Claude Agent SDK `([^`]+)`, and its bundled Claude CLI `([^`]+)`",
        english,
        "docs/codex-gateway.md",
    ).groups()
    chinese_baseline = _required_match(
        r"截至 ([0-9-]+)，已验证的兼容基线是 CLIProxyAPI `v([^`]+)`、"
        r"Claude Agent\s*SDK `([^`]+)` 以及其内置 Claude CLI `([^`]+)`",
        chinese,
        "docs/codex-gateway_zh.md",
    ).groups()

    assert english_baseline == chinese_baseline
    assert english_baseline[2:] == (sdk_version, bundled_cli_version)
