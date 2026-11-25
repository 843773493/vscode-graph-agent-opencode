import { t } from "../../../../../shared/core/i18n/index.js";
import { useState } from "react";
import type {
  SessionResource,
  SessionResourceAction,
  SessionResourceKind,
} from "../../../types/backend";
import { formatDateTime } from "../../../utils/format";
import type { ResourceThreadRef } from "../../../api/workspaceResources";
import { resourceThreadAssociations } from "../../../state/display/workspaceResourceDisplay";
import { failureSummary } from "../../../../../shared/core/failurePresentation";
import {
  actionLabel,
  kindLabel,
  resourceMetadataRows,
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
  previewContext,
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
  onReplaceBrowser?: () => void;
  extensionWindow?: boolean;
  previewContext?: "thread" | "workspace";
  canAttachCurrent?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const metadataRows = expanded ? resourceMetadataRows(resource) : [];
  const canOpen = resource.status === "running" && resource.metadata.reconciliation_status === "verified"
    && (resource.metadata.retention_scope === "workspace" || resourceThreadAssociations(resource).length > 0);
  const title = resourceTreeTitle(resource);
  const description = resourceTreeDescription(resource);
  const openLabel = previewContext === "thread" ? t("resources.open.thread", kindLabel(resource.kind))
    : previewContext === "workspace" ? t("resources.open.workspaceBrowser")
      : resource.kind === "terminal" ? extensionWindow ? t("resources.open.extensionTerminalView") : t("resources.open.bottomTerminal")
        : extensionWindow ? t("resources.open.previewBrowser") : t("resources.open.extensionBrowser");

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
          aria-label={expanded ? t("resources.collapse", title) : t("resources.expand", title)}
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
            {selected ? t("resources.current") : resourceTreeStatus(resource)}
          </span>
        </button>
      </div>

      {expanded ? (
        <div className="resource-tree-detail">
          <div className="resource-tree-actions" aria-label={t("resources.actions", title)}>
            {canOpen && resource.kind === "terminal" ? (
              <button
                type="button"
                className="resource-action-open"
                disabled={busy}
                onClick={handleOpen}
                title={openLabel}
                aria-label={openLabel}
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
                title={t("resources.open.extensionTerminal")}
                aria-label={t("resources.open.extensionTerminal")}
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
                title={openLabel}
                aria-label={openLabel}
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
                title={busy ? t("resources.busy") : actionLabel(resource, action)}
                aria-label={busy ? t("resources.busy") : actionLabel(resource, action)}
              >
                <span className={`codicon ${resourceActionIcon(action)}`} aria-hidden="true" />
              </button>
            ))}
            {resource.kind === "browser" && onReplaceBrowser &&
            (resource.status === "lost" || resource.available_actions.includes("resume")) ? (
              <button
                type="button"
                className="resource-action-replace"
                disabled={busy}
                onClick={onReplaceBrowser}
                title={t("resources.replaceBrowser")}
                aria-label={t("resources.replaceBrowser")}
              >
                <span className="codicon codicon-add" aria-hidden="true" />
              </button>
            ) : null}
          </div>
          {["terminal", "browser"].includes(resource.kind) ? <div className="resource-tree-lifecycle">
            <span>{resource.metadata.retention_scope === "workspace" ? t("resources.keep") : t("resources.releaseWithThread")}</span>
            {resource.metadata.retention_scope === "thread" && onOwnerControl ? <button type="button" disabled={busy || resource.metadata.reconciliation_status !== "verified"} onClick={() => onOwnerControl(resource.resource_id, "promote")}>{t("resources.keep")}</button> : null}
            {resourceThreadAssociations(resource).filter((ref) => extensionWindow || previewContext === "workspace" || ref.session_id === resource.session_id).map((ref) => <span key={`${ref.session_id}/${ref.thread_id}`} title={`${ref.session_id} / ${ref.thread_id}`}>
              {t("resources.thread", ref.thread_id.slice(-8))}
              {onOwnerControl ? <button type="button" disabled={busy || resource.metadata.reconciliation_status !== "verified"} onClick={() => onOwnerControl(resource.resource_id, "detach", ref)}>{t("resources.detach")}</button> : null}
            </span>)}
            {canAttachCurrent && onOwnerControl ? <button type="button" disabled={busy || resource.metadata.reconciliation_status !== "verified"} onClick={() => onOwnerControl(resource.resource_id, "attach")}>{t("resources.attach")}</button> : null}
            {typeof resource.metadata.reclaim_at === "string" ? <span>{t("resources.reclaim", formatDateTime(resource.metadata.reclaim_at))}</span> : null}
            {typeof resource.metadata.error === "string" && resource.metadata.error ? <span className="failure-summary" role="alert">{failureSummary(resource.metadata.error)}</span> : null}
          </div> : null}
          {metadataRows.length > 0 ? (
            <details className="resource-details">
              <summary>{t("resources.advanced")}</summary>
              <div className="resource-tree-lifecycle">
                <span>{t("resources.created", formatDateTime(resource.created_at) || t("resources.unknown"))}</span>
                {resource.ended_at ? <span>{t("resources.ended", formatDateTime(resource.ended_at))}</span> : null}
                <span title={resource.resource_id}>ID {resource.resource_id}</span>
              </div>
              <button type="button" onClick={() => onCopy(resource.resource_id)}>

                {t("resources.copyId")}
              </button>
              <dl className="resource-detail-grid">
                {metadataRows.map(([key, value]) => (
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
