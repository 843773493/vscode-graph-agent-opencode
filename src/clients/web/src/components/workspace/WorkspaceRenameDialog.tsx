import { gatewayWorkspaceKey, nullableGatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { useContext, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { t } from "../../../../shared/core/i18n/index.js";
import { OverlayRootContext } from "../overlays/OverlayRootContext";
import type { GatewayWorkspace } from "../../types/backend";
import { errorMessage } from "../../utils/errorMessage";
import { HttpRequestError } from "../../api/http";

interface WorkspaceRenameDialogProps {
  workspace: GatewayWorkspace | null;
  onClose: () => void;
  onSubmit: (workspaceId: string, name: string) => Promise<string>;
}

export default function WorkspaceRenameDialog({
  workspace,
  onClose,
  onSubmit,
}: WorkspaceRenameDialogProps) {
  const overlayRoot = useContext(OverlayRootContext);
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const workspaceId = nullableGatewayWorkspaceKey(workspace);
  const workspaceName = workspace?.name ?? null;

  useEffect(() => {
    if (workspaceId === null || workspaceName === null) {
      return;
    }
    setName(workspaceName);
    setSubmitting(false);
    setError(null);
  }, [workspaceId, workspaceName]);

  if (!workspace) {
    return null;
  }

  const handleSubmit = async () => {
    const normalizedName = name.trim();
    if (!normalizedName) {
      setError(t("gateway.workspaceRename.nameRequired"));
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(gatewayWorkspaceKey(workspace), normalizedName);
      onClose();
    } catch (submitError) {
      const message = submitError instanceof HttpRequestError && submitError.requestId
        ? `${errorMessage(submitError)} · request_id=${submitError.requestId}` : errorMessage(submitError);
      setError(t("gateway.workspaceDetails.actionFailed", workspace.gateway_id, workspace.workspace_id, message));
    } finally {
      setSubmitting(false);
    }
  };

  return createPortal(
    <div className="workspace-dialog-backdrop" role="presentation">
      <form
        className="workspace-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="workspace-rename-dialog-title"
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        <header className="workspace-dialog-header workspace-dialog-heading">
          <div>
            <h2 id="workspace-rename-dialog-title">{t("gateway.workspaceRename.title")}</h2>
            <p title={workspace.workspace_id}>{workspace.workspace_id}</p>
          </div>
          <button
            type="button"
            className="workspace-dialog-icon-button"
            aria-label={t("gateway.workspaceRename.close")}
            onClick={onClose}
            disabled={submitting}
          >
            ×
          </button>
        </header>
        <div className="workspace-dialog-grid">
          <label className="workspace-dialog-wide">
            <span>{t("gateway.workspaceRename.workspaceName")}</span>
            <input
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              disabled={submitting}
            />
          </label>
        </div>
        {error ? (
          <div className="workspace-dialog-error" role="alert">
            {error}
          </div>
        ) : null}
        <footer className="workspace-dialog-actions">
          <button type="button" onClick={onClose} disabled={submitting}>
            {t("gateway.workspaceRename.cancel")}
          </button>
          <button
            type="submit"
            className="workspace-dialog-primary"
            disabled={submitting || !name.trim()}
          >
            {submitting ? t("gateway.workspaceRename.saving") : t("gateway.workspaceRename.save")}
          </button>
        </footer>
      </form>
    </div>,
    overlayRoot ?? document.body,
  );
}
