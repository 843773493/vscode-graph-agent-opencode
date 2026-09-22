import { useCallback, useEffect, useRef, useState, type MouseEvent as ReactMouseEvent, type RefObject } from "react";
import {
  addSessionFileTreeShortcut,
  applyFileTreeShortcutToWorkspace,
  copyWorkspaceFileEntry,
  createWorkspaceFileDownloadRequest,
  createWorkspaceFileEntry,
  decodeFileTreePath,
  getSessionFileTreeSettings,
  pasteWorkspaceFileEntries,
  removeSessionFileTreeShortcut,
  revealWorkspaceFileEntry,
  uploadWorkspaceFileEntries,
  type WorkspaceFileLocation,
} from "../../../api";
import type {
  FileTreeShortcut,
  SessionFileTreeSettings,
  WorkspaceFileList,
  WorkspaceFileNode,
} from "../../../types/backend";
import { errorMessage } from "../../../utils/errorMessage";
import {
  copyTextToClipboard,
  readFilePathTextFromClipboard,
  readFilePathTextFromClipboardData,
} from "../../../utils/clipboard";
import { filesFromClipboardData, getFileTransferHost } from "../../../utils/fileTransferHost";
import { FILESYSTEM_ROOT_PATH, parentFileTreePath, parseClipboardFilePaths, ROOT_PATH } from "./workspaceFileTreePaths";

export interface FileTreeContextMenuTarget {
  treePath: string;
  absolutePath: string;
  label: string;
  kind: WorkspaceFileNode["kind"];
  shortcutSource: "session" | "workspace" | null;
  x: number;
  y: number;
}

export interface WorkspaceClipboardEntry {
  location: WorkspaceFileLocation;
  absolutePath: string;
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
  absolutePathForTreePath: (treePath: string) => string;
  replaceDirectory: (result: WorkspaceFileList) => void;
  loadDirectory: (path: string, force?: boolean, append?: boolean) => Promise<boolean>;
  acceptFileTreeSettings: (result: SessionFileTreeSettings) => void;
  onStatusChange: (text: string) => void;
}

export interface WorkspaceFileTreeContextMenuApi {
  contextMenu: FileTreeContextMenuTarget | null;
  copiedEntry: WorkspaceClipboardEntry | null;
  actionError: string | null;
  clearActionError: () => void;
  closeContextMenu: () => void;
  openContextMenu: (
    event: ReactMouseEvent,
    treePath: string,
    label: string,
    kind: WorkspaceFileNode["kind"],
    shortcutSource?: "session" | "workspace" | null,
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
  absolutePathForTreePath,
  replaceDirectory,
  loadDirectory,
  acceptFileTreeSettings,
  onStatusChange,
}: WorkspaceFileTreeContextMenuOptions): WorkspaceFileTreeContextMenuApi {
  const [contextMenu, setContextMenu] = useState<FileTreeContextMenuTarget | null>(null);
  const [copiedEntry, setCopiedEntry] = useState<WorkspaceClipboardEntry | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const uploadTargetRef = useRef<FileTreeContextMenuTarget | null>(null);

  const openContextMenu = (
    event: ReactMouseEvent,
    treePath: string,
    label: string,
    kind: WorkspaceFileNode["kind"],
    shortcutSource: "session" | "workspace" | null = null,
  ) => {
    event.preventDefault();
    setContextMenu({
      treePath,
      absolutePath: absolutePathForTreePath(treePath),
      label,
      kind,
      shortcutSource,
      x: event.clientX,
      y: event.clientY,
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
    const result = await createWorkspaceFileEntry(
      port,
      directoryPath,
      { name, kind },
      workspaceId,
    );
    replaceDirectory(result);
    onStatusChange(`已创建${kind === "file" ? "文件" : "文件夹"}: ${name}`);
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
        && sourcePaths[0] === copiedEntry.absolutePath
      ) {
        if (copiedEntry.workspaceId !== workspaceId) {
          throw new Error("暂不支持跨工作区粘贴，请在来源工作区下载后再上传");
        }
        const result = await copyWorkspaceFileEntry(
          port,
          directoryPath,
          copiedEntry.location,
          workspaceId,
        );
        replaceDirectory(result);
        onStatusChange(`已粘贴: ${copiedEntry.label}`);
        return;
      }
      const result = await pasteWorkspaceFileEntries(
        port,
        directoryPath,
        { source_paths: sourcePaths },
        workspaceId,
      );
      replaceDirectory(result);
      onStatusChange(`已粘贴 ${sourcePaths.length} 个文件或目录`);
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
      const result = await uploadWorkspaceFileEntries(
        port,
        directoryPath,
        files,
        workspaceId,
      );
      replaceDirectory(result);
      onStatusChange(`已上传 ${files.length} 个本地文件`);
    } catch (error) {
      await loadDirectory(directoryPath, true);
      throw error;
    }
  };

  const copyEntryToClipboard = async (target: FileTreeContextMenuTarget) => {
    setCopiedEntry({
      location: decodeFileTreePath(target.treePath),
      absolutePath: target.absolutePath,
      label: target.label,
      workspaceId,
    });
    await copyTextToClipboard(target.absolutePath);
    onStatusChange(`已复制文件: ${target.absolutePath}`);
  };

  const copyPathToClipboard = async (target: FileTreeContextMenuTarget) => {
    await copyTextToClipboard(target.absolutePath);
    onStatusChange(`已复制路径: ${target.absolutePath}`);
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
    onStatusChange(`已刷新目录: ${absolutePathForTreePath(directoryPath)}`);
  };

  const addShortcut = async (treePath: string, label: string) => {
    if (!sessionId) {
      throw new Error("添加快捷路径需要当前会话");
    }
    const result = await addSessionFileTreeShortcut(
      port,
      sessionId,
      { path: absolutePathForTreePath(treePath), label },
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
    await addAbsoluteShortcutAndDefault(absolutePathForTreePath(treePath), label);
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
      onStatusChange(`上传本地文件失败: ${errorMessage(error)}`);
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
          onStatusChange(`上传本地文件失败: ${errorMessage(error)}`);
        });
        return;
      }
      let clipboardText: string;
      try {
        clipboardText = readFilePathTextFromClipboardData(event.clipboardData);
      } catch (error) {
        onStatusChange(`粘贴失败: ${errorMessage(error)}`);
        return;
      }
      void pasteEntries(target, clipboardText).catch((error: unknown) => {
        onStatusChange(`粘贴失败: ${errorMessage(error)}`);
      });
    };
    const handleCopyShortcut = (event: KeyboardEvent) => {
      if (
        !(event.ctrlKey || event.metaKey)
        || event.key.toLowerCase() !== "c"
        || contextMenu.treePath === ROOT_PATH
        || contextMenu.treePath === FILESYSTEM_ROOT_PATH
      ) {
        return;
      }
      event.preventDefault();
      const target = contextMenu;
      setContextMenu(null);
      void copyEntryToClipboard(target).catch((error: unknown) => {
        onStatusChange(`复制文件失败: ${errorMessage(error)}`);
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
      const failureMessage = `${failurePrefix}: ${errorMessage(error)}`;
      setActionError(failureMessage);
      onStatusChange(failureMessage);
    });
  }, [onStatusChange]);

  const runStatusAction = useCallback((failurePrefix: string, action: () => Promise<unknown>) => {
    void action().catch((error: unknown) => {
      onStatusChange(`${failurePrefix}: ${errorMessage(error)}`);
    });
  }, [onStatusChange]);

  return {
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
