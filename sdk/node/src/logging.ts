export interface RuntimeLogger {
  debug(...values: unknown[]): void;
  info(...values: unknown[]): void;
  warning(...values: unknown[]): void;
  error(...values: unknown[]): void;
  critical(...values: unknown[]): void;
  withContext(context: Record<string, unknown>): RuntimeLogger;
}
export interface RuntimeLogRecord {
  timestamp: string;
  level: string;
  service: string;
  logger: string;
  pid: number;
  runtime_instance_id: string;
  message: string;
  context?: Record<string, unknown>;
  exception?: string;
}
export interface RuntimeLogContract {
  readonly version: number;
  readonly root: readonly string[];
  readonly paths: Record<string, readonly string[]>;
  readonly rotation: { readonly max_bytes: number; readonly archives: number };
  readonly retention: { readonly inactive: { readonly max_bytes: number } };
  readonly record: {
    readonly required_fields: readonly string[];
    readonly context_fields: readonly string[];
    readonly levels: Record<string, number>;
    readonly defaults: { readonly level: string; readonly pretty: boolean };
    readonly pretty: string;
    readonly compact: string;
    readonly timestamp: string;
    readonly raw_output: string;
  };
  fileIdentity(descriptor: number): { device: string; inode: string };
  sinkMatches(sink: RuntimeLogSink, descriptors: number[]): boolean;
  rootFor(home: string): string;
  pathFor(root: string, role: string, instance?: string | null): string;
}
export interface RuntimeLogWriter {
  sink: RuntimeLogSink | null;
  descriptors: number[];
}
export interface RuntimeLoggingUpdate {
  runtime_instance_id: string;
  revision: string;
  expected_revision: string | null;
  level: string;
  pretty: boolean;
}
export interface RuntimeLogSink {
  path: string;
  owner_id: string;
  role: string;
  source: string;
  device: string;
  inode: string;
}
export interface RuntimeLoggingStatus {
  service: string;
  pid: number;
  runtime_instance_id: string;
  applied_revision: string | null;
  level: string;
  pretty: boolean;
  log_sink: RuntimeLogSink | null;
}
import { randomUUID } from "node:crypto";
import { closeSync, fstatSync, openSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { formatWithOptions } from "node:util";

const contracts = new Map<string, RuntimeLogContract>();
const runtimeInstanceId = randomUUID();
let settings: { level: string; pretty: boolean } | null = null;
let appliedRevision: string | null = null;
let logWriter: RuntimeLogWriter = { sink: null, descriptors: [1, 2] };
let logSinkLoaded = false;

function runtimeLogFileIdentity(descriptor: number) {
  const identity = fstatSync(descriptor, { bigint: true });
  return { device: String(identity.dev), inode: String(identity.ino) };
}

function runtimeLogPathIdentity(logPath: string) {
  const descriptor = openSync(logPath, "r");
  try { return runtimeLogFileIdentity(descriptor); }
  finally { closeSync(descriptor); }
}

function runtimeLogSinkMatches(sink: RuntimeLogSink, descriptors: number[]) {
  const identities = [...descriptors.map(runtimeLogFileIdentity), runtimeLogPathIdentity(sink.path)];
  return identities.every((identity) => identity.device === sink.device && identity.inode === sink.inode);
}

export function registerRuntimeLogSink(writer: RuntimeLogWriter): void {
  verifiedRuntimeLogSink(writer);
  logWriter = writer;
  logSinkLoaded = true;
}

function readRuntimeLogSink(): RuntimeLogSink | null {
  if (process.env.BOXTEAM_RUNTIME_LOG_SINK === undefined) return null;
  const sink = JSON.parse(process.env.BOXTEAM_RUNTIME_LOG_SINK);
  const fields = ["path", "owner_id", "role", "source", "device", "inode"];
  if (sink === null || typeof sink !== "object" || Array.isArray(sink)
      || Object.keys(sink).length !== fields.length
      || fields.some((field) => typeof sink[field] !== "string" || !sink[field])
      || !path.isAbsolute(sink.path)) throw new Error("受管进程日志去向非法");
  return Object.freeze(sink);
}

function verifiedRuntimeLogSink(writer?: RuntimeLogWriter) {
  if (writer === undefined && !logSinkLoaded) {
    logWriter = { sink: readRuntimeLogSink(), descriptors: [1, 2] };
    logSinkLoaded = true;
  }
  const { sink, descriptors } = writer ?? logWriter;
  if (sink === null) return null;
  if (!Array.isArray(descriptors) || descriptors.length === 0 || descriptors.some((value) => !Number.isInteger(value) || value < 0)) {
    throw new TypeError("产品日志去向缺少实际描述符");
  }
  if (!runtimeLogSinkMatches(sink, descriptors)) {
    throw new Error("受管进程实际日志描述符与冻结去向不一致");
  }
  return { ...sink };
}

export class RuntimeLoggingConflictError extends Error {
  statusCode = 409;
}

export function loadRuntimeLogContract(): RuntimeLogContract {
  const resource = fileURLToPath(new URL("./resources/logging/runtime-contract.json", import.meta.url));
  if (!contracts.has(resource)) {
    const payload = JSON.parse(readFileSync(resource, "utf8")) as Omit<RuntimeLogContract, "fileIdentity" | "sinkMatches" | "rootFor" | "pathFor">;
    if (payload.version !== 1) throw new Error("不支持的产品日志合同版本");
    contracts.set(resource, {
      ...payload,
      fileIdentity: runtimeLogFileIdentity,
      sinkMatches: runtimeLogSinkMatches,
      rootFor(home) { return path.resolve(home, ...payload.root); },
      pathFor(root, role, instance = null) {
        if (!Object.hasOwn(payload.paths, role)) throw new Error("进程日志角色非法");
        const parts = payload.paths[role];
        if (parts.at(-1)!.includes("{instance}")) {
          if (instance === null || !/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(String(instance))) throw new Error("进程日志实例标识非法");
        } else if (instance !== null) {
          throw new Error("进程日志角色不接受实例参数");
        }
        return path.join(root, ...parts.map((part) => part.replace("{instance}", String(instance))));
      },
    });
  }
  return contracts.get(resource)!;
}

export function configureRuntimeLogging({ level, pretty }: { level?: string; pretty?: boolean } = {}, contract: RuntimeLogContract = loadRuntimeLogContract()): {
  level: string; pretty: boolean; pid: number; runtime_instance_id: string;
} {
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

export function runtimeLoggingState(service: string, contract: RuntimeLogContract = loadRuntimeLogContract()): RuntimeLoggingStatus {
  initializeRuntimeLogging(contract);
  return {
    service, pid: process.pid, runtime_instance_id: runtimeInstanceId,
    applied_revision: appliedRevision, ...settings!, log_sink: verifiedRuntimeLogSink(),
  };
}

function initializeRuntimeLogging(contract: RuntimeLogContract) {
  if (settings !== null) return;
  const serialized = process.env.BOXTEAM_RUNTIME_LOGGING;
  if (serialized === undefined) {
    configureRuntimeLogging({}, contract);
    return;
  }
  const payload = JSON.parse(serialized);
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)
      || Object.keys(payload).length !== 3 || !Object.hasOwn(payload, "level") || !Object.hasOwn(payload, "pretty")
      || typeof payload.revision !== "string" || !payload.revision.trim()
      || typeof payload.level !== "string" || typeof payload.pretty !== "boolean") {
    throw new TypeError("受管进程启动日志设置非法");
  }
  configureRuntimeLogging(payload, contract);
  appliedRevision = payload.revision;
}

export function applyRuntimeLogging(input: unknown, service: string, contract: RuntimeLogContract = loadRuntimeLogContract(), logger: RuntimeLogger | null = null): RuntimeLoggingStatus {
  const payload = input as Partial<RuntimeLoggingUpdate> | null;
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
    Object.assign(error, { statusCode: 422 });
    throw error;
  }
  const current = runtimeLoggingState(service, contract);
  const level = payload.level.trim().toLowerCase();
  if (payload.runtime_instance_id !== runtimeInstanceId) {
    throw new RuntimeLoggingConflictError("日志目标运行实例已变化");
  }
  if (payload.revision === appliedRevision) {
    if (level !== settings!.level || payload.pretty !== settings!.pretty) {
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

export function formatRuntimeLogRecord(record: RuntimeLogRecord, pretty: boolean, contract: RuntimeLogContract = loadRuntimeLogContract()): string {
  const context = contract.record.context_fields
    .filter((key) => record.context?.[key] !== undefined && record.context[key] !== null)
    .map((key) => ` ${key}=${JSON.stringify(record.context![key])}`).join("");
  const values = { ...record, level_upper: record.level.toUpperCase(), context };
  const template = pretty ? contract.record.pretty : contract.record.compact;
  const rendered = template.replace(/\{([a-z_]+)\}/g, (_match, key) => String(values[key as keyof typeof values]));
  return record.exception ? `${rendered}\n${record.exception}` : rendered;
}

/** 产品诊断只走这个入口；PTY、页面 console 与第三方流不经过本模块。 */
export function createRuntimeLogger(service: string, logger: string, stream: Pick<NodeJS.WritableStream, 'write'> | null = null, providedContract: RuntimeLogContract | null = null): RuntimeLogger {
  const write = (level: string, values: unknown[], context: Record<string, unknown>) => {
    const contract = providedContract ?? loadRuntimeLogContract();
    initializeRuntimeLogging(contract);
    if (contract.record.levels[level] < contract.record.levels[settings!.level]) return;
    const record = {
      timestamp: new Date().toISOString(), level, service, logger,
      pid: process.pid, runtime_instance_id: runtimeInstanceId,
      message: formatWithOptions({ colors: false }, ...values), context,
    };
    // 写入失败直接交给调用方，不能再向同一个失败 sink 递归记录。
    (stream ?? process.stderr).write(`${formatRuntimeLogRecord(record, settings!.pretty, contract)}\n`);
  };
  const bindContext = (context: Record<string, unknown>): RuntimeLogger => ({
    debug: (...values) => write("debug", values, context),
    info: (...values) => write("info", values, context),
    warning: (...values) => write("warning", values, context),
    error: (...values) => write("error", values, context),
    critical: (...values) => write("critical", values, context),
    withContext: additional => bindContext({ ...context, ...additional }),
  });
  return bindContext({});
}
