import {
  appendFileSync,
  chmodSync,
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
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

function dockerKnownHostPatternRows(knownHosts) {
  return knownHosts.toString("utf8").split("\n").filter((line) => {
    const trimmed = line.trimStart();
    if (!trimmed || trimmed.startsWith("#")) return false;
    const fields = trimmed.split(/[ \t]+/);
    const hostField = fields[0].startsWith("@") ? fields[1] : fields[0];
    return Boolean(hostField && /[*?!]/.test(hostField));
  });
}

function splitDockerKnownHostAliases(knownHosts) {
  const source = knownHosts.toString("utf8");
  return {
    contents: source.replace(
      /^([ \t]*)(?!#)((?:@\S+[ \t]+)?)([^\s,]+(?:,[^\s,]+)+)([ \t].*)$/gm,
      (line, indentation, marker, aliases, entry) => /[*?!]/.test(aliases)
        ? line
        : aliases
          .split(",")
          .map((alias) => `${indentation}${marker}${alias}${entry}`)
          .join("\n"),
    ),
    patternRows: dockerKnownHostPatternRows(source),
  };
}

function assertDockerKnownHostPatternsPreserved(patternRows, stagedKnownHostsPath, target) {
  const stagedPatternCounts = new Map();
  for (const row of dockerKnownHostPatternRows(readFileSync(stagedKnownHostsPath))) {
    stagedPatternCounts.set(row, (stagedPatternCounts.get(row) ?? 0) + 1);
  }
  for (const row of patternRows) {
    const remainingCount = stagedPatternCounts.get(row) ?? 0;
    if (remainingCount === 0) {
      throw new Error(
        `Docker 目标 ${target.id} (${target.platform}) SSH host key 更新会移除 ` +
          `known_hosts pattern-list，拒绝改变其匹配语义: ${row}`,
      );
    }
    stagedPatternCounts.set(row, remainingCount - 1);
  }
}

function replaceDockerKnownHost(target, knownHostsPath, hostKeyOutput) {
  const knownHostsDirectory = path.dirname(knownHostsPath);
  const stagingDirectory = mkdtempSync(path.join(knownHostsDirectory, ".boxteam-known-hosts-"));
  const stagedKnownHostsPath = path.join(stagingDirectory, path.basename(knownHostsPath));
  const knownHostsExists = existsSync(knownHostsPath);
  const knownHostsMode = knownHostsExists
    ? statSync(knownHostsPath).mode & 0o777
    : 0o600;

  try {
    const sourceKnownHosts = knownHostsExists ? readFileSync(knownHostsPath) : Buffer.alloc(0);
    const { contents, patternRows } = splitDockerKnownHostAliases(sourceKnownHosts);
    writeFileSync(
      stagedKnownHostsPath,
      contents,
      { mode: 0o600 },
    );
    const hostToken = target.ssh.port === 22
      ? target.ssh.host
      : `[${target.ssh.host}]:${target.ssh.port}`;
    runChecked(["ssh-keygen", "-R", hostToken, "-f", stagedKnownHostsPath], {
      label: `Docker 目标 ${target.id} (${target.platform}) SSH host key 更新阶段`,
    });
    assertDockerKnownHostPatternsPreserved(patternRows, stagedKnownHostsPath, target);
    rmSync(`${stagedKnownHostsPath}.old`, { force: true });

    const stagedContents = readFileSync(stagedKnownHostsPath);
    const lineSeparator = stagedContents.length > 0 && stagedContents.at(-1) !== 0x0a
      ? "\n"
      : "";
    appendFileSync(stagedKnownHostsPath, `${lineSeparator}${hostKeyOutput.trim()}\n`, "utf8");
    chmodSync(stagedKnownHostsPath, knownHostsMode);
    renameSync(stagedKnownHostsPath, knownHostsPath);
  } finally {
    rmSync(stagingDirectory, { recursive: true, force: true });
  }
}

function installDockerKnownHost(target) {
  const knownHostsPath = target.ssh.knownHostsFile;
  mkdirSync(path.dirname(knownHostsPath), { recursive: true });
  const timeoutMs = 30_000;
  const deadline = performance.now() + timeoutMs;
  let lastError = "ssh-keyscan 未返回 host key";
  while (true) {
    const remainingMs = Math.floor(deadline - performance.now());
    if (remainingMs <= 0) break;
    const result = Bun.spawnSync(
      ["ssh-keyscan", "-p", String(target.ssh.port), "-t", "ed25519", target.ssh.host],
      { stdout: "pipe", stderr: "pipe", timeout: remainingMs },
    );
    if (result.exitedDueToTimeout) {
      const stderr = new TextDecoder().decode(result.stderr).trim();
      lastError =
        `ssh-keyscan 超过本次剩余子进程期限 ${remainingMs} ms，signal=${result.signalCode ?? "unknown"}` +
        (stderr ? `：${stderr}` : "");
      break;
    }
    if (result.exitCode === 0) {
      const output = new TextDecoder().decode(result.stdout).trim();
      if (output) {
        if (performance.now() >= deadline) {
          lastError = "ssh-keyscan 在观测期限后返回 host key";
          break;
        }
        replaceDockerKnownHost(target, knownHostsPath, output);
        return;
      }
      lastError = "ssh-keyscan 成功退出但没有返回 host key";
    } else {
      const stderr = new TextDecoder().decode(result.stderr).trim();
      lastError = stderr || `ssh-keyscan exitCode=${result.exitCode}，signal=${result.signalCode ?? "none"}`;
    }
    const sleepMs = Math.min(500, deadline - performance.now());
    if (sleepMs <= 0) break;
    Bun.sleepSync(sleepMs);
  }
  throw new Error(
    `Docker 目标 ${target.id} (${target.platform}) 的 SSH host key 阶段超过 ${timeoutMs} ms，` +
      `host=${target.ssh.host}, port=${target.ssh.port}: ${lastError}`,
  );
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
