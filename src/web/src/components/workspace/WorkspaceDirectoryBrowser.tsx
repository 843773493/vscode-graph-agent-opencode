import type { GatewayDirectoryList } from "../../types/backend";

interface WorkspaceDirectoryBrowserProps {
  listing: GatewayDirectoryList | null;
  currentPath: string;
  loading: boolean;
  error: string | null;
  onNavigate: (path: string | null) => void;
}

export default function WorkspaceDirectoryBrowser({
  listing,
  currentPath,
  loading,
  error,
  onNavigate,
}: WorkspaceDirectoryBrowserProps) {
  return (
    <section className="workspace-dialog-wide workspace-directory-browser">
      <header className="workspace-directory-browser-header">
        <span>选择目录</span>
        <div className="workspace-directory-browser-actions">
          {listing?.parent_path ? (
            <button
              type="button"
              onClick={() => onNavigate(listing.parent_path ?? null)}
              disabled={loading}
              title="返回上一级目录"
            >
              <span className="codicon codicon-arrow-left" aria-hidden="true" />
              上一级
            </button>
          ) : null}
          <button
            type="button"
            onClick={() => onNavigate(currentPath.trim() || null)}
            disabled={loading}
            title="刷新当前输入路径"
          >
            <span className="codicon codicon-refresh" aria-hidden="true" />
            刷新
          </button>
          <button
            type="button"
            onClick={() => onNavigate(listing?.home_path ?? null)}
            disabled={loading}
            title="前往用户主目录"
          >
            <span className="codicon codicon-home" aria-hidden="true" />
            主目录
          </button>
        </div>
      </header>
      <div className="workspace-directory-current" title={listing?.path ?? currentPath}>
        {(listing?.path ?? currentPath) || "主目录"}
      </div>
      {error ? <div className="workspace-directory-error">{error}</div> : null}
      <div className="workspace-directory-list" aria-busy={loading}>
        {loading ? (
          <div className="workspace-directory-empty">正在读取目录...</div>
        ) : listing && listing.entries.length > 0 ? (
          listing.entries.map((entry) => (
            <button
              key={entry.path}
              type="button"
              className="workspace-directory-row"
              onClick={() => onNavigate(entry.path)}
              title={entry.path}
            >
              <span className="codicon codicon-folder" aria-hidden="true" />
              <span className="workspace-directory-name">{entry.name}</span>
              <span className="codicon codicon-chevron-right" aria-hidden="true" />
            </button>
          ))
        ) : (
          <div className="workspace-directory-empty">该目录下没有可进入的子目录</div>
        )}
      </div>
      {listing?.truncated ? (
        <div className="workspace-directory-hint">
          仅显示前 {listing.limit} 个目录，可在路径框中输入精确路径。
        </div>
      ) : null}
    </section>
  );
}
