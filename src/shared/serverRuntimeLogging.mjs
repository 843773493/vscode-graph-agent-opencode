import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { formatWithOptions } from "node:util";

const contracts = new Map();
const runtimeInstanceId = randomUUID();
let settings = null;

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
  return { ...settings, pid: process.pid, runtime_instance_id: runtimeInstanceId };
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
