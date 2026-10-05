import { useState } from "react";
import type {
  SessionResource,
  SessionResourceAction,
  SessionResourceKind,
} from "../../../types/backend";
import { formatDateTime } from "../../../utils/format";
import type { ResourceThreadRef } from "../../../api/workspaceResources";
import { resourceThreadAssociations } from "../../../state/display/workspaceResourceDisplay";
import {
  actionLabel,
  metadataRows,
  resourceTreeDescription,
  resourceKindIcon,
  resourceTreeStatus,
  resourceTreeTitle,
} from "../../../state/display/resourceDisplay";

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
  onOwnerControl,
  onCopy,
  onOpenTerminal,
  onOpenTerminalExtension,
  onOpenBrowser,
  onReplaceBrowser,
  extensionWindow = false,
  canAttachCurrent = false,
}: {
  resource: SessionResource;
  selected: boolean;
  busy: boolean;
  onControl: (
    kind: SessionResourceKind,
    resourceId: string,
    action: SessionResourceAction,
  ) => void;
  onOwnerControl?: (resourceId: string, action: "promote" | "detach" | "attach", thread?: ResourceThreadRef) => void;
  onCopy: (resourceId: string) => void;
  onOpenTerminal: (resourceId: string) => void;
  onOpenTerminalExtension?: (resourceId: string) => void;
  onOpenBrowser: (resourceId: string) => void;
  onReplaceBrowser: () => void;
  extensionWindow?: boolean;
  canAttachCurrent?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const rows = metadataRows(resource);
  const canOpen = resource.status === "running" && resource.metadata.reconciliation_status === "verified"
    && (resource.metadata.retention_scope === "workspace" || resourceThreadAssociations(resource).length > 0);
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
            className={`resource-tree-kind codicon ${resourceKindIcon(resource.kind)}`}
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
          {["terminal", "browser"].includes(resource.kind) ? <div className="resource-tree-lifecycle">
            <span>{resource.metadata.retention_scope === "workspace" ? "保留到工作区" : "随来源线程释放"}</span>
            {resource.metadata.retention_scope === "thread" && onOwnerControl ? <button type="button" disabled={busy || resource.metadata.reconciliation_status !== "verified"} onClick={() => onOwnerControl(resource.resource_id, "promote")}>保留到工作区</button> : null}
            {resourceThreadAssociations(resource).filter((ref) => extensionWindow || ref.session_id === resource.session_id).map((ref) => <span key={`${ref.session_id}/${ref.thread_id}`} title={`${ref.session_id} / ${ref.thread_id}`}>
              线程 {ref.thread_id.slice(-8)}
              {onOwnerControl ? <button type="button" disabled={busy || resource.metadata.reconciliation_status !== "verified"} onClick={() => onOwnerControl(resource.resource_id, "detach", ref)}>解除关联</button> : null}
            </span>)}
            {canAttachCurrent && onOwnerControl ? <button type="button" disabled={busy || resource.metadata.reconciliation_status !== "verified"} onClick={() => onOwnerControl(resource.resource_id, "attach")}>关联当前会话</button> : null}
            {typeof resource.metadata.reclaim_at === "string" ? <span>无关联，将于 {formatDateTime(resource.metadata.reclaim_at)} 回收</span> : null}
            {resource.metadata.error ? <span role="alert">{String(resource.metadata.error)}</span> : null}
          </div> : null}
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
