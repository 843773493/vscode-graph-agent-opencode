import { useCallback, useEffect, useRef, useState } from "react";
import { DEFAULT_BACKEND_PORT, getWorkspaceFileContent } from "../api";
import type {
  SessionChangeset,
  SessionFileChange,
  WebUiLayoutSettings,
  WorkspaceFileContent,
  WorkspaceFileNode,
} from "../types/backend";
import type { WorkspacePreviewTab } from "../components/workspace/WorkspaceFilePreviewArea";
import { toEmbeddedAttachUrl } from "../utils/attachUrls";
import type {
  WorkspaceFileReference,
  WorkspaceFileSelection,
} from "../utils/workspaceFileReferences";

interface UseWorkspacePreviewTabsOptions {
  apiPort: number;
  workspaceId: string | null;
  workspaceRoot: string;
  settingsLoaded: boolean;
  restoredLayout: WebUiLayoutSettings;
  onPersistLayout: (layout: WebUiLayoutSettings) => void;
  onStatusChange: (message: string) => void;
}

function previewLayoutKey(layout: WebUiLayoutSettings): string {
  return JSON.stringify({
    visible: layout.workspace_preview_visible ?? false,
    maximized: layout.workspace_preview_maximized ?? false,
    paths: (layout.workspace_preview_file_paths ?? []).slice(-20),
    activePath: layout.workspace_preview_active_file_path ?? null,
  });
}

export function useWorkspacePreviewTabs({
  apiPort,
  workspaceId,
  workspaceRoot,
  settingsLoaded,
  restoredLayout,
  onPersistLayout,
  onStatusChange,
}: UseWorkspacePreviewTabsOptions) {
  const [visible, setVisible] = useState(
    () => restoredLayout.workspace_preview_visible ?? false,
  );
  const [maximized, setMaximized] = useState(
    () => restoredLayout.workspace_preview_maximized ?? false,
  );
  const [tabs, setTabs] = useState<WorkspacePreviewTab[]>([]);
  const [activePath, setActivePath] = useState<string | null>(null);
  const [loadingPath, setLoadingPath] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [persistenceReady, setPersistenceReady] = useState(false);
  const persistLayoutRef = useRef(onPersistLayout);
  const persistedPreviewLayoutKeyRef = useRef(previewLayoutKey(restoredLayout));

  useEffect(() => {
    persistLayoutRef.current = onPersistLayout;
  }, [onPersistLayout]);

  useEffect(() => {
    let cancelled = false;
    setPersistenceReady(false);
    persistedPreviewLayoutKeyRef.current = previewLayoutKey(restoredLayout);
    setTabs([]);
    setActivePath(null);
    setLoadingPath(null);
    setError(null);
    setVisible(restoredLayout.workspace_preview_visible ?? false);
    setMaximized(restoredLayout.workspace_preview_maximized ?? false);

    if (!settingsLoaded || !workspaceRoot) {
      return () => {
        cancelled = true;
      };
    }

    const filePaths = (restoredLayout.workspace_preview_file_paths ?? []).slice(-20);
    if (filePaths.length === 0) {
      setPersistenceReady(true);
      return () => {
        cancelled = true;
      };
    }

    const restoredActivePath = filePaths.includes(
      restoredLayout.workspace_preview_active_file_path ?? "",
    )
      ? restoredLayout.workspace_preview_active_file_path ?? filePaths[0]
      : filePaths[0];
    const placeholderTabs: WorkspacePreviewTab[] = filePaths.map((path) => {
      const pathParts = path.split("/").filter(Boolean);
      return {
        previewType: "file-placeholder",
        path,
        name: pathParts[pathParts.length - 1] ?? path,
      };
    });
    setTabs(placeholderTabs);
    setActivePath(restoredActivePath);
    setLoadingPath(restoredActivePath);
    void getWorkspaceFileContent(
      apiPort ?? DEFAULT_BACKEND_PORT,
      restoredActivePath,
      workspaceId,
    )
      .then((content) => {
        if (cancelled) {
          return;
        }
        setTabs((current) => current.map((tab) =>
          tab.path === content.path
            ? { ...content, previewType: "file", selection: null }
            : tab,
        ));
        onStatusChange(`已恢复 ${filePaths.length} 个文件预览标签`);
      })
      .catch((restoreError: unknown) => {
        if (cancelled) {
          return;
        }
        const message = restoreError instanceof Error
          ? restoreError.message
          : String(restoreError);
        setError(message);
        onStatusChange(`恢复文件预览失败: ${message}`);
      })
      .finally(() => {
        if (!cancelled) {
          setLoadingPath(null);
          setPersistenceReady(true);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [apiPort, settingsLoaded, workspaceId, workspaceRoot]);

  const persistedFilePaths = tabs
    .filter((tab) =>
      tab.previewType === "file" || tab.previewType === "file-placeholder",
    )
    .map((tab) => tab.path)
    .slice(-20);
  const activeFilePath = tabs.some(
    (tab) =>
      (tab.previewType === "file" || tab.previewType === "file-placeholder") &&
      tab.path === activePath,
  )
    ? activePath
    : null;
  const persistedFilePathsKey = JSON.stringify(persistedFilePaths);
  const currentPreviewLayout: WebUiLayoutSettings = {
    workspace_preview_visible: visible,
    workspace_preview_maximized: maximized,
    workspace_preview_file_paths: persistedFilePaths,
    workspace_preview_active_file_path: activeFilePath,
  };
  const currentPreviewLayoutKey = previewLayoutKey(currentPreviewLayout);

  useEffect(() => {
    if (
      !persistenceReady ||
      currentPreviewLayoutKey === persistedPreviewLayoutKeyRef.current
    ) {
      return;
    }
    const timer = window.setTimeout(() => {
      persistedPreviewLayoutKeyRef.current = currentPreviewLayoutKey;
      persistLayoutRef.current(currentPreviewLayout);
    }, 180);
    return () => window.clearTimeout(timer);
  }, [
    activeFilePath,
    currentPreviewLayoutKey,
    maximized,
    persistedFilePathsKey,
    persistenceReady,
    visible,
  ]);

  const openWorkspaceFileContent = useCallback((
    content: WorkspaceFileContent,
    selection: WorkspaceFileSelection | null,
  ) => {
    setVisible(true);
    setActivePath(content.path);
    setLoadingPath(null);
    setError(null);
    setTabs((prev) => [
      ...prev.filter((tab) => tab.path !== content.path),
      { ...content, previewType: "file", selection },
    ]);
    onStatusChange(`已打开预览: ${content.path}`);
  }, [onStatusChange]);

  const selectWorkspacePreviewTab = useCallback((path: string) => {
    const tab = tabs.find((item) => item.path === path);
    setVisible(true);
    setActivePath(path);
    setError(null);
    if (!tab || tab.previewType !== "file-placeholder") {
      return;
    }
    setLoadingPath(path);
    onStatusChange(`正在读取文件: ${path}`);
    void getWorkspaceFileContent(
      apiPort ?? DEFAULT_BACKEND_PORT,
      path,
      workspaceId,
    )
      .then((content) => openWorkspaceFileContent(content, null))
      .catch((openError: unknown) => {
        const message = openError instanceof Error ? openError.message : String(openError);
        setError(message);
        onStatusChange(`文件预览失败: ${message}`);
      })
      .finally(() => setLoadingPath(null));
  }, [apiPort, onStatusChange, openWorkspaceFileContent, tabs, workspaceId]);

  const openWorkspaceFilePreview = (node: WorkspaceFileNode) => {
    if (node.kind !== "file" && node.kind !== "symlink" && node.kind !== "other") {
      return;
    }

    const existingTab = tabs.find((tab) => tab.path === node.path);
    if (existingTab) {
      if (existingTab.previewType === "file-placeholder") {
        selectWorkspacePreviewTab(existingTab.path);
        return;
      }
      setVisible(true);
      setActivePath(existingTab.path);
      setError(null);
      if (existingTab.previewType === "file" && existingTab.selection) {
        setTabs((prev) => prev.map((tab) =>
          tab.path === existingTab.path && tab.previewType === "file"
            ? { ...tab, selection: null }
            : tab,
        ));
      }
      onStatusChange(`已切换预览: ${existingTab.path}`);
      return;
    }

    setVisible(true);
    setActivePath(node.path);
    setLoadingPath(node.path);
    setError(null);
    onStatusChange(`正在读取文件: ${node.path}`);

    void getWorkspaceFileContent(
      apiPort ?? DEFAULT_BACKEND_PORT,
      node.path,
      workspaceId,
    )
      .then((content) => {
        openWorkspaceFileContent(content, null);
      })
      .catch((openError: unknown) => {
        const message = openError instanceof Error ? openError.message : String(openError);
        setError(message);
        onStatusChange(`文件预览失败: ${message}`);
      })
      .finally(() => {
        setLoadingPath(null);
      });
  };

  const openWorkspaceFileReference = useCallback((
    content: WorkspaceFileContent,
    reference: WorkspaceFileReference,
  ) => {
    openWorkspaceFileContent(content, reference.selection);
  }, [openWorkspaceFileContent]);

  const openTerminalPreview = (terminalId: string, attachUrl: string) => {
    const tabPath = `terminal://${terminalId}`;
    setVisible(true);
    setActivePath(tabPath);
    setLoadingPath(null);
    setError(null);
    setTabs((prev) => [
      ...prev.filter((tab) => tab.path !== tabPath),
      {
        previewType: "terminal",
        path: tabPath,
        name: `终端 ${terminalId.slice(0, 8)}`,
        terminalId,
        attachUrl: toEmbeddedAttachUrl(attachUrl),
      },
    ]);
    onStatusChange(`已在预览区连接终端: ${terminalId}`);
  };

  const openBrowserPreview = (browserId: string, attachUrl: string) => {
    const tabPath = `browser://${browserId}`;
    setVisible(true);
    setActivePath(tabPath);
    setLoadingPath(null);
    setError(null);
    setTabs((prev) => [
      ...prev.filter((tab) => tab.path !== tabPath),
      {
        previewType: "browser",
        path: tabPath,
        name: `浏览器 ${browserId.slice(0, 8)}`,
        browserId,
        attachUrl: toEmbeddedAttachUrl(attachUrl),
      },
    ]);
    onStatusChange(`已在预览区连接浏览器: ${browserId}`);
  };

  const openSessionChangePreview = useCallback(
    (changeset: SessionChangeset, file: SessionFileChange) => {
      const tabPath = `session-diff://${changeset.changeset_id}/${encodeURIComponent(file.file_path)}`;
      const filePathParts = file.file_path.split("/").filter(Boolean);
      const name = filePathParts[filePathParts.length - 1] || file.file_path;
      setVisible(true);
      setActivePath(tabPath);
      setLoadingPath(null);
      setError(null);
      setTabs((prev) => [
        ...prev.filter((tab) => tab.path !== tabPath),
        {
          previewType: "session-diff",
          path: tabPath,
          name,
          change: file,
          changesetLabel: changeset.label,
        },
      ]);
      onStatusChange(`已打开会话变更: ${file.file_path}`);
    },
    [onStatusChange],
  );

  const closeWorkspaceFilePreview = (path: string) => {
    setTabs((prev) => {
      const closedIndex = prev.findIndex((tab) => tab.path === path);
      const nextTabs = prev.filter((tab) => tab.path !== path);
      if (activePath === path) {
        const fallbackTab = nextTabs[Math.max(0, closedIndex - 1)] ?? nextTabs[0] ?? null;
        setActivePath(fallbackTab?.path ?? null);
      }
      return nextTabs;
    });
  };

  return {
    visible,
    maximized,
    tabs,
    activePath,
    loadingPath,
    error,
    setVisible,
    setMaximized,
    setActivePath,
    selectWorkspacePreviewTab,
    setError,
    openWorkspaceFilePreview,
    openWorkspaceFileReference,
    openTerminalPreview,
    openBrowserPreview,
    openSessionChangePreview,
    closeWorkspaceFilePreview,
  };
}
