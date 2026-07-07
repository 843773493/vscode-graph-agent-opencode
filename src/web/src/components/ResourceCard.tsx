import type {
  SessionResource,
  SessionResourceAction,
  SessionResourceKind,
} from "../types/backend";
import { formatDateTime } from "../utils/format";
import { toBrowserReachableTerminalUrl } from "../utils/terminalUrls";
import {
  actionLabel,
  kindLabel,
  metadataRows,
  resourceStateSummary,
  statusLabel,
} from "../state/resourceDisplay";

export default function ResourceCard({
  resource,
  busy,
  onControl,
  onCopy,
  onOpenTerminal,
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
  onShowConversation: (jobId?: string) => void;
}) {
  const rows = metadataRows(resource);
  const attachUrl =
    typeof resource.metadata.attach_url === "string"
      ? toBrowserReachableTerminalUrl(resource.metadata.attach_url)
      : null;
  const canOpenTerminal = resource.kind === "terminal" && resource.status === "running";
  const stateSummary = resourceStateSummary(resource);

  return (
    <article className="panel-card resource-card">
      <div className="panel-card-head">
        <div className="panel-title-row">
          <span className={`resource-kind resource-kind-${resource.kind}`}>
            {kindLabel(resource.kind)}
          </span>
          <span className="panel-type">{resource.name}</span>
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
        {attachUrl ? (
          <button
            type="button"
            className="resource-action-button resource-action-open"
            disabled={!canOpenTerminal}
            onClick={() => {
              if (canOpenTerminal) {
                window.open(attachUrl, "_blank", "noopener,noreferrer");
                onOpenTerminal(resource.resource_id);
              }
            }}
            title={canOpenTerminal ? "打开终端页面" : "终端已不可连接"}
          >
            打开终端
          </button>
        ) : null}
        {resource.kind === "job" ? (
          <button
            type="button"
            className="resource-action-button"
            onClick={() => onShowConversation(resource.resource_id)}
            title="查看该 Job 对应回复"
          >
            查看回复
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
