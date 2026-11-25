import { gatewayWorkspaceKey, nullableGatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { useContext, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { t } from "../../../../shared/core/i18n/index.js";
import { OverlayRootContext } from "../overlays/OverlayRootContext";
import type { GatewayWorkspace } from "../../types/backend";
import { errorSummary } from "../../utils/errorMessage";

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
  const [error, setError] = useState<unknown>(null);
  const workspaceId = nullableGatewayWorkspaceKey(workspace);
  const currentTarget = useRef({ key: workspaceId });
  if (currentTarget.current.key !== workspaceId) currentTarget.current = { key: workspaceId };
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
    const snapshot = currentTarget.current;
    const normalizedName = name.trim();
    if (!normalizedName) {
      setError(t("gateway.workspaceRename.nameRequired"));
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(gatewayWorkspaceKey(workspace), normalizedName);
      if (currentTarget.current === snapshot) onClose();
    } catch (submitError) {
      if (currentTarget.current === snapshot) setError(submitError);
    } finally {
      if (currentTarget.current === snapshot) setSubmitting(false);
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
          <div className="workspace-dialog-error failure-summary" role="alert">
            {errorSummary(error)}
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
