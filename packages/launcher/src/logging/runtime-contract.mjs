import { readFileSync } from "node:fs";
import path from "node:path";

const contracts = new Map();

export function loadRuntimeLogContract(applicationRoot = process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd()) {
  const resource = path.resolve(applicationRoot, "resources/logging/runtime-contract.json");
  if (!contracts.has(resource)) {
    const contract = JSON.parse(readFileSync(resource, "utf8"));
    if (contract.version !== 1) throw new Error("不支持的产品日志合同版本");
    contracts.set(resource, contract);
  }
  return contracts.get(resource);
}

export function runtimeLogRoot(boxteamHome, contract = loadRuntimeLogContract()) {
  return path.resolve(boxteamHome, ...contract.root);
}

export function runtimeLogPath(root, role, instance = null, contract = loadRuntimeLogContract()) {
  if (!Object.hasOwn(contract.paths, role)) throw new Error("进程日志角色非法");
  const parts = contract.paths[role];
  if (parts.at(-1).includes("{instance}")) {
    if (instance === null || !/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(String(instance))) {
      throw new Error("进程日志实例标识非法");
    }
  } else if (instance !== null) {
    throw new Error("进程日志角色不接受实例参数");
  }
  return path.join(root, ...parts.map((part) => part.replace("{instance}", String(instance))));
}
