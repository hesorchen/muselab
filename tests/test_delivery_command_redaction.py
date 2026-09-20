"""Command previews must not persist synthetic credentials hidden by shell quoting."""
import pytest


@pytest.mark.parametrize('command, suffix', [
    ('PASSWORD="SYNTHETIC_A SYNTHETIC_B" tool --mode safe', 'tool --mode safe'),
    ("PASSWORD='SYNTHETIC_A SYNTHETIC_B' tool", 'tool'),
    ("tool --password 'SYNTHETIC_A SYNTHETIC_B' --mode safe", '--mode safe'),
    ('tool --password="SYNTHETIC_A SYNTHETIC_B" --mode safe', '--mode safe'),
    (r'PASSWORD=SYNTHETIC_A\ SYNTHETIC_B tool', 'tool'),
    ("PASSWORD=SYNTHETIC_A' SYNTHETIC_B 'SYNTHETIC_C tool", 'tool'),
    ('curl -H "Authorization: Bearer SYNTHETIC_A" https://example.test', 'https://example.test'),
    ("curl -H 'Authorization: Basic SYNTHETIC_A' https://example.test", 'https://example.test'),
    ("AUTHORIZATION='Bearer SYNTHETIC_A' tool", 'tool'),
])
def test_redaction_covers_complete_quoted_values(command, suffix):
    from backend.task_delivery import _safe_command
    result = _safe_command(command)
    assert 'SYNTHETIC_' not in result
    assert '[redacted]' in result
    assert result.endswith(suffix)


def test_redaction_covers_a_quoted_value_cut_at_preview_limit():
    from backend.task_delivery import _safe_command
    command = 'tool --password "' + 'SYNTHETIC_A SYNTHETIC_B ' * 150 + '"'
    result = _safe_command(command)
    assert 'SYNTHETIC_' not in result
    assert '[redacted]' in result


def test_plain_command_preview_retains_its_original_structure():
    from backend.task_delivery import _safe_command
    command = 'python3 -m pytest tests/test_example.py && echo "test complete"'
    assert _safe_command(command) == command


def test_command_evidence_persists_only_redacted_header(app_module, temp_root, monkeypatch):
    from backend import task_delivery
    monkeypatch.setattr(task_delivery.sess, 'SESS_DIR', temp_root / 'evidence-sessions')
    sid, turn = 'redaction-session', 'redaction-turn'
    task_delivery.begin(sid, turn, temp_root)
    task_delivery.record_tool(sid, turn, temp_root, 'PreToolUse', {
        'tool_name': 'Bash',
        'tool_input': {
            'command': 'curl -H "Authorization: Bearer SYNTHETIC_A" https://example.test',
        },
    }, 'header-tool')
    stored = task_delivery.path(sid).read_text(encoding='utf-8')
    assert 'SYNTHETIC_' not in stored
    command = task_delivery.report(sid, temp_root)['commands'][0]['command']
    assert 'SYNTHETIC_' not in command
    assert command.endswith('https://example.test')
