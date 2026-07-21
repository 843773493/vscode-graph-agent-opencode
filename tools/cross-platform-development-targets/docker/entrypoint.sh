#!/bin/sh
set -eu

target_uid=${BOXTEAM_TARGET_UID:?缺少 BOXTEAM_TARGET_UID}
target_gid=${BOXTEAM_TARGET_GID:?缺少 BOXTEAM_TARGET_GID}
repository=/opt/boxteam-dev/repository
artifacts=/opt/boxteam-dev/artifacts
ssh_root=/opt/boxteam-dev/ssh
target_home=/home/boxteam
cache_root=$target_home/.cache
bootstrap_key=/run/boxteam-target/authorized_key.pub

actual_uid=$(id -u boxteam)
actual_gid=$(id -g boxteam)
test "$actual_uid" = "$target_uid" || {
  printf 'Docker 目标 UID 不一致: image=%s runtime=%s\n' "$actual_uid" "$target_uid" >&2
  exit 1
}
test "$actual_gid" = "$target_gid" || {
  printf 'Docker 目标 GID 不一致: image=%s runtime=%s\n' "$actual_gid" "$target_gid" >&2
  exit 1
}

mkdir -p "$repository" "$artifacts" "$ssh_root" "$target_home" "$cache_root" /run/sshd
chown -R "$target_uid:$target_gid" "$repository" "$artifacts" "$target_home" "$cache_root"

if test ! -f "$ssh_root/authorized_keys"; then
  test -f "$bootstrap_key" || {
    printf '缺少首次启动 SSH 公钥: %s\n' "$bootstrap_key" >&2
    exit 1
  }
  cp "$bootstrap_key" "$ssh_root/authorized_keys"
fi
chmod 600 "$ssh_root/authorized_keys"
chown "$target_uid:$target_gid" "$ssh_root/authorized_keys"

if test ! -f "$ssh_root/ssh_host_ed25519_key"; then
  ssh-keygen -q -t ed25519 -N '' -f "$ssh_root/ssh_host_ed25519_key"
fi
chmod 600 "$ssh_root/ssh_host_ed25519_key"
chmod 644 "$ssh_root/ssh_host_ed25519_key.pub"

sshd_config=$ssh_root/sshd_config
cat > "$sshd_config" <<EOF
Port 22
ListenAddress 0.0.0.0
HostKey $ssh_root/ssh_host_ed25519_key
PidFile /run/sshd/sshd.pid
AuthorizedKeysFile $ssh_root/authorized_keys
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers boxteam
Subsystem sftp internal-sftp
EOF
chmod 600 "$sshd_config"

exec /usr/sbin/sshd -D -e -f "$sshd_config"
