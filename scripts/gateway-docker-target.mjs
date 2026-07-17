import path from "node:path";
import os from "node:os";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

const scriptPath = fileURLToPath(import.meta.url);
const projectRoot = path.resolve(path.dirname(scriptPath), "..");
const composeFile = path.join(projectRoot, "tools", "docker-compose.gateway-test.yml");
const composeProject = "boxteam-gateway-target";
const serviceName = "boxteam-gateway-ssh-target";
const imageName = "boxteam-gateway-ssh-target:services-v4";
const sshPort = process.env.BOXTEAM_GATEWAY_E2E_SSH_PORT ?? "22222";
const playwrightCache = path.resolve(
  process.env.BOXTEAM_GATEWAY_E2E_PLAYWRIGHT_CACHE ??
    path.join(os.homedir(), ".cache", "ms-playwright"),
);

function requirePath(targetPath, label) {
  if (!existsSync(targetPath)) {
    throw new Error(`${label}不存在: ${targetPath}`);
  }
}

function run(command, options = {}) {
  const result = Bun.spawnSync(command, {
    cwd: projectRoot,
    env: {
      ...process.env,
      BOXTEAM_GATEWAY_E2E_SSH_PORT: sshPort,
      BOXTEAM_GATEWAY_E2E_PLAYWRIGHT_CACHE: playwrightCache,
    },
    stdout: options.stdout ?? "inherit",
    stderr: options.stderr ?? "inherit",
  });
  if (result.exitCode !== 0) {
    throw new Error(`命令执行失败(${result.exitCode}): ${command.join(" ")}`);
  }
  return result;
}

function compose(...args) {
  return run([
    process.env.BOXTEAM_DOCKER_COMMAND ?? "docker",
    "compose",
    "-f",
    composeFile,
    "-p",
    composeProject,
    ...args,
  ]);
}

function containerId() {
  const result = run(
    [
      process.env.BOXTEAM_DOCKER_COMMAND ?? "docker",
      "compose",
      "-f",
      composeFile,
      "-p",
      composeProject,
      "ps",
      "-q",
      serviceName,
    ],
    { stdout: "pipe", stderr: "pipe" },
  );
  const id = new TextDecoder().decode(result.stdout).trim();
  if (!id) throw new Error("Docker Gateway 目标容器未运行，请先执行 up");
  return id;
}

function dockerExec(args, options = {}) {
  return run(
    [process.env.BOXTEAM_DOCKER_COMMAND ?? "docker", "exec", containerId(), ...args],
    options,
  );
}

function containerOsId() {
  const result = dockerExec(
    ["sh", "-lc", '. /etc/os-release; printf "%s%s" "$ID" "$VERSION_ID"'],
    { stdout: "pipe", stderr: "pipe" },
  );
  const raw = new TextDecoder().decode(result.stdout).trim().toLowerCase();
  const sanitized = raw.replace(/[^a-z0-9]/g, "");
  if (!sanitized) throw new Error(`无法解析容器系统简称: ${raw || "<empty>"}`);
  return sanitized;
}

function ensurePythonEnvironment() {
  const environmentPath = `/workspace/vscode-graph-agent-opencode/.venv_docker_${containerOsId()}`;
  dockerExec([
    "sh",
    "-lc",
    [
      "set -eu",
      `cd ${JSON.stringify("/workspace/vscode-graph-agent-opencode")}`,
      `UV_PROJECT_ENVIRONMENT=${JSON.stringify(environmentPath)} uv sync --frozen`,
    ].join("; "),
  ]);
  return environmentPath;
}

function up() {
  requirePath(playwrightCache, "Playwright 浏览器缓存目录");
  const imageProbe = Bun.spawnSync(
    [process.env.BOXTEAM_DOCKER_COMMAND ?? "docker", "image", "inspect", imageName],
    { cwd: projectRoot, stdout: "ignore", stderr: "ignore" },
  );
  const shouldBuild =
    process.env.BOXTEAM_GATEWAY_E2E_REBUILD_IMAGE === "1" || imageProbe.exitCode !== 0;
  compose("up", "-d", shouldBuild ? "--build" : "--no-build");
}

function devStart() {
  up();
  const pythonEnvironment = ensurePythonEnvironment();
  try {
    dockerExec([
      "sh",
      "-lc",
      [
        "set -eu",
        "cd /workspace/vscode-graph-agent-opencode",
        [
          `BOXTEAM_PYTHON_BIN=${JSON.stringify(`${pythonEnvironment}/bin/python`)}`,
          `UV_PROJECT_ENVIRONMENT=${JSON.stringify(pythonEnvironment)}`,
          "BOXTEAM_VITE_CACHE_DIR=/tmp/boxteam-vite-cache",
          "BOXTEAM_ENABLE_GATEWAY_E2E_WORKSPACE=0",
          "BOXTEAM_INSTALL_DEVELOPMENT_ASSETS=0",
          "bun run scripts/dev.mjs --only-launch",
        ].join(" "),
      ].join("; "),
    ]);
  } catch (error) {
    devStop();
    throw error;
  }
}

function devStop() {
  const ports = [8002, 8010, 8011, 8012, 8013, 8014, 8015, 8016].join(" ");
  const stopCommand = [
    `for port in ${ports}; do`,
    'pids=$(lsof -tiTCP:$port -sTCP:LISTEN || true)',
    'if [ -n "$pids" ]; then kill $pids; fi',
    "done",
  ].join("\n");
  dockerExec([
    "sh",
    "-lc",
    stopCommand,
  ]);
}

function printUsage() {
  console.log(
    "用法: bun run scripts/gateway-docker-target.mjs <up|down|status|dev-start|dev-stop>",
  );
}

const command = process.argv[2];
switch (command) {
  case "up":
    up();
    break;
  case "down":
    compose("down", "--remove-orphans");
    break;
  case "status":
    compose("ps");
    break;
  case "dev-start":
    devStart();
    break;
  case "dev-stop":
    devStop();
    break;
  default:
    printUsage();
    process.exitCode = 2;
}
