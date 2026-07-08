#!/usr/bin/env bash
set -euo pipefail

public_key_path="${SSH_TEXT_PUBLIC_KEY_PATH:-/workspace/demo/ssh/id_ed25519.pub}"
authorized_keys_path="/home/demo/.ssh/authorized_keys"

if [[ ! -f "${public_key_path}" ]]; then
  echo "找不到 SSH 公钥: ${public_key_path}" >&2
  exit 1
fi

install -d -m 700 -o demo -g demo /home/demo/.ssh
install -m 600 -o demo -g demo "${public_key_path}" "${authorized_keys_path}"
ssh-keygen -A

/usr/sbin/sshd -t \
  -o PasswordAuthentication=no \
  -o PubkeyAuthentication=yes \
  -o PermitRootLogin=no

/usr/sbin/sshd -D -e \
  -o PasswordAuthentication=no \
  -o PubkeyAuthentication=yes \
  -o PermitRootLogin=no &

exec "$@"
