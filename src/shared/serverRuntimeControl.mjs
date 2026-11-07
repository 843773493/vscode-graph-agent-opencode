/** 本机运行时控制复用既有服务端口、原关闭任务与 Gateway 私有凭据。 */
import { timingSafeEqual } from "node:crypto";
import {
  applyRuntimeLogging, runtimeLoggingState,
} from "./serverRuntimeLogging.mjs";

export function verifyRuntimeControlToken(token, expected) {
  let statusCode = 401;
  if (!expected) statusCode = 503;
  else if (typeof token === "string") {
    const actualBytes = Buffer.from(token);
    const expectedBytes = Buffer.from(expected);
    if (actualBytes.length === expectedBytes.length && timingSafeEqual(actualBytes, expectedBytes)) return;
  }
  const error = new Error(statusCode === 503
    ? "此入口未配置本机运行时控制凭据" : "invalid runtime control token");
  error.statusCode = statusCode;
  throw error;
}

function verifyLocalRuntimeControlRequest(request) {
  if (!["127.0.0.1", "::1", "::ffff:127.0.0.1"].includes(request.socket.remoteAddress)
      || request.headers.origin !== undefined || request.headers["sec-fetch-site"] !== undefined) {
    const error = new Error("运行时控制只接受本机管理通道");
    error.statusCode = 403;
    throw error;
  }
}

export function runtimeLoggingControl(payload, { service, token, contract, logger }) {
  verifyRuntimeControlToken(payload.token, token);
  if (payload.action === "status") return runtimeLoggingState(service, contract);
  if (payload.action === "apply") return applyRuntimeLogging(payload.payload, service, contract, logger);
  throw new TypeError("未知日志管理操作");
}

export async function handleRuntimeLoggingRequest(request, response, { pathname, service, readJson }) {
  if (pathname !== "/internal/runtime-logging") return false;
  verifyLocalRuntimeControlRequest(request);
  verifyRuntimeControlToken(request.headers["x-local-token"], process.env.BOXTEAM_RUNTIME_CONTROL_TOKEN);
  let data;
  if (request.method === "GET") data = runtimeLoggingState(service);
  else if (request.method === "POST") data = applyRuntimeLogging(await readJson(request), service);
  else {
    const error = new Error("日志管理只接受 GET 或 POST");
    error.statusCode = 405;
    throw error;
  }
  response.writeHead(200, { "content-type": "application/json; charset=utf-8" });
  response.end(JSON.stringify({ data }));
  return true;
}

export async function handleRuntimeShutdownRequest(request, response, { pathname, workspaceId, readJson, shutdown, logger }) {
  if (pathname !== "/internal/runtime/shutdown") return false;
  verifyLocalRuntimeControlRequest(request);
  verifyRuntimeControlToken(request.headers["x-local-token"], process.env.BOXTEAM_RUNTIME_CONTROL_TOKEN);
  if (request.method !== "POST") {
    throw Object.assign(new Error("运行时关闭只接受 POST"), { statusCode: 405 });
  }
  const payload = await readJson(request);
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)
      || Object.keys(payload).length !== 2 || !Number.isSafeInteger(payload.process_id)
      || typeof payload.workspace_id !== "string") {
    throw Object.assign(new TypeError("运行时关闭需要明确的进程与工作区身份"), { statusCode: 400 });
  }
  if (payload.process_id !== process.pid || payload.workspace_id !== workspaceId) {
    throw Object.assign(new Error("关闭请求的进程或工作区身份已变化"), { statusCode: 409 });
  }
  // 先发送已接受回执，再进入同一 shutdown；原进程 owner 继续核实际退出。
  response.once("finish", () => {
    void Promise.resolve().then(shutdown).catch(error => {
      try {
        logger.error("本机控制关闭发生未处理错误", error);
      } finally {
        process.exit(1);
      }
    });
  });
  response.writeHead(200, { "content-type": "application/json; charset=utf-8" });
  response.end(JSON.stringify({ data: { ...payload, accepted: true } }));
  return true;
}
