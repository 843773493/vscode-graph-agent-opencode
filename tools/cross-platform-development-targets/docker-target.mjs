import { cpSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

import { runChecked } from "./process.mjs";

export function dockerPersistentPaths(target) {
  const root = path.resolve(target.docker.persistentRoot);
  return {
    root,
    repository: path.join(root, "repository"),
    home: path.join(root, "home"),
    caches: path.join(root, "caches"),
    ssh: path.join(root, "ssh"),
    artifacts: path.join(root, "artifacts"),
  };
}

export function dockerComposeEnvironment({ target, projectRoot }) {
  const persistent = dockerPersistentPaths(target);
  const uid = String(target.docker.hostUid ?? process.getuid?.() ?? 1000);
  const gid = String(target.docker.hostGid ?? process.getgid?.() ?? 1000);
  return {
    ...process.env,
    BOXTEAM_TARGET_UID: uid,
    BOXTEAM_TARGET_GID: gid,
    BOXTEAM_TARGET_IMAGE: target.docker.image,
    BOXTEAM_TARGET_SSH_PORT: String(target.ssh.port),
    BOXTEAM_TARGET_REPOSITORY: persistent.repository,
    BOXTEAM_TARGET_HOME: persistent.home,
    BOXTEAM_TARGET_CACHES: persistent.caches,
    BOXTEAM_TARGET_SSH: persistent.ssh,
    BOXTEAM_TARGET_ARTIFACTS: persistent.artifacts,
    BOXTEAM_TARGET_AUTHORIZED_KEY: path.join(
      projectRoot,
      "asset",
      "gateway_ssh",
      "boxteam_gateway_e2e_ed25519.pub",
    ),
  };
}

function dockerCommandPrefix() {
  return (process.env.BOXTEAM_DOCKER_COMMAND?.trim() || "docker").split(/\s+/);
}

export function dockerComposeCommand({ target, projectRoot }, ...args) {
  return [
    ...dockerCommandPrefix(),
    "compose",
    "-f",
    path.join(
      projectRoot,
      "tools",
      "cross-platform-development-targets",
      "docker",
      "compose.yml",
    ),
    "-p",
    target.docker.composeProject,
    ...args,
  ];
}

function initializePersistentDirectories(target) {
  const persistent = dockerPersistentPaths(target);
  for (const directory of Object.values(persistent)) {
    mkdirSync(directory, { recursive: true });
  }
  const source = target.docker.playwrightCacheSource;
  const destination = path.join(persistent.caches, "ms-playwright");
  if (source && existsSync(source) && !existsSync(destination)) {
    cpSync(source, destination, { recursive: true, preserveTimestamps: true });
  }
  return persistent;
}

function installDockerKnownHost(target) {
  const knownHostsPath = target.ssh.knownHostsFile;
  mkdirSync(path.dirname(knownHostsPath), { recursive: true });
  let lastError = "";
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const result = Bun.spawnSync(
      ["ssh-keyscan", "-p", String(target.ssh.port), "-t", "ed25519", target.ssh.host],
      { stdout: "pipe", stderr: "pipe" },
    );
    if (result.exitCode === 0) {
      const output = new TextDecoder().decode(result.stdout).trim();
      if (output) {
        writeFileSync(knownHostsPath, `${output}\n`, { encoding: "utf8", mode: 0o600 });
        return;
      }
    }
    lastError = new TextDecoder().decode(result.stderr).trim();
    Bun.sleepSync(500);
  }
  throw new Error(`Docker 目标 SSH host key 在 30 秒内不可用: ${lastError}`);
}

export function provisionDockerTarget({ target, projectRoot, rebuild = false }) {
  if (target.provisioner !== "docker") {
    throw new Error(`目标 ${target.id} 不是 Docker provisioner`);
  }
  initializePersistentDirectories(target);
  const environment = dockerComposeEnvironment({ target, projectRoot });
  const argumentsList = ["up", "-d"];
  const imageExists = Bun.spawnSync(
    [...dockerCommandPrefix(), "image", "inspect", target.docker.image],
    { stdout: "ignore", stderr: "ignore", env: environment },
  ).exitCode === 0;
  argumentsList.push(rebuild || !imageExists ? "--build" : "--no-build");
  runChecked(dockerComposeCommand({ target, projectRoot }, ...argumentsList), {
    cwd: projectRoot,
    environment,
    stdout: "inherit",
    stderr: "inherit",
    label: `创建 Docker 目标 ${target.id}`,
  });
  installDockerKnownHost(target);
  return dockerPersistentPaths(target);
}

export function destroyDockerContainer({ target, projectRoot }) {
  runChecked(dockerComposeCommand({ target, projectRoot }, "down", "--remove-orphans"), {
    cwd: projectRoot,
    environment: dockerComposeEnvironment({ target, projectRoot }),
    label: `删除 Docker 目标容器 ${target.id}`,
  });
}
