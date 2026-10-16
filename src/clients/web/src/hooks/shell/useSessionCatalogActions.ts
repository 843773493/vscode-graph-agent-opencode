import { useCallback, useMemo, useRef, useState } from "react";
import { catalogOutboxParentCreation } from "../../state/session/sessionCatalogOutbox";
import {
  DEFAULT_SESSION_TITLE,
} from "../../api";
import { fetchSessionCatalogSnapshot } from "../../api/session/sessionCatalogOperations";
import { getSessionForkSourcePoint } from "../../api/session/sessions";
import { buildSessionCatalogSyncKeys } from "../sessionResourceExplorer/resourceTreeSync";
import { errorMessage } from "../../utils/errorMessage";
import { listGatewayWorkspaces } from "../../gatewayApi";
import { HttpRequestError } from "../../api/http";
import type { GatewayRuntimeGroupRequest, GatewayUserAccess, GatewayWorkspace, Session, SessionForkMode, SessionForkRequest, SessionForkResult, SessionForkSourcePoint } from "../../types/backend";
import type { SessionCatalogOutboxRuntime } from "../session/sessionCatalogOutboxRuntime";
import { requireGatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";

export interface SessionNameDialogState {
  sessionId: string;
  workspaceId: string;
  initialTitle: string;
}

export interface SessionForkDialogState {
  sourceWorkspaceId: string;
  sourceSessionId: string;
  sourceTitle: string | null;
  sourceThreadId: string | undefined;
  sourceAnchorItemId: string | null;
  mode: SessionForkMode;
  anchorMode: "inclusive" | "before";
  targetContainerUri: string;
  idempotencyKey: string;
  point: SessionForkSourcePoint | null;
  request: SessionForkRequest | null;
  loading: boolean;
  submitting: boolean;
  error: string | null;
}

export type SessionForkDialogSelection = Pick<SessionForkDialogState, "mode" | "anchorMode" | "targetContainerUri">;

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
    request: SessionForkRequest,
  ) => Promise<SessionForkResult>;
  readSessionSelectionIntent: () => number;
  onForkCreated?: (workspaceId: string, result: SessionForkResult, selectionIntent: number) => Promise<void>;
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
  onForkCreated,
  readSessionSelectionIntent,
  setSessionParent,
}: SessionCatalogActionsInput) {
  const [nameDialog, setNameDialog] = useState<SessionNameDialogState | null>(null);
  const [nameDialogSubmitting, setNameDialogSubmitting] = useState(false);
  const [nameDialogError, setNameDialogError] = useState<string | null>(null);
  const [forkDialog, setForkDialog] = useState<SessionForkDialogState | null>(null);
  const forkDialogRead = useRef(0);
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
    const parent = catalogOutboxParentCreation(handle.driver.current(), parentNodeId);
    const catalog = await fetchSessionCatalogSnapshot(apiPort, workspaceTarget);
    const submitted = await runtime.submit(
      workspaceTarget,
      access,
      { kind: "create_folder", name, parentNodeId: parent ? parent.parentNodeId : parentNodeId,
        parentCreatedByOperationId: parent?.createdBy },
      { baseCatalogRevision: catalog.catalog_revision },
    );
    setStatus("会话文件夹已保存本地待同步变更，等待提交");
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
    let delegatedRemovalError: unknown;
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
          if (!members.length || !members.includes(workspaceId)
            || new Set(members).size !== members.length) {
            throw new Error("Backend 完整成员合同缺失");
          }
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
        if (!("workspace_id" in error.detail) || error.detail.workspace_id !== workspaceId
          || !("backend_generation" in error.detail) || error.detail.backend_generation !== groupRequest.expected_backend_generation
          || !affectedWorkspaceIds
          || affectedWorkspaceIds.length !== groupRequest.expected_workspace_ids.length
          || new Set(affectedWorkspaceIds).size !== groupRequest.expected_workspace_ids.length
          || groupRequest.expected_workspace_ids.some((id) => !affectedWorkspaceIds.includes(id))) {
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
        setStatus(`删除工作区失败: ${errorMessage(error)}`);
      }
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
  const forkSession = async (
    workspaceId: string, sourceSessionId: string,
    selection: {
      threadId?: string; turnId?: string; itemId?: string;
      sourceViewId?: string; sourceViewRevision?: number;
    } = {},
  ) => {
    const read = ++forkDialogRead.current;
    setForkDialog({
      sourceWorkspaceId: workspaceId, sourceSessionId,
      sourceTitle: sessionsByWorkspace.get(workspaceId)?.find((session) => session.session_id === sourceSessionId)?.title ?? null,
      sourceThreadId: selection.threadId, sourceAnchorItemId: selection.itemId ?? null,
      mode: "view_prefix_fork", anchorMode: "inclusive", targetContainerUri: "",
      idempotencyKey: crypto.randomUUID(), point: null,
      request: null, loading: true, submitting: false, error: null,
    });
    try {
      const point = await getSessionForkSourcePoint(apiPort, sourceSessionId, selection, workspaceId);
      if (forkDialogRead.current !== read) return;
      if (selection.sourceViewId !== undefined && (
        point.source_view_id !== selection.sourceViewId
        || point.source_view_revision !== selection.sourceViewRevision
        || selection.itemId !== undefined && point.anchor_item_id !== selection.itemId
      )) throw new Error("来源回执与实际展示快照不一致");
      setForkDialog((prior) => prior ? {
        ...prior, point, sourceAnchorItemId: point.anchor_item_id ?? null,
        targetContainerUri: point.child_container_uri, loading: false,
      } : null);
    } catch (error) {
      if (forkDialogRead.current !== read) return;
      setForkDialog((prior) => prior ? { ...prior, loading: false, error: errorMessage(error) } : null);
    }
  };
  const updateForkDialogSelection = async (change: Partial<SessionForkDialogSelection>) => {
    if (!forkDialog?.point || forkDialog.submitting || forkDialog.request) return;
    const sourcePoint = forkDialog.point;
    const prior = forkDialog;
    const next = { ...prior, ...change, loading: true, error: null };
    const read = ++forkDialogRead.current;
    setForkDialog(next);
    try {
      const point = await getSessionForkSourcePoint(apiPort, prior.sourceSessionId, {
        threadId: prior.sourceThreadId,
        itemId: next.mode === "view_prefix_fork" ? prior.sourceAnchorItemId ?? undefined : undefined,
        sourceViewId: sourcePoint.source_view_id, sourceViewRevision: sourcePoint.source_view_revision,
        mode: next.mode, anchorMode: next.anchorMode,
      }, prior.sourceWorkspaceId);
      if (forkDialogRead.current !== read) return;
      if (point.source_uri !== sourcePoint.source_uri
        || point.source_view_id !== sourcePoint.source_view_id
        || point.source_view_revision !== sourcePoint.source_view_revision
        || next.mode === "view_prefix_fork" && (point.anchor_item_id ?? null) !== prior.sourceAnchorItemId
        || next.mode === "full_rollout_copy" && point.anchor_item_id != null
        || point.resource_mode !== next.mode || point.resource_anchor_mode !== next.anchorMode) {
        throw new Error("附件候选回执与冻结的来源或当前复制范围不一致");
      }
      setForkDialog({ ...next, point, loading: false });
    } catch (error) {
      if (forkDialogRead.current !== read) return;
      setForkDialog({ ...next, loading: false, error: errorMessage(error) });
    }
  };
  const closeForkDialog = () => {
    if (forkDialog?.submitting) return;
    forkDialogRead.current += 1;
    setForkDialog(null);
  };
  const submitForkDialog = async (request: SessionForkRequest) => {
    if (!forkDialog?.point || forkDialog.submitting || forkDialog.loading
      || (!forkDialog.request && (forkDialog.error
        || forkDialog.mode === "view_prefix_fork" && !forkDialog.point.anchor_item_id
        || forkDialog.point.resource_mode !== forkDialog.mode
        || forkDialog.point.resource_anchor_mode !== forkDialog.anchorMode))) return;
    const dialog = forkDialog;
    const frozen = dialog.request ?? request;
    setForkDialog({ ...dialog, request: frozen, submitting: true, error: null });
    try {
      const operation = forkSessionContext(dialog.sourceWorkspaceId, dialog.sourceSessionId, frozen);
      const selectionIntent = readSessionSelectionIntent();
      const result = await operation;
      invalidateSessionCatalog(dialog.sourceWorkspaceId);
      await onForkCreated?.(dialog.sourceWorkspaceId, result, selectionIntent);
      const failures = result.resources.filter((resource) => resource.owner_reference
        && resource.action !== "omit" && resource.availability === "unavailable");
      if (failures.length > 0) {
        setForkDialog({ ...dialog, request: frozen, submitting: false,
          error: `目标已创建（${result.thread_id}），运行资源关联失败或待核实：${failures.map((resource) =>
            `${resource.owner_reference!.resource_id}：${resource.reason ?? "原 owner 尚未核实"}`).join("；")}` });
      } else {
        setForkDialog(null);
      }
    } catch (error) {
      setForkDialog({ ...dialog, request: frozen, submitting: false, error: errorMessage(error) });
    }
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
    forkDialog,
    updateForkDialogSelection,
    closeForkDialog,
    submitForkDialog,
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
