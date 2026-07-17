import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { browseGatewayLocalDirectories } from "../../gatewayApi";
import type {
  AddLocalGatewayWorkspaceRequest,
  GatewayDirectoryList,
  GatewayWorkspace,
} from "../../types/backend";
import WorkspaceDirectoryBrowser from "./WorkspaceDirectoryBrowser";

interface WorkspaceLocalDialogProps {
  open: boolean;
  apiPort: number;
  workspaces: GatewayWorkspace[];
  activeWorkspaceId: string | null;
  recentLocalWorkspacePaths: string[];
  onClose: () => void;
  onSubmit: (payload: AddLocalGatewayWorkspaceRequest) => Promise<void>;
}

interface WorkspaceLocalFormState {
  rootPath: string;
  name: string;
  backendUrl: string;
}

function required(value: string, label: string): string {
  const trimmed = value.trim();
  if (!trimmed) {
    throw new Error(`${label} 不能为空`);
  }
  return trimmed;
}

function parentPath(path: string): string {
  const normalized = path.trim().replace(/[\\/]+$/, "");
  if (!normalized) {
    return "";
  }
  const slashIndex = Math.max(
    normalized.lastIndexOf("/"),
    normalized.lastIndexOf("\\"),
  );
  if (slashIndex <= 0) {
    return normalized;
  }
  return normalized.slice(0, slashIndex);
}

function uniqueSuggestions(values: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values) {
    const trimmed = value.trim();
    if (!trimmed || seen.has(trimmed)) {
      continue;
    }
    seen.add(trimmed);
    result.push(trimmed);
  }
  return result;
}

export default function WorkspaceLocalDialog({
  open,
  apiPort,
  workspaces,
  activeWorkspaceId,
  recentLocalWorkspacePaths,
  onClose,
  onSubmit,
}: WorkspaceLocalDialogProps) {
  const activeWorkspace = useMemo(
    () => workspaces.find((workspace) => workspace.workspace_id === activeWorkspaceId),
    [activeWorkspaceId, workspaces],
  );
  const suggestions = useMemo(
    () =>
      uniqueSuggestions([
        activeWorkspace?.root_path ?? "",
        parentPath(activeWorkspace?.root_path ?? ""),
        ...recentLocalWorkspacePaths,
        ...workspaces
          .filter((workspace) => workspace.connection_kind === "local")
          .map((workspace) => workspace.root_path),
      ]),
    [activeWorkspace?.root_path, recentLocalWorkspacePaths, workspaces],
  );
  const [form, setForm] = useState<WorkspaceLocalFormState>({
    rootPath: "",
    name: "",
    backendUrl: "",
  });
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [browserListing, setBrowserListing] = useState<GatewayDirectoryList | null>(null);
  const [browserLoading, setBrowserLoading] = useState(false);
  const [browserError, setBrowserError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    const initialPath = suggestions[0] || "";
    setForm((current) => ({
      ...current,
      rootPath: current.rootPath.trim() || initialPath,
    }));
    setError(null);
    setSubmitting(false);
    setBrowserError(null);
    setBrowserListing(null);
    void loadDirectory(initialPath || null);
  }, [open, suggestions]);

  if (!open) {
    return null;
  }

  const update = (key: keyof WorkspaceLocalFormState, value: string) => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  const loadDirectory = async (path: string | null) => {
    setBrowserLoading(true);
    setBrowserError(null);
    try {
      const listing = await browseGatewayLocalDirectories(apiPort, path);
      setBrowserListing(listing);
      setForm((current) => ({
        ...current,
        rootPath: listing.path,
      }));
    } catch (loadError) {
      setBrowserError(loadError instanceof Error ? loadError.message : String(loadError));
    } finally {
      setBrowserLoading(false);
    }
  };

  const handleSubmit = async () => {
    setError(null);
    let payload: AddLocalGatewayWorkspaceRequest;
    try {
      payload = {
        root_path: required(form.rootPath, "本机工作区路径"),
        name: form.name.trim() || null,
        backend_url: form.backendUrl.trim() || null,
      };
    } catch (validationError) {
      setError(validationError instanceof Error ? validationError.message : String(validationError));
      return;
    }

    setSubmitting(true);
    try {
      await onSubmit(payload);
      setForm({
        rootPath: suggestions[0] || "",
        name: "",
        backendUrl: "",
      });
      onClose();
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : String(submitError));
    } finally {
      setSubmitting(false);
    }
  };

  return createPortal(
    <div className="workspace-dialog-backdrop" role="presentation">
      <section
        className="workspace-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="workspace-local-dialog-title"
      >
        <header className="workspace-dialog-header">
          <h2 id="workspace-local-dialog-title">添加本机工作区</h2>
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
            <span>本机工作区路径</span>
            <input
              autoFocus
              value={form.rootPath}
              onChange={(event) => update("rootPath", event.target.value)}
            />
          </label>
          {suggestions.length > 0 ? (
            <div className="workspace-dialog-wide workspace-path-suggestions">
              {suggestions.map((path) => (
                <button
                  key={path}
                  type="button"
                  onClick={() => update("rootPath", path)}
                  title={path}
                >
                  {path}
                </button>
              ))}
            </div>
          ) : null}
          <WorkspaceDirectoryBrowser
            listing={browserListing}
            currentPath={form.rootPath}
            loading={browserLoading}
            error={browserError}
            onNavigate={(path) => void loadDirectory(path)}
          />
          <label>
            <span>工作区名称</span>
            <input
              value={form.name}
              onChange={(event) => update("name", event.target.value)}
              placeholder="可留空"
            />
          </label>
          <label>
            <span>已有后端 URL</span>
            <input
              value={form.backendUrl}
              onChange={(event) => update("backendUrl", event.target.value)}
              placeholder="可留空，由 Gateway 启动"
            />
          </label>
        </div>
        {error ? <div className="workspace-dialog-error">{error}</div> : null}
        <footer className="workspace-dialog-actions">
          <button type="button" onClick={onClose} disabled={submitting}>
            取消
          </button>
          <button
            type="button"
            className="workspace-dialog-primary"
            onClick={handleSubmit}
            disabled={submitting}
          >
            {submitting ? "添加中" : "添加"}
          </button>
        </footer>
      </section>
    </div>,
    document.body,
  );
}
