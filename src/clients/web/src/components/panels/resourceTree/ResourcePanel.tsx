import { failureSummary } from "../../../../../shared/core/failurePresentation";
import { t, formatNumber } from "../../../../../shared/core/i18n/index.js";
import { useEffect, useRef, useState } from "react";
import type {
  SessionResource,
  SessionResourceAction,
  SessionResourceKind,
} from "../../../types/backend";
import { useWarmConfirm } from "../../shell/WarmConfirmProvider";
import {
  actionLabelForKind,
  groupSessionResources,
  isPreviewedResource,
  kindLabel,
  resourceKindIcon,
  type ResourceAttentionGroup,
  statusLabel,
} from "../../../state/display/resourceDisplay";
import { CREATABLE_SESSION_CONNECTION_OPTIONS } from "../../../state/creatableSessionConnectionOptions";
import type { CreatableSessionConnectionKind } from "../../../types/frontend";
import AnchoredOverlay from "../../overlays/AnchoredOverlay";
import ResourceTreeRow from "./ResourceTreeRow";
import { copyTextToClipboard } from "../../../utils/clipboard";
import { errorMessage } from "../../../utils/errorMessage";
import type { ResourceThreadRef } from "../../../api/workspaceResources";

const DEFAULT_GROUP_OPEN: Record<ResourceAttentionGroup, boolean> = {
  active: true,
  attention: true,
  available: true,
  sleeping: false,
  history: false,
};

type ResourcePanelProps = {
  resources: SessionResource[];
  loading: boolean;
  error: string | null;
  loadedAt: string | null;
  sessionId: string;
  workspaceId: string | null;
  mainThread: ResourceThreadRef | null;
  extensionWindow?: boolean;
  inlinePreview?: boolean;
  activePreviewPath: string | null;
  onRefresh: () => void;
  onControl: (
    kind: SessionResourceKind,
    resourceId: string,
    action: SessionResourceAction,
  ) => Promise<void>;
  onOwnerControl?: (resourceId: string, action: "promote" | "detach" | "attach", thread?: ResourceThreadRef) => Promise<void>;
  onOpenTerminalPreview: (terminalId: string) => void;
  onOpenTerminalExtension?: (terminalId: string) => void;
  onOpenBrowserPreview: (browserId: string) => void;
  onCloseResourcePreview: (
    kind: Extract<SessionResourceKind, "terminal" | "browser">,
    resourceId: string,
  ) => Promise<void>;
  onCreateConnection: (
    kind: CreatableSessionConnectionKind,
    thread: ResourceThreadRef,
  ) => Promise<void>;
};

type PendingResourceOperation = {
  requestId: number;
  resourceId: string;
};

type PendingCreateOperation = {
  requestId: number;
  kind: CreatableSessionConnectionKind;
};

type OpenedResource = {
  requestId: number;
  resourceId: string;
};

export default function ResourcePanel(props: ResourcePanelProps) {
  return (
    <ResourcePanelForOwner
      key={JSON.stringify([props.workspaceId, props.sessionId])}
      {...props}
    />
  );
}

function ResourcePanelForOwner({
  resources,
  loading,
  error,
  loadedAt,
  sessionId,
  workspaceId,
  mainThread,
  extensionWindow = false,
  inlinePreview = false,
  activePreviewPath,
  onRefresh,
  onControl,
  onOwnerControl,
  onOpenTerminalPreview,
  onOpenTerminalExtension,
  onOpenBrowserPreview,
  onCloseResourcePreview,
  onCreateConnection,
}: ResourcePanelProps) {
  const [busyOperation, setBusyOperation] = useState<PendingResourceOperation | null>(null);
  const [notice, setNotice] = useState("");
  const [noticeTechnicalDetails, setNoticeTechnicalDetails] = useState("");
  const [openedTerminal, setOpenedTerminal] = useState<OpenedResource | null>(null);
  const [openedBrowser, setOpenedBrowser] = useState<OpenedResource | null>(null);
  const [openGroups, setOpenGroups] = useState(DEFAULT_GROUP_OPEN);
  const [createMenuOpen, setCreateMenuOpen] = useState(false);
  const [creatingOperation, setCreatingOperation] = useState<PendingCreateOperation | null>(null);
  const mountedRef = useRef(false);
  const requestSequenceRef = useRef(0);
  const currentRequestRef = useRef(0);
  const createMenuAnchorRef = useRef<HTMLDivElement | null>(null);
  const confirm = useWarmConfirm();
  const busyResourceId = busyOperation?.resourceId ?? null;
  const creatingKind = creatingOperation?.kind ?? null;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const beginRequest = () => {
    const requestId = ++requestSequenceRef.current;
    currentRequestRef.current = requestId;
    return requestId;
  };
  const isCurrentRequest = (requestId: number) =>
    mountedRef.current && currentRequestRef.current === requestId;
  const presentNotice = (
    requestId: number,
    message: string,
    technicalDetails = "",
  ) => {
    if (!isCurrentRequest(requestId)) return;
    setNotice(message);
    setNoticeTechnicalDetails(technicalDetails);
  };

  useEffect(() => {
    if (!openedTerminal || !isCurrentRequest(openedTerminal.requestId)) {
      return;
    }
    const terminal = resources.find(
      (resource) =>
        resource.kind === "terminal" && resource.resource_id === openedTerminal.resourceId,
    );
    if (!terminal || terminal.status === "running") {
      return;
    }
    presentNotice(
      openedTerminal.requestId,
      t("resources.panel.terminalUnavailable", openedTerminal.resourceId, statusLabel(terminal.status)),
    );
    setOpenedTerminal((current) => current?.requestId === openedTerminal.requestId ? null : current);
  }, [openedTerminal, resources]);

  useEffect(() => {
    if (!openedBrowser || !isCurrentRequest(openedBrowser.requestId)) {
      return;
    }
    const browser = resources.find(
      (resource) =>
        resource.kind === "browser" && resource.resource_id === openedBrowser.resourceId,
    );
    if (!browser || browser.status === "running") {
      return;
    }
    presentNotice(
      openedBrowser.requestId,
      t("resources.panel.browserUnavailable", openedBrowser.resourceId, statusLabel(browser.status)),
    );
    setOpenedBrowser((current) => current?.requestId === openedBrowser.requestId ? null : current);
  }, [openedBrowser, resources]);

  const handleControl = async (
    kind: SessionResourceKind,
    resourceId: string,
    action: SessionResourceAction,
  ) => {
    if (!mountedRef.current) return;
    const requestId = beginRequest();
    if (action === "delete") {
      const targetLabel = kind === "terminal"
        ? t("resources.kind.terminal")
        : kind === "browser"
          ? t("resources.kind.browser")
          : t("resources.kind.background_task");
      const confirmed = await confirm({
        title: t("resources.panel.delete", targetLabel),
        message: kind === "terminal"
          ? t("resources.panel.deleteTerminal", resourceId)
          : kind === "browser"
            ? t("resources.panel.deleteBrowser", resourceId)
            : t("resources.panel.deleteTask", resourceId),
        confirmText: t("resources.action.delete"),
        danger: true,
      });
      if (!confirmed || !isCurrentRequest(requestId)) {
        return;
      }
    }
    setBusyOperation({ requestId, resourceId });
    setNotice("");
    setNoticeTechnicalDetails("");
    setOpenedTerminal(null);
    setOpenedBrowser(null);
    try {
      await onControl(kind, resourceId, action);
      if (!isCurrentRequest(requestId)) return;
      if (action === "delete" && (kind === "terminal" || kind === "browser")) {
        await onCloseResourcePreview(kind, resourceId);
        if (!isCurrentRequest(requestId)) return;
      }
      if (action === "resume" && kind === "browser" && isCurrentRequest(requestId)) {
        handleOpenBrowser(resourceId, requestId);
      }
      presentNotice(requestId, t(`resources.completed.${action === "cancel" && kind === "terminal" ? "terminate" : action === "cancel" && kind === "browser" ? "close" : action === "resume" && kind === "browser" ? "reopen" : action}`, kindLabel(kind), resourceId));
    } catch (controlError: unknown) {
      const technicalDetails = errorMessage(controlError);
      presentNotice(
        requestId,
        kind === "browser" && action === "resume"
          ? t("resources.panel.reopenFailed")
          : t("resources.panel.controlFailed", actionLabelForKind(kind, action)),
        technicalDetails,
      );
    } finally {
      if (mountedRef.current) {
        setBusyOperation((current) => current?.requestId === requestId ? null : current);
      }
    }
  };

  const handleCopy = (resourceId: string) => {
    if (!mountedRef.current) return;
    const requestId = beginRequest();
    // 复制统一走 utils/clipboard 的唯一实现：Clipboard API 被拒绝、非安全上下文
    // 下兼容复制也失败时，必须在同一处给出可见原因，不能把失败静默吞掉。
    void copyTextToClipboard(resourceId)
      .then(() => {
        presentNotice(requestId, t("resources.panel.copied", resourceId));
      })
      .catch((copyError: unknown) => {
        presentNotice(requestId, t("resources.panel.copyFailed"), errorMessage(copyError));
      });
  };

  const handleOwnerControl = async (resourceId: string, action: "promote" | "detach" | "attach", thread?: ResourceThreadRef) => {
    if (!mountedRef.current || !onOwnerControl) return;
    const requestId = beginRequest();
    setBusyOperation({ requestId, resourceId });
    setNotice("");
    setNoticeTechnicalDetails("");
    try {
      await onOwnerControl(resourceId, action, thread);
      presentNotice(requestId, action === "promote" ? t("resources.panel.promoted") : t("resources.panel.associationsUpdated"));
    } catch (cause) {
      presentNotice(requestId, t("resources.panel.manageFailed"), errorMessage(cause));
    } finally {
      if (mountedRef.current) {
        setBusyOperation((current) => current?.requestId === requestId ? null : current);
      }
    }
  };

  const handleOpenTerminal = (resourceId: string) => {
    if (!mountedRef.current) return;
    const requestId = beginRequest();
    if (!workspaceId) {
      presentNotice(requestId, t("resources.panel.terminalOwnerMissing"));
      return;
    }
    setOpenedTerminal({ requestId, resourceId });
    onOpenTerminalPreview(resourceId);
    presentNotice(requestId, inlinePreview ? t("resources.panel.terminalInline") : t("resources.panel.terminalBottom"));
  };
  const handleOpenBrowser = (resourceId: string, existingRequestId?: number) => {
    if (!mountedRef.current) return;
    const requestId = existingRequestId ?? beginRequest();
    if (!workspaceId) {
      presentNotice(requestId, t("resources.panel.browserOwnerMissing"));
      return;
    }
    setOpenedBrowser({ requestId, resourceId });
    onOpenBrowserPreview(resourceId);
    presentNotice(requestId, inlinePreview ? t("resources.panel.browserInline") : t("resources.panel.browserExtension"));
  };
  const createThread = mainThread?.session_id === sessionId && mainThread.thread_id
    ? mainThread
    : null;
  const handleCreateConnection = (kind: CreatableSessionConnectionKind) => {
    if (!mountedRef.current) return;
    const requestId = beginRequest();
    setCreateMenuOpen(false);
    if (!createThread) {
      presentNotice(requestId, t("resources.panel.threadMissing"));
      return;
    }
    setCreatingOperation({ requestId, kind });
    setNotice("");
    setNoticeTechnicalDetails("");
    void onCreateConnection(kind, createThread)
      .then(() => {
        presentNotice(
          requestId,
          inlinePreview ? t("resources.panel.createdInline", kindLabel(kind)) : kind === "terminal"
            ? t("resources.panel.createdTerminal")
            : t("resources.panel.created", kindLabel(kind)),
        );
      })
      .catch((createError: unknown) => {
        const details = errorMessage(createError);
        presentNotice(requestId, details);
      })
      .finally(() => {
        if (mountedRef.current) {
          setCreatingOperation((current) => current?.requestId === requestId ? null : current);
        }
      });
  };
  const handleRefresh = () => {
    if (!mountedRef.current) return;
    beginRequest();
    onRefresh();
  };
  const connectionResources = resources.filter(
    (resource) => resource.kind === "browser" || resource.kind === "terminal",
  );
  const resourceGroups = groupSessionResources(connectionResources, activePreviewPath);
  const activeCount = resourceGroups.find((group) => group.key === "active")
    ?.resources.length ?? 0;
  const attentionCount = resourceGroups.find((group) => group.key === "attention")
    ?.resources.length ?? 0;
  const historyCount = resourceGroups.find((group) => group.key === "history")
    ?.resources.length ?? 0;
  const waitingForFirstMessage = !sessionId;

  return (
    <section
      className={`panel-view resource-panel${extensionWindow ? " extension-window-region" : ""}`}
      data-extension-region={extensionWindow ? "secondary" : undefined}
    >
      <div className="panel-header">
        <div
          className="panel-title"
          title={`${sessionId || t("resources.panel.noSession")}${loadedAt ? t("resources.panel.readAt", loadedAt) : ""}`}
        >
          {t("resources.panel.total")} <span className="resource-total-count">{formatNumber(connectionResources.length)}</span>
        </div>
        <div className="panel-header-meta">
          <span>{t("resources.panel.activeCount", formatNumber(activeCount))}</span>
          {historyCount > 0 ? <span>{t("resources.panel.historyCount", formatNumber(historyCount))}</span> : null}
          {attentionCount > 0 ? <span className="resource-attention-count">{t("resources.panel.attentionCount", formatNumber(attentionCount))}</span> : null}
        </div>
        <button
          type="button"
          className="resource-icon-button"
          onClick={handleRefresh}
          disabled={loading || !sessionId}
          title={t("resources.panel.refresh")}
          aria-label={t("resources.panel.refresh")}
        >
          <span className={`codicon codicon-refresh${loading ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />
        </button>
        {sessionId && workspaceId ? <div ref={createMenuAnchorRef} className="resource-create-control">
          <button
            type="button"
            className="resource-refresh-button resource-create-button"
            onClick={() => setCreateMenuOpen((open) => !open)}
            disabled={creatingKind !== null || !createThread}
            title={!createThread ? t("resources.panel.waitingThread") : undefined}
            aria-haspopup="menu"
            aria-expanded={createMenuOpen}
          >
            {creatingKind ? t("resources.panel.creating") : t("resources.panel.create")}
            <span className="codicon codicon-chevron-down" aria-hidden="true" />
          </button>
          <AnchoredOverlay
            open={createMenuOpen}
            anchorRef={createMenuAnchorRef}
            placement="bottom-end"
            onClose={() => setCreateMenuOpen(false)}
          >
            <div className="resource-create-menu" role="menu">
              {CREATABLE_SESSION_CONNECTION_OPTIONS.map((option) => (
                <button
                  key={option.kind}
                  type="button"
                  role="menuitem"
                  disabled={!createThread}
                  onClick={() => handleCreateConnection(option.kind)}
                >
                  <span className={`codicon ${option.icon}`} aria-hidden="true" />
                  <span className="resource-create-menu-copy">
                    <strong>{option.label}</strong>
                    <small>{option.description}</small>
                  </span>
                </button>
              ))}
            </div>
          </AnchoredOverlay>
        </div> : null}
      </div>

      {/* 这里只承载可重新 attach 的持久资源；一次性 Agent job 仍属于对话或事件视图。 */}
      {notice || (error && !waitingForFirstMessage) ? (
        <div className="resource-panel-notifications">
          {notice ? <div className="resource-notice" role="status">
            <span>{notice}</span>
            {noticeTechnicalDetails ? (
              <details>
                <summary>{t("resources.panel.details")}</summary>
                <code>{noticeTechnicalDetails}</code>
              </details>
            ) : null}
          </div> : null}
          {error && !waitingForFirstMessage ? <div className="resource-notice is-error failure-summary" role="alert">{failureSummary(t("resources.panel.loadFailed", error))}</div> : null}
        </div>
      ) : null}
      {waitingForFirstMessage && !loading ? (
        <div className="empty-state">{t("resources.panel.firstMessage")}</div>
      ) : null}

      {resourceGroups.length > 0 ? (
        <div className="resource-tree" role="tree" aria-label={t("resources.panel.connections")}>
          {resourceGroups.map((group) => {
            const isOpen = openGroups[group.key];
            const resourcesByKind = (["browser", "terminal", "background_task"] as const)
              .map((kind) => ({
                kind,
                resources: group.resources.filter((resource) => resource.kind === kind),
              }))
              .filter((kindGroup) => kindGroup.resources.length > 0);
            const showKindGroups = group.resources.length > 1;
            return (
              <section key={group.key} className={`resource-tree-group resource-tree-group-${group.key}`}>
                <button
                  type="button"
                  className="resource-tree-group-row"
                  aria-expanded={isOpen}
                  onClick={() => setOpenGroups((current) => ({
                    ...current,
                    [group.key]: !current[group.key],
                  }))}
                  title={group.description}
                >
                  <span className={`codicon codicon-chevron-${isOpen ? "down" : "right"}`} aria-hidden="true" />
                  <strong>{group.label}</strong>
                  <span>{formatNumber(group.resources.length)}</span>
                  <small>{group.description}</small>
                </button>
                {isOpen ? (
                  <div className="resource-tree-group-children" role="group">
                    {resourcesByKind.map((kindGroup) => (
                      <div key={kindGroup.kind} className="resource-tree-kind-group">
                        {showKindGroups ? (
                          <div className="resource-tree-kind-heading">
                            <span className={`codicon ${resourceKindIcon(kindGroup.kind)}`} aria-hidden="true" />
                            <span>{kindLabel(kindGroup.kind)}</span>
                            <span>{formatNumber(kindGroup.resources.length)}</span>
                          </div>
                        ) : null}
                        <div className={showKindGroups ? "resource-tree-kind-children" : undefined}>
                          {kindGroup.resources.map((resource) => (
                            <ResourceTreeRow
                              key={`${resource.kind}-${resource.resource_id}`}
                              previewContext={inlinePreview ? "thread" : undefined}
                              resource={resource}
                              selected={isPreviewedResource(resource, activePreviewPath)}
                              busy={busyResourceId === resource.resource_id}
                              onControl={handleControl}
                              onOwnerControl={onOwnerControl ? handleOwnerControl : undefined}
                              onCopy={handleCopy}
                              onOpenTerminal={handleOpenTerminal}
                              onOpenTerminalExtension={onOpenTerminalExtension}
                              onOpenBrowser={handleOpenBrowser}
                              onReplaceBrowser={() => handleCreateConnection("browser")}
                              extensionWindow={extensionWindow}
                            />
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : null}
              </section>
            );
          })}
        </div>
      ) : null}

      {!loading && !error && connectionResources.length === 0 && !waitingForFirstMessage ? (
        <div className="empty-state">{t("resources.panel.empty")}</div>
      ) : null}
    </section>
  );
}
