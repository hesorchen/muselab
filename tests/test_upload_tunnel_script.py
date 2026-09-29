"""Execute the deployment helper with simulated SSH, never a real network."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ssh-upload-tunnel.sh"


@pytest.fixture
def tunnel_env(tmp_path):
    commands = tmp_path / "commands.jsonl"
    ssh = tmp_path / "ssh"
    ssh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['FAKE_COMMANDS'], 'a', encoding='utf-8') as log:\n"
        "    log.write(json.dumps({'command': 'ssh', 'argv': sys.argv[1:], 'pid': os.getpid()}) + '\\n')\n"
        "if '-T' in sys.argv:\n"
        "    print(os.environ.get('FAKE_ADDRESS', '192.0.2.10 192.0.2.11'))\n"
        "    sys.exit(int(os.environ.get('FAKE_DISCOVERY_EXIT', '0')))\n",
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    timeout = tmp_path / "timeout"
    timeout.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['FAKE_COMMANDS'], 'a', encoding='utf-8') as log:\n"
        "    log.write(json.dumps({'command': 'timeout', 'argv': sys.argv[1:]}) + '\\n')\n"
        "os.execvp(sys.argv[3], sys.argv[3:])\n",
        encoding="utf-8",
    )
    timeout.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("MUSELAB_UPLOAD_")}
    env.update(PATH=str(tmp_path) + os.pathsep + env.get("PATH", ""),
               FAKE_COMMANDS=str(commands), MUSELAB_UPLOAD_SSH_HOST="app-host")
    return env, commands


def run_tunnel(env):
    process = subprocess.Popen(["bash", str(SCRIPT)], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = process.communicate(timeout=5)
    return process, stdout, stderr


def read_commands(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_upload_tunnel_execs_a_separate_loopback_connection(tunnel_env):
    env, commands = tunnel_env
    env.update(MUSELAB_UPLOAD_SSH_USER="test account",
               MUSELAB_UPLOAD_SSH_IDENTITY_FILE="/test/key with spaces")
    process, _, stderr = run_tunnel(env)
    assert process.returncode == 0, stderr
    record, = read_commands(commands)
    assert record["pid"] == process.pid
    args = record["argv"]
    assert args[0] == "-NT"
    assert args[args.index("-L") + 1] == "127.0.0.1:8785:127.0.0.1:8765"
    assert "ControlMaster=no" in args and "ControlPath=none" in args
    assert "StrictHostKeyChecking=yes" in args and "ExitOnForwardFailure=yes" in args
    assert args[args.index("-l") + 1] == "test account"
    assert args[args.index("-i") + 1] == "/test/key with spaces"


def test_target_discovery_is_bounded_and_uses_one_remote_command(tunnel_env):
    env, commands = tunnel_env
    command = "wsl.exe -d Ubuntu -- hostname -I"
    env.update(MUSELAB_UPLOAD_TARGET_COMMAND=command, FAKE_ADDRESS="192.0.2.10 192.0.2.11\r\n",
               MUSELAB_UPLOAD_LISTEN_PORT="18785", MUSELAB_UPLOAD_TARGET_PORT="9876")
    process, _, stderr = run_tunnel(env)
    assert process.returncode == 0, stderr
    timeout, discovery, forward = read_commands(commands)
    assert timeout["argv"][:2] == ["--kill-after=5s", "30s"]
    assert discovery["argv"][-2:] == ["app-host", command]
    assert "ControlPath=none" in discovery["argv"]
    assert forward["argv"][forward["argv"].index("-L") + 1] == "127.0.0.1:18785:192.0.2.10:9876"


@pytest.mark.parametrize("address", ["", "not-an-ip", "192.0.2.256", "192.0.2.1:80"])
def test_invalid_discovery_never_starts_a_listener(tunnel_env, address):
    env, commands = tunnel_env
    env.update(MUSELAB_UPLOAD_TARGET_COMMAND="discover-ip", FAKE_ADDRESS=address)
    process, _, _ = run_tunnel(env)
    assert process.returncode != 0
    assert not any("-NT" in c["argv"] for c in read_commands(commands))


def test_failed_discovery_never_starts_a_listener(tunnel_env):
    env, commands = tunnel_env
    env.update(MUSELAB_UPLOAD_TARGET_COMMAND="discover-ip", FAKE_DISCOVERY_EXIT="255")
    process, _, stderr = run_tunnel(env)
    assert process.returncode != 0
    assert "Could not discover" in stderr
    assert not any("-NT" in c["argv"] for c in read_commands(commands))


@pytest.mark.parametrize("name,value", [
    ("MUSELAB_UPLOAD_LISTEN_PORT", "0"),
    ("MUSELAB_UPLOAD_LISTEN_PORT", "65536"),
    ("MUSELAB_UPLOAD_TARGET_PORT", "-1"),
    ("MUSELAB_UPLOAD_TARGET_PORT", "bad-port"),
    ("MUSELAB_UPLOAD_SSH_HOST", "-unexpected-option"),
])
def test_invalid_configuration_never_runs_ssh(tunnel_env, name, value):
    env, commands = tunnel_env
    env[name] = value
    process, _, _ = run_tunnel(env)
    assert process.returncode != 0
    assert read_commands(commands) == []
