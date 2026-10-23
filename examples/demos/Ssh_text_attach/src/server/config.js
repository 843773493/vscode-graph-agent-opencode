import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const demoRoot = fileURLToPath(new URL("../../", import.meta.url));
const DEFAULT_GATEWAY_PORT = 7910;
const DEFAULT_FRONTEND_PORT = 7911;
const DEFAULT_BACKEND_PORT = 7912;
const SHORT_UUID_PATTERN = /^[a-f0-9]{12}$/;

export function readOptionValue(name, argv = process.argv) {
  const index = argv.indexOf(name);
  if (index === -1) {
    return null;
  }
  const value = argv[index + 1];
  if (!value || value.startsWith("--")) {
    throw new Error(`缺少命令行参数 ${name} 的值`);
  }
  return value;
}

export function parsePort(rawValue, label) {
  const port = Number(rawValue);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new Error(`${label} 必须是 1-65535 的整数，当前值: ${rawValue}`);
  }
  return port;
}

function resolveProjectPath(pathBaseDirectory, rawPath) {
  if (path.isAbsolute(rawPath)) {
    return path.normalize(rawPath);
  }
  const resolvedBaseDirectory = path.resolve(pathBaseDirectory);
  const resolvedPath = path.resolve(resolvedBaseDirectory, rawPath);
  const relativePath = path.relative(resolvedBaseDirectory, resolvedPath);
  if (
    relativePath === ".."
    || relativePath.startsWith(`..${path.sep}`)
    || path.isAbsolute(relativePath)
  ) {
    throw new Error(`相对路径不得离开基准目录: ${rawPath}`);
  }
  return resolvedPath;
}

function assertObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} 必须是对象`);
  }
}

function assertString(value, label) {
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error(`${label} 必须是非空字符串`);
  }
}

function assertShortUuid(value, label) {
  assertString(value, label);
  if (!SHORT_UUID_PATTERN.test(value)) {
    throw new Error(`${label} 必须是 12 位小写 hex UUID，当前值: ${value}`);
  }
}

function normalizeLocalTarget(target) {
  assertString(target.backendOrigin, `目标 ${target.id} 的 backendOrigin`);
  return {
    id: target.id,
    name: target.name,
    kind: "local",
    backendOrigin: target.backendOrigin,
  };
}

function normalizeSshTarget(target, pathBaseDirectory) {
  assertObject(target.ssh, `目标 ${target.id} 的 ssh`);
  assertString(target.ssh.host, `目标 ${target.id} 的 ssh.host`);
  assertString(target.ssh.user, `目标 ${target.id} 的 ssh.user`);
  assertString(target.ssh.privateKeyPath, `目标 ${target.id} 的 ssh.privateKeyPath`);
  assertString(target.ssh.remoteBackendHost, `目标 ${target.id} 的 ssh.remoteBackendHost`);

  return {
    id: target.id,
    name: target.name,
    kind: "ssh",
    ssh: {
      host: target.ssh.host,
      port: parsePort(target.ssh.port, `目标 ${target.id} 的 ssh.port`),
      user: target.ssh.user,
      privateKeyPath: resolveProjectPath(pathBaseDirectory, target.ssh.privateKeyPath),
      remoteBackendHost: target.ssh.remoteBackendHost,
      remoteBackendPort: parsePort(target.ssh.remoteBackendPort, `目标 ${target.id} 的 ssh.remoteBackendPort`),
    },
  };
}

function normalizeTarget(target, pathBaseDirectory) {
  assertObject(target, "targets[]");
  assertShortUuid(target.id, "targets[].id");
  assertString(target.name, `目标 ${target.id} 的 name`);
  assertString(target.kind, `目标 ${target.id} 的 kind`);

  if (target.kind === "local") {
    return normalizeLocalTarget(target);
  }
  if (target.kind === "ssh") {
    return normalizeSshTarget(target, pathBaseDirectory);
  }
  throw new Error(`不支持的目标类型: ${target.kind}`);
}

export function parseTargetsConfigText(text, pathBaseDirectory) {
  const rawConfig = JSON.parse(text);
  assertObject(rawConfig, "targets 配置");
  assertShortUuid(rawConfig.defaultTargetId, "defaultTargetId");
  if (!Array.isArray(rawConfig.targets) || rawConfig.targets.length === 0) {
    throw new Error("targets 必须是非空数组");
  }

  const targets = rawConfig.targets.map((target) => normalizeTarget(target, pathBaseDirectory));
  const ids = new Set();
  for (const target of targets) {
    if (ids.has(target.id)) {
      throw new Error(`重复的目标 id: ${target.id}`);
    }
    ids.add(target.id);
  }
  if (!ids.has(rawConfig.defaultTargetId)) {
    throw new Error(`defaultTargetId 不存在: ${rawConfig.defaultTargetId}`);
  }

  return {
    defaultTargetId: rawConfig.defaultTargetId,
    targets,
  };
}

export async function loadTargetsConfig(configPath, pathBaseDirectory) {
  const text = await readFile(configPath, "utf8");
  return parseTargetsConfigText(text, pathBaseDirectory);
}

export function publicTarget(target) {
  if (target.kind === "local") {
    return {
      id: target.id,
      name: target.name,
      kind: target.kind,
      backendOrigin: target.backendOrigin,
    };
  }

  return {
    id: target.id,
    name: target.name,
    kind: target.kind,
    ssh: {
      host: target.ssh.host,
      port: target.ssh.port,
      user: target.ssh.user,
      privateKeyPath: target.ssh.privateKeyPath,
      remoteBackend: `${target.ssh.remoteBackendHost}:${target.ssh.remoteBackendPort}`,
    },
  };
}

export function resolveRuntimeConfig({ environment = process.env, argv = process.argv } = {}) {
  const rawRoles = environment.SSH_TEXT_ROLES || readOptionValue("--roles", argv) || "frontend,gateway,backend";
  const roles = rawRoles.split(",").map((role) => role.trim()).filter(Boolean);
  if (roles.length === 0) {
    throw new Error("SSH_TEXT_ROLES 至少要包含一个角色");
  }
  const supportedRoles = new Set(["frontend", "gateway", "backend"]);
  for (const role of roles) {
    if (!supportedRoles.has(role)) {
      throw new Error(`不支持的服务角色: ${role}`);
    }
  }

  const gatewayHost = environment.SSH_TEXT_GATEWAY_HOST || readOptionValue("--gateway-host", argv) || readOptionValue("--host", argv) || "127.0.0.1";
  const frontendHost = environment.SSH_TEXT_FRONTEND_HOST || readOptionValue("--frontend-host", argv) || readOptionValue("--host", argv) || "127.0.0.1";
  const backendHost = environment.SSH_TEXT_BACKEND_HOST || readOptionValue("--backend-host", argv) || readOptionValue("--host", argv) || "127.0.0.1";
  const gatewayPort = parsePort(environment.SSH_TEXT_GATEWAY_PORT || readOptionValue("--gateway-port", argv) || DEFAULT_GATEWAY_PORT, "gateway 端口");
  const backendPort = parsePort(environment.SSH_TEXT_BACKEND_PORT || readOptionValue("--backend-port", argv) || DEFAULT_BACKEND_PORT, "后端端口");
  const frontendPort = parsePort(environment.SSH_TEXT_FRONTEND_PORT || readOptionValue("--frontend-port", argv) || DEFAULT_FRONTEND_PORT, "前端端口");
  const dataFilePath = resolveProjectPath(
    demoRoot,
    environment.SSH_TEXT_DATA_FILE || readOptionValue("--data-file", argv) || ".boxteam/managed-note.txt",
  );
  const dataLabel = environment.SSH_TEXT_DATA_LABEL || readOptionValue("--data-label", argv) || "本地后端";
  const targetsConfigPath = resolveProjectPath(
    demoRoot,
    environment.SSH_TEXT_TARGETS_CONFIG || readOptionValue("--targets-config", argv) || "config/targets.json",
  );

  return {
    demoRoot,
    roles,
    gatewayHost,
    gatewayPort,
    backendHost,
    backendPort,
    frontendHost,
    frontendPort,
    dataFilePath,
    dataLabel,
    targetsConfigPath,
  };
}
