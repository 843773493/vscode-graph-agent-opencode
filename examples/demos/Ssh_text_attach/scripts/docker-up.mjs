import { spawnSync } from "node:child_process";

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

function commandOk(command, args) {
  const result = spawnSync(command, args, {
    env: commandEnvironment,
    stdio: "ignore",
  });
  return result.status === 0;
}

function dockerPrefix() {
  if (commandOk("docker", ["version"])) {
    return ["docker"];
  }
  if (commandOk("sudo", ["-n", "env", ...proxyEnvPairs(), "docker", "version"])) {
    return ["sudo", "-n", "env", ...proxyEnvPairs(), "docker"];
  }
  throw new Error("无法访问 Docker daemon：当前用户没有权限，且 sudo -n docker 不可用");
}

function run(prefix, args) {
  const result = spawnSync(prefix[0], [...prefix.slice(1), ...args], {
    env: commandEnvironment,
    stdio: "inherit",
  });
  if (result.status !== 0) {
    throw new Error(`命令失败: ${prefix.join(" ")} ${args.join(" ")}`);
  }
}

const prefix = dockerPrefix();

console.log(
  proxy
    ? `[docker] 使用代理 ${proxy} 拉取 ${upstreamImage}`
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
