import { useCallback, useEffect, useRef, useState, type MouseEvent as ReactMouseEvent, type RefObject } from "react";
import {
  addSessionFileTreeShortcut,
  applyFileTreeShortcutToWorkspace,
  createWorkspaceFileDownloadRequest,
  decodeFileTreePath,
  getSessionFileTreeSettings,
  removeSessionFileTreeShortcut,
  revealWorkspaceFileEntry,
  type WorkspaceFileLocation,
} from "../../../api";
import type {
  FileTreeShortcut,
  SessionFileTreeSettings,
  WorkspaceDirectoryRevision,
  WorkspaceDirectoryKey,
  WorkspaceFileList,
  WorkspaceFileNode,
} from "../../../types/backend";
import { errorDisplayMessage } from "../../../utils/errorMessage";
import {
  copyTextToClipboard,
  readFilePathTextFromClipboard,
  readFilePathTextFromClipboardData,
} from "../../../utils/clipboard";
import { filesFromClipboardData, getFileTransferHost } from "../../../utils/fileTransferHost";
import { parentFileTreePath, parseClipboardFilePaths } from "./workspaceFileTreePaths";
import { useWorkspaceFileOperations, type PendingFileCreation } from "./useWorkspaceFileOperations";

export interface FileTreeContextMenuTarget {
  treePath: string;
  uri: string;
  label: string;
  kind: WorkspaceFileNode["kind"];
  shortcutSource: "session" | "workspace" | null;
  x: number;
  y: number;
  createdByOperationId?: string;
}

export interface WorkspaceClipboardEntry {
  location: WorkspaceFileLocation;
  uri: string;
  label: string;
  workspaceId: string | null;
}

/** 右键目标归属目录：目录自身即目标，文件/软链接取其父目录。 */
export function contextTargetDirectory(target: FileTreeContextMenuTarget): string {
  return target.kind === "directory" ? target.treePath : parentFileTreePath(target.treePath);
}

export interface WorkspaceFileTreeContextMenuOptions {
  port: number;
  workspaceId: string | null;
  sessionId: string;
  referenceForTreePath: (treePath: string) => string;
  reconcileDirectories: (directories: readonly WorkspaceDirectoryRevision[], forcePath?: string) => Promise<void>;
  acceptSnapshots: (snapshots: readonly WorkspaceFileList[], forcePath?: string) => void;
  directoryVersion: (path: string) => { key: WorkspaceDirectoryKey; revision: number } | null;
  onCommitted: (directoryPath: string) => void;
  loadDirectory: (path: string, force?: boolean, append?: boolean) => Promise<boolean>;
  acceptFileTreeSettings: (result: SessionFileTreeSettings) => void;
  onStatusChange: (text: string) => void;
}

export interface WorkspaceFileTreeContextMenuApi {
  contextMenu: FileTreeContextMenuTarget | null;
  copiedEntry: WorkspaceClipboardEntry | null;
  actionError: string | null;
  pendingCreations: PendingFileCreation[];
  clearActionError: () => void;
  closeContextMenu: () => void;
  openContextMenu: (
    event: ReactMouseEvent,
    treePath: string,
    label: string,
    kind: WorkspaceFileNode["kind"],
    shortcutSource?: "session" | "workspace" | null,
    createdByOperationId?: string,
  ) => void;
  runContextAction: (failurePrefix: string, action: () => Promise<unknown>) => void;
  /** 与 runContextAction 的区别：只把失败写进状态栏，不弹动作错误横幅。 */
  runStatusAction: (failurePrefix: string, action: () => Promise<unknown>) => void;
  onStatusChange: (text: string) => void;
  requestUpload: (target: FileTreeContextMenuTarget) => void;
  uploadInputRef: RefObject<HTMLInputElement>;
  handleUploadInput: (files: readonly File[]) => void;
  createEntry: (target: FileTreeContextMenuTarget, kind: "file" | "directory") => Promise<void>;
  pasteEntries: (target: FileTreeContextMenuTarget, clipboardText?: string) => Promise<void>;
  copyEntryToClipboard: (target: FileTreeContextMenuTarget) => Promise<void>;
  copyPathToClipboard: (target: FileTreeContextMenuTarget) => Promise<void>;
  downloadEntry: (target: FileTreeContextMenuTarget) => Promise<void>;
  revealEntry: (target: FileTreeContextMenuTarget) => Promise<void>;
  refreshTargetDirectory: (target: FileTreeContextMenuTarget) => Promise<void>;
  addShortcut: (treePath: string, label: string) => Promise<void>;
  addShortcutAndDefault: (treePath: string, label: string) => Promise<void>;
  removeShortcut: (path: string) => Promise<void>;
  removeShortcutAndDefault: (shortcut: FileTreeShortcut) => Promise<void>;
  toggleDefaultShortcut: (shortcut: FileTreeShortcut, isDefault: boolean) => Promise<void>;
}

export async function runCurrentAndDefaultShortcutMutation(
  updateCurrentSession: () => Promise<SessionFileTreeSettings>,
  updateWorkspaceDefault: () => Promise<SessionFileTreeSettings>,
  recoverAuthoritativeState: () => Promise<void>,
): Promise<SessionFileTreeSettings> {
  try {
    await updateCurrentSession();
    return await updateWorkspaceDefault();
  } catch (mutationError) {
    try {
      await recoverAuthoritativeState();
    } catch (recoveryError) {
      throw new AggregateError(
        [mutationError, recoveryError],
        "快捷路径组合操作失败，且重新同步后端状态失败",
      );
    }
    throw mutationError;
  }
}

/**
 * 文件树右键菜单的唯一所有者：菜单目标与位置、剪贴板条目、动作错误横幅，
 * 以及所有菜单触发的文件操作与快捷路径动作。菜单渲染由
 * WorkspaceFileTreeContextMenu 组件承担，两者共享这里的 API。
 */
export function useWorkspaceFileTreeContextMenu({
  port,
  workspaceId,
  sessionId,
  referenceForTreePath,
  reconcileDirectories,
  acceptSnapshots,
  directoryVersion,
  onCommitted,
  loadDirectory,
  acceptFileTreeSettings,
  onStatusChange,
}: WorkspaceFileTreeContextMenuOptions): WorkspaceFileTreeContextMenuApi {
  const [contextMenu, setContextMenu] = useState<FileTreeContextMenuTarget | null>(null);
  const [copiedEntry, setCopiedEntry] = useState<WorkspaceClipboardEntry | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const uploadTargetRef = useRef<FileTreeContextMenuTarget | null>(null);
  const operations = useWorkspaceFileOperations({ port, workspaceId, reconcileDirectories, acceptSnapshots, directoryVersion, onCommitted, onStatusChange,
    onError: (message) => { setActionError(message); onStatusChange(message); } });

  const openContextMenu = (
    event: ReactMouseEvent,
    treePath: string,
    label: string,
    kind: WorkspaceFileNode["kind"],
    shortcutSource: "session" | "workspace" | null = null,
    createdByOperationId?: string,
  ) => {
    event.preventDefault();
    setContextMenu({
      treePath,
      uri: referenceForTreePath(treePath),
      label,
      kind,
      shortcutSource,
      x: event.clientX,
      y: event.clientY,
      createdByOperationId,
    });
  };

  const createEntry = async (
    target: FileTreeContextMenuTarget,
    kind: "file" | "directory",
  ) => {
    const name = window.prompt(kind === "file" ? "新文件名称" : "新文件夹名称");
    if (name === null) {
      return;
    }
    const directoryPath = contextTargetDirectory(target);
    await operations.submit(directoryPath, { kind: "create", name, entryKind: kind }, target.createdByOperationId);
  };

  const pasteEntries = async (
    target: FileTreeContextMenuTarget,
    clipboardText?: string,
  ) => {
    const directoryPath = contextTargetDirectory(target);
    try {
      const sourcePaths = parseClipboardFilePaths(
        clipboardText ?? await readFilePathTextFromClipboard(),
      );
      if (
        copiedEntry
        && sourcePaths.length === 1
        && sourcePaths[0] === copiedEntry.uri
      ) {
        if (copiedEntry.workspaceId !== workspaceId) {
          throw new Error("暂不支持跨工作区粘贴，请在来源工作区下载后再上传");
        }
        await operations.submit(directoryPath, { kind: "copy", sourcePath: copiedEntry.location.path,
          sourceScope: copiedEntry.location.scope }, target.createdByOperationId);
        return;
      }
      await operations.submit(directoryPath, { kind: "paste", sourcePaths }, target.createdByOperationId);
    } catch (error) {
      await loadDirectory(directoryPath, true);
      throw error;
    }
  };

  const uploadEntries = async (
    target: FileTreeContextMenuTarget,
    files: readonly File[],
  ) => {
    const directoryPath = contextTargetDirectory(target);
    try {
      await operations.submit(directoryPath, { kind: "upload", entries: files.map((file) => ({
        file, name: file.name, relativePath: file.webkitRelativePath || file.name,
      })) }, target.createdByOperationId);
    } catch (error) {
      await loadDirectory(directoryPath, true);
      throw error;
    }
  };

  const copyEntryToClipboard = async (target: FileTreeContextMenuTarget) => {
    setCopiedEntry({
      location: decodeFileTreePath(target.treePath),
      uri: target.uri,
      label: target.label,
      workspaceId,
    });
    await copyTextToClipboard(target.uri);
    onStatusChange(`已复制文件: ${target.uri}`);
  };

  const copyPathToClipboard = async (target: FileTreeContextMenuTarget) => {
    await copyTextToClipboard(target.uri);
    onStatusChange(`已复制路径: ${target.uri}`);
  };

  const downloadEntry = async (target: FileTreeContextMenuTarget) => {
    const request = await createWorkspaceFileDownloadRequest(
      port,
      target.treePath,
      target.kind === "directory" ? `${target.label}.zip` : target.label,
      workspaceId,
    );
    await getFileTransferHost().downloadWorkspaceFile(request);
    onStatusChange(`已开始下载: ${target.label}`);
  };

  const revealEntry = async (target: FileTreeContextMenuTarget) => {
    const result = await revealWorkspaceFileEntry(port, target.treePath, workspaceId);
    onStatusChange(`已请求系统显示: ${result.path}`);
  };

  const refreshTargetDirectory = async (target: FileTreeContextMenuTarget) => {
    const directoryPath = contextTargetDirectory(target);
    await loadDirectory(directoryPath, true);
    onStatusChange(`已刷新目录: ${referenceForTreePath(directoryPath)}`);
  };

  const addShortcut = async (treePath: string, label: string) => {
    if (!sessionId) {
      throw new Error("添加快捷路径需要当前会话");
    }
    const result = await addSessionFileTreeShortcut(
      port,
      sessionId,
      { path: referenceForTreePath(treePath), label },
      workspaceId,
    );
    acceptFileTreeSettings(result);
    onStatusChange(`已添加会话快捷路径: ${label}`);
  };

  const refreshShortcutSettings = async () => {
    if (!sessionId) {
      throw new Error("刷新快捷路径需要当前会话");
    }
    acceptFileTreeSettings(await getSessionFileTreeSettings(port, sessionId, workspaceId));
  };

  const addAbsoluteShortcutAndDefault = async (path: string, label: string) => {
    if (!sessionId) {
      throw new Error("添加当前会话和新会话默认快捷路径需要当前会话");
    }
    const result = await runCurrentAndDefaultShortcutMutation(
      () => addSessionFileTreeShortcut(port, sessionId, { path, label }, workspaceId),
      () => applyFileTreeShortcutToWorkspace(port, sessionId, path, label, workspaceId),
      refreshShortcutSettings,
    );
    acceptFileTreeSettings(result);
    onStatusChange(`已将 ${label} 添加到当前会话，并设为新会话默认快捷路径`);
  };

  const addShortcutAndDefault = async (treePath: string, label: string) => {
    await addAbsoluteShortcutAndDefault(referenceForTreePath(treePath), label);
  };

  const removeShortcutAndDefault = async (shortcut: FileTreeShortcut) => {
    if (!sessionId) {
      throw new Error("删除当前会话和新会话默认快捷路径需要当前会话");
    }
    const result = await runCurrentAndDefaultShortcutMutation(
      () => removeSessionFileTreeShortcut(port, sessionId, shortcut.path, "session", workspaceId),
      () => removeSessionFileTreeShortcut(port, sessionId, shortcut.path, "workspace", workspaceId),
      refreshShortcutSettings,
    );
    acceptFileTreeSettings(result);
    onStatusChange(`已从当前会话和新会话默认快捷路径中删除: ${shortcut.path}`);
  };

  const removeShortcut = async (path: string) => {
    if (!sessionId) {
      throw new Error("删除快捷路径需要当前会话");
    }
    const result = await removeSessionFileTreeShortcut(
      port,
      sessionId,
      path,
      "session",
      workspaceId,
    );
    acceptFileTreeSettings(result);
    onStatusChange(`已删除会话快捷路径: ${path}`);
  };

  const toggleDefaultShortcut = async (shortcut: FileTreeShortcut, isDefault: boolean) => {
    if (isDefault) {
      await removeShortcutAndDefault(shortcut);
      return;
    }
    await addAbsoluteShortcutAndDefault(shortcut.path, shortcut.label);
  };

  const requestUpload = (target: FileTreeContextMenuTarget) => {
    uploadTargetRef.current = target;
    uploadInputRef.current?.click();
  };

  const handleUploadInput = (files: readonly File[]) => {
    const target = uploadTargetRef.current;
    uploadTargetRef.current = null;
    if (!target || files.length === 0) {
      return;
    }
    void uploadEntries(target, files).catch((error: unknown) => {
      onStatusChange(`上传本地文件失败: ${errorDisplayMessage(error)}`);
    });
  };

  useEffect(() => {
    if (!contextMenu) {
      return;
    }
    const handlePaste = (event: ClipboardEvent) => {
      event.preventDefault();
      const target = contextMenu;
      setContextMenu(null);
      const files = filesFromClipboardData(event.clipboardData);
      if (files.length > 0) {
        void uploadEntries(target, files).catch((error: unknown) => {
          onStatusChange(`上传本地文件失败: ${errorDisplayMessage(error)}`);
        });
        return;
      }
      let clipboardText: string;
      try {
        clipboardText = readFilePathTextFromClipboardData(event.clipboardData);
      } catch (error) {
        onStatusChange(`粘贴失败: ${errorDisplayMessage(error)}`);
        return;
      }
      void pasteEntries(target, clipboardText).catch((error: unknown) => {
        onStatusChange(`粘贴失败: ${errorDisplayMessage(error)}`);
      });
    };
    const handleCopyShortcut = (event: KeyboardEvent) => {
      if (
        !(event.ctrlKey || event.metaKey)
        || event.key.toLowerCase() !== "c"
        || parentFileTreePath(contextMenu.treePath) === contextMenu.treePath
      ) {
        return;
      }
      event.preventDefault();
      const target = contextMenu;
      setContextMenu(null);
      void copyEntryToClipboard(target).catch((error: unknown) => {
        onStatusChange(`复制文件失败: ${errorDisplayMessage(error)}`);
      });
    };
    window.addEventListener("paste", handlePaste);
    window.addEventListener("keydown", handleCopyShortcut);
    return () => {
      window.removeEventListener("paste", handlePaste);
      window.removeEventListener("keydown", handleCopyShortcut);
    };
  }, [contextMenu, copiedEntry]);

  const runContextAction = useCallback((failurePrefix: string, action: () => Promise<unknown>) => {
    setContextMenu(null);
    setActionError(null);
    void action().catch((error: unknown) => {
      const failureMessage = `${failurePrefix}: ${errorDisplayMessage(error)}`;
      setActionError(failureMessage);
      onStatusChange(failureMessage);
    });
  }, [onStatusChange]);

  const runStatusAction = useCallback((failurePrefix: string, action: () => Promise<unknown>) => {
    void action().catch((error: unknown) => {
      onStatusChange(`${failurePrefix}: ${errorDisplayMessage(error)}`);
    });
  }, [onStatusChange]);

  return {
    pendingCreations: operations.pendingCreations,
    contextMenu,
    copiedEntry,
    actionError,
    clearActionError: () => setActionError(null),
    closeContextMenu: () => setContextMenu(null),
    openContextMenu,
    runContextAction,
    runStatusAction,
    onStatusChange,
    requestUpload,
    uploadInputRef,
    handleUploadInput,
    createEntry,
    pasteEntries,
    copyEntryToClipboard,
    copyPathToClipboard,
    downloadEntry,
    revealEntry,
    refreshTargetDirectory,
    addShortcut,
    addShortcutAndDefault,
    removeShortcut,
    removeShortcutAndDefault,
    toggleDefaultShortcut,
  };
}
