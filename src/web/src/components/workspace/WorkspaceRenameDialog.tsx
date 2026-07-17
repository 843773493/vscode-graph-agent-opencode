import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import type { GatewayWorkspace } from "../../types/backend";

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
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!workspace) {
      return;
    }
    setName(workspace.name);
    setSubmitting(false);
    setError(null);
  }, [workspace]);

  if (!workspace) {
    return null;
  }

  const handleSubmit = async () => {
    const normalizedName = name.trim();
    if (!normalizedName) {
      setError("工作区名称不能为空");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(workspace.workspace_id, normalizedName);
      onClose();
    } catch (submitError) {
      setError(
        submitError instanceof Error ? submitError.message : String(submitError),
      );
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
            <h2 id="workspace-rename-dialog-title">重命名工作区</h2>
            <p title={workspace.root_path}>{workspace.root_path}</p>
          </div>
          <button
            type="button"
            className="workspace-dialog-icon-button"
            aria-label="关闭"
            onClick={onClose}
            disabled={submitting}
          >
            ×
          </button>
        </header>
        <div className="workspace-dialog-grid">
          <label className="workspace-dialog-wide">
            <span>工作区名称</span>
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
            取消
          </button>
          <button
            type="submit"
            className="workspace-dialog-primary"
            disabled={submitting || !name.trim()}
          >
            {submitting ? "保存中" : "保存"}
          </button>
        </footer>
      </form>
    </div>,
    document.body,
  );
}
