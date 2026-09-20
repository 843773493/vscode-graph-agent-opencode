import { useCallback, useEffect, useState } from "react";

export interface NodeDebugOwnerSelection {
  workspaceId: string | null;
  sessionId: string | null;
  threadId: string;
}

interface NodeDebugSessionOwner {
  workspaceId: string | null;
  sessionId: string | null;
}

export function resolveNodeDebugThreadId(
  selection: NodeDebugOwnerSelection,
  owner: NodeDebugSessionOwner,
): string {
  return selection.workspaceId === owner.workspaceId
    && selection.sessionId === owner.sessionId
    ? selection.threadId
    : "main";
}

export function synchronizeNodeDebugOwner(
  selection: NodeDebugOwnerSelection,
  owner: NodeDebugSessionOwner,
): NodeDebugOwnerSelection {
  if (
    selection.workspaceId === owner.workspaceId
    && selection.sessionId === owner.sessionId
  ) {
    return selection;
  }
  return { ...owner, threadId: "main" };
}

/** 当前会话的调试线程选择；会话或工作区切换时立即回到 main。 */
export function useNodeDebugOwner(owner: NodeDebugSessionOwner): {
  threadId: string;
  selectThread: (threadId: string) => void;
} {
  const [selection, setSelection] = useState<NodeDebugOwnerSelection>({
    ...owner,
    threadId: "main",
  });
  const threadId = resolveNodeDebugThreadId(selection, owner);

  useEffect(() => {
    setSelection((previous) => synchronizeNodeDebugOwner(previous, owner));
  }, [owner.sessionId, owner.workspaceId]);

  const selectThread = useCallback((nextThreadId: string) => {
    setSelection({ ...owner, threadId: nextThreadId });
  }, [owner.sessionId, owner.workspaceId]);

  return { threadId, selectThread };
}
