import type {
  SessionResource,
  SessionResourceAction,
  SessionResourceKind,
} from "../types/backend";
import { formatDateTime } from "../utils/format";
import {
  actionLabel,
  kindLabel,
  metadataRows,
  resourceName,
  resourceStateSummary,
  statusLabel,
} from "../state/resourceDisplay";

export default function ResourceCard({
  resource,
  busy,
  onControl,
  onCopy,
  onOpenTerminal,
  onOpenBrowser,
  onShowConversation,
}: {
  resource: SessionResource;
  busy: boolean;
  onControl: (
    kind: SessionResourceKind,
    resourceId: string,
    action: SessionResourceAction,
  ) => void;
  onCopy: (resourceId: string) => void;
  onOpenTerminal: (resourceId: string) => void;
  onOpenBrowser: (resourceId: string) => void;
  onShowConversation: (jobId?: string) => void;
}) {
  const rows = metadataRows(resource);
  const canOpenTerminal = resource.kind === "terminal" && resource.status === "running";
  const canOpenBrowser = resource.kind === "browser" && resource.status === "running";
  const stateSummary = resourceStateSummary(resource);

  return (
    <article className="panel-card resource-card">
      <div className="panel-card-head">
        <div className="panel-title-row">
          <span className={`resource-kind resource-kind-${resource.kind}`}>
            {kindLabel(resource.kind)}
          </span>
          <span className="panel-type">{resourceName(resource)}</span>
        </div>
        <div className={`resource-status resource-status-${resource.status}`}>
          {statusLabel(resource.status)}
        </div>
      </div>

      <div className="panel-meta resource-meta">
        <span title={resource.resource_id}>UUID: {resource.resource_id}</span>
        <span>创建: {formatDateTime(resource.created_at) || "未知"}</span>
        {resource.started_at ? (
          <span>开始: {formatDateTime(resource.started_at)}</span>
        ) : null}
        {resource.ended_at ? (
          <span>结束: {formatDateTime(resource.ended_at)}</span>
        ) : null}
      </div>

      {stateSummary ? (
        <div className="resource-state-summary">{stateSummary}</div>
      ) : null}

      <div className="resource-actions">
        <button
          type="button"
          className="resource-action-button"
          onClick={() => onCopy(resource.resource_id)}
          title="复制 UUID"
        >
          复制 UUID
        </button>
        {resource.kind === "terminal" ? (
          <button
            type="button"
            className="resource-action-button resource-action-open"
            disabled={!canOpenTerminal}
            onClick={() => {
              if (canOpenTerminal) {
                onOpenTerminal(resource.resource_id);
              }
            }}
            title={canOpenTerminal ? "在预览区打开并连接终端" : "终端已不可连接"}
          >
            打开终端
          </button>
        ) : null}
        {resource.kind === "browser" ? (
          <button
            type="button"
            className="resource-action-button resource-action-open"
            disabled={!canOpenBrowser}
            onClick={() => {
              if (canOpenBrowser) {
                onOpenBrowser(resource.resource_id);
              }
            }}
            title={canOpenBrowser ? "在预览区打开并连接浏览器" : "浏览器已不可连接"}
          >
            打开浏览器
          </button>
        ) : null}
        {resource.available_actions.map((action) => (
          <button
            key={action}
            type="button"
            className={`resource-action-button resource-action-${action}`}
            disabled={busy}
            onClick={() =>
              onControl(resource.kind, resource.resource_id, action)
            }
            title={`${actionLabel(resource, action)} ${resource.resource_id}`}
          >
            {actionLabel(resource, action)}
          </button>
        ))}
      </div>

      {rows.length > 0 ? (
        <details className="resource-details">
          <summary>详细信息</summary>
          <dl className="resource-detail-grid">
            {rows.map(([key, value]) => (
              <div key={key} className="resource-detail-row">
                <dt>{key}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}
    </article>
  );
}
