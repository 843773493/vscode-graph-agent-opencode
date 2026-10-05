import { useCallback, useEffect, useState } from "react";

export interface NodeDebugOwnerSelection {
  workspaceId: string | null;
  sessionId: string | null;
  threadId: string | null;
}

interface NodeDebugSessionOwner {
  workspaceId: string | null;
  sessionId: string | null;
}

export function resolveNodeDebugThreadId(
  selection: NodeDebugOwnerSelection,
  owner: NodeDebugSessionOwner,
  mainThreadId: string | null,
): string | null {
  return selection.workspaceId === owner.workspaceId
    && selection.sessionId === owner.sessionId
    && selection.threadId !== null
    ? selection.threadId
    : mainThreadId;
}

export function synchronizeNodeDebugOwner(
  selection: NodeDebugOwnerSelection,
  owner: NodeDebugSessionOwner,
  mainThreadId: string | null,
): NodeDebugOwnerSelection {
  if (
    selection.workspaceId === owner.workspaceId
    && selection.sessionId === owner.sessionId
  ) {
    return selection;
  }
  return { ...owner, threadId: mainThreadId };
}

/** 当前会话的调试线程选择；会话或工作区切换时立即回到 main。 */
export function useNodeDebugOwner(
  owner: NodeDebugSessionOwner,
  mainThreadId: string | null,
): {
  threadId: string | null;
  mainThreadId: string | null;
  selectThread: (threadId: string) => void;
} {
  const [selection, setSelection] = useState<NodeDebugOwnerSelection>({
    ...owner,
    threadId: mainThreadId,
  });
  const threadId = resolveNodeDebugThreadId(selection, owner, mainThreadId);

  useEffect(() => {
    setSelection((previous) => synchronizeNodeDebugOwner(previous, owner, mainThreadId));
  }, [mainThreadId, owner.sessionId, owner.workspaceId]);

  const selectThread = useCallback((nextThreadId: string) => {
    setSelection({ ...owner, threadId: nextThreadId });
  }, [owner.sessionId, owner.workspaceId]);

  return { threadId, mainThreadId, selectThread };
}
