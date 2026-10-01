import { useCallback, useMemo, useState } from "react";
import {
  DEFAULT_SESSION_TITLE,
  createSessionCatalogFolder,
} from "../../api";
import { buildSessionCatalogSyncKeys } from "../sessionResourceExplorer/resourceTreeSync";
import { errorMessage } from "../../utils/errorMessage";
import type { GatewayWorkspace, Session } from "../../types/backend";

export interface SessionNameDialogState {
  sessionId: string;
  workspaceId: string;
  initialTitle: string;
}

interface SessionCatalogActionsInput {
  apiPort: number;
  activeGatewayWorkspaceId: string | null;
  activeSessionId: string | null;
  sessionsByWorkspace: ReadonlyMap<string, Session[]>;
  gatewayWorkspaces: GatewayWorkspace[];
  confirm: (options: {
    title: string;
    message: string;
    confirmText: string;
    danger: boolean;
  }) => Promise<boolean>;
  setStatus: (text: string) => void;
  activateGatewayWorkspace: (
    workspaceId: string,
    preferredSessionId?: string | null,
  ) => Promise<void>;
  createSession: (
    title?: string,
    workspaceId?: string | null,
    folderId?: string | null,
  ) => Promise<Session>;
  openWorkspaceSession: (workspaceId: string, sessionId: string) => Promise<void>;
  removeGatewayWorkspace: (workspaceId: string) => Promise<void>;
  deleteSession: (sessionId: string, workspaceId?: string | null) => Promise<void>;
  renameSession: (
    sessionId: string,
    title: string,
    workspaceId?: string | null,
  ) => Promise<void>;
  forkSessionContext: (
    workspaceId: string,
    sourceSessionId: string,
  ) => Promise<void>;
  setSessionParent: (
    workspaceId: string,
    sessionId: string,
    parentSessionId: string | null,
  ) => Promise<void>;
}

/**
 * AppShell 的会话目录编排链路：新建会话/文件夹、重命名对话框、删除与父子关系调整，
 * 以及让会话目录树重新拉取的刷新信号。目录本身只由后端权威返回，这里不做本地改写。
 */
export function useSessionCatalogActions({
  apiPort,
  activeGatewayWorkspaceId,
  activeSessionId,
  sessionsByWorkspace,
  gatewayWorkspaces,
  confirm,
  setStatus,
  activateGatewayWorkspace,
  createSession,
  openWorkspaceSession,
  removeGatewayWorkspace,
  deleteSession,
  renameSession,
  forkSessionContext,
  setSessionParent,
}: SessionCatalogActionsInput) {
  const [nameDialog, setNameDialog] = useState<SessionNameDialogState | null>(null);
  const [nameDialogSubmitting, setNameDialogSubmitting] = useState(false);
  const [nameDialogError, setNameDialogError] = useState<string | null>(null);
  const [sessionCatalogRefreshVersions, setSessionCatalogRefreshVersions] =
    useState<ReadonlyMap<string, number>>(new Map());

  const sessionCatalogSyncKeys = useMemo(
    () => buildSessionCatalogSyncKeys(sessionsByWorkspace),
    [sessionsByWorkspace],
  );
  const invalidateSessionCatalog = useCallback((workspaceId: string) => {
    setSessionCatalogRefreshVersions((previous) => {
      const next = new Map(previous);
      next.set(workspaceId, (previous.get(workspaceId) ?? 0) + 1);
      return next;
    });
  }, []);

  const createSessionInCatalog = async (workspaceId?: string | null) => {
    setNameDialog(null);
    setNameDialogError(null);
    // 顶部“新建会话”属于当前 Gateway 工作区；只有尚未激活工作区时
    // 才回退到系统默认 home，避免从测试/远程工作区误建到 home。
    const targetWorkspaceId = workspaceId
      ?? activeGatewayWorkspaceId
      ?? gatewayWorkspaces.find((workspace) => workspace.system_default)
        ?.workspace_id;
    if (!targetWorkspaceId) {
      const error = new Error("未找到默认 home 工作区，无法创建会话");
      setStatus(`创建会话失败: ${error.message}`);
      throw error;
    }
    try {
      if (targetWorkspaceId !== activeGatewayWorkspaceId) {
        await activateGatewayWorkspace(targetWorkspaceId);
      }
      await createSession(DEFAULT_SESSION_TITLE, targetWorkspaceId);
      invalidateSessionCatalog(targetWorkspaceId);
    } catch (error) {
      setStatus(`创建会话失败: ${errorMessage(error)}`);
      throw error;
    }
  };
  const createSessionInFolder = async (
    workspaceId: string,
    folderId: string,
  ) => {
    if (workspaceId !== activeGatewayWorkspaceId) {
      await activateGatewayWorkspace(workspaceId);
    }
    await createSession(DEFAULT_SESSION_TITLE, workspaceId, folderId);
    invalidateSessionCatalog(workspaceId);
  };
  const createSessionFolder = async (
    workspaceId: string,
    parentNodeId: string | null,
    name: string,
  ) => {
    await createSessionCatalogFolder(apiPort, workspaceId, name, parentNodeId);
    invalidateSessionCatalog(workspaceId);
  };
  const handleSessionFolderDeleted = async (
    workspaceId: string,
    deletedCurrentSession: boolean,
  ) => {
    if (workspaceId !== activeGatewayWorkspaceId) {
      return;
    }
    await activateGatewayWorkspace(
      workspaceId,
      deletedCurrentSession ? null : activeSessionId,
    );
  };
  const selectAgentSession = async (workspaceId: string, sessionId: string) => {
    await openWorkspaceSession(workspaceId, sessionId);
  };
  const removeWorkspace = (workspaceId: string, workspaceName: string) => {
    const label = workspaceName || workspaceId;
    void confirm({
      title: "删除工作区",
      message: `从 Web Gateway 列表移除工作区“${label}”。会话文件不会被删除。`,
      confirmText: "删除",
      danger: true,
    }).then(async (confirmed) => {
      if (confirmed) {
        await removeGatewayWorkspace(workspaceId);
      }
    }).catch((error: unknown) => {
      setStatus(`删除工作区失败: ${errorMessage(error)}`);
    });
  };
  const openRenameDialog = (
    sessionId: string,
    currentTitle: string,
    workspaceId: string,
  ) => {
    setNameDialog({
      sessionId,
      workspaceId,
      initialTitle: currentTitle || "新会话",
    });
    setNameDialogError(null);
  };
  const removeSession = (
    sessionId: string,
    title: string,
    workspaceId: string,
  ) => {
    const label = title || sessionId;
    void confirm({
      title: "永久删除会话",
      message: `永久删除会话“${label}”。如果它包含子会话，将级联删除整棵子会话树及其消息、检查点、日志、附件和运行资源。此操作无法撤销。`,
      confirmText: "删除",
      danger: true,
    }).then(async (confirmed) => {
      if (!confirmed) {
        return;
      }
      await deleteSession(sessionId, workspaceId);
      invalidateSessionCatalog(workspaceId);
    }).catch((error: unknown) => {
      setStatus(`删除会话失败: ${errorMessage(error)}`);
    });
  };
  const changeSessionParent = async (
    workspaceId: string,
    sessionId: string,
    parentSessionId: string | null,
  ) => {
    await setSessionParent(workspaceId, sessionId, parentSessionId);
    invalidateSessionCatalog(workspaceId);
  };
  const forkSession = async (workspaceId: string, sourceSessionId: string) => {
    await forkSessionContext(workspaceId, sourceSessionId);
    invalidateSessionCatalog(workspaceId);
  };
  const closeNameDialog = () => {
    if (nameDialogSubmitting) {
      return;
    }
    setNameDialog(null);
    setNameDialogError(null);
  };
  const submitNameDialog = (title: string) => {
    if (!nameDialog) {
      return;
    }

    setNameDialogSubmitting(true);
    setNameDialogError(null);
    const action = renameSession(nameDialog.sessionId, title, nameDialog.workspaceId);

    void action
      .then(() => {
        invalidateSessionCatalog(nameDialog.workspaceId);
        setNameDialog(null);
      })
      .catch((error: unknown) => {
        setNameDialogError(errorMessage(error));
      })
      .finally(() => {
        setNameDialogSubmitting(false);
      });
  };

  return {
    nameDialog,
    nameDialogSubmitting,
    nameDialogError,
    sessionCatalogRefreshVersions,
    sessionCatalogSyncKeys,
    invalidateSessionCatalog,
    createSessionInCatalog,
    createSessionInFolder,
    createSessionFolder,
    handleSessionFolderDeleted,
    selectAgentSession,
    removeWorkspace,
    openRenameDialog,
    removeSession,
    changeSessionParent,
    forkSession,
    closeNameDialog,
    submitNameDialog,
  };
}
