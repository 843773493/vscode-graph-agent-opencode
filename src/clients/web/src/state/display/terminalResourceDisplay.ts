import type { SessionResource } from "../../types/backend";
import type { ResourceThreadRef } from "../../api/workspaceResources";

export function terminalThreadAssociations(resource: SessionResource): ResourceThreadRef[] {
  if (resource.kind !== "terminal") return [];
  const refs = resource.metadata.thread_associations;
  if (!Array.isArray(refs)) throw new Error("Terminal 投影缺少权威 Thread 关联集合");
  return refs.map((ref: unknown) => {
    if (!ref || typeof ref !== "object" || !("session_id" in ref) || !("thread_id" in ref)
      || typeof ref.session_id !== "string" || typeof ref.thread_id !== "string") {
      throw new Error("Terminal 投影包含无效的 Thread 关联");
    }
    return { session_id: ref.session_id, thread_id: ref.thread_id };
  });
}

export function terminalPreviewThread(resource: SessionResource, sessionId: string): ResourceThreadRef | undefined {
  if (resource.metadata.retention_scope === "workspace") return undefined;
  const thread = terminalThreadAssociations(resource).find((ref) => ref.session_id === sessionId);
  if (!thread) throw new Error("临时终端没有当前会话的有效 Thread 关联，请在工作区终端面板重新关联或保留");
  return thread;
}
