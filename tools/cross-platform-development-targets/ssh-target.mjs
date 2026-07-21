import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import { runChecked, runCheckedWithInput, shellQuote } from "./process.mjs";

function sshOptions(target) {
  if (!existsSync(target.ssh.identityFile)) {
    throw new Error(`目标 ${target.id} 的 SSH identity 不存在: ${target.ssh.identityFile}`);
  }
  return [
    "-i",
    target.ssh.identityFile,
    "-p",
    String(target.ssh.port),
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    `UserKnownHostsFile=${target.ssh.knownHostsFile}`,
  ];
}

export function sshDestination(target) {
  return `${target.ssh.user}@${target.ssh.host}`;
}

export function sshCommand(target, remoteCommand) {
  return ["ssh", ...sshOptions(target), sshDestination(target), remoteCommand];
}

export function scpCommand(target, localPath, remotePath) {
  const options = sshOptions(target);
  const portIndex = options.indexOf("-p");
  options[portIndex] = "-P";
  return ["scp", ...options, localPath, `${sshDestination(target)}:${remotePath}`];
}

export function scpDownloadCommand(target, remotePath, localPath) {
  const options = sshOptions(target);
  const portIndex = options.indexOf("-p");
  options[portIndex] = "-P";
  return ["scp", ...options, `${sshDestination(target)}:${remotePath}`, localPath];
}

export async function runTargetAction({
  target,
  projectRoot,
  action,
  args = [],
}) {
  if (target.platform === "linux") {
    const scriptPath = path.join(
      projectRoot,
      "tools",
      "cross-platform-development-targets",
      "linux",
      "manage-target.sh",
    );
    const remoteCommand = ["sh", "-s", "--", action, ...args]
      .map(shellQuote)
      .join(" ");
    return runCheckedWithInput(
      sshCommand(target, remoteCommand),
      readFileSync(scriptPath, "utf8"),
      { cwd: projectRoot, label: `目标 ${target.id} Linux 动作 ${action}` },
    );
  }

  const scriptPath = path.join(
    projectRoot,
    "tools",
    "cross-platform-development-targets",
    "windows",
    "Manage-Target.ps1",
  );
  const encodedArguments = Buffer.from(JSON.stringify([action, ...args]), "utf8").toString(
    "base64",
  );
  const input = `$env:BOXTEAM_TARGET_ARGUMENTS_BASE64='${encodedArguments}'\n${readFileSync(
    scriptPath,
    "utf8",
  )}`;
  return runCheckedWithInput(
    sshCommand(target, "powershell.exe -NoLogo -NoProfile -NonInteractive -Command -"),
    input,
    { cwd: projectRoot, label: `目标 ${target.id} Windows 动作 ${action}` },
  );
}

function gitSshCommand(target) {
  return ["ssh", ...sshOptions(target)].map(shellQuote).join(" ");
}

function gitRemoteUrl(target) {
  if (target.platform === "windows") {
    // TODO: 在真实 VMware Windows OpenSSH 环境验证盘符路径经 git-receive-pack 的转义规则。
    const windowsPath = target.paths.repository.replaceAll("\\", "/");
    return `ssh://${encodeURIComponent(target.ssh.user)}@${target.ssh.host}:${target.ssh.port}/${windowsPath}`;
  }
  return `ssh://${encodeURIComponent(target.ssh.user)}@${target.ssh.host}:${target.ssh.port}${target.paths.repository}`;
}

export function pushSnapshot({ target, projectRoot, snapshot }) {
  const environment = {
    ...process.env,
    GIT_SSH_COMMAND: gitSshCommand(target),
  };
  return runChecked(
    ["git", "push", "--force", gitRemoteUrl(target), `${snapshot.commit}:${snapshot.ref}`],
    {
      cwd: projectRoot,
      environment,
      label: `向目标 ${target.id} 推送快照`,
    },
  );
}

export function assertMatchingSha256(localHash, remoteHash, targetId) {
  if (remoteHash !== localHash) {
    throw new Error(
      `目标 ${targetId} 的 .env SHA-256 校验失败: local=${localHash}, remote=${remoteHash}`,
    );
  }
}

export async function synchronizeEnvironmentFile({ target, projectRoot }) {
  const sourcePath = path.join(projectRoot, ".env");
  if (!existsSync(sourcePath)) throw new Error(`宿主机 .env 不存在: ${sourcePath}`);
  const contents = readFileSync(sourcePath);
  const localHash = createHash("sha256").update(contents).digest("hex");
  const temporaryPath = `${target.paths.repository}/.env.uploading-${process.pid}-${Date.now()}`;
  runChecked(scpCommand(target, sourcePath, temporaryPath), {
    cwd: projectRoot,
    label: `向目标 ${target.id} 上传 .env 临时文件`,
  });
  const remoteHash = (
    await runTargetAction({
      target,
      projectRoot,
      action: "hash-file",
      args: [temporaryPath],
    })
  ).stdout.trim();
  try {
    assertMatchingSha256(localHash, remoteHash, target.id);
  } catch (error) {
    await runTargetAction({
      target,
      projectRoot,
      action: "remove-upload",
      args: [temporaryPath],
    });
    throw error;
  }
  await runTargetAction({
    target,
    projectRoot,
    action: "install-env",
    args: [temporaryPath, `${target.paths.repository}/.env`],
  });
  return { bytes: contents.byteLength, sha256: localHash };
}
