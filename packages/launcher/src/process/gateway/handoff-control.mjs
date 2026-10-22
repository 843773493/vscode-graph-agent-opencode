import { createHash } from "node:crypto";
import { chmodSync, mkdirSync, rmSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";

import { gatewayStartupContract } from "./startup-contract.mjs";
import { closeServer, listenServer } from "./server-lifecycle.mjs";

export function resolveGatewaySupervisorSocketPath(boxteamHome) {
  if (typeof boxteamHome !== "string" || boxteamHome.trim() === "") {
    return null;
  }
  const resolvedHome = path.resolve(boxteamHome);
  const homeDigest = createHash("sha256").update(resolvedHome).digest("hex").slice(0, 32);
  if (process.platform === "win32") {
    return `\\\\.\\pipe\\boxteam-gateway-supervisor-${homeDigest}`;
  }
  const socketPath = path.join(
    resolvedHome,
    "state",
    "gateway-supervisor.sock",
  );
  // Unix domain socket 的路径有系统上限；长测试输出目录或用户目录不能
  // 让 supervisor 在 listen 后因 chmod 找不到实际 socket 而失败。
  if (Buffer.byteLength(socketPath) <= 100) return socketPath;
  return path.join(tmpdir(), `boxteam-gateway-supervisor-${homeDigest}.sock`);
}

export function createGatewaySupervisorControl({ socketPath, onHandoff }) {
  if (typeof socketPath !== "string" || socketPath.length === 0) {
    throw new TypeError("Gateway supervisor control socket 路径无效");
  }
  if (typeof onHandoff !== "function") {
    throw new TypeError("Gateway supervisor control handler 无效");
  }
  const server = net.createServer((socket) => {
    let buffer = "";
    let handled = false;
    socket.on("error", () => {
      // 控制请求方断开时只结束当前请求，不得让 supervisor 进程崩溃。
    });
    socket.on("data", (chunk) => {
      if (handled) return;
      buffer += chunk.toString("utf8");
      if (buffer.length > 64 * 1024) {
        socket.destroy(new Error("Gateway supervisor control 请求过大"));
        return;
      }
      const newlineIndex = buffer.indexOf("\n");
      if (newlineIndex === -1) return;
      handled = true;
      const line = buffer.slice(0, newlineIndex).trim();
      let payload;
      try {
        payload = JSON.parse(line);
      } catch (error) {
        socket.end(`${JSON.stringify({ ok: false, error: String(error) })}\n`);
        return;
      }
      Promise.resolve()
        .then(() => onHandoff(payload))
        .then((data) => {
          socket.end(`${JSON.stringify({ ok: true, data })}\n`);
        })
        .catch((error) => {
          socket.end(
            `${JSON.stringify({
              ok: false,
              error: error instanceof Error ? error.message : String(error),
            })}\n`,
          );
        });
    });
  });
  return {
    async listen() {
      if (process.platform !== "win32") {
        mkdirSync(path.dirname(socketPath), { recursive: true, mode: 0o700 });
        rmSync(socketPath, { force: true });
      }
      await listenServer(server, socketPath);
      if (process.platform !== "win32") chmodSync(socketPath, 0o600);
    },
    async close() {
      await closeServer(server);
      if (process.platform !== "win32") rmSync(socketPath, { force: true });
    },
  };
}

export function requestGatewayHandoff({
  boxteamHome,
  environment = process.env,
  timeoutMs = 120_000,
}) {
  const socketPath = resolveGatewaySupervisorSocketPath(boxteamHome);
  if (socketPath === null) {
    return Promise.resolve({ handled: false });
  }
  const startup = gatewayStartupContract(environment);
  if (startup.loadedSource !== "pending") {
    throw new Error("Gateway handoff 请求必须携带 pending 启动契约");
  }
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(socketPath);
    let buffer = "";
    let settled = false;
    const timer = setTimeout(() => {
      socket.destroy();
      reject(
        new Error(`Gateway supervisor handoff 在 ${timeoutMs}ms 内未响应`),
      );
    }, timeoutMs);
    timer.unref?.();
    const finish = (callback) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      callback();
    };
    socket.on("connect", () => {
      socket.write(
        `${JSON.stringify({
          type: "gateway_pending_handoff",
          candidate_ref: startup.candidateRef,
          target_generation: startup.generation,
          fencing_token: startup.fencingToken,
        })}\n`,
      );
    });
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      const newlineIndex = buffer.indexOf("\n");
      if (newlineIndex === -1) return;
      let response;
      try {
        response = JSON.parse(buffer.slice(0, newlineIndex));
      } catch (error) {
        finish(() => reject(error));
        return;
      }
      finish(() => {
        if (!response.ok) {
          reject(new Error(String(response.error ?? "Gateway handoff 失败")));
          return;
        }
        resolve({ handled: true, data: response.data ?? null });
      });
    });
    socket.on("error", (error) => {
      if (error.code === "ENOENT" || error.code === "ECONNREFUSED") {
        finish(() => resolve({ handled: false }));
        return;
      }
      finish(() => reject(error));
    });
  });
}
