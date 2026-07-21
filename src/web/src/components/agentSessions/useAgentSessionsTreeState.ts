import { useCallback, useState } from "react";

function toggleSetValue(values: Set<string>, value: string): Set<string> {
  const next = new Set(values);
  if (next.has(value)) {
    next.delete(value);
  } else {
    next.add(value);
  }
  return next;
}

export function useAgentSessionsTreeState() {
  const [collapsedWorkspaceIds, setCollapsedWorkspaceIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [collapsedSessionIds, setCollapsedSessionIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [expandedRootTreeIds, setExpandedRootTreeIds] = useState<Set<string>>(
    () => new Set(),
  );

  const toggleWorkspace = useCallback((workspaceId: string) => {
    setCollapsedWorkspaceIds((current) => toggleSetValue(current, workspaceId));
  }, []);

  const expandWorkspace = useCallback((workspaceId: string) => {
    setCollapsedWorkspaceIds((current) => {
      if (!current.has(workspaceId)) {
        return current;
      }
      const next = new Set(current);
      next.delete(workspaceId);
      return next;
    });
  }, []);

  const toggleSession = useCallback((sessionId: string) => {
    setCollapsedSessionIds((current) => toggleSetValue(current, sessionId));
  }, []);

  const toggleRootList = useCallback((treeId: string) => {
    setExpandedRootTreeIds((current) => toggleSetValue(current, treeId));
  }, []);

  return {
    collapsedWorkspaceIds,
    collapsedSessionIds,
    expandedRootTreeIds,
    toggleWorkspace,
    expandWorkspace,
    toggleSession,
    toggleRootList,
  };
}
