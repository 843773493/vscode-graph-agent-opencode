import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { terminateChildProcessTree } from "./processTree.js";
import { pipePrefixedOutput } from "./processOutput.js";

const demoRoot = fileURLToPath(new URL("../", import.meta.url));

function readOptionValue(name) {
  const index = process.argv.indexOf(name);
  if (index === -1) {
    return null;
  }
  const value = process.argv[index + 1];
  if (!value || value.startsWith("--")) {
    throw new Error(`缺少命令行参数 ${name} 的值`);
  }
  return value;
}

function profileProcesses(profile) {
  if (profile === "host") {
    return [
      {
        name: "host-backend",
        env: {
          SSH_TEXT_ROLES: "backend",
          SSH_TEXT_BACKEND_HOST: "127.0.0.1",
          SSH_TEXT_BACKEND_PORT: "7912",
          SSH_TEXT_DATA_FILE: ".boxteam/host-backend-note.txt",
          SSH_TEXT_DATA_LABEL: "宿主机后端",
        },
      },
      {
        name: "host-gateway",
        env: {
          SSH_TEXT_ROLES: "gateway",
          SSH_TEXT_GATEWAY_HOST: "0.0.0.0",
          SSH_TEXT_GATEWAY_PORT: "7910",
        },
      },
      {
        name: "host-frontend",
        env: {
          SSH_TEXT_ROLES: "frontend",
          SSH_TEXT_FRONTEND_HOST: "0.0.0.0",
          SSH_TEXT_FRONTEND_PORT: "7911",
        },
      },
    ];
  }

  if (profile === "container") {
    return [
      {
        name: "container-backend",
        env: {
          SSH_TEXT_ROLES: "backend",
          SSH_TEXT_BACKEND_HOST: "127.0.0.1",
          SSH_TEXT_BACKEND_PORT: "7912",
          SSH_TEXT_DATA_FILE: ".boxteam/container-backend-note.txt",
          SSH_TEXT_DATA_LABEL: "容器后端",
        },
      },
      {
        name: "container-gateway",
        env: {
          SSH_TEXT_ROLES: "gateway",
          SSH_TEXT_GATEWAY_HOST: "0.0.0.0",
          SSH_TEXT_GATEWAY_PORT: "7910",
        },
      },
      {
        name: "container-frontend",
        env: {
          SSH_TEXT_ROLES: "frontend",
          SSH_TEXT_FRONTEND_HOST: "0.0.0.0",
          SSH_TEXT_FRONTEND_PORT: "7911",
        },
      },
    ];
  }

  throw new Error(`未知 profile: ${profile}`);
}

const profile = readOptionValue("--profile") || process.env.SSH_TEXT_PROFILE || "host";
const children = [];
let shutdownStarted = false;

function requestChildProcessTreeTermination(entry) {
  void terminateChildProcessTree(entry).catch((error) => {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`[roles] ${message}`);
    process.exitCode = 1;
  });
}

function stopAfterChildFailure(name, details, exitCode) {
  if (shutdownStarted) {
    return;
  }
  shutdownStarted = true;
  removeSignalHandlers();
  console.error(`[roles] ${name} ${details}`);
  for (const entry of children) {
    requestChildProcessTreeTermination(entry);
  }
  process.exitCode = exitCode;
}

for (const processConfig of profileProcesses(profile)) {
  const child = spawn(process.execPath, ["src/server/server.js"], {
    cwd: demoRoot,
    env: {
      ...process.env,
      ...processConfig.env,
    },
    detached: process.platform !== "win32",
    stdio: ["ignore", "pipe", "pipe"],
  });

  children.push({ name: processConfig.name, child });
  pipePrefixedOutput(child.stdout, processConfig.name, process.stdout);
  pipePrefixedOutput(child.stderr, processConfig.name, process.stderr);

  child.once("error", (error) => {
    stopAfterChildFailure(
      processConfig.name,
      `启动失败: ${error.message}`,
      1,
    );
  });
  child.on("exit", (code, signal) => {
    stopAfterChildFailure(
      processConfig.name,
      `退出 code=${code ?? "null"} signal=${signal ?? "null"}`,
      code ?? 1,
    );
  });
}

console.log(`[roles] profile=${profile} started: ${children.map((entry) => entry.name).join(", ")}`);

function shutdown(signal) {
  if (shutdownStarted) {
    return;
  }
  shutdownStarted = true;
  removeSignalHandlers();
  console.log(`[roles] 收到 ${signal}`);
  for (const entry of children) {
    requestChildProcessTreeTermination(entry);
  }
}

function removeSignalHandlers() {
  process.off("SIGINT", handleInterrupt);
  process.off("SIGTERM", handleTermination);
}

function handleInterrupt() {
  shutdown("SIGINT");
}

function handleTermination() {
  shutdown("SIGTERM");
}

process.once("SIGINT", handleInterrupt);
process.once("SIGTERM", handleTermination);
