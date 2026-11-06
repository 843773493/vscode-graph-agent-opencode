import { listWorkspaceResources, type ResourceAction, type ResourceThreadRef, type WorkspaceConnectionKind, type WorkspaceResourceDTO } from "../../api/workspaceResources";
import { errorMessage } from "../../utils/errorMessage";

export const resourceActionLabels: Record<ResourceAction, string> = {
  promote: "保留到工作区", attach: "关联 Thread", detach: "解除关联", stop: "停止资源", delete: "删除资源",
};

export function workspaceResourceName(resource: WorkspaceResourceDTO): string {
  const title = resource.metadata?.title;
  return typeof title === "string" && title ? title : resource.resource_id;
}

/** 句柄只属于发起 Hook 的资源快照，命令不得把旧按钮转投到新范围。 */
export interface WorkspaceResourceActionSource {
  workspaceKey: string;
  resource: WorkspaceResourceDTO;
  run(action: ResourceAction, thread?: ResourceThreadRef): Promise<WorkspaceResourceDTO>;
}
export type DispatchWorkspaceResourceAction = (action: ResourceAction, source: WorkspaceResourceActionSource,
  thread?: ResourceThreadRef) => Promise<WorkspaceResourceDTO | undefined>;

/** 成功只用完整回执；失败向原 owner 回读，不重放结果未知的写入。 */
export async function settleWorkspaceResourceOperation(apiPort: number, workspaceKey: string, kind: WorkspaceConnectionKind,
  operation: () => Promise<WorkspaceResourceDTO>, canRead: () => boolean) {
  try { return { succeeded: true as const, resource: await operation() }; }
  catch (cause) {
    let error = errorMessage(cause);
    if (!canRead()) return { succeeded: false as const, cause, error, snapshot: null };
    try {
      const snapshot = await listWorkspaceResources(apiPort, workspaceKey, kind);
      return { succeeded: false as const, cause, error, snapshot };
    } catch (refreshCause) {
      error += `；刷新失败：${errorMessage(refreshCause)}`;
      return { succeeded: false as const, cause, error, snapshot: null };
    }
  }
}
