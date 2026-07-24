export type SessionResourceDragItem =
  | {
      kind: "workspace_folder";
      nodeId: string;
      parentNodeId: string | null;
    }
  | {
      kind: "workspace";
      nodeId: string;
      workspaceId: string;
      parentWorkspaceId: string | null;
      parentNodeId: string | null;
    }
  | {
      kind: "session_folder";
      nodeId: string;
      workspaceId: string;
      parentNodeId: string | null;
    }
  | {
      kind: "session";
      nodeId: string;
      sessionId: string;
      workspaceId: string;
      parentNodeId: string | null;
    };

export type SessionResourceDropTarget =
  | { kind: "navigation_root" }
  | { kind: "workspace_folder"; nodeId: string }
  | {
      kind: "workspace";
      nodeId: string;
      workspaceId: string;
      navigationParentNodeId: string | null;
    }
  | { kind: "session_folder"; nodeId: string; workspaceId: string }
  | { kind: "session"; nodeId: string; sessionId: string; workspaceId: string };

export type SessionResourceDropAction =
  | { kind: "move_workspace_navigation"; parentNodeId: string | null }
  | {
      kind: "set_workspace_parent";
      parentWorkspaceId: string;
      navigationParentNodeId: string | null;
    }
  | { kind: "move_catalog_node"; parentNodeId: string | null };

export type SessionResourceDropDecision =
  | { allowed: true; action: SessionResourceDropAction }
  | { allowed: false; reason: string };

export function decideSessionResourceDrop(
  source: SessionResourceDragItem,
  target: SessionResourceDropTarget,
): SessionResourceDropDecision {
  if (source.kind === "workspace_folder") {
    if (target.kind === "navigation_root") {
      if (source.parentNodeId === null) {
        return { allowed: false, reason: "工作区文件夹已经位于导航根" };
      }
      return {
        allowed: true,
        action: { kind: "move_workspace_navigation", parentNodeId: null },
      };
    }
    if (target.kind !== "workspace_folder") {
      return { allowed: false, reason: "工作区文件夹只能放入另一个工作区文件夹" };
    }
    if (source.nodeId === target.nodeId) {
      return { allowed: false, reason: "工作区文件夹不能放入自身" };
    }
    if (source.parentNodeId === target.nodeId) {
      return { allowed: false, reason: "工作区文件夹已经位于该文件夹中" };
    }
    return {
      allowed: true,
      action: { kind: "move_workspace_navigation", parentNodeId: target.nodeId },
    };
  }

  if (source.kind === "workspace") {
    if (target.kind === "workspace") {
      if (source.workspaceId === target.workspaceId) {
        return { allowed: false, reason: "工作区不能成为自己的子工作区" };
      }
      if (source.parentWorkspaceId === target.workspaceId) {
        return { allowed: false, reason: "工作区已经位于该父工作区下" };
      }
      return {
        allowed: true,
        action: {
          kind: "set_workspace_parent",
          parentWorkspaceId: target.workspaceId,
          navigationParentNodeId: target.navigationParentNodeId,
        },
      };
    }
    if (target.kind === "navigation_root" || target.kind === "workspace_folder") {
      const parentNodeId = target.kind === "workspace_folder" ? target.nodeId : null;
      return {
        allowed: true,
        action: { kind: "move_workspace_navigation", parentNodeId },
      };
    }
    return { allowed: false, reason: "工作区只能放入父工作区或工作区文件夹" };
  }

  if (
    target.kind !== "workspace" &&
    target.kind !== "session" &&
    target.kind !== "session_folder"
  ) {
    return { allowed: false, reason: "会话资源只能在所属工作区的会话树内移动" };
  }
  if (source.workspaceId !== target.workspaceId) {
    return { allowed: false, reason: "会话和会话文件夹不能拖到其他工作区" };
  }
  const parentNodeId = target.kind === "workspace" ? null : target.nodeId;
  if (source.nodeId === parentNodeId) {
    return { allowed: false, reason: "会话资源不能放入自身" };
  }
  if (source.parentNodeId === parentNodeId) {
    return { allowed: false, reason: "会话资源已经位于该位置" };
  }
  return {
    allowed: true,
    action: { kind: "move_catalog_node", parentNodeId },
  };
}

export function sessionResourceDropTargetKey(
  target: SessionResourceDropTarget,
): string {
  if (target.kind === "navigation_root") {
    return target.kind;
  }
  return `${target.kind}:${target.nodeId}`;
}
