import { spawn } from "node:child_process";
import net from "node:net";
import { performance } from "node:perf_hooks";
import { StringDecoder } from "node:string_decoder";
import { buildSshArguments } from "./sshCommand.js";

const MAX_SSH_STDERR_BYTES = 64 * 1024;
const SSH_TERMINATION_GRACE_MS = 1_000;
const SSH_FORCE_TERMINATION_TIMEOUT_MS = 1_000;

function appendStderrTail(stderrTail, chunk) {
  const { chunks } = stderrTail;
  const nextChunk = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
  if (nextChunk.length === 0) {
    return;
  }
  if (nextChunk.length >= MAX_SSH_STDERR_BYTES) {
    chunks.length = 0;
    stderrTail.startIndex = 0;
    chunks.push(Buffer.from(nextChunk.subarray(-MAX_SSH_STDERR_BYTES)));
    stderrTail.byteLength = MAX_SSH_STDERR_BYTES;
    return;
  }

  chunks.push(nextChunk);
  stderrTail.byteLength += nextChunk.length;
  while (stderrTail.byteLength > MAX_SSH_STDERR_BYTES) {
    const excessBytes = stderrTail.byteLength - MAX_SSH_STDERR_BYTES;
    const firstChunk = chunks[stderrTail.startIndex];
    if (firstChunk.length <= excessBytes) {
      stderrTail.startIndex += 1;
      stderrTail.byteLength -= firstChunk.length;
    } else {
      chunks[stderrTail.startIndex] = firstChunk.subarray(excessBytes);
      stderrTail.byteLength -= excessBytes;
    }
  }
  if (stderrTail.startIndex > 32 && stderrTail.startIndex * 2 >= chunks.length) {
    chunks.splice(0, stderrTail.startIndex);
    stderrTail.startIndex = 0;
  }
}

function waitForTunnelClose(tunnel, timeoutMs) {
  if (tunnel.closeInfo) {
    return Promise.resolve(true);
  }
  return new Promise((resolve) => {
    const timeout = setTimeout(() => finish(false), timeoutMs);
    function finish(closed) {
      clearTimeout(timeout);
      resolve(closed);
    }
    tunnel.closePromise.then(() => finish(true));
  });
}

function stopTunnel(tunnel, {
  terminationGraceMs = SSH_TERMINATION_GRACE_MS,
  forceTerminationTimeoutMs = SSH_FORCE_TERMINATION_TIMEOUT_MS,
} = {}) {
  tunnel.cancelled = true;
  if (!tunnel.child || tunnel.closeInfo) {
    return Promise.resolve();
  }
  if (tunnel.stopPromise) {
    return tunnel.stopPromise;
  }

  tunnel.stopPromise = (async () => {
    let forceTerminationSent = false;
    if (!tunnel.exitInfo) {
      tunnel.child.kill("SIGTERM");
    }
    if (await waitForTunnelClose(tunnel, terminationGraceMs)) {
      return;
    }

    if (!tunnel.exitInfo && !tunnel.closeInfo) {
      tunnel.child.kill("SIGKILL");
      forceTerminationSent = true;
    }
    if (!await waitForTunnelClose(tunnel, forceTerminationTimeoutMs)) {
      const timeoutDetails = forceTerminationSent
        ? `已发送 SIGKILL 后等待 ${forceTerminationTimeoutMs}ms`
        : `子进程已报告退出，但继续等待 ${forceTerminationTimeoutMs}ms 后仍未收到关闭事件`;
      throw new Error(
        `无法关闭 SSH 隧道 ${tunnel.id}: SIGTERM 后等待 ${terminationGraceMs}ms，${timeoutDetails}`,
      );
    }
  })();
  return tunnel.stopPromise;
}

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

function formatExit(exitInfo) {
  if (!exitInfo) {
    return "未退出";
  }
  return `code=${exitInfo.code ?? "null"} signal=${exitInfo.signal ?? "null"}`;
}

export async function waitForRemoteHealth(origin, assertTunnelAlive, fetchRemote = fetch) {
  const deadline = performance.now() + 10_000;
  let lastErrorMessage = "尚未请求";

  while (performance.now() < deadline) {
    assertTunnelAlive();
    try {
      const response = await fetchRemote(`${origin}/health`, {
        signal: AbortSignal.timeout(500),
      });
      const isHealthy = response.ok;
      if (!isHealthy) {
        lastErrorMessage = `HTTP ${response.status}`;
      }
      await response.body?.cancel();
      if (isHealthy) {
        return;
      }
    } catch (error) {
      lastErrorMessage = error instanceof Error ? error.message : String(error);
    }
    await sleep(150);
  }

  assertTunnelAlive();
  throw new Error(`SSH 隧道已启动，但远程后端未就绪: ${origin}/health，最后错误: ${lastErrorMessage}`);
}

export class SshTunnelManager {
  constructor({
    logger = console,
    spawnSsh = spawn,
    fetchRemote = fetch,
    allocatePort = allocateLocalPort,
    terminationGraceMs = SSH_TERMINATION_GRACE_MS,
    forceTerminationTimeoutMs = SSH_FORCE_TERMINATION_TIMEOUT_MS,
  } = {}) {
    this.logger = logger;
    this.spawnSsh = spawnSsh;
    this.fetchRemote = fetchRemote;
    this.allocatePort = allocatePort;
    this.terminationGraceMs = terminationGraceMs;
    this.forceTerminationTimeoutMs = forceTerminationTimeoutMs;
    this.tunnels = new Map();
    this.shuttingDown = false;
    this.shutdownPromise = null;
  }

  backendOriginFor(workspaceTargetConfig) {
    if (this.shuttingDown) {
      throw new Error(`SSH 隧道管理器正在关闭，无法连接目标 ${workspaceTargetConfig.id}`);
    }
    const current = this.tunnels.get(workspaceTargetConfig.id);
    if (current && !current.exitInfo) {
      return current.originPromise;
    }
    if (current) {
      this.tunnels.delete(workspaceTargetConfig.id);
    }

    const tunnel = {
      id: workspaceTargetConfig.id,
      origin: null,
      child: null,
      stderrDecoder: new StringDecoder("utf8"),
      exitInfo: null,
      closeInfo: null,
      spawnError: null,
      stderrTail: { chunks: [], startIndex: 0, byteLength: 0 },
      cancelled: false,
      closePromise: Promise.resolve(),
      stopPromise: null,
      originPromise: null,
    };
    this.tunnels.set(workspaceTargetConfig.id, tunnel);
    tunnel.originPromise = this.#startTunnel(workspaceTargetConfig, tunnel);
    return tunnel.originPromise;
  }

  async #startTunnel(workspaceTargetConfig, tunnel) {
    try {
      const localPort = await this.allocatePort();
      if (tunnel.cancelled) {
        throw new Error(`SSH 隧道启动已取消: ${workspaceTargetConfig.id}`);
      }
      tunnel.origin = `http://127.0.0.1:${localPort}`;
      tunnel.child = this.spawnSsh("ssh", buildSshArguments(workspaceTargetConfig.ssh, localPort), {
        stdio: ["ignore", "ignore", "pipe"],
      });
      tunnel.closePromise = new Promise((resolve) => {
        tunnel.child.once("close", (code, signal) => {
          const trailingStderr = tunnel.stderrDecoder.end();
          if (trailingStderr) {
            this.logger.error(`[ssh:${workspaceTargetConfig.id}] ${trailingStderr.trimEnd()}`);
          }
          tunnel.closeInfo = { code, signal };
          resolve(tunnel.closeInfo);
        });
      });

      tunnel.child.stderr.on("data", (chunk) => {
        appendStderrTail(tunnel.stderrTail, chunk);
        const decodedChunk = tunnel.stderrDecoder.write(chunk);
        if (decodedChunk) {
          this.logger.error(`[ssh:${workspaceTargetConfig.id}] ${decodedChunk.trimEnd()}`);
        }
      });
      tunnel.child.once("error", (error) => {
        tunnel.spawnError = error;
        this.logger.error(`[ssh:${workspaceTargetConfig.id}] 无法管理 SSH 子进程: ${error.message}`);
      });
      tunnel.child.once("exit", (code, signal) => {
        tunnel.exitInfo = { code, signal };
        if (this.tunnels.get(workspaceTargetConfig.id) === tunnel) {
          this.tunnels.delete(workspaceTargetConfig.id);
        }
      });

      const assertTunnelAlive = () => {
        if (tunnel.cancelled) {
          throw new Error(`SSH 隧道启动已取消: ${workspaceTargetConfig.id}`);
        }
        if (tunnel.spawnError) {
          throw new Error(`无法启动 ssh 命令: ${tunnel.spawnError.message}`);
        }
        if (tunnel.exitInfo) {
          const stderrText = Buffer.concat(
            tunnel.stderrTail.chunks.slice(tunnel.stderrTail.startIndex),
          ).toString("utf8").trim();
          throw new Error(`SSH 隧道提前退出，${formatExit(tunnel.exitInfo)}，stderr: ${stderrText || "空"}`);
        }
      };
      await waitForRemoteHealth(tunnel.origin, assertTunnelAlive, this.fetchRemote);
      assertTunnelAlive();

      return tunnel.origin;
    } catch (error) {
      try {
        await stopTunnel(tunnel, {
          terminationGraceMs: this.terminationGraceMs,
          forceTerminationTimeoutMs: this.forceTerminationTimeoutMs,
        });
      } catch (stopError) {
        if (this.tunnels.get(workspaceTargetConfig.id) === tunnel) {
          this.tunnels.delete(workspaceTargetConfig.id);
        }
        throw new AggregateError(
          [error, stopError],
          `SSH 隧道 ${workspaceTargetConfig.id} 启动失败且清理未完成`,
        );
      }
      if (this.tunnels.get(workspaceTargetConfig.id) === tunnel) {
        this.tunnels.delete(workspaceTargetConfig.id);
      }
      throw error;
    }
  }

  shutdown() {
    if (this.shutdownPromise) {
      return this.shutdownPromise;
    }
    this.shuttingDown = true;
    const tunnels = [...this.tunnels.values()];
    this.tunnels.clear();
    this.shutdownPromise = (async () => {
      const stopResults = await Promise.allSettled(tunnels.map((tunnel) => stopTunnel(tunnel, {
        terminationGraceMs: this.terminationGraceMs,
        forceTerminationTimeoutMs: this.forceTerminationTimeoutMs,
      })));
      const stopErrors = stopResults
        .filter((result) => result.status === "rejected")
        .map((result) => result.reason);
      if (stopErrors.length > 0) {
        throw new AggregateError(stopErrors, "一个或多个 SSH 隧道未能关闭");
      }
    })();
    return this.shutdownPromise;
  }
}
