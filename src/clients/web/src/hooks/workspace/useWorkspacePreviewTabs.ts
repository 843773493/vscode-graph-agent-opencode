import { useCallback, useEffect, useRef, useState } from "react";
import {
  DEFAULT_BACKEND_PORT,
  getWorkspaceFileContent,
  updateWorkspaceFileContent,
} from "../../api";
import type {
  SessionChangeset,
  SessionFileChange,
  WebUiLayoutSettings,
  WorkspaceFileContent,
  WorkspaceFileNode,
} from "../../types/backend";
import type { WorkspacePreviewTab } from "../../components/workspace/WorkspaceFilePreviewArea";
import { buildGatewayAttachUrl } from "../../utils/attachUrls";
import type {
  WorkspaceFileReference,
  WorkspaceFileSelection,
} from "../../utils/workspaceFileReferences";
import { isWorkspaceTextFilePath } from "../../utils/workspaceFileReferences";
import { errorDisplayMessage } from "../../utils/errorMessage";
import { useWarmConfirm } from "../../components/shell/WarmConfirmProvider";
import { trackInFlightRequest } from "../runtime/inFlightRequests";

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
  const confirm = useWarmConfirm();
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
  const [editingPath, setEditingPath] = useState<string | null>(null);
  const [draftContent, setDraftContent] = useState("");
  const [savingPath, setSavingPath] = useState<string | null>(null);
  const [persistenceReady, setPersistenceReady] = useState(false);
  const persistLayoutRef = useRef(onPersistLayout);
  const persistedPreviewLayoutKeyRef = useRef(previewLayoutKey(restoredLayout));
  const previewWorkspaceIdRef = useRef<string | null | undefined>(undefined);
  // 文件读取的在途归属：每次「切换活动页签」都推进这个序号，只有仍是最新一次的
  // 读取才允许写回活动路径、加载指示与错误通道。否则用户先点 B 再点 C 时，B 的
  // 迟到响应会把活动页签抢回 B，B 的迟到失败还会污染 C 的加载态。
  const fileOpenIntentRef = useRef(0);
  const fileContentRequestsRef = useRef(
    new Map<string, Promise<WorkspaceFileContent>>(),
  );

  const beginFileOpenIntent = useCallback((): number => {
    fileOpenIntentRef.current += 1;
    return fileOpenIntentRef.current;
  }, []);

  const loadWorkspaceFileContent = useCallback((path: string) => {
    const requestKey = [apiPort ?? DEFAULT_BACKEND_PORT, workspaceId ?? "", path].join(":");
    const existing = fileContentRequestsRef.current.get(requestKey);
    if (existing) {
      return existing;
    }
    const request = getWorkspaceFileContent(
      apiPort ?? DEFAULT_BACKEND_PORT,
      path,
      workspaceId,
    );
    trackInFlightRequest(fileContentRequestsRef.current, requestKey, request);
    return request;
  }, [apiPort, workspaceId]);

  useEffect(() => {
    persistLayoutRef.current = onPersistLayout;
  }, [onPersistLayout]);

  useEffect(() => {
    let cancelled = false;
    const intent = beginFileOpenIntent();
    setPersistenceReady(false);
    persistedPreviewLayoutKeyRef.current = previewLayoutKey(restoredLayout);
    setTabs([]);
    setActivePath(null);
    setLoadingPath(null);
    setError(null);
    setEditingPath(null);
    setDraftContent("");
    setSavingPath(null);
    setVisible(restoredLayout.workspace_preview_visible ?? false);
    setMaximized(restoredLayout.workspace_preview_maximized ?? false);

    if (!settingsLoaded || !workspaceRoot) {
      return () => {
        cancelled = true;
      };
    }

    const workspaceChanged = previewWorkspaceIdRef.current !== undefined
      && previewWorkspaceIdRef.current !== workspaceId;
    previewWorkspaceIdRef.current = workspaceId;
    if (workspaceChanged) {
      setVisible(false);
      setPersistenceReady(true);
      onStatusChange("已切换工作区；文件预览已清空，避免显示其他工作区的旧文件");
      return () => {
        cancelled = true;
      };
    }

    const filePaths = (restoredLayout.workspace_preview_file_paths ?? [])
      .filter(isWorkspaceTextFilePath)
      .slice(-20);
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
    void loadWorkspaceFileContent(restoredActivePath)
      .then((content) => {
        if (cancelled || fileOpenIntentRef.current !== intent) {
          return;
        }
        setTabs((current) => current.map((tab) =>
          tab.path === content.path
            ? { ...content, previewType: "file", selection: null }
            : tab,
        ));
        onStatusChange(`已恢复 ${filePaths.length} 个文件选择`);
      })
      .catch((restoreError: unknown) => {
        if (cancelled || fileOpenIntentRef.current !== intent) {
          return;
        }
        const message = errorDisplayMessage(restoreError);
        setError(message);
        onStatusChange(`恢复文件选择失败: ${message}`);
      })
      .finally(() => {
        if (cancelled) {
          return;
        }
        // persistenceReady 是恢复流程的单向闸门，不是某个页签的归属：即使恢复的
        // 读取已被用户的点击顶替，也必须放行，否则布局落库会被永久禁用。
        setPersistenceReady(true);
        if (fileOpenIntentRef.current === intent) {
          setLoadingPath(null);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [beginFileOpenIntent, loadWorkspaceFileContent, settingsLoaded, workspaceId, workspaceRoot]);

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
  const editingTab = editingPath
    ? tabs.find(
        (tab) => tab.path === editingPath && tab.previewType === "file",
      )
    : null;
  const hasUnsavedEdit = Boolean(
    editingTab?.previewType === "file" && draftContent !== editingTab.content,
  );

  useEffect(() => {
    if (!hasUnsavedEdit) {
      return;
    }
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [hasUnsavedEdit]);

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
    onStatusChange(`已选择文件: ${content.path}`);
  }, [onStatusChange]);

  const selectWorkspacePreviewTab = useCallback((path: string) => {
    if (!isWorkspaceTextFilePath(path)) {
      setError(null);
      setLoadingPath(null);
      onStatusChange(`二进制文件不支持文本预览: ${path}`);
      return;
    }
    const tab = tabs.find((item) => item.path === path);
    const intent = beginFileOpenIntent();
    setVisible(true);
    setActivePath(path);
    setError(null);
    if (!tab || tab.previewType !== "file-placeholder") {
      return;
    }
    setLoadingPath(path);
    onStatusChange(`正在读取文件: ${path}`);
    void loadWorkspaceFileContent(path)
      .then((content) => {
        if (fileOpenIntentRef.current !== intent) {
          return;
        }
        openWorkspaceFileContent(content, null);
      })
      .catch((openError: unknown) => {
        if (fileOpenIntentRef.current !== intent) {
          return;
        }
        const message = errorDisplayMessage(openError);
        setError(message);
        onStatusChange(`文件读取失败: ${message}`);
      })
      .finally(() => {
        if (fileOpenIntentRef.current === intent) {
          setLoadingPath(null);
        }
      });
  }, [
    beginFileOpenIntent,
    loadWorkspaceFileContent,
    onStatusChange,
    openWorkspaceFileContent,
    tabs,
  ]);

  const openWorkspaceFilePath = useCallback(async (path: string) => {
    if (!isWorkspaceTextFilePath(path)) {
      setError(null);
      setLoadingPath(null);
      onStatusChange(`二进制文件不支持文本预览: ${path}`);
      return;
    }
    const existingTab = tabs.find((tab) => tab.path === path);
    if (existingTab) {
      if (existingTab.previewType === "file-placeholder") {
        selectWorkspacePreviewTab(existingTab.path);
        return;
      }
      // 直接切到已装载的页签同样是一次新的打开意图：必须作废此前仍在路上的读取，
      // 否则它到达时会把我刚切到的页签抢回去。
      beginFileOpenIntent();
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
      onStatusChange(`已切换文件: ${existingTab.path}`);
      return;
    }

    setVisible(true);
    setActivePath(path);
    setLoadingPath(path);
    setError(null);
    onStatusChange(`正在读取文件: ${path}`);

    const intent = beginFileOpenIntent();

    try {
      const content = await loadWorkspaceFileContent(path);
      if (fileOpenIntentRef.current !== intent) {
        return;
      }
      openWorkspaceFileContent(content, null);
    } catch (openError) {
      if (fileOpenIntentRef.current !== intent) {
        return;
      }
      const message = errorDisplayMessage(openError);
      setError(message);
      onStatusChange(`文件读取失败: ${message}`);
    } finally {
      if (fileOpenIntentRef.current === intent) {
        setLoadingPath(null);
      }
    }
  }, [
    beginFileOpenIntent,
    loadWorkspaceFileContent,
    onStatusChange,
    openWorkspaceFileContent,
    selectWorkspacePreviewTab,
    tabs,
  ]);

  const openWorkspaceFilePreview = (node: WorkspaceFileNode) => {
    if (node.kind !== "file" && node.kind !== "symlink" && node.kind !== "other") {
      return;
    }
    void openWorkspaceFilePath(node.path);
  };

  const openWorkspaceFileReference = useCallback((
    content: WorkspaceFileContent,
    reference: WorkspaceFileReference,
  ) => {
    // 文件引用同样是「切到某个页签」：作废此前在途的读取，避免它到达后抢回页签。
    beginFileOpenIntent();
    openWorkspaceFileContent(content, reference.selection);
  }, [beginFileOpenIntent, openWorkspaceFileContent]);

  const openTerminalPreview = (terminalId: string) => {
    if (!workspaceId) {
      throw new Error("打开终端需要当前会话的 Gateway workspace_id");
    }
    const tabPath = `terminal://${terminalId}`;
    beginFileOpenIntent();
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
        attachUrl: buildGatewayAttachUrl("terminal", workspaceId, terminalId, true),
      },
    ]);
    onStatusChange(`已选择终端: ${terminalId}`);
  };

  const openBrowserPreview = (browserId: string) => {
    if (!workspaceId) {
      throw new Error("打开浏览器需要当前会话的 Gateway workspace_id");
    }
    const tabPath = `browser://${browserId}`;
    beginFileOpenIntent();
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
        attachUrl: buildGatewayAttachUrl("browser", workspaceId, browserId, true),
      },
    ]);
    onStatusChange(`已选择浏览器: ${browserId}`);
  };

  const openSessionChangePreview = useCallback(
    (changeset: SessionChangeset, file: SessionFileChange) => {
      const tabPath = `session-diff://${changeset.changeset_id}/${encodeURIComponent(file.file_path)}`;
      const filePathParts = file.file_path.split("/").filter(Boolean);
      const name = filePathParts[filePathParts.length - 1] || file.file_path;
      // 打开展示差异的新页签同样作废在途的文件读取，避免它到达后抢回页签。
      beginFileOpenIntent();
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
    [beginFileOpenIntent, onStatusChange],
  );

  const beginWorkspaceFileEdit = async (path: string) => {
    const tab = tabs.find((item) => item.path === path);
    if (!tab || tab.previewType !== "file") {
      throw new Error(`只有已加载的文本文件可以编辑: ${path}`);
    }
    if (
      editingPath &&
      editingPath !== path &&
      hasUnsavedEdit &&
      !(await confirm({
        title: "放弃未保存修改",
        message: "当前文件有未保存修改，放弃修改并编辑另一个文件？",
        confirmText: "放弃修改",
        danger: true,
      }))
    ) {
      return;
    }
    setEditingPath(path);
    setDraftContent(tab.content);
    setError(null);
    onStatusChange(`正在编辑: ${path}`);
  };

  const cancelWorkspaceFileEdit = async () => {
    if (
      hasUnsavedEdit &&
      !(await confirm({
        title: "放弃未保存修改",
        message: "放弃当前文件的未保存修改？",
        confirmText: "放弃修改",
        danger: true,
      }))
    ) {
      return;
    }
    setEditingPath(null);
    setDraftContent("");
    setError(null);
    onStatusChange("已退出文件编辑");
  };

  const saveWorkspaceFileEdit = async () => {
    if (!editingPath || !editingTab || editingTab.previewType !== "file") {
      throw new Error("当前没有可保存的文件编辑");
    }
    setSavingPath(editingPath);
    setError(null);
    try {
      const saved = await updateWorkspaceFileContent(
        apiPort ?? DEFAULT_BACKEND_PORT,
        editingPath,
        {
          content: draftContent,
          expected_revision: editingTab.revision,
        },
        workspaceId,
      );
      setTabs((current) => current.map((tab) =>
        tab.path === saved.path && tab.previewType === "file"
          ? {
              ...saved,
              previewType: "file",
              selection: tab.selection,
            }
          : tab,
      ));
      setEditingPath(saved.path);
      setDraftContent(saved.content);
      onStatusChange(`已保存文件: ${saved.path}`);
    } catch (saveError) {
      const message = errorDisplayMessage(saveError);
      setError(message);
      onStatusChange(`保存文件失败: ${message}`);
    } finally {
      setSavingPath(null);
    }
  };

  const closeWorkspaceFilePreview = async (path: string) => {
    if (
      path === editingPath &&
      hasUnsavedEdit &&
      !(await confirm({
        title: "关闭未保存文件",
        message: "该文件有未保存修改，仍要关闭标签吗？",
        confirmText: "关闭标签",
        danger: true,
      }))
    ) {
      return;
    }
    if (path === editingPath) {
      setEditingPath(null);
      setDraftContent("");
    }
    // 关闭页签也是一次新的打开意图：正在路上的该页签读取不得在关闭后把它加回来。
    beginFileOpenIntent();
    setTabs((prev) => {
      const closedIndex = prev.findIndex((tab) => tab.path === path);
      const nextTabs = prev.filter((tab) => tab.path !== path);
      if (activePath === path) {
        const fallbackTab = nextTabs[Math.max(0, closedIndex - 1)] ?? nextTabs[0] ?? null;
        setActivePath(fallbackTab?.path ?? null);
        if (!fallbackTab) {
          setVisible(false);
          setMaximized(false);
        }
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
    editingPath,
    draftContent,
    savingPath,
    hasUnsavedEdit,
    setVisible,
    setMaximized,
    setActivePath,
    selectWorkspacePreviewTab,
    setError,
    openWorkspaceFilePreview,
    openWorkspaceFilePath,
    openWorkspaceFileReference,
    openTerminalPreview,
    openBrowserPreview,
    openSessionChangePreview,
    beginWorkspaceFileEdit,
    setDraftContent,
    cancelWorkspaceFileEdit,
    saveWorkspaceFileEdit,
    closeWorkspaceFilePreview,
  };
}
