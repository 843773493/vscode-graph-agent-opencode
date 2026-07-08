import { spawn } from "node:child_process";
import net from "node:net";

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

async function allocateLocalPort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close();
        reject(new Error("无法分配本地 TCP 端口"));
        return;
      }
      const port = address.port;
      server.close(() => resolve(port));
    });
  });
}

function sshArgs(target, localPort) {
  return [
    "-N",
    "-L",
    `127.0.0.1:${localPort}:${target.ssh.remoteBackendHost}:${target.ssh.remoteBackendPort}`,
    "-i",
    target.ssh.privateKeyPath,
    "-p",
    String(target.ssh.port),
    "-o",
    "ExitOnForwardFailure=yes",
    "-o",
    "IdentitiesOnly=yes",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=2",
    `${target.ssh.user}@${target.ssh.host}`,
  ];
}

function formatExit(exitInfo) {
  if (!exitInfo) {
    return "未退出";
  }
  return `code=${exitInfo.code ?? "null"} signal=${exitInfo.signal ?? "null"}`;
}

async function waitForRemoteHealth(origin, aliveCheck) {
  const deadline = Date.now() + 10_000;
  let lastErrorMessage = "尚未请求";

  while (Date.now() < deadline) {
    aliveCheck();
    try {
      const response = await fetch(`${origin}/health`, {
        signal: AbortSignal.timeout(500),
      });
      if (response.ok) {
        return;
      }
      lastErrorMessage = `HTTP ${response.status}`;
    } catch (error) {
      lastErrorMessage = error instanceof Error ? error.message : String(error);
    }
    await sleep(150);
  }

  aliveCheck();
  throw new Error(`SSH 隧道已启动，但远程后端未就绪: ${origin}/health，最后错误: ${lastErrorMessage}`);
}

export class SshTunnelManager {
  constructor({ logger = console } = {}) {
    this.logger = logger;
    this.tunnels = new Map();
  }

  async backendOriginFor(target) {
    const current = this.tunnels.get(target.id);
    if (current && !current.exitInfo) {
      return current.origin;
    }
    if (current) {
      this.tunnels.delete(target.id);
    }

    const localPort = await allocateLocalPort();
    const origin = `http://127.0.0.1:${localPort}`;
    const stderrChunks = [];
    const child = spawn("ssh", sshArgs(target, localPort), {
      stdio: ["ignore", "ignore", "pipe"],
    });
    const tunnel = {
      id: target.id,
      origin,
      child,
      exitInfo: null,
      spawnError: null,
      stderrChunks,
    };
    this.tunnels.set(target.id, tunnel);

    child.stderr.on("data", (chunk) => {
      stderrChunks.push(chunk);
      this.logger.error(`[ssh:${target.id}] ${chunk.toString("utf8").trimEnd()}`);
    });
    child.once("error", (error) => {
      tunnel.spawnError = error;
    });
    child.once("exit", (code, signal) => {
      tunnel.exitInfo = { code, signal };
      this.tunnels.delete(target.id);
    });

    await waitForRemoteHealth(origin, () => {
      if (tunnel.spawnError) {
        throw new Error(`无法启动 ssh 命令: ${tunnel.spawnError.message}`);
      }
      if (tunnel.exitInfo) {
        const stderrText = Buffer.concat(stderrChunks).toString("utf8").trim();
        throw new Error(`SSH 隧道提前退出，${formatExit(tunnel.exitInfo)}，stderr: ${stderrText || "空"}`);
      }
    });

    return origin;
  }

  closeAll() {
    for (const tunnel of this.tunnels.values()) {
      if (!tunnel.exitInfo) {
        tunnel.child.kill("SIGTERM");
      }
    }
    this.tunnels.clear();
  }
}
