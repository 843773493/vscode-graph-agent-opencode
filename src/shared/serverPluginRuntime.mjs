import { fromJson, toJson } from "@bufbuild/protobuf";
import { PluginInstanceStatusSchema } from "../workspace-services/protocol/generated/boxteam/gateway/v2/service_plugin_pb.js";
import {
  handleRuntimeLoggingRequest,
  verifyLocalRuntimeControlRequest,
  verifyRuntimeControlToken,
} from "./serverRuntimeControl.mjs";
import { runtimeLoggingState } from "./serverRuntimeLogging.mjs";

function invalidEnvironment(name, detail) {
  throw new TypeError(`${name} ${detail}`);
}

function parseInstanceIdentity(raw) {
  let value;
  try {
    value = JSON.parse(raw);
  } catch (cause) {
    throw new TypeError("BOXTEAM_PLUGIN_INSTANCE 必须是合法 JSON", { cause });
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    invalidEnvironment("BOXTEAM_PLUGIN_INSTANCE", "必须是对象");
  }
  let identity;
  try {
    identity = toJson(PluginInstanceStatusSchema, fromJson(PluginInstanceStatusSchema, {
      ...value, status: "ready", process_id: 1, capabilities: [], log_sink: null,
    }), { useProtoFieldName: true });
  } catch (cause) {
    throw new TypeError("BOXTEAM_PLUGIN_INSTANCE 不符合生成的插件管理合同", { cause });
  }
  if (!Array.isArray(value.services)) invalidEnvironment("BOXTEAM_PLUGIN_INSTANCE", "services 必须是数组");
  for (const field of ["gateway_id", "package_id", "plugin_id", "package_version", "instance_id"]) {
    if (typeof identity[field] !== "string" || !identity[field].trim()) {
      invalidEnvironment("BOXTEAM_PLUGIN_INSTANCE", `${field} 必须是非空字符串`);
    }
  }
  if (identity.workspace_id !== undefined && (typeof identity.workspace_id !== "string" || !identity.workspace_id.trim())) {
    invalidEnvironment("BOXTEAM_PLUGIN_INSTANCE", "workspace_id 必须是非空字符串或省略");
  }
  if (!(identity.instance_scope === "gateway" || identity.instance_scope === "workspace")
      || ((identity.instance_scope === "workspace") !== (identity.workspace_id !== undefined))) {
    invalidEnvironment("BOXTEAM_PLUGIN_INSTANCE", "instance_scope 与 workspace_id 不匹配");
  }
  if (identity.management_protocol_version !== 1 || identity.extension_contract_version !== 1) {
    invalidEnvironment("BOXTEAM_PLUGIN_INSTANCE", "管理或扩展合同版本不受支持");
  }
  const services = identity.services ?? [];
  return Object.freeze({
    ...identity,
    workspace_id: identity.workspace_id ?? null,
    services: Object.freeze(services.map((service) => Object.freeze(service))),
  });
}

function readRequiredEnvironment(name) {
  const value = process.env[name];
  if (typeof value !== "string" || !value.trim()) invalidEnvironment(name, "必须提供");
  return value;
}

function readPluginRuntimeEnvironment() {
  const portText = readRequiredEnvironment("BOXTEAM_PLUGIN_PORT");
  if (!/^[0-9]+$/.test(portText)) invalidEnvironment("BOXTEAM_PLUGIN_PORT", "必须是 1–65535 的端口");
  const port = Number(portText);
  if (!Number.isSafeInteger(port) || port < 1 || port > 65535) {
    invalidEnvironment("BOXTEAM_PLUGIN_PORT", "必须是 1–65535 的端口");
  }
  return Object.freeze({
    port,
    token: readRequiredEnvironment("BOXTEAM_RUNTIME_CONTROL_TOKEN"),
    identity: parseInstanceIdentity(readRequiredEnvironment("BOXTEAM_PLUGIN_INSTANCE")),
  });
}

function sendJson(response, statusCode, value) {
  const body = JSON.stringify(value);
  response.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
  });
  response.end(body);
}

/**
 * 为受管 Node 插件提供统一管理合同；监听、业务路由和关闭时的资源核实仍归服务自身。
 * @param {{ service: string, logger: { error(...values: unknown[]): void }, readJson: (request: AsyncIterable<Uint8Array>) => Promise<unknown>, shutdown: () => void | Promise<void> }} options
 */
export function createPluginRuntimeManagement(options) {
  const environment = readPluginRuntimeEnvironment();
  if (typeof options.service !== "string" || !options.service.trim()) {
    throw new TypeError("插件服务名必须是非空字符串");
  }
  if (typeof options.shutdown !== "function") throw new TypeError("插件必须提供原服务关闭函数");

  return Object.freeze({
    ...environment,
    async handleRequest(request, response, pathname) {
      if (await handleRuntimeLoggingRequest(request, response, {
        pathname, service: options.service, readJson: options.readJson, token: environment.token,
      })) return true;
      if (pathname !== "/internal/plugin/status" && pathname !== "/internal/plugin/shutdown") return false;

      verifyLocalRuntimeControlRequest(request);
      verifyRuntimeControlToken(request.headers["x-local-token"], environment.token);
      if (pathname === "/internal/plugin/status") {
        if (request.method !== "GET") {
          throw Object.assign(new Error("插件状态只接受 GET"), { statusCode: 405 });
        }
        sendJson(response, 200, {
          ...environment.identity,
          status: "ready",
          process_id: process.pid,
          capabilities: ["status", "shutdown"],
          log_sink: runtimeLoggingState(options.service).log_sink,
        });
        return true;
      }

      if (request.method !== "POST") {
        throw Object.assign(new Error("插件关闭只接受 POST"), { statusCode: 405 });
      }
      const body = await options.readJson(request);
      if (body === null || typeof body !== "object" || Array.isArray(body) || Object.keys(body).length !== 0) {
        throw Object.assign(new TypeError("插件关闭不接受请求载荷"), { statusCode: 400 });
      }
      const receipt = { accepted: true, process_id: process.pid, instance_id: environment.identity.instance_id };
      response.once("finish", () => {
        void Promise.resolve().then(options.shutdown).catch((error) => {
          try {
            options.logger.error("插件关闭任务失败", error);
          } finally {
            process.exit(1);
          }
        });
      });
      sendJson(response, 200, receipt);
      return true;
    },
  });
}

export { readPluginRuntimeEnvironment };
