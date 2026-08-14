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
  resourceTreeDescription,
  resourceTreeStatus,
  resourceTreeTitle,
} from "../state/resourceDisplay";

const RESOURCE_ICONS: Record<SessionResourceKind, string> = {
  browser: "codicon-globe",
  terminal: "codicon-terminal",
  background_task: "codicon-server-process",
};

function resourceActionIcon(action: SessionResourceAction): string {
  if (action === "delete") return "codicon-trash";
  if (action === "cancel") return "codicon-debug-stop";
  if (action === "pause") return "codicon-debug-pause";
  if (action === "resume") return "codicon-debug-continue";
  return "codicon-settings-gear";
}

export default function ResourceTreeRow({
  resource,
  selected,
  busy,
  onControl,
  onCopy,
  onOpenTerminal,
  onOpenTerminalExtension,
  onOpenBrowser,
  onReplaceBrowser,
  extensionWindow = false,
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
  onOpenTerminalExtension?: (resourceId: string) => void;
  onOpenBrowser: (resourceId: string) => void;
  onReplaceBrowser: () => void;
  extensionWindow?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const rows = metadataRows(resource);
  const canOpen = resource.status === "running" &&
    (resource.kind === "terminal" || resource.kind === "browser");
  const title = resourceTreeTitle(resource);
  const description = resourceTreeDescription(resource);

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
          <div className="resource-tree-actions" aria-label={`${title}操作`}>
            {canOpen && resource.kind === "terminal" ? (
              <button
                type="button"
                className="resource-action-open"
                disabled={busy}
                onClick={handleOpen}
                title={extensionWindow ? "在扩展窗口查看终端" : "在底部面板打开终端"}
                aria-label={extensionWindow ? "在扩展窗口查看终端" : "在底部面板打开终端"}
              >
                <span className={`codicon ${extensionWindow ? "codicon-open-preview" : "codicon-terminal"}`} aria-hidden="true" />
              </button>
            ) : null}
            {canOpen && resource.kind === "terminal" && onOpenTerminalExtension ? (
              <button
                type="button"
                className="resource-action-extension"
                disabled={busy}
                onClick={() => onOpenTerminalExtension(resource.resource_id)}
                title="在扩展窗口打开终端"
                aria-label="在扩展窗口打开终端"
              >
                <span className="codicon codicon-open-preview" aria-hidden="true" />
              </button>
            ) : null}
            {canOpen && resource.kind === "browser" ? (
              <button
                type="button"
                className="resource-action-open"
                disabled={busy}
                onClick={handleOpen}
                title={extensionWindow ? "打开浏览器预览" : "在扩展窗口打开浏览器"}
                aria-label={extensionWindow ? "打开浏览器预览" : "在扩展窗口打开浏览器"}
              >
                <span className="codicon codicon-open-preview" aria-hidden="true" />
              </button>
            ) : null}
            {resource.available_actions.map((action) => (
              <button
                key={action}
                type="button"
                className={`resource-action-${action}`}
                disabled={busy}
                onClick={() => onControl(resource.kind, resource.resource_id, action)}
                title={busy ? "处理中…" : actionLabel(resource, action)}
                aria-label={busy ? "处理中…" : actionLabel(resource, action)}
              >
                <span className={`codicon ${resourceActionIcon(action)}`} aria-hidden="true" />
              </button>
            ))}
            {resource.kind === "browser" &&
            (resource.status === "lost" || resource.available_actions.includes("resume")) ? (
              <button
                type="button"
                className="resource-action-replace"
                disabled={busy}
                onClick={onReplaceBrowser}
                title="新建替代浏览器"
                aria-label="新建替代浏览器"
              >
                <span className="codicon codicon-add" aria-hidden="true" />
              </button>
            ) : null}
          </div>
          {rows.length > 0 ? (
            <details className="resource-details">
              <summary>高级信息</summary>
              <div className="resource-tree-lifecycle">
                <span>创建 {formatDateTime(resource.created_at) || "未知"}</span>
                {resource.ended_at ? <span>结束 {formatDateTime(resource.ended_at)}</span> : null}
                <span title={resource.resource_id}>ID {resource.resource_id}</span>
              </div>
              <button type="button" onClick={() => onCopy(resource.resource_id)}>
                复制 ID
              </button>
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
