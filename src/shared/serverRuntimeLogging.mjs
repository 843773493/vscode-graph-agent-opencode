import { randomUUID } from "node:crypto";
import { fstatSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { formatWithOptions } from "node:util";

const contracts = new Map();
const runtimeInstanceId = randomUUID();
let settings = null;
let appliedRevision = null;
let logSink = readRuntimeLogSink();
let logDescriptors = [1, 2];

export function registerRuntimeLogSink(sink, descriptors) {
  if (!Array.isArray(descriptors) || descriptors.length === 0 || descriptors.some((value) => !Number.isInteger(value) || value < 0)) {
    throw new TypeError("产品日志去向缺少实际描述符");
  }
  const frozen = Object.freeze({ ...sink });
  verifiedRuntimeLogSink(frozen, descriptors);
  logSink = frozen;
  logDescriptors = [...descriptors];
}

function readRuntimeLogSink() {
  if (process.env.BOXTEAM_RUNTIME_LOG_SINK === undefined) return null;
  const sink = JSON.parse(process.env.BOXTEAM_RUNTIME_LOG_SINK);
  const fields = ["path", "owner_id", "role", "source", "device", "inode"];
  if (sink === null || typeof sink !== "object" || Array.isArray(sink)
      || Object.keys(sink).length !== fields.length
      || fields.some((field) => typeof sink[field] !== "string" || !sink[field])
      || !path.isAbsolute(sink.path)) throw new Error("受管进程日志去向非法");
  return Object.freeze(sink);
}

function verifiedRuntimeLogSink(sink = logSink, descriptors = logDescriptors) {
  if (sink === null) return null;
  // bigint 避免 inode 丢失精度；核验实际持有的文件或继承的 stdout/stderr。
  const identities = [...descriptors.map((descriptor) => fstatSync(descriptor, { bigint: true })), statSync(sink.path, { bigint: true })];
  if (identities.some((identity) => String(identity.dev) !== sink.device || String(identity.ino) !== sink.inode)) {
    throw new Error("受管进程实际日志描述符与冻结去向不一致");
  }
  return { ...sink };
}

export class RuntimeLoggingConflictError extends Error {
  statusCode = 409;
}

export function loadRuntimeLogContract(applicationRoot = process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd()) {
  const resource = path.resolve(applicationRoot, "resources/logging/runtime-contract.json");
  if (!contracts.has(resource)) {
    const payload = JSON.parse(readFileSync(resource, "utf8"));
    if (payload.version !== 1) throw new Error("不支持的产品日志合同版本");
    contracts.set(resource, {
      ...payload,
      rootFor(home) { return path.resolve(home, ...payload.root); },
      pathFor(root, role, instance = null) {
        if (!Object.hasOwn(payload.paths, role)) throw new Error("进程日志角色非法");
        const parts = payload.paths[role];
        if (parts.at(-1).includes("{instance}")) {
          if (instance === null || !/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(String(instance))) throw new Error("进程日志实例标识非法");
        } else if (instance !== null) {
          throw new Error("进程日志角色不接受实例参数");
        }
        return path.join(root, ...parts.map((part) => part.replace("{instance}", String(instance))));
      },
    });
  }
  return contracts.get(resource);
}

export function configureRuntimeLogging({ level, pretty } = {}, contract = loadRuntimeLogContract()) {
  const normalizedLevel = level === undefined ? contract.record.defaults.level : level;
  const normalizedPretty = pretty === undefined ? contract.record.defaults.pretty : pretty;
  if (typeof normalizedLevel !== "string" || !Object.hasOwn(contract.record.levels, normalizedLevel.trim().toLowerCase())) {
    throw new Error("不支持的日志级别");
  }
  if (typeof normalizedPretty !== "boolean") throw new TypeError("日志 pretty 必须是布尔值");
  settings = { level: normalizedLevel.trim().toLowerCase(), pretty: normalizedPretty };
  appliedRevision = null;
  return { ...settings, pid: process.pid, runtime_instance_id: runtimeInstanceId };
}

export function runtimeLoggingState(service, contract = loadRuntimeLogContract()) {
  if (settings === null) configureRuntimeLogging({}, contract);
  return {
    service, pid: process.pid, runtime_instance_id: runtimeInstanceId,
    applied_revision: appliedRevision, ...settings, log_sink: verifiedRuntimeLogSink(),
  };
}

export function applyRuntimeLogging(payload, service, contract = loadRuntimeLogContract(), logger = null) {
  const fields = ["runtime_instance_id", "revision", "expected_revision", "level", "pretty"];
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)
      || Object.keys(payload).length !== fields.length
      || fields.some((field) => !Object.hasOwn(payload, field))
      || typeof payload.runtime_instance_id !== "string" || !payload.runtime_instance_id
      || typeof payload.revision !== "string" || !payload.revision.trim()
      || (payload.expected_revision !== null && typeof payload.expected_revision !== "string")
      || typeof payload.level !== "string"
      || !Object.hasOwn(contract.record.levels, payload.level.trim().toLowerCase())
      || typeof payload.pretty !== "boolean") {
    const error = new TypeError("日志应用参数非法");
    error.statusCode = 422;
    throw error;
  }
  const current = runtimeLoggingState(service, contract);
  const level = payload.level.trim().toLowerCase();
  if (payload.runtime_instance_id !== runtimeInstanceId) {
    throw new RuntimeLoggingConflictError("日志目标运行实例已变化");
  }
  if (payload.revision === appliedRevision) {
    if (level !== settings.level || payload.pretty !== settings.pretty) {
      throw new RuntimeLoggingConflictError("同一日志 revision 的设置不一致");
    }
    return current;
  }
  if (payload.expected_revision !== appliedRevision) {
    throw new RuntimeLoggingConflictError("日志当前 revision 已变化");
  }
  configureRuntimeLogging({ level, pretty: payload.pretty }, contract);
  appliedRevision = payload.revision;
  (logger ?? createRuntimeLogger(service, "runtime.logging", null, contract)).debug("应用产品日志设置: revision=%s", payload.revision);
  return runtimeLoggingState(service, contract);
}

export function formatRuntimeLogRecord(record, pretty, contract = loadRuntimeLogContract()) {
  const context = contract.record.context_fields
    .filter((key) => record.context?.[key] !== undefined && record.context[key] !== null)
    .map((key) => ` ${key}=${JSON.stringify(record.context[key])}`).join("");
  const values = { ...record, level_upper: record.level.toUpperCase(), context };
  const template = pretty ? contract.record.pretty : contract.record.compact;
  const rendered = template.replace(/\{([a-z_]+)\}/g, (_match, key) => String(values[key]));
  return record.exception ? `${rendered}\n${record.exception}` : rendered;
}

/** 产品诊断只走这个入口；PTY、页面 console 与第三方流不经过本模块。 */
export function createRuntimeLogger(service, logger, stream = null, providedContract = null) {
  const write = (level, values, context) => {
    const contract = providedContract ?? loadRuntimeLogContract();
    if (settings === null) configureRuntimeLogging({}, contract);
    if (contract.record.levels[level] < contract.record.levels[settings.level]) return;
    const record = {
      timestamp: new Date().toISOString(), level, service, logger,
      pid: process.pid, runtime_instance_id: runtimeInstanceId,
      message: formatWithOptions({ colors: false }, ...values), context,
    };
    // 写入失败直接交给调用方，不能再向同一个失败 sink 递归记录。
    (stream ?? process.stderr).write(`${formatRuntimeLogRecord(record, settings.pretty, contract)}\n`);
  };
  const bindContext = (context) => ({
    ...Object.fromEntries(["debug", "info", "warning", "error", "critical"]
      .map((level) => [level, (...values) => write(level, values, context)])),
    withContext: (additional) => bindContext({ ...context, ...additional }),
  });
  return bindContext({});
}
