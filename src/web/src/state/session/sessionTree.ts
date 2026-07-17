import type { Session } from "../../types/backend";

export interface SessionTreeNode {
  session: Session;
  children: SessionTreeNode[];
}

function sessionSortTime(
  session: Session,
  sortMode: "created" | "updated",
): number {
  const value = sortMode === "created" ? session.created_at : session.updated_at;
  const time = new Date(value || "").getTime();
  return Number.isFinite(time) ? time : 0;
}

function sortNodes(
  nodes: SessionTreeNode[],
  sortMode: "created" | "updated",
): SessionTreeNode[] {
  return nodes.sort(
    (left, right) =>
      sessionSortTime(right.session, sortMode) -
      sessionSortTime(left.session, sortMode),
  );
}

export function buildSessionTree(
  sessions: Session[],
  sortMode: "created" | "updated" = "updated",
): SessionTreeNode[] {
  const nodesById = new Map<string, SessionTreeNode>();
  for (const session of sessions) {
    nodesById.set(session.session_id, { session, children: [] });
  }

  const roots: SessionTreeNode[] = [];
  for (const node of nodesById.values()) {
    const parentId = node.session.parent_session_id;
    const parent = parentId ? nodesById.get(parentId) : undefined;
    if (parent) {
      parent.children.push(node);
    } else {
      roots.push(node);
    }
  }

  const visiting = new Set<string>();
  const visited = new Set<string>();
  const validateAndSort = (node: SessionTreeNode): void => {
    const sessionId = node.session.session_id;
    if (visiting.has(sessionId)) {
      throw new Error(`会话树包含循环关系: ${sessionId}`);
    }
    if (visited.has(sessionId)) {
      return;
    }
    visiting.add(sessionId);
    sortNodes(node.children, sortMode);
    node.children.forEach(validateAndSort);
    visiting.delete(sessionId);
    visited.add(sessionId);
  };

  sortNodes(roots, sortMode).forEach(validateAndSort);
  if (visited.size !== nodesById.size) {
    throw new Error("会话树存在无法从根节点访问的循环关系");
  }
  return roots;
}
