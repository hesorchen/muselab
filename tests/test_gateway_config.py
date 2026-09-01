"""Portable Codex Gateway contract for effort, Ultra, and Fast controls."""
from pathlib import Path


ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "examples" / "cli-proxy-muselab.config.yaml"


def _rule_block(text: str, header: str, value: str) -> str:
    marker = f'{header}: "{value}"'
    start = text.index(marker)
    candidates = [
        boundary for boundary in (
            text.find("\n    - models:", start + len(marker)),
            text.find("\n  filter:", start + len(marker)),
        )
        if boundary >= 0
    ]
    end = min(candidates, default=len(text))
    return text[start:end]


def test_cli_proxy_example_maps_every_muselab_runtime_control():
    text = CONFIG.read_text(encoding="utf-8")

    for effort in ("low", "medium", "high", "xhigh", "max"):
        block = _rule_block(text, "X-MuseLab-Effort", effort)
        assert f'"reasoning.effort": "{effort}"' in block

    ultra = _rule_block(text, "X-MuseLab-Effort", "ultra")
    assert 'name: "gpt-5.6-terra"' in ultra
    assert '"reasoning.effort": "max"' in ultra

    thinking = _rule_block(text, "X-MuseLab-Thinking", "summarized")
    assert '"reasoning.summary": "auto"' in thinking

    fast = _rule_block(text, "X-MuseLab-Service-Tier", "fast")
    assert '"service_tier": "priority"' in fast

    auto = _rule_block(text, "X-MuseLab-Effort", "auto")
    assert text.index("\n  filter:") < text.index('X-MuseLab-Effort: "auto"')
    assert '- "reasoning.effort"' in auto


def test_gateway_docs_require_the_portable_payload_rules():
    for name in ("codex-gateway.md", "codex-gateway_zh.md"):
        text = (ROOT / "docs" / name).read_text(encoding="utf-8")
        assert "X-MuseLab-Effort" in text
        assert "X-MuseLab-Thinking" in text
        assert "X-MuseLab-Service-Tier" in text
        assert "reasoning.summary" in text
        assert "payload.override" in text
        assert "service_tier: priority" in text
