#!/usr/bin/env bash
# Run on the Linux reverse-proxy host, separately from the control tunnel.
set -euo pipefail

fail() { printf '%s\n' "$1" >&2; exit 1; }

host=${MUSELAB_UPLOAD_SSH_HOST:-}
[[ -n "$host" && "$host" != -* && "$host" != *[[:space:]]* ]] \
  || fail 'Set MUSELAB_UPLOAD_SSH_HOST to an SSH host or config alias.'
listen_port=${MUSELAB_UPLOAD_LISTEN_PORT:-8785}
target_port=${MUSELAB_UPLOAD_TARGET_PORT:-8765}
for port in "$listen_port" "$target_port"; do
  [[ "$port" =~ ^[0-9]{1,5}$ ]] && (( 10#$port >= 1 && 10#$port <= 65535 )) \
    || fail 'Upload tunnel ports must be between 1 and 65535.'
done

# ControlMaster=no alone can still attach to an existing ControlPath.
# Both options are required to keep bulk data off the control connection.
ssh_options=(
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
  -o ConnectTimeout=8
  -o ConnectionAttempts=1
  -o ControlMaster=no
  -o ControlPath=none
)
if [[ -n ${MUSELAB_UPLOAD_SSH_USER:-} ]]; then
  ssh_options+=(-l "$MUSELAB_UPLOAD_SSH_USER")
fi
if [[ -n ${MUSELAB_UPLOAD_SSH_IDENTITY_FILE:-} ]]; then
  ssh_options+=(-i "$MUSELAB_UPLOAD_SSH_IDENTITY_FILE" -o IdentitiesOnly=yes)
fi

target_host=${MUSELAB_UPLOAD_TARGET_HOST:-127.0.0.1}
if [[ -n ${MUSELAB_UPLOAD_TARGET_COMMAND:-} ]]; then
  command -v timeout >/dev/null || fail 'Target discovery requires GNU timeout.'
  # A command can stall after SSH connects, so ConnectTimeout is insufficient.
  # Keep the remote command as one argument; never evaluate it locally.
  if ! addresses=$(timeout --kill-after=5s 30s ssh -T "${ssh_options[@]}" \
      "$host" "$MUSELAB_UPLOAD_TARGET_COMMAND" 2>/dev/null); then
    fail 'Could not discover the upload target.'
  fi
  target_host=$(printf '%s' "$addresses" | tr -d '\000\r' | awk 'NF {print $1; exit}')
fi

# This helper targets IPv4 listeners, including WSL2 NAT addresses.
[[ "$target_host" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] \
  || fail 'Upload target must be an IPv4 address.'
IFS=. read -r -a octets <<< "$target_host"
for octet in "${octets[@]}"; do
  (( 10#$octet <= 255 )) || fail 'Upload target must be a valid IPv4 address.'
done

exec ssh -NT "${ssh_options[@]}" \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=3 \
  -L "127.0.0.1:${listen_port}:${target_host}:${target_port}" \
  "$host"
