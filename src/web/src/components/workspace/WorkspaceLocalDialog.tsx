import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { browseGatewayLocalDirectories } from "../../gatewayApi";
import type {
  AddLocalGatewayWorkspaceRequest,
  GatewayWorkspace,
  LocalDirectoryList,
} from "../../types/backend";

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
  const [browserListing, setBrowserListing] = useState<LocalDirectoryList | null>(null);
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
          <section className="workspace-dialog-wide workspace-directory-browser">
            <header className="workspace-directory-browser-header">
              <span>选择目录</span>
              <div className="workspace-directory-browser-actions">
                {browserListing?.parent_path ? (
                  <button
                    type="button"
                    onClick={() => loadDirectory(browserListing.parent_path ?? null)}
                    disabled={browserLoading}
                  >
                    上一级
                  </button>
                ) : null}
                <button
                  type="button"
                  onClick={() => loadDirectory(form.rootPath.trim() || null)}
                  disabled={browserLoading}
                >
                  刷新
                </button>
                <button
                  type="button"
                  onClick={() => loadDirectory(browserListing?.home_path ?? null)}
                  disabled={browserLoading}
                >
                  主目录
                </button>
              </div>
            </header>
            <div className="workspace-directory-current" title={browserListing?.path ?? form.rootPath}>
              {(browserListing?.path ?? form.rootPath) || "主目录"}
            </div>
            {browserError ? (
              <div className="workspace-directory-error">{browserError}</div>
            ) : null}
            <div className="workspace-directory-list" aria-busy={browserLoading}>
              {browserLoading ? (
                <div className="workspace-directory-empty">正在读取目录...</div>
              ) : browserListing && browserListing.entries.length > 0 ? (
                browserListing.entries.map((entry) => (
                  <button
                    key={entry.path}
                    type="button"
                    className="workspace-directory-row"
                    onClick={() => loadDirectory(entry.path)}
                    title={entry.path}
                  >
                    <span className="codicon codicon-folder" aria-hidden="true" />
                    <span>{entry.name}</span>
                  </button>
                ))
              ) : (
                <div className="workspace-directory-empty">该目录下没有可进入的子目录</div>
              )}
            </div>
            {browserListing?.truncated ? (
              <div className="workspace-directory-hint">
                仅显示前 {browserListing.limit} 个目录，可在输入框中继续输入精确路径。
              </div>
            ) : null}
          </section>
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
