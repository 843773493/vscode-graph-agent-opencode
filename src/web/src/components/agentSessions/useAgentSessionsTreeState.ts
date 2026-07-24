import { useCallback, useMemo } from "react";
import {
  stableUiSettingIds,
  type AgentSessionsPreferences,
} from "../../state/uiSettings/preferences";
import type { WebUiSessionSidebarSettings } from "../../types/backend";

function toggleSetValue(values: Set<string>, value: string): Set<string> {
  const next = new Set(values);
  if (next.has(value)) {
    next.delete(value);
  } else {
    next.add(value);
  }
  return next;
}

export function useAgentSessionsTreeState({
  preferences,
  onPreferencesChange,
}: {
  preferences: AgentSessionsPreferences;
  onPreferencesChange: (
    updater: (
      current: WebUiSessionSidebarSettings,
    ) => Partial<WebUiSessionSidebarSettings>,
  ) => void;
}) {
  const collapsedWorkspaceIds = useMemo(
    () => new Set(preferences.collapsedWorkspaceIds),
    [preferences.collapsedWorkspaceIds],
  );
  const collapsedSessionIds = useMemo(
    () => new Set(preferences.collapsedSessionIds),
    [preferences.collapsedSessionIds],
  );
  const expandedRootTreeIds = useMemo(
    () => new Set(preferences.expandedRootTreeIds),
    [preferences.expandedRootTreeIds],
  );

  const toggleWorkspace = useCallback((workspaceId: string) => {
    onPreferencesChange((current) => ({
      collapsed_workspace_ids: stableUiSettingIds(
        toggleSetValue(new Set(current.collapsed_workspace_ids), workspaceId),
      ),
    }));
  }, [onPreferencesChange]);

  const expandWorkspace = useCallback((workspaceId: string) => {
    onPreferencesChange((current) => ({
      collapsed_workspace_ids: current.collapsed_workspace_ids.filter(
        (currentWorkspaceId) => currentWorkspaceId !== workspaceId,
      ),
    }));
  }, [onPreferencesChange]);

  const toggleSession = useCallback((sessionId: string) => {
    onPreferencesChange((current) => ({
      collapsed_session_ids: stableUiSettingIds(
        toggleSetValue(new Set(current.collapsed_session_ids), sessionId),
      ),
    }));
  }, [onPreferencesChange]);

  const toggleRootList = useCallback((treeId: string) => {
    onPreferencesChange((current) => ({
      expanded_root_tree_ids: stableUiSettingIds(
        toggleSetValue(new Set(current.expanded_root_tree_ids), treeId),
      ),
    }));
  }, [onPreferencesChange]);

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
