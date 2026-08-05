import json
import os
import subprocess
from pathlib import Path


def test_ducc_wrapper_removes_muselab_provider_identity(tmp_path):
    fake_ducc = tmp_path / "ducc"
    fake_ducc.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "names = [\n"
        "    'MUSELAB_TOKEN', 'ANTHROPIC_API_KEY',\n"
        "    'ANTHROPIC_CUSTOM_HEADERS', 'DEEPSEEK_API_KEY',\n"
        "    'CLAUDE_CODE_ENTRYPOINT', 'DUCC_AUTH_SOURCE', 'HOME',\n"
        "]\n"
        "print(json.dumps({name: name in os.environ for name in names}))\n",
        encoding="utf-8",
    )
    fake_ducc.chmod(0o755)
    wrapper = Path(__file__).resolve().parent.parent / "scripts" / "muselab-ducc"
    env = dict(os.environ)
    env.update({
        "MUSELAB_DUCC_CLI": str(fake_ducc),
        "MUSELAB_TOKEN": "synthetic-muselab-secret",
        "ANTHROPIC_API_KEY": "synthetic-api-key",
        "ANTHROPIC_CUSTOM_HEADERS": "synthetic-static-header",
        "DEEPSEEK_API_KEY": "synthetic-provider-key",
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "DUCC_AUTH_SOURCE": "synthetic-ducc-source",
    })

    completed = subprocess.run(
        [str(wrapper), "probe"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    present = json.loads(completed.stdout)

    assert present == {
        "MUSELAB_TOKEN": False,
        "ANTHROPIC_API_KEY": False,
        "ANTHROPIC_CUSTOM_HEADERS": False,
        "DEEPSEEK_API_KEY": False,
        "CLAUDE_CODE_ENTRYPOINT": False,
        "DUCC_AUTH_SOURCE": True,
        "HOME": True,
    }
