import { createHash } from "node:crypto";
import { chmodSync, mkdirSync, rmSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { closeServer, listenServer } from "./server-lifetime.mjs";

export function gatewaySupervisorControlAddress(
  boxteamHome,
  { platform = process.platform, temporaryDirectory = tmpdir() } = {},
) {
  if (typeof boxteamHome !== "string" || boxteamHome.trim() === "") return null;
  const paths = platform === "win32" ? path.win32 : path.posix;
  const resolvedHome = paths.resolve(boxteamHome);
  const homeDigest = createHash("sha256")
    .update(resolvedHome).digest("hex").slice(0, 32);
  if (platform === "win32") {
    // 命名管道由内核管理，不创建、chmod或删除同名文件。
    return String.raw`\\.\pipe\boxteam-gateway-supervisor-${homeDigest}`;
  }
  const socketPath = paths.join(resolvedHome, "state", "gateway-supervisor.sock");
  if (Buffer.byteLength(socketPath) <= 100) return socketPath;
  const temporarySocketPath = paths.join(
    temporaryDirectory, `boxteam-gateway-supervisor-${homeDigest}.sock`,
  );
  if (Buffer.byteLength(temporarySocketPath) > 100) {
    throw new Error(`Gateway supervisor control socket 路径超过 100 字节: ${temporarySocketPath}`);
  }
  return temporarySocketPath;
}

export function createGatewaySupervisorControl({ address, onHandoff }) {
  if (typeof address !== "string" || address.length === 0) {
    throw new TypeError("Gateway supervisor control socket 路径无效");
  }
  if (typeof onHandoff !== "function") {
    throw new TypeError("Gateway supervisor control handler 无效");
  }
  const isFilesystemSocketPath = !address.startsWith("\\\\.\\pipe\\");
  const server = net.createServer((socket) => {
    // Socket保留跨数据块的未完成UTF-8字符，不能逐块独立解码。
    socket.setEncoding("utf8");
    let buffer = "";
    let newlineSearchOffset = 0;
    let handled = false;
    socket.on("error", () => {
      // 控制请求方断开时只结束当前请求，不得让 supervisor 进程崩溃。
    });
    socket.on("data", (chunk) => {
      if (handled) return;
      buffer += chunk;
      if (buffer.length > 64 * 1024) {
        socket.destroy(new Error("Gateway supervisor control 请求过大"));
        return;
      }
      const newlineIndex = buffer.indexOf("\n", newlineSearchOffset);
      if (newlineIndex === -1) {
        newlineSearchOffset = buffer.length;
        return;
      }
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
      if (isFilesystemSocketPath) {
        mkdirSync(path.dirname(address), { recursive: true, mode: 0o700 });
        rmSync(address, { force: true });
      }
      await listenServer(server, address);
      if (isFilesystemSocketPath) chmodSync(address, 0o600);
    },
    async close() {
      await closeServer(server);
      if (isFilesystemSocketPath) rmSync(address, { force: true });
    },
  };
}

export function requestGatewaySupervisorControl({ address, payload, timeoutMs }) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(address);
    socket.setEncoding("utf8");
    let buffer = "";
    let newlineSearchOffset = 0;
    let settled = false;
    const timer = setTimeout(() => {
      finish(() => reject(
        new Error(`Gateway supervisor handoff 在 ${timeoutMs}ms 内未响应`),
      ));
    }, timeoutMs);
    timer.unref?.();
    const finish = (callback) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      socket.destroy();
      callback();
    };
    const incompleteResponse = () => finish(() => reject(
      new Error("Gateway supervisor handoff 连接在完整响应前结束"),
    ));
    socket.once("end", incompleteResponse);
    socket.once("close", incompleteResponse);
    socket.on("connect", () => {
      socket.write(
        `${JSON.stringify(payload)}\n`,
      );
    });
    socket.on("data", (chunk) => {
      if (settled) return;
      buffer += chunk;
      const newlineIndex = buffer.indexOf("\n", newlineSearchOffset);
      if (newlineIndex === -1) {
        newlineSearchOffset = buffer.length;
        return;
      }
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
