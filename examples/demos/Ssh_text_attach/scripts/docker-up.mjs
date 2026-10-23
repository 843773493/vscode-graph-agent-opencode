import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  describeCommandCheckResult,
  resolveDockerCommandPrefix,
} from "./dockerAccess.js";

const demoRoot = fileURLToPath(new URL("../", import.meta.url));
const proxy = process.env.SSH_TEXT_DOCKER_PROXY
  || process.env.HTTP_PROXY
  || process.env.http_proxy;
const upstreamImage = process.env.SSH_TEXT_UPSTREAM_NODE_IMAGE || "node:22-bookworm-slim";
const localImage = process.env.SSH_TEXT_BASE_IMAGE || "ssh-text-attach-node:22-bookworm-slim";
const serviceImage = process.env.SSH_TEXT_SERVICE_IMAGE || "ssh_text_attach-ssh-text-attach";
const proxyEnvironment = proxy
  ? {
      HTTP_PROXY: proxy,
      HTTPS_PROXY: proxy,
      ALL_PROXY: proxy,
      http_proxy: proxy,
      https_proxy: proxy,
      all_proxy: proxy,
    }
  : {};
const commandEnvironment = {
  ...process.env,
  ...proxyEnvironment,
  SSH_TEXT_BASE_IMAGE: localImage,
};

function proxyEnvPairs() {
  return [
    ...Object.entries(proxyEnvironment).map(([name, value]) => `${name}=${value}`),
    `SSH_TEXT_BASE_IMAGE=${localImage}`,
  ];
}

function checkCommand(command, args) {
  const result = spawnSync(command, args, {
    cwd: demoRoot,
    env: commandEnvironment,
    encoding: "utf8",
    maxBuffer: 64 * 1024,
    stdio: ["ignore", "ignore", "pipe"],
  });
  return describeCommandCheckResult(result);
}

function dockerPrefix() {
  return resolveDockerCommandPrefix({
    platform: process.platform,
    checkCommand,
    sudoEnvironmentPairs: proxyEnvPairs(),
  });
}

function run(prefix, args) {
  const result = spawnSync(prefix[0], [...prefix.slice(1), ...args], {
    cwd: demoRoot,
    env: commandEnvironment,
    stdio: "inherit",
  });
  const operation = args[0] || "unknown";
  if (result.error) {
    throw new Error(`Docker 操作 ${operation} 无法启动: ${result.error.message}`, {
      cause: result.error,
    });
  }
  if (result.status !== 0) {
    throw new Error(
      `Docker 操作 ${operation} 失败: code=${result.status ?? "null"} signal=${result.signal ?? "null"}`,
    );
  }
}

const prefix = dockerPrefix();

console.log(
  proxy
    ? `[docker] 已配置代理，使用代理拉取 ${upstreamImage}`
    : `[docker] 未配置代理，直接拉取 ${upstreamImage}`,
);
run(prefix, ["pull", upstreamImage]);
console.log(`[docker] 标记本地基础镜像 ${localImage}`);
run(prefix, ["tag", upstreamImage, localImage]);
console.log(`[docker] 构建服务镜像 ${serviceImage}`);
const proxyBuildArgs = Object.entries(proxyEnvironment).flatMap(([name, value]) => [
  "--build-arg",
  `${name}=${value}`,
]);
run(prefix, [
  "build",
  "--pull=false",
  "--build-arg",
  `SSH_TEXT_BASE_IMAGE=${localImage}`,
  ...proxyBuildArgs,
  "-t",
  serviceImage,
  ".",
]);
console.log("[docker] 启动 Compose 服务");
run(prefix, ["compose", "up", "-d", "--no-build", "--remove-orphans"]);
