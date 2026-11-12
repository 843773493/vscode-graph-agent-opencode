import { t } from '../../../../shared/core/i18n/index.js';
import { gatewayWorkspaceKey, nullableGatewayWorkspaceKey, workspaceOwnerFromKey } from "../../api/workspaceIdentity";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { catalogOutboxParentCreation } from "../../state/session/sessionCatalogOutbox";
import {
  DEFAULT_SESSION_TITLE,
} from "../../api";
import { fetchSessionCatalogSnapshot } from "../../api/session/sessionCatalogOperations";
import { buildSessionCatalogSyncKeys } from "../sessionResourceExplorer/resourceTreeSync";
import { errorMessage, isErrorReported } from "../../utils/errorMessage";
import { listGatewayWorkspaces } from "../../gatewayApi";
import { HttpRequestError } from "../../api/http";
import type { GatewayRuntimeGroupRequest, GatewayWorkspace, Session } from "../../types/backend";
import type { SessionCatalogOutboxRuntime } from "../session/sessionCatalogOutboxRuntime";
import { requireGatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import { useSessionForkDialog, type SessionForkDialogInput } from "./useSessionForkDialog";

export interface SessionNameDialogState {
  sessionId: string;
  workspaceId: string;
  initialTitle: string;
}

interface SessionCatalogActionsInput extends Pick<
  SessionForkDialogInput,
  "forkSessionContext" | "readSessionSelectionIntent" | "onForkCreated"
> {
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
  onError: (message: string) => void;
  sessionCatalogOutboxRuntime?: SessionCatalogOutboxRuntime;

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
  removeGatewayWorkspace: (workspaceId: string, groupRequest?: GatewayRuntimeGroupRequest, force?: boolean) => Promise<void>;
  deleteSession: (sessionId: string, workspaceId?: string | null) => Promise<void>;
  renameSession: (
    sessionId: string,
    title: string,
    workspaceId?: string | null,
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
  onError,
  sessionCatalogOutboxRuntime,

  activateGatewayWorkspace,
  createSession,
  openWorkspaceSession,
  removeGatewayWorkspace,
  deleteSession,
  renameSession,
  forkSessionContext,
  onForkCreated,
  readSessionSelectionIntent,
  setSessionParent,
}: SessionCatalogActionsInput) {
  const [nameDialog, setNameDialog] = useState<SessionNameDialogState | null>(null);
  const [nameDialogSubmitting, setNameDialogSubmitting] = useState(false);
  const [nameDialogError, setNameDialogError] = useState<string | null>(null);
  // 命令的身份检查覆盖整个原对话框；取消或 owner 接受后结束临时调用，不另存业务状态。
  const nameDialogAction = useRef<{ assertAccess?: () => void; complete: () => void }>();
  useEffect(() => () => nameDialogAction.current?.complete(), []);
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

  const { forkSession, ...forkDialogActions } = useSessionForkDialog({
    apiPort,
    forkSessionContext,
    readSessionSelectionIntent,
    onForkCreated,
    invalidateSessionCatalog,
  });

  const createSessionInCatalog = async (workspaceId?: string | null) => {
    nameDialogAction.current?.complete();
    nameDialogAction.current = undefined;
    setNameDialog(null);
    setNameDialogError(null);
    // 顶部“新建会话”属于当前 Gateway 工作区；只有尚未激活工作区时
    // 才回退到系统默认 home，避免从测试/远程工作区误建到 home。
    const targetWorkspaceId = workspaceId
      ?? activeGatewayWorkspaceId
      ?? nullableGatewayWorkspaceKey(gatewayWorkspaces.find((workspace) => workspace.system_default));
    if (!targetWorkspaceId) {
      const error = new Error(t('navigation.sessions.defaultWorkspaceMissing'));
      setStatus(t('navigation.sessions.createError', error.message));
      throw error;
    }
    try {
      if (targetWorkspaceId !== activeGatewayWorkspaceId) {
        await activateGatewayWorkspace(targetWorkspaceId);
      }
      await createSession(DEFAULT_SESSION_TITLE, targetWorkspaceId);
      invalidateSessionCatalog(targetWorkspaceId);
    } catch (error) {
      setStatus(t('navigation.sessions.createError', errorMessage(error)));
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
    const runtime = sessionCatalogOutboxRuntime;
    if (!runtime) {
      throw new Error(t('navigation.catalog.authNotReadyCreateFolder'));
    }
    const workspaceTarget = requireGatewayWorkspaceRequestTarget(gatewayWorkspaces, workspaceId);
    const handle = await runtime.openWorkspace(
      workspaceTarget,
    );
    const parent = catalogOutboxParentCreation(handle.driver.current(), parentNodeId);
    const catalog = await fetchSessionCatalogSnapshot(apiPort, workspaceTarget);
    const submitted = await runtime.submit(
      workspaceTarget,
      { kind: "create_folder", name, parentNodeId: parent ? parent.parentNodeId : parentNodeId,
        parentCreatedByOperationId: parent?.createdBy },
      { baseCatalogRevision: catalog.catalog_revision },
    );
    setStatus(t('navigation.folder.savedPending'));
    invalidateSessionCatalog(workspaceId);
    void runtime.waitForTerminal(handle, submitted.operationId)
      .then(() => invalidateSessionCatalog(workspaceId))
      .catch((error: unknown) => {
        invalidateSessionCatalog(workspaceId);
        if (!isErrorReported(error)) runtime.reportError(
          t('navigation.folder.createFailed', errorMessage(error)),
        );
      });
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
    let delegatedRemovalError: unknown;
    void (async () => {
      let groupRequest: GatewayRuntimeGroupRequest | undefined;
      let impact = "";
      const displayed = gatewayWorkspaces.find((item) => gatewayWorkspaceKey(item) === workspaceId);
      if (displayed?.managed && displayed.connection_kind === "local") {
        const snapshot = await listGatewayWorkspaces(apiPort);
        const target = snapshot.items.find((item) => gatewayWorkspaceKey(item) === workspaceId);
        if (!target?.managed) throw new Error(t('navigation.workspace.changedBeforeDelete'));
        if (target.backend_generation) {
          const members = target.affected_workspace_ids;
          if (!members.length || !members.includes(target.workspace_id)
            || new Set(members).size !== members.length) {
            throw new Error(t('navigation.workspace.backendMembersMissing'));
          }
          groupRequest = {
            expected_backend_generation: target.backend_generation,
            expected_workspace_ids: members,
            desired_workspace_ids: members.filter((id) => id !== target.workspace_id),
          };
          const names = members.map((id) => {
            const member = snapshot.items.find((item) => item.gateway_id === target.gateway_id && item.workspace_id === id);
            if (!member) throw new Error(t('navigation.workspace.backendMemberMissing', id));
            return `• ${member.name}（${id}）`;
          });
          impact = t('navigation.workspace.deleteGroupImpact', names.join("\n"), members.length === 1
            ? t('navigation.workspace.deleteLastMember')
            : t('navigation.workspace.deleteOtherMembers'));
        }
      }
      const confirmed = await confirm({
        title: t('navigation.workspace.delete'),
        message: t('navigation.workspace.deleteDescription', workspaceName || workspaceId, impact),
        confirmText: t('navigation.delete'),
        danger: true,
      });
      if (!confirmed) return;
      try {
        try {
          await removeGatewayWorkspace(workspaceId, groupRequest);
        } catch (error) {
          delegatedRemovalError = error;
          throw error;
        }
      } catch (error) {
        if (!(error instanceof HttpRequestError) || error.status !== 409 || !groupRequest
          || typeof error.detail !== "object" || error.detail === null
          || !("status" in error.detail) || error.detail.status !== "blocked"
          || !("blockers" in error.detail) || !Array.isArray(error.detail.blockers)) throw error;
        const affectedWorkspaceIds = "affected_workspace_ids" in error.detail
          && Array.isArray(error.detail.affected_workspace_ids)
          ? error.detail.affected_workspace_ids
          : null;
        if (!("workspace_id" in error.detail) || error.detail.workspace_id !== workspaceOwnerFromKey(workspaceId).workspaceId
          || !("gateway_id" in error.detail) || error.detail.gateway_id !== workspaceOwnerFromKey(workspaceId).gatewayId
          || !("backend_generation" in error.detail) || error.detail.backend_generation !== groupRequest.expected_backend_generation
          || !affectedWorkspaceIds
          || affectedWorkspaceIds.length !== groupRequest.expected_workspace_ids.length
          || new Set(affectedWorkspaceIds).size !== groupRequest.expected_workspace_ids.length
          || groupRequest.expected_workspace_ids.some((id) => !affectedWorkspaceIds.includes(id))) {
          throw new Error(t('navigation.workspace.deleteContractMismatch'));
        }
        const blockers = error.detail.blockers.map((blocker: unknown) => {
          if (typeof blocker !== "object" || blocker === null
            || !("workspace_id" in blocker) || typeof blocker.workspace_id !== "string"
            || !("resource_id" in blocker) || typeof blocker.resource_id !== "string"
            || !("detail" in blocker)) throw new Error(t('navigation.workspace.deleteBlockerMissing'));
          return `${blocker.workspace_id}：${typeof blocker.detail === "string" ? blocker.detail : blocker.resource_id}`;
        });
        const forceConfirmed = await confirm({
          title: t('navigation.workspace.forceDelete'),
          message: t('navigation.workspace.forceDeleteDescription', blockers.join("\n"), impact),
          confirmText: t('navigation.workspace.forceDeleteConfirm'),
          danger: true,
        });
        if (forceConfirmed) {
          try {
            await removeGatewayWorkspace(workspaceId, groupRequest, true);
          } catch (forceError) {
            delegatedRemovalError = forceError;
            throw forceError;
          }
        }
      }
    })().catch((error: unknown) => {
      if (error !== delegatedRemovalError) {
        setStatus(t('navigation.workspace.deleteError', errorMessage(error)));
        onError(t('navigation.workspace.deleteOwnerError', workspaceId, errorMessage(error)));
      }
    });
  };
  const openRenameDialog = (
    sessionId: string,
    currentTitle: string,
    workspaceId: string,
    assertAccess?: () => void,
  ): Promise<void> => {
    if (nameDialogSubmitting) return Promise.reject(new Error(t('navigation.sessions.renameSubmitting')));
    assertAccess?.();
    nameDialogAction.current?.complete();
    const closed = new Promise<void>((complete) => { nameDialogAction.current = { assertAccess, complete }; });
    setNameDialog({
      sessionId,
      workspaceId,
      initialTitle: currentTitle || t('navigation.sessions.newDefaultTitle'),
    });
    setNameDialogError(null);
    return closed;
  };
  const removeSession = async (
    sessionId: string,
    title: string,
    workspaceId: string,
    assertAccess?: () => void,
  ) => {
    const label = title || sessionId;
    try {
      assertAccess?.();
      const confirmed = await confirm({
        title: t('navigation.sessions.permanentlyDelete'),
        message: t('navigation.sessions.deleteDescription', label),
        confirmText: t('navigation.delete'),
        danger: true,
      });
      if (!confirmed) {
        return;
      }
      assertAccess?.();
      await deleteSession(sessionId, workspaceId);
      // 生命周期动作已刷新并对账被删会话的真实父分支；会话集合变化再驱动
      // sessionCatalogSyncKeys 刷新其余已展开分支。
    } catch (error) {
      setStatus(t('navigation.sessions.deleteError', errorMessage(error)));
      if (!isErrorReported(error)) onError(t('navigation.sessions.deleteOwnerError', workspaceId, sessionId, errorMessage(error)));
    }
  };
  const changeSessionParent = async (
    workspaceId: string,
    sessionId: string,
    parentSessionId: string | null,
  ) => {
    await setSessionParent(workspaceId, sessionId, parentSessionId);
    invalidateSessionCatalog(workspaceId);
  };
  const closeNameDialog = () => {
    if (nameDialogSubmitting) {
      return;
    }
    setNameDialog(null);
    setNameDialogError(null);
    nameDialogAction.current?.complete();
    nameDialogAction.current = undefined;
  };
  const submitNameDialog = (title: string) => {
    if (!nameDialog || nameDialogSubmitting) {
      return;
    }

    setNameDialogSubmitting(true);
    setNameDialogError(null);
    const dialogAction = nameDialogAction.current;
    const action = Promise.resolve().then(() => {
      dialogAction?.assertAccess?.();
      return renameSession(nameDialog.sessionId, title, nameDialog.workspaceId);
    });

    void action
      .then(() => {
        invalidateSessionCatalog(nameDialog.workspaceId);
        setNameDialog(null);
        dialogAction?.complete();
        nameDialogAction.current = undefined;
      })
      .catch((error: unknown) => {
        setNameDialogError(errorMessage(error));
      })
      .finally(() => {
        setNameDialogSubmitting(false);
      });
  };

  return {
    ...forkDialogActions,
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
