import { useEffect, useState } from "react";
import type {
  SessionResource,
  SessionResourceAction,
  SessionResourceKind,
} from "../types/backend";
import { formatDateTime } from "../utils/format";
import ResourceCard from "./ResourceCard";
import { actionLabelForKind, statusLabel } from "../state/resourceDisplay";

export default function ResourcePanel({
  resources,
  loading,
  error,
  loadedAt,
  sessionId,
  onRefresh,
  onControl,
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
  onShowConversation: (jobId?: string) => void;
}) {
  const [busyResourceId, setBusyResourceId] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [openedTerminalId, setOpenedTerminalId] = useState<string | null>(null);

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
      `终端 ${openedTerminalId} ${statusLabel(terminal.status)}，当前不可连接；历史信息仍可在资源卡片查看。`,
    );
    setOpenedTerminalId(null);
  }, [openedTerminalId, resources]);

  const handleControl = (
    kind: SessionResourceKind,
    resourceId: string,
    action: SessionResourceAction,
  ) => {
    if (action === "delete") {
      const confirmed = window.confirm(
        kind === "terminal"
          ? `确认删除终端 ${resourceId}？删除后当前终端不可再 attach，只保留历史记录。`
          : `确认删除资源 ${resourceId}？`,
      );
      if (!confirmed) {
        return;
      }
    }
    setBusyResourceId(resourceId);
    setNotice("");
    setOpenedTerminalId(null);
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

  const handleOpenTerminal = (resourceId: string) => {
    setOpenedTerminalId(resourceId);
    setNotice(`已打开终端页面: ${resourceId}。如果没有看到，请检查浏览器新标签页。`);
  };
  const historicalResourceCount = resources.filter(
    (resource) =>
      resource.status === "deleted" ||
      resource.status === "lost" ||
      resource.metadata.resource_source === "历史记录",
  ).length;
  const resourceCountText =
    historicalResourceCount > 0
      ? `${resources.length} 个资源（含 ${historicalResourceCount} 个历史/不可连接资源）`
      : `${resources.length} 个资源`;

  return (
    <section className="panel-view resource-panel">
      <div className="panel-header">
        <div className="panel-title">资源视图</div>
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
          title="刷新资源"
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
        运行中终端可打开并 attach；已删除或已断开的终端只保留历史信息，当前不可连接。
      </div>
      {notice ? <div className="resource-notice">{notice}</div> : null}
      {loading ? <div className="empty-state">正在读取会话资源...</div> : null}
      {error ? <div className="empty-state">会话资源加载失败：{error}</div> : null}

      {!loading && !error && resources.length > 0 ? (
        <div className="panel-list">
          {resources.map((resource) => (
            <ResourceCard
              key={`${resource.kind}-${resource.resource_id}`}
              resource={resource}
              busy={busyResourceId === resource.resource_id}
              onControl={handleControl}
              onCopy={handleCopy}
              onOpenTerminal={handleOpenTerminal}
              onShowConversation={onShowConversation}
            />
          ))}
        </div>
      ) : null}

      {!loading && !error && resources.length === 0 ? (
        <div className="empty-state">当前会话还没有后台资源</div>
      ) : null}
    </section>
  );
}
