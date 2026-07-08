import { spawnSync } from "node:child_process";

const proxy = process.env.SSH_TEXT_DOCKER_PROXY
  || process.env.HTTP_PROXY
  || process.env.http_proxy
  || "http://100.64.0.58:10809";
const upstreamImage = process.env.SSH_TEXT_UPSTREAM_NODE_IMAGE || "node:22-bookworm-slim";
const localImage = process.env.SSH_TEXT_BASE_IMAGE || "ssh-text-attach-node:22-bookworm-slim";
const serviceImage = process.env.SSH_TEXT_SERVICE_IMAGE || "ssh_text_attach-ssh-text-attach";

function proxyEnvPairs() {
  return [
    `HTTP_PROXY=${proxy}`,
    `HTTPS_PROXY=${proxy}`,
    `ALL_PROXY=${proxy}`,
    `http_proxy=${proxy}`,
    `https_proxy=${proxy}`,
    `all_proxy=${proxy}`,
    `SSH_TEXT_BASE_IMAGE=${localImage}`,
  ];
}

function commandOk(command, args) {
  const result = spawnSync(command, args, {
    env: { ...process.env, HTTP_PROXY: proxy, HTTPS_PROXY: proxy, ALL_PROXY: proxy, SSH_TEXT_BASE_IMAGE: localImage },
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
    env: { ...process.env, HTTP_PROXY: proxy, HTTPS_PROXY: proxy, ALL_PROXY: proxy, SSH_TEXT_BASE_IMAGE: localImage },
    stdio: "inherit",
  });
  if (result.status !== 0) {
    throw new Error(`命令失败: ${prefix.join(" ")} ${args.join(" ")}`);
  }
}

const prefix = dockerPrefix();

console.log(`[docker] 使用代理 ${proxy} 拉取 ${upstreamImage}`);
run(prefix, ["pull", upstreamImage]);
console.log(`[docker] 标记本地基础镜像 ${localImage}`);
run(prefix, ["tag", upstreamImage, localImage]);
console.log(`[docker] 构建服务镜像 ${serviceImage}`);
run(prefix, [
  "build",
  "--pull=false",
  "--build-arg",
  `SSH_TEXT_BASE_IMAGE=${localImage}`,
  "--build-arg",
  `HTTP_PROXY=${proxy}`,
  "--build-arg",
  `HTTPS_PROXY=${proxy}`,
  "--build-arg",
  `ALL_PROXY=${proxy}`,
  "--build-arg",
  `http_proxy=${proxy}`,
  "--build-arg",
  `https_proxy=${proxy}`,
  "--build-arg",
  `all_proxy=${proxy}`,
  "-t",
  serviceImage,
  ".",
]);
console.log("[docker] 启动 Compose 服务");
run(prefix, ["compose", "up", "-d", "--no-build", "--remove-orphans"]);
