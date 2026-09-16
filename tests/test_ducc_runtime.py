import json
import os
import subprocess
from pathlib import Path

import pytest


def test_ducc_wrapper_removes_muselab_provider_identity(tmp_path):
    fake_ducc = tmp_path / "ducc"
    fake_ducc.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "names = [\n"
        "    'MUSELAB_TOKEN', 'ANTHROPIC_API_KEY',\n"
        "    'ANTHROPIC_CUSTOM_HEADERS', 'DEEPSEEK_API_KEY',\n"
        "    'CLAUDE_CODE_ENTRYPOINT', 'GITHUB_TOKEN',\n"
        "    'AWS_SECRET_ACCESS_KEY', 'DATABASE_URL', 'SSH_AUTH_SOCK',\n"
        "    'UNRELATED_PRIVATE_VALUE', 'MUSELAB_DUCC_CLI',\n"
        "    'HTTPS_PROXY', 'DUCC_AUTH_SOURCE', 'HOME',\n"
        "    'CLAUDE_CODE_RESUME_SOURCE_ALIVE',\n"
        "    'LC_ALL', 'LC_PRIVATE_SENTINEL',\n"
        "]\n"
        "print(json.dumps({\n"
        "    'present': {name: name in os.environ for name in names},\n"
        "    'resume_source_alive': "
        "os.environ.get('CLAUDE_CODE_RESUME_SOURCE_ALIVE'),\n"
        "}))\n",
        encoding="utf-8",
    )
    fake_ducc.chmod(0o755)
    wrapper = Path(__file__).resolve().parent.parent / "scripts" / "muselab-ducc"
    assert "if [[ -v " not in wrapper.read_text(encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "MUSELAB_DUCC_CLI": str(fake_ducc),
        "MUSELAB_TOKEN": "synthetic-muselab-secret",
        "ANTHROPIC_API_KEY": "synthetic-api-key",
        "ANTHROPIC_CUSTOM_HEADERS": "synthetic-static-header",
        "DEEPSEEK_API_KEY": "synthetic-provider-key",
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "GITHUB_TOKEN": "synthetic-github-token",
        "AWS_SECRET_ACCESS_KEY": "synthetic-cloud-secret",
        "DATABASE_URL": "postgres://synthetic-private-db",
        "SSH_AUTH_SOCK": "/tmp/synthetic-private-agent.sock",
        "UNRELATED_PRIVATE_VALUE": "synthetic-private-value",
        "HTTPS_PROXY": "https://user:password@proxy.invalid",
        "DUCC_AUTH_SOURCE": "synthetic-ducc-source",
        "CLAUDE_CODE_RESUME_SOURCE_ALIVE": "2026-08-14T09:21:38.123Z",
        "LC_ALL": "C.UTF-8",
        "LC_PRIVATE_SENTINEL": "private-locale-shaped-value",
    })

    completed = subprocess.run(
        [str(wrapper), "probe"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    output = json.loads(completed.stdout)
    present = output["present"]

    assert present == {
        "MUSELAB_TOKEN": False,
        "ANTHROPIC_API_KEY": False,
        "ANTHROPIC_CUSTOM_HEADERS": False,
        "DEEPSEEK_API_KEY": False,
        "CLAUDE_CODE_ENTRYPOINT": False,
        "GITHUB_TOKEN": False,
        "AWS_SECRET_ACCESS_KEY": False,
        "DATABASE_URL": False,
        "SSH_AUTH_SOCK": False,
        "UNRELATED_PRIVATE_VALUE": False,
        "MUSELAB_DUCC_CLI": False,
        "HTTPS_PROXY": False,
        "DUCC_AUTH_SOURCE": True,
        "HOME": True,
        "CLAUDE_CODE_RESUME_SOURCE_ALIVE": True,
        "LC_ALL": True,
        "LC_PRIVATE_SENTINEL": False,
    }
    assert output["resume_source_alive"] == "2026-08-14T09:21:38.123Z"

    env["CLAUDE_CODE_RESUME_SOURCE_ALIVE"] = (
        "private text that must not cross the DUCC boundary"
    )
    invalid = subprocess.run(
        [str(wrapper), "probe"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    invalid_output = json.loads(invalid.stdout)
    assert invalid_output["present"]["CLAUDE_CODE_RESUME_SOURCE_ALIVE"] is False
    assert invalid_output["resume_source_alive"] is None


@pytest.mark.parametrize("inherited_pwd", [None, "/", "/stale/workspace"])
@pytest.mark.parametrize("via_symlink", [False, True])
def test_ducc_wrapper_reports_actual_workspace(tmp_path, inherited_pwd, via_symlink):
    workspace = tmp_path / "workspace with spaces 工作区"
    workspace.mkdir()
    launch_cwd = workspace
    if via_symlink:
        launch_cwd = tmp_path / "workspace alias"
        launch_cwd.symlink_to(workspace, target_is_directory=True)
    fake_ducc = tmp_path / "ducc"
    fake_ducc.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess\n"
        "print(json.dumps({\n"
        "    'cwd': os.getcwd(),\n"
        "    'pwd': os.environ.get('PWD', '/'),\n"
        "    'shell': subprocess.check_output(\n"
        "        ['/bin/sh', '-c', 'pwd -P'], text=True).strip(),\n"
        "    'wrapper_metadata': 'MUSELAB_DUCC_WORKSPACE' in os.environ,\n"
        "}))\n",
        encoding="utf-8",
    )
    fake_ducc.chmod(0o755)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "MUSELAB_DUCC_CLI": str(fake_ducc),
        "MUSELAB_DUCC_WORKSPACE": str(workspace.resolve()),
    }
    if inherited_pwd is not None:
        env["PWD"] = inherited_pwd
    wrapper = Path(__file__).resolve().parent.parent / "scripts" / "muselab-ducc"
    completed = subprocess.run(
        [str(wrapper)], cwd=launch_cwd, env=env, check=True,
        capture_output=True, text=True,
    )
    assert json.loads(completed.stdout) == {
        "cwd": str(workspace.resolve()),
        "pwd": str(workspace.resolve()),
        "shell": str(workspace.resolve()),
        "wrapper_metadata": False,
    }
    assert completed.stderr == ""


def test_ducc_wrapper_rejects_wrong_workspace_before_launch(tmp_path):
    expected = tmp_path / "expected private workspace"
    expected.mkdir()
    actual = tmp_path / "wrong private workspace"
    actual.mkdir()
    fake_ducc = tmp_path / "ducc"
    fake_ducc.write_text("#!/bin/sh\nprintf 'runtime launched'\n", encoding="utf-8")
    fake_ducc.chmod(0o755)
    wrapper = Path(__file__).resolve().parent.parent / "scripts" / "muselab-ducc"
    completed = subprocess.run(
        [str(wrapper)], cwd=actual,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "MUSELAB_DUCC_CLI": str(fake_ducc),
            "MUSELAB_DUCC_WORKSPACE": str(expected.resolve()),
            "PWD": str(expected.resolve()),
        },
        capture_output=True, text=True,
    )
    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr == "MuseLab DUCC runtime: workspace cwd mismatch\n"
