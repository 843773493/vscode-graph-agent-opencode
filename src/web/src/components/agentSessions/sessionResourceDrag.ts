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
  | {
      kind: "workspace_folder";
      nodeId: string;
      parentNodeId: string | null;
    }
  | {
      kind: "workspace";
      nodeId: string;
      workspaceId: string;
      navigationParentNodeId: string | null;
      parentWorkspaceId: string | null;
    }
  | { kind: "session_folder"; nodeId: string; workspaceId: string }
  | { kind: "session"; nodeId: string; sessionId: string; workspaceId: string };

export type SessionResourceDropAction =
  | {
      kind: "move_workspace_navigation";
      parentNodeId: string | null;
      placement: WorkspaceNavigationPlacement;
    }
  | {
      kind: "set_workspace_parent";
      parentWorkspaceId: string;
      navigationParentNodeId: string | null;
      placement: WorkspaceNavigationPlacement;
    }
  | { kind: "move_catalog_node"; parentNodeId: string | null };

export type SessionResourceDropZone = "before" | "inside" | "after";

export type WorkspaceNavigationPlacement =
  | { mode: "before" | "after"; targetNodeId: string }
  | { mode: "last" };

export type SessionResourceDropDecision =
  | { allowed: true; action: SessionResourceDropAction }
  | { allowed: false; reason: string };

const WORKSPACE_DROP_EDGE_RATIO = 0.25;

export function workspaceDropZoneForPointer(
  clientY: number,
  top: number,
  height: number,
): SessionResourceDropZone {
  if (height <= 0) {
    return "inside";
  }
  const ratio = (clientY - top) / height;
  if (ratio <= WORKSPACE_DROP_EDGE_RATIO) {
    return "before";
  }
  if (ratio >= 1 - WORKSPACE_DROP_EDGE_RATIO) {
    return "after";
  }
  return "inside";
}

function relativePlacement(
  zone: Exclude<SessionResourceDropZone, "inside">,
  targetNodeId: string,
): WorkspaceNavigationPlacement {
  return { mode: zone, targetNodeId };
}

export function decideSessionResourceDrop(
  source: SessionResourceDragItem,
  target: SessionResourceDropTarget,
  zone: SessionResourceDropZone = "inside",
): SessionResourceDropDecision {
  if (source.kind === "workspace_folder") {
    if (target.kind === "navigation_root") {
      return {
        allowed: true,
        action: {
          kind: "move_workspace_navigation",
          parentNodeId: null,
          placement: { mode: "last" },
        },
      };
    }
    if (target.kind === "workspace") {
      if (zone === "inside") {
        return { allowed: false, reason: "工作区文件夹不能放入工作区" };
      }
      if (target.parentWorkspaceId !== null) {
        return {
          allowed: false,
          reason: "工作区文件夹不能插入子工作区列表",
        };
      }
      return {
        allowed: true,
        action: {
          kind: "move_workspace_navigation",
          parentNodeId: target.navigationParentNodeId,
          placement: relativePlacement(zone, target.nodeId),
        },
      };
    }
    if (target.kind !== "workspace_folder") {
      return { allowed: false, reason: "工作区文件夹只能放入或插入工作区文件夹层级" };
    }
    if (source.nodeId === target.nodeId) {
      return { allowed: false, reason: "工作区文件夹不能放入自身" };
    }
    return {
      allowed: true,
      action: {
        kind: "move_workspace_navigation",
        parentNodeId: zone === "inside" ? target.nodeId : target.parentNodeId,
        placement: zone === "inside"
          ? { mode: "last" }
          : relativePlacement(zone, target.nodeId),
      },
    };
  }

  if (source.kind === "workspace") {
    if (target.kind === "workspace") {
      if (source.workspaceId === target.workspaceId) {
        return { allowed: false, reason: "工作区不能成为自己的子工作区" };
      }
      if (zone !== "inside") {
        if (target.parentWorkspaceId === null) {
          return {
            allowed: true,
            action: {
              kind: "move_workspace_navigation",
              parentNodeId: target.navigationParentNodeId,
              placement: relativePlacement(zone, target.nodeId),
            },
          };
        }
        return {
          allowed: true,
          action: {
            kind: "set_workspace_parent",
            parentWorkspaceId: target.parentWorkspaceId,
            navigationParentNodeId: target.navigationParentNodeId,
            placement: relativePlacement(zone, target.nodeId),
          },
        };
      }
      return {
        allowed: true,
        action: {
          kind: "set_workspace_parent",
          parentWorkspaceId: target.workspaceId,
          navigationParentNodeId: target.navigationParentNodeId,
          placement: { mode: "last" },
        },
      };
    }
    if (target.kind === "navigation_root" || target.kind === "workspace_folder") {
      const parentNodeId = target.kind === "workspace_folder"
        ? zone === "inside" ? target.nodeId : target.parentNodeId
        : null;
      return {
        allowed: true,
        action: {
          kind: "move_workspace_navigation",
          parentNodeId,
          placement: target.kind === "workspace_folder" && zone !== "inside"
            ? relativePlacement(zone, target.nodeId)
            : { mode: "last" },
        },
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
