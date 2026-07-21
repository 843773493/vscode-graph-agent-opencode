import { existsSync, readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import Ajv from "ajv";
import { parse, printParseErrorCode } from "jsonc-parser";

const CONFIG_ENV = "BOXTEAM_CROSS_PLATFORM_TARGET_CONFIG";

function replaceTokens(value, { projectRoot, homeDirectory }) {
  return value
    .replaceAll("${PROJECT_ROOT}", projectRoot)
    .replaceAll("${HOME}", homeDirectory);
}

function expandValue(value, context) {
  if (typeof value === "string") return replaceTokens(value, context);
  if (Array.isArray(value)) return value.map((item) => expandValue(item, context));
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, expandValue(item, context)]),
    );
  }
  return value;
}

function isWindowsAbsolute(value) {
  return /^[A-Za-z]:[\\/]/.test(value);
}

function validateResolvedTarget(target) {
  const duplicateSensitivePaths = [
    ["ssh.identityFile", target.ssh.identityFile],
    ["ssh.knownHostsFile", target.ssh.knownHostsFile],
  ];
  if (target.docker) {
    duplicateSensitivePaths.push(["docker.persistentRoot", target.docker.persistentRoot]);
  }
  for (const [label, value] of duplicateSensitivePaths) {
    if (!path.isAbsolute(value)) {
      throw new Error(`目标 ${target.id} 的 ${label} 必须解析为宿主机绝对路径: ${value}`);
    }
  }

  const remotePaths = Object.entries(target.paths);
  for (const [label, value] of remotePaths) {
    const absolute =
      target.platform === "windows" ? isWindowsAbsolute(value) : value.startsWith("/");
    if (!absolute) {
      throw new Error(
        `目标 ${target.id} 的 paths.${label} 不是 ${target.platform} 绝对路径: ${value}`,
      );
    }
  }
  if (target.provisioner === "docker" && target.platform !== "linux") {
    throw new Error(`目标 ${target.id}: 当前 Docker provisioner 只支持 platform=linux`);
  }
}

export function defaultTargetConfigPath({ environment = process.env } = {}) {
  const homeDirectory = os.homedir();
  const configuredHome = environment.BOXTEAM_HOME?.trim();
  const boxteamHome = configuredHome
    ? path.resolve(configuredHome.replace(/^~(?=$|[\\/])/, homeDirectory))
    : path.join(homeDirectory, ".boxteams-dev");
  return path.join(boxteamHome, "config", "cross-platform-development-targets.jsonc");
}

export function loadTargetConfiguration({
  configPath,
  projectRoot = process.cwd(),
  environment = process.env,
} = {}) {
  const resolvedProjectRoot = path.resolve(projectRoot);
  const requestedPath =
    configPath ?? environment[CONFIG_ENV]?.trim() ?? defaultTargetConfigPath({ environment });
  const resolvedConfigPath = path.resolve(
    requestedPath.replace(/^~(?=$|[\\/])/, os.homedir()),
  );
  if (!existsSync(resolvedConfigPath)) {
    throw new Error(
      `跨平台开发目标配置不存在: ${resolvedConfigPath}\n` +
        "请从 tools/cross-platform-development-targets/targets.example.jsonc 复制后配置。",
    );
  }

  const parseErrors = [];
  const raw = parse(readFileSync(resolvedConfigPath, "utf8"), parseErrors, {
    allowTrailingComma: true,
    disallowComments: false,
  });
  if (parseErrors.length > 0) {
    const details = parseErrors
      .map((error) => `${printParseErrorCode(error.error)}@${error.offset}`)
      .join(", ");
    throw new Error(`目标配置 JSONC 解析失败: ${resolvedConfigPath}: ${details}`);
  }

  const schemaPath = path.join(
    resolvedProjectRoot,
    "tools",
    "cross-platform-development-targets",
    "target.schema.json",
  );
  const schema = JSON.parse(readFileSync(schemaPath, "utf8"));
  const validate = new Ajv({ allErrors: true, strict: true }).compile(schema);
  if (!validate(raw)) {
    const details = validate.errors
      .map((error) => `${error.instancePath || "/"} ${error.message}`)
      .join("; ");
    throw new Error(`目标配置不符合 schema: ${resolvedConfigPath}: ${details}`);
  }

  const expanded = expandValue(raw, {
    projectRoot: resolvedProjectRoot,
    homeDirectory: os.homedir(),
  });
  const ids = new Set();
  for (const target of expanded.targets) {
    if (ids.has(target.id)) throw new Error(`目标 ID 重复: ${target.id}`);
    ids.add(target.id);
    validateResolvedTarget(target);
  }
  return {
    configPath: resolvedConfigPath,
    projectRoot: resolvedProjectRoot,
    targets: expanded.targets,
  };
}

export function selectTarget(configuration, targetId) {
  const target = configuration.targets.find((candidate) => candidate.id === targetId);
  if (!target) {
    throw new Error(
      `未知目标: ${targetId}; 可用目标: ${configuration.targets
        .map((candidate) => candidate.id)
        .join(", ")}`,
    );
  }
  return target;
}
