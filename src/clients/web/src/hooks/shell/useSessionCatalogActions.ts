import { useCallback, useMemo, useState } from "react";
import {
  DEFAULT_SESSION_TITLE,
} from "../../api";
import { fetchSessionCatalogSnapshot } from "../../api/session/sessionCatalogOperations";
import { buildSessionCatalogSyncKeys } from "../sessionResourceExplorer/resourceTreeSync";
import { errorMessage } from "../../utils/errorMessage";
import { listGatewayWorkspaces } from "../../gatewayApi";
import { HttpRequestError } from "../../api/http";
import type { GatewayRuntimeGroupRequest, GatewayUserAccess, GatewayWorkspace, Session } from "../../types/backend";
import type { SessionCatalogOutboxRuntime } from "../session/sessionCatalogOutboxRuntime";
import { requireGatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";

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
  sessionCatalogOutboxRuntime?: SessionCatalogOutboxRuntime;
  gatewayUserAccess?: GatewayUserAccess | null;
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
  sessionCatalogOutboxRuntime,
  gatewayUserAccess,
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
    const runtime = sessionCatalogOutboxRuntime;
    const access = gatewayUserAccess;
    if (!runtime || !access) {
      throw new Error("Gateway 认证状态尚未就绪，不能新建会话文件夹");
    }
    const workspaceTarget = requireGatewayWorkspaceRequestTarget(gatewayWorkspaces, workspaceId);
    const handle = await runtime.openWorkspace(
      workspaceTarget,
      access,
    );
    let resolvedParentNodeId = parentNodeId;
    if (parentNodeId) {
      const parentCreation = handle.driver.current().operations.find(
        (operation) => operation.kind === "create_folder"
          && (operation.client_ref === parentNodeId
            || operation.created_node_id === parentNodeId),
      );
      if (parentCreation && !parentCreation.created_node_id) {
        throw new Error("父文件夹尚未提交，当前目录协议不能在待提交文件夹下新建子文件夹");
      }
      if (parentCreation?.created_node_id) {
        resolvedParentNodeId = parentCreation.created_node_id;
      }
    }
    const catalog = await fetchSessionCatalogSnapshot(apiPort, workspaceTarget);
    const submitted = await runtime.submit(
      workspaceTarget,
      access,
      { kind: "create_folder", name, parentNodeId: resolvedParentNodeId },
      { baseCatalogRevision: catalog.catalog_revision },
    );
    if (submitted.dispatchOutcome === "accepted") {
      setStatus("会话文件夹已持久受理，等待提交");
    } else if (submitted.dispatchOutcome === "unknown") {
      setStatus("会话文件夹状态待确认，保留本地待同步变更");
    }
    invalidateSessionCatalog(workspaceId);
    void runtime.waitForTerminal(handle, submitted.operationId)
      .then(() => invalidateSessionCatalog(workspaceId))
      .catch((error: unknown) => {
        invalidateSessionCatalog(workspaceId);
        runtime.reportError(
          `新建会话文件夹失败: ${errorMessage(error)}`,
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
    void (async () => {
      let groupRequest: GatewayRuntimeGroupRequest | undefined;
      let impact = "";
      const displayed = gatewayWorkspaces.find((item) => item.workspace_id === workspaceId);
      if (displayed?.managed && displayed.connection_kind === "local") {
        const snapshot = await listGatewayWorkspaces(apiPort);
        const target = snapshot.items.find((item) => item.workspace_id === workspaceId);
        if (!target?.managed) throw new Error("工作区已变化，请刷新后重新删除");
        if (target.backend_generation) {
          const members = target.affected_workspace_ids;
          if (!members.length || !members.includes(workspaceId)) throw new Error("Backend 完整成员合同缺失");
          groupRequest = {
            expected_backend_generation: target.backend_generation,
            expected_workspace_ids: members,
            desired_workspace_ids: members.filter((id) => id !== workspaceId),
          };
          const names = members.map((id) => {
            const member = snapshot.items.find((item) => item.workspace_id === id);
            if (!member) throw new Error(`Backend 成员未登记：${id}`);
            return `• ${member.name}（${id}）`;
          });
          impact = `\n\n以下完整组将先排空：\n${names.join("\n")}\n${members.length === 1
            ? "最后成员退出后将关闭 Backend。"
            : "其余成员将一起重启，删除目标会退出 Backend。"}`;
        }
      }
      const confirmed = await confirm({
        title: "删除工作区",
        message: `从 Web Gateway 列表移除工作区“${workspaceName || workspaceId}”。会话文件不会被删除。${impact}`,
        confirmText: "删除",
        danger: true,
      });
      if (!confirmed) return;
      try {
        await removeGatewayWorkspace(workspaceId, groupRequest);
      } catch (error) {
        if (!(error instanceof HttpRequestError) || error.status !== 409 || !groupRequest
          || typeof error.detail !== "object" || error.detail === null
          || !("status" in error.detail) || error.detail.status !== "blocked"
          || !("blockers" in error.detail) || !Array.isArray(error.detail.blockers)) throw error;
        if (!("workspace_id" in error.detail) || error.detail.workspace_id !== workspaceId
          || !("backend_generation" in error.detail) || error.detail.backend_generation !== groupRequest.expected_backend_generation
          || !("affected_workspace_ids" in error.detail) || !Array.isArray(error.detail.affected_workspace_ids)
          || error.detail.affected_workspace_ids.length !== groupRequest.expected_workspace_ids.length
          || error.detail.affected_workspace_ids.some((id: unknown) => typeof id !== "string" || !groupRequest.expected_workspace_ids.includes(id))) {
          throw new Error("删除阻塞回执与已确认的 Backend 完整合同不匹配");
        }
        const blockers = error.detail.blockers.map((blocker: unknown) => {
          if (typeof blocker !== "object" || blocker === null
            || !("workspace_id" in blocker) || typeof blocker.workspace_id !== "string"
            || !("resource_id" in blocker) || typeof blocker.resource_id !== "string"
            || !("detail" in blocker)) throw new Error("删除阻塞回执缺少完整 blocker 身份");
          return `${blocker.workspace_id}：${typeof blocker.detail === "string" ? blocker.detail : blocker.resource_id}`;
        });
        const forceConfirmed = await confirm({
          title: "强制删除工作区",
          message: `当前删除被阻塞：\n${blockers.join("\n")}\n\n确认后，原完整组内的活动任务将先记录为 interrupted，再关闭或重启 Backend 并删除目标。${impact}`,
          confirmText: "中断任务并删除",
          danger: true,
        });
        if (forceConfirmed) await removeGatewayWorkspace(workspaceId, groupRequest, true);
      }
    })().catch((error: unknown) => {
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
      // 生命周期动作已刷新并对账被删会话的真实父分支；会话集合变化再驱动
      // sessionCatalogSyncKeys 刷新其余已展开分支。
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
