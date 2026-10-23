import type { WorkspaceResourceDTO } from "../../api/workspaceResources";
import type { SessionResource } from "../../types/backend";
import type { ResourceThreadRef } from "../../api/workspaceResources";

export function resourceThreadAssociations(resource: SessionResource): ResourceThreadRef[] {
  if (!["terminal", "browser"].includes(resource.kind)) return [];
  const threadAssociations = resource.metadata.thread_associations;
  if (!Array.isArray(threadAssociations)) throw new Error("资源投影缺少权威 Thread 关联集合");
  return threadAssociations.map((association: unknown) => {
    assertResourceThreadAssociation(association);
    return { session_id: association.session_id, thread_id: association.thread_id };
  });
}

export function resourcePreviewThread(resource: SessionResource, sessionId: string): ResourceThreadRef | undefined {
  if (resource.metadata.retention_scope === "workspace") return undefined;
  if (!["terminal", "browser"].includes(resource.kind)) {
    throw new Error("临时资源没有当前会话的有效 Thread 关联，请在资源入口重新关联或保留");
  }
  const threadAssociations = resource.metadata.thread_associations;
  if (!Array.isArray(threadAssociations)) throw new Error("资源投影缺少权威 Thread 关联集合");
  let matchingAssociation: ResourceThreadRef | undefined;
  for (const association of threadAssociations) {
    assertResourceThreadAssociation(association);
    if (!matchingAssociation && association.session_id === sessionId) {
      matchingAssociation = {
        session_id: association.session_id,
        thread_id: association.thread_id,
      };
    }
  }
  if (!matchingAssociation) throw new Error("临时资源没有当前会话的有效 Thread 关联，请在资源入口重新关联或保留");
  return matchingAssociation;
}

function assertResourceThreadAssociation(value: unknown): asserts value is ResourceThreadRef {
  if (!value || typeof value !== "object" || !("session_id" in value) || !("thread_id" in value)
    || typeof value.session_id !== "string" || typeof value.thread_id !== "string") {
    throw new Error("资源投影包含无效的 Thread 关联");
  }
}


// 只生成行展示，不另存 owner 生命周期事实。
export function workspaceResourceDisplay(resource: WorkspaceResourceDTO): SessionResource {
  if (resource.kind !== "browser" && resource.kind !== "terminal") throw new Error("资源类型不支持连接展示");
  return {
    resource_id: resource.resource_id, kind: resource.kind,
    session_id: resource.source_thread!.session_id,
    name: typeof resource.metadata?.title === "string" ? resource.metadata.title : resource.resource_id,
    status: resource.status, created_at: resource.created_at, updated_at: resource.updated_at,
    available_actions: resource.available_actions.flatMap((action) =>
      action === "stop" ? ["cancel" as const] : action === "delete" ? ["delete" as const] : []),
    metadata: { ...resource.metadata, ...resource },
  };
}
