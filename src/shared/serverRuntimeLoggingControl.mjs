/** 本机日志管理只复用既有服务端口和 Gateway 私有凭据。 */
import { timingSafeEqual } from "node:crypto";
import {
  applyRuntimeLogging, runtimeLoggingState,
} from "./serverRuntimeLogging.mjs";

export function verifyRuntimeLoggingToken(token, expected) {
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

export function runtimeLoggingControl(payload, { service, token, contract, logger }) {
  verifyRuntimeLoggingToken(payload.token, token);
  if (payload.action === "status") return runtimeLoggingState(service, contract);
  if (payload.action === "apply") return applyRuntimeLogging(payload.payload, service, contract, logger);
  throw new TypeError("未知日志管理操作");
}

export async function handleRuntimeLoggingRequest(request, response, { pathname, service, readJson }) {
  if (pathname !== "/internal/runtime-logging") return false;
  if (!["127.0.0.1", "::1", "::ffff:127.0.0.1"].includes(request.socket.remoteAddress)
      || request.headers.origin !== undefined || request.headers["sec-fetch-site"] !== undefined) {
    const error = new Error("运行时日志控制只接受本机管理通道");
    error.statusCode = 403;
    throw error;
  }
  verifyRuntimeLoggingToken(request.headers["x-local-token"], process.env.BOXTEAM_RUNTIME_CONTROL_TOKEN);
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
