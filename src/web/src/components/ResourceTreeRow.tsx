import { useState } from "react";
import type {
  SessionResource,
  SessionResourceAction,
  SessionResourceKind,
} from "../types/backend";
import { formatDateTime } from "../utils/format";
import {
  actionLabel,
  metadataRows,
  resourceStateSummary,
  resourceTreeDescription,
  resourceTreeStatus,
  resourceTreeTitle,
} from "../state/resourceDisplay";

const RESOURCE_ICONS: Record<SessionResourceKind, string> = {
  browser: "codicon-globe",
  terminal: "codicon-terminal",
  background_task: "codicon-server-process",
};

export default function ResourceTreeRow({
  resource,
  selected,
  busy,
  onControl,
  onCopy,
  onOpenTerminal,
  onOpenBrowser,
  onShowConversation,
  onReplaceBrowser,
}: {
  resource: SessionResource;
  selected: boolean;
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
  onReplaceBrowser: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const rows = metadataRows(resource);
  const canOpen = resource.status === "running" &&
    (resource.kind === "terminal" || resource.kind === "browser");
  const title = resourceTreeTitle(resource);
  const description = resourceTreeDescription(resource);
  const stateSummary = resourceStateSummary(resource);

  const handleOpen = () => {
    if (resource.kind === "terminal" && canOpen) {
      onOpenTerminal(resource.resource_id);
      return;
    }
    if (resource.kind === "browser" && canOpen) {
      onOpenBrowser(resource.resource_id);
      return;
    }
    if (resource.kind === "browser" && resource.available_actions.includes("resume")) {
      onControl(resource.kind, resource.resource_id, "resume");
      return;
    }
    if (resource.kind === "background_task") {
      const jobId = typeof resource.metadata.job_id === "string"
        ? resource.metadata.job_id
        : undefined;
      onShowConversation(jobId);
      return;
    }
    setExpanded((open) => !open);
  };

  return (
    <article
      className={`resource-tree-item${selected ? " is-selected" : ""}${expanded ? " is-expanded" : ""}`}
      data-resource-kind={resource.kind}
      data-resource-status={resource.status}
      aria-busy={busy}
    >
      <div className="resource-tree-row">
        <button
          type="button"
          className="resource-tree-chevron"
          aria-label={expanded ? `收起 ${title} 详情` : `展开 ${title} 详情`}
          aria-expanded={expanded}
          onClick={() => setExpanded((open) => !open)}
        >
          <span
            className={`codicon codicon-chevron-${expanded ? "down" : "right"}`}
            aria-hidden="true"
          />
        </button>
        <button
          type="button"
          className="resource-tree-main"
          disabled={busy}
          onClick={handleOpen}
          title={`${title} · ${description}`}
        >
          <span
            className={`resource-tree-kind codicon ${RESOURCE_ICONS[resource.kind]}`}
            aria-hidden="true"
          />
          <span className="resource-tree-copy">
            <strong>{title}</strong>
            <small>{description}</small>
          </span>
          <span className={`resource-tree-status resource-status-${resource.status}`}>
            {selected ? "当前" : resourceTreeStatus(resource)}
          </span>
        </button>
      </div>

      {expanded ? (
        <div className="resource-tree-detail">
          {stateSummary ? <p>{stateSummary}</p> : null}
          <div className="resource-tree-lifecycle">
            <span>创建 {formatDateTime(resource.created_at) || "未知"}</span>
            {resource.ended_at ? <span>结束 {formatDateTime(resource.ended_at)}</span> : null}
            <span title={resource.resource_id}>ID {resource.resource_id}</span>
          </div>
          <div className="resource-tree-actions">
            <button type="button" onClick={() => onCopy(resource.resource_id)}>
              复制 ID
            </button>
            {canOpen ? (
              <button type="button" className="resource-action-open" onClick={handleOpen}>
                {resource.kind === "terminal" ? "打开终端" : "打开浏览器"}
              </button>
            ) : null}
            {resource.available_actions.map((action) => (
              <button
                key={action}
                type="button"
                className={`resource-action-${action}`}
                disabled={busy}
                onClick={() => onControl(resource.kind, resource.resource_id, action)}
              >
                {busy ? "处理中…" : actionLabel(resource, action)}
              </button>
            ))}
            {resource.kind === "browser" &&
            (resource.status === "lost" || resource.available_actions.includes("resume")) ? (
              <button type="button" className="resource-action-open" disabled={busy} onClick={onReplaceBrowser}>
                新建替代浏览器
              </button>
            ) : null}
          </div>
          {rows.length > 0 ? (
            <details className="resource-details">
              <summary>技术详情</summary>
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
        </div>
      ) : null}
    </article>
  );
}
