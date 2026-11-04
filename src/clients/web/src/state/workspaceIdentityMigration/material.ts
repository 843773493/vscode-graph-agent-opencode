import { gatewayWorkspaceKey, type WorkspaceOwner } from "../../api/workspaceIdentity";

export interface WorkspaceIdentityRecovery {
  format_version: 1;
  migration_id: string;
  registry_gateway_id: string;
  mappings: { legacy_workspace_id: string; gateway_id: string; workspace_id: string }[];
}

export function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label}必须是对象`);
  return value as Record<string, unknown>;
}

export function text(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim() || value.includes("\u0000")) throw new Error(`${label}缺少有效字符串`);
  return value;
}

export function workspaceUuid(value: unknown): string {
  const id = text(value, "工作区 UUID");
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id)) throw new Error("工作区身份不是标准 UUID");
  return id;
}

export function parseWorkspaceIdentityRecovery(value: unknown): WorkspaceIdentityRecovery {
  const source = record(value, "工作区身份恢复材料");
  if (source.format_version !== 1 || !Array.isArray(source.mappings)) throw new Error("不支持的工作区身份恢复材料格式");
  const migration_id = workspaceUuid(source.migration_id);
  const registry_gateway_id = text(source.registry_gateway_id, "登记 Gateway");
  const aliases = new Set<string>();
  const mappings = source.mappings.map(value => {
    const mapping = record(value, "恢复映射");
    const legacy_workspace_id = text(mapping.legacy_workspace_id, "旧工作区别名");
    if (aliases.has(legacy_workspace_id)) throw new Error(`恢复材料包含重复旧别名：${legacy_workspace_id}`);
    aliases.add(legacy_workspace_id);
    return { legacy_workspace_id, gateway_id: text(mapping.gateway_id, "原生 Gateway"), workspace_id: workspaceUuid(mapping.workspace_id) };
  });
  return { format_version: 1, migration_id, registry_gateway_id, mappings };
}

export function parseWorkspaceIdentityRecoveries(value: unknown): WorkspaceIdentityRecovery[] {
  const materials = (Array.isArray(value) ? value : [value]).map(parseWorkspaceIdentityRecovery);
  if (!materials.length) throw new Error("请选择至少一份工作区身份恢复材料");
  const gateways = new Set<string>();
  for (const material of materials) {
    if (gateways.has(material.registry_gateway_id)) throw new Error(`同一登记 Gateway 提供了多份恢复材料：${material.registry_gateway_id}`);
    gateways.add(material.registry_gateway_id);
  }
  return materials;
}

export function recoveryForGateway(materials: readonly WorkspaceIdentityRecovery[], gatewayId: string): WorkspaceIdentityRecovery {
  const material = materials.find(value => value.registry_gateway_id === gatewayId);
  if (!material) throw new Error(`缺少登记 Gateway 的恢复材料：${gatewayId}；请同时导入各来源材料`);
  return material;
}

export function recoverWorkspaceOwner(material: WorkspaceIdentityRecovery, reference: string): WorkspaceOwner {
  if (reference.startsWith("[")) {
    const owner: unknown = JSON.parse(reference);
    if (!Array.isArray(owner) || owner.length !== 2) throw new Error("工作区恢复引用缺少完整 owner");
    return { gatewayId: text(owner[0], "原生 Gateway"), workspaceId: workspaceUuid(owner[1]) };
  }
  const mapping = material.mappings.find(value => value.legacy_workspace_id === reference);
  if (!mapping) throw new Error(`恢复材料没有旧工作区别名：${reference}`);
  return { gatewayId: mapping.gateway_id, workspaceId: mapping.workspace_id };
}

export function recoveredWorkspaceKey(material: WorkspaceIdentityRecovery, reference: string): string {
  return gatewayWorkspaceKey(recoverWorkspaceOwner(material, reference));
}

/** 旧队列只有 Backend UUID 时，所有连接映射必须指向同一个原生 owner。 */
export function recoverPartitionGateway(material: WorkspaceIdentityRecovery, gatewayId: string, workspaceId: string): string {
  workspaceUuid(workspaceId);
  if (gatewayId !== material.registry_gateway_id) throw new Error(`旧队列缺少其登记 Gateway 的恢复材料：${gatewayId}`);
  const candidates = new Set(material.mappings.filter(value => value.workspace_id === workspaceId).map(value => value.gateway_id));
  if (candidates.size !== 1) throw new Error(`旧队列无法唯一确认 Gateway 归属：${gatewayId}/${workspaceId}`);
  return [...candidates][0]!;
}

/** 正确的 v2 输入原样保留；无来源的旧输入不能用导入时的 Gateway 补齐。 */
export function recoverThreadEditor(value: unknown, materials: readonly WorkspaceIdentityRecovery[]): Record<string, unknown> {
  const stored = record(value, "Thread 恢复记录");
  if (stored.unavailable) return recoverThreadEditor(stored.unavailable, materials);
  if (stored.version === 2) {
    const target = record(stored.target, "Thread owner");
    text(target.gatewayId, "Thread Gateway");
    workspaceUuid(target.workspaceId);
    text(target.sessionId, "Session");
    text(target.threadId, "Thread");
    return stored;
  }
  if (stored.version !== undefined) throw new Error("不支持的旧 Thread 恢复版本");
  const owner = record(stored.owner, "旧 Thread owner");
  const gatewayId = text(owner.gatewayId, "旧 Thread 来源 Gateway，原记录已保留");
  const workspaceId = text(owner.workspaceId, "旧 Thread 工作区");
  const material = materials.find(value => value.registry_gateway_id === gatewayId);
  let target: WorkspaceOwner;
  if (material?.mappings.some(item => item.legacy_workspace_id === workspaceId)) {
    target = recoverWorkspaceOwner(material, workspaceId);
  } else target = { gatewayId, workspaceId: workspaceUuid(workspaceId) };
  return { version: 2, target: { ...target, sessionId: text(owner.sessionId, "Session"), threadId: text(owner.threadId, "Thread") },
    title: text(owner.title, "Thread 标题"), viewState: stored.viewState };
}
