import { useEffect, useRef, useState } from "react";
import type {
  SessionResource,
  SessionResourceAction,
  SessionResourceKind,
} from "../types/backend";
import { useWarmConfirm } from "./WarmConfirmProvider";
import {
  actionLabelForKind,
  groupSessionResources,
  isPreviewedResource,
  kindLabel,
  type ResourceAttentionGroup,
  statusLabel,
} from "../state/resourceDisplay";
import { CREATABLE_SESSION_CONNECTIONS } from "../state/sessionConnections";
import type { CreatableSessionConnectionKind } from "../types/frontend";
import AnchoredOverlay from "./AnchoredOverlay";
import ResourceTreeRow from "./ResourceTreeRow";

const DEFAULT_GROUP_OPEN: Record<ResourceAttentionGroup, boolean> = {
  active: true,
  attention: true,
  available: true,
  sleeping: false,
  history: false,
};

export default function ResourcePanel({
  resources,
  loading,
  error,
  loadedAt,
  sessionId,
  workspaceId,
  extensionWindow = false,
  activePreviewPath,
  onRefresh,
  onControl,
  onOpenTerminalPreview,
  onOpenTerminalExtension,
  onOpenBrowserPreview,
  onCloseResourcePreview,
  onCreateConnection,
}: {
  resources: SessionResource[];
  loading: boolean;
  error: string | null;
  loadedAt: string | null;
  sessionId: string;
  workspaceId: string | null;
  extensionWindow?: boolean;
  activePreviewPath: string | null;
  onRefresh: () => void;
  onControl: (
    kind: SessionResourceKind,
    resourceId: string,
    action: SessionResourceAction,
  ) => Promise<void>;
  onOpenTerminalPreview: (terminalId: string) => void;
  onOpenTerminalExtension?: (terminalId: string) => void;
  onOpenBrowserPreview: (browserId: string) => void;
  onCloseResourcePreview: (
    kind: Extract<SessionResourceKind, "terminal" | "browser">,
    resourceId: string,
  ) => Promise<void>;
  onCreateConnection: (kind: CreatableSessionConnectionKind) => Promise<void>;
}) {
  const [busyResourceId, setBusyResourceId] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [noticeTechnicalDetails, setNoticeTechnicalDetails] = useState("");
  const [openedTerminalId, setOpenedTerminalId] = useState<string | null>(null);
  const [openedBrowserId, setOpenedBrowserId] = useState<string | null>(null);
  const [openGroups, setOpenGroups] = useState(DEFAULT_GROUP_OPEN);
  const [createMenuOpen, setCreateMenuOpen] = useState(false);
  const [creatingKind, setCreatingKind] = useState<CreatableSessionConnectionKind | null>(null);
  const createMenuAnchorRef = useRef<HTMLDivElement | null>(null);
  const confirm = useWarmConfirm();

  useEffect(() => {
    setOpenGroups(DEFAULT_GROUP_OPEN);
  }, [sessionId]);

  useEffect(() => {
    if (!openedTerminalId) {
      return;
    }
    const terminal = resources.find(
      (resource) =>
        resource.kind === "terminal" && resource.resource_id === openedTerminalId,
    );
    if (!terminal || terminal.status === "running") {
      return;
    }
    setNoticeTechnicalDetails("");
    setNotice(
      `终端 ${openedTerminalId} ${statusLabel(terminal.status)}，当前不可连接；历史信息仍可在展开详情中查看。`,
    );
    setOpenedTerminalId(null);
  }, [openedTerminalId, resources]);

  useEffect(() => {
    if (!openedBrowserId) {
      return;
    }
    const browser = resources.find(
      (resource) =>
        resource.kind === "browser" && resource.resource_id === openedBrowserId,
    );
    if (!browser || browser.status === "running") {
      return;
    }
    setNoticeTechnicalDetails("");
    setNotice(
      `浏览器 ${openedBrowserId} ${statusLabel(browser.status)}，当前不可连接；历史信息仍可在展开详情中查看。`,
    );
    setOpenedBrowserId(null);
  }, [openedBrowserId, resources]);

  const handleControl = async (
    kind: SessionResourceKind,
    resourceId: string,
    action: SessionResourceAction,
  ) => {
    if (action === "delete") {
      const targetLabel = kind === "terminal"
        ? "终端"
        : kind === "browser"
          ? "浏览器"
          : "后台任务";
      const confirmed = await confirm({
        title: `删除${targetLabel}`,
        message: kind === "terminal"
          ? `确认删除终端 ${resourceId}？删除后当前终端不可再 attach，只保留历史记录。`
          : kind === "browser"
            ? `确认删除浏览器 ${resourceId}？删除后无法恢复该页面，只保留历史记录。`
            : `确认删除后台任务 ${resourceId}？`,
        confirmText: "删除",
        danger: true,
      });
      if (!confirmed) {
        return;
      }
    }
    setBusyResourceId(resourceId);
    setNotice("");
    setNoticeTechnicalDetails("");
    setOpenedTerminalId(null);
    setOpenedBrowserId(null);
    void onControl(kind, resourceId, action)
      .then(async () => {
        if (action === "delete" && (kind === "terminal" || kind === "browser")) {
          await onCloseResourcePreview(kind, resourceId);
        }
        if (action === "resume" && kind === "browser") {
          handleOpenBrowser(resourceId);
        }
        setNoticeTechnicalDetails("");
        setNotice(`已${actionLabelForKind(kind, action)}${kindLabel(kind)}：${resourceId}`);
      })
      .catch((controlError: unknown) => {
        const technicalDetails = controlError instanceof Error
          ? controlError.message
          : String(controlError);
        setNoticeTechnicalDetails(technicalDetails);
        setNotice(
          kind === "browser" && action === "resume"
            ? "重新打开失败：恢复检查点不可用或浏览器服务未就绪。你可以重试，或新建替代浏览器；原记录仍保留。"
            : `未能${actionLabelForKind(kind, action)}。请重试；问题持续时可查看技术详情。`,
        );
      })
      .finally(() => {
        setBusyResourceId(null);
      });
  };

  const handleCopy = (resourceId: string) => {
    const fallbackCopy = () => {
      const textarea = document.createElement("textarea");
      textarea.value = resourceId;
      textarea.style.position = "fixed";
      textarea.style.left = "-9999px";
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      const copied = document.execCommand("copy");
      textarea.remove();
      if (!copied) {
        throw new Error("浏览器拒绝复制");
      }
    };

    try {
      if (navigator.clipboard) {
        void navigator.clipboard
          .writeText(resourceId)
          .then(() => {
            setNoticeTechnicalDetails("");
            setNotice(`已复制 UUID: ${resourceId}`);
          })
          .catch(() => {
            fallbackCopy();
            setNoticeTechnicalDetails("");
            setNotice(`已复制 UUID: ${resourceId}`);
          });
        return;
      }
      fallbackCopy();
      setNoticeTechnicalDetails("");
      setNotice(`已复制 UUID: ${resourceId}`);
    } catch (copyError) {
      setNotice(
        `复制失败: ${
          copyError instanceof Error ? copyError.message : String(copyError)
        }`,
      );
    }
  };

  const handleOpenTerminal = (resourceId: string) => {
    if (!workspaceId) {
      setNotice("打开终端失败：当前会话缺少 Gateway workspace_id");
      return;
    }
    setOpenedTerminalId(resourceId);
    onOpenTerminalPreview(resourceId);
    setNoticeTechnicalDetails("");
    setNotice("已在主窗口底部面板打开终端；连接管理请在运行与连接中查看");
  };
  const handleOpenBrowser = (resourceId: string) => {
    if (!workspaceId) {
      setNotice("打开浏览器失败：当前会话缺少 Gateway workspace_id");
      return;
    }
    setOpenedBrowserId(resourceId);
    onOpenBrowserPreview(resourceId);
    setNoticeTechnicalDetails("");
    setNotice("已在扩展窗口打开浏览器预览；连接状态请在当前运行与连接界面查看");
  };
  const handleCreateConnection = (kind: CreatableSessionConnectionKind) => {
    setCreateMenuOpen(false);
    setCreatingKind(kind);
    setNotice("");
    setNoticeTechnicalDetails("");
    void onCreateConnection(kind)
      .then(() => {
        const option = CREATABLE_SESSION_CONNECTIONS.find(
          (candidate) => candidate.kind === kind,
        );
        setNotice(
          kind === "terminal"
            ? "终端创建成功，已在主窗口底部面板打开"
            : `${option?.label ?? "新建连接"}成功；连接管理请在运行与连接中查看`,
        );
      })
      .catch((createError: unknown) => {
        setNotice("新建连接失败。请确认当前工作区仍在线后重试。");
        setNoticeTechnicalDetails(
          createError instanceof Error ? createError.message : String(createError),
        );
      })
      .finally(() => setCreatingKind(null));
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
  const waitingForFirstMessage = !sessionId || error === "当前没有会话可读取资源";

  return (
    <section
      className={`panel-view resource-panel${extensionWindow ? " extension-window-region" : ""}`}
      data-extension-region={extensionWindow ? "secondary" : undefined}
    >
      <div className="panel-header">
        <div
          className="panel-title"
          title={`${sessionId || "无会话"}${loadedAt ? ` · 最近读取 ${loadedAt}` : ""}`}
        >
          连接总数 <span className="resource-total-count">{connectionResources.length}</span>
        </div>
        <div className="panel-header-meta">
          <span>{activeCount} 个正在使用</span>
          {historyCount > 0 ? <span>{historyCount} 个历史</span> : null}
          {attentionCount > 0 ? <span className="resource-attention-count">{attentionCount} 个需要处理</span> : null}
        </div>
        <button
          type="button"
          className="resource-icon-button"
          onClick={onRefresh}
          disabled={loading || !sessionId}
          title="刷新后台连接"
          aria-label="刷新后台连接"
        >
          <span className={`codicon codicon-refresh${loading ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />
        </button>
        {sessionId && workspaceId ? <div ref={createMenuAnchorRef} className="resource-create-control">
          <button
            type="button"
            className="resource-refresh-button resource-create-button"
            onClick={() => setCreateMenuOpen((open) => !open)}
            disabled={creatingKind !== null}
            aria-haspopup="menu"
            aria-expanded={createMenuOpen}
          >
            {creatingKind ? "创建中" : "新建连接"}
            <span className="codicon codicon-chevron-down" aria-hidden="true" />
          </button>
          <AnchoredOverlay
            open={createMenuOpen}
            anchorRef={createMenuAnchorRef}
            placement="bottom-end"
            onClose={() => setCreateMenuOpen(false)}
          >
            <div className="resource-create-menu" role="menu">
              {CREATABLE_SESSION_CONNECTIONS.map((option) => (
                <button
                  key={option.kind}
                  type="button"
                  role="menuitem"
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
      {notice ? (
        <div className="resource-notice" role="status">
          <span>{notice}</span>
          {noticeTechnicalDetails ? (
            <details>
              <summary>技术详情</summary>
              <code>{noticeTechnicalDetails}</code>
            </details>
          ) : null}
        </div>
      ) : null}
      {loading ? <div className="empty-state">正在读取后台连接...</div> : null}
      {error && !waitingForFirstMessage ? (
        <div className="empty-state">后台连接加载失败：{error}</div>
      ) : null}
      {waitingForFirstMessage && !loading ? (
        <div className="empty-state">发送第一条消息后，这里会显示当前会话创建的可连接后台对象。</div>
      ) : null}

      {!loading && !error && resourceGroups.length > 0 ? (
        <div className="resource-tree" role="tree" aria-label="后台连接">
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
                  <span>{group.resources.length}</span>
                  <small>{group.description}</small>
                </button>
                {isOpen ? (
                  <div className="resource-tree-group-children" role="group">
                    {resourcesByKind.map((kindGroup) => (
                      <div key={kindGroup.kind} className="resource-tree-kind-group">
                        {showKindGroups ? (
                          <div className="resource-tree-kind-heading">
                            <span className={`codicon ${kindGroup.kind === "browser" ? "codicon-globe" : kindGroup.kind === "terminal" ? "codicon-terminal" : "codicon-server-process"}`} aria-hidden="true" />
                            <span>{kindLabel(kindGroup.kind)}</span>
                            <span>{kindGroup.resources.length}</span>
                          </div>
                        ) : null}
                        <div className={showKindGroups ? "resource-tree-kind-children" : undefined}>
                          {kindGroup.resources.map((resource) => (
                            <ResourceTreeRow
                              key={`${resource.kind}-${resource.resource_id}`}
                              resource={resource}
                              selected={isPreviewedResource(resource, activePreviewPath)}
                              busy={busyResourceId === resource.resource_id}
                              onControl={handleControl}
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
        <div className="empty-state">当前会话还没有后台连接</div>
      ) : null}
    </section>
  );
}
