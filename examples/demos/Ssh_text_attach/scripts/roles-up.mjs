import { spawn } from "node:child_process";

function readArg(name) {
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

function pipeWithPrefix(stream, prefix, target) {
  let pending = "";
  stream.setEncoding("utf8");
  stream.on("data", (chunk) => {
    pending += chunk;
    const lines = pending.split("\n");
    pending = lines.pop() ?? "";
    for (const line of lines) {
      target.write(`[${prefix}] ${line}\n`);
    }
  });
  stream.on("end", () => {
    if (pending) {
      target.write(`[${prefix}] ${pending}\n`);
    }
  });
}

const profile = readArg("--profile") || process.env.SSH_TEXT_PROFILE || "host";
const children = [];
let stopping = false;

for (const processConfig of profileProcesses(profile)) {
  const child = spawn(process.execPath, ["src/server/server.js"], {
    env: {
      ...process.env,
      ...processConfig.env,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });

  children.push({ name: processConfig.name, child });
  pipeWithPrefix(child.stdout, processConfig.name, process.stdout);
  pipeWithPrefix(child.stderr, processConfig.name, process.stderr);

  child.on("exit", (code, signal) => {
    if (stopping) {
      return;
    }
    stopping = true;
    console.error(`[roles] ${processConfig.name} 退出 code=${code ?? "null"} signal=${signal ?? "null"}`);
    for (const other of children) {
      if (other.child.pid !== child.pid) {
        other.child.kill("SIGTERM");
      }
    }
    process.exit(code ?? 1);
  });
}

console.log(`[roles] profile=${profile} started: ${children.map((entry) => entry.name).join(", ")}`);

function shutdown(signal) {
  if (stopping) {
    return;
  }
  stopping = true;
  console.log(`[roles] 收到 ${signal}`);
  for (const entry of children) {
    entry.child.kill("SIGTERM");
  }
}

process.once("SIGINT", () => shutdown("SIGINT"));
process.once("SIGTERM", () => shutdown("SIGTERM"));
