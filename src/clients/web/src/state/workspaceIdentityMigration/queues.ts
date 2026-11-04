import { fileIntentScopeKey, type FileIntentScope } from "../workspaceFiles/intentStore";
import { recoveryForGateway, recoverPartitionGateway, recoverWorkspaceOwner, record, text, workspaceUuid, type WorkspaceIdentityRecovery } from "./material";

export function recoverFileScope(value: unknown, materials: readonly WorkspaceIdentityRecovery[]): FileIntentScope {
  const scope = record(value, "文件操作 scope");
  const gatewayId = text(scope.gatewayId, "文件操作 Gateway");
  const workspaceRoot = text(scope.workspaceRoot, "文件操作资源根");
  if (scope.actor !== "local" || !workspaceRoot.startsWith("boxteam://")) throw new Error("文件操作原 actor／URI 无效");
  if (scope.workspaceId !== undefined) return { gatewayId, workspaceId: workspaceUuid(scope.workspaceId), workspaceRoot, actor: "local" };
  const material = recoveryForGateway(materials, gatewayId);
  const owner = recoverWorkspaceOwner(material, text(scope.gatewayWorkspaceId, "文件操作旧别名"));
  if (workspaceUuid(scope.backendWorkspaceId) !== owner.workspaceId) throw new Error("文件操作 Backend UUID 与恢复映射冲突");
  return { ...owner, workspaceRoot, actor: "local" };
}

export function recoverFileScopeKey(key: unknown, materials: readonly WorkspaceIdentityRecovery[]): string {
  if (typeof key !== "string") throw new Error("文件操作分区键必须为字符串");
  const parts = key.split("\u0000");
  if (parts.length === 4) return fileIntentScopeKey(recoverFileScope({ gatewayId: parts[0], workspaceId: parts[1], workspaceRoot: parts[2], actor: parts[3] }, materials));
  if (parts.length !== 5) throw new Error("旧文件操作分区键格式无效");
  return fileIntentScopeKey(recoverFileScope({ gatewayId: parts[0], gatewayWorkspaceId: parts[1], backendWorkspaceId: parts[2], workspaceRoot: parts[3], actor: parts[4] }, materials));
}

export function recoverFileIntent(value: unknown, materials: readonly WorkspaceIdentityRecovery[]): Record<string, unknown> {
  const stored = record(value, "文件操作");
  const scope = recoverFileScope(stored.scope, materials), scopeKey = recoverFileScopeKey(stored.scopeKey, materials);
  if (scopeKey !== fileIntentScopeKey(scope)) throw new Error("文件操作原 scope 与分区键不一致");
  const intent = record(stored.intent, "文件操作 intent"), directory = record(intent.directory_key, "文件操作目录键");
  if (directory.workspace_id !== scope.workspaceId) throw new Error("文件操作目录键与原 Backend UUID 不一致");
  return { ...stored, scope, scopeKey };
}

export function recoverCatalogPartition(key: unknown, materials: readonly WorkspaceIdentityRecovery[]): string {
  if (typeof key !== "string") throw new Error("目录队列分区键必须为字符串");
  const parts = key.split("\u0000");
  if (parts.length !== 3 || !parts.every(Boolean)) throw new Error("目录队列分区键格式无效");
  return [recoverPartitionGateway(recoveryForGateway(materials, parts[0]!), parts[0]!, parts[1]!), parts[1], parts[2]].join("\u0000");
}
