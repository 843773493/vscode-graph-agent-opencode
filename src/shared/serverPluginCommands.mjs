/** 具名命令复用插件 HTTP listener；实际 handler 和取消由所属实例持有。 */
import { randomUUID } from "node:crypto";
import Ajv from "ajv/dist/2020.js";
import { fromJson, toJson } from "@bufbuild/protobuf";
import {
  PluginCommandCallRequestSchema, PluginCommandCancelRequestSchema, PluginCommandEventDTOSchema,
} from "../workspace-services/protocol/generated/boxteam/gateway/v2/plugin_commands_pb.js";
import { PluginCommandContributionSchema } from "../workspace-services/protocol/generated/boxteam/gateway/v2/service_plugin_pb.js";

function protocol(schema, value) {
  return toJson(schema, fromJson(schema, value), { useProtoFieldName: true, emitDefaultValues: true });
}

function reject(message, statusCode = 409) {
  throw Object.assign(new Error(message), { statusCode });
}

export function createPluginCommands(identity, providedHandlers = {}, parameters = {}) {
  const handlers = Object.freeze({ ...providedHandlers });
  const raw = JSON.parse(process.env.BOXTEAM_PLUGIN_COMMANDS ?? "[]");
  if (!Array.isArray(raw)) throw new TypeError("BOXTEAM_PLUGIN_COMMANDS 必须是数组");
  const declarations = raw.map(value => protocol(PluginCommandContributionSchema, value));
  const declaredHandlers = new Set(declarations.map(command => command.handler));
  if (Object.keys(handlers).length !== declaredHandlers.size
      || Object.keys(handlers).some(name => !declaredHandlers.has(name) || typeof handlers[name] !== "function")) {
    throw new TypeError("实际命令 handler 与插件清单不一致");
  }
  const ajv = new Ajv({ strict: false, allErrors: true });
  const commands = new Map(declarations.map(command => [command.id, {
    ...command, registration_id: randomUUID(),
    argumentsValidator: ajv.compile(command.arguments_schema), resultValidator: ajv.compile(command.result_schema),
  }]));
  const active = new Map();

  function checked(value) {
    for (const field of ["gateway_id", "package_id", "package_version", "plugin_id", "instance_id"]) {
      if (value[field] !== identity[field]) reject("命令不属于原插件实例");
    }
    if ((value.workspace_id ?? null) !== identity.workspace_id) reject("命令工作区不符");
    const command = commands.get(value.command_id);
    if (!command || command.registration_id !== value.registration_id) reject("具名命令注册已经失效");
    return command;
  }

  return {
    registrations: { instance_id: identity.instance_id, commands: [...commands.values()].map(command => ({
      command_id: command.id, handler: command.handler, registration_id: command.registration_id,
    })) },
    async handle(request, response, pathname, readJson, logger) {
      if (pathname === "/internal/plugin/commands") {
        if (request.method !== "GET") reject("命令注册只接受 GET", 405);
        response.writeHead(200, { "content-type": "application/json" });
        response.end(JSON.stringify(this.registrations));
        return;
      }
      if (request.method !== "POST") reject("命令调用与取消只接受 POST", 405);
      const cancel = pathname === "/internal/plugin/commands/cancel";
      const value = protocol(cancel ? PluginCommandCancelRequestSchema : PluginCommandCallRequestSchema,
        await readJson(request));
      const command = checked(value);
      if (cancel) {
        const call = active.get(value.call_id);
        if (call && (call.value.command_id !== value.command_id || call.value.registration_id !== value.registration_id)) {
          reject("取消目标与原调用不符");
        }
        call?.controller.abort(new Error("用户取消命令"));
        response.writeHead(200, { "content-type": "application/json" });
        response.end(JSON.stringify({ instance_id: identity.instance_id, call_id: value.call_id, accepted: !!call }));
        return;
      }
      if (active.has(value.call_id)) reject("原命令调用仍在进行，不重复执行");
      if (!Number.isSafeInteger(value.remaining_ms) || value.remaining_ms <= 0) reject("命令缺少剩余期限", 422);
      if (!command.argumentsValidator(value.arguments)) reject("命令参数不符合声明 schema", 422);
      const requestId = request.headers["x-request-id"];
      if (typeof requestId !== "string" || !requestId || !value.call_id) reject("命令缺少调用身份", 422);
      const controller = new AbortController();
      active.set(value.call_id, { value, controller });
      let sequence = 0, expired = false;
      const timer = setTimeout(() => { expired = true; controller.abort(new Error("命令执行期限已到")); }, value.remaining_ms);
      const disconnected = () => controller.abort(new Error("原命令连接已断开；结果不得自动重放"));
      response.once("close", disconnected);
      response.writeHead(200, { "content-type": "application/x-ndjson" });
      async function emit(kind, fields = {}) {
        const event = protocol(PluginCommandEventDTOSchema, {
          instance_id: identity.instance_id, registration_id: value.registration_id,
          command_id: value.command_id, call_id: value.call_id,
          request_id: requestId, sequence: sequence + 1, kind, ...fields,
        });
        if (response.destroyed) return;
        sequence += 1;
        await new Promise((resolve, reject) => response.write(JSON.stringify(event) + "\n",
          error => error ? reject(error) : resolve()));
      }
      try {
        await emit("started");
        const result = await handlers[command.handler](value.arguments, {
          call: Object.freeze({ ...value }), requestId,
          signal: controller.signal,
          parameters,
          progress: (completed, total, message) => emit("progress", { completed, total, message }),
        });
        controller.signal.throwIfAborted();
        if (result === undefined) throw new TypeError("命令必须返回 JSON 结果，空值请返回 null");
        if (!command.resultValidator(result)) throw new TypeError("命令结果不符合声明 schema");
        await emit("result", { result });
      } catch (error) {
        if (controller.signal.aborted && !expired) await emit("cancelled");
        else {
          logger.error("插件命令失败", error);
          await emit("error", { error_code: expired ? "deadline-exceeded" : "handler-failed", message: error.message });
        }
      } finally {
        clearTimeout(timer);
        response.removeListener("close", disconnected);
        active.delete(value.call_id);
        response.end();
      }
    },
  };
}
