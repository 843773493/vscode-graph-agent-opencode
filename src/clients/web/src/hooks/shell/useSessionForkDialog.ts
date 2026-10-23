import { useRef, useState } from "react";
import { getSessionForkSourcePoint } from "../../api/session/sessions";
import type {
  Session,
  SessionForkMode,
  SessionForkRequest,
  SessionForkResult,
  SessionForkSourcePoint,
} from "../../types/backend";
import { errorMessage } from "../../utils/errorMessage";

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
  sourcePoint: SessionForkSourcePoint | null;
  frozenRequest: SessionForkRequest | null;
  loading: boolean;
  submitting: boolean;
  error: string | null;
}

export type SessionForkDialogSelection = Pick<SessionForkDialogState, "mode" | "anchorMode" | "targetContainerUri">;

interface SessionForkSourceSelection {
  threadId?: string; turnId?: string; itemId?: string;
  sourceViewId?: string; sourceViewRevision?: number;
}

export interface SessionForkDialogInput {
  apiPort: number;
  sessionsByWorkspace: ReadonlyMap<string, Session[]>;
  forkSessionContext: (workspaceId: string, sourceSessionId: string, request: SessionForkRequest) => Promise<SessionForkResult>;
  readSessionSelectionIntent: () => number;
  onForkCreated?: (workspaceId: string, result: SessionForkResult, selectionIntent: number) => Promise<void>;
  invalidateSessionCatalog: (workspaceId: string) => void;
}

/** 管理 fork 来源读取、复制范围选择和提交对话框的状态流。 */
export function useSessionForkDialog({
  apiPort,
  sessionsByWorkspace,
  forkSessionContext,
  readSessionSelectionIntent,
  onForkCreated,
  invalidateSessionCatalog,
}: SessionForkDialogInput) {
  const [forkDialog, setForkDialog] = useState<SessionForkDialogState | null>(null);
  const dialogRequestVersion = useRef(0);

  const forkSession = async (
    workspaceId: string,
    sourceSessionId: string,
    sourceSelection: SessionForkSourceSelection = {},
  ) => {
    const requestVersion = ++dialogRequestVersion.current;
    setForkDialog({
      sourceWorkspaceId: workspaceId, sourceSessionId,
      sourceTitle: sessionsByWorkspace.get(workspaceId)
        ?.find((session) => session.session_id === sourceSessionId)?.title ?? null,
      sourceThreadId: sourceSelection.threadId, sourceAnchorItemId: sourceSelection.itemId ?? null,
      mode: "view_prefix_fork", anchorMode: "inclusive", targetContainerUri: "",
      idempotencyKey: crypto.randomUUID(), sourcePoint: null, frozenRequest: null,
      loading: true, submitting: false, error: null,
    });
    try {
      const sourcePoint = await getSessionForkSourcePoint(apiPort, sourceSessionId, sourceSelection, workspaceId);
      if (dialogRequestVersion.current !== requestVersion) return;
      if (sourceSelection.sourceViewId !== undefined && (
        sourcePoint.source_view_id !== sourceSelection.sourceViewId
        || sourcePoint.source_view_revision !== sourceSelection.sourceViewRevision
        || sourceSelection.itemId !== undefined && sourcePoint.anchor_item_id !== sourceSelection.itemId
      )) throw new Error("来源回执与实际展示快照不一致");
      setForkDialog((currentDialog) => currentDialog ? {
        ...currentDialog, sourcePoint,
        sourceAnchorItemId: sourcePoint.anchor_item_id ?? null,
        targetContainerUri: sourcePoint.child_container_uri, loading: false,
      } : null);
    } catch (error) {
      if (dialogRequestVersion.current !== requestVersion) return;
      setForkDialog((currentDialog) => currentDialog
        ? { ...currentDialog, loading: false, error: errorMessage(error) }
        : null);
    }
  };

  const updateForkDialogSelection = async (selectionChange: Partial<SessionForkDialogSelection>) => {
    if (!forkDialog?.sourcePoint || forkDialog.submitting || forkDialog.frozenRequest) return;
    const frozenSourcePoint = forkDialog.sourcePoint;
    const previousDialog = forkDialog;
    const nextDialog = { ...previousDialog, ...selectionChange, loading: true, error: null };
    const requestVersion = ++dialogRequestVersion.current;
    setForkDialog(nextDialog);
    try {
      const sourcePoint = await getSessionForkSourcePoint(apiPort, previousDialog.sourceSessionId, {
        threadId: previousDialog.sourceThreadId,
        itemId: nextDialog.mode === "view_prefix_fork" ? previousDialog.sourceAnchorItemId ?? undefined : undefined,
        sourceViewId: frozenSourcePoint.source_view_id,
        sourceViewRevision: frozenSourcePoint.source_view_revision,
        mode: nextDialog.mode, anchorMode: nextDialog.anchorMode,
      }, previousDialog.sourceWorkspaceId);
      if (dialogRequestVersion.current !== requestVersion) return;
      if (sourcePoint.source_uri !== frozenSourcePoint.source_uri
        || sourcePoint.source_view_id !== frozenSourcePoint.source_view_id
        || sourcePoint.source_view_revision !== frozenSourcePoint.source_view_revision
        || nextDialog.mode === "view_prefix_fork" && (sourcePoint.anchor_item_id ?? null) !== previousDialog.sourceAnchorItemId
        || nextDialog.mode === "full_rollout_copy" && sourcePoint.anchor_item_id != null
        || sourcePoint.resource_mode !== nextDialog.mode
        || sourcePoint.resource_anchor_mode !== nextDialog.anchorMode) {
        throw new Error("附件候选回执与冻结的来源或当前复制范围不一致");
      }
      setForkDialog({ ...nextDialog, sourcePoint, loading: false });
    } catch (error) {
      if (dialogRequestVersion.current !== requestVersion) return;
      setForkDialog({ ...nextDialog, loading: false, error: errorMessage(error) });
    }
  };

  const closeForkDialog = () => {
    if (forkDialog?.submitting) return;
    dialogRequestVersion.current += 1;
    setForkDialog(null);
  };

  const submitForkDialog = async (request: SessionForkRequest) => {
    if (!forkDialog?.sourcePoint || forkDialog.submitting || forkDialog.loading
      || (!forkDialog.frozenRequest && (forkDialog.error
        || forkDialog.mode === "view_prefix_fork" && !forkDialog.sourcePoint.anchor_item_id
        || forkDialog.sourcePoint.resource_mode !== forkDialog.mode
        || forkDialog.sourcePoint.resource_anchor_mode !== forkDialog.anchorMode))) return;
    const submittedDialog = forkDialog;
    const frozenRequest = submittedDialog.frozenRequest ?? request;
    setForkDialog({ ...submittedDialog, frozenRequest, submitting: true, error: null });
    try {
      const forkOperation = forkSessionContext(submittedDialog.sourceWorkspaceId, submittedDialog.sourceSessionId, frozenRequest);
      const selectionIntent = readSessionSelectionIntent();
      const forkResult = await forkOperation;
      invalidateSessionCatalog(submittedDialog.sourceWorkspaceId);
      await onForkCreated?.(submittedDialog.sourceWorkspaceId, forkResult, selectionIntent);
      const unavailableCopiedResources = forkResult.resources.filter((resource) =>
        resource.owner_reference && resource.action !== "omit" && resource.availability === "unavailable");
      if (unavailableCopiedResources.length > 0) {
        setForkDialog({ ...submittedDialog, frozenRequest, submitting: false,
          error: `目标已创建（${forkResult.thread_id}），运行资源关联失败或待核实：${unavailableCopiedResources.map((resource) =>
            `${resource.owner_reference!.resource_id}：${resource.reason ?? "原 owner 尚未核实"}`).join("；")}` });
      } else {
        setForkDialog(null);
      }
    } catch (error) {
      setForkDialog({ ...submittedDialog, frozenRequest, submitting: false, error: errorMessage(error) });
    }
  };

  return { forkDialog, forkSession, updateForkDialogSelection, closeForkDialog, submitForkDialog };
}
