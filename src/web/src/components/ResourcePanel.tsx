import { useEffect, useState } from "react";
import type {
  SessionResource,
  SessionResourceAction,
  SessionResourceKind,
} from "../types/backend";
import { formatDateTime } from "../utils/format";
import ResourceCard from "./ResourceCard";
import {
  actionLabelForKind,
  isClosedBackgroundTask,
  statusLabel,
} from "../state/resourceDisplay";

export default function ResourcePanel({
  resources,
  loading,
  error,
  loadedAt,
  sessionId,
  onRefresh,
  onControl,
  onOpenTerminalPreview,
  onOpenBrowserPreview,
  onShowConversation,
}: {
  resources: SessionResource[];
  loading: boolean;
  error: string | null;
  loadedAt: string | null;
  sessionId: string;
  onRefresh: () => void;
  onControl: (
    kind: SessionResourceKind,
    resourceId: string,
    action: SessionResourceAction,
  ) => Promise<void>;
  onOpenTerminalPreview: (terminalId: string, attachUrl: string) => void;
  onOpenBrowserPreview: (browserId: string, attachUrl: string) => void;
  onShowConversation: (jobId?: string) => void;
}) {
  const [busyResourceId, setBusyResourceId] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [openedTerminalId, setOpenedTerminalId] = useState<string | null>(null);
  const [openedBrowserId, setOpenedBrowserId] = useState<string | null>(null);
  const [closedGroupOpen, setClosedGroupOpen] = useState(false);

  useEffect(() => {
    setClosedGroupOpen(false);
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
    setNotice(
      `终端 ${openedTerminalId} ${statusLabel(terminal.status)}，当前不可连接；历史信息仍可在后台连接卡片查看。`,
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
    setNotice(
      `浏览器 ${openedBrowserId} ${statusLabel(browser.status)}，当前不可连接；历史信息仍可在后台连接卡片查看。`,
    );
    setOpenedBrowserId(null);
  }, [openedBrowserId, resources]);

  const handleControl = (
    kind: SessionResourceKind,
    resourceId: string,
    action: SessionResourceAction,
  ) => {
    if (action === "delete") {
      const confirmed = window.confirm(
        kind === "terminal"
          ? `确认删除终端 ${resourceId}？删除后当前终端不可再 attach，只保留历史记录。`
          : `确认删除后台连接 ${resourceId}？`,
      );
      if (!confirmed) {
        return;
      }
    }
    setBusyResourceId(resourceId);
    setNotice("");
    setOpenedTerminalId(null);
    setOpenedBrowserId(null);
    void onControl(kind, resourceId, action)
      .then(() => {
        setNotice(`已执行 ${actionLabelForKind(kind, action)}: ${resourceId}`);
      })
      .catch((controlError: unknown) => {
        setNotice(
          `操作失败: ${
            controlError instanceof Error
              ? controlError.message
              : String(controlError)
          }`,
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
          .then(() => setNotice(`已复制 UUID: ${resourceId}`))
          .catch(() => {
            fallbackCopy();
            setNotice(`已复制 UUID: ${resourceId}`);
          });
        return;
      }
      fallbackCopy();
      setNotice(`已复制 UUID: ${resourceId}`);
    } catch (copyError) {
      setNotice(
        `复制失败: ${
          copyError instanceof Error ? copyError.message : String(copyError)
        }`,
      );
    }
  };

  const handleOpenTerminal = (resourceId: string, attachUrl: string) => {
    setOpenedTerminalId(resourceId);
    onOpenTerminalPreview(resourceId, attachUrl);
    setNotice(`已在预览区连接终端: ${resourceId}`);
  };
  const handleOpenBrowser = (resourceId: string, attachUrl: string) => {
    setOpenedBrowserId(resourceId);
    onOpenBrowserPreview(resourceId, attachUrl);
    setNotice(`已在预览区连接浏览器: ${resourceId}`);
  };
  const historicalResourceCount = resources.filter(
    (resource) =>
      resource.status === "deleted" ||
      resource.status === "lost" ||
      resource.metadata.resource_source === "历史记录",
  ).length;
  const closedBackgroundTasks = resources.filter(isClosedBackgroundTask);
  const openResources = resources.filter(
    (resource) => !isClosedBackgroundTask(resource),
  );
  const resourceCountText =
    historicalResourceCount > 0
      ? `${resources.length} 个连接（含 ${historicalResourceCount} 个历史/不可连接连接）`
      : `${resources.length} 个连接`;
  const waitingForFirstMessage = !sessionId || error === "当前没有会话可读取资源";

  return (
    <section className="panel-view resource-panel">
      <div className="panel-header">
        <div className="panel-title">后台连接</div>
        <div className="panel-header-meta">
          <span>{resourceCountText}</span>
          <span>{sessionId || "无会话"}</span>
          {loadedAt ? <span>读取于 {formatDateTime(loadedAt)}</span> : null}
        </div>
        <button
          type="button"
          className="resource-refresh-button"
          onClick={onRefresh}
          disabled={loading || !sessionId}
          title="刷新后台连接"
        >
          刷新
        </button>
        <button
          type="button"
          className="resource-refresh-button"
          onClick={() => onShowConversation()}
          disabled={!sessionId}
          title="查看默认对话回复"
        >
          查看最新回复
        </button>
      </div>

      <div className="resource-status-note">
        这里只展示可保留、可重新打开或可连接的后台对象，例如持久终端、浏览器页面和持续后台任务；一次性 agent job 请在默认视图或事件视图查看。
      </div>
      {notice ? <div className="resource-notice">{notice}</div> : null}
      {loading ? <div className="empty-state">正在读取后台连接...</div> : null}
      {error && !waitingForFirstMessage ? (
        <div className="empty-state">后台连接加载失败：{error}</div>
      ) : null}
      {waitingForFirstMessage && !loading ? (
        <div className="empty-state">发送第一条消息后，这里会显示当前会话创建的可连接后台对象。</div>
      ) : null}

      {!loading && !error && openResources.length > 0 ? (
        <div className="panel-list">
          {openResources.map((resource) => (
            <ResourceCard
              key={`${resource.kind}-${resource.resource_id}`}
              resource={resource}
              busy={busyResourceId === resource.resource_id}
              onControl={handleControl}
              onCopy={handleCopy}
              onOpenTerminal={handleOpenTerminal}
              onOpenBrowser={handleOpenBrowser}
              onShowConversation={onShowConversation}
            />
          ))}
        </div>
      ) : null}

      {!loading && !error && closedBackgroundTasks.length > 0 ? (
        <section className="resource-closed-group">
          <button
            type="button"
            className="resource-closed-summary"
            aria-expanded={closedGroupOpen}
            onClick={() => setClosedGroupOpen((open) => !open)}
          >
            已关闭后台连接 ({closedBackgroundTasks.length})
            <span aria-hidden="true">{closedGroupOpen ? "⌄" : "›"}</span>
          </button>
          {closedGroupOpen ? (
            <div className="panel-list">
              {closedBackgroundTasks.map((resource) => (
                <ResourceCard
                  key={`${resource.kind}-${resource.resource_id}`}
                  resource={resource}
                  busy={busyResourceId === resource.resource_id}
                  onControl={handleControl}
                  onCopy={handleCopy}
                  onOpenTerminal={handleOpenTerminal}
                  onOpenBrowser={handleOpenBrowser}
                  onShowConversation={onShowConversation}
                />
              ))}
            </div>
          ) : null}
        </section>
      ) : null}

      {!loading && !error && resources.length === 0 ? (
        <div className="empty-state">当前会话还没有后台连接</div>
      ) : null}
    </section>
  );
}
