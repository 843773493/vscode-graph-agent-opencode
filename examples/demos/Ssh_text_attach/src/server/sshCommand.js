export function buildSshArguments(
  sshConfig,
  localPort,
  platform = process.platform,
) {
  const knownHostsSinkPath = platform === "win32" ? "NUL" : "/dev/null";
  return [
    "-N",
    "-L",
    `127.0.0.1:${localPort}:${sshConfig.remoteBackendHost}:${sshConfig.remoteBackendPort}`,
    "-i",
    sshConfig.privateKeyPath,
    "-p",
    String(sshConfig.port),
    "-o",
    "ExitOnForwardFailure=yes",
    "-o",
    "IdentitiesOnly=yes",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    `UserKnownHostsFile=${knownHostsSinkPath}`,
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=2",
    `${sshConfig.user}@${sshConfig.host}`,
  ];
}
