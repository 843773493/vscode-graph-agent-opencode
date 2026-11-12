import { lazy, Suspense, useContext } from "react";
import { createPortal } from "react-dom";
import { t } from "../../../../shared/core/i18n/index.js";
import { OverlayRootContext } from "../overlays/OverlayRootContext";

import type {
  AddManagedGatewayWorkspaceRequest,
  GatewayWorkspace,
} from "../../types/backend";

export interface WorkspaceAddDialogProps {
  open: boolean;
  apiPort: number;
  workspaces: GatewayWorkspace[];
  onClose: () => void;
  onOpenConnectionManager: () => void;
  onAdd: (payload: AddManagedGatewayWorkspaceRequest) => Promise<void>;
}

const WorkspaceAddDialogContent = lazy(() => import("./workspaceAddDialog/Content"));

function WorkspaceAddDialogLoading({ onClose }: Pick<WorkspaceAddDialogProps, "onClose">) {
  const overlayRoot = useContext(OverlayRootContext);
  return createPortal(
    <div
      className="workspace-add-overlay"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        className="workspace-add-panel workspace-add-two-level"
        role="dialog"
        aria-modal="true"
        aria-labelledby="workspace-add-title"
        aria-busy="true"
      >
        <header className="workspace-add-header">
          <div className="workspace-add-dialog-heading">
            <div>
              <h2 id="workspace-add-title">{t("gateway.workspaceAdd.selectDevice")}</h2>
              <p>{t("gateway.workspaceAdd.preparingSelector")}</p>
            </div>
            <button
              type="button"
              className="workspace-add-icon-button workspace-add-close-button"
              aria-label={t("gateway.workspaceAdd.close")}
              onClick={onClose}
            >
              <span className="codicon codicon-close" aria-hidden="true" />
            </button>
          </div>
        </header>
        <div className="workspace-add-results" role="status">
          <div className="workspace-add-state">{t("gateway.workspaceAdd.loadingDevices")}</div>
        </div>
      </section>
    </div>,
    overlayRoot ?? document.body,
  );
}

export default function WorkspaceAddDialog(props: WorkspaceAddDialogProps) {
  if (!props.open) return null;
  return (
    <Suspense fallback={<WorkspaceAddDialogLoading onClose={props.onClose} />}>
      <WorkspaceAddDialogContent {...props} />
    </Suspense>
  );
}
